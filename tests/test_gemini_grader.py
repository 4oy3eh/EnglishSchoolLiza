"""GeminiWritingGrader — rubric-based writing auto-grading (mocked HTTP).

The real grader calls the Gemini REST API; here we monkeypatch `httpx.post` so the
parsing/clamping/escalation logic is covered without a network call or key.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.grading.llm_gemini import GeminiWritingGrader
from contracts import OpenWritingItem


def _item() -> OpenWritingItem:
    return OpenWritingItem(
        id="w1",
        prompt="Write an email to a friend.",
        word_min=25,
        bullet_points=["say when", "say where"],
        rubric="A2 Writing (max 5). Reward covering the points; tolerate small errors.",
    )


class _Resp:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._payload


def _gemini_payload(text: str) -> dict[str, Any]:
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    }


def test_requires_api_key() -> None:
    with pytest.raises(ValueError):
        GeminiWritingGrader(api_key="")


def test_grades_and_parses_score(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.grading.llm_gemini.httpx.post",
        lambda *a, **k: _Resp(_gemini_payload('{"score": 4, "feedback": "Good email."}')),
    )
    grade = GeminiWritingGrader(api_key="k").grade_writing(_item(), "Hi! Let's meet...")
    assert grade.awarded == 4.0
    assert grade.max_points == 5.0
    assert grade.needs_review is False
    assert grade.feedback == "Good email."
    assert grade.model_id


def test_score_is_clamped_to_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.grading.llm_gemini.httpx.post",
        lambda *a, **k: _Resp(_gemini_payload('{"score": 9.9, "feedback": "x"}')),
    )
    grade = GeminiWritingGrader(api_key="k").grade_writing(_item(), "answer")
    assert grade.awarded == 5.0  # clamped to max_points


def test_blank_required_response_flags_review() -> None:
    grade = GeminiWritingGrader(api_key="k").grade_writing(_item(), "   ")
    assert grade.awarded == 0.0
    assert grade.max_points == 5.0
    assert grade.needs_review is True


def test_api_error_escalates_to_human(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("network down")

    monkeypatch.setattr("app.grading.llm_gemini.httpx.post", boom)
    grade = GeminiWritingGrader(api_key="k").grade_writing(_item(), "answer")
    assert grade.needs_review is True
    assert grade.awarded == 0.0
