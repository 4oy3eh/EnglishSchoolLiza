"""Delivery engine: exam runtime — attempt lifecycle (Phase 4).

Public surface:

* `DeliveryService` — start (create-or-resume), serve (no `correct`), save answer
  (displayed -> canonical), server-authoritative state/timer, submit.
* `ExamWindow` — when the link is live (+ grace); caps the per-attempt deadline.
* `AttemptState` — server-authoritative timer/state snapshot.
* errors: `DeliveryError` and its subclasses (window / expiry / state / not-found,
  PIN required / wrong / locked, session token).
* access (Phase 14): `PinRateLimiter` (+ the process-wide `get_pin_limiter()`),
  `PinActivity`, `TOKEN_HEADER`, `new_session_token`.
* timed sections (IQ, Phase 17.2): `SectionStatus` / `SectionsState` /
  `SectionBegin` + `SectionStateError` (rules in `app.delivery.sections`).
* projection helpers (`project_test`, `project_item`, ...) for the `Client*` shape.
"""

from __future__ import annotations

from app.delivery.access import (
    TOKEN_HEADER,
    PinActivity,
    PinDecision,
    PinRateLimiter,
    get_pin_limiter,
    new_session_token,
)
from app.delivery.audio import ListeningTrack, listening_tracks
from app.delivery.projection import (
    project_item,
    project_option,
    project_section,
    project_test,
)
from app.delivery.service import (
    ALWAYS_OPEN,
    AnswerTooLargeError,
    AttemptExpiredError,
    AttemptState,
    AttemptStateError,
    DeliveryError,
    DeliveryService,
    ExamWindow,
    NotFoundError,
    PinLockedError,
    PinRequiredError,
    SectionBegin,
    SectionsState,
    SectionStateError,
    SectionStatus,
    SessionTokenError,
    UnknownTrackError,
    WindowClosedError,
    WindowNotOpenError,
    WindowState,
    WrongPinError,
)

__all__ = [
    "DeliveryService",
    "ALWAYS_OPEN",
    "AnswerTooLargeError",
    "ExamWindow",
    "WindowState",
    "AttemptState",
    "DeliveryError",
    "WindowNotOpenError",
    "WindowClosedError",
    "AttemptExpiredError",
    "AttemptStateError",
    "NotFoundError",
    "PinRequiredError",
    "WrongPinError",
    "PinLockedError",
    "SessionTokenError",
    "UnknownTrackError",
    "SectionStateError",
    "SectionStatus",
    "SectionsState",
    "SectionBegin",
    "ListeningTrack",
    "listening_tracks",
    "PinRateLimiter",
    "PinDecision",
    "PinActivity",
    "get_pin_limiter",
    "TOKEN_HEADER",
    "new_session_token",
    "project_test",
    "project_section",
    "project_item",
    "project_option",
]
