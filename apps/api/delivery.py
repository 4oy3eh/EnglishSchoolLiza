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

No teacher auth: students reach this via the per-test share link, identified by
their `roster_entry_id`. The exam window is permissive in this phase (links are
always live); the per-attempt deadline is therefore `start + duration_minutes`.
Scheduled windows are a later concern and would live on the authoring `Test`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.core.logging import get_logger
from app.delivery import (
    AttemptExpiredError,
    AttemptState,
    AttemptStateError,
    DeliveryError,
    DeliveryService,
    ExamWindow,
    NotFoundError,
    WindowClosedError,
    WindowNotOpenError,
)
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import ClientItem, ClientTest

log = get_logger(__name__)

router = APIRouter(prefix="/exam", tags=["exam"])

# Permissive "always live" window for this phase: opened in the distant past and
# closing in the distant future, so the per-attempt deadline is driven purely by
# the test's `duration_minutes` (golden rule #3 timer is still fully enforced).
_ALWAYS_OPEN = ExamWindow(
    opens_at=datetime(1970, 1, 1, tzinfo=UTC),
    closes_at=datetime(9999, 1, 1, tzinfo=UTC),
)


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

    @classmethod
    def of(cls, state: AttemptState) -> StateResponse:
        return cls(
            attempt_id=state.attempt_id,
            status=state.status,
            deadline=state.deadline,
            remaining_seconds=state.remaining_seconds,
            expired=state.expired,
        )


class StartResponse(BaseModel):
    """What the runner needs to (re)hydrate an attempt after start/resume."""

    attempt_id: str
    test_id: str
    state: StateResponse


class SaveAnswerRequest(BaseModel):
    """A single saved answer. `response` is the *displayed* option index (int)
    for single_choice, or the literal text / pool key (str) for everything else;
    delivery maps it to the canonical key before persisting (golden rule #1)."""

    response: str | int


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
    if isinstance(exc, NotFoundError):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, (WindowNotOpenError, WindowClosedError)):
        code = status.HTTP_403_FORBIDDEN
    elif isinstance(exc, (AttemptExpiredError, AttemptStateError)):
        code = status.HTTP_409_CONFLICT
    else:
        code = status.HTTP_400_BAD_REQUEST
    # Surface every rejection at the HTTP boundary (validation failures -> WARNING
    # per CLAUDE.md), since the service-layer logs success paths but not the
    # mapped status a client actually receives.
    log.warning("exam request rejected -> %d: %s", code, exc)
    return HTTPException(code, str(exc))


# --------------------------------------------------------------------------- #
# Pick-your-name (share-link landing).
# --------------------------------------------------------------------------- #
@router.get("/tests/{test_id}/roster", response_model=list[RosterChoice])
def roster_for_link(test_id: str, svc: DeliveryDep) -> list[RosterChoice]:
    """List the names on a test's roster so the student can pick their own.

    Carries no answer key and no attempt internals beyond the per-entry status,
    so it is safe to serve unauthenticated behind the share link.
    """
    entries = svc.attempts.list_roster_entries(test_id)
    log.info("exam roster_for_link test=%s -> %d", test_id, len(entries))
    return [
        RosterChoice(roster_entry_id=e.id, display_name=e.display_name, status=e.status)
        for e in entries
    ]


# --------------------------------------------------------------------------- #
# Attempt lifecycle.
# --------------------------------------------------------------------------- #
@router.post("/roster/{roster_entry_id}/start", response_model=StartResponse)
def start(roster_entry_id: str, svc: DeliveryDep) -> StartResponse:
    """Start the attempt for a roster entry, or resume the existing one."""
    try:
        attempt = svc.start(roster_entry_id, _ALWAYS_OPEN)
    except DeliveryError as exc:
        raise _http(exc) from exc
    state = svc.get_state(attempt.id)
    return StartResponse(
        attempt_id=attempt.id, test_id=attempt.test_id, state=StateResponse.of(state)
    )


@router.get("/attempts/{attempt_id}/test", response_model=ClientTest)
def client_test(attempt_id: str, svc: DeliveryDep) -> ClientTest:
    """The full student-facing test (drawn + shuffled, structurally keyless)."""
    try:
        return svc.client_test(attempt_id)
    except DeliveryError as exc:
        raise _http(exc) from exc


@router.get("/attempts/{attempt_id}/items/{item_id}", response_model=ClientItem)
def serve_item(attempt_id: str, item_id: str, svc: DeliveryDep) -> ClientItem:
    """Serve a single item one-at-a-time, stripped of its answer key."""
    try:
        return svc.serve_item(attempt_id, item_id)
    except DeliveryError as exc:
        raise _http(exc) from exc


@router.get("/attempts/{attempt_id}/state", response_model=StateResponse)
def get_state(attempt_id: str, svc: DeliveryDep) -> StateResponse:
    """Server-authoritative timer/state; expires a crossed-deadline attempt."""
    try:
        return StateResponse.of(svc.get_state(attempt_id))
    except DeliveryError as exc:
        raise _http(exc) from exc


@router.put("/attempts/{attempt_id}/answers/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def save_answer(
    attempt_id: str, item_id: str, body: SaveAnswerRequest, svc: DeliveryDep
) -> None:
    """Persist one answer (displayed -> canonical). Rejected past the deadline."""
    try:
        svc.save_answer(attempt_id, item_id, body.response)
    except DeliveryError as exc:
        raise _http(exc) from exc


@router.post("/attempts/{attempt_id}/submit", response_model=StateResponse)
def submit(attempt_id: str, svc: DeliveryDep) -> StateResponse:
    """Finalize the attempt. A late submit (past the deadline) is rejected."""
    try:
        svc.submit(attempt_id)
    except DeliveryError as exc:
        raise _http(exc) from exc
    return StateResponse.of(svc.get_state(attempt_id))
