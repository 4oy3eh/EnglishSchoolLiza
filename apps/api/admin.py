"""Admin (teacher) HTTP surface (Phase 10).

Wires `AdminService` behind a required bearer token. Every route below
(except `POST /admin/login`) depends on `require_teacher`, so auth is enforced
platform-wide for the dashboard: bank, review queue + approve (the publish gate),
roster, and results.

Auth model (see `app/admin/auth.py`): the teacher posts the shared password to
`/admin/login` and gets a short-lived HMAC-signed token; the dashboard sends it as
`Authorization: Bearer <token>` on every subsequent call. Wrong passwords are
rate-limited per client IP and globally (429). While the password or token secret
is still the public dev default the whole surface refuses to work (503, loud
ERROR log) — fail-closed: the only exception is an explicitly declared dev/test
environment AND a request made on this machine (loopback, not relayed).

Client IP caveat: `request.client.host` is the socket peer unless uvicorn resolves
a proxy's `X-Forwarded-For` (`--proxy-headers`, on by default, trusted only from
`--forwarded-allow-ips`, default 127.0.0.1). Behind a proxy that is not trusted
there — or that sends no such header — every client shares the proxy's address as
ONE rate-limit key; see `app/admin/CLAUDE.md`.

`detail` strings on errors are shown to the teacher verbatim, so they are Russian.

Uploads (`POST /admin/tests/import`, `POST /admin/assets`) are bounded BEFORE the
body is buffered: `UploadLimitMiddleware` (added in `apps/api/main.py`) refuses a
declared `Content-Length` over the route's cap with 413 without reading anything,
and counts the streamed bytes otherwise (a missing or lying `Content-Length`) —
413 as soon as the cap is crossed. The multipart form is parsed by the route's own
dependency AFTER the teacher check (so an anonymous client never gets a body
spooled) with Starlette's `max_files` / `max_fields` set; the parts stay spooled
temp files and are streamed to storage, never read whole into memory.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from functools import lru_cache
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.admin import (
    AdminError,
    AdminService,
    AssetUploadResult,
    AttemptOverview,
    AttemptResult,
    AuthError,
    BulkAddResult,
    ImportResult,
    LoginRateLimiter,
    ReviewDraft,
    RosterStatus,
    StudentReport,
    TestStats,
    TokenSigner,
    build_admin_service,
    insecure_defaults,
    is_local_request,
    verify_password,
)
from app.content.editor import MAX_ASSET_BYTES
from app.content.storage import FilesystemStorage
from app.core.config import (
    DEV_ADMIN_TOKEN_SECRET,
    DEV_ENVIRONMENTS,
    DEV_TEACHER_PASSWORD,
    settings,
)
from app.core.db import get_session
from app.core.logging import get_logger
from app.feedback.llm import FeedbackLLM
from app.grading.llm import LLMGrader
from contracts import ClientTest, RosterEntry, Test

log = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# One signer for the process, keyed by the configured secret + TTL.
_signer = TokenSigner(settings.admin_token_secret, ttl_seconds=settings.admin_token_ttl_seconds)
_bearer = HTTPBearer(auto_error=False)
# Failed-login limiter: per client IP + a global cap (the real brute-force bound).
# The key is `request.client.host` — see the client-IP caveat in the module doc.
login_limiter = LoginRateLimiter(
    max_failures=settings.login_max_failures,
    window_seconds=settings.login_window_seconds,
    global_max_failures=settings.login_global_max_failures,
)

_INSECURE_DETAIL = (
    "Вход отключён: сервер запущен с паролем/секретом по умолчанию. Администратор "
    "должен задать TEACHER_PASSWORD и ADMIN_TOKEN_SECRET."
)
_INSECURE_REMOTE_DETAIL = (
    "Вход с паролем/секретом по умолчанию разрешён только с того же компьютера, где "
    "запущен сервер (localhost). Для входа с другого устройства или через туннель "
    "администратор должен задать TEACHER_PASSWORD и ADMIN_TOKEN_SECRET."
)


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _is_dev_environment() -> bool:
    return settings.environment.strip().lower() in DEV_ENVIRONMENTS


def _insecure_settings(*, local_request: bool) -> list[str]:
    """Auth settings still on the public dev default and not excused (re-read live).

    Excused only in an explicit dev/test environment for a local request."""
    return insecure_defaults(
        environment=settings.environment,
        teacher_password=settings.teacher_password,
        admin_token_secret=settings.admin_token_secret,
        dev_password=DEV_TEACHER_PASSWORD,
        dev_secret=DEV_ADMIN_TOKEN_SECRET,
        dev_environments=DEV_ENVIRONMENTS,
        local_request=local_request,
    )


def _refuse_if_insecure(action: str, request: Request) -> None:
    """503 (+ ERROR log) while the dev default password/secret is in use, unless this
    is an explicit dev/test environment AND the request is local. Fail-closed."""
    local = is_local_request(request.client.host if request.client else None, request.headers)
    bad = _insecure_settings(local_request=local)
    if not bad:
        return
    if _is_dev_environment():
        log.error(
            "admin %s REFUSED: %s still the dev default and the request is not local "
            "(client=%s, environment=%r) — the dev defaults only work from this machine; "
            "set them in the env to use the dashboard remotely",
            action,
            "+".join(bad),
            _client_key(request),
            settings.environment,
        )
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _INSECURE_REMOTE_DETAIL)
    log.error(
        "admin %s REFUSED: %s still the dev default in environment=%r — set them in the "
        "env (local development: ENVIRONMENT=dev in .env, see .env.example)",
        action,
        "+".join(bad),
        settings.environment,
    )
    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, _INSECURE_DETAIL)


def _log_startup_auth_state() -> None:  # pragma: no cover - depends on the process env
    bad = _insecure_settings(local_request=False)
    if not bad:
        return
    if _is_dev_environment():
        log.warning(
            "admin auth uses the DEV DEFAULT %s (environment=%r): teacher login works "
            "ONLY from this machine (loopback, not through a tunnel/proxy) — set them "
            "in the env before sharing the dashboard",
            "+".join(bad),
            settings.environment,
        )
    else:
        log.error(
            "admin auth uses the DEV DEFAULT %s in environment=%r: teacher login is "
            "disabled until they are overridden (local development: ENVIRONMENT=dev in "
            ".env, see .env.example)",
            "+".join(bad),
            settings.environment,
        )


_log_startup_auth_state()


# --------------------------------------------------------------------------- #
# Auth.
# --------------------------------------------------------------------------- #
class LoginRequest(BaseModel):
    password: str


class TokenResponse(BaseModel):
    token: str


def require_teacher(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    """Resolve only for a valid, unexpired teacher token; else 401.

    On the public dev defaults every token is refused (503) — the default secret is
    known, so a token signed with it proves nothing — except for a local request in
    an explicit dev/test environment."""
    _refuse_if_insecure("request", request)
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Нужно войти заново.")
    try:
        return _signer.verify(creds.credentials)
    except AuthError as exc:
        log.warning("admin auth rejected: %s", exc)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Сессия истекла — войдите заново."
        ) from exc


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request) -> TokenResponse:
    """Exchange the shared teacher password for a signed bearer token.

    Refused (503) on the dev defaults (see `_refuse_if_insecure`). Too many recent
    wrong passwords — from this client IP, or from everyone together — get 429
    (+ Retry-After) without the password even being checked. The lockout check,
    the password check and the failure count are one atomic step
    (`LoginRateLimiter.attempt`), so parallel guesses cannot outrun the limit."""
    _refuse_if_insecure("login", request)
    key = _client_key(request)
    decision = login_limiter.attempt(
        key, lambda: verify_password(body.password, settings.teacher_password)
    )
    if decision.retry_after:
        log.warning(
            "admin login rate-limited ip=%s scope=%s retry_after=%ds",
            key,
            decision.scope,
            decision.retry_after,
        )
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Слишком много неудачных попыток входа. Подождите "
            f"{-(-decision.retry_after // 60)} мин.",
            headers={"Retry-After": str(decision.retry_after)},
        )
    if not decision.ok:
        log.warning(
            "admin login rejected: bad password ip=%s (%d/%d for this ip, %d/%s overall "
            "in window)",
            key,
            decision.failures,
            login_limiter.max_failures,
            decision.global_failures,
            login_limiter.global_max_failures or "-",
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный пароль.")
    log.info("admin login ok ip=%s", key)
    return TokenResponse(token=_signer.mint())


# --------------------------------------------------------------------------- #
# Service wiring.
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _writing_grader() -> LLMGrader | None:
    """The shared writing grader: Gemini when a key is configured, else manual."""
    if settings.gemini_api_key:
        from app.grading.llm_gemini import GeminiWritingGrader

        log.info("writing auto-grading enabled via Gemini (%s)", settings.gemini_model)
        return GeminiWritingGrader(
            api_key=settings.gemini_api_key, model=settings.gemini_model
        )
    return None


@lru_cache(maxsize=1)
def _feedback_analyst() -> FeedbackLLM | None:
    """The shared per-skill feedback analyst: Gemini when configured, else none."""
    if settings.gemini_api_key:
        from app.feedback.llm_gemini import GeminiFeedbackAnalyst

        log.info("skill feedback enabled via Gemini (%s)", settings.gemini_model)
        return GeminiFeedbackAnalyst(
            api_key=settings.gemini_api_key, model=settings.gemini_model
        )
    return None


def get_admin_service(
    session: Annotated[Session, Depends(get_session)],
) -> Iterator[AdminService]:
    yield build_admin_service(
        session,
        FilesystemStorage(settings.assets_dir),
        llm_grader=_writing_grader(),
        feedback_llm=_feedback_analyst(),
    )


AdminDep = Annotated[AdminService, Depends(get_admin_service)]
_Teacher = Depends(require_teacher)


def _http(exc: AdminError) -> HTTPException:
    """Map an `AdminError` to HTTP: 404 unknown target, 400 bad value, 409 wrong state."""
    return HTTPException(exc.status, str(exc))


# --------------------------------------------------------------------------- #
# Bank.
# --------------------------------------------------------------------------- #
@router.get("/tests", response_model=list[Test], dependencies=[_Teacher])
def list_tests(admin: AdminDep) -> list[Test]:
    return admin.list_tests()


@router.get("/tests/{test_id}", response_model=Test, dependencies=[_Teacher])
def get_test(test_id: str, admin: AdminDep) -> Test:
    try:
        return admin.get_test(test_id)
    except AdminError as exc:
        raise _http(exc) from exc


@router.get("/tests/{test_id}/stats", response_model=TestStats, dependencies=[_Teacher])
def test_stats(test_id: str, admin: AdminDep) -> TestStats:
    """Roster / attempt counts — what a delete or unpublish would affect."""
    try:
        return admin.test_stats(test_id)
    except AdminError as exc:
        raise _http(exc) from exc


@router.delete("/tests/{test_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_Teacher])
def delete_test(test_id: str, admin: AdminDep, force: bool = False) -> None:
    """Delete a test. 409 while it has student attempts unless `?force=true`
    (which wipes them with it)."""
    try:
        admin.delete_test(test_id, force=force)
    except AdminError as exc:
        raise _http(exc) from exc


# --------------------------------------------------------------------------- #
# Content editor (no code): update / reorder / add / remove, copy, import/export,
# asset upload, keyless preview. Bodies are plain JSON objects validated by the
# content engine against the contracts, so a bad value is a 400 with a Russian
# message (not FastAPI's English 422). A published test only takes cosmetic text
# fixes, and only with `?force=true`; key / structure changes need a draft.
# --------------------------------------------------------------------------- #
# Upload caps (module-level so tests can shrink them; read at request time).
_MB = 1024 * 1024
ASSET_MAX_BYTES = MAX_ASSET_BYTES  # one asset file (service check, 400)
IMPORT_JSON_MAX_BYTES = 5 * _MB  # the Test JSON part of an import (400)
# Whole-request caps enforced by `UploadLimitMiddleware` before the body is
# buffered (413): one asset + multipart overhead, and a whole import.
ASSET_REQUEST_MAX_BYTES = ASSET_MAX_BYTES + 5 * _MB
IMPORT_REQUEST_MAX_BYTES = 200 * _MB
IMPORT_MAX_FILES = 200  # asset files per import (the Test JSON comes on top)
FORM_MAX_FIELDS = 8  # non-file form fields (none are expected)

IMPORT_PATH = "/admin/tests/import"
ASSET_UPLOAD_PATH = "/admin/assets"

_JsonObject = Annotated[dict[str, Any], Body()]


def upload_limit(method: str, path: str) -> tuple[int, str] | None:
    """(byte cap, Russian 413 message) for an upload route, else None."""
    if method != "POST":
        return None
    if path == ASSET_UPLOAD_PATH:
        cap = ASSET_REQUEST_MAX_BYTES
        return cap, (
            f"Файл слишком большой: максимум {ASSET_MAX_BYTES // _MB} МБ за одну загрузку."
        )
    if path == IMPORT_PATH:
        cap = IMPORT_REQUEST_MAX_BYTES
        return cap, (
            f"Импорт слишком большой: максимум {cap // _MB} МБ вместе со всеми файлами. "
            "Загрузите крупные файлы заранее («Загрузить файл…» в редакторе) и "
            "импортируйте тест без них."
        )
    return None


class _BodyTooLarge(Exception):
    """Raised from `receive` once the streamed body crosses the route's cap."""


