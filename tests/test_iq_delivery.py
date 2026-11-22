"""Phase 17.2 / 17.4 / 17.6 backend: timed sections (strict order, per-section server
clocks, intro cap, study window, finish early, expiry, resume), the attempt deadline
math, the IQ block in the teacher API (recomputed after an override / birth-date
change), the birth-date API, the CSV IQ columns, and the regressions on a non-IQ test.

Synthetic fixture only (`tests/iq_fixture.py`): sections s1 60 s (mark_toggle),
s2 120 s (drawing), s3 study 20 s + 90 s (recall). Nothing under /private or /pdf.
"""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.admin import AdminService, build_admin_service
from app.analysis import MockAnalysisLLM
from app.content.service import ContentService
from app.content.storage import FilesystemStorage
from app.core.db import get_session
from app.delivery import DeliveryService, ExamWindow, SectionStateError
from app.delivery.sections import overall_seconds, plan_sections, resolve
from app.persistence import models as m
from app.persistence.repository import AttemptRepository, ContentRepository
from apps.api import admin as admin_api
from apps.api.main import app
from contracts import RosterEntry
from contracts import Test as ExamTest
from tests import iq_fixture as fx
from tests.test_iq_family import L, _answers, _zig

TID = "iq-demo"
S1, S2, S3 = f"{TID}-s1", f"{TID}-s2", f"{TID}-s3"
T0 = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)
CAP = 30  # intro cap used by the service-level tests
STUDY_TEXT = "cat dog sun"
PASSWORD = "test-teacher-pw"


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _import(session: Session, tmp_path: Path, data: dict[str, Any] | None = None) -> None:
    content = ContentService(ContentRepository(session), FilesystemStorage(tmp_path))
    content.import_test(data or fx.iq_test_json(TID), fx.ASSETS)
    assert content.publish(TID)


def _setup(
    session: Session,
    tmp_path: Path,
    *,
    birth: date | None = None,
    data: dict[str, Any] | None = None,
    cap: int = CAP,
) -> DeliveryService:
    _import(session, tmp_path, data)
    AttemptRepository(session).add_roster_entry(
        RosterEntry(id="iq-r1", test_id=TID, display_name="Ann", birth_date=birth)
    )
    return DeliveryService(
        ContentRepository(session), AttemptRepository(session), intro_cap_seconds=cap
    )


def _code(fn: Any) -> str:
    with pytest.raises(SectionStateError) as info:
        fn()
    return info.value.code


def _phases(svc: DeliveryService, attempt_id: str, now: datetime) -> dict[str, str]:
    return {s.section_id: s.phase for s in svc.section_states(attempt_id, now=now).sections}


# --------------------------------------------------------------------------- #
# Deadline math (+ regressions on tests without section clocks).
# --------------------------------------------------------------------------- #
def test_deadline_is_sum_of_section_clocks_plus_intro_allowance(
    session: Session, tmp_path: Path
) -> None:
    svc = _setup(session, tmp_path)
    attempt = svc.start("iq-r1", now=T0)
    # (30 + 60) + (30 + 120) + (30 + 20 + 90) — duration_minutes (10) is not used.
    assert attempt.deadline == _at(380)
    plans = plan_sections(ExamTest.model_validate(fx.iq_test_json(TID)))
    assert plans is not None and overall_seconds(plans, CAP) == 380
    assert [p.study_seconds for p in plans] == [0, 0, 20]


def test_window_close_caps_the_attempt_and_section_deadlines(
    session: Session, tmp_path: Path
) -> None:
    svc = _setup(session, tmp_path)
    window = ExamWindow(opens_at=_at(-3600), closes_at=_at(50))
    attempt = svc.start("iq-r1", window, now=T0)
    assert attempt.deadline == _at(50)
    begin = svc.begin_section(attempt.id, S1, now=_at(5))
    assert begin.section.deadline == _at(50)  # not 5 + 60


