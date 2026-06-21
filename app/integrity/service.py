"""Integrity engine: deterministic feature extraction over the event stream.

Layer 2 of the three integrity layers (golden rule #6): `telemetry` (capture) ->
**`integrity` (deterministic features)** -> `analysis` (LLM verdict). This service
only *reads* the append-only stream (via `EventRepository`, which telemetry owns)
and reduces it to an `IntegrityProfile` with the pure extractor in `features.py`.

No judgment of guilt, no score, no LLM. Same events -> same profile.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.integrity.features import extract_profile
from app.persistence.repository import EventRepository
from contracts import IntegrityProfile

log = get_logger(__name__)


class IntegrityService:
    """Build an attempt's `IntegrityProfile` from its persisted event stream."""

    def __init__(self, events: EventRepository) -> None:
        self.events = events

    def profile(self, attempt_id: str) -> IntegrityProfile:
        """Read the attempt's events and extract deterministic features."""
        events = self.events.list_events(attempt_id)
        profile = extract_profile(attempt_id, events)
        log.info(
            "integrity profile attempt=%s questions=%d hidden=%d "
            "total_hidden_ms=%d pacing_cv=%.3f systematicity=%.3f",
            attempt_id,
            len(profile.question_timings),
            len(profile.hidden_intervals),
            profile.total_hidden_ms,
            profile.pacing_cv,
            profile.systematicity_rate,
        )
        return profile
