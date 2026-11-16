"""Real `FeedbackLLM` backed by the Google Gemini API (REST via httpx).

Given a per-skill performance breakdown, it writes — in Russian, for the teacher —
short strengths/weaknesses for each skill plus one overall summary. Structured JSON
output keeps it machine-readable. Every call logs model id + token usage + latency
(golden rule #8).

No SDK dependency (httpx only), mirroring `app/grading/llm_gemini.py`. Constructed
only when `GEMINI_API_KEY` is configured; otherwise the service falls back to a
scores-only report (no prose).

The API key is sent in the `x-goog-api-key` header, never as a URL parameter (an
httpx error string prints the URL), and exception text is scrubbed (`_redact`)
before it is logged. The exception itself is re-raised; `app/results` scrubs it
again before storing `AttemptReport.error`.
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import quote

import httpx

from app.core.logging import get_logger
from app.feedback.llm import ReportDraft, SkillBreakdown, SkillVerdict

log = get_logger(__name__)

DEFAULT_MODEL = "gemini-2.5-flash"
_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_SYSTEM = (
    "Ты — преподаватель английского, который анализирует результат ученика по "
    "Cambridge-тесту (A2 Key / B1 Preliminary). На вход — разбивка по навыкам "
    "(reading / listening / writing) с баллами и по каждому вопросу: формулировка, "
    "ответ ученика, верно/неверно (для writing — текст ответа и комментарий "
    "экзаменатора). Для КАЖДОГО навыка напиши кратко (1-3 предложения) на РУССКОМ "
    "языке: 'strengths' — что получилось хорошо, и 'weaknesses' — что западает и над "
    "чем работать (грамматика, лексика, понимание на слух, чтение деталей и т.п.). "
    "Будь конкретным и опирайся на данные, не выдумывай. В конце дай 'summary' — "
    "общий вывод по работе (1-2 предложения). Не оценивай честность/списывание — "
    "только учебный результат."
)

_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "skills": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "skill": {"type": "STRING"},
                    "strengths": {"type": "STRING"},
                    "weaknesses": {"type": "STRING"},
                },
                "required": ["skill", "strengths", "weaknesses"],
            },
        },
        "summary": {"type": "STRING"},
    },
    "required": ["skills", "summary"],
}

# Transient-failure policy (mirrors `app/grading/llm_gemini.py`, kept local so the
# engines stay independent): 2 retries, backoff 1 s then 3 s, on an httpx timeout,
# a 429 or any 5xx. Everything else (4xx, parse errors) is final.
DEFAULT_TIMEOUT = 60.0
RETRY_DELAYS: tuple[float, ...] = (1.0, 3.0)
_sleep = time.sleep  # module attribute so tests can stub the backoff

# --------------------------------------------------------------------------- #
# Secret redaction (engine-local copy — also in app/analysis/redact.py and
# app/results/service.py; duplicated on purpose, like the retry code, so the
# engines stay independent). An httpx exception string carries the request URL;
# the key is sent in a header, and every exception string logged here is scrubbed
# as a second line of defence.
# --------------------------------------------------------------------------- #
_REDACTED = "[REDACTED]"
_MIN_SECRET_CHARS = 8  # a shorter "secret" would shred ordinary words
_KEY_PARAM_RE = re.compile(r"(?i)((?:[?&]|\b)(?:api[_-]?key|key)=)[^&\s'\"<>]+")
_KEY_HEADER_RE = re.compile(r"(?i)(x-goog-api-key['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+")
_GOOGLE_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")


def _redact(text: object, api_key: str | None = None) -> str:
    """`text` without credentials: the literal key and any `key=<...>` / key shape."""
    out = str(text)
    if api_key and len(api_key) >= _MIN_SECRET_CHARS:
        out = out.replace(api_key, _REDACTED).replace(quote(api_key, safe=""), _REDACTED)
    out = _KEY_PARAM_RE.sub(lambda m: m.group(1) + _REDACTED, out)
    out = _KEY_HEADER_RE.sub(lambda m: m.group(1) + _REDACTED, out)
    return _GOOGLE_KEY_RE.sub(_REDACTED, out)


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
            # The key goes in a header, NEVER in the URL: httpx exception strings
            # carry the URL and end up in logs / stored report errors.
            resp = httpx.post(
                url, headers={"x-goog-api-key": api_key}, json=body, timeout=timeout
            )
            resp.raise_for_status()
            return resp
        except Exception as exc:
            if attempt >= len(delays) or not _is_transient(exc):
                raise
            delay = delays[attempt]
            log.warning(
                "gemini feedback model=%s transient failure (%s: %s) -> retry %d/%d in %.0fs",
                model,
                type(exc).__name__,
                _redact(exc, api_key),
                attempt + 1,
                len(delays),
                delay,
            )
            _sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


class GeminiFeedbackAnalyst:
    """Per-skill pedagogical feedback (Russian) via the Gemini API."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        retry_delays: tuple[float, ...] = RETRY_DELAYS,
    ) -> None:
        if not api_key:
            raise ValueError("GeminiFeedbackAnalyst requires a non-empty api_key")
        self._api_key = api_key
        self.model = model
        self.timeout = timeout
        self.retry_delays = retry_delays

    def analyze(self, breakdowns: list[SkillBreakdown]) -> ReportDraft:
        prompt = self._render(breakdowns)
        body = {
            "systemInstruction": {"parts": [{"text": _SYSTEM}]},
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.3,
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
            parsed = json.loads(text)
            skills = [
                SkillVerdict(
                    skill=str(s["skill"]),
                    strengths=str(s.get("strengths", "")).strip(),
                    weaknesses=str(s.get("weaknesses", "")).strip(),
                )
                for s in parsed.get("skills", [])
            ]
            summary = str(parsed.get("summary", "")).strip()
        except Exception as exc:  # network / parse / API error -> let caller mark failed
            log.warning(
                "gemini feedback failed (%s: %s)",
                type(exc).__name__,
                _redact(exc, self._api_key),
            )
            raise

        usage = data.get("usageMetadata", {})
        log.info(
            "gemini feedback model=%s skills=%d tokens=%s+%s latency=%.0fms",
            self.model,
            len(skills),
            usage.get("promptTokenCount", "?"),
            usage.get("candidatesTokenCount", "?"),
            (time.perf_counter() - start) * 1000,
        )
        return ReportDraft(skills=skills, summary=summary, model_id=self.model)

    @staticmethod
    def _render(breakdowns: list[SkillBreakdown]) -> str:
        lines: list[str] = []
        for b in breakdowns:
            lines.append(f"\n## Навык: {b.skill} — балл {b.score:.0f}/{b.max_score:.0f}")
            for i, it in enumerate(b.items, 1):
                ans = it.student_answer if it.student_answer not in (None, "") else "(нет ответа)"
                if it.is_correct is None:  # writing
                    fb = f" | комментарий: {it.item_feedback}" if it.item_feedback else ""
                    lines.append(
                        f"{i}. [writing {it.awarded:.0f}/{it.max_points:.0f}] "
                        f"{it.prompt}\n   ответ: {ans}{fb}"
                    )
                else:
                    mark = "верно" if it.is_correct else "неверно"
                    lines.append(f"{i}. [{mark}] {it.prompt} — ответ: {ans}")
        return "\n".join(lines)
