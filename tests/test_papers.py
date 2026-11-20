"""Papers: a test taken WHOLE (default) or only the papers the teacher assigned.

Covers the pure derivation (`contracts.papers`: paper per level + skill, the
proportional minute split that always sums to the duration, explicit minutes,
the teacher's choice validated), the editor rules for `Test.paper_minutes` (sum ==
duration, timing meta allowed on a locked test, JSON export / import round trip),
the roster API (assign on add / bulk / per student / whole class; unknown paper
422; a paper the test lacks 422; a change after the start 409; reset unlocks it),
delivery (only the chosen papers' sections are served, in authored order; the
deadline is the sum of their minutes, still capped by the window; an unserved item
or track is refused; no answer key on the wire), grading and results (max score,
per-skill report, choose-one writing, Cambridge scale, CSV / list / detail / print
labels only over the served papers; the timeline notes the served parts) and the
migration up / down on a scratch DB.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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
    paper_minutes_errors,
)
from app.core.db import get_session
from app.delivery import DeliveryService, NotFoundError, UnknownTrackError
from app.grading.service import GradingService
from app.persistence.repository import AttemptRepository, ContentRepository
from apps.api import admin as admin_api
from apps.api import assets as assets_api
from apps.api.main import app
from contracts import (
    AudioAssetStimulus,
    GapFillItem,
    MatchingItem,
    MatchingPoolStimulus,
    OpenWritingItem,
    PassageTextStimulus,
    PoolOption,
    RosterEntry,
    Section,
    SingleChoiceItem,
    TextOption,
)
from contracts import Test as ExamTest
from contracts.papers import (
    OFFICIAL_PAPER_MINUTES,
    PaperSelectionError,
    normalize_papers,
    paper_for,
    paper_minutes,
    papers_label,
    papers_of,
    partial_label,
    served_minutes,
    served_test,
    split_minutes,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "papers-teacher-pw"
LOOPBACK = ("127.0.0.1", 50000)


# --------------------------------------------------------------------------- #
# Fixtures: an A2 Key test (Reading & Writing + Listening) and a B2 First test.
# --------------------------------------------------------------------------- #
def _sc(iid: str, correct: str = "B") -> SingleChoiceItem:
    return SingleChoiceItem(
        id=iid,
        prompt=f"Pick {iid}",
        options=[TextOption(key="A", text="alpha"), TextOption(key="B", text="bravo")],
        correct=correct,
    )


def a2_test(**update: object) -> ExamTest:
    test = ExamTest(
        id="pa",
        title="A2 Key — papers",
        level="A2_KEY",
        status="published",
        duration_minutes=95,
        sections=[
            Section(
                id="pa-r1",
                title="Reading Part 1",
                skill="reading",
                stimulus=PassageTextStimulus(text="Read this."),
                items=[_sc("pa-q1"), GapFillItem(id="pa-q2", prompt="Fill", accepted=["house"])],
            ),
            Section(
                id="pa-w1",
                title="Writing Part 6",
                skill="writing",
                stimulus=PassageTextStimulus(text=""),
                items=[OpenWritingItem(id="pa-q3", prompt="Write", word_min=25, rubric="r")],
            ),
            Section(
                id="pa-l1",
                title="Listening Part 1",
                skill="listening",
                stimulus=AudioAssetStimulus(asset_id="pa-l1.mp3"),
                items=[_sc("pa-q4"), _sc("pa-q5", "A")],
            ),
            Section(
                id="pa-l2",
                title="Listening Part 2",
                skill="listening",
                stimulus=AudioAssetStimulus(asset_id="pa-l2.mp3"),
                items=[GapFillItem(id="pa-q6", prompt="Name", accepted=["Tom"])],
            ),
        ],
    )
    return ExamTest.model_validate({**test.model_dump(), **update})


def b2_test() -> ExamTest:
    return ExamTest(
        id="pb",
        title="B2 First — papers",
        level="B2_FIRST",
        status="published",
        duration_minutes=195,
        sections=[
            Section(
                id="pb-r1",
                skill="reading",
                stimulus=PassageTextStimulus(text="Read."),
                items=[_sc("pb-q1")],
            ),
            Section(
                id="pb-u2",
                skill="reading",
                reported_skill="use_of_english",
                stimulus=PassageTextStimulus(text="Cloze."),
                items=[GapFillItem(id="pb-q2", prompt="Fill", accepted=["since"])],
            ),
            Section(
                id="pb-w1",
                skill="writing",
                stimulus=PassageTextStimulus(text=""),
                items=[
                    OpenWritingItem(
                        id="pb-w1a", prompt="Essay", word_min=140, rubric="r", points=20
                    )
                ],
            ),
            Section(
                id="pb-w2",
                skill="writing",
                stimulus=PassageTextStimulus(text=""),
                items=[
                    OpenWritingItem(
                        id=f"pb-w2{x}",
                        prompt=f"Task {x}",
                        word_min=140,
                        rubric="r",
                        optional=True,
                        points=20,
                    )
                    for x in "abc"
                ],
            ),
            Section(
                id="pb-l1",
                skill="listening",
                stimulus=MatchingPoolStimulus(
                    options=[PoolOption(key="A", text="a"), PoolOption(key="B", text="b")],
                    audio=AudioAssetStimulus(asset_id="pb-l1.mp3"),
                ),
                items=[MatchingItem(id="pb-q9", prompt="Speaker 1", correct="A")],
            ),
        ],
    )


def _section(sid: str, skill: str | None) -> Section:
    return Section(
        id=sid,
        skill=skill,  # type: ignore[arg-type]
        stimulus=PassageTextStimulus(text="x"),
        items=[GapFillItem(id=f"{sid}-q", prompt="p", accepted=["a"])],
    )


def _level_test(level: str, skills: list[str | None], duration: int = 120) -> ExamTest:
    return ExamTest(
        id="lv",
        title="T",
        level=level,  # type: ignore[arg-type]
        duration_minutes=duration,
        sections=[_section(f"lv-s{i}", s) for i, s in enumerate(skills)],
    )


# --------------------------------------------------------------------------- #
# Pure derivation.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("PRE_A1_STARTERS", ["listening", "reading_writing"]),
        ("A1_MOVERS", ["listening", "reading_writing"]),
        ("A2_FLYERS", ["listening", "reading_writing"]),
        ("A2_KEY", ["listening", "reading_writing"]),
        ("B1_PRELIMINARY", ["listening", "reading", "writing"]),
        ("B2_FIRST", ["listening", "reading_use_of_english", "writing"]),
        ("C1_ADVANCED", ["listening", "reading_use_of_english", "writing"]),
        ("C2_PROFICIENCY", ["listening", "reading_use_of_english", "writing"]),
    ],
)
def test_paper_derivation_per_level(level: str, expected: list[str]) -> None:
    test = _level_test(level, ["listening", "reading", "writing"])
    # Distinct papers in order of first appearance (YLE / A2: reading + writing = one).
    assert papers_of(test) == expected
    assert set(OFFICIAL_PAPER_MINUTES[level]) == set(expected)


def test_paper_of_skillless_and_use_of_english_sections() -> None:
    # No skill counts as reading; Use of English stays in the R&UoE paper.
    assert paper_for("B1_PRELIMINARY", None) == "reading"
    assert paper_for("A2_KEY", None) == "reading_writing"
    assert paper_for("C1_ADVANCED", None) == "reading_use_of_english"
    assert papers_of(b2_test()) == ["reading_use_of_english", "writing", "listening"]


@pytest.mark.parametrize(
    ("total", "weights", "expected"),
    [
        (95, [60, 35], [60, 35]),
        (90, [60, 35], [57, 33]),  # 56.84 / 33.16 -> the larger remainder gets the minute
        (7, [1, 1, 1], [3, 2, 2]),  # ties -> the earlier part
        (100, [45, 45, 36], [36, 36, 28]),
        (3, [100, 1, 1], [1, 1, 1]),  # every part keeps at least one minute
        (10, [0, 0], [5, 5]),  # degenerate weights -> equal split
    ],
)
def test_split_minutes_examples(total: int, weights: list[int], expected: list[int]) -> None:
    assert split_minutes(total, weights) == expected
    assert sum(expected) == total


def test_split_minutes_always_sums_exactly() -> None:
    rng = random.Random(16)
    for _ in range(2000):
        n = rng.randint(1, 4)
        weights = [rng.randint(1, 120) for _ in range(n)]
        total = rng.randint(1, 300)
        parts = split_minutes(total, weights)
        assert sum(parts) == total and all(isinstance(p, int) for p in parts)
        if total >= n:
            assert min(parts) >= 1


@pytest.mark.parametrize("level", list(OFFICIAL_PAPER_MINUTES))
def test_proportional_minutes_sum_to_the_duration(level: str) -> None:
    for duration in (1, 7, 59, 95, 120, 199, 225):
        test = _level_test(level, ["reading", "writing", "listening"], duration)
        minutes = paper_minutes(test)
        assert sum(minutes.values()) == duration
        assert list(minutes) == papers_of(test)


def test_official_full_time_is_the_official_split() -> None:
    assert paper_minutes(a2_test()) == {"reading_writing": 60, "listening": 35}
    assert paper_minutes(b2_test()) == {
        "reading_use_of_english": 75,
        "writing": 80,
        "listening": 40,
    }


def test_explicit_minutes_win_when_valid_else_proportional() -> None:
    t = a2_test(paper_minutes={"reading_writing": 70, "listening": 25})
    assert paper_minutes(t) == {"reading_writing": 70, "listening": 25}
    # Stale explicit minutes (no longer summing to the duration) are ignored.
    stale = a2_test(paper_minutes={"reading_writing": 70, "listening": 30})
    assert paper_minutes(stale) == {"reading_writing": 60, "listening": 35}
    with pytest.raises(ValidationError):
        a2_test(paper_minutes={"speaking": 10})
    with pytest.raises(ValidationError):
        a2_test(paper_minutes={"listening": 0, "reading_writing": 95})


def test_normalize_papers_validates_the_choice() -> None:
    test = a2_test()
    assert normalize_papers(test, None) is None
    assert normalize_papers(test, ["listening"]) == ["listening"]
    assert normalize_papers(test, ["listening", "listening"]) == ["listening"]
    # Every paper == the whole test.
    assert normalize_papers(test, ["listening", "reading_writing"]) is None
    with pytest.raises(PaperSelectionError, match="хотя бы одну"):
        normalize_papers(test, [])
    with pytest.raises(PaperSelectionError, match="нет части «Writing»"):
        normalize_papers(test, ["writing"])
    with pytest.raises(PaperSelectionError, match="Неизвестная"):
        normalize_papers(test, ["speaking"])


def test_served_test_keeps_order_and_sums_minutes() -> None:
    test = a2_test()
    listening = served_test(test, ["listening"])
    assert [s.id for s in listening.sections] == ["pa-l1", "pa-l2"]
    assert listening.duration_minutes == 35 == served_minutes(test, ["listening"])
    assert listening.paper_minutes == {"listening": 35}
    # Whole test: unchanged object, the full duration.
    assert served_test(test, None) is test
    assert served_test(test, ["reading_writing", "listening"]) is test
    assert served_minutes(test, None) == 95
    assert papers_label(["reading_writing", "listening"]) == "Reading & Writing + Listening"
    assert partial_label(test, ["listening"]) == "Частично: Listening"
    assert partial_label(test, None) is None


# --------------------------------------------------------------------------- #
# Editor: explicit minutes (timing meta), JSON round trip.
# --------------------------------------------------------------------------- #
def test_editor_requires_every_paper_and_the_duration_sum() -> None:
    assert paper_minutes_errors(a2_test()) == []
    ok = a2_test(paper_minutes={"reading_writing": 60, "listening": 35})
    assert paper_minutes_errors(ok) == []
    missing = a2_test(paper_minutes={"listening": 95})
    assert "не указано для части «Reading & Writing»" in paper_minutes_errors(missing)[0]
    extra = a2_test(paper_minutes={"reading_writing": 60, "listening": 30, "writing": 5})
    assert any("нет части «Writing»" in e for e in paper_minutes_errors(extra))
    wrong = a2_test(paper_minutes={"reading_writing": 60, "listening": 30})
    assert "в сумме 90 мин" in paper_minutes_errors(wrong)[0]


def test_paper_minutes_are_timing_meta_not_key() -> None:
    old = a2_test()
    new = a2_test(paper_minutes={"reading_writing": 65, "listening": 30})
    assert is_schedule_only_change(old, new) and is_cosmetic_change(old, new)
    # Allowed on a published test WITH attempts, without force.
    assert check_edit_allowed(old, new, attempts=3, force=False) is True


MP3 = b"ID3" + bytes(70)  # an ID3 header is enough for storage


@pytest.fixture
def content(session: Session, tmp_path: Path) -> ContentService:
    store = FilesystemStorage(tmp_path / "assets")
    for asset in ("pa-l1.mp3", "pa-l2.mp3"):
        store.put(asset, MP3)
    return ContentService(ContentRepository(session), store)


def test_set_paper_minutes_on_a_published_test_with_attempts(
    session: Session, content: ContentService
) -> None:
    ContentRepository(session).add_test(a2_test())
    repo = AttemptRepository(session)
    repo.add_roster_entry(RosterEntry(id="e1", test_id="pa", display_name="A"))
    DeliveryService(ContentRepository(session), repo).start("e1")
    session.commit()

    t = content.set_paper_minutes("pa", {"reading_writing": 65, "listening": 30})
    assert t.paper_minutes == {"reading_writing": 65, "listening": 30}
    assert ContentRepository(session).get_test("pa").paper_minutes == t.paper_minutes
    with pytest.raises(ContentValidationError, match="в сумме 90"):
        content.set_paper_minutes("pa", {"reading_writing": 60, "listening": 30})
    with pytest.raises(ContentValidationError):
        content.set_paper_minutes("pa", {"speaking": 95})
    assert content.set_paper_minutes("pa", None).paper_minutes is None


def test_meta_patch_takes_duration_and_minutes_together(
    session: Session, content: ContentService
) -> None:
    ContentRepository(session).add_test(a2_test(status="draft"))
    content.set_paper_minutes("pa", {"reading_writing": 60, "listening": 35})
    with pytest.raises(ContentValidationError, match="должны совпадать"):
        content.update_test_meta("pa", {"duration_minutes": 100})
    t = content.update_test_meta(
        "pa",
        {"duration_minutes": 100, "paper_minutes": {"reading_writing": 64, "listening": 36}},
    )
    assert (t.duration_minutes, t.paper_minutes) == (100, {"reading_writing": 64, "listening": 36})


def test_json_export_import_round_trips_paper_minutes(
    session: Session, content: ContentService
) -> None:
    src = a2_test(status="draft", paper_minutes={"reading_writing": 70, "listening": 25})
    ContentRepository(session).add_test(src)
    exported = content.export_test("pa")
    assert exported.paper_minutes == {"reading_writing": 70, "listening": 25}
    raw = json.dumps(exported.model_dump(mode="json"))
    outcome = content.import_test(raw, {}, new_id="pa2")
    assert outcome.test.paper_minutes == {"reading_writing": 70, "listening": 25}
    assert ContentRepository(session).get_test("pa2").paper_minutes == outcome.test.paper_minutes
    copy = content.duplicate_test("pa")
    assert copy.paper_minutes == src.paper_minutes


# --------------------------------------------------------------------------- #
# Delivery: only the chosen papers, deadline = their minutes.
# --------------------------------------------------------------------------- #
def _seed(session: Session, test: ExamTest, entries: dict[str, list[str] | None]) -> None:
    ContentRepository(session).add_test(test)
    repo = AttemptRepository(session)
    for eid, papers in entries.items():
        repo.add_roster_entry(
            RosterEntry(id=eid, test_id=test.id, display_name=eid.upper(), papers=papers)
        )
    session.commit()


def test_delivery_serves_only_the_chosen_papers(session: Session) -> None:
    _seed(session, a2_test(), {"e1": ["listening"], "e2": None})
    svc = DeliveryService(ContentRepository(session), AttemptRepository(session))
    now = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)

    a1 = svc.start("e1", now=now)
    assert a1.papers == ["listening"]
    assert a1.deadline == now + timedelta(minutes=35)
    client = svc.client_test(a1.id)
    assert [s.id for s in client.sections] == ["pa-l1", "pa-l2"]
    assert client.duration_minutes == 35
    dump = json.dumps(client.model_dump(mode="json"))
    assert "correct" not in dump and "accepted" not in dump and "pa-q1" not in dump
    # An item / track of an unserved paper is refused.
    with pytest.raises(NotFoundError):
        svc.save_answer(a1.id, "pa-q2", "house", now=now)
    with pytest.raises(NotFoundError):
        svc.serve_item(a1.id, "pa-q1")
    svc.save_answer(a1.id, "pa-q6", "Tom", now=now)
    assert svc.report_track_progress(a1.id, "pa-l2.mp3", 5) == {"pa-l2.mp3": 5}

    a2 = svc.start("e2", now=now)
    assert a2.papers is None and a2.deadline == now + timedelta(minutes=95)
    assert len(svc.client_test(a2.id).sections) == 4

    # Resume serves the identical (partial) test.
    assert svc.start("e1", now=now + timedelta(minutes=1)).id == a1.id
    assert [s.id for s in svc.client_test(a1.id).sections] == ["pa-l1", "pa-l2"]


def test_reading_only_refuses_listening_tracks(session: Session) -> None:
    _seed(session, a2_test(), {"e1": ["reading_writing"]})
    svc = DeliveryService(ContentRepository(session), AttemptRepository(session))
    a = svc.start("e1")
    with pytest.raises(UnknownTrackError):
        svc.report_track_progress(a.id, "pa-l1.mp3", 5)


def test_partial_deadline_uses_explicit_minutes_and_the_window_cap(session: Session) -> None:
    now = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
    test = a2_test(
        paper_minutes={"reading_writing": 70, "listening": 25},
        closes_at=now + timedelta(minutes=20),
        late_start_grace_minutes=0,
    )
    _seed(session, test, {"e1": ["reading_writing"], "e2": ["listening"]})
    svc = DeliveryService(ContentRepository(session), AttemptRepository(session))
    a = svc.start("e1", now=now)
    assert a.deadline == now + timedelta(minutes=20)  # capped by the close
    ContentRepository(session).set_schedule(
        "pa", opens_at=None, closes_at=None, late_start_grace_minutes=0
    )
    b = svc.start("e2", now=now)
    assert b.deadline == now + timedelta(minutes=25)


def test_choice_lost_by_an_edit_falls_back_to_the_whole_test(session: Session) -> None:
    _seed(session, a2_test(), {"e1": ["writing"]})  # stored before a (hypothetical) edit
    svc = DeliveryService(ContentRepository(session), AttemptRepository(session))
    a = svc.start("e1")
    assert a.papers is None and len(svc.client_test(a.id).sections) == 4


# --------------------------------------------------------------------------- #
# Grading / results over the served papers only.
# --------------------------------------------------------------------------- #
def test_grading_counts_only_the_served_papers(session: Session) -> None:
    _seed(session, a2_test(), {"e1": ["listening"]})
    repo = AttemptRepository(session)
    svc = DeliveryService(ContentRepository(session), repo)
    a = svc.start("e1")
    svc.save_answer(a.id, "pa-q6", "Tom")
    result = GradingService(ContentRepository(session), repo).grade(a.id)
    assert {g.item_id for g in result.items} == {"pa-q4", "pa-q5", "pa-q6"}
    assert (result.score, result.max_score) == (1, 3)
    assert not result.needs_review  # the writing task was not served


def test_choose_one_writing_only_when_writing_is_served(session: Session) -> None:
    _seed(session, b2_test(), {"w": ["writing"], "r": ["reading_use_of_english"]})
    repo = AttemptRepository(session)
    svc = DeliveryService(ContentRepository(session), repo)
    grading = GradingService(ContentRepository(session), repo)
    aw = svc.start("w")
    svc.save_answer(aw.id, "pb-w2b", "My answer " * 20)
    gw = grading.grade(aw.id)
    # Part 1 (20) + ONE choose-one task (20): the group counts once.
    assert gw.max_score == 40
    assert {g.item_id for g in gw.items} == {"pb-w1a", "pb-w2b"}
    ar = svc.start("r")
    gr = grading.grade(ar.id)
    assert {g.item_id for g in gr.items} == {"pb-q1", "pb-q2"} and gr.max_score == 2


# --------------------------------------------------------------------------- #
# HTTP: teacher roster API + student runner API + results.
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _no_background_finalize(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.results as results

    monkeypatch.setattr(results, "finalize_attempt", lambda _id: None)


@pytest.fixture
def storage(tmp_path: Path) -> FilesystemStorage:
    return FilesystemStorage(tmp_path / "assets")


@pytest.fixture
def client(
    session: Session, storage: FilesystemStorage, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setattr(admin_api.settings, "teacher_password", PASSWORD)
    monkeypatch.setattr(admin_api.settings, "environment", "test")
    admin_api.login_limiter.reset()
    ContentRepository(session).add_test(a2_test())
    ContentRepository(session).add_test(b2_test())
    session.commit()
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
def auth(client: TestClient) -> dict[str, str]:
    token = client.post("/admin/login", json={"password": PASSWORD}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _add(client: TestClient, auth: dict, name: str, papers=None, test_id: str = "pa") -> dict:
    body: dict = {"display_name": name}
    if papers is not None:
        body["papers"] = papers
    resp = client.post(f"/admin/tests/{test_id}/roster", json=body, headers=auth)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _start(client: TestClient, entry: dict) -> tuple[str, dict[str, str]]:
    resp = client.post(f"/exam/roster/{entry['id']}/start", json={"pin": entry["pin"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body["attempt_id"], {"X-Attempt-Token": body["token"]}


def test_papers_endpoint_lists_minutes(client: TestClient, auth: dict) -> None:
    assert client.get("/admin/tests/pa/papers").status_code == 401
    info = client.get("/admin/tests/pa/papers", headers=auth).json()
    assert info["explicit"] is False and info["duration_minutes"] == 95
    assert [(p["paper"], p["label"], p["minutes"]) for p in info["papers"]] == [
        ("reading_writing", "Reading & Writing", 60),
        ("listening", "Listening", 35),
    ]
    resp = client.put(
        "/admin/tests/pa/paper-minutes",
        json={"paper_minutes": {"reading_writing": 70, "listening": 25}},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    info = client.get("/admin/tests/pa/papers", headers=auth).json()
    assert info["explicit"] is True and [p["minutes"] for p in info["papers"]] == [70, 25]
    bad = client.put(
        "/admin/tests/pa/paper-minutes",
        json={"paper_minutes": {"reading_writing": 70, "listening": 20}},
        headers=auth,
    )
    assert bad.status_code == 400 and "в сумме 90" in bad.json()["detail"]
    assert client.get("/admin/tests/nope/papers", headers=auth).status_code == 404


def test_assign_validation_unknown_and_absent_papers(client: TestClient, auth: dict) -> None:
    unknown = client.post(
        "/admin/tests/pa/roster", json={"display_name": "X", "papers": ["speaking"]}, headers=auth
    )
    assert unknown.status_code == 422
    absent = client.post(
        "/admin/tests/pa/roster", json={"display_name": "X", "papers": ["writing"]}, headers=auth
    )
    assert absent.status_code == 422 and "нет части «Writing»" in absent.json()["detail"]
    empty = client.post(
        "/admin/tests/pa/roster", json={"display_name": "X", "papers": []}, headers=auth
    )
    assert empty.status_code == 422
    entry = _add(client, auth, "Anna", ["listening"])
    assert entry["papers"] == ["listening"]
    # Every paper == whole test (stored as None).
    assert _add(client, auth, "Bob", ["listening", "reading_writing"])["papers"] is None
    bulk = client.post(
        "/admin/tests/pa/roster/bulk",
        json={"names": ["C1", "C2"], "papers": ["reading_writing"]},
        headers=auth,
    ).json()
    assert [e["papers"] for e in bulk["added"]] == [["reading_writing"]] * 2
    rows = client.get("/admin/tests/pa/roster", headers=auth).json()
    roster = {r["display_name"]: r for r in rows}
    assert roster["Anna"]["papers_label"] == "Listening" and roster["Anna"]["minutes"] == 35
    assert roster["Bob"]["papers"] is None and roster["Bob"]["minutes"] == 95
    assert roster["C1"]["minutes"] == 60


def test_change_only_before_start(client: TestClient, auth: dict) -> None:
    anna = _add(client, auth, "Anna")
    url = f"/admin/roster/{anna['id']}/papers"
    resp = client.put(url, json={"papers": ["listening"]}, headers=auth)
    assert resp.status_code == 200 and resp.json()["papers"] == ["listening"]
    assert client.put(url, json={"papers": ["nope"]}, headers=auth).status_code == 422
    assert client.put(url, json={"papers": ["writing"]}, headers=auth).status_code == 422
    attempt_id, _ = _start(client, anna)
    locked = client.put(url, json={"papers": None}, headers=auth)
    assert locked.status_code == 409 and "уже начал" in locked.json()["detail"]
    # The roster shows what the attempt serves.
    row = next(r for r in client.get("/admin/tests/pa/roster", headers=auth).json())
    assert row["papers"] == ["listening"] and row["attempt_id"] == attempt_id
    # After a reset the teacher may assign other parts again.
    assert client.post(f"/admin/roster/{anna['id']}/reset", headers=auth).status_code == 200
    assert client.put(url, json={"papers": None}, headers=auth).status_code == 200
    unknown = client.put("/admin/roster/nope/papers", json={"papers": None}, headers=auth)
    assert unknown.status_code == 404
    assert client.put(url, json={"papers": None}).status_code == 401


def test_class_bulk_skips_started_students(client: TestClient, auth: dict) -> None:
    anna = _add(client, auth, "Anna")
    _add(client, auth, "Bob")
    _add(client, auth, "Cid")
    _start(client, anna)
    res = client.put(
        "/admin/tests/pa/roster/papers", json={"papers": ["listening"]}, headers=auth
    ).json()
    assert sorted(r["display_name"] for r in res["updated"]) == ["Bob", "Cid"]
    assert res["skipped_started"] == ["Anna"]
    assert (
        client.put(
            "/admin/tests/pa/roster/papers", json={"papers": ["writing"]}, headers=auth
        ).status_code
        == 422
    )


def test_listening_only_end_to_end_results(
    client: TestClient, auth: dict, session: Session, storage: FilesystemStorage
) -> None:
    anna = _add(client, auth, "Anna", ["listening"])
    whole = _add(client, auth, "Bob")
    aid, tok = _start(client, anna)
    test = client.get(f"/exam/attempts/{aid}/test", headers=tok)
    assert test.status_code == 200
    body = test.json()
    assert [s["skill"] for s in body["sections"]] == ["listening", "listening"]
    assert "correct" not in test.text and "accepted" not in test.text
    state = client.get(f"/exam/attempts/{aid}/state", headers=tok).json()
    assert 34 * 60 <= state["remaining_seconds"] <= 35 * 60
    # The reading item of the unserved paper cannot be answered.
    assert (
        client.put(
            f"/exam/attempts/{aid}/answers/pa-q2", json={"response": "house"}, headers=tok
        ).status_code
        == 404
    )
    assert client.put(
        f"/exam/attempts/{aid}/answers/pa-q6", json={"response": "Tom"}, headers=tok
    ).status_code in (200, 204)
    assert client.post(f"/exam/attempts/{aid}/submit", headers=tok).status_code == 200
    bid, btok = _start(client, whole)
    assert client.post(f"/exam/attempts/{bid}/submit", headers=btok).status_code == 200

    svc = build_admin_service(session, storage, analysis_llm=MockAnalysisLLM())
    svc.regrade(aid)
    svc.regrade(bid)
    session.commit()

    listed = client.get("/admin/tests/pa/results", headers=auth).json()
    rows = {o["display_name"]: o for o in listed}
    assert rows["Anna"]["max_score"] == 3 and rows["Anna"]["score"] == 1
    assert rows["Anna"]["papers_label"] == "Частично: Listening"
    assert rows["Bob"]["papers_label"] is None and rows["Bob"]["max_score"] > 3

    detail = client.get(f"/admin/results/{aid}", headers=auth).json()
    assert detail["papers_label"] == "Частично: Listening"
    assert {r["item_id"] for r in detail["responses"]} == {"pa-q4", "pa-q5", "pa-q6"}
    assert all(s["skill"] == "listening" for s in detail["skills"])
    note = detail["timeline"][0]
    assert note["type"] == "papers" and "Listening (35 мин)" in note["label"]
    assert not note["integrity"]
    # The integrity profile is unaffected metadata-wise (still computed).
    assert detail["verdict"] is not None

    report = client.get(f"/admin/results/{aid}/report", headers=auth)
    assert report.status_code == 200
    rep = report.json()
    assert rep["papers_label"] == "Частично: Listening"
    assert [s["skill"] for s in rep["skills"]] == ["listening"]
    assert rep["writing"] == [] and rep["max_score"] == 3

    csv_text = client.get("/admin/tests/pa/results.csv", headers=auth).content.decode("utf-8-sig")
    lines = [line.split(",") for line in csv_text.split("\r\n") if line]
    header, by_name = lines[0], {line[0]: line for line in lines[1:]}
    assert header[-1] == "parts"
    assert by_name["Anna"][-1] == "Частично: Listening"
    assert by_name["Bob"][-1] == "все части"
    anna = by_name["Anna"]
    assert anna[header.index("max")] == "3"
    assert anna[header.index("reading_score")] == "" and anna[header.index("writing_max")] == ""


def test_b2_partial_scale_and_skills_only_over_served(
    session: Session, storage: FilesystemStorage
) -> None:
    _seed(session, b2_test(), {"e1": ["listening"]})
    repo = AttemptRepository(session)
    delivery = DeliveryService(ContentRepository(session), repo)
    a = delivery.start("e1")
    delivery.save_answer(a.id, "pb-q9", "A")
    delivery.submit(a.id)
    svc = build_admin_service(session, storage, analysis_llm=MockAnalysisLLM())
    svc.regrade(a.id)
    result = svc.attempt_result(a.id)
    assert [t.skill for t in result.skill_totals] == ["listening"]
    # No overall over a subset of the skills: per-skill values only.
    assert result.skills_average_percent is None
    assert result.overall_note == "общий балл не считается — сданы не все части"
    assert result.cambridge_scale is not None
    assert [r.skill for r in result.cambridge_scale.skills] == ["listening"]
    assert result.cambridge_scale.skills[0].scale is not None
    assert result.cambridge_scale.overall is None and result.cambridge_scale.overall_cefr is None
    report = svc.student_report(a.id)
    assert report.writing == [] and report.papers_label == "Частично: Listening"
    assert report.skills_average_percent is None and report.overall_note
    assert (report.score, report.max_score) == (1, 1)


# --------------------------------------------------------------------------- #
# Migration d0e1f2a3b4c5 on a scratch DB (never the real english_exam.db).
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


def _columns(db: Path, table: str) -> set[str]:
    with sqlite3.connect(db) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_migration_adds_and_drops_the_paper_columns(tmp_path: Path) -> None:
    db = tmp_path / "mig.db"
    _alembic(db, "upgrade", "c9d0e1f2a3b4")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO tests (id, title, level, status, duration_minutes) "
            "VALUES ('t1', 'T', 'A2_KEY', 'published', 95)"
        )
        conn.execute(
            "INSERT INTO roster_entries (id, test_id, display_name, status, pin) "
            "VALUES ('e1', 't1', 'Anna', 'not_started', 'AB3D')"
        )
    _alembic(db, "upgrade", "head")
    assert "paper_minutes" in _columns(db, "tests")
    assert "papers" in _columns(db, "roster_entries") and "papers" in _columns(db, "attempts")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT papers FROM roster_entries").fetchone() == (None,)
        conn.execute("UPDATE roster_entries SET papers = '[\"listening\"]'")
    _alembic(db, "downgrade", "c9d0e1f2a3b4")
    assert "paper_minutes" not in _columns(db, "tests")
    assert "papers" not in _columns(db, "roster_entries") | _columns(db, "attempts")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM roster_entries").fetchone()[0] == 1
    _alembic(db, "upgrade", "head")
    assert "papers" in _columns(db, "roster_entries")
