"""Student (delivery) HTTP surface (Phase 11).

The exam runtime's browser-facing API. Wires `DeliveryService` so the student
runner can: list the roster for a shared test link (pick-your-name), start (or
resume) the single attempt, fetch the keyless `ClientTest`, poll the
server-authoritative timer, save answers, and submit.

Golden rules enforced here:
* **#1 no answer key:** every served payload is a `Client*` projection — this
  router never returns an authoring `Test`/`Item` (those carry `correct`).
* **#3 server-authoritative timer:** `GET .../state` is the only source of truth
  for time remaining; the client clock is display-only. A crossed deadline
  expires the attempt server-side regardless of the browser.

No teacher auth: students reach this via the per-test share link, pick their name
(`roster_entry_id`) and type their 4-character classroom PIN (Phase 14). A good PIN
on `/start` returns a per-attempt session `token`; every `/exam/attempts/{id}/...`
call must carry it in the `X-Attempt-Token` header (missing / wrong -> 401, unknown
attempt -> 404). Wrong PINs are rate-limited in memory (`app.delivery.access`
`get_pin_limiter()`: per roster entry lock + a per-IP spray throttle -> 429 +
Retry-After; per-test monitor for the teacher). The roster list serves names and
statuses only — never PINs. The exam window is permissive in this phase (links are
always live); the per-attempt deadline is therefore `start + duration_minutes`.
Scheduled windows are a later concern and would live on the authoring `Test`.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.core.logging import get_logger
from app.delivery import (
    TOKEN_HEADER,
    AttemptExpiredError,
    AttemptState,
    AttemptStateError,
    DeliveryError,
    DeliveryService,
    ExamWindow,
    NotFoundError,
    PinLockedError,
    PinRequiredError,
    SessionTokenError,
    WindowClosedError,
    WindowNotOpenError,
    WindowState,
    WrongPinError,
    get_pin_limiter,
)
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import ClientItem, ClientTest, Test
from contracts.content import Level

log = get_logger(__name__)

router = APIRouter(prefix="/exam", tags=["exam"])

# The exam window comes from the test's own schedule (`ExamWindow.for_test`:
# `Test.opens_at` / `closes_at` / `late_start_grace_minutes`, set by the teacher on
# the dashboard). A test without a schedule is always open, so its deadline is
# driven by `duration_minutes` alone (golden rule #3 timer fully enforced either way).
# Header naming the window state on a 403 from `/start` (`not_yet_open` / `closed`),
# so the runner can tell it from a wrong PIN (also 403, with `X-Pin-Tries-Left`).
WINDOW_HEADER = "X-Exam-Window"

# --------------------------------------------------------------------------- #
# Response DTOs (read-side, never persisted; compose service output).
# --------------------------------------------------------------------------- #
class RosterChoice(BaseModel):
    """One pick-your-name option for the share-link landing screen."""

    roster_entry_id: str
    display_name: str
    status: str


class StateResponse(BaseModel):
    """Server-authoritative timer/state snapshot for the runner."""

    attempt_id: str
    status: str
    deadline: datetime | None
    remaining_seconds: int
    expired: bool
    # LEGACY single-track position (written only by an old runner) and the
    # per-track furthest positions {track asset_id: seconds} (per-part audio).
    audio_progress_seconds: int = 0
    audio_progress: dict[str, int] = Field(default_factory=dict)

    @classmethod
    def of(cls, state: AttemptState) -> StateResponse:
        return cls(
            attempt_id=state.attempt_id,
            status=state.status,
            deadline=state.deadline,
            remaining_seconds=state.remaining_seconds,
            expired=state.expired,
            audio_progress_seconds=state.audio_progress_seconds,
            audio_progress=dict(state.audio_progress),
        )


class TestInfo(BaseModel):
    """Public facts about a share-link test for the start screen (no content, no key).

    The runner uses `level` to decide whether to offer the YLE UI-language switch,
    and the window fields to show "Opens at …" / "Closed" before the PIN screen.
    `window_state` is computed on the server clock (`server_now`, authoritative);
    `starts_until` = `closes_at` + the late-start grace (None = never closes).
    """

    test_id: str
    title: str
    level: Level
    window_state: WindowState = "open"
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    starts_until: datetime | None = None
    server_now: datetime | None = None


class StartRequest(BaseModel):
    """The PIN the student typed (case-insensitive, spaces ignored)."""

    pin: str | None = None


class StartResponse(BaseModel):
    """What the runner needs to (re)hydrate an attempt after start/resume.

    `token` is this device's session for the attempt: send it as `X-Attempt-Token`
    on every later attempt call. Only its hash is stored server-side.
    """

    attempt_id: str
    test_id: str
    state: StateResponse
    token: str


class SaveAnswerRequest(BaseModel):
    """A single saved answer. `response` is the *displayed* option index (int)
    for single_choice, or the literal text / pool key (str) for everything else;
    delivery maps it to the canonical key before persisting (golden rule #1)."""

    response: str | int


class AudioProgressRequest(BaseModel):
    """The furthest position (seconds) reached in one listening track.

    `asset_id` names the track (per-part audio). Without it (a runner page loaded
    before per-part audio shipped) the value goes to the legacy single-track
    `attempts.audio_progress_seconds`, which the new runner applies to the first
    track — so an open tab survives the deploy.

    DEPRECATED: the legacy `{seconds}` form (no `asset_id`) is kept for one release
    only (tabs opened before per-part audio shipped) and will be removed in the next
    phase together with `attempts.audio_progress_seconds`.

    `seconds` is bounded (0..86400, a day — no recording is longer): an unbounded
    int (e.g. 1e30) overflowed SQLite's 64-bit integer (500). Strict int: NaN /
    Infinity / fractions are refused (422)."""

    seconds: int = Field(ge=0, le=86_400, strict=True)
    asset_id: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("seconds", mode="before")
    @classmethod
    def _json_safe_non_finite(cls, v: object) -> object:
        # Python's JSON parser accepts NaN / Infinity; the 422 body echoes the input
        # and Starlette refuses to serialise a non-finite float (-> 500). Swap it for
        # a JSON-safe marker so the strict int check answers a clean 422.
        if isinstance(v, float) and not math.isfinite(v):
            return "non-finite"
        return v


# --------------------------------------------------------------------------- #
# Service wiring.
# --------------------------------------------------------------------------- #
def get_delivery_service(
    session: Annotated[Session, Depends(get_session)],
) -> Iterator[DeliveryService]:
    yield DeliveryService(ContentRepository(session), AttemptRepository(session))


DeliveryDep = Annotated[DeliveryService, Depends(get_delivery_service)]


def _http(exc: DeliveryError) -> HTTPException:
    """Map a delivery rejection to the right HTTP status (distinguishable)."""
    headers: dict[str, str] | None = None
    if isinstance(exc, NotFoundError):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, SessionTokenError):
        code = status.HTTP_401_UNAUTHORIZED
    elif isinstance(exc, WrongPinError):
        code = status.HTTP_403_FORBIDDEN
        headers = {"X-Pin-Tries-Left": str(exc.tries_left)}
    elif isinstance(exc, PinLockedError):
        code = status.HTTP_429_TOO_MANY_REQUESTS
        headers = {"Retry-After": str(exc.retry_after)}
    elif isinstance(exc, PinRequiredError):
        code = status.HTTP_400_BAD_REQUEST
    elif isinstance(exc, WindowNotOpenError):
        code = status.HTTP_403_FORBIDDEN
        headers = {WINDOW_HEADER: "not_yet_open"}
        if exc.opens_at is not None:
            headers["X-Exam-Opens-At"] = exc.opens_at.isoformat()
    elif isinstance(exc, WindowClosedError):
        code = status.HTTP_403_FORBIDDEN
        headers = {WINDOW_HEADER: "closed"}
    elif isinstance(exc, (AttemptExpiredError, AttemptStateError)):
        code = status.HTTP_409_CONFLICT
    else:
        code = status.HTTP_400_BAD_REQUEST
    # Surface every rejection at the HTTP boundary (validation failures -> WARNING
    # per CLAUDE.md), since the service-layer logs success paths but not the
    # mapped status a client actually receives.
    log.warning("exam request rejected -> %d: %s", code, exc)
    return HTTPException(code, str(exc), headers=headers)


