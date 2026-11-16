"""The deterministic side of the advisory verdict (no LLM) + the floor under an LLM.

The verdict of record is the integrity layer's rule engine
(`app.integrity.rules`): this module only maps its `RuleReport` onto the
`VerdictDraft` shape the service stores, builds the per-writing-item
`WritingAssessment`s from the same rules, and floors an LLM second opinion so it
can never undercut the deterministic evidence. Pure: same profile + report ->
same draft. It re-derives nothing — every number comes from `app.integrity`.

* `suspicion_score` / `confidence` / `flags` / `summary` = the rule report's
  (`RuleReport.suspicion_score`, `.confidence`, `.codes()`, `.summary_ru()`);
* per writing item: ``likelihood = rules.combine(severity of each of its flags)``
  — the same bounded combination the engine uses for the whole attempt, capped at
  ``MAX_LIKELIHOOD`` (0.95), with the rule titles as the Russian rationale;
* the floor (`apply_deterministic_floor`): whatever an analyst returns for a
  writing item, ``ai_assist_likelihood_i = max(llm_i, LLM_FLOOR_SHARE x rules_i)``
  (0.8). A model that was talked into "likelihood 0" by the examinee's text
  (prompt injection), or that ignored the telemetry, cannot clear a flagged item.
  The overall suspicion needs no floor: it is never taken from a model.

Advisory only and score-free (golden rule #2): nothing here sees or moves a grade.
"""

from __future__ import annotations

from dataclasses import replace

from app.analysis.llm import VerdictDraft
from app.integrity.rules import (
    MAX_SUSPICION,
    RULE_TITLES,
    WRITING_RULE_SEVERITY,
    RuleReport,
    combine,
)
from contracts import IntegrityProfile, WritingAssessment, WritingFeatures

# An LLM second opinion may not fall below this share of the rule engine's
# per-writing-item likelihood — see `apply_deterministic_floor`.
LLM_FLOOR_SHARE = 0.8
MAX_LIKELIHOOD = MAX_SUSPICION  # never assert certainty


def _r(value: float) -> float:
    return round(value, 4)


def writing_likelihood(features: WritingFeatures) -> float:
    """Deterministic outside-help likelihood for one writing item, in [0, 0.95].

    The rule engine's combination over this item's flags (each flag is one rule
    hit; an unknown flag weighs nothing).
    """
    return combine(
        WRITING_RULE_SEVERITY[f] for f in features.flags if f in WRITING_RULE_SEVERITY
    )


def _rationale(features: WritingFeatures) -> str:
    titles = [RULE_TITLES[f] for f in features.flags if f in RULE_TITLES]
    if not titles:
        if not features.has_keystroke_telemetry:
            return "Нет телеметрии набора для этого ответа; правила ничего не нашли."
        return "Правила не нашли признаков посторонней помощи."
    return f"Сработавшие правила: {'; '.join(titles)}. Требуется проверка учителем."


def writing_assessments(profile: IntegrityProfile) -> list[WritingAssessment]:
    """One deterministic `WritingAssessment` per writing item in the profile."""
    return [
        WritingAssessment(
            item_id=w.item_id,
            ai_assist_likelihood=writing_likelihood(w),
            signals=list(w.flags),
            rationale=_rationale(w),
        )
        for w in profile.writing
    ]


def deterministic_verdict(profile: IntegrityProfile, report: RuleReport) -> VerdictDraft:
    """The rule engine's verdict as a `VerdictDraft` (`model_id=None`)."""
    return VerdictDraft(
        suspicion_score=report.suspicion_score,
        confidence=report.confidence,
        flags=tuple(report.codes()),
        summary=report.summary_ru(),
        model_id=None,
        writing=tuple(writing_assessments(profile)),
    )


def apply_deterministic_floor(draft: VerdictDraft, profile: IntegrityProfile) -> VerdictDraft:
    """Raise an analyst's per-item likelihoods to >= ``LLM_FLOOR_SHARE`` x the rules'.

    ``ai_assist_likelihood_i = max(llm_i, 0.8 * rules_i)`` for every writing item
    the profile knows. Never lowers anything and never exceeds ``MAX_LIKELIHOOD``
    (the rule combination is capped). Pure; returns `draft` itself when nothing
    had to move.
    """
    floors = {w.item_id: _r(LLM_FLOOR_SHARE * writing_likelihood(w)) for w in profile.writing}
    writing = tuple(
        w.model_copy(update={"ai_assist_likelihood": floors[w.item_id]})
        if floors.get(w.item_id, 0.0) > w.ai_assist_likelihood
        else w
        for w in draft.writing
    )
    if writing == draft.writing:
        return draft
    return replace(draft, writing=writing)
