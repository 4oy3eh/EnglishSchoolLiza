"""Phase 8: analysis engine — advisory LLM cheating-likelihood verdict.

Gate (docs/PROMPTS.md Prompt 8): the verdict validates against the schema; the LLM is
mocked deterministically; the score is never touched (golden rule #2). Plus segment
flagging goldens, the mock's monotone suspicion, the no-analyst fallback, and the
service reading through the repo + integrity.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.analysis import (
    AnalysisService,
    FlaggedSegment,
    MockAnalysisLLM,
    flag_segments,
)
from app.analysis.segments import LONG_HIDDEN_MS
from app.grading import GradingService, MockLLMGrader
from app.integrity import IntegrityService, extract_profile
from app.persistence.repository import (
    AttemptRepository,
    ContentRepository,
    EventRepository,
)
from contracts import (
    AnalysisVerdict,
    Answer,
    Attempt,
    IntegrityEvent,
    PassageTextStimulus,
    Section,
    SingleChoiceItem,
    TextOption,
)
from contracts import Test as ExamTest

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


def _cheaty_stream() -> list[IntegrityEvent]:
    """q1 cold; q2 answered 1s after a 13s tab return; q3 answered slowly."""
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


def _honest_stream() -> list[IntegrityEvent]:
    """Two questions answered cold, no hiding, no fast post-return pattern."""
    return [
        ev("interaction", 0, "q1"),
        ev("answer_change", 8, "q1", {"value": "A"}),
        ev("interaction", 9, "q2"),
        ev("answer_change", 19, "q2", {"value": "B"}),
    ]


# --------------------------------------------------------------------------- #
# Deterministic segment flagging.
# --------------------------------------------------------------------------- #
def test_flag_segments_golden() -> None:
    events = _cheaty_stream()
    profile = extract_profile("att-1", events)
    segments = flag_segments(profile, events)

    assert [s.kind for s in segments] == ["long_hidden", "fast_post_return"]

    long_hidden = segments[0]
    assert long_hidden.start == T0 + timedelta(seconds=7)
    assert long_hidden.end == T0 + timedelta(seconds=20)
    # The raw hide + return events fall inside the window (rule #6 auditability).
    assert {e.type for e in long_hidden.events} == {
        "visibility_hidden",
        "visibility_visible",
    }

    fast = segments[1]
    assert fast.item_id == "q2"
    assert {e.item_id for e in fast.events} == {"q2"}


def test_short_hidden_interval_is_not_flagged() -> None:
    # A hide shorter than LONG_HIDDEN_MS produces no long_hidden segment.
    short = (LONG_HIDDEN_MS - 1000) / 1000
    events = [
        ev("interaction", 0, "q1"),
        ev("visibility_hidden", 1),
        ev("visibility_visible", 1 + short),
        ev("answer_change", 30, "q1"),  # answered long after, no fast post-return
    ]
    profile = extract_profile("att-1", events)
    assert flag_segments(profile, events) == []


def test_honest_stream_flags_nothing() -> None:
    events = _honest_stream()
    profile = extract_profile("att-1", events)
    assert flag_segments(profile, events) == []


# --------------------------------------------------------------------------- #
# The deterministic mock analyst.
# --------------------------------------------------------------------------- #
def test_mock_verdict_is_deterministic_and_in_range() -> None:
    events = _cheaty_stream()
    profile = extract_profile("att-1", events)
    segments = flag_segments(profile, events)

    first = MockAnalysisLLM().analyze(profile, segments)
    second = MockAnalysisLLM().analyze(profile, segments)
    assert first == second  # pure given the same inputs

    # 0.6 * (1/3 systematicity) + 0.4 * (13000/60000 hidden) = 0.2867.
    assert first.suspicion_score == pytest.approx(0.2867, abs=1e-4)
    assert 0.0 <= first.suspicion_score <= 1.0
    assert first.confidence == pytest.approx(0.3)  # 3 questions / ref 10
    assert first.flags == ("long_hidden", "fast_post_return")
    assert first.model_id == "mock-analysis"


def test_mock_suspicion_is_monotone_cheaty_beats_honest() -> None:
    cheaty = _cheaty_stream()
    honest = _honest_stream()
    cheaty_v = MockAnalysisLLM().analyze(
        extract_profile("att-1", cheaty), flag_segments(extract_profile("att-1", cheaty), cheaty)
    )
    honest_p = extract_profile("att-1", honest)
    honest_v = MockAnalysisLLM().analyze(honest_p, flag_segments(honest_p, honest))

    assert honest_v.suspicion_score == 0.0
    assert honest_v.flags == ()
    assert cheaty_v.suspicion_score > honest_v.suspicion_score


def test_mock_empty_profile_is_zeroed() -> None:
    verdict = MockAnalysisLLM().analyze(extract_profile("att-1", []), [])
    assert verdict.suspicion_score == 0.0
    assert verdict.confidence == 0.0
    assert verdict.flags == ()
    assert "No notable" in verdict.summary


# --------------------------------------------------------------------------- #
# Service: profile -> segments -> verdict, validating against the contract.
# --------------------------------------------------------------------------- #
def _seed_events(session: Session, events: list[IntegrityEvent]) -> EventRepository:
    repo = EventRepository(session)
    for event in events:  # server_ts already stamped (T0-based) -> add_event keeps it
        repo.add_event(event)
    session.commit()
    return repo


def test_service_builds_verdict_through_repo_and_integrity(session: Session) -> None:
    repo = _seed_events(session, _cheaty_stream())
    svc = AnalysisService(repo, IntegrityService(repo), llm=MockAnalysisLLM())

    verdict = svc.analyze("att-1")

    assert isinstance(verdict, AnalysisVerdict)
    assert verdict.attempt_id == "att-1"
    assert verdict.suspicion_score == pytest.approx(0.2867, abs=1e-4)
    assert verdict.flags == ["long_hidden", "fast_post_return"]
    assert verdict.model_id == "mock-analysis"
    # Re-validates cleanly against its own schema (round-trip).
    assert AnalysisVerdict.model_validate(verdict.model_dump()) == verdict


def test_service_without_analyst_returns_neutral_verdict(session: Session) -> None:
    repo = _seed_events(session, _cheaty_stream())
    svc = AnalysisService(repo, IntegrityService(repo))  # no llm injected

    verdict = svc.analyze("att-1")
    assert verdict.suspicion_score == 0.0
    assert verdict.confidence == 0.0
    assert verdict.flags == []
    assert "No analyst configured" in verdict.summary


# --------------------------------------------------------------------------- #
# Golden rule #2: advisory only — the verdict carries no score and never moves one.
# --------------------------------------------------------------------------- #
def test_verdict_has_no_score_field() -> None:
    verdict = AnalysisVerdict(
        attempt_id="att-1", suspicion_score=0.9, confidence=0.5, summary="x"
    )
    dumped = verdict.model_dump()
    assert "score" not in dumped
    assert "awarded" not in dumped
    assert not hasattr(verdict, "score")


def test_running_analysis_does_not_change_the_grade(session: Session) -> None:
    # One single_choice item, answered correctly; grade it, then run analysis over
    # the same attempt's telemetry and confirm a re-grade is byte-for-byte identical.
    test = ExamTest(
        id="t1",
        title="A2 Key — Mock",
        level="A2_KEY",
        status="published",
        duration_minutes=30,
        sections=[
            Section(
                id="sec-1",
                skill="reading",
                stimulus=PassageTextStimulus(text="Read this."),
                items=[
                    SingleChoiceItem(
                        id="q1",
                        prompt="Pick one",
                        options=[
                            TextOption(key="A", text="alpha"),
                            TextOption(key="B", text="bravo"),
                        ],
                        correct="A",
                    )
                ],
            )
        ],
    )
    ContentRepository(session).add_test(test)
    attempts = AttemptRepository(session)
    attempts.add_attempt(
        Attempt(id="att-1", test_id="t1", roster_entry_id="r1", status="submitted", seed=1)
    )
    attempts.save_answer(
        Answer(attempt_id="att-1", item_id="q1", response="A", answered_at=T0)
    )
    repo = _seed_events(session, _cheaty_stream())
    session.commit()

    grading = GradingService(
        ContentRepository(session), AttemptRepository(session), llm_grader=MockLLMGrader()
    )
    before = grading.grade("att-1")

    verdict = AnalysisService(repo, IntegrityService(repo), llm=MockAnalysisLLM()).analyze(
        "att-1"
    )
    assert verdict.suspicion_score > 0.0  # analysis actually ran and flagged something

    after = grading.grade("att-1")
    assert before.model_dump() == after.model_dump()  # score untouched (rule #2)
    assert before.score == 1.0


# --------------------------------------------------------------------------- #
# Invariant #2: analysis never imports grading. (It MAY import the LLM SDK — it is
# the LLM layer — so the guard forbids only app.grading, not anthropic/instructor.)
# --------------------------------------------------------------------------- #
def test_analysis_engine_never_imports_grading() -> None:
    forbidden = ("app.grading",)
    analysis_dir = Path(__file__).resolve().parent.parent / "app" / "analysis"
    for path in analysis_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(forbidden), path.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden), path.name


def test_flagged_segment_defaults() -> None:
    seg = FlaggedSegment(kind="x", reason="y")
    assert seg.item_id is None
    assert seg.events == ()
