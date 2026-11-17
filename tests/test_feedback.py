"""Feedback engine: per-skill pedagogical strengths & weaknesses.

Gate: skills are grouped from the test's sections with deterministic scores copied
straight from the `GradingResult`; the mock analyst's prose tracks the score ratio;
with no analyst the report is scores-only; the engine imports no integrity/telemetry
module (golden rules #2/#6); the produced `SkillReport`s validate against the schema.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import httpx
import pytest

from app.feedback import FeedbackService, MockFeedbackAnalyst
from app.feedback.llm import ItemContext, SkillBreakdown
from app.feedback.llm_gemini import GeminiFeedbackAnalyst
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


# --------------------------------------------------------------------------- #
# Gemini analyst (mocked httpx): transient failures retried, others raised at once.
# --------------------------------------------------------------------------- #
def _breakdowns() -> list[SkillBreakdown]:
    return [
        SkillBreakdown(
            skill="reading",
            score=1.0,
            max_score=2.0,
            items=[
                ItemContext(
                    prompt="gap 1", student_answer="cat", is_correct=True,
                    awarded=1.0, max_points=1.0,
                )
            ],
        )
    ]


class _Resp:
    def __init__(self, text: str) -> None:
        self._text = text

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {
            "candidates": [{"content": {"parts": [{"text": self._text}]}}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
        }


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://example.invalid/gemini")
    return httpx.HTTPStatusError(
        f"HTTP {code}", request=req, response=httpx.Response(code, request=req)
    )


def _scripted(monkeypatch, outcomes: list) -> tuple[list, list]:  # noqa: ANN001
    calls: list = []
    sleeps: list[float] = []

    def post(*a, **k):  # noqa: ANN001, ANN202
        calls.append(k.get("timeout"))
        out = outcomes[len(calls) - 1]
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr("app.feedback.llm_gemini.httpx.post", post)
    monkeypatch.setattr("app.feedback.llm_gemini._sleep", sleeps.append)
    return calls, sleeps


def test_gemini_feedback_retries_transient_then_parses(monkeypatch, caplog) -> None:  # noqa: ANN001
    ok = _Resp(
        '{"skills": [{"skill": "reading", "strengths": "Хорошо", "weaknesses": "Детали"}],'
        ' "summary": "Итог."}'
    )
    timeout = httpx.ReadTimeout("slow", request=httpx.Request("POST", "https://x"))
    calls, sleeps = _scripted(monkeypatch, [timeout, _status_error(503), ok])
    with caplog.at_level(logging.WARNING):
        draft = GeminiFeedbackAnalyst(api_key="k", model="gemini-test").analyze(_breakdowns())
    assert draft.summary == "Итог." and draft.skills[0].weaknesses == "Детали"
    assert draft.model_id == "gemini-test"
    assert len(calls) == 3 and sleeps == [1.0, 3.0] and calls[0] == 60.0
    retries = [r for r in caplog.records if "retry" in r.message]
    assert len(retries) == 2 and all("gemini-test" in r.message for r in retries)


def test_gemini_feedback_gives_up_and_raises(monkeypatch) -> None:  # noqa: ANN001
    calls, sleeps = _scripted(monkeypatch, [_status_error(429)] * 3)
    with pytest.raises(httpx.HTTPStatusError):
        GeminiFeedbackAnalyst(api_key="k").analyze(_breakdowns())
    assert len(calls) == 3 and sleeps == [1.0, 3.0]


def test_gemini_feedback_client_error_not_retried(monkeypatch) -> None:  # noqa: ANN001
    calls, sleeps = _scripted(monkeypatch, [_status_error(404)])
    with pytest.raises(httpx.HTTPStatusError):
        GeminiFeedbackAnalyst(api_key="k").analyze(_breakdowns())
    assert len(calls) == 1 and sleeps == []


# --------------------------------------------------------------------------- #
# The API key never reaches a URL or a log line (the raised error is scrubbed
# again by app/results before it is stored — see tests/test_results_verdict.py).
# --------------------------------------------------------------------------- #
KEY = "sk-test-SECRET-0123456789abcdef"


def _leaky_status_error(code: int) -> httpx.HTTPStatusError:
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


def test_gemini_feedback_key_in_header_not_in_url(monkeypatch) -> None:  # noqa: ANN001
    seen: dict = {}

    def post(url, **kw):  # noqa: ANN001, ANN202
        seen["url"] = url
        seen["kw"] = kw
        return _Resp('{"skills": [], "summary": "Итог."}')

    monkeypatch.setattr("app.feedback.llm_gemini.httpx.post", post)
    GeminiFeedbackAnalyst(api_key=KEY).analyze(_breakdowns())
    assert seen["kw"]["headers"] == {"x-goog-api-key": KEY}
    assert "params" not in seen["kw"]
    assert KEY not in seen["url"] and "key=" not in seen["url"]


def test_gemini_feedback_429_with_key_in_url_never_leaks_into_logs(monkeypatch, caplog) -> None:  # noqa: ANN001
    assert KEY in str(_leaky_status_error(429))  # the raw text would leak
    calls, sleeps = _scripted(monkeypatch, [_leaky_status_error(429)] * 3)
    with caplog.at_level(logging.DEBUG), pytest.raises(httpx.HTTPStatusError):
        GeminiFeedbackAnalyst(api_key=KEY).analyze(_breakdowns())
    assert len(calls) == 3 and sleeps == [1.0, 3.0]
    logged = "\n".join(r.getMessage() for r in caplog.records) + caplog.text
    assert KEY not in logged
    assert "key=[REDACTED]" in logged
    assert sum("retry" in r.getMessage() for r in caplog.records) == 2
    assert any("gemini feedback failed" in r.getMessage() for r in caplog.records)


def test_gemini_feedback_literal_key_scrubbed_from_logs(monkeypatch, caplog) -> None:  # noqa: ANN001
    _scripted(monkeypatch, [RuntimeError(f"proxy rejected credential {KEY}")])
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError):
        GeminiFeedbackAnalyst(api_key=KEY).analyze(_breakdowns())
    assert KEY not in caplog.text and "[REDACTED]" in caplog.text
