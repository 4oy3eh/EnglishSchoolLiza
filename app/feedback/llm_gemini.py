"""Real `FeedbackLLM` backed by the Google Gemini API (REST via httpx).

Given a per-skill performance breakdown, it writes — in Russian, for the teacher —
short strengths/weaknesses for each skill plus one overall summary. Structured JSON
output keeps it machine-readable. Every call logs model id + token usage + latency
(golden rule #8).

No SDK dependency (httpx only), mirroring `app/grading/llm_gemini.py`. Constructed
only when `GEMINI_API_KEY` is configured; otherwise the service falls back to a
scores-only report (no prose).
"""

from __future__ import annotations

import json
import time

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


class GeminiFeedbackAnalyst:
    """Per-skill pedagogical feedback (Russian) via the Gemini API."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout: float = 30.0,
    ) -> None:
        if not api_key:
            raise ValueError("GeminiFeedbackAnalyst requires a non-empty api_key")
        self._api_key = api_key
        self.model = model
        self.timeout = timeout

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
            resp = httpx.post(
                url, params={"key": self._api_key}, json=body, timeout=self.timeout
            )
            resp.raise_for_status()
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
            log.warning("gemini feedback failed (%s)", exc)
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
