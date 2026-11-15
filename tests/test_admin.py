"""Phase 10: admin (teacher) API + dashboard backend.

Gate (docs/PROMPTS.md Prompt 10): auth is enforced; the approve flow flips
draft->published (the only publish path, golden rule #5); the results endpoint
returns score + advisory verdict + event count; results rank suspicious-first;
admin actions are logged. Grading and integrity stay independent (golden rule #2).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.admin import (
    AdminError,
    AdminService,
    AuthError,
    TokenSigner,
    build_admin_service,
)
from app.analysis import MockAnalysisLLM
from app.content import FilesystemStorage
from app.core.db import get_session
from app.persistence.repository import AttemptRepository, EventRepository
from apps.api import admin as admin_api
from apps.api.main import app
from contracts import (
    Answer,
    Attempt,
    IntegrityEvent,
    OpenWritingItem,
    PassageTextStimulus,
    RosterEntry,
    Section,
    SingleChoiceItem,
    TextOption,
)
from contracts import Test as ExamTest

PASSWORD = "test-teacher-pw"
T0 = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Builders.
# --------------------------------------------------------------------------- #
def _test(test_id: str, *, status: str = "draft") -> ExamTest:
    return ExamTest(
        id=test_id,
        title=f"A2 Key — {test_id}",
        level="A2_KEY",
        status=status,  # type: ignore[arg-type]
        duration_minutes=60,
        sections=[
            Section(
                id=f"{test_id}-sec",
                skill="reading",
                stimulus=PassageTextStimulus(text="Read."),
                items=[
                    SingleChoiceItem(
                        id=f"{test_id}-q1",
                        prompt="?",
                        options=[
                            TextOption(key="A", text="a"),
                            TextOption(key="B", text="b"),
                        ],
                        correct="A",
                    )
                ],
            )
        ],
    )


def _seed_attempt(
    session: Session,
    *,
    test_id: str,
    name: str,
    answer: str,
    hidden_secs: float = 0.0,
    status: str = "submitted",
    submitted_at: datetime | None = T0 + timedelta(minutes=5),
) -> str:
    """Create a roster entry + submitted attempt with one answer and some events.

    `hidden_secs` injects a bounded hidden interval so the mock analyst produces a
    non-zero suspicion (used to prove suspicious-first ranking). `status` /
    `submitted_at` shape the attempt lifecycle (an in-progress one, a just-submitted
    one inside the finalize grace window, ...).
    """
    repo = AttemptRepository(session)
    entry = RosterEntry(id=f"{test_id}-{name}", test_id=test_id, display_name=name)
    repo.add_roster_entry(entry)
    attempt_id = f"att-{test_id}-{name}"
    repo.add_attempt(
        Attempt(
            id=attempt_id,
            test_id=test_id,
            roster_entry_id=entry.id,
            status=status,  # type: ignore[arg-type]
            seed=1,
            started_at=T0,
            submitted_at=submitted_at if status != "in_progress" else None,
            deadline=T0 + timedelta(hours=1),
        )
    )
    repo.update_roster_entry(
        entry.model_copy(update={"status": status, "attempt_id": attempt_id})
    )
    repo.save_answer(
        Answer(
            attempt_id=attempt_id,
            item_id=f"{test_id}-q1",
            response=answer,
            answered_at=T0 + timedelta(seconds=1),
        )
    )

    events = EventRepository(session)
    if hidden_secs:
        ts = T0 + timedelta(seconds=10)
        events.add_event(
            IntegrityEvent(
                attempt_id=attempt_id, type="visibility_hidden", client_ts=ts, server_ts=ts
            )
        )
        back = ts + timedelta(seconds=hidden_secs)
        events.add_event(
            IntegrityEvent(
                attempt_id=attempt_id, type="visibility_visible", client_ts=back, server_ts=back
            )
        )
    else:
        events.add_event(
            IntegrityEvent(
                attempt_id=attempt_id, item_id=f"{test_id}-q1", type="interaction",
                client_ts=T0, server_ts=T0,
            )
        )
    session.commit()
    return attempt_id


# --------------------------------------------------------------------------- #
# Service-layer: approve is the publish gate (rule #5).
# --------------------------------------------------------------------------- #
def test_approve_flips_draft_to_published(session: Session, tmp_path) -> None:
    svc = build_admin_service(session, FilesystemStorage(tmp_path), analysis_llm=MockAnalysisLLM())
    svc.content.create_test(_test("t1", status="draft"))
    session.commit()

    assert [d.test_id for d in svc.review_queue()] == ["t1"]
    published = svc.approve("t1")
    assert published.status == "published"
    # No longer a draft -> drops out of the review queue, and re-approve is refused.
    assert svc.review_queue() == []
    with pytest.raises(AdminError):
        svc.approve("t1")


def test_unpublish_reverts_only_published(session: Session, tmp_path) -> None:
    svc = build_admin_service(session, FilesystemStorage(tmp_path), analysis_llm=MockAnalysisLLM())
    svc.content.create_test(_test("t1", status="draft"))
    session.commit()

    # A draft cannot be unpublished (guarded like approve refuses non-drafts).
    with pytest.raises(AdminError):
        svc.unpublish("t1")

    svc.approve("t1")
    reverted = svc.unpublish("t1")
    assert reverted.status == "draft"
    # Back in the review queue after the revert.
    assert [d.test_id for d in svc.review_queue()] == ["t1"]


def test_results_independent_and_ranked(session: Session, tmp_path) -> None:
    svc = build_admin_service(session, FilesystemStorage(tmp_path), analysis_llm=MockAnalysisLLM())
    svc.content.create_test(_test("t1", status="published"))
    session.commit()

    # Honest: correct answer, no hidden time. Cheaty: wrong answer, long hide.
    honest = _seed_attempt(session, test_id="t1", name="Honest", answer="A")
    cheaty = _seed_attempt(session, test_id="t1", name="Cheaty", answer="B", hidden_secs=120)

    ranked = svc.results_for_test("t1")
    assert [o.attempt_id for o in ranked] == [cheaty, honest]  # suspicious-first
    # Score is independent of suspicion (rule #2): the more-suspicious attempt
    # actually scored *lower* here, and ranking never touched either score.
    assert ranked[0].suspicion_score >= ranked[1].suspicion_score
    assert ranked[0].score == 0.0 and ranked[1].score == 1.0

    detail = svc.attempt_result(honest)
    assert detail.grading.score == 1.0
    assert detail.event_count == len(detail.events) >= 1


# --------------------------------------------------------------------------- #
# Token signer.
# --------------------------------------------------------------------------- #
def test_token_roundtrip_and_expiry() -> None:
    signer = TokenSigner("secret", ttl_seconds=100)
    now = time.time()
    token = signer.mint(now=now)
    assert signer.verify(token, now=now + 10) == "teacher"
    with pytest.raises(AuthError):
        signer.verify(token, now=now + 101)  # expired
    with pytest.raises(AuthError):
        signer.verify(token + "x", now=now)  # tampered signature
    # A different secret cannot validate the token.
    with pytest.raises(AuthError):
        TokenSigner("other", ttl_seconds=100).verify(token, now=now)


# --------------------------------------------------------------------------- #
# HTTP surface: auth enforced + the full gate flow.
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(admin_api.settings, "teacher_password", PASSWORD)
    # Point asset storage + the analyst at deterministic test doubles by overriding
    # the service factory dependency to inject the mock analyst.
    app.dependency_overrides[get_session] = lambda: session

    def _svc() -> Iterator[AdminService]:
        yield build_admin_service(
            session, FilesystemStorage(tmp_path), analysis_llm=MockAnalysisLLM()
        )

    app.dependency_overrides[admin_api.get_admin_service] = _svc
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_auth_is_enforced(client: TestClient) -> None:
    # No token -> 401.
    assert client.get("/admin/tests").status_code == 401
    # Bad token -> 401.
    assert client.get("/admin/tests", headers=_auth("garbage")).status_code == 401
    # Wrong password -> 401, no token issued.
    assert client.post("/admin/login", json={"password": "nope"}).status_code == 401


def test_login_then_full_flow(client: TestClient, session: Session, tmp_path) -> None:
    # Login mints a usable token.
    resp = client.post("/admin/login", json={"password": PASSWORD})
    assert resp.status_code == 200
    token = resp.json()["token"]
    headers = _auth(token)

    # Seed a draft + a graded/observed attempt directly in the DB.
    svc = build_admin_service(session, FilesystemStorage(tmp_path))
    svc.content.create_test(_test("t1", status="draft"))
    session.commit()
    attempt_id = _seed_attempt(session, test_id="t1", name="Ann", answer="A", hidden_secs=90)

    # Review queue lists the draft; approve publishes it.
    assert [d["test_id"] for d in client.get("/admin/review", headers=headers).json()] == ["t1"]
    approved = client.post("/admin/review/t1/approve", headers=headers)
    assert approved.status_code == 200 and approved.json()["status"] == "published"
    assert client.get("/admin/review", headers=headers).json() == []

    # Results endpoint returns score + verdict + event count.
    detail = client.get(f"/admin/results/{attempt_id}", headers=headers).json()
    assert detail["grading"]["score"] == 1.0
    assert detail["grading"]["max_score"] == 1.0
    assert detail["verdict"]["suspicion_score"] > 0.0  # mock analyst saw the hide
    assert detail["event_count"] == 2

    # Ranked list is reachable and carries the same score (independent of verdict).
    ranked = client.get("/admin/tests/t1/results", headers=headers).json()
    assert ranked[0]["attempt_id"] == attempt_id
    assert ranked[0]["score"] == 1.0


def test_admin_actions_are_logged(
    session: Session, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    svc = build_admin_service(session, FilesystemStorage(tmp_path), analysis_llm=MockAnalysisLLM())
    svc.content.create_test(_test("t1", status="draft"))
    session.commit()
    with caplog.at_level(logging.INFO):
        svc.approve("t1")
    assert any("APPROVE" in r.message and "t1" in r.message for r in caplog.records)


def test_regrade_finalizes_and_reads_persist(session: Session, tmp_path) -> None:
    """An un-finalized attempt reads as 'pending'; re-grade persists the report,
    and subsequent reads serve the frozen grade (no LLM on the read path)."""
    from app.feedback import MockFeedbackAnalyst
    from app.grading.llm import MockLLMGrader

    svc = build_admin_service(
        session,
        FilesystemStorage(tmp_path),
        analysis_llm=MockAnalysisLLM(),
        llm_grader=MockLLMGrader(),
        feedback_llm=MockFeedbackAnalyst(),
    )
    svc.content.create_test(_test("t1", status="published"))
    session.commit()
    attempt_id = _seed_attempt(session, test_id="t1", name="Ann", answer="A")

    # No report, and the attempt finished long ago (T0) -> the overview says the
    # finalize is "missing" (past the grace window); the detail view reads "pending".
    assert svc.results_for_test("t1")[0].report_status == "missing"
    assert svc.attempt_result(attempt_id).report_status == "pending"

    # Re-grade runs the one-shot finalize and persists the report.
    detail = svc.regrade(attempt_id)
    assert detail.report_status == "complete"
    assert [s.skill for s in detail.skills] == ["reading"]
    assert svc.attempts.get_report(attempt_id) is not None

    # Now the read path serves the frozen report (status complete).
    assert svc.results_for_test("t1")[0].report_status == "complete"


# --------------------------------------------------------------------------- #
# Report status chip: pending / complete / failed / missing (+ error exposed).
# --------------------------------------------------------------------------- #
def _llm_svc(session: Session, tmp_path) -> AdminService:
    from app.feedback import MockFeedbackAnalyst
    from app.grading.llm import MockLLMGrader

    return build_admin_service(
        session,
        FilesystemStorage(tmp_path),
        analysis_llm=MockAnalysisLLM(),
        llm_grader=MockLLMGrader(),
        feedback_llm=MockFeedbackAnalyst(),
    )


def test_results_report_status_and_error(session: Session, tmp_path) -> None:
    from datetime import UTC as _UTC

    svc = _llm_svc(session, tmp_path)
    svc.content.create_test(_test("t1", status="published"))
    session.commit()
    old = _seed_attempt(session, test_id="t1", name="Old", answer="A")
    fresh = _seed_attempt(
        session, test_id="t1", name="Fresh", answer="A", submitted_at=datetime.now(_UTC)
    )
    working = _seed_attempt(session, test_id="t1", name="Working", answer="A", status="in_progress")
    failed = _seed_attempt(session, test_id="t1", name="Failed", answer="A")
    svc.results.record_failure(failed, RuntimeError("gemini exploded"))
    session.commit()

    rows = {o.attempt_id: o for o in svc.results_for_test("t1")}
    assert rows[old].report_status == "missing"  # finished long ago, no report
    assert rows[fresh].report_status == "pending"  # inside the grace window
    assert rows[working].report_status == "pending"
    assert rows[working].attempt_status == "in_progress"
    assert rows[failed].report_status == "failed"
    assert rows[failed].report_error == "RuntimeError: gemini exploded"
    assert rows[old].report_error is None
    # Detail view carries the error too.
    assert svc.attempt_result(failed).report_error == "RuntimeError: gemini exploded"


@contextmanager
def _fake_scope(session: Session, trace: list[str] | None = None):
    """Stand-in for `session_scope` on the test session (commit, never close)."""
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
    """Point `app.results.finalize` (the per-attempt runner) at the test session + mocks."""
    from app.feedback import MockFeedbackAnalyst
    from app.grading.llm import MockLLMGrader
    from app.results import finalize as bg

    monkeypatch.setattr(bg, "session_scope", lambda: _fake_scope(session, trace))
    monkeypatch.setattr(bg, "_writing_grader", lambda: MockLLMGrader())
    monkeypatch.setattr(bg, "_feedback_analyst", lambda: MockFeedbackAnalyst())
    return bg


@contextmanager
def _held_elsewhere(attempt_id: str):
    """Hold the attempt's finalize lock on another thread (a finalize in flight)."""
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


