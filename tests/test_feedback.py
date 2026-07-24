"""Feedback engine: per-skill pedagogical strengths & weaknesses.

Gate: skills are grouped from the test's sections with deterministic scores copied
straight from the `GradingResult`; the mock analyst's prose tracks the score ratio;
with no analyst the report is scores-only; the engine imports no integrity/telemetry
module (golden rules #2/#6); the produced `SkillReport`s validate against the schema.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.feedback import FeedbackService, MockFeedbackAnalyst
from contracts import (
    GapFillItem,
    GradingResult,
    ItemGrade,
    OpenWritingItem,
    PassageTextStimulus,
    Section,
    SkillReport,
)
from contracts import Test as ExamTest


def _test() -> ExamTest:
    return ExamTest(
        id="t1",
        title="B1 — t1",
        level="B1_PRELIMINARY",
        duration_minutes=60,
        sections=[
            Section(
                id="read",
                skill="reading",
                stimulus=PassageTextStimulus(text="Read."),
                items=[
                    GapFillItem(id="r1", prompt="gap 1", accepted=["cat"]),
                    GapFillItem(id="r2", prompt="gap 2", accepted=["dog"]),
                ],
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


def _grading() -> GradingResult:
    # reading: 2/2 (strong); writing: 1/5 (weak), carries examiner feedback.
    items = [
        ItemGrade(item_id="r1", awarded=1.0, max_points=1.0, method="gap_fill"),
        ItemGrade(item_id="r2", awarded=1.0, max_points=1.0, method="gap_fill"),
        ItemGrade(
            item_id="w1",
            awarded=1.0,
            max_points=5.0,
            method="open_writing_llm",
            feedback="Many basic errors.",
        ),
    ]
    return GradingResult(
        attempt_id="att1", items=items, score=3.0, max_score=7.0, needs_review=False
    )


def test_per_skill_grouping_and_scores() -> None:
    reports, summary, model_id = FeedbackService(llm=MockFeedbackAnalyst()).build_report(
        _test(), _grading(), {"r1": "cat", "r2": "dog", "w1": "hi i am here"}
    )
    by_skill = {r.skill: r for r in reports}
    assert set(by_skill) == {"reading", "writing"}
    # Scores are copied from the GradingResult, never invented by the analyst.
    assert (by_skill["reading"].score, by_skill["reading"].max_score) == (2.0, 2.0)
    assert (by_skill["writing"].score, by_skill["writing"].max_score) == (1.0, 5.0)
    assert model_id == "mock-feedback"
    assert summary  # overall summary present


def test_mock_prose_tracks_ratio() -> None:
    reports, _, _ = FeedbackService(llm=MockFeedbackAnalyst()).build_report(
        _test(), _grading(), {}
    )
    by_skill = {r.skill: r for r in reports}
    # Strong reading -> no real weakness; weak writing -> a real "work on" note.
    assert "пробел" in by_skill["reading"].weaknesses.lower() or by_skill["reading"].strengths
    assert by_skill["writing"].weaknesses
    # Every report validates against the schema (round-trips cleanly).
    for r in reports:
        assert SkillReport.model_validate(r.model_dump())


def test_no_analyst_scores_only() -> None:
    reports, summary, model_id = FeedbackService(llm=None).build_report(
        _test(), _grading(), {}
    )
    assert model_id is None and summary == ""
    assert all(r.strengths == "" and r.weaknesses == "" for r in reports)
    # Scores are still present without any LLM.
    assert {r.skill: r.score for r in reports} == {"reading": 2.0, "writing": 1.0}


def test_engine_imports_no_integrity_or_telemetry() -> None:
    """Golden rules #2/#6: feedback is the academic axis; no cheating-signal import."""
    forbidden = ("app.integrity", "app.telemetry", "app.analysis")
    root = Path(__file__).resolve().parents[1] / "app" / "feedback"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            mod = (
                node.module
                if isinstance(node, ast.ImportFrom)
                else None
            )
            names = (
                [a.name for a in node.names] if isinstance(node, ast.Import) else []
            )
            for target in ([mod] if mod else []) + names:
                assert not any(target == f or target.startswith(f + ".") for f in forbidden), (
                    f"{path.name} imports {target}"
                )
