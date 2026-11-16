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
from collections.abc import Mapping, Sequence
from datetime import datetime

from app.integrity.writing import dedupe_events, extract_writing_features
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
    attempt_id: str,
    events: list[IntegrityEvent],
    *,
    answers: Mapping[str, str] | None = None,
    writing_items: Sequence[str] | None = None,
) -> IntegrityProfile:
    """Reduce an attempt's raw event stream to a deterministic `IntegrityProfile`.

    A pure function: the same input list always yields the same profile. Events
    are ordered by trusted `server_ts` with a *stable* sort, so the canonical
    ingest order `EventRepository.list_events` supplies (rows by id) is preserved
    on ties — events sharing an identical `server_ts` keep their ingest order
    rather than being reordered. (Two *different* orderings of events that collide
    on `server_ts` can therefore differ; the guarantee is for the repository's
    canonical stream. Distinct-timestamp streams are fully order-independent.)

    Exact replays of an event (same `type` / `client_ts` / `item_id` /
    `duration_ms` / payload — the recorder resends unacknowledged events) are
    dropped after ordering, so no count or sum below sees an event twice.

    `answers` (item_id -> submitted text) and `writing_items` (the test's
    open_writing ids, in display order) feed the per-writing-item features
    (`app.integrity.writing`); both are optional — without them the writing items
    are derived from the writing-field telemetry and the final text from the last
    `answer_change` snapshot.
    """
    ordered = sorted(
        enumerate(events), key=lambda pair: (_ts(pair[1]), pair[0])
    )
    # The recorder is at-least-once (replay after a reload / a timed-out batch):
    # drop exact replays, keeping the earliest ingest, before anything is counted.
    chron = dedupe_events([event for _, event in ordered])

    hidden_intervals = _hidden_intervals(chron)
    total_hidden_ms = sum(iv.duration_ms for iv in hidden_intervals)
    visibility = _visibility_timeline(chron)
    timings = _question_timings(chron, visibility)

    answered_paces = [t.latency_ms for t in timings if t.latency_ms > 0]
    pacing_cv = _coefficient_of_variation(answered_paces)
    systematicity_rate = _systematicity_rate(timings)
    device_count, device_changed = _device_summary(chron)
    writing = extract_writing_features(chron, answers=answers, writing_items=writing_items)

    return IntegrityProfile(
        attempt_id=attempt_id,
        question_timings=timings,
        hidden_intervals=hidden_intervals,
        total_hidden_ms=total_hidden_ms,
        pacing_cv=pacing_cv,
        systematicity_rate=systematicity_rate,
        device_count=device_count,
        device_changed=device_changed,
        paste_attempt_count=sum(1 for e in chron if e.type == "paste_attempt"),
        fullscreen_exit_count=sum(1 for e in chron if e.type == "fullscreen_exit"),
        writing=writing,
    )


def _device_summary(chron: list[IntegrityEvent]) -> tuple[int, bool]:
    """Count distinct device fingerprints across `device_info` captures.

    Each (re)start emits one `device_info` event; a resume on a different machine
    yields a second fingerprint. The fingerprint is the device identity
    (user-agent + platform + screen size), not the network — an IP change alone is
    not a device change. Deterministic and judgment-free: it reports *that* the
    device differed, never whether that means guilt (golden rule #6).
    """
    fingerprints: list[tuple] = []
    for event in chron:
        if event.type != "device_info":
            continue
        p = event.payload or {}
        screen = p.get("screen") or {}
        fingerprints.append(
            (
                p.get("user_agent", ""),
                p.get("platform", ""),
                screen.get("width"),
                screen.get("height"),
            )
        )
    distinct = list(dict.fromkeys(fingerprints))  # unique, order-preserving
    return len(distinct), len(distinct) > 1


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
