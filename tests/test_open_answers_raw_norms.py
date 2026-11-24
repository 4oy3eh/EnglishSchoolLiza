"""Phase 20 machinery (SYNTHETIC data only — no licensed test content):

* partial-credit open answers on gap_fill (`partial` / `partial_points` /
  `review_unmatched`), Russian normalization (case, ё ≡ е, edge punctuation, typos),
  the teacher's 0..max override, authoring-only (golden rule #1);
* norm scoring WITHOUT tables (raw composites, % of maximum, `norms_loaded=False`,
  no IQ) + the forward path (per-age scaled tables, a raw total -> score by age,
  `final="composite"`), the editor rules, the teacher detail / CSV;
* `Test.ui_language` (persisted, migration a8b9c0d1e2f3, served on ClientTest and
  `/exam/tests/{id}/info`, cosmetic for the content lock).
"""

from __future__ import annotations

import csv
import io
import json
import shutil
import subprocess
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.admin import AdminService, build_admin_service
from app.analysis import MockAnalysisLLM
from app.content.editor import (
    is_cosmetic_change,
    norm_scoring_errors,
    reid_test,
    semantic_errors,
    validate_test_data,
)
from app.content.service import ContentService
from app.content.storage import FilesystemStorage
from app.core.db import get_session
from app.delivery import DeliveryService
from app.grading.deterministic import (
    PARTIAL_FEEDBACK,
    REVIEW_FEEDBACK,
    grade_gap_fill,
    partial_points_of,
)
from app.grading.norm_scoring import compute_iq_report
from app.grading.service import GradingService, item_max_points
from app.persistence.repository import AttemptRepository, ContentRepository
from apps.api import admin as admin_api
from apps.api.main import app
from contracts import GapFillItem, NormScoring, RosterEntry
from contracts import Test as ExamTest
from tests.test_iq_family import _alembic, _columns

TID = "ru-demo"
S1, S2, S3 = f"{TID}-s1", f"{TID}-s2", f"{TID}-s3"
T0 = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)
PASSWORD = "test-teacher-pw"
LET = "абв"


# --------------------------------------------------------------------------- #
# A synthetic Russian-UI test: letters (no shuffle), open 2/1/0 answers, numbers.
# --------------------------------------------------------------------------- #
def _open(qid: str, accepted: list[str], partial: list[str] | None = None) -> dict[str, Any]:
    return {
        "item_type": "gap_fill",
        "id": f"{TID}-{qid}",
        "prompt": f"Пара слов {qid}",
        "accepted": accepted,
        "partial": partial or [],
        "review_unmatched": True,
        "spelling": "tolerant",
        "points": 2,
    }


def ru_test_json() -> dict[str, Any]:
    blank = {"kind": "passage_text", "text": ""}
    letters = [{"kind": "text", "key": k, "text": k} for k in LET]
    return {
        "id": TID,
        "title": "Синтетический тест",
        "level": "IQ",
        "ui_language": "ru",
        "duration_minutes": 10,
        "sections": [
            {
                "id": S1,
                "title": "Буквы",
                "stimulus": blank,
                "time_limit_seconds": 60,
                "items": [
                    {
                        "item_type": "single_choice",
                        "id": f"{TID}-c{n}",
                        "prompt": f"Вопрос {n}",
                        "options": letters,
                        "correct": LET[n - 1],
                        "shuffle_options": False,
                    }
                    for n in (1, 2)
                ],
            },
            {
                "id": S2,
                "title": "Обобщение",
                "stimulus": blank,
                "time_limit_seconds": 60,
                "items": [
                    _open("o1", ["пушистые зверьки", "пушистый зверек"], ["звери"]),
                    _open("o2", ["ёлки"]),
                ],
            },
            {
                "id": S3,
                "title": "Числа",
                "stimulus": blank,
                "time_limit_seconds": 60,
                "items": [
                    {
                        "item_type": "gap_fill",
                        "id": f"{TID}-n1",
                        "prompt": "1  2  3  ?",
                        "accepted": ["4"],
                        "spelling": "exact",
                        "max_words": 1,
                        "numeric": True,
                    }
                ],
            },
        ],
        "norm_scoring": {
            "composites": [
                {"key": "A", "label": "A = s1 + s2", "section_ids": [S1, S2], "basis": "raw"},
                {"key": "B", "label": "B = s3", "section_ids": [S3], "basis": "raw"},
                {"key": "ALL", "label": "все", "section_ids": [S1, S2, S3], "basis": "raw"},
            ],
            "note": "Синтетические нормы не загружены.",
        },
    }


