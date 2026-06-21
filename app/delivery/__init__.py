"""Delivery engine: exam runtime — attempt lifecycle (Phase 4).

Public surface:

* `DeliveryService` — start (create-or-resume), serve (no `correct`), save answer
  (displayed -> canonical), server-authoritative state/timer, submit.
* `ExamWindow` — when the link is live (+ grace); caps the per-attempt deadline.
* `AttemptState` — server-authoritative timer/state snapshot.
* errors: `DeliveryError` and its subclasses (window / expiry / state / not-found).
* projection helpers (`project_test`, `project_item`, ...) for the `Client*` shape.
"""

from __future__ import annotations

from app.delivery.projection import (
    project_item,
    project_option,
    project_section,
    project_test,
)
from app.delivery.service import (
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

__all__ = [
    "DeliveryService",
    "ExamWindow",
    "AttemptState",
    "DeliveryError",
    "WindowNotOpenError",
    "WindowClosedError",
    "AttemptExpiredError",
    "AttemptStateError",
    "NotFoundError",
    "project_test",
    "project_section",
    "project_item",
    "project_option",
]
