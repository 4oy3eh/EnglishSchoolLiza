"""Results engine: finalize an attempt once (grade + skill report) and persist it.

Gate: finalize grades the attempt (objective + the injected writing grader), builds
the per-skill report, and persists ONE frozen `AttemptReport`; re-finalize overwrites
(idempotent); a feedback LLM that raises still saves the score with status="failed";
the report round-trips through the repository.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.feedback import FeedbackService, MockFeedbackAnalyst
from app.grading.llm import MockLLMGrader
from app.grading.service import GradingService
from app.persistence.repository import AttemptRepository, ContentRepository
from app.results import ResultsError, ResultsService
from contracts import (
    Answer,
    Attempt,
    GapFillItem,
    OpenWritingItem,
    PassageTextStimulus,
    RosterEntry,
    Section,
)
from contracts import Test as ExamTest

T0 = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)


def _seed(session: Session) -> str:
    content = ContentRepository(session)
    content.add_test(
        ExamTest(
            id="t1",
            title="B1 — t1",
            level="B1_PRELIMINARY",
            status="published",
            duration_minutes=60,
            sections=[
                Section(
                    id="read",
                    skill="reading",
                    stimulus=PassageTextStimulus(text="Read."),
                    items=[GapFillItem(id="r1", prompt="gap", accepted=["cat"])],
                ),
                Section(
                    id="write",
                    skill="writing",
                    stimulus=PassageTextStimulus(text="Write."),
                    items=[
                        OpenWritingItem(
                            id="w1",
                            prompt="Write a note.",
                            word_min=25,
                            bullet_points=["say hi"],
                            rubric="B1 note.",
                        )
                    ],
                ),
            ],
        )
    )
    attempts = AttemptRepository(session)
    entry = RosterEntry(id="e1", test_id="t1", display_name="Ann")
    attempts.add_roster_entry(entry)
    attempts.add_attempt(
        Attempt(
            id="att1",
            test_id="t1",
            roster_entry_id="e1",
            status="submitted",
            seed=1,
            started_at=T0,
            submitted_at=T0 + timedelta(minutes=5),
            deadline=T0 + timedelta(hours=1),
        )
    )
    attempts.save_answer(Answer(attempt_id="att1", item_id="r1", response="cat", answered_at=T0))
    attempts.save_answer(
        Answer(attempt_id="att1", item_id="w1", response="hi i am here today", answered_at=T0)
    )
    session.commit()
    return "att1"


def _service(session: Session, *, feedback_llm=None) -> ResultsService:
    if feedback_llm is None:
        feedback_llm = MockFeedbackAnalyst()
    content = ContentRepository(session)
    attempts = AttemptRepository(session)
    return ResultsService(
        content=content,
        attempts=attempts,
        grading=GradingService(content, attempts, llm_grader=MockLLMGrader()),
        feedback=FeedbackService(llm=feedback_llm),
    )


def test_finalize_persists_grade_and_skills(session: Session) -> None:
    attempt_id = _seed(session)
    report = _service(session).finalize(attempt_id)

    assert report.status == "complete"
    # The writing grade was computed by the injected grader (method reflects the LLM
    # path, not the manual-review fallback you'd get with no grader injected).
    w1 = next(g for g in report.grading.items if g.item_id == "w1")
    assert w1.method == "open_writing_llm"
    # Per-skill report covers both skills, with scores from the grading.
    assert {s.skill for s in report.skills} == {"reading", "writing"}

    # Persisted and reloadable through the repository.
    stored = AttemptRepository(session).get_report(attempt_id)
    assert stored is not None
    assert stored.grading.score == report.grading.score
    assert {s.skill for s in stored.skills} == {"reading", "writing"}


def test_refinalize_is_idempotent(session: Session) -> None:
    attempt_id = _seed(session)
    svc = _service(session)
    svc.finalize(attempt_id)
    svc.finalize(attempt_id)  # second run must overwrite, not duplicate
    # Still exactly one row (PK is attempt_id), and it reloads.
    assert AttemptRepository(session).get_report(attempt_id) is not None


def test_failed_feedback_still_saves_score(session: Session) -> None:
    class _Boom:
        def analyze(self, breakdowns):  # noqa: ANN001
            raise RuntimeError("gemini down")

    attempt_id = _seed(session)
    report = _service(session, feedback_llm=_Boom()).finalize(attempt_id)

    assert report.status == "failed"
    assert report.grading.score > 0  # the grade survived the feedback failure
    assert AttemptRepository(session).get_report(attempt_id).status == "failed"


def test_finalize_unknown_attempt_raises(session: Session) -> None:
    with pytest.raises(ResultsError):
        _service(session).finalize("nope")
