"""Integrity engine: deterministic feature extraction over the event stream.

Layer 2 of the three integrity layers (golden rule #6): `telemetry` (capture) ->
**`integrity` (deterministic features)** -> `analysis` (LLM verdict). This service
only *reads* the append-only stream (via `EventRepository`, which telemetry owns)
and reduces it to an `IntegrityProfile` with the pure extractor in `features.py`.

No judgment of guilt, no score, no LLM. Same events -> same profile.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.core.logging import get_logger
from app.integrity.features import extract_profile
from app.integrity.writing import dedupe_events
from app.persistence.repository import EventRepository
from contracts import IntegrityProfile

log = get_logger(__name__)


class IntegrityService:
    """Build an attempt's `IntegrityProfile` from its persisted event stream."""

    def __init__(self, events: EventRepository) -> None:
        self.events = events

    def profile(
        self,
        attempt_id: str,
        *,
        answers: Mapping[str, str] | None = None,
        writing_items: Sequence[str] | None = None,
    ) -> IntegrityProfile:
        """Read the attempt's events and extract deterministic features.

        `answers` / `writing_items` are optional attempt data for the writing
        features (the results layer passes the submitted text and the test's
        open_writing ids); without them they are derived from the event stream.
        """
        events = self.events.list_events(attempt_id)
        replays = len(events) - len(dedupe_events(events))
        if replays:
            log.info(
                "integrity attempt=%s ignoring %d replayed event(s) of %d",
                attempt_id,
                replays,
                len(events),
            )
        profile = extract_profile(
            attempt_id, events, answers=answers, writing_items=writing_items
        )
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
        for w in profile.writing:
            log.info(
                "integrity writing attempt=%s item=%s len=%d typed=%d non_kb=%d "
                "unexplained=%d pastes=%d away_ms=%d telemetry_gap=%s "
                "(uncovered=%d restarts=%d) flags=%s",
                attempt_id,
                w.item_id,
                w.final_length,
                w.typed_chars,
                w.non_keyboard_chars,
                w.unexplained_chars,
                w.paste_attempts,
                max(w.hidden_ms_while_writing, w.blur_ms_while_writing),
                w.telemetry_gap,
                w.uncovered_snapshot_growth,
                w.recorder_restarts_while_writing,
                ",".join(w.flags) or "-",
            )
            if w.flags:
                log.warning(
                    "integrity writing flags attempt=%s item=%s -> %s",
                    attempt_id,
                    w.item_id,
                    ",".join(w.flags),
                )
        return profile