def require_attempt_token(
    attempt_id: str,
    svc: DeliveryDep,
    x_attempt_token: Annotated[str | None, Header(alias=TOKEN_HEADER)] = None,
) -> None:
    """Dependency for every `/attempts/{attempt_id}/...` route: a valid session token.

    Unknown attempt -> 404 (as before); missing / wrong token -> 401. This is what
    stops "anyone who knows an attempt id can post answers".
    """
    try:
        svc.check_session(attempt_id, x_attempt_token)
    except DeliveryError as exc:
        raise _http(exc) from exc


AttemptAuth = Depends(require_attempt_token)


def _schedule_finalize(
    svc: DeliveryService, attempt_id: str, background: BackgroundTasks, reason: str
) -> None:
    """Commit the request's lifecycle write, then schedule the one-shot finalize.

    Commit FIRST: the request session otherwise commits only when the dependency
    finalizes — which FastAPI runs *after* background tasks — so the task (its own
    connection) would block on this request's uncommitted write and deadlock on
    SQLite. Committing frees the lock and makes the submitted/expired state durable
    before the grade+report runs. `finalize_attempt` is looked up at call time.
    """
    svc.attempts.session.commit()
    from app.results import finalize_attempt

    background.add_task(finalize_attempt, attempt_id)
    log.info("exam %s attempt=%s -> finalize scheduled", reason, attempt_id)


