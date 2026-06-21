"""Phase 7: integrity engine — deterministic feature extraction.

Gate (docs/PROMPTS.md Prompt 7): synthetic event streams map to expected feature
values (latency, hidden intervals, post-return pattern, pacing CV, systematicity);
the extractor is deterministic (same events -> same profile) and calls no LLM.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.integrity import POST_RETURN_FAST_MS, IntegrityService, extract_profile
from app.integrity.features import _coefficient_of_variation
from app.persistence.repository import EventRepository
from contracts import IntegrityEvent

T0 = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)


def ev(
    event_type: str,
    secs: float,
    item_id: str | None = None,
    payload: dict | None = None,
) -> IntegrityEvent:
    """An event at T0 + `secs`, with server_ts stamped (trusted time)."""
    ts = T0 + timedelta(seconds=secs)
    return IntegrityEvent(
        attempt_id="att-1",
        item_id=item_id,
        type=event_type,  # type: ignore[arg-type]
        client_ts=ts,
        server_ts=ts,
        payload=payload or {},
    )


def _golden_stream() -> list[IntegrityEvent]:
    """q1 answered cold; q2 answered right after a tab return; q3 answered slowly."""
    return [
        ev("interaction", 0, "q1"),
        ev("answer_change", 5, "q1", {"value": "A"}),
        ev("interaction", 6, "q2"),
        ev("visibility_hidden", 7),
        ev("visibility_visible", 20),
        ev("answer_change", 21, "q2", {"value": "B"}),
        ev("interaction", 22, "q3"),
        ev("answer_change", 40, "q3", {"value": "C"}),
    ]


# --------------------------------------------------------------------------- #
# Golden feature values.
# --------------------------------------------------------------------------- #
def test_question_timings_golden() -> None:
    profile = extract_profile("att-1", _golden_stream())

    timings = {t.item_id: t for t in profile.question_timings}
    assert [t.item_id for t in profile.question_timings] == ["q1", "q2", "q3"]

    # q1: seen +0s, answered +5s, no prior tab return -> no post-return pattern.
    assert timings["q1"].latency_ms == 5000
    assert timings["q1"].interaction_count == 1
    assert timings["q1"].post_return_latency_ms is None

    # q2: seen +6s, answered +21s; last visibility before the answer was the
    # return at +20s -> answered 1s after coming back.
    assert timings["q2"].latency_ms == 15000
    assert timings["q2"].post_return_latency_ms == 1000

    # q3: seen +22s, answered +40s; the tab was already visible (return at +20s)
    # but the answer is 20s later -> post-return latency present but not "fast".
    assert timings["q3"].latency_ms == 18000
    assert timings["q3"].post_return_latency_ms == 20000


def test_hidden_intervals_golden() -> None:
    profile = extract_profile("att-1", _golden_stream())
    assert len(profile.hidden_intervals) == 1
    interval = profile.hidden_intervals[0]
    assert interval.start == T0 + timedelta(seconds=7)
    assert interval.end == T0 + timedelta(seconds=20)
    assert interval.duration_ms == 13000
    assert profile.total_hidden_ms == 13000


def test_pacing_and_systematicity_golden() -> None:
    profile = extract_profile("att-1", _golden_stream())
    # Paces are the answered latencies [5000, 15000, 18000].
    assert profile.pacing_cv == pytest.approx(0.4388, abs=1e-3)
    # Only q2 fits the "returned then answered immediately" pattern -> 1 of 3.
    assert profile.systematicity_rate == pytest.approx(1 / 3, abs=1e-6)


def test_systematicity_threshold_boundary() -> None:
    # An answer exactly at POST_RETURN_FAST_MS still counts; one past it does not.
    fast = POST_RETURN_FAST_MS / 1000
    stream = [
        ev("interaction", 0, "qa"),
        ev("visibility_hidden", 1),
        ev("visibility_visible", 10),
        ev("answer_change", 10 + fast, "qa"),  # exactly at threshold -> counts
        ev("interaction", 30, "qb"),
        ev("visibility_hidden", 31),
        ev("visibility_visible", 40),
        ev("answer_change", 40 + fast + 0.5, "qb"),  # past threshold -> excluded
    ]
    profile = extract_profile("att-1", stream)
    assert profile.systematicity_rate == pytest.approx(0.5, abs=1e-6)


# --------------------------------------------------------------------------- #
# Edge cases.
# --------------------------------------------------------------------------- #
def test_audio_events_are_not_questions() -> None:
    # audio_play/seek reference a section, not a question item.
    stream = [
        ev("audio_play", 0, "sec-1", {"position": 0.0}),
        ev("audio_seek", 3, "sec-1", {"position": 12.5}),
        ev("interaction", 4, "q1"),
        ev("answer_change", 6, "q1"),
    ]
    profile = extract_profile("att-1", stream)
    assert [t.item_id for t in profile.question_timings] == ["q1"]


def test_unanswered_question_has_zero_latency_and_is_excluded_from_pacing() -> None:
    stream = [
        ev("interaction", 0, "q1"),
        ev("answer_change", 4, "q1"),
        ev("interaction", 5, "q2"),  # seen but never answered
    ]
    profile = extract_profile("att-1", stream)
    timings = {t.item_id: t for t in profile.question_timings}
    assert timings["q2"].latency_ms == 0
    assert timings["q2"].post_return_latency_ms is None
    # Only one answered question -> CV is 0 (needs >= 2 samples).
    assert profile.pacing_cv == 0.0


def test_unclosed_hide_is_dropped() -> None:
    # A hide with no matching return (tab closed) has no measurable end.
    stream = [
        ev("interaction", 0, "q1"),
        ev("visibility_hidden", 5),
    ]
    profile = extract_profile("att-1", stream)
    assert profile.hidden_intervals == []
    assert profile.total_hidden_ms == 0


def test_empty_stream_is_a_zeroed_profile() -> None:
    profile = extract_profile("att-1", [])
    assert profile.attempt_id == "att-1"
    assert profile.question_timings == []
    assert profile.hidden_intervals == []
    assert profile.total_hidden_ms == 0
    assert profile.pacing_cv == 0.0
    assert profile.systematicity_rate == 0.0


def test_coefficient_of_variation_helper() -> None:
    assert _coefficient_of_variation([]) == 0.0
    assert _coefficient_of_variation([100]) == 0.0
    assert _coefficient_of_variation([100, 100, 100]) == 0.0  # no spread
    assert _coefficient_of_variation([10, 30]) == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# Determinism: order in -> identical profile out.
# --------------------------------------------------------------------------- #
def test_extractor_is_deterministic_under_reordering() -> None:
    # The golden stream has all-distinct server_ts, so sorting by server_ts
    # recovers the one canonical order from ANY permutation, byte-for-byte.
    stream = _golden_stream()
    baseline = extract_profile("att-1", stream).model_dump()

    scrambled = [stream[i] for i in (5, 0, 7, 2, 4, 1, 6, 3)]
    assert extract_profile("att-1", scrambled).model_dump() == baseline
    assert extract_profile("att-1", list(reversed(stream))).model_dump() == baseline


def test_equal_timestamp_ties_resolved_by_ingest_order() -> None:
    # When events collide on server_ts, the stable sort preserves the given
    # (ingest) order rather than reordering by type. The function stays pure
    # (same input -> same output); the canonical stream EventRepository supplies
    # (rows by id) is what pins the result.
    stream = [
        ev("interaction", 0, "q1"),
        ev("visibility_hidden", 5),
        ev("visibility_visible", 5),  # identical server_ts to the hide
        ev("answer_change", 5, "q1"),
    ]
    first = extract_profile("att-1", stream).model_dump()
    assert extract_profile("att-1", stream).model_dump() == first  # pure

    # hide-then-visible at the same instant -> a bounded zero-length interval,
    # not a dropped (unclosed) hide.
    profile = extract_profile("att-1", stream)
    assert len(profile.hidden_intervals) == 1
    assert profile.hidden_intervals[0].duration_ms == 0


def test_multiple_hidden_intervals_accumulate() -> None:
    stream = [
        ev("visibility_hidden", 1),
        ev("visibility_visible", 4),  # 3000 ms
        ev("visibility_hidden", 10),
        ev("visibility_visible", 16),  # 6000 ms
    ]
    profile = extract_profile("att-1", stream)
    assert [iv.duration_ms for iv in profile.hidden_intervals] == [3000, 6000]
    assert profile.total_hidden_ms == 9000


# --------------------------------------------------------------------------- #
# Service reads through the append-only repository (telemetry owns the stream).
# --------------------------------------------------------------------------- #
def test_service_profiles_from_persisted_events(session: Session) -> None:
    repo = EventRepository(session)
    for event in _golden_stream():
        # Re-stamp server_ts via the repo (the trusted ingest path); insertion
        # order is chronological so the read-back order matches.
        repo.add_event(event.model_copy(update={"server_ts": None}))

    profile = IntegrityService(repo).profile("att-1")
    assert [t.item_id for t in profile.question_timings] == ["q1", "q2", "q3"]
    assert len(profile.hidden_intervals) == 1
    # The service result equals extracting directly over the read-back stream.
    direct = extract_profile("att-1", repo.list_events("att-1"))
    assert profile.model_dump() == direct.model_dump()


# --------------------------------------------------------------------------- #
# Invariant #6 / #2: deterministic features only — no LLM, grading, or analysis.
# --------------------------------------------------------------------------- #
def test_integrity_engine_calls_no_llm_and_does_not_judge() -> None:
    # AST-level import check (not substring), so docstrings may name the layers.
    forbidden = ("app.grading", "app.analysis", "anthropic", "instructor", "openai")
    integrity_dir = Path(__file__).resolve().parent.parent / "app" / "integrity"
    for path in integrity_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(forbidden), path.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden), path.name
