"""Results engine: finalize an attempt once (grade + skill report) and persist it.

Gate: finalize grades the attempt (objective + the injected writing grader), builds
the per-skill report, and persists ONE frozen `AttemptReport`; re-finalize overwrites
(idempotent); a feedback LLM that raises still saves the score with status="failed";
the report round-trips through the repository.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
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


# --------------------------------------------------------------------------- #
# Failure is persisted, never silent.
# --------------------------------------------------------------------------- #
class _Boom:
    def analyze(self, breakdowns):  # noqa: ANN001
        raise RuntimeError("gemini down")


def test_failed_feedback_records_error(session: Session) -> None:
    attempt_id = _seed(session)
    report = _service(session, feedback_llm=_Boom()).finalize(attempt_id)
    assert report.status == "failed"
    assert report.error == "RuntimeError: gemini down"
    stored = AttemptRepository(session).get_report(attempt_id)
    assert stored.error == "RuntimeError: gemini down"
    # A successful re-run clears it.
    assert _service(session).finalize(attempt_id).error is None
    assert AttemptRepository(session).get_report(attempt_id).error is None


def test_describe_error_truncates() -> None:
    from app.results.service import ERROR_MAX_CHARS, describe_error

    assert describe_error(ValueError("x")) == "ValueError: x"
    long = describe_error(ValueError("y" * 2000))
    assert len(long) == ERROR_MAX_CHARS and long.startswith("ValueError: yyy")


def test_record_failure_persists_failed_report_with_deterministic_grade(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    attempt_id = _seed(session)
    with caplog.at_level(logging.WARNING):
        report = _service(session).record_failure(attempt_id, RuntimeError("grader crashed"))
    assert report.status == "failed" and report.error == "RuntimeError: grader crashed"
    # Objective items were still graded deterministically; writing went to review.
    assert report.grading.score == 1.0  # the gap "cat" is right
    w1 = next(g for g in report.grading.items if g.item_id == "w1")
    assert w1.needs_review and w1.awarded == 0.0  # no LLM: writing routed to a human
    assert report.skills == [] and report.model_id is None
    stored = AttemptRepository(session).get_report(attempt_id)
    assert stored.status == "failed" and stored.error == report.error
    assert any("recorded as failed" in r.message for r in caplog.records)


def test_record_failure_writes_zero_grade_when_grading_also_fails(session: Session) -> None:
    """Even when the deterministic grade itself fails, a zero report is written."""
    attempt_id = _seed(session)
    svc = _service(session)
    svc.content = _BrokenContent()  # the deterministic grade cannot load the test
    report = svc.record_failure(attempt_id, RuntimeError("boom"))
    assert report.status == "failed" and report.error == "RuntimeError: boom"
    assert report.grading.score == 0.0 and report.grading.max_score == 0.0
    assert report.grading.needs_review is True
    assert AttemptRepository(session).get_report(attempt_id).status == "failed"


class _BrokenContent:
    def get_test(self, test_id):  # noqa: ANN001
        raise RuntimeError("db gone")


# --------------------------------------------------------------------------- #
# Recovery: finalize_missing / sweep_missing_reports / the background entrypoint.
# --------------------------------------------------------------------------- #
def _seed_second_attempt(session: Session, attempt_id: str, *, status: str = "submitted") -> None:
    attempts = AttemptRepository(session)
    attempts.add_roster_entry(RosterEntry(id=f"e-{attempt_id}", test_id="t1", display_name="X"))
    attempts.add_attempt(
        Attempt(
            id=attempt_id,
            test_id="t1",
            roster_entry_id=f"e-{attempt_id}",
            status=status,  # type: ignore[arg-type]
            seed=3,
            started_at=T0,
            submitted_at=T0 + timedelta(minutes=9) if status == "submitted" else None,
            deadline=T0 + timedelta(hours=1),
        )
    )
    attempts.save_answer(
        Answer(attempt_id=attempt_id, item_id="r1", response="dog", answered_at=T0)
    )
    session.commit()


@contextmanager
def _fake_scope(session: Session, trace: list[str] | None = None):
    """Stand-in for `session_scope`: same test session, commit, never close.

    `trace` (optional) records "open"/"close" so a test can assert transactions are
    sequential (one per attempt), never nested around another attempt's work.
    """
    if trace is not None:
        trace.append("open")
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        if trace is not None:
            trace.append("close")


def _bg_on(session: Session, monkeypatch: pytest.MonkeyPatch, trace: list[str] | None = None):
    """Point the background finalize module at the test session + mock LLMs."""
    from app.results import finalize as bg

    monkeypatch.setattr(bg, "session_scope", lambda: _fake_scope(session, trace))
    monkeypatch.setattr(bg, "_writing_grader", lambda: MockLLMGrader())
    monkeypatch.setattr(bg, "_feedback_analyst", lambda: MockFeedbackAnalyst())
    return bg


@contextmanager
def _held_elsewhere(attempt_id: str):
    """Hold the attempt's finalize lock on ANOTHER thread (a finalize in flight)."""
    from app.results.locks import lock_for

    acquired, release = threading.Event(), threading.Event()

    def hold() -> None:
        with lock_for(attempt_id):
            acquired.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    assert acquired.wait(5)
    try:
        yield
    finally:
        release.set()
        t.join(5)