def _test() -> ExamTest:
    return ExamTest.model_validate(ru_test_json())


def _item(qid: str) -> GapFillItem:
    item = next(i for s in _test().sections for i in s.items if i.id == f"{TID}-{qid}")
    assert isinstance(item, GapFillItem)
    return item


# --------------------------------------------------------------------------- #
# Partial-credit open answers (grading engine, pure).
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("response", "awarded", "review", "feedback"),
    [
        ("пушистые зверьки", 2, False, None),
        ("  Пушистые Зверьки. ", 2, False, None),  # case + edge punctuation
        ("«пушистый зверёк»", 2, False, None),  # ё == е, quotes around
        ("пушистые зверки", 2, False, None),  # one typo in a long word (tolerant)
        ("звери", 1, False, PARTIAL_FEEDBACK),
        ("Звери!", 1, False, PARTIAL_FEEDBACK),
        ("коты", 0, True, REVIEW_FEEDBACK),  # not in the key -> the teacher decides
        ("", 0, False, None),  # blank: nothing to review
        ("   ", 0, False, None),
        (None, 0, False, None),
    ],
)
def test_open_answer_full_partial_review(
    response: str | None, awarded: float, review: bool, feedback: str | None
) -> None:
    g = grade_gap_fill(_item("o1"), response)
    assert (g.awarded, g.max_points, g.needs_review, g.feedback) == (awarded, 2, review, feedback)


def test_yo_folds_to_ye_both_ways() -> None:
    item = _item("o2")  # key written with ё
    assert grade_gap_fill(item, "елки").awarded == 2
    assert grade_gap_fill(item, "ЁЛКИ").awarded == 2


def test_rejected_form_is_wrong_without_review() -> None:
    item = _item("o1").model_copy(update={"rejected": ["зверьки"]})
    g = grade_gap_fill(item, "зверьки")
    assert g.awarded == 0 and not g.needs_review


def test_partial_points_explicit_and_default() -> None:
    item = _item("o1")
    assert partial_points_of(item) == 1  # half of 2
    three = item.model_copy(update={"points": 3, "partial_points": 2})
    assert grade_gap_fill(three, "звери").awarded == 2
    assert grade_gap_fill(three, "пушистые зверьки").awarded == 3
    assert item_max_points(three) == 3


def test_exact_spelling_open_answer_sends_typos_to_review() -> None:
    item = _item("o1").model_copy(update={"spelling": "exact"})
    g = grade_gap_fill(item, "пушистые зверки")
    assert g.awarded == 0 and g.needs_review


def test_without_review_flag_unmatched_is_plain_wrong() -> None:
    item = _item("o1").model_copy(update={"review_unmatched": False})
    g = grade_gap_fill(item, "коты")
    assert g.awarded == 0 and not g.needs_review and g.feedback is None
    assert grade_gap_fill(item, "звери").awarded == 1  # partial still works


def test_partial_points_must_be_below_the_worth() -> None:
    data = _open("x", ["a"], ["b"])
    data["partial_points"] = 2
    with pytest.raises(ValueError, match="partial_points"):
        GapFillItem.model_validate(data)
    one = _open("x", ["a"], ["b"]) | {"points": None, "partial_points": 0.5}
    assert GapFillItem.model_validate(one).partial_points == 0.5


def test_editor_refuses_blank_or_duplicated_partial_answers() -> None:
    data = ru_test_json()
    data["sections"][1]["items"][0]["partial"] = ["звери", " "]
    data["sections"][1]["items"][1]["partial"] = ["Ёлки"]  # also a full key
    errors = semantic_errors(ExamTest.model_validate(data))
    assert any("пустой ответ на частичный балл" in e for e in errors), errors
    assert any("и в принимаемых, и в частичных" in e for e in errors), errors


