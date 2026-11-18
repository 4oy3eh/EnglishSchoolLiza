"""Scheduled exam window: `Test.opens_at` / `closes_at` / `late_start_grace_minutes`.

Covers the contract (aware datetimes, order, grace bounds), persistence (columns,
round trip, the narrow `set_schedule` UPDATE), the content lock (the window is
schedule meta: allowed on a published test with attempts, no `force`, sections
untouched), delivery (a NEW start respects the window, a resume never does, the
deadline is min(start + duration, close + grace) and fixed at start — golden rule
#3; the recovery sweep still expires overdue attempts), the student HTTP surface
(`/info` window state on the server clock, `/start` 403 + `X-Exam-Window` BEFORE the
PIN, so typos on a closed test are not counted) and the teacher API (set / clear,
«Открыть сейчас на N минут», «Закрыть сейчас», server time; 401 anonymous; Russian
400s). The migration round trip on a scratch DB is at the end.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.admin import AdminService, build_admin_service
from app.analysis import MockAnalysisLLM
from app.content import ContentService, FilesystemStorage
from app.content.editor import (
    ContentValidationError,
    check_edit_allowed,
    is_cosmetic_change,
    is_schedule_only_change,
)
from app.core.db import get_session
from app.delivery import (
    ALWAYS_OPEN,
    DeliveryService,
    ExamWindow,
    WindowClosedError,
    WindowNotOpenError,
)
from app.delivery.access import get_pin_limiter
from app.persistence.repository import AttemptRepository, ContentRepository
from apps.api import admin as admin_api
from apps.api import assets as assets_api
from apps.api.main import app
from contracts import (
    Attempt,
    GapFillItem,
    PassageTextStimulus,
    RosterEntry,
    Section,
    SingleChoiceItem,
    TextOption,
)
from contracts import Test as ExamTest

REPO_ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
PASSWORD = "window-teacher-pw"
LOOPBACK = ("127.0.0.1", 50000)
PINS = {"e1": "AB3D", "e2": "XY7K", "e3": "MN5P"}


def _test(**update: object) -> ExamTest:
    test = ExamTest(
        id="t1",
        title="A2 Key — Window",
        level="A2_KEY",
        status="published",
        duration_minutes=30,
        sections=[
            Section(
                id="t1-s1",
                skill="reading",
                stimulus=PassageTextStimulus(text="Read this."),
                items=[
                    SingleChoiceItem(
                        id="t1-q1",
                        prompt="Pick one",
                        options=[
                            TextOption(key="A", text="alpha"),
                            TextOption(key="B", text="bravo"),
                        ],
                        correct="B",
                    ),
                    GapFillItem(id="t1-q2", prompt="Fill", accepted=["house"]),
                ],
            )
        ],
    )
    return ExamTest.model_validate({**test.model_dump(), **update})


def _seed(session: Session, test: ExamTest | None = None, entries: int = 2) -> None:
    ContentRepository(session).add_test(test or _test())
    repo = AttemptRepository(session)
    for n in range(1, entries + 1):
        repo.add_roster_entry(
            RosterEntry(id=f"e{n}", test_id="t1", display_name=f"S{n}", pin=PINS[f"e{n}"])
        )
    session.commit()


# --------------------------------------------------------------------------- #
# Contract.
# --------------------------------------------------------------------------- #
def test_contract_window_defaults_to_unbounded() -> None:
    t = _test()
    assert (t.opens_at, t.closes_at, t.late_start_grace_minutes) == (None, None, 0)
    assert ExamWindow.for_test(t) == ALWAYS_OPEN
    assert ExamWindow.for_test(t).state(BASE) == "open"


def test_contract_requires_timezone_and_order() -> None:
    with pytest.raises(ValidationError):
        _test(opens_at=datetime(2026, 11, 17, 9, 0))  # naive
    with pytest.raises(ValidationError):
        _test(opens_at=BASE, closes_at=BASE)  # empty window
    with pytest.raises(ValidationError):
        _test(opens_at=BASE, closes_at=BASE - timedelta(minutes=1))
    with pytest.raises(ValidationError):
        _test(late_start_grace_minutes=-1)
    with pytest.raises(ValidationError):
        _test(late_start_grace_minutes=1441)
    # Any offset is accepted and compares correctly (Kyiv = UTC+2 in November).
    kyiv = datetime(2026, 11, 17, 11, 0, tzinfo=timezone(timedelta(hours=2)))
    t = _test(opens_at=kyiv, closes_at=BASE + timedelta(hours=1))
    assert t.opens_at == BASE


def test_window_state_and_grace() -> None:
    t = _test(opens_at=BASE, closes_at=BASE + timedelta(hours=1), late_start_grace_minutes=10)
    w = ExamWindow.for_test(t)
    assert w.hard_close == BASE + timedelta(minutes=70)
    assert w.state(BASE - timedelta(seconds=1)) == "not_yet_open"
    assert w.state(BASE) == "open"
    assert w.state(BASE + timedelta(minutes=65)) == "open"  # inside the grace
    assert w.state(BASE + timedelta(minutes=70, seconds=1)) == "closed"
    # Only one bound set: the other is unbounded.
    only_close = ExamWindow.for_test(_test(closes_at=BASE))
    assert only_close.state(datetime(2000, 1, 1, tzinfo=UTC)) == "open"
    only_open = ExamWindow.for_test(_test(opens_at=BASE))
    assert only_open.state(datetime(2999, 1, 1, tzinfo=UTC)) == "open"


# --------------------------------------------------------------------------- #
# Persistence + content service + the editor lock.
# --------------------------------------------------------------------------- #
def test_window_round_trips_through_the_repository(session: Session) -> None:
    repo = ContentRepository(session)
    repo.add_test(_test(opens_at=BASE, closes_at=BASE + timedelta(hours=2),
                        late_start_grace_minutes=5))
    session.commit()
    session.expire_all()
    back = repo.get_test("t1")
    assert back is not None
    assert back.opens_at == BASE and back.opens_at.tzinfo is not None
    assert back.closes_at == BASE + timedelta(hours=2)
    assert back.late_start_grace_minutes == 5
    # replace_test (the editor / import path) writes the window too.
    repo.replace_test(back.model_copy(update={"late_start_grace_minutes": 7}))
    session.expire_all()
    assert repo.get_test("t1").late_start_grace_minutes == 7  # type: ignore[union-attr]


def test_set_schedule_allowed_on_published_test_with_attempts(
    session: Session, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _seed(session)
    AttemptRepository(session).add_attempt(
        Attempt(id="a1", test_id="t1", roster_entry_id="e1", status="submitted", seed=1)
    )
    session.commit()
    svc = ContentService(ContentRepository(session), FilesystemStorage(tmp_path))
    before = svc.get_test("t1")
    with caplog.at_level(logging.INFO):
        out = svc.set_schedule(
            "t1",
            opens_at=BASE,
            closes_at=BASE + timedelta(hours=1),
            late_start_grace_minutes=15,
        )
    assert (out.opens_at, out.closes_at, out.late_start_grace_minutes) == (
        BASE,
        BASE + timedelta(hours=1),
        15,
    )
    session.expire_all()
    after = svc.get_test("t1")
    # Only the window changed: content + status untouched, no WARNING (not forced).
    assert after.model_dump(exclude={"opens_at", "closes_at", "late_start_grace_minutes"}) == (
        before.model_dump(exclude={"opens_at", "closes_at", "late_start_grace_minutes"})
    )
    assert after.opens_at == BASE
    assert "content set_schedule test=t1" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    # Clearing works too.
    cleared = svc.set_schedule("t1", opens_at=None, closes_at=None)
    assert (cleared.opens_at, cleared.closes_at, cleared.late_start_grace_minutes) == (
        None,
        None,
        0,
    )


@pytest.mark.parametrize(
    ("opens", "closes", "grace", "fragment"),
    [
        (BASE, BASE, 0, "позже времени открытия"),
        (BASE, BASE - timedelta(hours=1), 0, "позже времени открытия"),
        (datetime(2026, 11, 17, 9, 0), None, 0, "часовым поясом"),
        (None, None, -5, "от 0 до 1440"),
        (None, None, 2000, "от 0 до 1440"),
    ],
)
def test_set_schedule_refusals_are_russian(
    session: Session,
    tmp_path: Path,
    opens: datetime | None,
    closes: datetime | None,
    grace: int,
    fragment: str,
) -> None:
    _seed(session)
    svc = ContentService(ContentRepository(session), FilesystemStorage(tmp_path))
    with pytest.raises(ContentValidationError, match=fragment):
        svc.set_schedule("t1", opens_at=opens, closes_at=closes, late_start_grace_minutes=grace)


def test_editor_lock_treats_the_window_as_schedule_meta() -> None:
    old = _test()
    new = old.model_copy(update={"opens_at": BASE, "late_start_grace_minutes": 3})
    assert is_schedule_only_change(old, new)
    assert is_cosmetic_change(old, new)  # not part of the key signature
    # Allowed WITHOUT force even published with attempts.
    assert check_edit_allowed(old, new, attempts=5, force=False) is True
    # A window change bundled with a key change is still refused.
    from app.content.editor import ContentConflictError

    keyed = new.model_copy(deep=True)
    keyed.sections[0].items[0].correct = "A"  # type: ignore[union-attr]
    assert not is_schedule_only_change(old, keyed)
    with pytest.raises(ContentConflictError):
        check_edit_allowed(old, keyed, attempts=5, force=True)


def test_export_import_keep_the_window(session: Session, tmp_path: Path) -> None:
    svc = ContentService(ContentRepository(session), FilesystemStorage(tmp_path))
    svc.create_test(_test(opens_at=BASE, closes_at=BASE + timedelta(hours=1)))
    exported = svc.export_test("t1").model_dump(mode="json")
    assert exported["opens_at"].startswith("2026-11-17T09:00:00")
    outcome = svc.import_test(exported, new_id="t2")
    copy = svc.get_test(outcome.test.id)
    assert copy.status == "draft" and copy.opens_at == BASE


# --------------------------------------------------------------------------- #
# Delivery service: new starts vs resume, the deadline cap, the sweep.
# --------------------------------------------------------------------------- #
def _delivery(session: Session) -> DeliveryService:
    return DeliveryService(ContentRepository(session), AttemptRepository(session))


def test_start_uses_the_tests_own_window(session: Session) -> None:
    _seed(
        session,
        _test(opens_at=BASE, closes_at=BASE + timedelta(minutes=40), late_start_grace_minutes=5),
    )
    svc = _delivery(session)
    with pytest.raises(WindowNotOpenError) as early:
        svc.start("e1", now=BASE - timedelta(minutes=1))
    assert early.value.opens_at == BASE
    # In time: full duration (30 min) fits before close + grace.
    a1 = svc.start("e1", now=BASE)
    assert a1.deadline == BASE + timedelta(minutes=30)
    # Late start (inside the grace): deadline capped at close + grace, not start + 30.
    late = BASE + timedelta(minutes=42)
    a2 = svc.start("e2", now=late)
    assert a2.deadline == BASE + timedelta(minutes=45)
    session.commit()
    # After close + grace: no NEW start ...
    _add_entry(session, "e3")
    with pytest.raises(WindowClosedError) as closed:
        svc.start("e3", now=BASE + timedelta(minutes=46))
    assert closed.value.closed_at == BASE + timedelta(minutes=45)
    # ... but a RESUME is never window-checked (its deadline was fixed at start).
    resumed = svc.start("e1", now=BASE + timedelta(minutes=46))
    assert resumed.id == a1.id and resumed.deadline == a1.deadline


def _add_entry(session: Session, entry_id: str) -> None:
    AttemptRepository(session).add_roster_entry(
        RosterEntry(id=entry_id, test_id="t1", display_name=entry_id, pin=PINS[entry_id])
    )
    session.commit()


def test_changing_the_window_never_moves_a_running_deadline(
    session: Session, tmp_path: Path
) -> None:
    _seed(session, _test(opens_at=BASE, closes_at=BASE + timedelta(hours=2)))
    svc = _delivery(session)
    attempt = svc.start("e1", now=BASE + timedelta(minutes=5))
    session.commit()
    content = ContentService(ContentRepository(session), FilesystemStorage(tmp_path))
    # The teacher closes the window early: the running attempt keeps its deadline.
    content.set_schedule("t1", opens_at=BASE, closes_at=BASE + timedelta(minutes=10))
    session.commit()
    state = svc.get_state(attempt.id, now=BASE + timedelta(minutes=20))
    assert state.deadline == BASE + timedelta(minutes=35)
    assert state.status == "in_progress" and not state.expired


def test_recovery_sweep_still_expires_overdue_attempts(session: Session) -> None:
    _seed(session, _test(opens_at=BASE, closes_at=BASE + timedelta(minutes=20)))
    svc = _delivery(session)
    attempt = svc.start("e1", now=BASE + timedelta(minutes=10))
    session.commit()
    assert attempt.deadline == BASE + timedelta(minutes=20)  # capped by the close
    assert svc.expire_overdue(BASE + timedelta(minutes=19)) == []
    assert svc.expire_overdue(BASE + timedelta(minutes=21)) == [attempt.id]
    session.commit()
    assert AttemptRepository(session).get_attempt(attempt.id).status == "expired"  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# Student HTTP surface.
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _spy_finalize(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.results as results

    monkeypatch.setattr(results, "finalize_attempt", lambda _id: None)


@pytest.fixture
def exam_client(session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_session] = lambda: session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _set_window(session: Session, **fields: object) -> None:
    repo = ContentRepository(session)
    repo.set_schedule(
        "t1",
        opens_at=fields.get("opens_at"),  # type: ignore[arg-type]
        closes_at=fields.get("closes_at"),  # type: ignore[arg-type]
        late_start_grace_minutes=int(fields.get("grace", 0)),  # type: ignore[call-overload]
    )
    session.commit()


def test_info_reports_the_window_on_the_server_clock(
    session: Session, exam_client: TestClient
) -> None:
    _seed(session)
    now = datetime.now(UTC)
    opens = now + timedelta(hours=1)
    _set_window(session, opens_at=opens, closes_at=opens + timedelta(hours=1), grace=10)
    info = exam_client.get("/exam/tests/t1/info").json()
    assert info["window_state"] == "not_yet_open"
    assert datetime.fromisoformat(info["opens_at"]) == opens
    assert datetime.fromisoformat(info["starts_until"]) == opens + timedelta(minutes=70)
    assert abs(datetime.fromisoformat(info["server_now"]) - now) < timedelta(seconds=30)

    _set_window(session, opens_at=now - timedelta(hours=2), closes_at=now - timedelta(hours=1))
    assert exam_client.get("/exam/tests/t1/info").json()["window_state"] == "closed"
    _set_window(session, opens_at=now - timedelta(minutes=1))
    info = exam_client.get("/exam/tests/t1/info").json()
    assert info["window_state"] == "open" and info["starts_until"] is None


def test_start_is_refused_outside_the_window_before_the_pin(
    session: Session, exam_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    _seed(session)
    now = datetime.now(UTC)
    opens = now + timedelta(minutes=30)
    _set_window(session, opens_at=opens, closes_at=opens + timedelta(hours=1))
    # Not yet open: 403 + header + opening time, even with a WRONG PIN (never counted).
    with caplog.at_level(logging.WARNING):
        for _ in range(8):
            r = exam_client.post("/exam/roster/e1/start", json={"pin": "ZZZZ"})
            assert r.status_code == 403
            assert r.headers["X-Exam-Window"] == "not_yet_open"
            assert datetime.fromisoformat(r.headers["X-Exam-Opens-At"]) == opens
            assert "X-Pin-Tries-Left" not in r.headers
    assert "rejected -> 403" in caplog.text
    assert get_pin_limiter().test_activity("t1", ["e1"]).wrong_pins == 0

    # Closed: 403 + header.
    _set_window(session, opens_at=now - timedelta(hours=2), closes_at=now - timedelta(minutes=1))
    r = exam_client.post("/exam/roster/e1/start", json={"pin": PINS["e1"]})
    assert r.status_code == 403 and r.headers["X-Exam-Window"] == "closed"
    assert AttemptRepository(session).get_roster_entry("e1").attempt_id is None  # type: ignore[union-attr]


def test_start_inside_the_window_and_resume_after_close(
    session: Session, exam_client: TestClient
) -> None:
    _seed(session)
    now = datetime.now(UTC)
    closes = now + timedelta(minutes=10)  # shorter than the 30-min test
    _set_window(session, opens_at=now - timedelta(minutes=5), closes_at=closes, grace=2)
    r = exam_client.post("/exam/roster/e1/start", json={"pin": PINS["e1"]})
    assert r.status_code == 200, r.text
    body = r.json()
    deadline = datetime.fromisoformat(body["state"]["deadline"])
    assert deadline == closes + timedelta(minutes=2)  # capped by close + grace
    # The teacher closes the window: a NEW start is refused, the resume works.
    _set_window(session, opens_at=now - timedelta(minutes=5), closes_at=now - timedelta(seconds=1))
    assert exam_client.post("/exam/roster/e2/start", json={"pin": PINS["e2"]}).status_code == 403
    again = exam_client.post("/exam/roster/e1/start", json={"pin": PINS["e1"]})
    assert again.status_code == 200
    assert again.json()["attempt_id"] == body["attempt_id"]
    assert datetime.fromisoformat(again.json()["state"]["deadline"]) == deadline


# --------------------------------------------------------------------------- #
# Teacher API.
# --------------------------------------------------------------------------- #
@pytest.fixture
def admin_client(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setattr(admin_api.settings, "teacher_password", PASSWORD)
    monkeypatch.setattr(admin_api.settings, "environment", "test")
    admin_api.login_limiter.reset()
    storage = FilesystemStorage(tmp_path / "assets")
    _seed(session)
    app.dependency_overrides[get_session] = lambda: session

    def _dep() -> Iterator[AdminService]:
        yield build_admin_service(session, storage, analysis_llm=MockAnalysisLLM())

    app.dependency_overrides[admin_api.get_admin_service] = _dep
    app.dependency_overrides[assets_api.get_storage] = lambda: storage
    try:
        yield TestClient(app, client=LOOPBACK)
    finally:
        app.dependency_overrides.clear()
        admin_api.login_limiter.reset()


@pytest.fixture
def auth(admin_client: TestClient) -> dict[str, str]:
    token = admin_client.post("/admin/login", json={"password": PASSWORD}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_window_routes_are_teacher_only(admin_client: TestClient) -> None:
    assert admin_client.get("/admin/server-time").status_code == 401
    assert admin_client.put("/admin/tests/t1/window", json={}).status_code == 401
    assert (
        admin_client.post("/admin/tests/t1/window/open-now", json={"minutes": 5}).status_code
        == 401
    )
    assert admin_client.post("/admin/tests/t1/window/close-now").status_code == 401


def test_teacher_sets_and_clears_the_window(
    admin_client: TestClient, auth: dict, session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    # A published test WITH a finished attempt: the window is still editable.
    AttemptRepository(session).add_attempt(
        Attempt(id="a1", test_id="t1", roster_entry_id="e1", status="submitted", seed=1)
    )
    session.commit()
    body = {
        "opens_at": "2026-11-18T07:00:00Z",  # 09:00 in Kyiv
        "closes_at": "2026-11-18T08:30:00+00:00",
        "late_start_grace_minutes": 10,
    }
    with caplog.at_level(logging.INFO):
        r = admin_client.put("/admin/tests/t1/window", json=body, headers=auth)
    assert r.status_code == 200, r.text
    t = r.json()
    assert datetime.fromisoformat(t["opens_at"]) == datetime(2026, 11, 18, 7, 0, tzinfo=UTC)
    assert t["late_start_grace_minutes"] == 10 and t["status"] == "published"
    assert "admin set_window target=t1 ok" in caplog.text
    # The bank list carries it (no extra request per test).
    listed = {x["id"]: x for x in admin_client.get("/admin/tests", headers=auth).json()}
    assert listed["t1"]["closes_at"] is not None
    # Clear.
    r = admin_client.put("/admin/tests/t1/window", json={}, headers=auth)
    assert r.status_code == 200
    assert (r.json()["opens_at"], r.json()["closes_at"]) == (None, None)


@pytest.mark.parametrize(
    "body",
    [
        {"opens_at": "2026-11-18T09:00:00Z", "closes_at": "2026-11-18T08:00:00Z"},
        {"opens_at": "2026-11-18T09:00:00"},  # no time zone
    ],
)
def test_bad_window_is_a_russian_400(
    admin_client: TestClient, auth: dict, body: dict
) -> None:
    r = admin_client.put("/admin/tests/t1/window", json=body, headers=auth)
    assert r.status_code == 400
    assert "Окно экзамена" in r.json()["detail"] or "часовым поясом" in r.json()["detail"]


def test_unknown_test_window_is_404(admin_client: TestClient, auth: dict) -> None:
    assert admin_client.put("/admin/tests/nope/window", json={}, headers=auth).status_code == 404
    assert (
        admin_client.post("/admin/tests/nope/window/close-now", headers=auth).status_code == 404
    )


def test_open_now_and_close_now(admin_client: TestClient, auth: dict) -> None:
    before = datetime.now(UTC)
    r = admin_client.post("/admin/tests/t1/window/open-now", json={"minutes": 45}, headers=auth)
    assert r.status_code == 200, r.text
    t = r.json()
    opens, closes = datetime.fromisoformat(t["opens_at"]), datetime.fromisoformat(t["closes_at"])
    assert before - timedelta(seconds=1) <= opens <= datetime.now(UTC)
    assert closes - opens == timedelta(minutes=45)
    assert admin_client.get("/exam/tests/t1/info").json()["window_state"] == "open"
    # A student starts while it is open ...
    started = admin_client.post("/exam/roster/e1/start", json={"pin": PINS["e1"]})
    assert started.status_code == 200
    # ... the teacher closes it: no NEW start, the running student resumes.
    r = admin_client.post("/admin/tests/t1/window/close-now", headers=auth)
    assert r.status_code == 200
    t = r.json()
    assert t["late_start_grace_minutes"] == 0
    assert datetime.fromisoformat(t["opens_at"]) == opens  # kept (it is in the past)
    assert admin_client.get("/exam/tests/t1/info").json()["window_state"] == "closed"
    assert admin_client.post("/exam/roster/e2/start", json={"pin": PINS["e2"]}).status_code == 403
    resumed = admin_client.post("/exam/roster/e1/start", json={"pin": PINS["e1"]})
    assert resumed.status_code == 200
    assert resumed.json()["attempt_id"] == started.json()["attempt_id"]
    # Bounds on N.
    for bad in (0, 1441):
        assert (
            admin_client.post(
                "/admin/tests/t1/window/open-now", json={"minutes": bad}, headers=auth
            ).status_code
            == 422
        )


def test_close_now_before_a_future_opening_clears_it(
    admin_client: TestClient, auth: dict
) -> None:
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    admin_client.put("/admin/tests/t1/window", json={"opens_at": future}, headers=auth)
    r = admin_client.post("/admin/tests/t1/window/close-now", headers=auth)
    assert r.status_code == 200
    assert r.json()["opens_at"] is None
    assert admin_client.get("/exam/tests/t1/info").json()["window_state"] == "closed"


def test_server_time(admin_client: TestClient, auth: dict) -> None:
    r = admin_client.get("/admin/server-time", headers=auth)
    assert r.status_code == 200
    now = datetime.fromisoformat(r.json()["now"])
    assert now.tzinfo is not None
    assert abs(now - datetime.now(UTC)) < timedelta(seconds=30)


# --------------------------------------------------------------------------- #
# Migration a7b8c9d0e1f2 on a scratch DB.
# --------------------------------------------------------------------------- #
def _alembic(db: Path, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db.as_posix()}"}
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        timeout=120,
    )


def _columns(db: Path) -> set[str]:
    with sqlite3.connect(db) as conn:
        return {row[1] for row in conn.execute("PRAGMA table_info(tests)")}


def test_migration_adds_window_columns_and_round_trips(tmp_path: Path) -> None:
    db = tmp_path / "mig.db"
    _alembic(db, "upgrade", "f6a7b8c9d0e1")  # the schema before the window
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO tests (id, title, level, status, duration_minutes) "
            "VALUES ('t1', 'T', 'A1_MOVERS', 'published', 30)"
        )
        conn.execute(
            "INSERT INTO sections (id, test_id, position, stimulus) "
            "VALUES ('s1', 't1', 0, '{}')"
        )
    _alembic(db, "upgrade", "a7b8c9d0e1f2")
    assert {"opens_at", "closes_at", "late_start_grace_minutes"} <= _columns(db)
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT opens_at, closes_at, late_start_grace_minutes FROM tests"
        ).fetchone()
        assert row == (None, None, 0)  # existing tests stay "always open"
    _alembic(db, "downgrade", "f6a7b8c9d0e1")
    assert not {"opens_at", "closes_at", "late_start_grace_minutes"} & _columns(db)
    with sqlite3.connect(db) as conn:
        # No table rebuild: the FK children survived.
        assert conn.execute("SELECT COUNT(*) FROM sections").fetchone()[0] == 1
    _alembic(db, "upgrade", "head")
    assert {"opens_at", "closes_at", "late_start_grace_minutes"} <= _columns(db)
