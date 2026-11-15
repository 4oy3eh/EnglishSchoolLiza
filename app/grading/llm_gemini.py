"""Real `LLMGrader` backed by the Google Gemini API (REST via httpx).

Grades `open_writing` like a Cambridge examiner: given the task, the rubric (which
states the level + what a good answer covers) and the candidate's response, it
returns a 0-5 score and short feedback. Structured JSON output keeps the verdict
machine-readable. Every call logs model id + token usage + latency (golden rule #8).

No SDK dependency — uses `httpx` (already required) so the image stays light, and
the grading engine still depends only on the `LLMGrader` protocol. Constructed only
when `GEMINI_API_KEY` is configured; otherwise writing falls back to manual review.

Choose-one tasks (B1 Writing Part 2 offers two prompts, the student answers one):
an empty response is **skipped** — awarded 0 of max 0 — so the unchosen task does
not drag the score down.
"""

from __future__ import annotations

import json
import time

import httpx

from app.core.logging import get_logger
from app.grading.llm import DEFAULT_WRITING_POINTS, WritingGrade
from contracts import OpenWritingItem

log = get_logger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash"
_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_SYSTEM = (
    "You are an examiner for Cambridge Young Learners / A2 Key / B1 Preliminary "
    "writing tasks. Grade the candidate's response like a teacher marking a student "
    "at that level, against the provided rubric and required content points, on a "
    "0 to {max_points} scale. Mark on BOTH axes together — (a) CONTENT: are all the "
    "required points covered? and (b) LANGUAGE: spelling, grammar, word order and "
    "range. Frequent errors must lower the mark even when every content point is "
    "present. Use these bands (scale 0-5; rescale proportionally if max differs):\n"
    "5 = all content points clearly covered; language well controlled, only "
    "occasional minor slips that never impede understanding.\n"
    "4 = all content points covered; mostly accurate, a few noticeable errors that "
    "rarely impede understanding.\n"
    "3 = all/most content points covered, but FREQUENT errors in spelling/grammar/"
    "word order that sometimes impede understanding.\n"
    "2 = only some content covered, OR errors so frequent they often impede "
    "understanding.\n"
    "1 = very little relevant content, or barely intelligible.\n"
    "0 = blank, off-topic, or impossible to understand.\n"
    "Be a fair examiner for the level (do not demand native perfection), but a "
    "response riddled with basic errors like 'i no have', 'tomorow', 'anser', "
    "'2 clock' belongs in band 2-3, not 5. Return a number in range and one or two "
    "sentences of feedback naming the main strengths and the errors you penalised."
)

_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "score": {"type": "NUMBER"},
        "feedback": {"type": "STRING"},
    },
    "required": ["score", "feedback"],
}

# Transient-failure policy: 2 retries with exponential backoff (1 s, then 3 s) on
# an httpx timeout, a 429, or any 5xx. Everything else (4xx, parse errors) is final.
DEFAULT_TIMEOUT = 60.0
RETRY_DELAYS: tuple[float, ...] = (1.0, 3.0)
_sleep = time.sleep  # module attribute so tests can stub the backoff


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


def _post_with_retry(
    url: str,
    *,
    api_key: str,
    body: dict,
    timeout: float,
    model: str,
    delays: tuple[float, ...],
) -> httpx.Response:
    """POST once, retrying only transient failures; re-raises the last error."""
    for attempt in range(len(delays) + 1):
        try:
            resp = httpx.post(url, params={"key": api_key}, json=body, timeout=timeout)
            resp.raise_for_status()
            return resp
        except Exception as exc:
            if attempt >= len(delays) or not _is_transient(exc):
                raise
            delay = delays[attempt]
            log.warning(
                "gemini grade model=%s transient failure (%s: %s) -> retry %d/%d in %.0fs",
                model,
                type(exc).__name__,
                exc,
                attempt + 1,
                len(delays),
                delay,
            )
            _sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


class GeminiWritingGrader:
    """Rubric-based `open_writing` grader using the Gemini API."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        max_points: float = DEFAULT_WRITING_POINTS,
        timeout: float = DEFAULT_TIMEOUT,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
    ) -> None:
        if not api_key:
            raise ValueError("GeminiWritingGrader requires a non-empty api_key")
        self._api_key = api_key
        self.model = model
        self.max_points = max_points
        self.timeout = timeout
        self.retry_delays = retry_delays

    def grade_writing(self, item: OpenWritingItem, response: str) -> WritingGrade:
        # Blank required task -> 0, flagged for a human. (A blank *optional* task is
        # dropped earlier by the grading service, so it never reaches here.)
        if not response.strip():
            log.info("gemini grade item=%s -> empty response (0, needs_review)", item.id)
            return WritingGrade(
                awarded=0.0,
                max_points=self.max_points,
                feedback="Empty response.",
                needs_review=True,
                model_id=self.model,
            )

        bullets = "\n".join(f"- {b}" for b in item.bullet_points) or "(none)"
        prompt = (
            f"RUBRIC (level + how to mark):\n{item.rubric}\n\n"
            f"TASK PROMPT:\n{item.prompt}\n\n"
            f"REQUIRED CONTENT POINTS:\n{bullets}\n\n"
            f"TARGET LENGTH (words): about {item.word_min}\n"
            f"SCORE RANGE: 0 to {self.max_points}\n\n"
            f"CANDIDATE RESPONSE:\n{response}"
        )
        body = {
            "systemInstruction": {
                "parts": [{"text": _SYSTEM.format(max_points=self.max_points)}]
            },
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": _RESPONSE_SCHEMA,
            },
        }
        url = _ENDPOINT.format(model=self.model)

        start = time.perf_counter()
        try:
            resp = _post_with_retry(
                url,
                api_key=self._api_key,
                body=body,
                timeout=self.timeout,
                model=self.model,
                delays=self.retry_delays,
            )
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            verdict = json.loads(text)
            score = max(0.0, min(float(verdict["score"]), self.max_points))
            feedback = str(verdict.get("feedback", "")).strip() or "(no feedback)"
        except Exception as exc:  # network / parse / API error -> escalate to human
            log.warning("gemini grade item=%s failed (%s) -> needs_review", item.id, exc)
            return WritingGrade(
                awarded=0.0,
                max_points=self.max_points,
                feedback=f"Auto-grading failed ({type(exc).__name__}); needs manual review.",
                needs_review=True,
                model_id=self.model,
            )

        usage = data.get("usageMetadata", {})
        log.info(
            "gemini grade item=%s model=%s score=%.1f/%.1f tokens=%s+%s latency=%.0fms",
            item.id,
            self.model,
            score,
            self.max_points,
            usage.get("promptTokenCount", "?"),
            usage.get("candidatesTokenCount", "?"),
            (time.perf_counter() - start) * 1000,
        )
        return WritingGrade(
            awarded=round(score, 1),
            max_points=self.max_points,
            feedback=feedback,
            needs_review=False,
            model_id=self.model,
        )