def test_old_gap_fill_grading_is_unchanged_by_default() -> None:
    plain = GapFillItem(id="g", prompt="p", accepted=["train"], spelling="tolerant")
    assert grade_gap_fill(plain, "trian").awarded == 1  # typo still accepted
    assert grade_gap_fill(plain, "rain").awarded == 0  # a real word never is
    assert not grade_gap_fill(plain, "bus").needs_review


# --------------------------------------------------------------------------- #
# Norm scoring without tables + the forward (IST-style) path.
# --------------------------------------------------------------------------- #
def _norms(**update: Any) -> NormScoring:
    data = ru_test_json()["norm_scoring"] | update
    return NormScoring.model_validate(data)


MAXIMA = {S1: 2.0, S2: 4.0, S3: 1.0}


def test_raw_report_without_tables() -> None:
    r = compute_iq_report({S1: 1, S2: 3, S3: 1}, _norms(), 15, raw_max_by_section=MAXIMA)
    assert r.norms_loaded is False and r.notes == ["no_norm_tables"]
    assert [(s.raw, s.raw_max, s.percent, s.scaled) for s in r.sections] == [
        (1, 2, 50.0, None),
        (3, 4, 75.0, None),
        (1, 1, 100.0, None),
    ]
    by = {c.key: c for c in r.composites}
    assert (by["A"].raw_sum, by["A"].raw_max, by["A"].percent) == (4, 6, 66.7)
    assert (by["ALL"].raw_sum, by["ALL"].raw_max, by["ALL"].percent) == (5, 7, 71.4)
    assert all(c.basis == "raw" and c.score is None for c in r.composites)
    assert r.overall is None and r.bracket is None and r.age == 15
    assert r.note == "Синтетические нормы не загружены."


def test_raw_report_missing_section_and_no_age_notes() -> None:
    r = compute_iq_report({S1: 2, S2: None, S3: 0}, _norms(), None, raw_max_by_section=MAXIMA)
    assert r.notes == [f"section_not_graded:{S2}", "no_norm_tables", "incomplete_composite:A",
                       "incomplete_composite:ALL"]  # fmt: skip
    assert {c.key: c.raw_sum for c in r.composites} == {"A": None, "B": 0, "ALL": None}


def test_raw_total_to_score_by_age_and_final_composite() -> None:
    """The later IST-style table: the raw total -> a score per age group, and that
    composite's score IS the overall (no mean over composites)."""
    norms = _norms(
        by_age={
            "age_groups": [{"key": "13-15", "min_age": 13, "max_age": 15}],
            "rows": [{"sum": s, "values": {"13-15": 80 + 5 * s}} for s in range(0, 8)],
        },
        final="composite",
        final_composite="ALL",
        brackets=[{"label": "низкий", "min": 0}, {"label": "средний", "min": 100}],
        min_age=13,
    )
    r = compute_iq_report({S1: 2, S2: 2.5, S3: 1}, norms, 14, raw_max_by_section=MAXIMA)
    by = {c.key: c for c in r.composites}
    assert by["ALL"].raw_sum == 5.5 and by["ALL"].score == 80 + 5 * 6  # 5.5 rounds half-up
    assert r.norms_loaded and r.overall == 110 and r.bracket == "средний"
    assert r.final == "composite" and r.notes == []


def test_scaled_tables_by_age_take_precedence() -> None:
    band = {"raw_min": 0, "raw_max": 4}
    norms = NormScoring.model_validate(
        {
            "composites": [{"key": "X", "section_ids": [S2]}],
            "scaled_tables": {S2: [band | {"scaled": 1}]},
            "scaled_tables_by_age": {"teen": {S2: [band | {"scaled": 9}]}},
            "by_age": {
                "age_groups": [
                    {"key": "teen", "min_age": 13, "max_age": 17},
                    {"key": "adult", "min_age": 18},
                ],
                "rows": [{"sum": 1, "values": {"adult": 70}}, {"sum": 9, "values": {"teen": 120}}],
            },
            "min_age": 13,
        }
    )
    teen = compute_iq_report({S2: 3}, norms, 15)
    adult = compute_iq_report({S2: 3}, norms, 30)
    assert teen.sections[0].scaled == 9 and teen.overall == 120
    assert adult.sections[0].scaled == 1 and adult.overall == 70


