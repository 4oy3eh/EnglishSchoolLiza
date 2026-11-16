"""GeminiWritingGrader — rubric-based writing auto-grading (mocked HTTP).

The real grader calls the Gemini REST API; here we monkeypatch `httpx.post` so the
parsing/clamping/escalation logic is covered without a network call or key.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
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


# --------------------------------------------------------------------------- #
# Transient failures are retried (timeout / 429 / 5xx), others are final.
# --------------------------------------------------------------------------- #
def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://example.invalid/gemini")
    return httpx.HTTPStatusError(
        f"HTTP {code}", request=req, response=httpx.Response(code, request=req)
    )


def _timeout() -> httpx.ReadTimeout:
    return httpx.ReadTimeout("read timed out", request=httpx.Request("POST", "https://x"))


def _scripted(monkeypatch: pytest.MonkeyPatch, outcomes: list) -> tuple[list, list]:
    """httpx.post yields each outcome in turn (raise if an exception); sleeps recorded."""
    calls: list = []
    sleeps: list[float] = []

    def post(*a: Any, **k: Any) -> _Resp:
        calls.append(k.get("timeout"))
        out = outcomes[len(calls) - 1]
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr("app.grading.llm_gemini.httpx.post", post)
    monkeypatch.setattr("app.grading.llm_gemini._sleep", sleeps.append)
    return calls, sleeps


def test_retries_5xx_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ok = _Resp(_gemini_payload('{"score": 3, "feedback": "ok"}'))
    calls, sleeps = _scripted(monkeypatch, [_status_error(503), _status_error(429), ok])
    with caplog.at_level(logging.WARNING):
        grade = GeminiWritingGrader(api_key="k", model="gemini-test").grade_writing(
            _item(), "answer"
        )
    assert grade.awarded == 3.0 and grade.needs_review is False
    assert len(calls) == 3
    assert sleeps == [1.0, 3.0]  # exponential backoff between the retries
    assert calls[0] == 60.0  # explicit httpx timeout
    retries = [r for r in caplog.records if "retry" in r.message]
    assert len(retries) == 2 and all("gemini-test" in r.message for r in retries)


def test_retries_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    ok = _Resp(_gemini_payload('{"score": 5, "feedback": "ok"}'))
    calls, sleeps = _scripted(monkeypatch, [_timeout(), ok])
    grade = GeminiWritingGrader(api_key="k").grade_writing(_item(), "answer")
    assert grade.awarded == 5.0 and len(calls) == 2 and sleeps == [1.0]


def test_gives_up_after_two_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, sleeps = _scripted(monkeypatch, [_status_error(500)] * 3)
    grade = GeminiWritingGrader(api_key="k").grade_writing(_item(), "answer")
    assert grade.needs_review is True and grade.awarded == 0.0
    assert len(calls) == 3 and sleeps == [1.0, 3.0]
    assert "HTTPStatusError" in grade.feedback


def test_client_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, sleeps = _scripted(monkeypatch, [_status_error(400)])
    grade = GeminiWritingGrader(api_key="k").grade_writing(_item(), "answer")
    assert grade.needs_review is True
    assert len(calls) == 1 and sleeps == []


# --------------------------------------------------------------------------- #
# The API key never reaches a URL, a log line or the stored item feedback.
# --------------------------------------------------------------------------- #
KEY = "sk-test-SECRET-0123456789abcdef"


def _leaky_status_error(code: int, key: str = KEY) -> httpx.HTTPStatusError:
    """What httpx raises for a request whose URL carries `?key=...`."""
    req = httpx.Request(
        "POST",
        f"https://generativelanguage.googleapis.com/v1beta/models/m:generateContent?key={key}",
    )
    try:
        httpx.Response(code, request=req).raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("unreachable")


def test_key_is_sent_in_the_header_and_never_in_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def post(url: str, **kw: Any) -> _Resp:
        seen["url"] = url
        seen["kw"] = kw
        return _Resp(_gemini_payload('{"score": 4, "feedback": "ok"}'))

    monkeypatch.setattr("app.grading.llm_gemini.httpx.post", post)
    GeminiWritingGrader(api_key=KEY).grade_writing(_item(), "answer")
    assert seen["kw"]["headers"] == {"x-goog-api-key": KEY}
    assert "params" not in seen["kw"]
    assert KEY not in seen["url"] and "key=" not in seen["url"]


def test_429_with_key_in_url_never_leaks_into_logs_or_feedback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    assert KEY in str(_leaky_status_error(429))  # the raw text would leak
    calls, sleeps = _scripted(monkeypatch, [_leaky_status_error(429)] * 3)
    with caplog.at_level(logging.DEBUG):
        grade = GeminiWritingGrader(api_key=KEY).grade_writing(_item(), "answer")
    assert len(calls) == 3 and sleeps == [1.0, 3.0]
    assert grade.needs_review is True
    assert KEY not in grade.feedback and "key=" not in grade.feedback
    logged = "\n".join(r.getMessage() for r in caplog.records) + caplog.text
    assert KEY not in logged
    assert "key=[REDACTED]" in logged  # both retry lines and the final failure line
    assert sum("retry" in r.getMessage() for r in caplog.records) == 2


def test_literal_key_in_exception_text_is_scrubbed_from_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _scripted(monkeypatch, [RuntimeError(f"proxy rejected credential {KEY}")])
    with caplog.at_level(logging.DEBUG):
        grade = GeminiWritingGrader(api_key=KEY).grade_writing(_item(), "answer")
    assert grade.needs_review is True and KEY not in grade.feedback
    assert KEY not in caplog.text and "[REDACTED]" in caplog.text
