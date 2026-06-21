"""Deterministic integrity features over an attempt's event stream (Phase 7).

Pure functions only: given the same `IntegrityEvent`s, they return an identical
`IntegrityProfile`. No clock, no randomness, no I/O, **no LLM** — this layer
computes reproducible numbers and judges *nothing* about guilt (golden rule #6).
The advisory verdict is Phase 8 (`app/analysis`).

Trusted time is `server_ts` (stamped on ingest); the client clock is captured but
never trusted for decisions (mirrors the server-authoritative timer, rule #3).
"""

from __future__ import annotations

import statistics
from datetime import datetime

from contracts import (
    HiddenInterval,
    IntegrityEvent,
    IntegrityProfile,
    QuestionTiming,
)

# An answer landing within this window of returning to the tab counts as the
# "left -> came back -> answered immediately" pattern (systematicity signal).
POST_RETURN_FAST_MS = 2000

# Events that mark a question (as opposed to a section-scoped audio event). An
# item_id is treated as a question only if it carries one of these.
_QUESTION_EVENT_TYPES = ("interaction", "answer_change")
_VISIBILITY_TYPES = ("visibility_hidden", "visibility_visible")


def _ts(event: IntegrityEvent) -> datetime:
    """Trusted timestamp: server time, falling back to client time if unstamped."""
    return event.server_ts or event.client_ts


def _ms(start: datetime, end: datetime) -> int:
    """Non-negative whole milliseconds between two timestamps."""
    return max(0, round((end - start).total_seconds() * 1000))


def extract_profile(
    attempt_id: str, events: list[IntegrityEvent]
) -> IntegrityProfile:
    """Reduce an attempt's raw event stream to a deterministic `IntegrityProfile`.

    A pure function: the same input list always yields the same profile. Events
    are ordered by trusted `server_ts` with a *stable* sort, so the canonical
    ingest order `EventRepository.list_events` supplies (rows by id) is preserved
    on ties — events sharing an identical `server_ts` keep their ingest order
    rather than being reordered. (Two *different* orderings of events that collide
    on `server_ts` can therefore differ; the guarantee is for the repository's
    canonical stream. Distinct-timestamp streams are fully order-independent.)
    """
    ordered = sorted(
        enumerate(events), key=lambda pair: (_ts(pair[1]), pair[0])
    )
    chron = [event for _, event in ordered]

    hidden_intervals = _hidden_intervals(chron)
    total_hidden_ms = sum(iv.duration_ms for iv in hidden_intervals)
    visibility = _visibility_timeline(chron)
    timings = _question_timings(chron, visibility)

    answered_paces = [t.latency_ms for t in timings if t.latency_ms > 0]
    pacing_cv = _coefficient_of_variation(answered_paces)
    systematicity_rate = _systematicity_rate(timings)

    return IntegrityProfile(
        attempt_id=attempt_id,
        question_timings=timings,
        hidden_intervals=hidden_intervals,
        total_hidden_ms=total_hidden_ms,
        pacing_cv=pacing_cv,
        systematicity_rate=systematicity_rate,
    )


def _hidden_intervals(chron: list[IntegrityEvent]) -> list[HiddenInterval]:
    """Pair each `visibility_hidden` with the next `visibility_visible`.

    A hide with no matching return (e.g. the tab is closed) is left open and not
    reported — only fully bounded intervals have a measurable duration.
    """
    intervals: list[HiddenInterval] = []
    pending: datetime | None = None
    for event in chron:
        if event.type == "visibility_hidden":
            if pending is None:  # ignore repeated hides without an intervening show
                pending = _ts(event)
        elif event.type == "visibility_visible" and pending is not None:
            end = _ts(event)
            intervals.append(
                HiddenInterval(start=pending, end=end, duration_ms=_ms(pending, end))
            )
            pending = None
    return intervals


def _visibility_timeline(chron: list[IntegrityEvent]) -> list[tuple[datetime, str]]:
    """Chronological (timestamp, type) list of visibility transitions only."""
    return [(_ts(e), e.type) for e in chron if e.type in _VISIBILITY_TYPES]


def _latest_visible_before(
    visibility: list[tuple[datetime, str]], when: datetime
) -> datetime | None:
    """Return the tab's last become-visible time at/just before `when`.

    If the most recent visibility transition at `when` is `visibility_visible`,
    the student had returned to the tab; that timestamp is returned. If it was a
    hide (answering while hidden) or there was no transition yet, returns None.
    """
    latest: tuple[datetime, str] | None = None
    for ts, kind in visibility:  # visibility is already chronological
        if ts <= when:
            latest = (ts, kind)
        else:
            break
    if latest is not None and latest[1] == "visibility_visible":
        return latest[0]
    return None


def _question_timings(
    chron: list[IntegrityEvent], visibility: list[tuple[datetime, str]]
) -> list[QuestionTiming]:
    """One `QuestionTiming` per question item, in first-seen order.

    A question item is any `item_id` carrying an `interaction`/`answer_change`
    event (audio events reference a section, not a question, so they are skipped).
    Latency is time from first sighting to first answer; `post_return_latency_ms`
    is the visible->answer gap when the answer immediately followed a tab return.
    """
    order: list[str] = []
    by_item: dict[str, list[IntegrityEvent]] = {}
    for event in chron:
        if event.item_id is None or event.type not in _QUESTION_EVENT_TYPES:
            continue
        if event.item_id not in by_item:
            by_item[event.item_id] = []
            order.append(event.item_id)
        by_item[event.item_id].append(event)

    timings: list[QuestionTiming] = []
    for item_id in order:
        item_events = by_item[item_id]
        first_seen = _ts(item_events[0])
        interaction_count = sum(1 for e in item_events if e.type == "interaction")
        answers = [e for e in item_events if e.type == "answer_change"]

        post_return: int | None = None
        if answers:
            first_answer = _ts(answers[0])
            latency_ms = _ms(first_seen, first_answer)
            visible_at = _latest_visible_before(visibility, first_answer)
            if visible_at is not None:
                post_return = _ms(visible_at, first_answer)
        else:
            latency_ms = 0  # seen but never answered

        timings.append(
            QuestionTiming(
                item_id=item_id,
                latency_ms=latency_ms,
                interaction_count=interaction_count,
                post_return_latency_ms=post_return,
            )
        )
    return timings


def _coefficient_of_variation(values: list[int]) -> float:
    """Population std / mean. 0.0 for <2 samples or a zero mean (no spread)."""
    if len(values) < 2:
        return 0.0
    mean = statistics.fmean(values)
    if mean == 0:
        return 0.0
    return statistics.pstdev(values) / mean


def _systematicity_rate(timings: list[QuestionTiming]) -> float:
    """Fraction of questions answered immediately after returning to the tab."""
    if not timings:
        return 0.0
    pattern = sum(
        1
        for t in timings
        if t.post_return_latency_ms is not None
        and t.post_return_latency_ms <= POST_RETURN_FAST_MS
    )
    return pattern / len(timings)
