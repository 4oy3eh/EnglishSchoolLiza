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
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.admin import (
    AdminError,
    AdminService,
    AttemptOverview,
    AttemptResult,
    AuthError,
    BulkAddResult,
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
from contracts import RosterEntry, Test

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