def test_report_status_in_flight_and_grace(session: Session, tmp_path) -> None:
    """In flight -> "pending" even far past the grace window (submitted AND expired,
    whose age basis is the deadline); idle past grace -> "missing"; grace = 300 s."""
    from app.admin.service import PENDING_GRACE_SECONDS

    assert PENDING_GRACE_SECONDS == 300
    svc = _llm_svc(session, tmp_path)
    svc.content.create_test(_test("t1", status="published"))
    session.commit()
    now = datetime.now(UTC)
    old = _seed_attempt(session, test_id="t1", name="Old", answer="A")  # T0: long ago
    expired = _seed_attempt(session, test_id="t1", name="Exp", answer="A", status="in_progress")
    # Lazily expired (delivery's get_state crossed the deadline): no submitted_at,
    # deadline T0+1h is the finish time — long ago.
    repo = AttemptRepository(session)
    repo.update_attempt(repo.get_attempt(expired).model_copy(update={"status": "expired"}))
    session.commit()
    recent = _seed_attempt(
        session, test_id="t1", name="Recent", answer="A",
        submitted_at=now - timedelta(seconds=200),  # past the old 120 s grace
    )
    stale = _seed_attempt(
        session, test_id="t1", name="Stale", answer="A",
        submitted_at=now - timedelta(seconds=PENDING_GRACE_SECONDS + 5),
    )

    def statuses() -> dict[str, str]:
        return {o.attempt_id: o.report_status for o in svc.results_for_test("t1")}

    idle = statuses()
    assert idle[old] == "missing" and idle[expired] == "missing" and idle[stale] == "missing"
    assert idle[recent] == "pending"  # inside the 300 s grace

    with _held_elsewhere(old), _held_elsewhere(expired):
        busy = statuses()
    assert busy[old] == "pending"  # finalize in flight: never flash "missing"
    assert busy[expired] == "pending"  # lazily-expired attempt being finalized
    assert busy[stale] == "missing"  # not in flight -> still missing
    assert statuses()[old] == "missing"  # lock released, still no report


