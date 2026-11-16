"""Results engine: finalize an attempt ONCE — grade it and write its report.

This is the composition that runs when an attempt is submitted: it grades the
attempt (deterministic objective items + the Gemini writing grader), builds the
per-skill pedagogical feedback, and persists a single frozen `AttemptReport`. The
teacher dashboard then reads that report instead of re-running the LLM on every load.

It composes the grading and feedback engines but keeps their boundaries: grading
never sees integrity, feedback never sees a cheating signal, and a feedback verdict
never moves a score (golden rules #2/#6). After grading + feedback it also runs the
integrity -> analysis layers ONCE and stores the deterministic `IntegrityProfile` and
the advisory `AnalysisVerdict` next to (never inside) the grade; an analysis failure
never fails the grade (deterministic-only verdict + `error`). Results is the only
place holding both axes, so it is also where the opaque objective percentage is
handed to the analyst as level-mismatch evidence — input only, nothing flows back
into a score. The trigger lives in the API layer
(delivery's submit endpoint) as a background task — delivery itself stays grading-free.

Failure is persisted, not swallowed: a finalize that blows up ends as a `failed`
report carrying the error (`record_failure`), and `finalize_missing` recovers every
finished attempt whose report is missing or failed (lost background task, outage).

Cost control for those automatic retries:

* **The LLM verdict is paid for once per input.** Every stored verdict carries an
  `input_fingerprint` (event count + last event id + a digest of the analysis
  context). A service built with `reuse_verdict=True` (the background / sweep /
  "grade all missing" path) reuses a stored LLM verdict whose fingerprint still
  matches instead of calling the analyst again; the teacher's explicit regrade
  (`reuse_verdict=False`, the default) always recomputes.
* **Automatic retries are capped.** A `failed` report written by automatic retry
  number n carries ` [auto-retry n]` at the end of its `error`; the sweep stops
  after `MAX_AUTO_RETRIES` (see `app.results.finalize`).

Nothing logged or stored here may carry a credential: every exception string goes
through `app.results.redact.redact_secrets` (`describe_error`).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime

from sqlalchemy import func, select

from app.analysis import AnalysisContext, AnalysisService, WritingTask
from app.analysis.llm import AnalysisLLM
from app.core.logging import get_logger
from app.feedback import FeedbackService
from app.grading.service import GradingService
from app.integrity import IntegrityService
from app.persistence import models as pm
from app.persistence.repository import AttemptRepository, ContentRepository, EventRepository
from app.results import analyst as _analyst
from app.results.locks import attempt_lock, is_locked
from app.results.redact import redact_secrets
from contracts import (
    AnalysisVerdict,
    AttemptReport,
    GradingResult,
    IntegrityProfile,
    OpenWritingItem,
    Test,
)

log = get_logger(__name__)

# `AttemptReport.error` is capped so a stack of nested causes can't bloat the row.
ERROR_MAX_CHARS = 500

# Item-grade methods that make up the "objective sections" percentage handed to the
# analyst (opaque level-mismatch evidence; never fed back into a score).
OBJECTIVE_METHODS = frozenset({"single_choice", "gap_fill", "matching"})

# The sweep retries a `failed` report at most this many times on its own; after
# that it waits for the teacher (single regrade or "grade all missing").
MAX_AUTO_RETRIES = 3
# ` [auto-retry n]` at the very end of `AttemptReport.error`: how many AUTOMATIC
# retries produced this failed report. Kept in the error text (visible to the
# teacher, no schema change / migration).
_AUTO_RETRY_RE = re.compile(r" \[auto-retry (\d+)\]$")
# Bump when the analysis inputs/prompt change in a way that must invalidate the
# stored verdicts' fingerprints.
FINGERPRINT_VERSION = 1


class _FromSettings:
    """Sentinel: resolve the analyst via `app.results.analyst.default_analysis_llm`."""


FROM_SETTINGS = _FromSettings()


class ResultsError(Exception):
    """Finalization referenced something that does not exist."""


def describe_error(exc: BaseException, limit: int = ERROR_MAX_CHARS) -> str:
    """`<ExceptionType>: message`, truncated — what the teacher sees on a failed report.

    Credentials are scrubbed first (`redact_secrets`): this string is logged AND
    stored in `attempt_reports.error`, so it must never carry an API key.
    """
    text = redact_secrets(f"{type(exc).__name__}: {exc}".strip())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def auto_retry_count(error: str | None) -> int:
    """How many automatic retries a failed report's `error` records (0 = none)."""
    match = _AUTO_RETRY_RE.search(error or "")
    return int(match.group(1)) if match else 0