def test_finalize_missing_picks_no_report_and_failed_only(
    session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    att1 = _seed(session)  # no report
    _seed_second_attempt(session, "att-failed")
    _seed_second_attempt(session, "att-done")
    _seed_second_attempt(session, "att-expired", status="expired")
    _seed_second_attempt(session, "att-working", status="in_progress")
    svc = _service(session)
    svc.record_failure("att-failed", RuntimeError("earlier"))
    svc.finalize("att-done")
    session.commit()
    bg = _bg_on(session, monkeypatch)

    with caplog.at_level(logging.INFO):
        processed = bg.finalize_missing(None)
    assert set(processed) == {att1, "att-failed", "att-expired"}
    assert "att-working" not in processed and "att-done" not in processed
    attempts = AttemptRepository(session)
    for aid in processed:
        rep = attempts.get_report(aid)
        assert rep is not None and rep.status == "complete" and rep.error is None
    assert attempts.get_report("att-working") is None
    assert sum("finalize_missing attempt=" in r.message for r in caplog.records) == 3
    # Scoped to a test id (none match a bogus id).
    assert bg.finalize_missing("other-test") == []


def test_finalize_missing_records_failure_and_continues(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure on the first attempt doesn't stop the second (own txn each)."""
    att1 = _seed(session)
    _seed_second_attempt(session, "att-b")
    bg = _bg_on(session, monkeypatch)
    real = ResultsService.finalize

    def flaky(self, attempt_id):  # noqa: ANN001
        if attempt_id == att1:
            raise RuntimeError("grader exploded")
        return real(self, attempt_id)

    monkeypatch.setattr(ResultsService, "finalize", flaky)
    processed = bg.finalize_missing(None)
    assert processed == [att1, "att-b"]  # att1 first (earlier submit), then att-b
    attempts = AttemptRepository(session)
    assert attempts.get_report(att1).status == "failed"
    assert attempts.get_report(att1).error == "RuntimeError: grader exploded"
    assert attempts.get_report("att-b").status == "complete"


def test_finalize_missing_one_transaction_per_attempt(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Candidates are read in a short txn that closes BEFORE any finalize; each
    attempt then gets its own sequential txn (never nested, never shared)."""
    att1 = _seed(session)
    _seed_second_attempt(session, "att-b")
    _seed_second_attempt(session, "att-c")
    trace: list[str] = []
    bg = _bg_on(session, monkeypatch, trace)
    seen: list[str] = []
    real = ResultsService.finalize

    def spy(self, attempt_id):  # noqa: ANN001
        seen.append(f"finalize:{attempt_id}:depth={trace.count('open') - trace.count('close')}")
        return real(self, attempt_id)

    monkeypatch.setattr(ResultsService, "finalize", spy)
    processed = bg.finalize_missing(None)
    assert processed == [att1, "att-b", "att-c"]
    # 1 read txn + 1 txn per attempt, strictly open/close alternating.
    assert trace == ["open", "close"] * 4
    # Every finalize ran inside exactly one open transaction (its own).
    assert seen == [f"finalize:{a}:depth=1" for a in processed]


def test_finalize_missing_skips_in_flight(
    session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    att1 = _seed(session)
    _seed_second_attempt(session, "att-b")
    bg = _bg_on(session, monkeypatch)
    with _held_elsewhere(att1), caplog.at_level(logging.INFO):
        processed = bg.finalize_missing(None)
    assert processed == ["att-b"]  # no duplicate paid run for the in-flight one
    assert AttemptRepository(session).get_report(att1) is None
    assert any(
        att1 in r.message and "finalize in flight" in r.message for r in caplog.records
    )
    # Once the in-flight work ends, the next run picks it up.
    assert bg.finalize_missing(None) == [att1]


def test_background_finalize_persists_failure(
    session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    attempt_id = _seed(session)
    bg = _bg_on(session, monkeypatch)

    def explode(self, aid):  # noqa: ANN001
        raise RuntimeError("worker crashed mid-finalize")

    monkeypatch.setattr(ResultsService, "finalize", explode)
    with caplog.at_level(logging.ERROR):
        bg.finalize_attempt(attempt_id)  # must not raise
    stored = AttemptRepository(session).get_report(attempt_id)
    assert stored is not None and stored.status == "failed"
    assert stored.error == "RuntimeError: worker crashed mid-finalize"
    assert stored.grading.score == 1.0  # deterministic grade kept
    assert any("background finalize" in r.message and "failed" in r.message for r in caplog.records)

    # Happy path through the same entrypoint overwrites it with a complete report.
    monkeypatch.setattr(ResultsService, "finalize", ResultsService._finalize_locked)
    bg.finalize_attempt(attempt_id)
    assert AttemptRepository(session).get_report(attempt_id).status == "complete"


def test_sweep_missing_reports(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.results import sweep_missing_reports

    att1 = _seed(session)
    _seed_second_attempt(session, "att-b")
    _bg_on(session, monkeypatch)

    assert set(sweep_missing_reports()) == {att1, "att-b"}
    attempts = AttemptRepository(session)
    assert attempts.get_report(att1).status == "complete"
    assert attempts.get_report("att-b").status == "complete"
    assert sweep_missing_reports() == []  # nothing left


def _age_report(session: Session, attempt_id: str, graded_at: datetime) -> None:
    attempts = AttemptRepository(session)
    rep = attempts.get_report(attempt_id)
    attempts.upsert_report(rep.model_copy(update={"graded_at": graded_at}))
    session.commit()


def test_sweep_retries_failed_only_after_cooldown(
    session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Startup sweep: no-report attempts always; `failed` ones only once their
    report is older than FAILED_RETRY_AFTER (10 min); younger ones are skipped
    and logged at INFO. The teacher's explicit run has no cooldown."""
    from app.results import sweep_missing_reports

    lost = _seed(session)  # no report at all
    _seed_second_attempt(session, "att-fresh-fail")
    _seed_second_attempt(session, "att-old-fail")
    svc = _service(session)
    svc.record_failure("att-fresh-fail", RuntimeError("just now"))
    svc.record_failure("att-old-fail", RuntimeError("long ago"))
    now = datetime.now(UTC)
    _age_report(session, "att-fresh-fail", now - timedelta(minutes=2))
    _age_report(session, "att-old-fail", now - timedelta(minutes=11))
    bg = _bg_on(session, monkeypatch)
    assert timedelta(minutes=10) == bg.FAILED_RETRY_AFTER

    with caplog.at_level(logging.INFO):
        processed = sweep_missing_reports()
    assert set(processed) == {lost, "att-old-fail"}
    attempts = AttemptRepository(session)
    assert attempts.get_report(lost).status == "complete"
    assert attempts.get_report("att-old-fail").status == "complete"
    assert attempts.get_report("att-fresh-fail").status == "failed"  # left alone
    skipped = [
        r for r in caplog.records
        if "att-fresh-fail" in r.message and "skipped" in r.message
    ]
    assert skipped and all(r.levelno == logging.INFO for r in skipped)

    # The explicit (teacher) run retries every failed report regardless of age.
    assert bg.finalize_missing("t1") == ["att-fresh-fail"]
    assert attempts.get_report("att-fresh-fail").status == "complete"


# --------------------------------------------------------------------------- #
# Regrade vs background finalize: per-attempt lock serializes them.
# --------------------------------------------------------------------------- #
def test_attempt_lock_registry_is_per_attempt() -> None:
    from app.results.locks import attempt_lock, is_locked, lock_for

    assert lock_for("a") is lock_for("a")
    assert lock_for("a") is not lock_for("b")
    assert not is_locked("never-finalized")
    with attempt_lock("a"):
        assert is_locked("a")
        assert not is_locked("b")
    assert not is_locked("a")
    with _held_elsewhere("a"):  # held by another thread -> in flight
        assert is_locked("a")
    assert not is_locked("a")


def test_finalize_waits_for_concurrent_finalize(session: Session) -> None:
    from app.results.locks import lock_for

    attempt_id = _seed(session)
    svc = _service(session)
    finished = threading.Event()
    result: list = []

    def run() -> None:
        result.append(svc.finalize(attempt_id))
        finished.set()

    lock = lock_for(attempt_id)
    lock.acquire()  # simulate the submit-time background finalize still running
    try:
        t = threading.Thread(target=run)
        t.start()
        assert not finished.wait(0.3)  # the regrade is blocked, not interleaved
    finally:
        lock.release()
    assert finished.wait(5)
    t.join()
    assert result and result[0].status == "complete"


# --------------------------------------------------------------------------- #
# Abandoned attempts: in_progress past the deadline, never polled again.
# --------------------------------------------------------------------------- #
def test_sweep_expires_abandoned_attempt_and_reports_it(
    session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The student closed the tab: nothing ever expired the attempt lazily. The
    sweep expires it (own committed txn, BEFORE any finalize) and then finalizes it
    in its own txn, so it ends `expired` with a complete report."""
    from app.results import sweep_missing_reports

    _seed(session)
    _seed_second_attempt(session, "att-abandoned", status="in_progress")  # deadline T0+1h
    _seed_second_attempt(session, "att-future", status="in_progress")
    attempts = AttemptRepository(session)
    future = attempts.get_attempt("att-future")
    attempts.update_attempt(
        future.model_copy(update={"deadline": datetime.now(UTC) + timedelta(hours=1)})
    )
    session.commit()
    trace: list[str] = []
    _bg_on(session, monkeypatch, trace)
    # The abandoned attempt is invisible to a plain finalize_missing run.
    from app.results import finalize as bg

    assert "att-abandoned" not in bg.finalize_missing(None)
    trace.clear()

    with caplog.at_level(logging.INFO):
        processed = sweep_missing_reports()
    assert processed == ["att-abandoned"]
    abandoned = attempts.get_attempt("att-abandoned")
    assert abandoned.status == "expired"
    assert abandoned.deadline == T0 + timedelta(hours=1)  # never moved (rule #3)
    report = attempts.get_report("att-abandoned")
    assert report is not None and report.status == "complete"
    assert attempts.get_attempt("att-future").status == "in_progress"  # not due yet
    assert attempts.get_report("att-future") is None
    # expire txn, candidate read txn, then the one finalize txn — sequential.
    assert trace == ["open", "close"] * 3
    assert any(
        "attempt expire attempt=att-abandoned" in r.message and r.levelno == logging.INFO
        for r in caplog.records
    )
    # Idempotent: the next sweep finds nothing to expire and nothing to report.
    assert sweep_missing_reports() == []


def test_expire_overdue_attempts_never_raises(
    session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing expiry step must not block recovery of already-finished attempts."""
    from app.delivery import DeliveryService

    att1 = _seed(session)
    bg = _bg_on(session, monkeypatch)

    def boom(self, now=None, *, test_id=None):  # noqa: ANN001
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(DeliveryService, "expire_overdue", boom)
    with caplog.at_level(logging.ERROR):
        assert bg.expire_overdue_attempts() == []
        assert bg.finalize_missing(None, expire_overdue=True) == [att1]
    assert any("expire overdue attempts" in r.message for r in caplog.records)


def test_expire_overdue_attempts_scoped_to_test(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(session)
    _seed_second_attempt(session, "att-abandoned", status="in_progress")
    bg = _bg_on(session, monkeypatch)
    assert bg.expire_overdue_attempts("other-test") == []
    assert AttemptRepository(session).get_attempt("att-abandoned").status == "in_progress"
    assert bg.expire_overdue_attempts("t1") == ["att-abandoned"]
    assert bg.expire_overdue_attempts("t1") == []


# --------------------------------------------------------------------------- #
# No duplicate paid finalize: the "already done?" check runs INSIDE the lock.
# --------------------------------------------------------------------------- #
class _CountingFeedback(MockFeedbackAnalyst):
    """Mock feedback LLM that counts (paid) calls; can park mid-call on `gate`."""

    def __init__(self, gate: threading.Event | None = None) -> None:
        super().__init__()
        self.calls: list[int] = []
        self.entered = threading.Event()
        self.gate = gate

    def analyze(self, breakdowns):  # noqa: ANN001
        self.calls.append(1)
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(10), "test never released the in-flight finalize"
        return super().analyze(breakdowns)


def _count_llm(monkeypatch: pytest.MonkeyPatch, bg, gate=None) -> _CountingFeedback:  # noqa: ANN001
    counter = _CountingFeedback(gate)
    monkeypatch.setattr(bg, "_feedback_analyst", lambda: counter)
    return counter


def test_background_finalize_skips_when_complete_report_exists(
    session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The submit/expiry task is the FIRST finalize: a complete report means
    someone else (sweep / button) already did it - no second paid LLM run."""
    attempt_id = _seed(session)
    bg = _bg_on(session, monkeypatch)
    llm = _count_llm(monkeypatch, bg)
    assert bg.finalize_attempt(attempt_id) is True
    assert len(llm.calls) == 1
    with caplog.at_level(logging.INFO):
        assert bg.finalize_attempt(attempt_id) is False
    assert len(llm.calls) == 1
    assert any(
        f"finalize attempt={attempt_id} skipped: complete report" in r.message
        and r.levelno == logging.INFO
        for r in caplog.records
    )
    # skip_if_complete=False forces a run (no skip check at all).
    assert bg.finalize_attempt(attempt_id, skip_if_complete=False) is True
    assert len(llm.calls) == 2


def test_background_finalize_reruns_when_report_predates_the_finish(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A complete report older than the attempt's finish (teacher re-opened it and
    the student submitted again) is stale: the new submit is graded."""
    attempt_id = _seed(session)  # submitted at T0+5min
    bg = _bg_on(session, monkeypatch)
    llm = _count_llm(monkeypatch, bg)
    _service(session).finalize(attempt_id)
    session.commit()
    _age_report(session, attempt_id, T0)  # graded before the (re)submit
    assert bg.finalize_attempt(attempt_id) is True
    assert len(llm.calls) == 1
    assert AttemptRepository(session).get_report(attempt_id).graded_at > T0


def test_finalize_missing_skips_report_completed_after_candidate_read(
    session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Window (b): a background finalize completes between the candidate read and
    the loop reaching that id - the in-lock re-check skips it (sweep AND button)."""
    from sqlalchemy import text

    from app.results import sweep_missing_reports

    att1 = _seed(session)
    _seed_second_attempt(session, "att-b")
    bg = _bg_on(session, monkeypatch)
    llm = _count_llm(monkeypatch, bg)
    real_list = ResultsService.list_missing

    def list_then_bg_finishes(self, *args, **kwargs):  # noqa: ANN001
        ids = real_list(self, *args, **kwargs)
        _service(session).finalize("att-b")  # the background task lands meanwhile
        session.commit()
        return ids

    monkeypatch.setattr(ResultsService, "list_missing", list_then_bg_finishes)
    with caplog.at_level(logging.INFO):
        assert sweep_missing_reports() == [att1]
    assert len(llm.calls) == 1  # only att1 paid; att-b's own finalize used another mock
    assert any(
        "finalize attempt=att-b skipped: complete report" in r.message for r in caplog.records
    )

    # The teacher's "grade all missing" re-checks the same way.
    session.execute(text("DELETE FROM attempt_reports WHERE attempt_id = 'att-b'"))
    session.commit()
    assert bg.finalize_missing("t1") == []
    assert len(llm.calls) == 1


def test_sweep_skips_failure_written_after_candidate_read_but_button_retries(
    session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A finalize that FAILED right after the candidate read is inside the sweep's
    10-min cooldown (re-checked in the lock); the teacher's button retries it."""
    from app.results import sweep_missing_reports

    att1 = _seed(session)
    bg = _bg_on(session, monkeypatch)
    llm = _count_llm(monkeypatch, bg)
    real_list = ResultsService.list_missing

    def list_then_bg_fails(self, *args, **kwargs):  # noqa: ANN001
        ids = real_list(self, *args, **kwargs)
        _service(session).record_failure(att1, RuntimeError("gemini 503"))
        session.commit()
        return ids

    monkeypatch.setattr(ResultsService, "list_missing", list_then_bg_fails)
    with caplog.at_level(logging.INFO):
        assert sweep_missing_reports() == []
    assert llm.calls == []
    assert AttemptRepository(session).get_report(att1).status == "failed"
    assert any(
        f"finalize attempt={att1} skipped: failed at" in r.message for r in caplog.records
    )
    monkeypatch.setattr(ResultsService, "list_missing", real_list)
    assert bg.finalize_missing("t1") == [att1]  # button: no cooldown
    assert len(llm.calls) == 1
    assert AttemptRepository(session).get_report(att1).status == "complete"


def test_sweep_leaves_recent_finishes_to_their_background_task(
    session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Window (a): an attempt submitted / lazily expired < 60 s ago belongs to the
    background finalize its request scheduled - the sweep (only) leaves it alone.
    An attempt the sweep expired ITSELF has no such task and is taken at once."""
    from app.results import sweep_missing_reports

    _seed(session)  # test + an old submitted attempt (no report)
    _seed_second_attempt(session, "att-just-submitted")
    _seed_second_attempt(session, "att-just-expired", status="expired")
    _seed_second_attempt(session, "att-abandoned", status="in_progress")
    repo = AttemptRepository(session)
    now = datetime.now(UTC)
    for aid, update in (
        ("att-just-submitted", {"submitted_at": now - timedelta(seconds=10)}),
        ("att-just-expired", {"deadline": now - timedelta(seconds=10)}),
        ("att-abandoned", {"deadline": now - timedelta(seconds=5)}),
    ):
        repo.update_attempt(repo.get_attempt(aid).model_copy(update=update))
    session.commit()
    bg = _bg_on(session, monkeypatch)
    llm = _count_llm(monkeypatch, bg)
    assert timedelta(seconds=60) == bg.SWEEP_MIN_AGE

    with caplog.at_level(logging.INFO):
        assert set(sweep_missing_reports()) == {"att1", "att-abandoned"}
    assert len(llm.calls) == 2
    for aid in ("att-just-submitted", "att-just-expired"):
        assert repo.get_report(aid) is None
        assert any(
            f"list_missing attempt={aid} skipped: finished at" in r.message
            and r.levelno == logging.INFO
            for r in caplog.records
        )
    # The teacher's button has no age filter.
    assert set(bg.finalize_missing("t1")) == {"att-just-submitted", "att-just-expired"}


def test_sweep_waiting_on_in_flight_finalize_does_not_pay_twice(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrency: the background finalize holds the lock mid-LLM-call while the
    sweep runs. The sweep (its `is_locked` fast path defeated, as when the check
    raced the lock) blocks on the lock; once the background run commits and
    releases, the sweep re-reads the report inside the lock and skips - exactly
    ONE paid LLM call. Real per-thread sessions on a file DB."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.db import Base
    from app.results import finalize as bg
    from app.results import service as results_service

    engine = create_engine(
        f"sqlite:///{(tmp_path / 'race.db').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    @contextmanager
    def real_scope():
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    with factory() as s:
        attempt_id = _seed(s)
    gate = threading.Event()
    llm = _count_llm(monkeypatch, bg, gate)
    monkeypatch.setattr(bg, "session_scope", real_scope)
    monkeypatch.setattr(bg, "_writing_grader", lambda: MockLLMGrader())
    # Simulate the non-atomic `is_locked` check losing the race everywhere.
    monkeypatch.setattr(bg, "is_locked", lambda _aid: False)
    monkeypatch.setattr(results_service, "is_locked", lambda _aid: False)

    background_ran: list[bool] = []
    background = threading.Thread(
        target=lambda: background_ran.append(bg.finalize_attempt(attempt_id))
    )
    background.start()
    assert llm.entered.wait(5)  # background finalize is mid-LLM, holding the lock

    swept: list[list[str]] = []
    sweeper = threading.Thread(target=lambda: swept.append(bg.sweep_missing_reports()))
    sweeper.start()
    sweeper.join(0.5)
    assert sweeper.is_alive()  # blocked on the attempt lock, not running the LLM
    assert len(llm.calls) == 1

    gate.set()  # the background LLM call returns; report committed; lock released
    background.join(10)
    sweeper.join(10)
    assert not background.is_alive() and not sweeper.is_alive()
    assert background_ran == [True]
    assert swept == [[]]  # the sweep skipped it after re-checking inside the lock
    assert len(llm.calls) == 1  # no second paid run
    with factory() as s:
        assert AttemptRepository(s).get_report(attempt_id).status == "complete"
    engine.dispose()