def test_non_timed_test_keeps_its_behaviour(session: Session) -> None:
    from tests.test_delivery_api import _test as english_test

    ContentRepository(session).add_test(english_test())
    attempts = AttemptRepository(session)
    attempts.add_roster_entry(RosterEntry(id="e1", test_id="t1", display_name="Bo"))
    svc = DeliveryService(ContentRepository(session), attempts, intro_cap_seconds=CAP)
    attempt = svc.start("e1", now=T0)
    assert attempt.deadline == T0 + timedelta(minutes=30)
    state = svc.section_states(attempt.id, now=_at(1))
    assert state.timed is False and state.sections == [] and state.current_section_id is None
    client = svc.client_test(attempt.id, now=_at(1))
    assert all(s.items and not s.items_withheld for s in client.sections)
    svc.save_answer(attempt.id, "q-gap", "house", now=_at(1))  # no begin needed
    assert _code(lambda: svc.begin_section(attempt.id, "sec-read", now=_at(2))) == "not_timed"
    assert session.query(m.AttemptSectionRow).count() == 0


def test_partly_timed_test_ignores_section_clocks(
    session: Session, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The editor refuses a partly timed test now (`editor.section_clock_errors`), but
    # a row persisted before that rule must still degrade safely: store it directly.
    data = fx.iq_test_json(TID)
    data["sections"][1]["time_limit_seconds"] = None  # s2 has no clock now
    ContentRepository(session).add_test(ExamTest.model_validate({**data, "status": "published"}))
    AttemptRepository(session).add_roster_entry(
        RosterEntry(id="iq-r1", test_id=TID, display_name="Ann")
    )
    svc = DeliveryService(
        ContentRepository(session), AttemptRepository(session), intro_cap_seconds=CAP
    )
    with caplog.at_level(logging.WARNING):
        attempt = svc.start("iq-r1", now=T0)
    assert attempt.deadline == T0 + timedelta(minutes=10)
    assert "section clocks ignored" in caplog.text
    svc.save_answer(attempt.id, f"{TID}-r1", "1", now=_at(1))
    assert svc.section_states(attempt.id, now=_at(1)).timed is False


# --------------------------------------------------------------------------- #
# Order, begin, answer, expiry, finish early.
# --------------------------------------------------------------------------- #
def test_sections_run_in_order_and_items_wait_for_begin(
    session: Session, tmp_path: Path
) -> None:
    svc = _setup(session, tmp_path)
    attempt = svc.start("iq-r1", now=T0)
    state = svc.section_states(attempt.id, now=_at(1))
    assert state.timed and state.current_section_id == S1 and state.intro_max_seconds == CAP
    s1 = state.sections[0]
    assert (s1.phase, s1.intro_ends_at, s1.remaining_seconds) == ("intro", _at(30), 29)
    assert [s.phase for s in state.sections[1:]] == ["pending", "pending"]
    client = svc.client_test(attempt.id, now=_at(1))
    assert all(s.items == [] and s.items_withheld for s in client.sections)
    assert STUDY_TEXT not in client.model_dump_json()
    a = attempt.id
    assert _code(lambda: svc.save_answer(a, f"{TID}-r1", "1", now=_at(2))) == "section_not_begun"
    assert _code(lambda: svc.save_answer(a, f"{TID}-m1", 0, now=_at(2))) == "section_not_current"
    assert _code(lambda: svc.begin_section(a, S2, now=_at(2))) == "section_not_current"
    assert _code(lambda: svc.finish_section(a, S1, now=_at(2))) == "section_not_begun"
    assert _code(lambda: svc.section_items(a, S1, now=_at(2))) == "section_not_begun"
    assert _code(lambda: svc.serve_item(a, f"{TID}-r1", now=_at(2))) == "section_not_begun"


def test_begin_answer_then_expiry_rejects_late_answers(
    session: Session, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    svc = _setup(session, tmp_path)
    a = svc.start("iq-r1", now=T0).id
    with caplog.at_level(logging.INFO):
        begin = svc.begin_section(a, S1, now=_at(10))
    assert begin.study is None and begin.section.phase == "answering"
    assert begin.section.started_by == "student" and begin.section.deadline == _at(70)
    assert "section begin" in caplog.text
    svc.save_answer(a, f"{TID}-r1", "1", now=_at(11))
    again = svc.begin_section(a, S1, now=_at(12))  # idempotent (a refresh)
    assert again.section.started_at == _at(10)
    client = svc.client_test(a, now=_at(12))
    assert [len(s.items) for s in client.sections] == [6, 0, 0]
    assert len(svc.section_items(a, S1, now=_at(12)).items) == 6
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        code = _code(lambda: svc.save_answer(a, f"{TID}-r3", "1", now=_at(70)))
    assert code == "section_expired"
    assert "answer REJECTED" in caplog.text and "section_expired" in caplog.text
    state = svc.section_states(a, now=_at(71))
    s1, s2 = state.sections[0], state.sections[1]
    assert (s1.phase, s1.close_reason, s1.closed_at) == ("closed", "expired", _at(70))
    assert (s2.phase, s2.intro_started_at) == ("intro", _at(70))
    assert state.current_section_id == S2
    assert _code(lambda: svc.begin_section(a, S1, now=_at(72))) == "section_expired"
    assert svc.get_saved_answers(a) == {f"{TID}-r1": "1"}  # the late one was not saved


def test_finish_early_closes_and_starts_the_next_intro(
    session: Session, tmp_path: Path
) -> None:
    svc = _setup(session, tmp_path)
    a = svc.start("iq-r1", now=T0).id
    svc.begin_section(a, S1, now=_at(5))
    state = svc.finish_section(a, S1, now=_at(20))
    s1, s2 = state.sections[0], state.sections[1]
    assert (s1.phase, s1.close_reason, s1.closed_at) == ("closed", "finished", _at(20))
    assert (s2.phase, s2.intro_started_at, state.current_section_id) == ("intro", _at(20), S2)
    assert _code(lambda: svc.save_answer(a, f"{TID}-r1", "1", now=_at(21))) == "section_closed"
    assert _code(lambda: svc.finish_section(a, S1, now=_at(21))) == "section_closed"
    assert _code(lambda: svc.begin_section(a, S1, now=_at(21))) == "section_closed"
    # Closed sections stay readable (keyless), never answerable.
    assert len(svc.section_items(a, S1, now=_at(22)).items) == 6


def test_intro_cap_begins_the_section_by_itself(session: Session, tmp_path: Path) -> None:
    svc = _setup(session, tmp_path)
    a = svc.start("iq-r1", now=T0).id
    s1 = svc.section_states(a, now=_at(31)).sections[0]
    assert (s1.phase, s1.started_by, s1.started_at, s1.deadline) == (
        "answering",
        "intro_cap",
        _at(30),
        _at(90),
    )
    svc.save_answer(a, f"{TID}-r1", "1", now=_at(32))


# --------------------------------------------------------------------------- #
# Study window (memory subtest).
# --------------------------------------------------------------------------- #
def _to_s3(svc: DeliveryService, a: str) -> None:
    svc.begin_section(a, S1, now=_at(0))
    svc.finish_section(a, S1, now=_at(1))
    svc.begin_section(a, S2, now=_at(2))
    svc.finish_section(a, S2, now=_at(3))


def test_study_window_serves_the_stimulus_only_inside_it(
    session: Session, tmp_path: Path
) -> None:
    svc = _setup(session, tmp_path)
    a = svc.start("iq-r1", now=T0).id
    _to_s3(svc, a)
    assert _code(lambda: svc.section_study(a, S3, now=_at(3.5))) == "study_not_open"
    begin = svc.begin_section(a, S3, now=_at(4))
    assert begin.section.phase == "study" and begin.section.study_ends_at == _at(24)
    assert begin.section.deadline == _at(114)  # 4 + 20 study + 90 recall
    assert begin.study is not None and begin.study.text == STUDY_TEXT
    assert "items" not in begin.model_dump()  # never alongside the recall items
    assert svc.section_study(a, S3, now=_at(10)).text == STUDY_TEXT  # reload in window
    # Recall items: not served, not answerable during the study window.
    assert _code(lambda: svc.section_items(a, S3, now=_at(10))) == "section_study"
    assert _code(lambda: svc.save_answer(a, f"{TID}-m1", 0, now=_at(10))) == "section_study"
    assert _code(lambda: svc.serve_item(a, f"{TID}-m1", now=_at(10))) == "section_study"
    bulk = svc.client_test(a, now=_at(10))
    assert bulk.sections[2].items_withheld and STUDY_TEXT not in bulk.model_dump_json()
    assert bulk.sections[2].study is not None and bulk.sections[2].study.study_seconds == 20
    # After the window: the stimulus is gone for good, the recall opens.
    assert _code(lambda: svc.section_study(a, S3, now=_at(24))) == "study_not_open"
    assert [i.id for i in svc.section_items(a, S3, now=_at(24)).items] == [
        f"{TID}-m1",
        f"{TID}-m2",
    ]
    svc.save_answer(a, f"{TID}-m1", 0, now=_at(25))
    bulk = svc.client_test(a, now=_at(25))
    assert len(bulk.sections[2].items) == 2 and STUDY_TEXT not in bulk.model_dump_json()
    rows = AttemptRepository(session).list_attempt_sections(a)
    assert rows[S3].answering_started_at == _at(24) and rows[S3].study_started_at == _at(4)


def test_finish_during_study_is_allowed_and_closes_it(
    session: Session, tmp_path: Path
) -> None:
    svc = _setup(session, tmp_path)
    a = svc.start("iq-r1", now=T0).id
    _to_s3(svc, a)
    svc.begin_section(a, S3, now=_at(4))
    state = svc.finish_section(a, S3, now=_at(6))
    assert state.sections[2].phase == "closed" and state.current_section_id is None
    assert _code(lambda: svc.section_study(a, S3, now=_at(7))) == "study_not_open"


# --------------------------------------------------------------------------- #
# Server-authoritative: abandoned tab, attempt expiry, resume after refresh.
# --------------------------------------------------------------------------- #
def test_abandoned_attempt_runs_every_section_clock(session: Session, tmp_path: Path) -> None:
    svc = _setup(session, tmp_path)
    a = svc.start("iq-r1", now=T0).id
    state = svc.section_states(a, now=_at(379))
    s1, s2, s3 = state.sections
    assert (s1.closed_at, s1.close_reason) == (_at(90), "expired")
    assert (s2.intro_started_at, s2.started_at, s2.closed_at) == (_at(90), _at(120), _at(240))
    assert (s3.intro_started_at, s3.started_at, s3.study_ends_at) == (_at(240), _at(270), _at(290))
    assert (s3.phase, s3.deadline) == ("answering", _at(380))
    rows = AttemptRepository(session).list_attempt_sections(a)
    assert set(rows) == {S1, S2, S3} and rows[S3].answering_started_at == _at(290)
    # The last section ends exactly at the attempt deadline; then the attempt expires.
    assert svc.get_state(a, now=_at(380)).just_expired
    assert set(_phases(svc, a, _at(381)).values()) == {"closed"}


def test_resolve_is_pure_and_idempotent(session: Session, tmp_path: Path) -> None:
    plans = plan_sections(ExamTest.model_validate(fx.iq_test_json(TID)))
    assert plans is not None
    kw = dict(
        attempt_id="x", attempt_started_at=T0, attempt_deadline=_at(380), intro_cap=CAP
    )
    first = resolve(plans, {}, now=_at(200), **kw)  # type: ignore[arg-type]
    again = resolve(plans, {s.section_id: s for s in first}, now=_at(200), **kw)  # type: ignore[arg-type]
    assert first == again and [s.section_id for s in first] == [S1, S2]


def test_resume_after_refresh_keeps_the_section_clock(session: Session, tmp_path: Path) -> None:
    svc = _setup(session, tmp_path)
    attempt = svc.start("iq-r1", now=T0)
    svc.begin_section(attempt.id, S1, now=_at(5))
    svc.save_answer(attempt.id, f"{TID}-r1", "1", now=_at(6))
    session.commit()
    session.expire_all()
    fresh = DeliveryService(
        ContentRepository(session), AttemptRepository(session), intro_cap_seconds=CAP
    )
    resumed = fresh.start("iq-r1", now=_at(20))
    assert resumed.id == attempt.id and resumed.deadline == attempt.deadline
    s1 = fresh.section_states(resumed.id, now=_at(20)).sections[0]
    assert (s1.phase, s1.started_at, s1.deadline, s1.remaining_seconds) == (
        "answering",
        _at(5),
        _at(65),
        45,
    )
    assert fresh.get_saved_answers(resumed.id) == {f"{TID}-r1": "1"}
    assert len(fresh.client_test(resumed.id, now=_at(20)).sections[0].items) == 6


def test_reset_drops_the_section_clocks(session: Session, tmp_path: Path) -> None:
    svc = _setup(session, tmp_path)
    a = svc.start("iq-r1", now=T0).id
    svc.begin_section(a, S1, now=_at(1))
    assert AttemptRepository(session).delete_attempt(a)
    assert session.query(m.AttemptSectionRow).count() == 0


# --------------------------------------------------------------------------- #
# HTTP surface (student): codes, headers, token, nothing teacher-only leaks.
# --------------------------------------------------------------------------- #
@pytest.fixture
def exam(session: Session, tmp_path: Path) -> Iterator[tuple[TestClient, str]]:
    _import(session, tmp_path)
    attempts = AttemptRepository(session)
    attempts.add_roster_entry(
        RosterEntry(id="iq-r1", test_id=TID, display_name="Ann", birth_date=date(2009, 9, 9))
    )
    session.commit()
    entry = attempts.get_roster_entry("iq-r1")
    assert entry is not None and entry.pin
    app.dependency_overrides[get_session] = lambda: session
    try:
        yield TestClient(app), entry.pin
    finally:
        app.dependency_overrides.clear()


def _shift(session: Session, section_id: str, **values: datetime) -> None:
    """Move a stored section clock into the past (instead of sleeping)."""
    session.execute(
        update(m.AttemptSectionRow)
        .where(m.AttemptSectionRow.section_id == section_id)
        .values(**values)
    )
    session.commit()


def test_http_timed_section_flow(session: Session, exam: tuple[TestClient, str]) -> None:
    api, pin = exam
    start = api.post("/exam/roster/iq-r1/start", json={"pin": pin})
    assert start.status_code == 200
    a, token = start.json()["attempt_id"], start.json()["token"]
    h = {"X-Attempt-Token": token}
    base = f"/exam/attempts/{a}"
    assert api.get(f"{base}/sections").status_code == 401  # token required
    state = api.get(f"{base}/sections", headers=h).json()
    assert state["timed"] and state["current_section_id"] == S1
    assert state["sections"][0]["phase"] == "intro" and state["intro_max_seconds"] == 300
    bulk = api.get(f"{base}/test", headers=h)
    assert all(s["items_withheld"] and s["items"] == [] for s in bulk.json()["sections"])
    for leak in (STUDY_TEXT, "norm", "birth", "2009", "target", '"correct"'):
        assert leak not in bulk.text and leak not in start.text

    late = api.put(f"{base}/answers/{TID}-r1", json={"response": "1"}, headers=h)
    assert late.status_code == 409 and late.headers["X-Section-Error"] == "section_not_begun"
    assert late.json()["code"] == "section_not_begun" and late.json()["section_id"] == S1
    skip = api.post(f"{base}/sections/{S2}/begin", headers=h)
    assert skip.status_code == 409 and skip.json()["code"] == "section_not_current"

    begin = api.post(f"{base}/sections/{S1}/begin", headers=h)
    assert begin.status_code == 200 and begin.json()["section"]["phase"] == "answering"
    assert begin.json()["study"] is None
    items = api.get(f"{base}/sections/{S1}/items", headers=h)
    assert items.status_code == 200 and len(items.json()["items"]) == 6
    assert '"correct"' not in items.text
    assert api.put(f"{base}/answers/{TID}-r1", json={"response": "1"}, headers=h).status_code == 204
    assert api.get(f"{base}/items/{TID}-r1", headers=h).status_code == 200
    fin = api.post(f"{base}/sections/{S1}/finish", headers=h)
    assert fin.status_code == 200 and fin.json()["current_section_id"] == S2
    closed = api.put(f"{base}/answers/{TID}-r2", json={"response": "1"}, headers=h)
    assert closed.status_code == 409 and closed.json()["code"] == "section_closed"

    assert api.post(f"{base}/sections/{S2}/begin", headers=h).status_code == 200
    assert api.post(f"{base}/sections/{S2}/finish", headers=h).status_code == 200
    early = api.get(f"{base}/sections/{S3}/study", headers=h)
    assert early.status_code == 409 and early.json()["code"] == "study_not_open"
    b3 = api.post(f"{base}/sections/{S3}/begin", headers=h)
    assert b3.status_code == 200 and b3.json()["section"]["phase"] == "study"
    assert b3.json()["study"]["text"] == STUDY_TEXT and "items" not in b3.json()
    assert api.get(f"{base}/sections/{S3}/study", headers=h).json()["text"] == STUDY_TEXT
    recall = api.get(f"{base}/sections/{S3}/items", headers=h)
    assert recall.status_code == 409 and recall.json()["code"] == "section_study"
    during = api.put(f"{base}/answers/{TID}-m1", json={"response": 0}, headers=h)
    assert during.status_code == 409 and during.json()["code"] == "section_study"
    assert STUDY_TEXT not in api.get(f"{base}/test", headers=h).text

    past = datetime.now(UTC) - timedelta(seconds=1)
    _shift(session, S3, study_ends_at=past)  # the study window is over
    assert api.get(f"{base}/sections/{S3}/study", headers=h).status_code == 409
    assert len(api.get(f"{base}/sections/{S3}/items", headers=h).json()["items"]) == 2
    assert api.put(f"{base}/answers/{TID}-m1", json={"response": 0}, headers=h).status_code == 204
    _shift(session, S3, deadline=past)  # its time is over too
    expired = api.put(f"{base}/answers/{TID}-m2", json={"response": 1}, headers=h)
    assert expired.status_code == 409 and expired.json()["code"] == "section_expired"
    assert api.get(f"{base}/answers", headers=h).json() == {f"{TID}-r1": "1", f"{TID}-m1": 0}
    sub = api.post(f"{base}/submit", headers=h)
    assert sub.status_code == 200 and sub.json()["status"] == "submitted"


def test_http_sections_on_a_non_timed_test(session: Session) -> None:
    from tests.test_delivery_api import _test as english_test

    ContentRepository(session).add_test(english_test())
    attempts = AttemptRepository(session)
    attempts.add_roster_entry(RosterEntry(id="e1", test_id="t1", display_name="Bo"))
    session.commit()
    pin = attempts.get_roster_entry("e1").pin  # type: ignore[union-attr]
    app.dependency_overrides[get_session] = lambda: session
    try:
        api = TestClient(app)
        start = api.post("/exam/roster/e1/start", json={"pin": pin}).json()
        h = {"X-Attempt-Token": start["token"]}
        state = api.get(f"/exam/attempts/{start['attempt_id']}/sections", headers=h).json()
        assert state["timed"] is False and state["sections"] == []
        begin = api.post(f"/exam/attempts/{start['attempt_id']}/sections/sec-read/begin", headers=h)
        assert begin.status_code == 409 and begin.json()["code"] == "not_timed"
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Teacher API: IQ block (recomputed on read), drawings, CSV, birth date.
# --------------------------------------------------------------------------- #
def _finish_attempt(session: Session, svc: DeliveryService, answers: dict[str, Any]) -> str:
    """Take the whole synthetic test through its sections; returns the attempt id."""
    attempt = svc.start("iq-r1", now=T0)
    now = T0
    for section in ExamTest.model_validate(fx.iq_test_json(TID)).sections:
        svc.begin_section(attempt.id, section.id, now=now)
        if section.study is not None:
            now += timedelta(seconds=section.study.study_seconds)
        for item in section.items:
            if item.id in answers:
                svc.save_answer(attempt.id, item.id, answers[item.id], now=now)
        svc.finish_section(attempt.id, section.id, now=now)
        now += timedelta(seconds=1)
    svc.submit(attempt.id, now=now)
    session.commit()
    return attempt.id


@pytest.fixture
def teacher(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, dict[str, str], str]]:
    monkeypatch.setattr(admin_api.settings, "teacher_password", PASSWORD)
    monkeypatch.setattr(admin_api.settings, "environment", "test")
    admin_api.login_limiter.reset()
    svc = _setup(session, tmp_path, birth=date(2010, 1, 1))  # 16 at the start
    hit = fx.picture_payload([_zig(0.65, 0.65, 0.85, 0.85)])
    borderline = fx.square_payload([L[0], [(0.5, 0.5), (0.5, 0.80)]])  # -> needs review
    attempt_id = _finish_attempt(session, svc, _answers(hit, borderline))
    app.dependency_overrides[get_session] = lambda: session

    def _svc() -> Iterator[AdminService]:
        yield build_admin_service(
            session, FilesystemStorage(tmp_path), analysis_llm=MockAnalysisLLM()
        )

    app.dependency_overrides[admin_api.get_admin_service] = _svc
    try:
        api = TestClient(app, client=("127.0.0.1", 50000))
        token = api.post("/admin/login", json={"password": PASSWORD}).json()["token"]
        yield api, {"Authorization": f"Bearer {token}"}, attempt_id
    finally:
        app.dependency_overrides.clear()


def _csv(api: TestClient, h: dict[str, str]) -> list[dict[str, str]]:
    resp = api.get(f"/admin/tests/{TID}/results.csv", headers=h)
    assert resp.status_code == 200
    return list(csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig"))))


def test_teacher_iq_block_drawings_override_and_csv(
    teacher: tuple[TestClient, dict[str, str], str],
) -> None:
    api, h, a = teacher
    result = api.get(f"/admin/results/{a}", headers=h).json()
    iq = result["iq"]
    # raw 2 / 1 / 1 -> scaled 7 / 8 / 6; I = 13 -> 112, II = 8 -> 92; (112 + 92) / 2.
    assert iq["age"] == 16 and iq["age_group"] == "teen"
    assert [s["raw"] for s in iq["sections"]] == [2, 1, 1]
    assert [c["score"] for c in iq["composites"]] == [112, 92]
    assert iq["overall"] == 102 and iq["bracket"] == "Average"
    rows = {r["item_id"]: r for r in result["responses"]}
    d2 = rows[f"{TID}-d2"]
    assert d2["needs_review"] and d2["awarded"] == 0 and "уверенность" in d2["note"]
    drawing = d2["drawing"]
    assert drawing["mode"] == "lines" and drawing["asset_id"] == "iq-square.png"
    assert drawing["square"] == fx.SQUARE and drawing["w"] == fx.W and drawing["h"] == fx.H
    assert len(drawing["strokes"]) == 2 and drawing["target"]["mode"] == "lines"
    assert rows[f"{TID}-d1"]["drawing"]["mode"] == "region"
    assert rows[f"{TID}-r2"]["student_answer"] == "отмечено"
    assert rows[f"{TID}-r2"]["is_correct"] is False  # a false mark
    assert rows[f"{TID}-r4"]["is_correct"] is True  # rightly left unmarked

    # The teacher accepts the drawing (existing ManualGrade path) -> IQ recomputed.
    fixed = api.put(f"/admin/results/{a}/grades/{TID}-d2", json={"awarded": 1}, headers=h)
    assert fixed.status_code == 200
    iq = fixed.json()["iq"]
    assert iq["sections"][1]["raw"] == 2 and iq["composites"][1]["score"] == 108
    assert iq["overall"] == 110 and iq["bracket"] == "Above average"
    report = api.get(f"/admin/results/{a}/report", headers=h).json()
    assert report["iq"]["overall"] == 110
    assert "birth" not in str(report)

    # CSV: no report yet -> empty IQ cells; after the finalize the IQ columns fill.
    row = _csv(api, h)[0]
    assert row["iq_overall"] == "" and "iq_s3_scaled" in row
    assert api.post(f"/admin/results/{a}/regrade", headers=h).status_code == 200
    row = _csv(api, h)[0]
    assert (row["iq_s1_raw"], row["iq_s1_scaled"], row["iq_s2_raw"], row["iq_s2_scaled"]) == (
        "2",
        "7",
        "2",
        "12",
    )
    assert (row["iq_I_sum"], row["iq_I_score"], row["iq_II_sum"], row["iq_II_score"]) == (
        "13",
        "112",
        "12",
        "108",
    )
    assert (row["iq_overall"], row["iq_bracket"], row["iq_age"]) == ("110", "Above average", "16")
    assert row["parts"] == "все части"


def test_birth_date_api_and_iq_without_it(
    teacher: tuple[TestClient, dict[str, str], str],
) -> None:
    api, h, a = teacher
    roster = api.get(f"/admin/tests/{TID}/roster", headers=h).json()
    assert roster[0]["birth_date"] == "2010-01-01"
    cleared = api.put("/admin/roster/iq-r1/birth-date", json={"birth_date": None}, headers=h)
    assert cleared.status_code == 200 and cleared.json()["birth_date"] is None
    iq = api.get(f"/admin/results/{a}", headers=h).json()["iq"]
    assert iq["age"] is None and iq["overall"] is None and "no_birth_date" in iq["notes"]
    assert [s["scaled"] for s in iq["sections"]] == [7, 8, 6]  # raw + scaled still shown
    future = api.put("/admin/roster/iq-r1/birth-date", json={"birth_date": "2999-01-01"}, headers=h)
    assert future.status_code == 400
    missing = api.put("/admin/roster/nope/birth-date", json={"birth_date": None}, headers=h)
    assert missing.status_code == 404
    adult = api.put("/admin/roster/iq-r1/birth-date", json={"birth_date": "1990-05-05"}, headers=h)
    assert adult.status_code == 200
    iq = api.get(f"/admin/results/{a}", headers=h).json()["iq"]
    assert iq["age"] == 36 and iq["age_group"] == "adult"
    # Add one / many with a birth date.
    one = api.post(
        f"/admin/tests/{TID}/roster",
        json={"display_name": "Bob", "birth_date": "2008-03-04"},
        headers=h,
    )
    assert one.status_code == 200 and one.json()["birth_date"] == "2008-03-04"
    bulk = api.post(
        f"/admin/tests/{TID}/roster/bulk",
        json={"names": ["Cy", "Di"], "birth_dates": {"Cy": "2007-07-07"}},
        headers=h,
    )
    assert bulk.status_code == 200
    added = {e["display_name"]: e["birth_date"] for e in bulk.json()["added"]}
    assert added == {"Cy": "2007-07-07", "Di": None}
    bad = api.post(
        f"/admin/tests/{TID}/roster/bulk",
        json={"names": ["Ed"], "birth_dates": {"Ed": "2999-01-01"}},
        headers=h,
    )
    assert bad.status_code == 400
    # The student side never shows a birth date.
    student = api.get(f"/exam/tests/{TID}/roster")
    assert student.status_code == 200 and "birth" not in student.text and "2008" not in student.text


def test_non_iq_results_and_csv_have_no_iq(session: Session, tmp_path: Path) -> None:
    from tests.test_delivery_api import _test as english_test

    ContentRepository(session).add_test(english_test())
    attempts = AttemptRepository(session)
    attempts.add_roster_entry(RosterEntry(id="e1", test_id="t1", display_name="Bo"))
    delivery = DeliveryService(ContentRepository(session), attempts)
    attempt = delivery.start("e1")
    delivery.submit(attempt.id)
    admin = build_admin_service(session, FilesystemStorage(tmp_path), analysis_llm=None)
    assert admin.attempt_result(attempt.id).iq is None
    assert admin.student_report(attempt.id).iq is None
    admin.regrade(attempt.id)
    header = admin.results_csv("t1").splitlines()[0]
    assert "iq_" not in header and header.endswith("parts")


# --------------------------------------------------------------------------- #
# Migration f2a3b4c5d6e7 on a scratch DB.
# --------------------------------------------------------------------------- #
def test_migration_adds_and_drops_attempt_sections(tmp_path: Path) -> None:
    import sqlite3

    from tests.test_iq_family import _alembic, _columns

    db = tmp_path / "mig.db"
    _alembic(db, "upgrade", "head")
    assert {"intro_started_at", "started_at", "study_ends_at", "deadline", "closed_at"} <= (
        _columns(db, "attempt_sections")
    )
    _alembic(db, "downgrade", "e1f2a3b4c5d6")
    with sqlite3.connect(db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "attempt_sections" not in tables
    _alembic(db, "upgrade", "head")
    assert "close_reason" in _columns(db, "attempt_sections")