def with_auto_retry(error: str, attempt_no: int, limit: int = ERROR_MAX_CHARS) -> str:
    """`error` + the ` [auto-retry n]` marker (n >= 1), still within `limit`."""
    if attempt_no <= 0:
        return error
    marker = f" [auto-retry {attempt_no}]"
    base = _AUTO_RETRY_RE.sub("", error)
    room = limit - len(marker)
    if len(base) > room:
        base = base[: room - 1] + "…"
    return base + marker


class ResultsService:
    """Grade + report an attempt once, and persist the frozen result."""

    def __init__(
        self,
        *,
        content: ContentRepository,
        attempts: AttemptRepository,
        grading: GradingService,
        feedback: FeedbackService,
        analysis_llm: AnalysisLLM | None | _FromSettings = FROM_SETTINGS,
        reuse_verdict: bool = False,
        auto_retry: int = 0,
    ) -> None:
        self.content = content
        self.attempts = attempts
        self.grading = grading
        self.feedback = feedback
        # True on the automatic paths (background task, sweep, "grade all missing"):
        # a stored LLM verdict whose input fingerprint still matches is reused
        # instead of paying for the same analysis again. False (the default — the
        # teacher's explicit regrade service) always recomputes.
        self.reuse_verdict = reuse_verdict
        # n >= 1 when this run is the n-th AUTOMATIC retry of a failed report
        # (set by `finalize_attempt`); a report that fails again records it.
        self.auto_retry = auto_retry
        # Default: the Gemini analyst from settings (None without a key). Kept a
        # keyword with a default so existing constructors (the admin layer's regrade
        # service) keep working and still get a verdict.
        self._analysis_llm = analysis_llm

    def _analyst(self) -> AnalysisLLM | None:
        if isinstance(self._analysis_llm, _FromSettings):
            return _analyst.default_analysis_llm()
        return self._analysis_llm

    # -- integrity / analysis (advisory, next to the grade) -------------------- #
    @staticmethod
    def analysis_context(
        test: Test, grading: GradingResult, answers: dict[str, str]
    ) -> AnalysisContext:
        """Writing tasks + texts, the level and the opaque objective percentage.

        An optional (choose-one) writing task the student left blank is skipped.
        """
        tasks: list[WritingTask] = []
        for section in test.sections:
            for item in section.items:
                if not isinstance(item, OpenWritingItem):
                    continue
                text = str(answers.get(item.id) or "")
                if item.optional and not text.strip():
                    continue
                tasks.append(
                    WritingTask(
                        item_id=item.id,
                        prompt=item.prompt,
                        text=text,
                        bullet_points=tuple(item.bullet_points),
                        word_min=item.word_min,
                    )
                )
        objective = [g for g in grading.items if g.method in OBJECTIVE_METHODS]
        max_points = sum(g.max_points for g in objective)
        pct = (
            round(100.0 * sum(g.awarded for g in objective) / max_points, 1)
            if max_points > 0
            else None
        )
        return AnalysisContext(level=test.level, writing=tuple(tasks), objective_pct=pct)

    def _fingerprint(self, attempt_id: str, context: AnalysisContext | None) -> str | None:
        """Identity of the analysis inputs: `v<ver>:<events>:<last event id>:<ctx digest>`.

        Read BEFORE the analysis runs, so an event that lands mid-analysis makes the
        stored fingerprint stale (-> recomputed next time), never falsely fresh.
        The context digest covers the level, the objective % and every writing
        task + submitted text. None when the event stream cannot be read.
        """
        try:
            count, last_id = self.attempts.session.execute(
                select(
                    func.count(pm.IntegrityEventRow.id), func.max(pm.IntegrityEventRow.id)
                ).where(pm.IntegrityEventRow.attempt_id == attempt_id)
            ).one()
        except Exception as exc:
            log.warning(
                "finalize attempt=%s cannot fingerprint the event stream (%s)",
                attempt_id,
                describe_error(exc),
            )
            return None
        ctx = context or AnalysisContext()
        payload = json.dumps(
            {
                "level": ctx.level,
                "objective_pct": ctx.objective_pct,
                "writing": [
                    [t.item_id, t.prompt, t.text, list(t.bullet_points), t.word_min]
                    for t in ctx.writing
                ],
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return f"v{FINGERPRINT_VERSION}:{int(count or 0)}:{int(last_id or 0)}:{digest}"

    def _reusable(
        self, attempt_id: str, fingerprint: str | None
    ) -> tuple[IntegrityProfile, AnalysisVerdict] | None:
        """The stored (profile, LLM verdict) when it was computed from these inputs.

        Only an LLM verdict (`model_id` set, no `error`) is worth reusing — the
        deterministic one is free to recompute. Never raises.
        """
        if fingerprint is None:
            return None
        try:
            stored = self.attempts.get_report(attempt_id)
        except Exception as exc:  # a malformed old row: just recompute
            log.warning(
                "finalize attempt=%s stored report unreadable (%s) -> recompute verdict",
                attempt_id,
                describe_error(exc),
            )
            return None
        if stored is None or stored.verdict is None or stored.integrity is None:
            return None
        verdict = stored.verdict
        if verdict.model_id is None or verdict.error is not None:
            return None
        if verdict.input_fingerprint != fingerprint:
            return None
        return stored.integrity, verdict

    def _assess(
        self,
        attempt_id: str,
        context: AnalysisContext | None,
        *,
        use_llm: bool = True,
    ) -> tuple[IntegrityProfile | None, AnalysisVerdict | None]:
        """Integrity profile + advisory verdict. NEVER raises.

        The analysis service already degrades an analyst failure to the
        deterministic verdict; anything else (a malformed stored event, a DB hiccup)
        is caught here and logged, and NO verdict is stored (`(None, None)`): a
        made-up "suspicion 0.0" would rank the attempt as a scored, clean row. The
        dashboard shows "—" for a missing verdict and a regrade fills it. The grade
        is always saved.
        """
        events = EventRepository(self.attempts.session)
        service = AnalysisService(
            events,
            IntegrityService(events),
            llm=self._analyst() if use_llm else None,
        )
        try:
            return service.assess(attempt_id, context=context)
        except Exception as exc:
            log.error(
                "finalize attempt=%s analysis failed (%s) -> no verdict stored "
                "(dashboard shows it as not assessed; a regrade retries)",
                attempt_id,
                describe_error(exc),
            )
            return None, None

    def get_verdict(self, attempt_id: str) -> AnalysisVerdict | None:
        """The stored advisory verdict (computed once at finalize), or None.

        Read path for the dashboard: no LLM, no recompute. None when the attempt has
        no report yet, or its report predates verdict persistence (re-grade fills it).
        """
        return self.attempts.get_verdict(attempt_id)

    def get_integrity(self, attempt_id: str) -> IntegrityProfile | None:
        """The stored integrity profile the verdict was computed from, or None."""
        report = self.attempts.get_report(attempt_id)
        return report.integrity if report is not None else None

    def finalize(self, attempt_id: str) -> AttemptReport:
        """Grade the attempt once, build its skill report, and persist both.

        Resilient: the writing grader already degrades to needs-review on a Gemini
        error, and a failed feedback LLM is caught here so the score is still saved
        (status="failed" + `error`, so the teacher can re-grade). Never raises on LLM
        failure. Serialized per attempt (`app.results.locks`): a teacher regrade that
        races the submit-time background finalize waits, then overwrites.

        The integrity verdict is recomputed unless this service was built with
        `reuse_verdict=True` AND the stored LLM verdict's input fingerprint still
        matches (see the module docstring) — the explicit regrade always recomputes.
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
            error = self._failure_text(attempt_id, exc)
            log.warning("finalize attempt=%s feedback failed (%s)", attempt_id, error)
            status = "failed"

        # Integrity axis, computed once and stored next to the grade. Its inputs
        # include the grade-derived objective % (analysis input only); its output
        # never touches `grading` (golden rule #2).
        context = self.analysis_context(test, grading, answers)
        fingerprint = self._fingerprint(attempt_id, context)
        reused = self._reusable(attempt_id, fingerprint) if self.reuse_verdict else None
        if reused is not None:
            profile, verdict = reused
            log.info(
                "finalize attempt=%s reusing stored verdict model=%s (inputs unchanged, "
                "fingerprint=%s) -> no analyst call",
                attempt_id,
                verdict.model_id,
                fingerprint,
            )
        else:
            profile, verdict = self._assess(attempt_id, context)
            if verdict is not None:
                verdict = verdict.model_copy(update={"input_fingerprint": fingerprint})

        report = AttemptReport(
            attempt_id=attempt_id,
            graded_at=datetime.now(UTC),
            status=status,  # type: ignore[arg-type]
            grading=grading,
            skills=skills,
            summary=summary,
            model_id=model_id,
            error=error,
            integrity=profile,
            verdict=verdict,
        )
        self.attempts.upsert_report(report)
        log.info(
            "finalize attempt=%s score=%.2f/%.2f skills=%d status=%s "
            "suspicion=%.3f verdict_model=%s",
            attempt_id,
            grading.score,
            grading.max_score,
            len(skills),
            status,
            verdict.suspicion_score if verdict is not None else float("nan"),
            verdict.model_id if verdict is not None else "-",
        )
        return report

    def _failure_text(self, attempt_id: str, exc: BaseException) -> str:
        """The scrubbed error for a failed report (+ the automatic-retry marker)."""
        error = with_auto_retry(describe_error(exc), self.auto_retry)
        if self.auto_retry >= MAX_AUTO_RETRIES:
            log.warning(
                "finalize attempt=%s failed on automatic retry %d of %d -> no further "
                "automatic retries; needs a teacher regrade",
                attempt_id,
                self.auto_retry,
                MAX_AUTO_RETRIES,
            )
        return error

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
            error = self._failure_text(attempt_id, exc)
            try:
                grading = GradingService(self.content, self.attempts, llm_grader=None).grade(
                    attempt_id
                )
            except Exception as grade_exc:
                log.warning(
                    "record_failure attempt=%s deterministic grade also failed (%s)",
                    attempt_id,
                    describe_error(grade_exc),
                )
                # A DB error leaves the session poisoned; clear it so the failed
                # report below can still be written.
                self.attempts.session.rollback()
                grading = GradingResult(
                    attempt_id=attempt_id, items=[], score=0.0, max_score=0.0, needs_review=True
                )
            # Best effort, no LLM (this is the crash path): the deterministic verdict
            # (or none at all when even that cannot be computed).
            profile, verdict = self._assess(attempt_id, None, use_llm=False)
            if profile is None:
                self.attempts.session.rollback()
            report = AttemptReport(
                attempt_id=attempt_id,
                graded_at=datetime.now(UTC),
                status="failed",
                grading=grading,
                skills=[],
                summary="",
                model_id=None,
                error=error,
                integrity=profile,
                verdict=verdict,
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
