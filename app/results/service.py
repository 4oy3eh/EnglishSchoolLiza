"""Results engine: finalize an attempt ONCE — grade it and write its report.

This is the composition that runs when an attempt is submitted: it grades the
attempt (deterministic objective items + the Gemini writing grader), builds the
per-skill pedagogical feedback, and persists a single frozen `AttemptReport`. The
teacher dashboard then reads that report instead of re-running the LLM on every load.

It composes the grading and feedback engines but keeps their boundaries: grading
never sees integrity, feedback never sees a cheating signal, and a feedback verdict
never moves a score (golden rules #2/#6). The trigger lives in the API layer
(delivery's submit endpoint) as a background task — delivery itself stays grading-free.

Failure is persisted, not swallowed: a finalize that blows up ends as a `failed`
report carrying the error (`record_failure`), and `finalize_missing` recovers every
finished attempt whose report is missing or failed (lost background task, outage).
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.logging import get_logger
from app.feedback import FeedbackService
from app.grading.service import GradingService
from app.persistence.repository import AttemptRepository, ContentRepository
from app.results.locks import attempt_lock, is_locked
from contracts import AttemptReport, GradingResult

log = get_logger(__name__)

# `AttemptReport.error` is capped so a stack of nested causes can't bloat the row.
ERROR_MAX_CHARS = 500


class ResultsError(Exception):
    """Finalization referenced something that does not exist."""


def describe_error(exc: BaseException, limit: int = ERROR_MAX_CHARS) -> str:
    """`<ExceptionType>: message`, truncated — what the teacher sees on a failed report."""
    text = f"{type(exc).__name__}: {exc}".strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


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
        (status="failed" + `error`, so the teacher can re-grade). Never raises on LLM
        failure. Serialized per attempt (`app.results.locks`): a teacher regrade that
        races the submit-time background finalize waits, then overwrites.
        """
        with attempt_lock(attempt_id):
            return self._finalize_locked(attempt_id)

    def _finalize_locked(self, attempt_id: str) -> AttemptReport:
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
        error: str | None = None
        try:
            skills, summary, model_id = self.feedback.build_report(test, grading, answers)
        except Exception as exc:  # feedback LLM failed -> keep the grade, flag for re-grade
            log.warning("finalize attempt=%s feedback failed (%s)", attempt_id, exc)
            status = "failed"
            error = describe_error(exc)

        report = AttemptReport(
            attempt_id=attempt_id,
            graded_at=datetime.now(UTC),
            status=status,  # type: ignore[arg-type]
            grading=grading,
            skills=skills,
            summary=summary,
            model_id=model_id,
            error=error,
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

    def record_failure(self, attempt_id: str, exc: BaseException) -> AttemptReport:
        """Persist a `failed` report after `finalize` itself blew up.

        Called by the background entrypoint / the sweep when the one-shot finalize
        raised past the feedback guard (grader crash, DB error, bug). The row carries
        the error and the DETERMINISTIC grade (objective items only, writing ->
        needs-review, no LLM) when that still works, else an empty zero grade — so
        the attempt never sits `submitted` with no report and the dashboard can
        show the cause and offer a re-grade instead of "grading…" forever.
        """
        with attempt_lock(attempt_id):
            error = describe_error(exc)
            try:
                grading = GradingService(self.content, self.attempts, llm_grader=None).grade(
                    attempt_id
                )
            except Exception as grade_exc:
                log.warning(
                    "record_failure attempt=%s deterministic grade also failed (%s)",
                    attempt_id,
                    grade_exc,
                )
                # A DB error leaves the session poisoned; clear it so the failed
                # report below can still be written.
                self.attempts.session.rollback()
                grading = GradingResult(
                    attempt_id=attempt_id, items=[], score=0.0, max_score=0.0, needs_review=True
                )
            report = AttemptReport(
                attempt_id=attempt_id,
                graded_at=datetime.now(UTC),
                status="failed",
                grading=grading,
                skills=[],
                summary="",
                model_id=None,
                error=error,
            )
            self.attempts.upsert_report(report)
            log.warning("finalize attempt=%s recorded as failed: %s", attempt_id, error)
            return report

    def list_missing(
        self,
        test_id: str | None = None,
        *,
        failed_before: datetime | None = None,
        finished_before: datetime | None = None,
        exempt: frozenset[str] = frozenset(),
    ) -> list[str]:
        """Attempt ids whose finalize must be (re)run: no report, or a `failed` one.

        Read-only — it only *selects*; the caller finalizes each id in its own
        transaction (`app.results.finalize.finalize_missing`), so no write lock is
        held across another attempt's LLM calls. Skips an attempt whose finalize
        is in flight right now (lock held: no duplicate paid run), and — when
        `failed_before` is given (the automatic sweep) — a `failed` report younger
        than that cutoff, so a restart doesn't hammer a freshly failed attempt.
        `finished_before` (the sweep) also skips an attempt that finished after
        that cutoff — its submit/lazy-expiry request scheduled a background
        finalize that owns it; `exempt` ids (attempts the sweep expired itself, so
        no background task exists) bypass that age filter.
        """
        picked: list[str] = []
        for cand in self.attempts.list_attempts_missing_report(test_id):
            if is_locked(cand.attempt_id):
                log.info("list_missing attempt=%s skipped: finalize in flight", cand.attempt_id)
                continue
            if (
                failed_before is not None
                and cand.report_status == "failed"
                and cand.graded_at is not None
                and cand.graded_at >= failed_before
            ):
                log.info(
                    "list_missing attempt=%s skipped: failed at %s (younger than cutoff)",
                    cand.attempt_id,
                    cand.graded_at.isoformat(timespec="seconds"),
                )
                continue
            if (
                finished_before is not None
                and cand.attempt_id not in exempt
                and cand.report_status is None
                and cand.finished_at is not None
                and cand.finished_at > finished_before
            ):
                log.info(
                    "list_missing attempt=%s skipped: finished at %s, its background "
                    "finalize owns it",
                    cand.attempt_id,
                    cand.finished_at.isoformat(timespec="seconds"),
                )
                continue
            picked.append(cand.attempt_id)
        log.info("list_missing test=%s -> %d attempt(s)", test_id or "*", len(picked))
        return picked
