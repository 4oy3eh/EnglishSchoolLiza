"""Admin (teacher) engine — public surface (Phase 10).

The composition root for the teacher dashboard: bank management, the review-queue
publish gate (golden rule #5), live roster, and a results view that puts the score
next to the advisory cheating verdict and the raw replay (rules #2, #6).
"""

from __future__ import annotations

from app.admin.auth import (
    AuthError,
    LoginDecision,
    LoginRateLimiter,
    TokenSigner,
    insecure_defaults,
    is_local_request,
    is_loopback_host,
    verify_password,
)
from app.admin.models import (
    AttemptOverview,
    AttemptResult,
    BulkAddResult,
    ReviewDraft,
    RosterStatus,
    StudentReport,
    TestStats,
)
from app.admin.service import AdminError, AdminService, build_admin_service

__all__ = [
    "AdminError",
    "AdminService",
    "build_admin_service",
    "AuthError",
    "TokenSigner",
    "verify_password",
    "insecure_defaults",
    "is_local_request",
    "is_loopback_host",
    "LoginDecision",
    "LoginRateLimiter",
    "ReviewDraft",
    "RosterStatus",
    "AttemptOverview",
    "AttemptResult",
    "BulkAddResult",
    "StudentReport",
    "TestStats",
]
