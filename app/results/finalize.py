"""Background finalize entrypoint — wire a `ResultsService` from settings + run it.

Called as a FastAPI background task right after an attempt is submitted (see
`apps/api/delivery.py`). It opens its OWN session (the request session is already
closed by the time the task runs) and commits via `session_scope`, then grades +
reports the attempt once and persists the frozen `AttemptReport`.

The Gemini grader/feedback analyst/integrity analyst are built only when
`GEMINI_API_KEY` is configured; without it, writing falls back to manual review,
the report is scores-only and the stored verdict is the deterministic-only one.

`finalize_missing` is the recovery loop behind both the teacher's "grade all
missing" button and `sweep_missing_reports`, the startup hook (a process restart
right after a submit loses the in-flight background task). It runs ONE
transaction per attempt — never several attempts' LLM calls inside one session.
With `expire_overdue=True` it first runs `expire_overdue_attempts` (the delivery
engine's expiry, in its own short committed transaction) so an attempt abandoned
past its deadline — never expired lazily because the student never came back — is
finalized too. The API process re-runs the sweep periodically (`apps/api/main.py`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.llm import AnalysisLLM
from app.core.config import settings
from app.core.db import session_scope
from app.core.logging import get_logger
from app.delivery import DeliveryService
from app.feedback import FeedbackService
from app.feedback.llm import FeedbackLLM
from app.grading.llm import LLMGrader
from app.grading.service import GradingService
from app.persistence import models as pm
from app.persistence.repository import AttemptRepository, ContentRepository
from app.results import analyst as _analyst
from app.results.locks import attempt_lock, is_locked
from app.results.redact import redacted_traceback
from app.results.service import (
    FROM_SETTINGS,
    MAX_AUTO_RETRIES,
    ResultsService,
    _FromSettings,
    auto_retry_count,
    describe_error,
)

log = get_logger(__name__)


@lru_cache(maxsize=1)
def _writing_grader() -> LLMGrader | None:
    if settings.gemini_api_key:
        from app.grading.llm_gemini import GeminiWritingGrader

        return GeminiWritingGrader(api_key=settings.gemini_api_key, model=settings.gemini_model)
    return None


@lru_cache(maxsize=1)
def _feedback_analyst() -> FeedbackLLM | None:
    if settings.gemini_api_key:
        from app.feedback.llm_gemini import GeminiFeedbackAnalyst

        return GeminiFeedbackAnalyst(api_key=settings.gemini_api_key, model=settings.gemini_model)
    return None


def build_results_service(
    session: Session,
    *,
    llm_grader: LLMGrader | None = None,
    feedback_llm: FeedbackLLM | None = None,
    analysis_llm: AnalysisLLM | None | _FromSettings = FROM_SETTINGS,
    reuse_verdict: bool = False,
    auto_retry: int = 0,
) -> ResultsService:
    """Construct a `ResultsService` and the engines it composes from a session.

    `analysis_llm` defaults to the settings-configured analyst
    (`app.results.analyst.default_analysis_llm`: Gemini with a key, else None ->
    deterministic-only verdict); pass None explicitly for deterministic-only.
    `reuse_verdict` / `auto_retry` are for the automatic paths only (see
    `ResultsService`); the default always recomputes the verdict.
    """
    content_repo = ContentRepository(session)
    attempt_repo = AttemptRepository(session)
    return ResultsService(
        content=content_repo,
        attempts=attempt_repo,
        grading=GradingService(content_repo, attempt_repo, llm_grader=llm_grader),
        feedback=FeedbackService(llm=feedback_llm),
        analysis_llm=analysis_llm,
        reuse_verdict=reuse_verdict,
        auto_retry=auto_retry,
    )


def _service(session: Session, *, auto_retry: int = 0) -> ResultsService:
    """The automatic-path service: reuses a still-valid stored LLM verdict."""
    return build_results_service(
        session,
        llm_grader=_writing_grader(),
        feedback_llm=_feedback_analyst(),
        analysis_llm=_analyst.default_analysis_llm(),
        reuse_verdict=True,
        auto_retry=auto_retry,
    )


def _log_failure(message: str, *args: object, exc: BaseException) -> None:
    """ERROR line + the traceback, both scrubbed of credentials.

    Used instead of `log.exception`: a raw traceback prints the exception message
    verbatim, and an LLM client's error can carry a request URL.
    """
    log.error(
        message + " (%s)\n%s", *args, describe_error(exc), redacted_traceback(exc)
    )


def _auto_retries_spent(session: Session, attempt_id: str) -> int | None:
    """Automatic retries already spent on this attempt's CURRENT failed report.

    None when there is nothing to retry automatically-with-a-budget: no report, a
    non-failed one, or a failed one left over from BEFORE the attempt's latest
    finish (re-open + re-submit starts a fresh budget). Read with plain column
    SELECTs inside the per-attempt lock.
    """
    marker = AttemptRepository(session).get_report_marker(attempt_id)
    if marker is None or marker.report_status != "failed" or marker.graded_at is None:
        return None
    if marker.finished_at is not None and marker.graded_at < marker.finished_at:
        return None
    error = session.execute(
        select(pm.AttemptReportRow.error).where(pm.AttemptReportRow.attempt_id == attempt_id)
    ).scalar_one_or_none()
    return auto_retry_count(error)


def _skip_reason(
    session: Session,
    attempt_id: str,
    *,
    skip_if_complete: bool,
    skip_failed_since: datetime | None,
) -> str | None:
    """Why this finalize is redundant right now, or None to run it.

    Evaluated INSIDE the per-attempt lock, on a session opened after the lock was
    taken, through a plain column SELECT (`get_report_marker`): it sees whatever a
    concurrent finalize committed while we waited. A `complete` report counts only
    when it is at least as new as the attempt's finish (`submitted_at`, or the
    deadline when expired) - a report left over from before a teacher "re-open" and
    a re-submit is stale, and the new finish must still be graded.
    """
    marker = AttemptRepository(session).get_report_marker(attempt_id)
    if marker is None or marker.report_status is None or marker.graded_at is None:
        return None
    if (
        skip_if_complete
        and marker.report_status == "complete"
        and (marker.finished_at is None or marker.graded_at >= marker.finished_at)
    ):
        return "complete report already exists"
    if (
        skip_failed_since is not None
        and marker.report_status == "failed"
        and marker.graded_at >= skip_failed_since
    ):
        return f"failed at {marker.graded_at.isoformat(timespec='seconds')} (within cooldown)"
    return None


def finalize_attempt(
    attempt_id: str,
    *,
    skip_if_complete: bool = True,
    skip_failed_since: datetime | None = None,
    max_auto_retries: int | None = None,
) -> bool:
    """Finalize one attempt in its own transaction (FastAPI background task).

    Returns True when the finalize ran (complete OR recorded as failed), False when
    it was skipped as already done. The whole run holds the per-attempt lock, and
    the "already done?" check happens AFTER taking it, on the fresh session:

    * `skip_if_complete` (default - the submit/expiry background task, the sweep and
      "grade all missing"): a `complete` report for this finish already exists, so
      another finalize got there first; never pay the LLM twice.
    * `skip_failed_since` (the sweep): also skip a `failed` report written at/after
      this cutoff (the 10-min retry cooldown).
    * `max_auto_retries` (the sweep): a `failed` report is retried automatically at
      most this many times. Each automatic retry that fails again records its
      ordinal in the report's `error` (` [auto-retry n]`); once n reaches the cap
      the attempt is skipped (INFO) until a teacher regrades it or presses "grade
      all missing" (both pass None: no cap, and a failure there resets the count).

    Every run through here reuses a stored LLM verdict whose input fingerprint is
    unchanged (`ResultsService(reuse_verdict=True)`), so a retry caused by a failed
    FEEDBACK call does not pay for the integrity analysis again.

    The teacher's explicit single-attempt regrade does not come through here - it
    calls `ResultsService.finalize` directly and always re-runs (and recomputes
    the verdict).

    Never raises: a background task must never crash the worker. A failure is
    logged AND persisted - the failed transaction is rolled back by `session_scope`,
    so a second, fresh session writes a `failed` report with the error (and the
    deterministic grade), still under the lock, so no waiter can slip in between.
    """
    with attempt_lock(attempt_id):
        auto_retry = 0  # n >= 1: this run is the n-th automatic retry of a failure
        try:
            with session_scope() as session:
                if skip_if_complete or skip_failed_since is not None:
                    reason = _skip_reason(
                        session,
                        attempt_id,
                        skip_if_complete=skip_if_complete,
                        skip_failed_since=skip_failed_since,
                    )
                    if reason is not None:
                        log.info("finalize attempt=%s skipped: %s", attempt_id, reason)
                        return False
                if max_auto_retries is not None:
                    spent = _auto_retries_spent(session, attempt_id)
                    if spent is not None and spent >= max_auto_retries:
                        log.info(
                            "finalize attempt=%s skipped: automatic retries exhausted "
                            "(%d/%d); waiting for a teacher regrade",
                            attempt_id,
                            spent,
                            max_auto_retries,
                        )
                        return False
                    if spent is not None:
                        auto_retry = spent + 1
                        log.info(
                            "finalize attempt=%s automatic retry %d/%d of a failed report",
                            attempt_id,
                            auto_retry,
                            max_auto_retries,
                        )
                _service(session, auto_retry=auto_retry).finalize(attempt_id)
            log.info("background finalize attempt=%s done", attempt_id)
            return True
        except Exception as exc:
            _log_failure("background finalize attempt=%s failed", attempt_id, exc=exc)
            failure = exc
        try:
            with session_scope() as session:
                _service(session, auto_retry=auto_retry).record_failure(attempt_id, failure)
        except Exception as exc:  # pragma: no cover - e.g. the DB itself is down
            _log_failure(
                "background finalize attempt=%s could not record failure", attempt_id, exc=exc
            )
        return True


def expire_overdue_attempts(test_id: str | None = None) -> list[str]:
    """Expire abandoned attempts (`in_progress` past their deadline) — short txn.

    A student who closes the tab never polls `/state` again, so the lazy expiry
    never fires and the attempt would stay `in_progress` (dashboard "pending",
    never finalized) forever. This runs the delivery engine's own transition
    (`DeliveryService.expire_overdue`, constructed exactly as the delivery API
    does) in its own short session that commits BEFORE any finalize starts, so the
    newly expired attempts become ordinary `finalize_missing` candidates.

    Results orchestrates delivery here (results -> delivery, never the reverse:
    `DeliveryService` still never grades or reports). Never raises — a failed
    expiry must not stop the recovery of already-finished attempts.
    """
    try:
        with session_scope() as session:
            delivery = DeliveryService(ContentRepository(session), AttemptRepository(session))
            ids = delivery.expire_overdue(datetime.now(UTC), test_id=test_id)
    except Exception as exc:
        _log_failure("expire overdue attempts test=%s failed", test_id or "*", exc=exc)
        return []
    log.info("expire overdue attempts test=%s -> %d expired", test_id or "*", len(ids))
    return ids


def finalize_missing(
    test_id: str | None = None,
    *,
    failed_before: datetime | None = None,
    expire_overdue: bool = False,
    min_age: timedelta | None = None,
    max_auto_retries: int | None = None,
) -> list[str]:
    """Finalize every finished attempt with no report or a `failed` one.

    One transaction per attempt: the candidates are selected in a short read
    session, then each id goes through the one-shot `finalize_attempt` (own
    session, commit, failure recorded as a `failed` report). A write lock is thus
    never held across another attempt's LLM calls, so students' answer/telemetry
    writes keep flowing during a long recovery. Returns the ids actually
    finalized, in order.

    No duplicate paid run: an attempt whose finalize is in flight (lock held) is
    skipped up front, and - because the candidate list can go stale (a background
    finalize finishing meanwhile, or taking the lock right after `is_locked` said
    free) - `finalize_attempt` re-checks the report INSIDE the lock: a `complete`
    report is always skipped; a `failed` one younger than `failed_before` too (the
    sweep's cooldown; the teacher's button passes None and retries every failure).

    `expire_overdue=True` (the sweep and the teacher's button) first expires
    abandoned past-deadline attempts in their own committed transaction
    (`expire_overdue_attempts`), so they are picked up in the same run.
    `min_age` (the sweep only) leaves alone an attempt with no report that finished
    less than `min_age` ago: the request that submitted/expired it scheduled a
    background finalize that owns it. Attempts this run expired itself have no such
    task and are exempt.
    `max_auto_retries` (the sweep only) caps how often a `failed` report is retried
    without a human (see `finalize_attempt`); None = no cap (the teacher's button).
    """
    expired_here: list[str] = []
    if expire_overdue:
        expired_here = expire_overdue_attempts(test_id)
    finished_before = datetime.now(UTC) - min_age if min_age is not None else None
    with session_scope() as session:
        ids = _service(session).list_missing(
            test_id,
            failed_before=failed_before,
            finished_before=finished_before,
            exempt=frozenset(expired_here),
        )
    log.info("finalize_missing test=%s -> %d candidate(s)", test_id or "*", len(ids))
    processed: list[str] = []
    for attempt_id in ids:
        # Fast path: a submit / regrade may have started this attempt's finalize
        # since the candidate list was read - don't even queue behind it.
        if is_locked(attempt_id):
            log.info("finalize_missing attempt=%s skipped: finalize in flight", attempt_id)
            continue
        log.info("finalize_missing attempt=%s (re)finalizing", attempt_id)
        if finalize_attempt(
            attempt_id,
            skip_if_complete=True,
            skip_failed_since=failed_before,
            max_auto_retries=max_auto_retries,
        ):
            processed.append(attempt_id)
    log.info("finalize_missing test=%s done -> %d attempt(s)", test_id or "*", len(processed))
    return processed


# The automatic sweep leaves a `failed` report alone this long after it was
# written, so a restart loop doesn't re-run paid LLM calls for an attempt that
# just failed (the teacher's explicit button has no such cutoff).
FAILED_RETRY_AFTER = timedelta(minutes=10)
# The sweep leaves an attempt that finished (submit / lazy expiry) less than this
# long ago to the background finalize its request scheduled.
SWEEP_MIN_AGE = timedelta(seconds=60)


def sweep_missing_reports() -> list[str]:
    """Recover lost finalizes across all tests (startup + periodic API recovery loop).

    A uvicorn restart right after a submit loses the in-flight background task;
    this first expires abandoned past-deadline attempts, then picks up (a) every
    finished attempt with NO report and (b) `failed` reports older than
    `FAILED_RETRY_AFTER` (younger ones are skipped, logged at INFO by
    `list_missing`, and re-checked inside the lock). In-flight finalizes (lock held)
    and attempts that finished less than `SWEEP_MIN_AGE` ago (their background
    finalize owns them) are skipped. A failed report is retried at most
    `MAX_AUTO_RETRIES` (3) times by the sweep — each retry is a paid grader +
    feedback call — and a stored LLM verdict with unchanged inputs is reused rather
    than recomputed. Never raises.
    """
    try:
        ids = finalize_missing(
            None,
            failed_before=datetime.now(UTC) - FAILED_RETRY_AFTER,
            expire_overdue=True,
            min_age=SWEEP_MIN_AGE,
            max_auto_retries=MAX_AUTO_RETRIES,
        )
        log.info("sweep missing reports -> %d attempt(s)", len(ids))
        return ids
    except Exception as exc:  # pragma: no cover - never take the app down on a sweep
        _log_failure("sweep missing reports failed", exc=exc)
        return []