def test_final_composite_must_name_a_composite() -> None:
    with pytest.raises(ValueError, match="final_composite"):
        _norms(final="composite", final_composite="NOPE")


def test_editor_rules_for_optional_tables() -> None:
    assert norm_scoring_errors(_test()) == []  # raw composites need no scaled table
    data = ru_test_json()
    data["norm_scoring"]["composites"][0]["basis"] = "scaled"
    data["norm_scoring"]["scaled_tables_by_age"] = {"kids": {S3: [{"raw_min": 0, "raw_max": 1,
                                                                   "scaled": 1}]}}  # fmt: skip
    data["norm_scoring"]["brackets"] = [{"label": "x", "min": 0}]
    errors = norm_scoring_errors(ExamTest.model_validate(data))
    assert any(f"нет таблицы шкальных баллов для раздела «{S1}»" in e for e in errors)
    assert any("неизвестной возрастной группы «kids»" in e for e in errors)
    assert any("диапазоны оценок без таблицы норм" in e for e in errors)


def test_reid_rebases_per_age_tables() -> None:
    data = ru_test_json()
    data["norm_scoring"]["scaled_tables_by_age"] = {
        "g": {S3: [{"raw_min": 0, "raw_max": 1, "scaled": 1}]}
    }
    data["norm_scoring"]["by_age"] = {
        "age_groups": [{"key": "g", "min_age": 10}],
        "rows": [{"sum": 1, "values": {"g": 90}}],
    }
    copy = reid_test(ExamTest.model_validate(data), "ru-copy")
    assert copy.norm_scoring is not None
    assert list(copy.norm_scoring.scaled_tables_by_age["g"]) == ["ru-copy-s3"]
    assert copy.norm_scoring.composites[2].section_ids == ["ru-copy-s1", "ru-copy-s2", "ru-copy-s3"]