def _finalize_if_just_expired(
    svc: DeliveryService, state: AttemptState, background: BackgroundTasks
) -> AttemptState:
    """Schedule the finalize when THIS request flipped the attempt to `expired`.

    `just_expired` is true only on the call that performed the transition (the
    repository flips it with a conditional update), so later polls of an already
    expired attempt never schedule a duplicate finalize.
    """
    if state.just_expired:
        _schedule_finalize(svc, state.attempt_id, background, "lazy expiry")
    return state


def _reject_late(
    svc: DeliveryService,
    attempt_id: str,
    exc: AttemptExpiredError,
    background: BackgroundTasks,
) -> JSONResponse:
    """409 for a save/submit past the deadline, persisting the expiry it implies.

    Raising `HTTPException` would roll the request session back (losing the
    expiry) and drop the injected background tasks, so the transition is committed
    here and the 409 is returned as a response that carries the background finalize
    (FastAPI attaches the endpoint's `BackgroundTasks` to a returned `Response`).
    Same body shape as `HTTPException` (`{"detail": ...}`).
    """
    err = _http(exc)
    if svc.expire_if_overdue(attempt_id):
        _schedule_finalize(svc, attempt_id, background, "late action expiry")
    return JSONResponse({"detail": err.detail}, status_code=err.status_code)


# --------------------------------------------------------------------------- #
# Pick-your-name (share-link landing).
# --------------------------------------------------------------------------- #
def _published_test_or_404(svc: DeliveryService, test_id: str) -> Test:
    """The test behind a share link — published only; a draft looks exactly like
    an unknown id (404), so the link reveals nothing about unpublished content."""
    test = svc.content.get_test(test_id)
    if test is None or test.status != "published":
        raise _http(NotFoundError(f"test {test_id!r} not found"))
    return test


@router.get("/tests/{test_id}/roster", response_model=list[RosterChoice])
def roster_for_link(test_id: str, svc: DeliveryDep) -> list[RosterChoice]:
    """List the names on a test's roster so the student can pick their own.

    Carries no answer key and no attempt internals beyond the per-entry status,
    so it is safe to serve unauthenticated behind the share link. Only a PUBLISHED
    test has a live link: a draft (or unknown id) is 404, like `/info`.
    """
    _published_test_or_404(svc, test_id)
    entries = svc.attempts.list_roster_entries(test_id)
    log.info("exam roster_for_link test=%s -> %d", test_id, len(entries))
    # Names + status ONLY: the PIN never leaves through the share link.
    return [
        RosterChoice(roster_entry_id=e.id, display_name=e.display_name, status=e.status)
        for e in entries
    ]


