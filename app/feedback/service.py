"""Feedback engine: per-skill pedagogical strengths & weaknesses for an attempt.

Builds, from the **grading result** + the authoring `Test` + the student's answers,
a per-skill breakdown (reading / listening / writing), computes each skill's score
deterministically, and asks an injected `FeedbackLLM` to write short Russian prose on
what the student did well and what to work on, plus one overall summary.

Boundaries (golden rules #2 / #6): this is the *academic-performance* axis. It reads
grading + content only, never the telemetry/integrity stream, and a feedback verdict
can never move a score — the scores here are copied straight from the `GradingResult`.
Every LLM call logs model id + token usage + latency (golden rule #8).
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.feedback.llm import FeedbackLLM, ItemContext, SkillBreakdown
from contracts import (
    GradingResult,
    OpenWritingItem,
    SkillReport,
    Test,
)

log = get_logger(__name__)


class FeedbackService:
    """Assemble an attempt's per-skill `SkillReport` list + an overall summary."""

    def __init__(self, *, llm: FeedbackLLM | None = None) -> None:
        self.llm = llm

    def build_report(
        self,
        test: Test,
        grading: GradingResult,
        answers: dict[str, str],
    ) -> tuple[list[SkillReport], str, str | None]:
        """Return (per-skill reports, overall summary, model_id).

        Skills are taken from each section; a section with no `skill` is bucketed
        under "writing"/"reading"/... only if labelled, otherwise skipped from the
        per-skill view (its items still counted in the grade elsewhere).
        """
        grades = {g.item_id: g for g in grading.items}
        breakdowns = self._breakdowns(test, grades, answers)

        if not breakdowns:
            log.info("feedback build attempt=%s -> no labelled skills", grading.attempt_id)
            return [], "", None

        if self.llm is None:
            log.warning(
                "feedback build attempt=%s no analyst -> scores only, no prose",
                grading.attempt_id,
            )
            reports = [
                SkillReport(
                    skill=b.skill,  # type: ignore[arg-type]
                    score=round(b.score, 2),
                    max_score=round(b.max_score, 2),
                    strengths="",
                    weaknesses="",
                )
                for b in breakdowns
            ]
            return reports, "", None

        draft = self.llm.analyze(breakdowns)
        prose = {v.skill: v for v in draft.skills}
        reports = [
            SkillReport(
                skill=b.skill,  # type: ignore[arg-type]
                score=round(b.score, 2),
                max_score=round(b.max_score, 2),
                strengths=prose[b.skill].strengths if b.skill in prose else "",
                weaknesses=prose[b.skill].weaknesses if b.skill in prose else "",
            )
            for b in breakdowns
        ]
        log.info(
            "feedback build attempt=%s model=%s skills=%d",
            grading.attempt_id,
            draft.model_id,
            len(reports),
        )
        return reports, draft.summary, draft.model_id

    def _breakdowns(
        self, test: Test, grades: dict, answers: dict[str, str]
    ) -> list[SkillBreakdown]:
        """Group items by section score skill (`reported_skill` or `skill`: B2-C2
        Use of English is its own skill), in test order, with per-item context."""
        by_skill: dict[str, list[ItemContext]] = {}
        score: dict[str, float] = {}
        max_score: dict[str, float] = {}
        order: list[str] = []
        for section in test.sections:
            skill = section.score_skill
            if skill is None:
                continue
            if skill not in by_skill:
                by_skill[skill] = []
                score[skill] = 0.0
                max_score[skill] = 0.0
                order.append(skill)
            for item in section.items:
                grade = grades.get(item.id)
                if grade is None:
                    continue  # e.g. an unchosen optional writing task (dropped in grading)
                score[skill] += grade.awarded
                max_score[skill] += grade.max_points
                is_writing = isinstance(item, OpenWritingItem)
                by_skill[skill].append(
                    ItemContext(
                        prompt=item.prompt,
                        student_answer=answers.get(item.id),
                        awarded=grade.awarded,
                        max_points=grade.max_points,
                        is_correct=(
                            None if is_writing else grade.awarded >= grade.max_points
                        ),
                        item_feedback=grade.feedback,
                    )
                )
        return [
            SkillBreakdown(
                skill=s, score=score[s], max_score=max_score[s], items=by_skill[s]
            )
            for s in order
        ]