def test_finalize_missing_recovers_lost_reports(
    session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bg_on(session, monkeypatch)
    svc = _llm_svc(session, tmp_path)
    svc.content.create_test(_test("t1", status="published"))
    svc.content.create_test(_test("t2", status="published"))
    session.commit()
    lost = _seed_attempt(session, test_id="t1", name="Lost", answer="A")
    failed = _seed_attempt(session, test_id="t1", name="Failed", answer="B")
    done = _seed_attempt(session, test_id="t1", name="Done", answer="A")
    other = _seed_attempt(session, test_id="t2", name="Other", answer="A")
    svc.results.record_failure(failed, RuntimeError("boom"))
    svc.results.finalize(done)
    session.commit()

    processed = svc.finalize_missing("t1")
    assert sorted(processed) == sorted([lost, failed])  # scoped to t1; `done` untouched
    rows = {o.attempt_id: o for o in svc.results_for_test("t1")}
    assert {rows[a].report_status for a in (lost, failed, done)} == {"complete"}
    assert rows[failed].report_error is None  # the re-run cleared the error
    assert svc.results_for_test("t2")[0].report_status == "missing"
    assert svc.attempts.get_report(other) is None
    with pytest.raises(AdminError):
        svc.finalize_missing("nope")


def test_admin_finalize_missing_uses_per_attempt_transactions(
    session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The button never loops LLM work on the request session: the request's read
    txn is ended first, then the runner opens one sequential txn per attempt; a
    failure on the first attempt still lets the second complete."""
    from app.results import ResultsService

    trace: list[str] = []
    _bg_on(session, monkeypatch, trace)
    svc = _llm_svc(session, tmp_path)
    svc.content.create_test(_test("t1", status="published"))
    session.commit()
    first = _seed_attempt(
        session, test_id="t1", name="First", answer="A", submitted_at=T0 + timedelta(minutes=1)
    )
    second = _seed_attempt(session, test_id="t1", name="Second", answer="B")
    svc.get_test("t1")  # the request has read something -> a txn may be open
    real = ResultsService.finalize
    depths: list[int] = []

    def flaky(self, attempt_id):  # noqa: ANN001
        depths.append(trace.count("open") - trace.count("close"))
        if attempt_id == first:
            raise RuntimeError("gemini down")
        return real(self, attempt_id)

    monkeypatch.setattr(ResultsService, "finalize", flaky)
    assert svc.finalize_missing("t1") == [first, second]
    # 1 expire-overdue txn + 1 candidate read + per attempt: finalize txn (+ the
    # failure-record txn for `first`).
    assert trace == ["open", "close"] * 5
    assert depths == [1, 1]  # each ran inside exactly its own txn
    assert svc.attempts.get_report(first).status == "failed"
    assert svc.attempts.get_report(first).error == "RuntimeError: gemini down"
    assert svc.attempts.get_report(second).status == "complete"


def test_admin_finalize_missing_skips_in_flight(
    session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bg_on(session, monkeypatch)
    svc = _llm_svc(session, tmp_path)
    svc.content.create_test(_test("t1", status="published"))
    session.commit()
    busy = _seed_attempt(session, test_id="t1", name="Busy", answer="A")
    idle = _seed_attempt(session, test_id="t1", name="Idle", answer="A")
    with _held_elsewhere(busy):
        assert svc.finalize_missing("t1") == [idle]
    assert svc.attempts.get_report(busy) is None  # no duplicate paid run
    assert svc.finalize_missing("t1") == [busy]


def test_admin_finalize_missing_expires_abandoned_attempts(
    session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Дооценить все" also recovers a student who closed the tab: the test's
    in_progress attempts past their deadline are expired, then finalized."""
    _bg_on(session, monkeypatch)
    svc = _llm_svc(session, tmp_path)
    svc.content.create_test(_test("t1", status="published"))
    svc.content.create_test(_test("t2", status="published"))
    session.commit()
    # Deadline T0+1h is long past: abandoned, never expired lazily.
    gone = _seed_attempt(session, test_id="t1", name="Gone", answer="A", status="in_progress")
    other = _seed_attempt(session, test_id="t2", name="Other", answer="A", status="in_progress")
    assert {o.attempt_id: o.report_status for o in svc.results_for_test("t1")}[gone] == "pending"

    assert svc.finalize_missing("t1") == [gone]
    assert svc.attempts.get_attempt(gone).status == "expired"
    rows = {o.attempt_id: o for o in svc.results_for_test("t1")}
    assert rows[gone].report_status == "complete"
    # Scoped to the test: another test's abandoned attempt is left to its own run.
    assert svc.attempts.get_attempt(other).status == "in_progress"


def test_admin_finalize_missing_ends_request_txn_before_runner(session: Session, tmp_path) -> None:
    from app.admin.service import AdminService

    base = build_admin_service(session, FilesystemStorage(tmp_path))
    base.content.create_test(_test("t1", status="published"))
    session.commit()
    seen: list[bool] = []

    def runner(test_id: str) -> list[str]:
        seen.append(session.in_transaction())
        return ["x"]

    svc = AdminService(
        content=base.content, roster=base.roster, grading=base.grading,
        integrity=base.integrity, analysis=base.analysis, results=base.results,
        attempts=base.attempts, events=base.events, finalize_missing_runner=runner,
    )
    assert svc.finalize_missing("t1") == ["x"]
    assert seen == [False]


# --------------------------------------------------------------------------- #
# Recorder-truncated answer_change: shown as head … [+N chars] … tail.
# --------------------------------------------------------------------------- #
def test_truncated_answer_change_rendered_in_history_and_timeline(
    session: Session, tmp_path
) -> None:
    svc = build_admin_service(session, FilesystemStorage(tmp_path), analysis_llm=MockAnalysisLLM())
    svc.content.create_test(
        ExamTest(
            id="tw",
            title="B1 — writing",
            level="B1_PRELIMINARY",
            status="published",
            duration_minutes=60,
            sections=[
                Section(
                    id="tw-sec",
                    skill="writing",
                    stimulus=PassageTextStimulus(text="Write."),
                    items=[
                        OpenWritingItem(
                            id="tw-w1", prompt="Write.", word_min=25, bullet_points=[], rubric="r"
                        )
                    ],
                )
            ],
        )
    )
    repo = AttemptRepository(session)
    entry = RosterEntry(id="tw-Ann", test_id="tw", display_name="Ann")
    repo.add_roster_entry(entry)
    repo.add_attempt(
        Attempt(
            id="att-tw", test_id="tw", roster_entry_id=entry.id, status="submitted", seed=1,
            started_at=T0, submitted_at=T0 + timedelta(minutes=5),
            deadline=T0 + timedelta(hours=1),
        )
    )
    full = "H" * 200 + "m" * 2600 + "T" * 200  # 3000 chars > 2048 -> truncated
    repo.save_answer(
        Answer(attempt_id="att-tw", item_id="tw-w1", response=full, answered_at=T0)
    )
    ev = EventRepository(session)
    ts1, ts2 = T0 + timedelta(seconds=5), T0 + timedelta(seconds=9)
    ev.add_event(
        IntegrityEvent(
            attempt_id="att-tw", item_id="tw-w1", type="answer_change",
            client_ts=ts1, server_ts=ts1, payload={"value": "short draft"},
        )
    )
    ev.add_event(
        IntegrityEvent(
            attempt_id="att-tw", item_id="tw-w1", type="answer_change",
            client_ts=ts2, server_ts=ts2,
            payload={
                "value": full[:200], "tail": full[-200:], "length": len(full), "truncated": True
            },
        )
    )
    session.commit()

    detail = svc.attempt_result("att-tw")
    expected = "H" * 200 + " … [+2600 chars] … " + "T" * 200
    (resp,) = [r for r in detail.responses if r.item_id == "tw-w1"]
    assert [h.value for h in resp.history] == ["short draft", expected]
    labels = [t.label for t in detail.timeline if t.type == "answer_change"]
    assert labels[0].endswith('answered "short draft"')
    assert labels[1].endswith(f'answered "{expected}"')
    assert resp.writing_text == full  # the stored answer itself is never truncated


# --------------------------------------------------------------------------- #
# Error mapping: 404 unknown, 400 bad value, 409 wrong state.
# --------------------------------------------------------------------------- #
def test_admin_errors_map_to_http_status(client: TestClient, session: Session, tmp_path) -> None:
    from app.admin.service import AdminStateError, AdminValidationError

    headers = _auth(client.post("/admin/login", json={"password": PASSWORD}).json()["token"])
    svc = build_admin_service(session, FilesystemStorage(tmp_path))
    svc.content.create_test(_test("t1", status="published"))
    session.commit()
    finished = _seed_attempt(session, test_id="t1", name="Ann", answer="A")
    working = _seed_attempt(session, test_id="t1", name="Bob", answer="A", status="in_progress")

    # Unknown targets stay 404.
    assert client.get("/admin/tests/nope", headers=headers).status_code == 404
    assert client.post("/admin/results/nope/regrade", headers=headers).status_code == 404
    assert client.post("/admin/tests/nope/finalize-missing", headers=headers).status_code == 404
    # Approving a published test is a state conflict -> 409, not 404.
    assert client.post("/admin/review/t1/approve", headers=headers).status_code == 409
    # A mark out of range -> 400.
    resp = client.put(
        f"/admin/results/{finished}/grades/t1-q1", json={"awarded": 7}, headers=headers
    )
    assert resp.status_code == 400
    assert "out of range" in resp.json()["detail"]
    # Marking an attempt still in progress -> 409.
    resp = client.put(
        f"/admin/results/{working}/grades/t1-q1", json={"awarded": 1}, headers=headers
    )
    assert resp.status_code == 409
    # A valid mark on a finished attempt still works.
    resp = client.put(
        f"/admin/results/{finished}/grades/t1-q1", json={"awarded": 0}, headers=headers
    )
    assert resp.status_code == 200 and resp.json()["grading"]["score"] == 0.0
    # The service-level exceptions carry their status.
    assert AdminValidationError.status == 400 and AdminStateError.status == 409
    assert issubclass(AdminStateError, AdminError) and AdminError.status == 404


def test_finalize_missing_endpoint(
    client: TestClient, session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bg_on(session, monkeypatch)
    headers = _auth(client.post("/admin/login", json={"password": PASSWORD}).json()["token"])
    svc = build_admin_service(session, FilesystemStorage(tmp_path))
    svc.content.create_test(_test("t1", status="published"))
    session.commit()
    attempt_id = _seed_attempt(session, test_id="t1", name="Ann", answer="A")

    rows = client.get("/admin/tests/t1/results", headers=headers).json()
    assert rows[0]["report_status"] == "missing" and rows[0]["report_error"] is None

    resp = client.post("/admin/tests/t1/finalize-missing", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"attempt_ids": [attempt_id]}
    rows = client.get("/admin/tests/t1/results", headers=headers).json()
    assert rows[0]["report_status"] == "complete"
    assert rows[0]["attempt_status"] == "submitted"
    # Nothing left to do -> empty, not an error.
    assert client.post("/admin/tests/t1/finalize-missing", headers=headers).json() == {
        "attempt_ids": []
    }


# --------------------------------------------------------------------------- #
# Explicit regrade always re-runs; roster status after auto-expiry.
# --------------------------------------------------------------------------- #
def test_explicit_regrade_always_reruns_even_with_complete_report(
    session: Session, tmp_path
) -> None:
    """`POST /admin/results/{id}/regrade` is the teacher's deliberate re-run: no
    "already done" skip (unlike the background task / sweep / grade-all button)."""
    from app.feedback import MockFeedbackAnalyst
    from app.grading.llm import MockLLMGrader

    class Counting(MockFeedbackAnalyst):
        calls = 0

        def analyze(self, breakdowns):  # noqa: ANN001
            Counting.calls += 1
            return super().analyze(breakdowns)

    svc = build_admin_service(
        session,
        FilesystemStorage(tmp_path),
        analysis_llm=MockAnalysisLLM(),
        llm_grader=MockLLMGrader(),
        feedback_llm=Counting(),
    )
    svc.content.create_test(_test("t1", status="published"))
    session.commit()
    attempt_id = _seed_attempt(session, test_id="t1", name="Ann", answer="A")
    assert svc.regrade(attempt_id).report_status == "complete"
    assert svc.regrade(attempt_id).report_status == "complete"
    assert Counting.calls == 2


def _expire_via_delivery(session: Session, test_id: str = "t1") -> list[str]:
    from app.delivery import DeliveryService
    from app.persistence.repository import ContentRepository

    ids = DeliveryService(ContentRepository(session), AttemptRepository(session)).expire_overdue(
        datetime.now(UTC), test_id=test_id
    )
    session.commit()
    return ids


def test_roster_status_shows_expired_after_auto_expiry(session: Session, tmp_path) -> None:
    svc = _llm_svc(session, tmp_path)
    svc.content.create_test(_test("t1", status="published"))
    session.commit()
    attempt_id = _seed_attempt(session, test_id="t1", name="Exp", answer="A", status="in_progress")
    assert [r.status for r in svc.roster_status("t1")] == ["in_progress"]
    assert _expire_via_delivery(session) == [attempt_id]
    [row] = svc.roster_status("t1")
    assert row.status == "expired" and row.attempt_id == attempt_id


def test_reset_and_reopen_restore_roster_from_expired(session: Session, tmp_path) -> None:
    """Reset -> `not_started` (fresh retake); reopen -> `in_progress` with the
    original deadline kept (rule #3), so a past deadline re-expires it at once —
    roster included."""
    svc = _llm_svc(session, tmp_path)
    svc.content.create_test(_test("t1", status="published"))
    session.commit()
    a1 = _seed_attempt(session, test_id="t1", name="A1", answer="A", status="in_progress")
    a2 = _seed_attempt(session, test_id="t1", name="A2", answer="A", status="in_progress")
    assert set(_expire_via_delivery(session)) == {a1, a2}
    statuses = {r.display_name: r.status for r in svc.roster_status("t1")}
    assert statuses == {"A1": "expired", "A2": "expired"}

    reset = svc.reset_attempt("t1-A1")
    assert reset.status == "not_started" and reset.attempt_id is None

    reopened = svc.reopen_attempt("t1-A2")
    session.commit()
    assert reopened.status == "in_progress" and reopened.attempt_id == a2
    repo = AttemptRepository(session)
    attempt = repo.get_attempt(a2)
    assert attempt.status == "in_progress"
    assert attempt.deadline == T0 + timedelta(hours=1)  # never extended
    # The deadline is long past: the next expiry flips attempt AND roster again.
    assert _expire_via_delivery(session) == [a2]
    statuses = {r.display_name: r.status for r in svc.roster_status("t1")}
    assert statuses == {"A1": "not_started", "A2": "expired"}