def test_editor_warns_when_norms_are_not_loaded(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    with caplog.at_level(logging.WARNING):
        validate_test_data(ru_test_json())
    assert any("только сырые баллы" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# ui_language: contract, persistence, projection, /info, content lock.
# --------------------------------------------------------------------------- #
def test_ui_language_default_projection_and_cosmetic() -> None:
    test = _test()
    assert test.ui_language == "ru"
    english = ExamTest.model_validate(ru_test_json() | {"ui_language": "en"})
    assert ExamTest.model_validate({k: v for k, v in ru_test_json().items()
                                    if k != "ui_language"}).ui_language == "en"  # fmt: skip
    assert is_cosmetic_change(test, english)  # chrome language is not key-bearing
    with pytest.raises(ValueError):
        ExamTest.model_validate(ru_test_json() | {"ui_language": "de"})


def _content(session: Session, tmp_path: Path) -> ContentService:
    return ContentService(ContentRepository(session), FilesystemStorage(tmp_path))


def test_ui_language_and_open_answer_fields_persist(session: Session, tmp_path: Path) -> None:
    _content(session, tmp_path).import_test(json.dumps(ru_test_json()), {})
    session.expire_all()
    stored = ContentRepository(session).get_test(TID)
    assert stored is not None
    assert stored.model_dump(mode="json") == _test().model_dump(mode="json")
    updated = _content(session, tmp_path).update_test_meta(TID, {"ui_language": "uk"})
    assert updated.ui_language == "uk"


def _start(session: Session, tmp_path: Path, birth: date | None = None) -> DeliveryService:
    content = _content(session, tmp_path)
    content.import_test(ru_test_json(), {})
    assert content.publish(TID)
    AttemptRepository(session).add_roster_entry(
        RosterEntry(id="ru-r1", test_id=TID, display_name="Аня", birth_date=birth)
    )
    return DeliveryService(ContentRepository(session), AttemptRepository(session))


def test_client_test_and_info_carry_ui_language(session: Session, tmp_path: Path) -> None:
    svc = _start(session, tmp_path)
    attempt = svc.start("ru-r1", now=T0)
    served = svc.client_test(attempt.id, now=T0)
    assert served.ui_language == "ru"
    svc.begin_section(attempt.id, S1, now=T0)
    svc.finish_section(attempt.id, S1, now=T0)
    svc.begin_section(attempt.id, S2, now=T0)
    open_items = svc.section_items(attempt.id, S2, now=T0)
    assert [i.id for i in open_items.items] == [f"{TID}-o1", f"{TID}-o2"]
    dumped = json.dumps(
        [served.model_dump(mode="json"), open_items.model_dump(mode="json")], ensure_ascii=False
    )
    for secret in ("partial", "review_unmatched", "пушистые", "звери", "ёлки", "norm", "accepted"):
        assert secret not in dumped, secret
    session.commit()
    app.dependency_overrides[get_session] = lambda: session
    try:
        info = TestClient(app).get(f"/exam/tests/{TID}/info")
    finally:
        app.dependency_overrides.clear()
    assert info.status_code == 200 and info.json()["ui_language"] == "ru"


def test_migration_adds_and_drops_ui_language(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "mig.db"
    _alembic(db, "upgrade", "f2a3b4c5d6e7")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO tests (id, title, level, status, duration_minutes) "
            "VALUES ('t1', 'T', 'A2_KEY', 'published', 30)"
        )
    _alembic(db, "upgrade", "head")
    assert "ui_language" in _columns(db, "tests")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT ui_language FROM tests WHERE id='t1'").fetchone() == ("en",)
    _alembic(db, "downgrade", "f2a3b4c5d6e7")
    assert "ui_language" not in _columns(db, "tests")
    _alembic(db, "upgrade", "head")
    assert "ui_language" in _columns(db, "tests")


# --------------------------------------------------------------------------- #
# Service + teacher API: review queue, 0..max override, raw IQ block, CSV.
# --------------------------------------------------------------------------- #
def _finish(svc: DeliveryService, answers: dict[str, Any]) -> str:
    attempt = svc.start("ru-r1", now=T0)
    now = T0
    for section in _test().sections:
        svc.begin_section(attempt.id, section.id, now=now)
        for item in section.items:
            if item.id in answers:
                svc.save_answer(attempt.id, item.id, answers[item.id], now=now)
        svc.finish_section(attempt.id, section.id, now=now)
        now += timedelta(seconds=1)
    svc.submit(attempt.id, now=now)
    return attempt.id


ANSWERS = {
    f"{TID}-c1": 0,  # "а" (no shuffle) -> right
    f"{TID}-c2": 0,  # "а" -> wrong (key "б")
    f"{TID}-o1": "звери",  # partial: 1 of 2
    f"{TID}-o2": "сосны",  # not in the key -> review
    f"{TID}-n1": "4",
}


def test_service_review_queue_and_override(session: Session, tmp_path: Path) -> None:
    svc = _start(session, tmp_path)
    attempt_id = _finish(svc, ANSWERS)
    grading = GradingService(ContentRepository(session), AttemptRepository(session))
    result = grading.grade(attempt_id)
    by = {g.item_id: g for g in result.items}
    assert by[f"{TID}-o1"].awarded == 1 and not by[f"{TID}-o1"].needs_review
    assert by[f"{TID}-o2"].needs_review and [g.item_id for g in grading.review_queue(result)] == [
        f"{TID}-o2"
    ]
    assert result.score == 1 + 1 + 0 + 1 and result.max_score == 2 + 4 + 1
    report = grading.iq_report(result)
    assert report is not None and not report.norms_loaded
    assert [c.raw_sum for c in report.composites] == [2, 1, 3]
    fixed = grading.apply_override(result, f"{TID}-o2", awarded=1)  # "similar but incomplete"
    assert not fixed.needs_review and fixed.score == 4
    after = grading.iq_report(fixed)
    assert after is not None and [c.raw_sum for c in after.composites] == [3, 1, 4]


@pytest.fixture
def teacher(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, dict[str, str], str]]:
    monkeypatch.setattr(admin_api.settings, "teacher_password", PASSWORD)
    monkeypatch.setattr(admin_api.settings, "environment", "test")
    admin_api.login_limiter.reset()
    svc = _start(session, tmp_path, birth=date(2011, 1, 1))
    attempt_id = _finish(svc, ANSWERS)
    session.commit()
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


def test_teacher_detail_override_and_csv(teacher: tuple[TestClient, dict[str, str], str]) -> None:
    api, h, a = teacher
    result = api.get(f"/admin/results/{a}", headers=h).json()
    iq = result["iq"]
    assert iq["norms_loaded"] is False and iq["overall"] is None
    assert iq["notes"] == ["no_norm_tables"] and iq["age"] == 15
    assert [s["percent"] for s in iq["sections"]] == [50.0, 25.0, 100.0]
    rows = {r["item_id"]: r for r in result["responses"]}
    o1, o2 = rows[f"{TID}-o1"], rows[f"{TID}-o2"]
    assert o1["partial"] and o1["awarded"] == 1 and o1["note"] == PARTIAL_FEEDBACK
    assert "частично (½): звери" in o1["correct_answer"]
    assert o2["needs_review"] and o2["note"] == REVIEW_FEEDBACK and o2["max_points"] == 2

    # The teacher gives 1 of 2 (the existing ManualGrade override) -> raw recomputed.
    fixed = api.put(f"/admin/results/{a}/grades/{TID}-o2", json={"awarded": 1}, headers=h)
    assert fixed.status_code == 200
    body = fixed.json()
    assert {c["key"]: c["raw_sum"] for c in body["iq"]["composites"]} == {"A": 3, "B": 1, "ALL": 4}
    assert not body["grading"]["needs_review"]
    too_much = api.put(f"/admin/results/{a}/grades/{TID}-o2", json={"awarded": 3}, headers=h)
    assert too_much.status_code == 400

    assert api.post(f"/admin/results/{a}/regrade", headers=h).status_code == 200
    resp = api.get(f"/admin/tests/{TID}/results.csv", headers=h)
    row = next(csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig"))))
    assert (row["iq_s2_raw"], row["iq_s2_scaled"], row["iq_s2_pct"]) == ("2", "", "50")
    assert (row["iq_ALL_raw"], row["iq_ALL_pct"], row["iq_ALL_score"]) == ("4", "57.1", "")
    assert "iq_A_sum" not in row and row["iq_overall"] == "" and "no_norm_tables" in row["iq_notes"]


# --------------------------------------------------------------------------- #
# Runner: units follow the chrome language (pure helper under Node).
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_ready_screen_units_in_russian() -> None:
    src = (ROOT / "apps" / "web" / "exam-iq.js").read_text("utf-8")
    exam = (ROOT / "apps" / "web" / "exam.js").read_text("utf-8")
    assert exam.count('unitMin: "мин"') == 1 and exam.count('unitMin: "хв"') == 1
    assert "configureLanguage(test.level, test.ui_language)" in exam
    script = r"""
const src = require("fs").readFileSync(0, "utf8").replace(/\r\n/g, "\n");
const grab = (n) => {
  const i = src.indexOf(`\nfunction ${n}(`);
  return src.slice(i, src.indexOf("\n}\n", i) + 3);
};
const t = (k) => ({ unitMin: "мин", unitSec: "с" })[k];
const api = new Function("t", grab("iqLimitText") + grab("iqPerQuestionText") +
  grab("iqPracticeNorm") + "return { iqLimitText, iqPerQuestionText, iqPracticeNorm };")(t);
console.log(JSON.stringify([api.iqLimitText(360), api.iqLimitText(90),
  api.iqPerQuestionText(360, 20), api.iqPracticeNorm("  Слово Ёмкость ")]));
"""
    out = subprocess.run(
        ["node", "-e", script], input=src, capture_output=True, text=True, encoding="utf-8",
        timeout=60, check=True,
    )  # fmt: skip
    assert json.loads(out.stdout) == ["6 мин", "1 мин 30 с", "18 с", "слово емкость"]
