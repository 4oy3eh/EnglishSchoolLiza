"""Results engine: finalize an attempt ONCE — grade it and write its report.

This is the composition that runs when an attempt is submitted: it grades the
attempt (deterministic objective items + the Gemini writing grader), builds the
per-skill pedagogical feedback, and persists a single frozen `AttemptReport`. The
teacher dashboard then reads that report instead of re-running the LLM on every load.

It composes the grading and feedback engines but keeps their boundaries: grading
never sees integrity, feedback never sees a cheating signal, and a feedback verdict
never moves a score (golden rules #2/#6). The trigger lives in the API layer
(delivery's submit endpoint) as a background task — delivery itself stays grading-free.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.logging import get_logger
from app.feedback import FeedbackService
from app.grading.service import GradingService
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import AttemptReport

log = get_logger(__name__)


class ResultsError(Exception):
    """Finalization referenced something that does not exist."""


class ResultsService:
    """Grade + report an attempt once, and persist the frozen result."""

    def __init__(
        self,
        *,
        content: ContentRepository,
        attempts: AttemptRepository,
        grading: GradingService,
        feedback: FeedbackService,
    ) -> None:
        self.content = content
        self.attempts = attempts
        self.grading = grading
        self.feedback = feedback

    def finalize(self, attempt_id: str) -> AttemptReport:
        """Grade the attempt once, build its skill report, and persist both.

        Resilient: the writing grader already degrades to needs-review on a Gemini
        error, and a failed feedback LLM is caught here so the score is still saved
        (status="failed" so the teacher can re-grade). Never raises on LLM failure.
        """
        attempt = self.attempts.get_attempt(attempt_id)
        if attempt is None:
            raise ResultsError(f"attempt {attempt_id!r} not found")
        test = self.content.get_test(attempt.test_id)
        if test is None:  # pragma: no cover - attempt implies a test
            raise ResultsError(f"test {attempt.test_id!r} not found")

        grading = self.grading.grade(attempt_id)
        answers = {a.item_id: a.response for a in self.attempts.get_answers(attempt_id)}

        status: str = "complete"
        summary = ""
        model_id = None
        skills = []
        try:
            skills, summary, model_id = self.feedback.build_report(test, grading, answers)
        except Exception as exc:  # feedback LLM failed -> keep the grade, flag for re-grade
            log.warning("finalize attempt=%s feedback failed (%s)", attempt_id, exc)
            status = "failed"

        report = AttemptReport(
            attempt_id=attempt_id,
            graded_at=datetime.now(UTC),
            status=status,  # type: ignore[arg-type]
            grading=grading,
            skills=skills,
            summary=summary,
            model_id=model_id,
        )
        self.attempts.upsert_report(report)
        log.info(
            "finalize attempt=%s score=%.2f/%.2f skills=%d status=%s",
            attempt_id,
            grading.score,
            grading.max_score,
            len(skills),
            status,
        )
        return report