class UploadLimitMiddleware:
    """Pure-ASGI request-body cap for the admin upload routes (`upload_limit`).

    * declared `Content-Length` over the cap -> 413 at once, the body is never read;
    * otherwise every `http.request` chunk is counted as the app reads it; crossing
      the cap raises inside `receive`, whatever response the app then tries to send
      is dropped and a 413 is sent instead (covers chunked bodies and a lying
      `Content-Length`).
    Other routes pass through untouched."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        root = scope.get("root_path", "")
        if root and path.startswith(root):
            path = path[len(root) :]
        rule = upload_limit(scope.get("method", ""), path)
        if rule is None:
            await self.app(scope, receive, send)
            return
        cap, message = rule
        declared = _content_length(scope)
        if declared is not None and declared > cap:
            log.warning(
                "upload refused -> 413 path=%s content-length=%d cap=%d (body not read)",
                path,
                declared,
                cap,
            )
            await _send_413(send, message)
            return

        received = 0
        exceeded = False
        started = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body", b""))
                if received > cap:
                    exceeded = True
                    raise _BodyTooLarge
            return msg

        async def guarded_send(msg: Message) -> None:
            nonlocal started
            if exceeded:
                return  # the app's (error) response is replaced by our 413
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not exceeded:
                raise
        if exceeded:
            log.warning(
                "upload refused -> 413 path=%s streamed over cap=%d (declared=%s)",
                path,
                cap,
                declared,
            )
            if not started:
                await _send_413(send, message)


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _send_413(send: Send, message: str) -> None:
    body = json.dumps({"detail": message}, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status.HTTP_413_CONTENT_TOO_LARGE,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _parse_form(request: Request, *, max_files: int, what: str) -> FormData:
    """Parse the multipart body with Starlette's file / field limits (Russian 400)."""
    try:
        return await request.form(max_files=max_files, max_fields=FORM_MAX_FIELDS)
    except StarletteHTTPException as exc:
        detail = str(exc.detail)
        log.warning("admin %s refused: multipart parse failed: %s", what, detail)
        if "Too many files" in detail:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Слишком много файлов за один раз: максимум {IMPORT_MAX_FILES}."
                if what == "import"
                else "Загружайте по одному файлу.",
            ) from exc
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Не удалось прочитать загруженные файлы (multipart): "
            + ("слишком много полей формы." if "Too many fields" in detail else detail),
        ) from exc


