"""Admin (teacher) engine — public surface (Phase 10).

The composition root for the teacher dashboard: bank management, the review-queue
publish gate (golden rule #5), live roster, and a results view that puts the score
next to the advisory cheating verdict and the raw replay (rules #2, #6).
"""

from __future__ import annotations

from app.admin.auth import AuthError, TokenSigner, verify_password
from app.admin.models import (
    AttemptOverview,
    AttemptResult,
    ReviewDraft,
    RosterStatus,
)
from app.admin.service import AdminError, AdminService, build_admin_service

__all__ = [
    "AdminError",
    "AdminService",
    "build_admin_service",
    "AuthError",
    "TokenSigner",
    "verify_password",
    "ReviewDraft",
    "RosterStatus",
    "AttemptOverview",
    "AttemptResult",
]
