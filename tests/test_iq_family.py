"""Phase 17 (IQ family) backend: contracts, mark_toggle, drawing, norm scoring,
roster birth date, editor rules, import, grading service wiring, migration.

Everything runs on the SYNTHETIC fixture in `tests/iq_fixture.py` — no licensed
content (nothing under /private/ or /pdf/ is read).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.content.editor import (
    ContentValidationError,
    is_cosmetic_change,
    referenced_asset_ids,
    reid_test,
    semantic_errors,
    validate_test_data,
)
from app.content.roster import RosterService
from app.content.service import ContentService
from app.content.storage import FilesystemStorage
from app.core.db import get_session
from app.delivery.projection import project_section, project_study, project_test
from app.delivery.service import DeliveryService
from app.grading import GradingService, item_max_points
from app.grading.drawing import (
    MAX_STROKES,
    REVIEW_BELOW,
    evaluate_drawing,
    grade_drawing,
    parse_drawing,
)
from app.grading.mark_toggle import grade_mark_toggle_section
from app.grading.norm_scoring import compute_iq_report, parse_cell, scaled_for
from app.persistence.repository import AttemptRepository, ContentRepository
from apps.api.main import app
from contracts import (
    DrawingItem,
    DrawingLinesTarget,
    DrawingRegionTarget,
    MarkToggleItem,
    NormScoring,
    RosterEntry,
    age_at,
)
from contracts import Test as ExamTest
from contracts.content import ScaledBand
from contracts.papers import papers_of, section_paper, served_test
from tests import iq_fixture as fx

REPO_ROOT = Path(__file__).resolve().parents[1]
TID = "iq-demo"


def _test() -> ExamTest:
    return ExamTest.model_validate(fx.iq_test_json(TID))


def _item(test: ExamTest, item_id: str) -> Any:
    return next(i for s in test.sections for i in s.items if i.id == item_id)


# --------------------------------------------------------------------------- #
# Contracts: level, papers, client projection (golden rule #1).
# --------------------------------------------------------------------------- #
def test_iq_level_is_one_paper_with_whole_duration() -> None:
    test = _test()
    assert papers_of(test) == ["iq"]
    assert {section_paper("IQ", s) for s in test.sections} == {"iq"}
    assert served_test(test, ["iq"]) is test  # every paper = the whole test


def test_client_payload_has_no_key_target_norms_or_study_content() -> None:
    from app.content.pooling import AttemptLayout

    test = _test()
    layout = AttemptLayout(
        test_id=TID, seed=1, section_ids=tuple(s.id for s in test.sections), option_shuffles={}
    )
    client = project_test(test, layout)
    blob = client.model_dump_json()
    for forbidden in (
        '"correct"',
        '"target"',
        '"zones"',
        '"segments"',
        '"tolerance"',
        '"norm_scoring"',
        '"scaled_tables"',
        '"by_age"',
        "cat dog sun",  # the study text
    ):
        assert forbidden not in blob, forbidden
    pairs, drawing, memory = client.sections
    assert pairs.time_limit_seconds == 60 and pairs.intro is not None
    assert pairs.items[0].item_type == "mark_toggle"
    d1, d2 = drawing.items
    assert d1.item_type == "drawing" and d1.mode == "region" and d1.square is None  # type: ignore[union-attr]
    assert d2.mode == "lines" and d2.square is not None and d2.square.w == 0.8  # type: ignore[union-attr]
    assert memory.study is not None and memory.study.study_seconds == 20
    assert set(memory.study.model_dump()) == {"study_seconds"}


def test_study_stimulus_is_a_separate_projection() -> None:
    test = _test()
    study = project_study(test.sections[2], "att-1")
    assert study is not None and study.text == "cat dog sun" and study.study_seconds == 20
    assert project_study(test.sections[0], "att-1") is None
    # The bulk section projection never carries it.
    from app.content.pooling import AttemptLayout

    layout = AttemptLayout(
        test_id=TID, seed=1, section_ids=(test.sections[2].id,), option_shuffles={}
    )
    assert "cat dog sun" not in project_section(test.sections[2], layout).model_dump_json()


def test_drawing_mode_must_match_target() -> None:
    data = fx.iq_test_json(TID)
    data["sections"][1]["items"][0]["mode"] = "lines"
    with pytest.raises(ContentValidationError):
        validate_test_data(data)


# --------------------------------------------------------------------------- #
# mark_toggle: each false mark cancels one correct mark (floor 0).
# --------------------------------------------------------------------------- #
ROWS = [MarkToggleItem(id=rid, prompt=rid, correct=ok) for rid, ok in fx.PAIR_ROWS]


def _toggle(marked: set[str]) -> tuple[float, dict[str, Any]]:
    grades = grade_mark_toggle_section(ROWS, {r.id: "1" if r.id in marked else "0" for r in ROWS})
    return sum(g.awarded for g in grades), {g.item_id: g for g in grades}


@pytest.mark.parametrize(
    ("marked", "raw"),
    [
        ({"r1", "r3", "r5"}, 3),  # all right, nothing wrong
        ({"r1", "r3"}, 2),
        (set(), 0),
        ({"r1", "r2", "r3", "r4", "r5", "r6"}, 0),  # mark everything -> nothing
        ({"r1", "r3", "r5", "r2"}, 2),  # one false mark cancels one
        ({"r1", "r2", "r4"}, 0),  # floor 0, never negative
        ({"r2", "r4", "r6"}, 0),
    ],
)
def test_mark_toggle_penalty_golden(marked: set[str], raw: float) -> None:
    total, _ = _toggle(marked)
    assert total == raw


def test_mark_toggle_grades_explain_the_cancellation() -> None:
    _, by_id = _toggle({"r1", "r3", "r5", "r2"})
    assert by_id["r1"].awarded == 1 and by_id["r3"].awarded == 1
    # The LAST correct mark is the one cancelled, by the false mark on r2.
    assert by_id["r5"].awarded == 0 and by_id["r5"].max_points == 1
    assert "r2" in (by_id["r5"].feedback or "")
    assert by_id["r2"].max_points == 0 and "ложная" in (by_id["r2"].feedback or "")
    assert all(g.method == "mark_toggle" for g in by_id.values())
    assert sum(g.max_points for g in by_id.values()) == 3  # section max = rows to mark


def test_mark_toggle_unmarked_values() -> None:
    for value in ("0", "", "x", "true", None):
        grades = grade_mark_toggle_section(ROWS[:1], {"r1": value})
        assert grades[0].awarded == 0
    assert item_max_points(ROWS[0]) == 1 and item_max_points(ROWS[1]) == 0


# --------------------------------------------------------------------------- #
# drawing: region mode.
# --------------------------------------------------------------------------- #
def _zig(u0: float, v0: float, u1: float, v1: float, n: int = 6) -> list[tuple[float, float]]:
    return [(u0 + (u1 - u0) * k / n, v0 if k % 2 == 0 else v1) for k in range(n + 1)]


def _region() -> DrawingItem:
    return _item(_test(), f"{TID}-d1")


def _lines(**target_update: Any) -> DrawingItem:
    item: DrawingItem = _item(_test(), f"{TID}-d2")
    if target_update:
        item = item.model_copy(update={"target": item.target.model_copy(update=target_update)})
    return item


def test_region_hit_is_correct_and_confident() -> None:
    g = grade_drawing(_region(), fx.picture_payload([_zig(0.65, 0.65, 0.85, 0.85)]))
    assert g.awarded == 1 and g.max_points == 1 and not g.needs_review
    assert g.method == "drawing"


def test_region_miss_is_wrong_and_confident() -> None:
    g = grade_drawing(_region(), fx.picture_payload([_zig(0.1, 0.1, 0.3, 0.3)]))
    assert g.awarded == 0 and not g.needs_review


def test_region_scribble_outside_is_wrong() -> None:
    strokes = [_zig(0.65, 0.65, 0.85, 0.85), _zig(0.05, 0.05, 0.5, 0.5, 12)]
    v = evaluate_drawing(_region().target, fx.picture_payload(strokes))
    assert not v.correct and v.confidence >= REVIEW_BELOW


def test_region_too_little_ink_is_wrong() -> None:
    v = evaluate_drawing(_region().target, fx.picture_payload([[(0.7, 0.7), (0.75, 0.7)]]))
    assert not v.correct and v.confidence == 1.0


def test_region_just_enough_ink_goes_to_review() -> None:
    g = grade_drawing(_region(), fx.picture_payload([[(0.65, 0.7), (0.865, 0.7)]]))
    assert g.awarded == 1 and g.needs_review and "проверьте" in (g.feedback or "")


def test_region_all_zones_must_get_ink() -> None:
    data = _region().target.model_dump()
    data["zones"].append({"kind": "rect", "x": 0.0, "y": 0.0, "w": 0.2, "h": 0.2})
    two = DrawingRegionTarget.model_validate(data)
    payload = fx.picture_payload([_zig(0.65, 0.65, 0.85, 0.85)])
    assert not evaluate_drawing(two, payload).correct  # second zone empty
    both = fx.picture_payload([_zig(0.65, 0.65, 0.85, 0.85), _zig(0.02, 0.02, 0.18, 0.18)])
    assert evaluate_drawing(two, both).correct


# --------------------------------------------------------------------------- #
# drawing: lines mode (divide the square).
# --------------------------------------------------------------------------- #
L = [[(0.0, 0.5), (0.5, 0.5)], [(0.5, 0.5), (0.5, 1.0)]]


def _map(strokes: list[list[tuple[float, float]]], fn: Any) -> list[list[tuple[float, float]]]:
    return [[fn(p) for p in s] for s in strokes]


def test_lines_exact_and_single_stroke() -> None:
    item = _lines()
    assert grade_drawing(item, fx.square_payload(L)).awarded == 1
    one = grade_drawing(item, fx.square_payload([[(0, 0.5), (0.5, 0.5), (0.5, 1)]]))
    assert one.awarded == 1 and not one.needs_review


def test_lines_jittery_hand_drawing_passes() -> None:
    n = 40
    wobbly = [
        [(0.5 * k / n, 0.5 + (0.015 if k % 2 else -0.015)) for k in range(n + 1)],
        [(0.5 + (0.015 if k % 2 else -0.015), 0.5 + 0.5 * k / n) for k in range(n + 1)],
    ]
    g = grade_drawing(_lines(), fx.square_payload(wobbly))
    assert g.awarded == 1 and not g.needs_review


@pytest.mark.parametrize(
    "fn",
    [
        lambda p: (1 - p[1], p[0]),  # 90
        lambda p: (1 - p[0], 1 - p[1]),  # 180
        lambda p: (p[1], 1 - p[0]),  # 270
    ],
)
def test_lines_rotations_accepted_only_when_allowed(fn: Any) -> None:
    payload = fx.square_payload(_map(L, fn))
    assert grade_drawing(_lines(), payload).awarded == 1
    assert grade_drawing(_lines(allow_transforms=False), payload).awarded == 0


def test_lines_reflection_of_a_chiral_figure() -> None:
    # L + a tick up from the left arm: no rotation maps it onto its mirror image.
    figure = [*L, [(0.25, 0.5), (0.25, 0.3)]]
    target = DrawingLinesTarget.model_validate(
        {**_lines().target.model_dump(), "segments": [{"a": a, "b": b} for a, b in figure]}
    )
    mirrored = fx.square_payload(_map(figure, lambda p: (1 - p[0], p[1])))
    v = evaluate_drawing(target, mirrored)
    assert v.correct and "отражение" in v.detail
    no_flip = target.model_copy(update={"allow_transforms": False})
    assert not evaluate_drawing(no_flip, mirrored).correct
    assert evaluate_drawing(no_flip, fx.square_payload(figure)).correct


@pytest.mark.parametrize(
    ("dx", "ok", "review"),
    [(0.07, True, False), (0.15, True, True), (0.22, False, True), (0.3, False, False)],
)
def test_lines_offset_bands(dx: float, ok: bool, review: bool) -> None:
    # tolerance 0.08 (reach 0.2, far 0.32): the vertical arm drawn dx to the side —
    # within tolerance confident, up to reach accepted for review, beyond rejected.
    strokes = [L[0], [(0.5 + dx, 0.5), (0.5 + dx, 1.0)]]
    g = grade_drawing(_lines(), fx.square_payload(strokes))
    assert (g.awarded == 1) is ok and g.needs_review is review


def test_lines_far_shift_stray_line_and_missing_segment_fail() -> None:
    target = _lines().target
    shifted = _map(L, lambda p: (p[0] + 0.3, p[1] + 0.3))
    for strokes in (shifted, [*L, [(0.0, 0.0), (1.0, 1.0)]], L[:1]):
        v = evaluate_drawing(target, fx.square_payload(strokes))
        assert not v.correct and v.confidence >= REVIEW_BELOW


def test_lines_tracing_the_printed_border_is_ignored() -> None:
    border = [[(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]]
    assert evaluate_drawing(_lines().target, fx.square_payload([*L, *border])).correct
    strict = _lines(ignore_border=False).target
    assert not evaluate_drawing(strict, fx.square_payload([*L, *border])).correct


@pytest.mark.parametrize("end", [0.85, 0.80])
def test_lines_borderline_coverage_goes_to_review(end: float) -> None:
    g = grade_drawing(_lines(), fx.square_payload([L[0], [(0.5, 0.5), (0.5, end)]]))
    assert g.needs_review


def test_drawing_blank_is_zero_without_review() -> None:
    for blank in ("", None, json.dumps({"w": 400, "h": 300, "strokes": []})):
        g = grade_drawing(_lines(), blank)
        assert g.awarded == 0 and not g.needs_review


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[1, 2]",
        '{"w": 0, "h": 10, "strokes": []}',
        '{"w": 10, "h": 10, "strokes": [[[NaN, 1]]]}',
        '{"w": 10, "h": 10, "strokes": [[[1, 2, 3]]]}',
        '{"w": 10, "h": 10, "strokes": [[["1", 2]]]}',
        '{"w": 10, "h": 10, "strokes": [[[100000, 2]]]}',
        json.dumps({"w": 10, "h": 10, "strokes": [[[1, 1]]] * (MAX_STROKES + 1)}),
        json.dumps({"w": 10, "h": 10, "strokes": [[[1, 1]] * 6000]}),
        "x" * 400_000,
    ],
    ids=[
        "not-json",
        "not-object",
        "zero-width",
        "nan",
        "three-coords",
        "string-coord",
        "far-outside",
        "too-many-strokes",
        "too-many-points",
        "too-long",
    ],
)
def test_absurd_payload_is_zero_and_needs_review(payload: str) -> None:
    g = grade_drawing(_lines(), payload)
    assert g.awarded == 0 and g.needs_review and "не читается" in (g.feedback or "")


def test_parse_drawing_reads_canvas_pixels() -> None:
    w, h, strokes = parse_drawing(json.dumps({"w": 400, "h": 300, "strokes": [[[1, 2], [3, 4]]]}))
    assert (w, h, strokes) == (400.0, 300.0, [[(1.0, 2.0), (3.0, 4.0)]])


def test_drawing_grader_is_deterministic() -> None:
    payload = fx.square_payload([L[0], [(0.5, 0.5), (0.5, 0.85)]])
    assert evaluate_drawing(_lines().target, payload) == evaluate_drawing(_lines().target, payload)


# --------------------------------------------------------------------------- #
# Norm scoring (pure).
# --------------------------------------------------------------------------- #
def _norms() -> NormScoring:
    return NormScoring.model_validate(fx.norm_scoring_json(TID))


S1, S2, S3 = f"{TID}-s1", f"{TID}-s2", f"{TID}-s3"


def test_norm_report_full_marks_teen_open_top() -> None:
    r = compute_iq_report({S1: 3, S2: 2, S3: 2}, _norms(), 16)
    assert [s.scaled for s in r.sections] == [10, 12, 10]
    one, two = r.composites
    assert one.scaled_sum == 20 and one.score == 140 and one.score_display == "140+"
    assert one.open_bound == "above"
    assert two.scaled_sum == 12 and two.score == 60 + 4 * 12
    assert r.overall_exact == (140 + 108) / 2 and r.overall == 124
    assert r.overall_display == "124+" and r.bracket == "Above average"
    assert r.age_group == "teen" and r.notes == []


def test_norm_report_by_age_band() -> None:
    raw = {S1: 2, S2: 1, S3: 1}  # scaled 7, 8, 6 -> I = 13, II = 8
    teen = compute_iq_report(raw, _norms(), 19)
    adult = compute_iq_report(raw, _norms(), 20)
    senior = compute_iq_report(raw, _norms(), 75)
    assert [c.score for c in teen.composites] == [112, 92]
    assert [c.score for c in adult.composites] == [107, 87]
    assert [c.score for c in senior.composites] == [117, 97]
    assert adult.overall == 97 and adult.bracket == "Average"
    assert senior.age_group == "senior"


def test_norm_report_open_bottom_and_missing_cell() -> None:
    # I = 1 + 2 = 3 -> adult "<70" (open below), senior None.
    raw = {S1: 0, S2: 0, S3: 0}  # II = 3
    adult = compute_iq_report(raw, _norms(), 30)
    assert adult.composites[0].score_display == "<70"
    assert adult.overall_display == "<70" and adult.composites[0].open_bound == "below"
    senior = compute_iq_report(raw, _norms(), 70)
    assert senior.overall is None
    assert "no_norm_value:I" in senior.notes and "overall_incomplete" in senior.notes


@pytest.mark.parametrize(
    ("age", "notes"),
    [
        (None, ["no_birth_date", "no_norms_for_age"]),
        (13, ["under_min_age", "no_norms_for_age"]),
    ],
)
def test_norm_report_without_norms_still_shows_raw_and_scaled(
    age: int | None, notes: list[str]
) -> None:
    r = compute_iq_report({S1: 3, S2: 2, S3: 2}, _norms(), age)
    assert [s.scaled for s in r.sections] == [10, 12, 10]
    assert [c.scaled_sum for c in r.composites] == [20, 12]
    assert all(c.score is None for c in r.composites)
    assert r.overall is None and r.bracket is None
    assert r.notes == notes


def test_norm_report_no_group_for_age() -> None:
    data = fx.norm_scoring_json(TID)
    data["by_age"]["age_groups"] = data["by_age"]["age_groups"][:2]  # up to 59 only
    r = compute_iq_report({S1: 3, S2: 2, S3: 2}, NormScoring.model_validate(data), 70)
    assert r.notes == ["no_norms_for_age"] and r.overall is None


def test_norm_report_sum_beyond_table_and_raw_beyond_scale() -> None:
    data = fx.norm_scoring_json(TID)
    data["by_age"]["rows"] = [r for r in data["by_age"]["rows"] if r["sum"] <= 15]
    r = compute_iq_report({S1: 5, S2: 2, S3: 2}, NormScoring.model_validate(data), 30)
    assert "raw_above_scale_table:" + S1 in r.notes  # raw 5 > the table's 3
    assert r.sections[0].scaled == 10
    one = r.composites[0]
    assert one.scaled_sum == 20 and "above_table" in r.notes
    assert one.score == 55 + 4 * 15 and one.score_display == f"{55 + 4 * 15}+"


def test_norm_report_missing_section_makes_composite_incomplete() -> None:
    r = compute_iq_report({S1: 3, S2: None, S3: 2}, _norms(), 30)
    assert f"section_not_graded:{S2}" in r.notes and "incomplete_composite:II" in r.notes
    assert r.composites[0].score is not None and r.composites[1].score is None
    assert r.overall is None


def test_norm_report_mixed_bounds_and_rounding() -> None:
    data = fx.norm_scoring_json(TID)
    for row in data["by_age"]["rows"]:
        if row["sum"] == 3:
            row["values"]["teen"] = "<73"
    # I = 20 -> teen "140+", II = 3 -> teen "<73": mixed.
    r = compute_iq_report({S1: 3, S2: 0, S3: 2}, NormScoring.model_validate(data), 15)
    assert "mixed_open_bounds" in r.notes
    assert r.overall_exact == 106.5 and r.overall == 107 and r.overall_display == "107"


def test_norm_helpers() -> None:
    assert parse_cell("130+") == (130, "above", "130+")
    assert parse_cell("<70") == (70, "below", "<70")
    assert parse_cell(101) == (101, None, "101")
    assert parse_cell(None) == (None, None, None)
    bands = [ScaledBand(raw_min=0, raw_max=2, scaled=1), ScaledBand(raw_min=5, raw_max=9, scaled=3)]
    assert scaled_for(3, bands) == (None, "gap")
    assert scaled_for(1, bands) == (1, None)
    assert compute_iq_report({S1: 2.5, S2: 2, S3: 2}, _norms(), 30).sections[0].scaled == 10


def test_age_at() -> None:
    born = date(2010, 6, 15)
    assert age_at(born, date(2026, 6, 14)) == 15
    assert age_at(born, date(2026, 6, 15)) == 16
    assert age_at(born, datetime(2026, 6, 15, 0, 1, tzinfo=UTC)) == 16
    assert age_at(date(2008, 2, 29), date(2026, 2, 28)) == 17
    assert age_at(date(2008, 2, 29), date(2026, 3, 1)) == 18


# --------------------------------------------------------------------------- #
# Editor rules.
# --------------------------------------------------------------------------- #
def _errors(mutate: Any) -> list[str]:
    data = fx.iq_test_json(TID)
    mutate(data)
    return semantic_errors(ExamTest.model_validate(data))


def test_synthetic_iq_test_is_valid() -> None:
    assert semantic_errors(_test()) == []
    assert referenced_asset_ids(_test()) == set(fx.ASSETS)


def _no_limit(d: dict[str, Any]) -> None:
    d["sections"][2]["time_limit_seconds"] = None


def _bad_band(d: dict[str, Any]) -> None:
    d["norm_scoring"]["scaled_tables"][S2][2]["scaled"] = 1


def _gap(d: dict[str, Any]) -> None:
    d["norm_scoring"]["scaled_tables"][S2][1]["raw_min"] = 2
    d["norm_scoring"]["scaled_tables"][S2][1]["raw_max"] = 2
    d["norm_scoring"]["scaled_tables"][S2][2]["raw_min"] = 3
    d["norm_scoring"]["scaled_tables"][S2][2]["raw_max"] = 3


def _unknown_section(d: dict[str, Any]) -> None:
    d["norm_scoring"]["composites"][1]["section_ids"] = ["nope"]


def _no_table(d: dict[str, Any]) -> None:
    del d["norm_scoring"]["scaled_tables"][S2]


def _falling_row(d: dict[str, Any]) -> None:
    d["norm_scoring"]["by_age"]["rows"][5]["values"]["teen"] = 1


def _overlap_age(d: dict[str, Any]) -> None:
    d["norm_scoring"]["by_age"]["age_groups"][1]["min_age"] = 18


def _unknown_group(d: dict[str, Any]) -> None:
    d["norm_scoring"]["by_age"]["rows"][0]["values"]["kids"] = 50


def _square_out(d: dict[str, Any]) -> None:
    d["sections"][1]["items"][1]["target"]["square"]["x"] = 0.5


def _zero_seg(d: dict[str, Any]) -> None:
    d["sections"][1]["items"][1]["target"]["segments"][0] = {"a": [0.2, 0.2], "b": [0.2, 0.2]}


def _no_true_row(d: dict[str, Any]) -> None:
    for it in d["sections"][0]["items"]:
        it["correct"] = False


def _dup_bracket(d: dict[str, Any]) -> None:
    d["norm_scoring"]["brackets"][1]["min"] = 0


def _bad_asset(d: dict[str, Any]) -> None:
    d["sections"][1]["intro"]["images"] = ["../etc/passwd"]


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (_no_limit, "отдельного времени"),
        (_no_limit, "time_limit_seconds"),
        (_bad_band, "убывает"),
        (_gap, "пропуск"),
        (_unknown_section, "несуществующий раздел"),
        (_no_table, "нет таблицы"),
        (_falling_row, "балл убывает при росте суммы"),
        (_overlap_age, "пересекаются"),
        (_unknown_group, "неизвестная возрастная группа"),
        (_square_out, "квадрат выходит"),
        (_zero_seg, "нулевой длины"),
        (_no_true_row, "нет ни одной верной"),
        (_dup_bracket, "одинаковые нижние границы"),
        (_bad_asset, "недопустимое имя файла"),
    ],
)
def test_editor_refuses_broken_iq_content(mutate: Any, needle: str) -> None:
    errors = _errors(mutate)
    assert any(needle in e for e in errors), errors


def _untimed_iq(d: dict[str, Any]) -> None:
    for sec in d["sections"]:
        sec["time_limit_seconds"] = None
        sec["study"] = None


def _partly_timed_english(d: dict[str, Any]) -> None:
    d["level"] = "A2_KEY"
    d["norm_scoring"] = None
    d["sections"][0]["time_limit_seconds"] = None


@pytest.mark.parametrize(
    ("mutate", "needle", "sections"),
    [
        (_no_limit, "в IQ-тесте у каждого раздела", "Разделы 3:"),
        (_untimed_iq, "в IQ-тесте у каждого раздела", "Разделы 1, 2, 3:"),
        (_partly_timed_english, "только у части разделов", "Разделы 1:"),
    ],
)
def test_editor_requires_every_section_clock(mutate: Any, needle: str, sections: str) -> None:
    """Delivery runs section clocks only when EVERY section has one; a partly timed
    test (or an IQ test without clocks) would silently degrade -> refused."""
    errors = _errors(mutate)
    assert any(needle in e and e.startswith(sections) for e in errors), errors


def test_editor_allows_untimed_non_iq_test() -> None:
    def untimed_english(d: dict[str, Any]) -> None:
        _untimed_iq(d)
        d["level"] = "A2_KEY"
        d["norm_scoring"] = None

    assert not any("time_limit_seconds" in e for e in _errors(untimed_english))


def _recall_gap(image: str) -> dict[str, Any]:
    return {
        "item_type": "gap_fill",
        "id": f"{TID}-g1",
        "prompt": "Number for this symbol",
        "image": image,
        "accepted": ["3"],
    }


def test_gap_fill_image_is_a_checked_asset_and_served() -> None:
    from app.delivery.projection import project_item
    from contracts import ClientGapFillItem, GapFillItem

    def bad(d: dict[str, Any]) -> None:
        d["sections"][2]["items"].append(_recall_gap("../x.png"))

    assert any("недопустимое имя файла" in e for e in _errors(bad))
    data = fx.iq_test_json(TID)
    data["sections"][2]["items"].append(_recall_gap("iq-sample.png"))
    test = ExamTest.model_validate(data)
    assert "iq-sample.png" in referenced_asset_ids(test)
    item = test.sections[2].items[-1]
    assert isinstance(item, GapFillItem) and item.image == "iq-sample.png"
    served = project_item(item, None, level=test.level)
    assert isinstance(served, ClientGapFillItem) and served.image == "iq-sample.png"
    assert "accepted" not in served.model_dump()
    # Without an image the field stays None (every older gap_fill).
    assert ClientGapFillItem(id="x", prompt="p").image is None


def test_editor_warns_on_partial_scale_coverage(caplog: pytest.LogCaptureFixture) -> None:
    data = fx.iq_test_json(TID)
    data["norm_scoring"]["scaled_tables"][S2] = data["norm_scoring"]["scaled_tables"][S2][:2]
    with caplog.at_level(logging.WARNING):
        validate_test_data(data)
    assert any("покрывает сырые баллы 0–1" in r.getMessage() for r in caplog.records)


def test_study_text_is_key_bearing_but_intro_text_is_cosmetic() -> None:
    old = _test()
    data = fx.iq_test_json(TID)
    data["sections"][0]["intro"]["text"] = "Put an X on each pair that differs."
    assert is_cosmetic_change(old, ExamTest.model_validate(data))
    data["sections"][2]["study"]["text"] = "cat dog moon"
    assert not is_cosmetic_change(old, ExamTest.model_validate(data))


def test_reid_rebases_norm_section_ids() -> None:
    copy = reid_test(_test(), "iq-copy")
    assert copy.norm_scoring is not None
    assert copy.norm_scoring.composites[0].section_ids == ["iq-copy-s1", "iq-copy-s3"]
    assert set(copy.norm_scoring.scaled_tables) == {"iq-copy-s1", "iq-copy-s2", "iq-copy-s3"}


# --------------------------------------------------------------------------- #
# Import -> persist -> deliver -> grade -> IQ report (+ override recompute).
# --------------------------------------------------------------------------- #
def _content(session: Session, tmp_path: Path) -> ContentService:
    return ContentService(ContentRepository(session), FilesystemStorage(tmp_path))


def test_import_persists_every_iq_field(session: Session, tmp_path: Path) -> None:
    outcome = _content(session, tmp_path).import_test(json.dumps(fx.iq_test_json(TID)), fx.ASSETS)
    assert outcome.test.status == "draft" and outcome.test.level == "IQ"
    session.expire_all()
    stored = ContentRepository(session).get_test(TID)
    assert stored is not None
    assert stored.model_dump(mode="json") == _test().model_dump(mode="json")


def test_gap_fill_image_must_exist_on_import_and_round_trips(
    session: Session, tmp_path: Path
) -> None:
    from app.content.editor import ContentEditError

    data = fx.iq_test_json(TID)
    data["sections"][2]["items"].append(_recall_gap("iq-symbol.png"))
    content = _content(session, tmp_path)
    with pytest.raises(ContentEditError) as info:
        content.import_test(json.dumps(data), fx.ASSETS)
    assert "iq-symbol.png" in str(info.value)
    content.import_test(json.dumps(data), {**fx.ASSETS, "iq-symbol.png": fx.PNG})
    exported = content.export_test(TID)
    assert exported.sections[2].items[-1].model_dump()["image"] == "iq-symbol.png"


def _graded_attempt(
    session: Session, tmp_path: Path, answers: dict[str, str | int], birth: date | None
) -> tuple[GradingService, str]:
    content = _content(session, tmp_path)
    content.import_test(fx.iq_test_json(TID), fx.ASSETS)
    assert content.publish(TID)
    attempts = AttemptRepository(session)
    attempts.add_roster_entry(
        RosterEntry(id="iq-r1", test_id=TID, display_name="Ann", birth_date=birth)
    )
    delivery = DeliveryService(ContentRepository(session), attempts)
    started = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)
    attempt = delivery.start("iq-r1", now=started)
    # Timed sections (Phase 17.2): each subtest is begun, answered, finished in order.
    now = started
    for section in _test().sections:
        delivery.begin_section(attempt.id, section.id, now=now)
        if section.study is not None:
            now += timedelta(seconds=section.study.study_seconds)
        for item in section.items:
            if item.id in answers:
                delivery.save_answer(attempt.id, item.id, answers[item.id], now=now)
        delivery.finish_section(attempt.id, section.id, now=now)
    delivery.submit(attempt.id, now=now)
    return GradingService(ContentRepository(session), attempts), attempt.id


def _answers(d1: str, d2: str) -> dict[str, str | int]:
    marks = {f"{TID}-{rid}": "1" if ok else "0" for rid, ok in fx.PAIR_ROWS}
    marks[f"{TID}-r2"] = "1"  # one false mark: cancels r5
    return {
        **marks,
        f"{TID}-d1": d1,
        f"{TID}-d2": d2,
        f"{TID}-m1": 0,  # displayed index 0 = "cat" (no shuffle) -> right
        f"{TID}-m2": 0,  # "moon" -> wrong
    }


def test_service_grades_and_reports_iq(session: Session, tmp_path: Path) -> None:
    hit = fx.picture_payload([_zig(0.65, 0.65, 0.85, 0.85)])
    svc, attempt_id = _graded_attempt(
        session, tmp_path, _answers(hit, fx.square_payload(L)), date(2010, 1, 1)
    )
    result = svc.grade(attempt_id)
    by_id = {g.item_id: g for g in result.items}
    assert by_id[f"{TID}-r5"].awarded == 0  # cancelled by the false mark on r2
    assert result.score == 2 + 2 + 1 and result.max_score == 3 + 2 + 2
    report = svc.iq_report(result)
    assert report is not None and report.age == 16 and report.age_group == "teen"
    # raw 2 / 2 / 1 -> scaled 7 / 12 / 6; I = 13 -> 112, II = 12 -> 108.
    assert [s.raw for s in report.sections] == [2, 2, 1]
    assert [c.score for c in report.composites] == [112, 108]
    assert report.overall == 110 and report.bracket == "Above average"


def test_override_of_a_review_drawing_recomputes_the_report(
    session: Session, tmp_path: Path
) -> None:
    borderline = fx.square_payload([L[0], [(0.5, 0.5), (0.5, 0.80)]])
    hit = fx.picture_payload([_zig(0.65, 0.65, 0.85, 0.85)])
    svc, attempt_id = _graded_attempt(session, tmp_path, _answers(hit, borderline), None)
    result = svc.grade(attempt_id)
    d2 = next(g for g in result.items if g.item_id == f"{TID}-d2")
    assert d2.awarded == 0 and d2.needs_review and result.needs_review
    assert d2 in svc.review_queue(result)
    before = svc.iq_report(result)
    assert before is not None and before.notes[:2] == ["no_birth_date", "no_norms_for_age"]
    assert before.sections[1].raw == 1 and before.sections[1].scaled == 8
    # The teacher accepts the drawing (existing ManualGrade override path).
    fixed = svc.apply_override(result, f"{TID}-d2", awarded=1)
    assert not fixed.needs_review
    after = svc.iq_report(fixed)
    assert after is not None and after.sections[1].raw == 2 and after.sections[1].scaled == 12
    assert after.composites[1].scaled_sum == 12


def test_non_iq_test_has_no_iq_report(session: Session) -> None:
    from tests.test_delivery_api import _test as english_test

    ContentRepository(session).add_test(english_test())
    attempts = AttemptRepository(session)
    attempts.add_roster_entry(RosterEntry(id="e1", test_id="t1", display_name="Bo"))
    delivery = DeliveryService(ContentRepository(session), attempts)
    attempt = delivery.start("e1")
    svc = GradingService(ContentRepository(session), attempts)
    assert svc.iq_report(svc.grade(attempt.id)) is None


# --------------------------------------------------------------------------- #
# Roster birth date: stored, teacher-only, never on the student roster list.
# --------------------------------------------------------------------------- #
def test_birth_date_round_trip_and_setter(session: Session) -> None:
    ContentRepository(session).add_test(_test())
    attempts = AttemptRepository(session)
    attempts.add_roster_entry(
        RosterEntry(id="b1", test_id=TID, display_name="Ann", birth_date=date(2001, 2, 3))
    )
    entry = attempts.get_roster_entry("b1")
    assert entry is not None and entry.birth_date == date(2001, 2, 3)
    roster = RosterService(attempts)
    assert roster.set_birth_date("b1", None) is True
    assert attempts.get_roster_entry("b1", fresh=True).birth_date is None  # type: ignore[union-attr]
    assert roster.set_birth_date("nope", date(2000, 1, 1)) is False


@pytest.fixture
def api(session: Session) -> Iterator[TestClient]:
    content = ContentRepository(session)
    content.add_test(_test().model_copy(update={"status": "published"}))
    AttemptRepository(session).add_roster_entry(
        RosterEntry(id="b1", test_id=TID, display_name="Ann", birth_date=date(2009, 9, 9))
    )
    session.commit()
    app.dependency_overrides[get_session] = lambda: session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_student_roster_list_never_carries_birth_date(api: TestClient) -> None:
    resp = api.get(f"/exam/tests/{TID}/roster")
    assert resp.status_code == 200
    assert [r["display_name"] for r in resp.json()] == ["Ann"]
    assert "birth" not in resp.text and "2009" not in resp.text
    info = api.get(f"/exam/tests/{TID}/info")
    assert info.status_code == 200 and "2009" not in info.text


# --------------------------------------------------------------------------- #
# Migration e1f2a3b4c5d6 on a scratch DB.
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


def test_migration_adds_and_drops_iq_columns(tmp_path: Path) -> None:
    db = tmp_path / "mig.db"
    _alembic(db, "upgrade", "d0e1f2a3b4c5")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO tests (id, title, level, status, duration_minutes) "
            "VALUES ('t1', 'T', 'A2_KEY', 'published', 30)"
        )
        conn.execute(
            "INSERT INTO roster_entries (id, test_id, display_name, status, pin) "
            "VALUES ('e1', 't1', 'Ann', 'not_started', 'ABCD')"
        )
    _alembic(db, "upgrade", "head")
    assert "norm_scoring" in _columns(db, "tests")
    assert {"time_limit_seconds", "intro", "study"} <= _columns(db, "sections")
    assert "birth_date" in _columns(db, "roster_entries")
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT birth_date FROM roster_entries WHERE id='e1'").fetchone()
    assert row == (None,)
    _alembic(db, "downgrade", "d0e1f2a3b4c5")
    assert "norm_scoring" not in _columns(db, "tests")
    assert not {"time_limit_seconds", "intro", "study"} & _columns(db, "sections")
    assert "birth_date" not in _columns(db, "roster_entries")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM roster_entries").fetchone()[0] == 1
    _alembic(db, "upgrade", "head")
    assert "birth_date" in _columns(db, "roster_entries")