async def _import_form(request: Request) -> AsyncIterator[FormData]:
    form = await _parse_form(request, max_files=IMPORT_MAX_FILES + 1, what="import")
    try:
        yield form
    finally:
        await form.close()


async def _asset_form(request: Request) -> AsyncIterator[FormData]:
    form = await _parse_form(request, max_files=1, what="upload")
    try:
        yield form
    finally:
        await form.close()


def _file_part(form: FormData, name: str, missing: str) -> StarletteUploadFile:
    part = form.get(name)
    if not isinstance(part, StarletteUploadFile):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, missing)
    return part


class OrderRequest(BaseModel):
    order: list[str]


class AddItemRequest(BaseModel):
    item: dict[str, Any]
    position: int | None = None


class AddSectionRequest(BaseModel):
    section: dict[str, Any]
    position: int | None = None


def _read_capped(upload: StarletteUploadFile, cap: int, what: str) -> bytes:
    """Read an upload, refusing (400, Russian) anything larger than `cap` bytes."""
    data = upload.file.read(cap + 1)
    if len(data) > cap:
        log.warning("admin upload refused: %s %r over %d bytes", what, upload.filename, cap)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Файл «{upload.filename}» слишком большой: максимум {cap // (1024 * 1024)} МБ.",
        )
    return data


