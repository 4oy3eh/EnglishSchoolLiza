"""Results hardening: no leaked credentials, no made-up verdict, no repeated paid work.

* **Secrets.** Whatever an LLM client raises, the string results logs and stores in
  `AttemptReport.error` carries no API key (`describe_error` -> `redact_secrets`),
  including the traceback of a crashed background finalize.
* **Crash path.** When the analysis layer itself blows up no verdict is stored
  (`None`), instead of a "suspicion 0.0 / confidence 0.0" row that would rank as a
  scored, clean attempt.
* **Cost.** An automatic retry reuses the stored LLM verdict while the attempt's
  events and analysis context are unchanged (`input_fingerprint`); the sweep gives
  up on a failed report after `MAX_AUTO_RETRIES`; the teacher's explicit regrade
  always recomputes.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.orm import Session

from app.analysis import AnalysisContext, AnalysisService, VerdictDraft
from app.core.config import settings
from app.feedback import FeedbackService, MockFeedbackAnalyst
from app.grading.llm import MockLLMGrader
from app.grading.service import GradingService
from app.persistence.repository import AttemptRepository, ContentRepository, EventRepository
from app.results import ResultsService, sweep_missing_reports
from app.results.service import (
    ERROR_MAX_CHARS,
    MAX_AUTO_RETRIES,
    auto_retry_count,
    describe_error,
    with_auto_retry,
)
from contracts import Answer, IntegrityEvent, WritingAssessment
from tests.test_results import T0, _age_report, _bg_on, _seed

KEY = "sk-test-SECRET-0123456789abcdef"


def _leaky_status_error(code: int = 429) -> httpx.HTTPStatusError:
    """What httpx raises for a request whose URL carries `?key=...`."""
    req = httpx.Request(
        "POST",
        f"https://generativelanguage.googleapis.com/v1beta/models/m:generateContent?key={KEY}",
    )
    try:
        httpx.Response(code, request=req).raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("unreachable")


class _FailingFeedback:
    """A feedback LLM that fails `fail_times` times (None = always), then works."""

    def __init__(self, exc: BaseException, fail_times: int | None = None) -> None:
        self.exc = exc
        self.fail_times = fail_times
        self.calls = 0
        self._ok = MockFeedbackAnalyst()

    def analyze(self, breakdowns):  # noqa: ANN001
        self.calls += 1
        if self.fail_times is None or self.calls <= self.fail_times:
            raise self.exc
        return self._ok.analyze(breakdowns)


class _CountingAnalyst:
    """An "LLM" integrity analyst: counts calls, returns a fixed advisory draft."""

    model_id = "counting-analyst"

    def __init__(self) -> None:
        self.calls = 0
        self.contexts: list[AnalysisContext | None] = []

    def analyze(self, profile, segments, context=None):  # noqa: ANN001
        self.calls += 1
        self.contexts.append(context)
        return VerdictDraft(
            suspicion_score=0.4,
            confidence=0.7,
            flags=("large_paste_attempt",),
            summary=f"Вызов {self.calls}.",
            model_id=self.model_id,
            writing=(WritingAssessment(item_id="w1", ai_assist_likelihood=0.5),),
        )


def _service(session: Session, *, feedback_llm=None, **kw) -> ResultsService:  # noqa: ANN001
    content = ContentRepository(session)
    attempts = AttemptRepository(session)
    return ResultsService(
        content=content,
        attempts=attempts,
        grading=GradingService(content, attempts, llm_grader=MockLLMGrader()),
        feedback=FeedbackService(llm=feedback_llm or MockFeedbackAnalyst()),
        **kw,
    )


def _add_event(session: Session, secs: float, kind: str = "paste_attempt") -> None:
    ts = T0 + timedelta(seconds=secs)
    EventRepository(session).add_event(
        IntegrityEvent(
            attempt_id="att1",
            item_id="w1",
            type=kind,  # type: ignore[arg-type]
            client_ts=ts,
            server_ts=ts,
            payload={"kind": "paste", "length": 500},
        )
    )
    session.commit()


# --------------------------------------------------------------------------- #
# Secrets never reach the report or the logs.
# --------------------------------------------------------------------------- #
def test_describe_error_scrubs_key_param_and_configured_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exc = _leaky_status_error()
    assert KEY in str(exc)
    text = describe_error(exc)
    assert text.startswith("HTTPStatusError: ")
    assert KEY not in text and "key=[REDACTED]" in text
    # The literal configured key is scrubbed wherever it appears in a message.
    monkeypatch.setattr(settings, "gemini_api_key", KEY)
    assert describe_error(RuntimeError(f"bad credential {KEY}!")) == (
        "RuntimeError: bad credential [REDACTED]!"
    )
    assert describe_error(ValueError("x")) == "ValueError: x"  # ordinary text untouched
    assert len(describe_error(ValueError(f"{KEY} " * 400))) <= ERROR_MAX_CHARS


def test_failed_feedback_error_is_stored_and_logged_without_the_key(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    attempt_id = _seed(session)
    with caplog.at_level(logging.DEBUG):
        report = _service(session, feedback_llm=_FailingFeedback(_leaky_status_error())).finalize(
            attempt_id
        )
    assert report.status == "failed"
    assert report.error is not None and report.error.startswith("HTTPStatusError")
    assert KEY not in report.error and "key=[REDACTED]" in report.error
    stored = AttemptRepository(session).get_report(attempt_id)
    assert KEY not in stored.model_dump_json()
    assert KEY not in caplog.text
    assert KEY not in "\n".join(r.getMessage() for r in caplog.records)


def test_crashed_background_finalize_logs_a_scrubbed_traceback(
    session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    attempt_id = _seed(session)
    bg = _bg_on(session, monkeypatch)
    monkeypatch.setattr(settings, "gemini_api_key", KEY)

    def explode(self, aid):  # noqa: ANN001
        raise RuntimeError(f"POST https://x/v1?key={KEY} failed; header x-goog-api-key: {KEY}")

    monkeypatch.setattr(ResultsService, "finalize", explode)
    with caplog.at_level(logging.DEBUG):
        assert bg.finalize_attempt(attempt_id) is True
    stored = AttemptRepository(session).get_report(attempt_id)
    assert stored.status == "failed"
    assert KEY not in stored.error and "[REDACTED]" in stored.error
    assert KEY not in caplog.text  # message, args and traceback
    failure = next(r for r in caplog.records if "background finalize" in r.getMessage())
    assert failure.levelno == logging.ERROR
    assert "Traceback" in failure.getMessage() and "RuntimeError" in failure.getMessage()
    assert failure.exc_info is None  # no raw traceback attached to the record


# --------------------------------------------------------------------------- #
# Crash path: no made-up verdict.
# --------------------------------------------------------------------------- #
def test_analysis_crash_stores_no_verdict_but_keeps_the_grade(
    session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def explode(self, attempt_id, *, context=None):  # noqa: ANN001
        raise ValueError("bad stored event")

    monkeypatch.setattr(AnalysisService, "assess", explode)
    attempt_id = _seed(session)
    with caplog.at_level(logging.ERROR, logger="app.results.service"):
        report = _service(session).finalize(attempt_id)
    assert report.status == "complete" and report.error is None
    assert report.grading.score > 0
    assert report.verdict is None and report.integrity is None  # NOT a 0.0 / 0.0 verdict
    attempts = AttemptRepository(session)
    assert attempts.get_verdict(attempt_id) is None
    assert attempts.get_report(attempt_id).verdict is None
    assert _service(session).get_verdict(attempt_id) is None
    assert any(
        "analysis failed (ValueError: bad stored event)" in r.getMessage()
        for r in caplog.records
    )
    # record_failure (the crash-of-the-crash path) behaves the same way.
    failed = _service(session).record_failure(attempt_id, RuntimeError("grader crashed"))
    assert failed.status == "failed" and failed.verdict is None


# --------------------------------------------------------------------------- #
# Cost: the LLM verdict is reused while its inputs are unchanged.
# --------------------------------------------------------------------------- #
def test_automatic_path_reuses_the_stored_llm_verdict_until_inputs_change(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    attempt_id = _seed(session)
    _add_event(session, 2)
    analyst = _CountingAnalyst()

    def auto() -> ResultsService:  # what finalize_attempt builds (sweep / background)
        return _service(session, analysis_llm=analyst, reuse_verdict=True)

    first = auto().finalize(attempt_id)
    assert analyst.calls == 1
    fingerprint = first.verdict.input_fingerprint
    assert fingerprint is not None and fingerprint.startswith("v1:1:")
    assert first.verdict.model_id == "counting-analyst"

    with caplog.at_level(logging.INFO, logger="app.results.service"):
        second = auto().finalize(attempt_id)
    assert analyst.calls == 1  # no second paid analysis
    assert second.verdict == first.verdict and second.integrity == first.integrity
    assert any("reusing stored verdict" in r.getMessage() for r in caplog.records)

    # A new event changes the count + last id -> recomputed.
    _add_event(session, 30)
    third = auto().finalize(attempt_id)
    assert analyst.calls == 2
    assert third.verdict.input_fingerprint != fingerprint
    assert third.integrity.writing[0].paste_attempts == 2
    assert auto().finalize(attempt_id).verdict == third.verdict
    assert analyst.calls == 2

    # A changed submitted text (re-open + re-submit) changes the context digest.
    AttemptRepository(session).save_answer(
        Answer(attempt_id=attempt_id, item_id="w1", response="a new text", answered_at=T0)
    )
    session.commit()
    auto().finalize(attempt_id)
    assert analyst.calls == 3
    assert analyst.contexts[-1].writing[0].text == "a new text"


def test_explicit_regrade_always_recomputes_the_verdict(session: Session) -> None:
    attempt_id = _seed(session)
    _add_event(session, 2)
    analyst = _CountingAnalyst()
    _service(session, analysis_llm=analyst, reuse_verdict=True).finalize(attempt_id)
    assert analyst.calls == 1
    # The teacher's regrade service is built without `reuse_verdict` (the default).
    regrade = _service(session, analysis_llm=analyst)
    assert regrade.reuse_verdict is False
    report = regrade.finalize(attempt_id)
    assert analyst.calls == 2 and report.verdict.summary == "Вызов 2."
    regrade.finalize(attempt_id)
    assert analyst.calls == 3


def test_failed_or_deterministic_verdicts_are_never_reused(session: Session) -> None:
    class _Boom:
        def analyze(self, profile, segments, context=None):  # noqa: ANN001
            raise RuntimeError("gemini 503")

    attempt_id = _seed(session)
    _add_event(session, 2)
    failed = _service(session, analysis_llm=_Boom(), reuse_verdict=True).finalize(attempt_id)
    assert failed.verdict.model_id is None and failed.verdict.error == "RuntimeError: gemini 503"
    assert failed.verdict.input_fingerprint is not None
    # Same inputs, but the stored verdict is the deterministic fallback: call the LLM.
    analyst = _CountingAnalyst()
    report = _service(session, analysis_llm=analyst, reuse_verdict=True).finalize(attempt_id)
    assert analyst.calls == 1 and report.verdict.model_id == "counting-analyst"


def test_sweep_retry_of_failed_feedback_does_not_pay_for_analysis_again(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt_id = _seed(session)
    _add_event(session, 2)
    bg = _bg_on(session, monkeypatch)
    analyst = _CountingAnalyst()
    feedback = _FailingFeedback(RuntimeError("gemini feedback 503"), fail_times=2)
    monkeypatch.setattr(bg, "_feedback_analyst", lambda: feedback)
    monkeypatch.setattr("app.results.analyst.default_analysis_llm", lambda: analyst)
    attempts = AttemptRepository(session)

    assert bg.finalize_attempt(attempt_id) is True  # the submit-time background task
    assert attempts.get_report(attempt_id).status == "failed"
    assert (feedback.calls, analyst.calls) == (1, 1)
    verdict = attempts.get_verdict(attempt_id)

    old = datetime.now(UTC) - timedelta(minutes=11)
    _age_report(session, attempt_id, old)
    assert sweep_missing_reports() == [attempt_id]  # retry 1: feedback fails again
    assert (feedback.calls, analyst.calls) == (2, 1)  # ...but the verdict was reused
    error = attempts.get_report(attempt_id).error
    assert error == "RuntimeError: gemini feedback 503 [auto-retry 1]"

    _age_report(session, attempt_id, old)
    assert sweep_missing_reports() == [attempt_id]  # retry 2: feedback recovers
    assert (feedback.calls, analyst.calls) == (3, 1)
    report = attempts.get_report(attempt_id)
    assert report.status == "complete" and report.error is None
    assert report.verdict == verdict  # the very verdict paid for once


# --------------------------------------------------------------------------- #
# Cost: the sweep stops retrying a failed report after MAX_AUTO_RETRIES.
# --------------------------------------------------------------------------- #
def test_auto_retry_marker_helpers() -> None:
    assert MAX_AUTO_RETRIES == 3
    assert auto_retry_count(None) == 0
    assert auto_retry_count("RuntimeError: boom") == 0
    assert with_auto_retry("RuntimeError: boom", 0) == "RuntimeError: boom"
    one = with_auto_retry("RuntimeError: boom", 1)
    assert one == "RuntimeError: boom [auto-retry 1]" and auto_retry_count(one) == 1
    # Re-marking replaces the old marker; a marker in the middle is not one.
    assert with_auto_retry(one, 2) == "RuntimeError: boom [auto-retry 2]"
    assert auto_retry_count("x [auto-retry 9] y") == 0
    long = with_auto_retry("E: " + "y" * 2000, 3)
    assert len(long) == ERROR_MAX_CHARS and long.endswith("… [auto-retry 3]")
    assert auto_retry_count(long) == 3


def test_sweep_caps_automatic_retries_of_a_failed_report(
    session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    attempt_id = _seed(session)
    bg = _bg_on(session, monkeypatch)
    feedback = _FailingFeedback(RuntimeError("quota exceeded"))  # never recovers
    monkeypatch.setattr(bg, "_feedback_analyst", lambda: feedback)
    attempts = AttemptRepository(session)
    old = datetime.now(UTC) - timedelta(minutes=11)

    assert bg.finalize_attempt(attempt_id) is True  # the original (submit-time) run
    assert attempts.get_report(attempt_id).error == "RuntimeError: quota exceeded"
    assert feedback.calls == 1

    for n in range(1, MAX_AUTO_RETRIES + 1):  # automatic retries 1..3
        _age_report(session, attempt_id, old)
        with caplog.at_level(logging.INFO):
            assert sweep_missing_reports() == [attempt_id]
        report = attempts.get_report(attempt_id)
        assert report.status == "failed"
        assert report.error == f"RuntimeError: quota exceeded [auto-retry {n}]"
        assert feedback.calls == 1 + n
    assert any(
        "no further automatic retries" in r.getMessage() and r.levelno == logging.WARNING
        for r in caplog.records
    )

    # Budget spent: every later sweep leaves it alone (no LLM call), however old.
    caplog.clear()
    for _ in range(3):
        _age_report(session, attempt_id, old)
        with caplog.at_level(logging.INFO):
            assert sweep_missing_reports() == []
    assert feedback.calls == 1 + MAX_AUTO_RETRIES
    assert any(
        "automatic retries exhausted (3/3)" in r.getMessage() and r.levelno == logging.INFO
        for r in caplog.records
    )
    assert attempts.get_report(attempt_id).error.endswith("[auto-retry 3]")

    # The teacher's "grade all missing" has no cap, and a failure there resets it.
    assert bg.finalize_missing("t1") == [attempt_id]
    assert feedback.calls == 2 + MAX_AUTO_RETRIES
    assert attempts.get_report(attempt_id).error == "RuntimeError: quota exceeded"
    # ...and so does the explicit single regrade, which then succeeds.
    report = _service(session).finalize(attempt_id)
    assert report.status == "complete" and report.error is None


def test_retry_budget_also_counts_crashed_finalizes_and_resets_on_a_new_finish(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt_id = _seed(session)
    bg = _bg_on(session, monkeypatch)
    attempts = AttemptRepository(session)
    real = ResultsService.finalize

    def explode(self, aid):  # noqa: ANN001
        raise RuntimeError("grader crashed")

    monkeypatch.setattr(ResultsService, "finalize", explode)
    old = datetime.now(UTC) - timedelta(minutes=11)
    bg.finalize_attempt(attempt_id)
    for n in range(1, MAX_AUTO_RETRIES + 1):  # record_failure carries the marker too
        _age_report(session, attempt_id, old)
        assert sweep_missing_reports() == [attempt_id]
        error = attempts.get_report(attempt_id).error
        assert error == f"RuntimeError: grader crashed [auto-retry {n}]"
    _age_report(session, attempt_id, old)
    assert sweep_missing_reports() == []

    # Re-open + re-submit: the exhausted report is older than the new finish, so
    # the sweep treats it as a fresh failure budget (the new finish must be graded).
    attempt = attempts.get_attempt(attempt_id)
    attempts.update_attempt(
        attempt.model_copy(update={"submitted_at": datetime.now(UTC) - timedelta(minutes=5)})
    )
    session.commit()
    monkeypatch.setattr(ResultsService, "finalize", real)
    assert sweep_missing_reports() == [attempt_id]
    assert attempts.get_report(attempt_id).status == "complete"
