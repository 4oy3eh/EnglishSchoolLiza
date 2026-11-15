"""Admin (teacher) HTTP surface (Phase 10).

Wires `AdminService` behind a required bearer token. Every route below
(except `POST /admin/login`) depends on `require_teacher`, so auth is enforced
platform-wide for the dashboard: bank, review queue + approve (the publish gate),
roster, and results.

Auth model (see `app/admin/auth.py`): the teacher posts the shared password to
`/admin/login` and gets a short-lived HMAC-signed token; the dashboard sends it as
`Authorization: Bearer <token>` on every subsequent call.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.admin import (
    AdminError,
    AdminService,
    AttemptOverview,
    AttemptResult,
    AuthError,
    ReviewDraft,
    RosterStatus,
    TokenSigner,
    build_admin_service,
    verify_password,
)
from app.content.storage import FilesystemStorage
from app.core.config import settings
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


# --------------------------------------------------------------------------- #
# Auth.
# --------------------------------------------------------------------------- #
class LoginRequest(BaseModel):
    password: str


class TokenResponse(BaseModel):
    token: str


def require_teacher(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    """Resolve only for a valid, unexpired teacher token; else 401."""
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        return _signer.verify(creds.credentials)
    except AuthError as exc:
        log.warning("admin auth rejected: %s", exc)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from exc


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest) -> TokenResponse:
    """Exchange the shared teacher password for a signed bearer token."""
    if not verify_password(body.password, settings.teacher_password):
        log.warning("admin login rejected: bad password")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad credentials")
    log.info("admin login ok")
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


@router.delete("/tests/{test_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_Teacher])
def delete_test(test_id: str, admin: AdminDep) -> None:
    try:
        admin.delete_test(test_id)
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
def unpublish(test_id: str, admin: AdminDep) -> Test:
    try:
        return admin.unpublish(test_id)
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


@router.post(
    "/roster/{roster_entry_id}/reopen", response_model=RosterStatus, dependencies=[_Teacher]
)
def reopen_attempt(roster_entry_id: str, admin: AdminDep) -> RosterStatus:
    """Let a student back in keeping their progress; the deadline keeps running."""
    try:
        return admin.reopen_attempt(roster_entry_id)
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