@router.patch("/tests/{test_id}", response_model=Test, dependencies=[_Teacher])
def update_test_meta(
    test_id: str, body: _JsonObject, admin: AdminDep, force: bool = False
) -> Test:
    """Change title / level / duration_minutes."""
    try:
        return admin.update_test_meta(test_id, body, force=force)
    except AdminError as exc:
        raise _http(exc) from exc


@router.patch(
    "/tests/{test_id}/sections/{section_id}", response_model=Test, dependencies=[_Teacher]
)
def update_section(
    test_id: str, section_id: str, body: _JsonObject, admin: AdminDep, force: bool = False
) -> Test:
    """Change a section's title / skill / stimulus."""
    try:
        return admin.update_section(test_id, section_id, body, force=force)
    except AdminError as exc:
        raise _http(exc) from exc


@router.post("/tests/{test_id}/sections/reorder", response_model=Test, dependencies=[_Teacher])
def reorder_sections(test_id: str, body: OrderRequest, admin: AdminDep) -> Test:
    try:
        return admin.reorder_sections(test_id, body.order)
    except AdminError as exc:
        raise _http(exc) from exc


@router.post(
    "/tests/{test_id}/sections/{section_id}/items/reorder",
    response_model=Test,
    dependencies=[_Teacher],
)
def reorder_items(test_id: str, section_id: str, body: OrderRequest, admin: AdminDep) -> Test:
    """Only for a section without a shared stimulus (golden rule #7) — else 409."""
    try:
        return admin.reorder_items(test_id, section_id, body.order)
    except AdminError as exc:
        raise _http(exc) from exc


