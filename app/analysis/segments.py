"""Deterministic selection of raw event segments worth showing to the analyst.

The analysis layer's LLM input is the Phase 7 `IntegrityProfile` **plus the raw
event segments that drove its strongest signals** (golden rule #6: the teacher
always sees the raw replay next to the verdict). This module turns the profile +
the append-only stream into those segments.

Pure and deterministic: same profile + same events -> same segments. No clock, no
randomness, no LLM, no judgment of guilt — just "here are the moments worth a look".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.integrity import POST_RETURN_FAST_MS
from contracts import IntegrityEvent, IntegrityProfile

# A bounded hidden interval at least this long is worth surfacing as a segment.
LONG_HIDDEN_MS = 10_000


@dataclass(frozen=True)
class FlaggedSegment:
    """A slice of the raw stream that explains one integrity signal.

    `kind` is the machine flag (becomes an `AnalysisVerdict.flags` entry), `reason`
    is the human sentence, and `events` are the raw `IntegrityEvent`s in the window
    so the teacher's replay can jump straight to them (rule #6). Carrying the raw
    events keeps analysis advisory and auditable — it never invents data.
    """

    kind: str
    reason: str
    item_id: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    events: tuple[IntegrityEvent, ...] = field(default_factory=tuple)


def _ts(event: IntegrityEvent) -> datetime:
    """Trusted timestamp: server time, falling back to client time if unstamped."""
    return event.server_ts or event.client_ts


def flag_segments(
    profile: IntegrityProfile, events: list[IntegrityEvent]
) -> list[FlaggedSegment]:
    """Select the raw segments behind the profile's strongest signals.

    Two deterministic passes, both reading numbers the integrity layer already
    computed (this layer does not re-derive features, golden rule #6):

    * **long hidden intervals** — every bounded hide >= ``LONG_HIDDEN_MS``, with the
      raw events captured inside the window.
    * **fast post-return answers** — every question answered within
      ``POST_RETURN_FAST_MS`` of returning to the tab, with that item's raw events.

    Segments are emitted long-hidden-first, each pass in the profile's own order, so
    the result is a pure function of its inputs.
    """
    segments: list[FlaggedSegment] = []

    for interval in profile.hidden_intervals:
        if interval.duration_ms < LONG_HIDDEN_MS:
            continue
        # Closed interval on purpose: the window brackets the hide with its bounding
        # `visibility_hidden`/`visibility_visible` transitions so the teacher's replay
        # has the return marker. (A non-visibility event landing exactly on the return
        # ms is thus included as context — advisory only, and the teacher sees raw.)
        window = tuple(
            e for e in events if interval.start <= _ts(e) <= interval.end
        )
        segments.append(
            FlaggedSegment(
                kind="long_hidden",
                reason=(
                    f"Tab hidden for {interval.duration_ms / 1000:.1f}s "
                    "(left the test page)."
                ),
                start=interval.start,
                end=interval.end,
                events=window,
            )
        )

    for timing in profile.question_timings:
        if (
            timing.post_return_latency_ms is None
            or timing.post_return_latency_ms > POST_RETURN_FAST_MS
        ):
            continue
        item_events = tuple(e for e in events if e.item_id == timing.item_id)
        segments.append(
            FlaggedSegment(
                kind="fast_post_return",
                reason=(
                    f"Question {timing.item_id} answered "
                    f"{timing.post_return_latency_ms} ms after returning to the tab."
                ),
                item_id=timing.item_id,
                events=item_events,
            )
        )

    return segments