@router.get("/tests/{test_id}/info", response_model=TestInfo)
def test_info(test_id: str, svc: DeliveryDep) -> TestInfo:
    """Title + level for the start screen (the YLE language switch keys off `level`).

    Published tests only (a draft or unknown id is 404 — golden rule #5)."""
    test = _published_test_or_404(svc, test_id)
    now = datetime.now(UTC)
    window = ExamWindow.for_test(test)
    state = window.state(now)
    log.info("exam test_info test=%s level=%s window=%s", test_id, test.level, state)
    return TestInfo(
        test_id=test.id,
        title=test.title,
        level=test.level,
        window_state=state,
        opens_at=test.opens_at,
        closes_at=test.closes_at,
        starts_until=window.hard_close if test.closes_at is not None else None,
        server_now=now,
    )


# --------------------------------------------------------------------------- #
# Attempt lifecycle.
# --------------------------------------------------------------------------- #
def _client_ip(request: Request) -> str | None:
    """The rate-limit key: the socket peer as uvicorn reports it.

    Never a raw `X-Forwarded-For` (attacker-controlled); uvicorn's
    `--proxy-headers` rewrites `client.host` for a trusted proxy (see
    app/admin/CLAUDE.md "Client IP").
    """
    return request.client.host if request.client else None


@router.post("/roster/{roster_entry_id}/start", response_model=StartResponse)
def start(
    roster_entry_id: str,
    svc: DeliveryDep,
    background: BackgroundTasks,
    request: Request,
    body: StartRequest | None = None,
) -> StartResponse:
    """Check the PIN, then start the attempt for a roster entry or resume it.

    No / malformed PIN -> 400; wrong PIN -> 403 (`X-Pin-Tries-Left`); too many
    wrong PINs -> 429 (`Retry-After`). A resume needs the PIN too (or the runner
    skips `/start` and uses its stored session token). Each successful call issues
    a new session `token`. Resuming past the deadline expires the attempt here
    (via `get_state`), which schedules its finalize exactly like `/state` does.
    """
    entry = svc.attempts.get_roster_entry(roster_entry_id)
    if entry is not None and entry.attempt_id is None:
        # A NEW attempt only on a published test (same rule as the share-link
        # roster): an unpublished/draft test looks like an unknown id. Resuming an
        # attempt that already exists stays possible (a teacher's forced unpublish
        # mid-exam must not strand a student who is already writing).
        test = _published_test_or_404(svc, entry.test_id)
        # The window BEFORE the PIN: a student on a closed / not-yet-open test sees
        # why at once, and their typos are not counted against the PIN limiter. A
        # RESUME is never window-checked (its deadline was fixed at start).
        # `svc.start` re-checks (the authoritative check, same clock rule).
        window = ExamWindow.for_test(test)
        state = window.state(datetime.now(UTC))
        if state == "not_yet_open":
            raise _http(
                WindowNotOpenError(
                    f"window opens at {window.opens_at.isoformat()}",
                    opens_at=window.opens_at,
                )
            )
        if state == "closed":
            raise _http(
                WindowClosedError(
                    f"window closed at {window.hard_close.isoformat()}",
                    closed_at=window.hard_close,
                )
            )
    try:
        svc.authenticate(
            roster_entry_id,
            body.pin if body else None,
            limiter=get_pin_limiter(),
            client_ip=_client_ip(request),
        )
        attempt = svc.start(roster_entry_id)
    except DeliveryError as exc:
        raise _http(exc) from exc
    token = svc.open_session(attempt.id)
    state = _finalize_if_just_expired(svc, svc.get_state(attempt.id), background)
    return StartResponse(
        attempt_id=attempt.id,
        test_id=attempt.test_id,
        state=StateResponse.of(state),
        token=token,
    )


@router.get(
    "/attempts/{attempt_id}/test", response_model=ClientTest, dependencies=[AttemptAuth]
)
def client_test(attempt_id: str, svc: DeliveryDep) -> ClientTest:
    """The full student-facing test (drawn + shuffled, structurally keyless)."""
    try:
        return svc.client_test(attempt_id)
    except DeliveryError as exc:
        raise _http(exc) from exc