@router.post("/tests/{test_id}/sections", response_model=Test, dependencies=[_Teacher])
def add_section(test_id: str, body: AddSectionRequest, admin: AdminDep) -> Test:
    try:
        return admin.add_section(test_id, body.section, position=body.position)
    except AdminError as exc:
        raise _http(exc) from exc


@router.delete(
    "/tests/{test_id}/sections/{section_id}", response_model=Test, dependencies=[_Teacher]
)
def remove_section(test_id: str, section_id: str, admin: AdminDep) -> Test:
    try:
        return admin.remove_section(test_id, section_id)
    except AdminError as exc:
        raise _http(exc) from exc


@router.post(
    "/tests/{test_id}/sections/{section_id}/items", response_model=Test, dependencies=[_Teacher]
)
def add_item(test_id: str, section_id: str, body: AddItemRequest, admin: AdminDep) -> Test:
    try:
        return admin.add_item(test_id, section_id, body.item, position=body.position)
    except AdminError as exc:
        raise _http(exc) from exc


@router.put("/tests/{test_id}/items/{item_id}", response_model=Test, dependencies=[_Teacher])
def update_item(
    test_id: str, item_id: str, body: _JsonObject, admin: AdminDep, force: bool = False
) -> Test:
    """Replace one item (the whole authoring item object; its id is kept)."""
    try:
        return admin.update_item(test_id, item_id, body, force=force)
    except AdminError as exc:
        raise _http(exc) from exc


