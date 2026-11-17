"""The feedback analyst seam: protocol, value objects, and a deterministic mock.

The analyst turns a per-skill breakdown of how the student performed into short
strengths/weaknesses prose (in Russian, for the teacher) plus one overall summary.
It is a pedagogical judgement over the *grading* result — it never sees any
integrity/cheating signal (golden rules #2/#6).

Scores are computed deterministically by `FeedbackService`; the analyst only writes
the qualitative prose, so a missing/failed LLM never changes a number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ItemContext:
    """One question's outcome, as context for the analyst."""

    prompt: str
    student_answer: str | None
    awarded: float
    max_points: float
    is_correct: bool | None  # None for writing (scored, not pass/fail)
    item_feedback: str | None = None  # examiner feedback for writing, if any


@dataclass(frozen=True)
class SkillBreakdown:
    """How the student did on one skill (reading / listening / writing)."""

    skill: str
    score: float
    max_score: float
    items: list[ItemContext] = field(default_factory=list)


@dataclass(frozen=True)
class SkillVerdict:
    """The analyst's prose for one skill."""

    skill: str
    strengths: str
    weaknesses: str


@dataclass(frozen=True)
class ReportDraft:
    """The analyst's full output: per-skill prose + an overall summary."""

    skills: list[SkillVerdict]
    summary: str
    model_id: str | None = None


class FeedbackLLM(Protocol):
    """Produce qualitative per-skill feedback from the performance breakdown."""

    def analyze(self, breakdowns: list[SkillBreakdown]) -> ReportDraft: ...


def _ratio(b: SkillBreakdown) -> float:
    return b.score / b.max_score if b.max_score else 0.0


class MockFeedbackAnalyst:
    """Deterministic analyst for tests: prose derived from the score ratio.

    No I/O, no clock — same breakdown in, same prose out, so tests are stable and
    the engine has a usable default without a real API key.
    """

    model_id = "mock-feedback"

    def analyze(self, breakdowns: list[SkillBreakdown]) -> ReportDraft:
        skills: list[SkillVerdict] = []
        for b in breakdowns:
            r = _ratio(b)
            if r >= 0.8:
                strengths = f"Сильный результат: {b.score:.0f} из {b.max_score:.0f}."
                weaknesses = "Существенных пробелов не выявлено."
            elif r >= 0.5:
                strengths = f"Базовые навыки освоены: {b.score:.0f} из {b.max_score:.0f}."
                weaknesses = "Есть ошибки — стоит закрепить тему."
            else:
                strengths = "Отдельные верные ответы есть."
                weaknesses = (
                    f"Слабый результат: {b.score:.0f} из {b.max_score:.0f}; нужна проработка."
                )
            skills.append(SkillVerdict(skill=b.skill, strengths=strengths, weaknesses=weaknesses))
        total = sum(b.score for b in breakdowns)
        total_max = sum(b.max_score for b in breakdowns)
        summary = f"Итог: {total:.0f} из {total_max:.0f} по {len(breakdowns)} навыкам."
        return ReportDraft(skills=skills, summary=summary, model_id=self.model_id)