@router.get(
    "/attempts/{attempt_id}/items/{item_id}", response_model=ClientItem, dependencies=[AttemptAuth]
)
def serve_item(attempt_id: str, item_id: str, svc: DeliveryDep) -> ClientItem:
    """Serve a single item one-at-a-time, stripped of its answer key."""
    try:
        return svc.serve_item(attempt_id, item_id)
    except DeliveryError as exc:
        raise _http(exc) from exc


@router.get(
    "/attempts/{attempt_id}/state", response_model=StateResponse, dependencies=[AttemptAuth]
)
def get_state(
    attempt_id: str, svc: DeliveryDep, background: BackgroundTasks
) -> StateResponse:
    """Server-authoritative timer/state; expires a crossed-deadline attempt.

    The poll that performs the expiry schedules the attempt's finalize (once per
    transition — later polls see `expired` and schedule nothing).
    """
    try:
        state = svc.get_state(attempt_id)
    except DeliveryError as exc:
        raise _http(exc) from exc
    return StateResponse.of(_finalize_if_just_expired(svc, state, background))


@router.post("/attempts/{attempt_id}/audio-progress", dependencies=[AttemptAuth])
def audio_progress(
    attempt_id: str, body: AudioProgressRequest, svc: DeliveryDep
) -> dict[str, int | dict[str, int]]:
    """Advance the server-side furthest listening position (monotonic anti-replay).

    `{asset_id, seconds}` -> that track's position; answers `{"audio_progress":
    {asset_id: seconds, ...}}`. Legacy `{seconds}` -> `{"audio_progress_seconds": n}`
    (unchanged shape for an old runner). An asset that is not a listening track of
    the test -> 400."""
    try:
        if body.asset_id is None:
            # Legacy single-track path (pre per-part runner) — remove next phase.
            return {
                "audio_progress_seconds": svc.report_audio_progress(attempt_id, body.seconds)
            }
        return {
            "audio_progress": svc.report_track_progress(
                attempt_id, body.asset_id, body.seconds
            )
        }
    except DeliveryError as exc:
        raise _http(exc) from exc


@router.get("/attempts/{attempt_id}/answers", dependencies=[AttemptAuth])
def saved_answers(attempt_id: str, svc: DeliveryDep) -> dict[str, str | int]:
    """Saved answers as client display values, so the runner repopulates on refresh."""
    try:
        return svc.get_saved_answers(attempt_id)
    except DeliveryError as exc:
        raise _http(exc) from exc


@router.put(
    "/attempts/{attempt_id}/answers/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    dependencies=[AttemptAuth],
)
def save_answer(
    attempt_id: str,
    item_id: str,
    body: SaveAnswerRequest,
    svc: DeliveryDep,
    background: BackgroundTasks,
) -> Response | None:
    """Persist one answer (displayed -> canonical). Rejected past the deadline.

    A late save that crosses the deadline expires the attempt (committed) and
    schedules its finalize before answering 409.
    """
    try:
        svc.save_answer(attempt_id, item_id, body.response)
    except AttemptExpiredError as exc:
        return _reject_late(svc, attempt_id, exc, background)
    except DeliveryError as exc:
        raise _http(exc) from exc
    return None


@router.post(
    "/attempts/{attempt_id}/submit", response_model=StateResponse, dependencies=[AttemptAuth]
)
def submit(
    attempt_id: str, svc: DeliveryDep, background: BackgroundTasks
) -> StateResponse | Response:
    """Finalize the attempt. A late submit (past the deadline) is rejected.

    On a successful submit, schedule the one-shot grade+report job to run after the
    response is sent (its own session): the student sees "submitted" instantly and
    the teacher's dashboard gets the frozen `AttemptReport` a few seconds later
    (golden rule #3: submit stays fast; grading never blocks the timer/UX).

    A late submit that crosses the deadline is rejected (409) but still expires the
    attempt and schedules its finalize — the saved answers get graded either way.
    """
    try:
        svc.submit(attempt_id)
    except AttemptExpiredError as exc:
        return _reject_late(svc, attempt_id, exc, background)
    except DeliveryError as exc:
        raise _http(exc) from exc
    # Commit the submit NOW, before scheduling the finalize task (see
    # `_schedule_finalize`: avoids the SQLite lock against the background task).
    _schedule_finalize(svc, attempt_id, background, "submit")
    return StateResponse.of(svc.get_state(attempt_id))