@router.delete("/tests/{test_id}/items/{item_id}", response_model=Test, dependencies=[_Teacher])
def remove_item(test_id: str, item_id: str, admin: AdminDep) -> Test:
    try:
        return admin.remove_item(test_id, item_id)
    except AdminError as exc:
        raise _http(exc) from exc


@router.post("/tests/{test_id}/duplicate", response_model=Test, dependencies=[_Teacher])
def duplicate_test(test_id: str, admin: AdminDep) -> Test:
    """Save as a new draft copy `<id>-v2` (lands in the review queue)."""
    try:
        return admin.duplicate_test(test_id)
    except AdminError as exc:
        raise _http(exc) from exc


@router.get("/tests/{test_id}/export.json", dependencies=[_Teacher])
def export_test(test_id: str, admin: AdminDep) -> Response:
    """Download the authoring Test JSON WITH the answer key (teacher only)."""
    try:
        test = admin.export_test(test_id)
    except AdminError as exc:
        raise _http(exc) from exc
    return Response(
        content=test.model_dump_json(indent=2).encode("utf-8"),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{_safe(test_id)}.json"'},
    )


@router.get("/tests/{test_id}/preview", response_model=ClientTest, dependencies=[_Teacher])
def preview_test(test_id: str, admin: AdminDep) -> ClientTest:
    """The student-facing projection — structurally keyless (golden rule #1)."""
    try:
        return admin.preview_test(test_id)
    except AdminError as exc:
        raise _http(exc) from exc


@router.post(
    "/tests/import",
    response_model=ImportResult,
    dependencies=[_Teacher],
    openapi_extra={
        "requestBody": {
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["test"],
                        "properties": {
                            "test": {"type": "string", "format": "binary"},
                            "assets": {
                                "type": "array",
                                "items": {"type": "string", "format": "binary"},
                            },
                        },
                    }
                }
            }
        }
    },
)
def import_test(
    admin: AdminDep,
    form: Annotated[FormData, Depends(_import_form)],
    replace: bool = False,
    new_id: str | None = None,
) -> ImportResult:
    """Import a Test JSON (multipart `test`) + asset files (repeated `assets`, named
    by their asset id) as a DRAFT.

    Never publishes — the draft appears in the review queue. 409 on an id clash
    unless `?replace=true` and the existing test has no attempts (`unpublished` in
    the result says a published test went back to draft); 400 listing the missing
    asset ids when a reference resolves to neither an upload nor a stored asset;
    413 over `IMPORT_REQUEST_MAX_BYTES`; 400 over `IMPORT_MAX_FILES` files. The
    asset parts are streamed to storage (never held in memory together)."""
    test = _file_part(form, "test", "Приложите файл теста (JSON) в поле «test».")
    raw = _read_capped(test, IMPORT_JSON_MAX_BYTES, "test json")
    files: dict[str, Any] = {}
    for upload in form.getlist("assets"):
        if not isinstance(upload, StarletteUploadFile):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Поле «assets» должно содержать файлы."
            )
        files[upload.filename or ""] = upload.file
    try:
        return admin.import_test(raw, files, replace=replace, new_id=new_id)
    except AdminError as exc:
        raise _http(exc) from exc


@router.post(
    "/assets",
    response_model=AssetUploadResult,
    dependencies=[_Teacher],
    openapi_extra={
        "requestBody": {
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {"file": {"type": "string", "format": "binary"}},
                    }
                }
            }
        }
    },
)
def upload_asset(
    admin: AdminDep, form: Annotated[FormData, Depends(_asset_form)]
) -> AssetUploadResult:
    """Store one audio / image file (multipart `file`: mp3 / png / jpg / webp);
    returns the asset id to reference. Streamed to storage in chunks."""
    file = _file_part(form, "file", "Приложите файл в поле «file».")
    try:
        return admin.upload_asset(file.filename or "", file.file, max_bytes=ASSET_MAX_BYTES)
    except AdminError as exc:
        raise _http(exc) from exc


