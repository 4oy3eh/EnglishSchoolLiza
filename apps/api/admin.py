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
def get_admin_service(
    session: Annotated[Session, Depends(get_session)],
) -> Iterator[AdminService]:
    yield build_admin_service(session, FilesystemStorage(settings.assets_dir))


AdminDep = Annotated[AdminService, Depends(get_admin_service)]
_Teacher = Depends(require_teacher)


def _not_found(exc: AdminError) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


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
        raise _not_found(exc) from exc


@router.delete("/tests/{test_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_Teacher])
def delete_test(test_id: str, admin: AdminDep) -> None:
    try:
        admin.delete_test(test_id)
    except AdminError as exc:
        raise _not_found(exc) from exc


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
        raise _not_found(exc) from exc


@router.post("/tests/{test_id}/unpublish", response_model=Test, dependencies=[_Teacher])
def unpublish(test_id: str, admin: AdminDep) -> Test:
    try:
        return admin.unpublish(test_id)
    except AdminError as exc:
        raise _not_found(exc) from exc


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
        raise _not_found(exc) from exc


@router.get(
    "/tests/{test_id}/roster", response_model=list[RosterStatus], dependencies=[_Teacher]
)
def roster_status(test_id: str, admin: AdminDep) -> list[RosterStatus]:
    return admin.roster_status(test_id)


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
        raise _not_found(exc) from exc


@router.get("/results/{attempt_id}", response_model=AttemptResult, dependencies=[_Teacher])
def attempt_result(attempt_id: str, admin: AdminDep) -> AttemptResult:
    try:
        return admin.attempt_result(attempt_id)
    except AdminError as exc:
        raise _not_found(exc) from exc