# --------------------------------------------------------------------------- #
# Review queue (the draft -> published human gate, rule #5).
# --------------------------------------------------------------------------- #
@router.get("/review", response_model=list[ReviewDraft], dependencies=[_Teacher])
def review_queue(admin: AdminDep) -> list[ReviewDraft]:
    return admin.review_queue()


@router.post("/review/{test_id}/approve", response_model=Test, dependencies=[_Teacher])
def approve(test_id: str, admin: AdminDep) -> Test:
    """Publish a reviewed draft (the ONLY path content goes live, rule #5)."""
    try:
        return admin.approve(test_id)
    except AdminError as exc:
        raise _http(exc) from exc


@router.post("/tests/{test_id}/unpublish", response_model=Test, dependencies=[_Teacher])
def unpublish(test_id: str, admin: AdminDep, force: bool = False) -> Test:
    """Published -> draft. 409 while students are mid-attempt unless `?force=true`."""
    try:
        return admin.unpublish(test_id, force=force)
    except AdminError as exc:
        raise _http(exc) from exc


# --------------------------------------------------------------------------- #
# Roster.
# --------------------------------------------------------------------------- #
class AddStudentRequest(BaseModel):
    display_name: str


@router.post("/tests/{test_id}/roster", response_model=RosterEntry, dependencies=[_Teacher])
def add_student(test_id: str, body: AddStudentRequest, admin: AdminDep) -> RosterEntry:
    try:
        return admin.add_student(test_id, body.display_name)
    except AdminError as exc:
        raise _http(exc) from exc


class BulkAddRequest(BaseModel):
    names: list[str] = Field(max_length=500)


@router.post(
    "/tests/{test_id}/roster/bulk", response_model=BulkAddResult, dependencies=[_Teacher]
)
def add_students_bulk(test_id: str, body: BulkAddRequest, admin: AdminDep) -> BulkAddResult:
    """Add many names at once; exact duplicates (on the roster or in the request)
    are skipped and listed in `skipped`."""
    try:
        return admin.add_students_bulk(test_id, body.names)
    except AdminError as exc:
        raise _http(exc) from exc


@router.get(
    "/tests/{test_id}/roster", response_model=list[RosterStatus], dependencies=[_Teacher]
)
def roster_status(test_id: str, admin: AdminDep) -> list[RosterStatus]:
    return admin.roster_status(test_id)


@router.post(
    "/roster/{roster_entry_id}/reset", response_model=RosterStatus, dependencies=[_Teacher]
)
def reset_attempt(roster_entry_id: str, admin: AdminDep) -> RosterStatus:
    """Discard a student's attempt so they can retake (destructive)."""
    try:
        return admin.reset_attempt(roster_entry_id)
    except AdminError as exc:
        raise _http(exc) from exc


class ReopenRequest(BaseModel):
    # Teacher-granted extra time: new deadline = max(old, now + N). Omitted -> the
    # original deadline keeps running.
    extend_minutes: int | None = Field(default=None, ge=1, le=24 * 60)


@router.post(
    "/roster/{roster_entry_id}/reopen", response_model=RosterStatus, dependencies=[_Teacher]
)
def reopen_attempt(
    roster_entry_id: str, admin: AdminDep, body: ReopenRequest | None = None
) -> RosterStatus:
    """Let a student back in keeping their progress; the deadline keeps running
    unless the teacher grants `extend_minutes`."""
    try:
        return admin.reopen_attempt(
            roster_entry_id, extend_minutes=body.extend_minutes if body else None
        )
    except AdminError as exc:
        raise _http(exc) from exc


@router.delete(
    "/roster/{roster_entry_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_Teacher]
)
def remove_student(roster_entry_id: str, admin: AdminDep) -> None:
    try:
        admin.remove_student(roster_entry_id)
    except AdminError as exc:
        raise _http(exc) from exc


# --------------------------------------------------------------------------- #
# Results (score + advisory verdict + raw replay, ranked suspicious-first).
# --------------------------------------------------------------------------- #
@router.get(
    "/tests/{test_id}/results", response_model=list[AttemptOverview], dependencies=[_Teacher]
)
def results_for_test(test_id: str, admin: AdminDep) -> list[AttemptOverview]:
    try:
        return admin.results_for_test(test_id)
    except AdminError as exc:
        raise _http(exc) from exc


@router.get("/tests/{test_id}/results.csv", dependencies=[_Teacher])
def results_csv(test_id: str, admin: AdminDep) -> Response:
    """Download the results table as CSV (UTF-8 with BOM so Excel reads Cyrillic).

    Stored data only — never triggers grading or an LLM call."""
    try:
        text = admin.results_csv(test_id)
    except AdminError as exc:
        raise _http(exc) from exc
    return Response(
        content=text.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="results-{_safe(test_id)}.csv"'},
    )


def _safe(name: str) -> str:
    """A filename-safe rendition of an id for Content-Disposition."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name) or "test"


@router.get(
    "/results/{attempt_id}/report", response_model=StudentReport, dependencies=[_Teacher]
)
def student_report(attempt_id: str, admin: AdminDep) -> StudentReport:
    """Printable student/parent report: scores + feedback + writing marks, no
    integrity data."""
    try:
        return admin.student_report(attempt_id)
    except AdminError as exc:
        raise _http(exc) from exc


@router.get("/results/{attempt_id}", response_model=AttemptResult, dependencies=[_Teacher])
def attempt_result(attempt_id: str, admin: AdminDep) -> AttemptResult:
    try:
        return admin.attempt_result(attempt_id)
    except AdminError as exc:
        raise _http(exc) from exc


@router.post(
    "/results/{attempt_id}/regrade", response_model=AttemptResult, dependencies=[_Teacher]
)
def regrade(attempt_id: str, admin: AdminDep) -> AttemptResult:
    """Re-run the one-shot grade + skill report for an attempt (synchronous).

    For an attempt submitted before this feature (no stored report) or whose feedback
    LLM previously failed. The student-submit path schedules this in the background;
    here the teacher waits for it. Runs the writing/feedback LLM — the only admin
    route that does."""
    try:
        return admin.regrade(attempt_id)
    except AdminError as exc:
        raise _http(exc) from exc


class FinalizeMissingResponse(BaseModel):
    attempt_ids: list[str]


@router.post(
    "/tests/{test_id}/finalize-missing",
    response_model=FinalizeMissingResponse,
    dependencies=[_Teacher],
)
def finalize_missing(test_id: str, admin: AdminDep) -> FinalizeMissingResponse:
    """Grade + report every finished attempt whose report is missing or failed.

    Recovery for a background finalize lost to a restart / outage, and a retry of
    every failed report (no cooldown — an explicit teacher action). Synchronous and
    LLM-backed, ONE transaction per attempt (`app.results.finalize.finalize_missing`,
    never a loop on this request's session); in-flight attempts are skipped.
    Returns the attempt ids processed."""
    try:
        return FinalizeMissingResponse(attempt_ids=admin.finalize_missing(test_id))
    except AdminError as exc:
        raise _http(exc) from exc


class ManualGradeRequest(BaseModel):
    awarded: float


@router.put(
    "/results/{attempt_id}/grades/{item_id}",
    response_model=AttemptResult,
    dependencies=[_Teacher],
)
def set_manual_grade(
    attempt_id: str, item_id: str, body: ManualGradeRequest, admin: AdminDep
) -> AttemptResult:
    """Record the teacher's mark for one item (writing score / gap ✓/✗ override)."""
    try:
        return admin.set_manual_grade(attempt_id, item_id, body.awarded)
    except AdminError as exc:
        raise _http(exc) from exc
