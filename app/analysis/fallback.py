"""Deterministic-only advisory verdict (no LLM) — the offline / failure fallback.

Used when no analyst is configured (no `GEMINI_API_KEY`) or when the LLM call
fails: the feature keeps working offline, with a documented formula over the
integrity layer's deterministic flags. Pure: same profile + segments -> same draft.

Formula (documented in `app/analysis/CLAUDE.md` — keep in sync):

* per writing item: noisy-OR over its integrity flags,
  ``likelihood = 1 - prod(1 - FLAG_WEIGHTS[f])``, capped at ``MAX_LIKELIHOOD``;
* attempt-level behavioural base:
  ``base = 0.4 * systematicity_rate + 0.25 * min(total_hidden_ms / 120 000, 1)
  + 0.1 * device_changed``;
* ``suspicion = min(MAX_LIKELIHOOD, 1 - (1 - base) * prod(1 - likelihood_i))``;
* ``confidence`` is deliberately LOW: ``CONFIDENCE_WITH_EVIDENCE`` (0.25) when
  there is anything to go on (answered questions or writing telemetry), else
  ``CONFIDENCE_NO_EVIDENCE`` (0.1).

The same formula is also the FLOOR under any LLM verdict
(`apply_deterministic_floor`): the deterministic features + flags dominate, so a
model that was talked into "suspicion 0" by the examinee's text (prompt injection)
or that simply ignored the telemetry cannot report less than
``LLM_FLOOR_SHARE`` (0.8) of what the formula gives — per writing item and overall.

Advisory only and score-free (golden rule #2): nothing here sees or moves a grade.
"""

from __future__ import annotations

from dataclasses import replace

from app.analysis.llm import VerdictDraft
from app.analysis.segments import FlaggedSegment
from contracts import IntegrityProfile, WritingAssessment, WritingFeatures

# Weight of each integrity writing flag in the noisy-OR (behavioural evidence is
# strong; leaving fullscreen alone is weak).
FLAG_WEIGHTS: dict[str, float] = {
    "text_appeared_without_typing": 0.5,
    "scripted_input": 0.5,
    "typing_speed_implausible": 0.3,
    "large_paste_attempt": 0.25,
    "away_while_writing": 0.25,
    "no_input_telemetry": 0.15,
    # `text_appeared_without_typing` downgraded by the integrity layer: the text is
    # unexplained only because keystroke telemetry was lost (reload / dropped batch).
    "telemetry_gap": 0.15,
    "fullscreen_exit_while_writing": 0.1,
}
# An LLM verdict may not fall below this share of the deterministic formula
# (per writing item and overall) — see `apply_deterministic_floor`.
LLM_FLOOR_SHARE = 0.8
HIDDEN_REF_MS = 120_000  # ~2 min hidden saturates the "left the page" signal
MAX_LIKELIHOOD = 0.95  # never assert certainty
CONFIDENCE_WITH_EVIDENCE = 0.25
CONFIDENCE_NO_EVIDENCE = 0.1

# Russian phrases for the teacher (rationale / summary).
_FLAG_RU: dict[str, str] = {
    "text_appeared_without_typing": "текст появился без набора с клавиатуры",
    "scripted_input": "ввод сгенерирован скриптом",
    "typing_speed_implausible": "неправдоподобно высокая скорость набора",
    "large_paste_attempt": "попытка вставить большой фрагмент текста",
    "away_while_writing": "уход со страницы во время письма",
    "no_input_telemetry": "текст сохранён без зафиксированного ввода",
    "telemetry_gap": (
        "часть текста не подтверждена записью набора — телеметрия прерывалась "
        "(перезагрузка страницы или потеря данных), слабый признак"
    ),
    "fullscreen_exit_while_writing": "выход из полноэкранного режима во время письма",
}
_SEGMENT_RU: dict[str, str] = {
    "long_hidden": "долгий уход со вкладки",
    "fast_post_return": "мгновенные ответы сразу после возвращения на вкладку",
    "writing_flagged": "подозрительные признаки при письме",
}


def _r(value: float) -> float:
    return round(value, 4)


def _noisy_or(weights: list[float]) -> float:
    keep = 1.0
    for w in weights:
        keep *= 1.0 - w
    return 1.0 - keep


def writing_likelihood(features: WritingFeatures) -> float:
    """Deterministic outside-help likelihood for one writing item, in [0, 0.95]."""
    return _r(min(MAX_LIKELIHOOD, _noisy_or([FLAG_WEIGHTS.get(f, 0.0) for f in features.flags])))


def _rationale(features: WritingFeatures) -> str:
    if not features.flags:
        if not features.has_keystroke_telemetry:
            return "Нет телеметрии набора для этого ответа; поведенческих признаков нет."
        return "Поведенческих признаков посторонней помощи не обнаружено."
    phrases = ", ".join(_FLAG_RU.get(f, f) for f in features.flags)
    return f"Детерминированные признаки: {phrases}. Требуется проверка учителем."


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


def deterministic_flags(profile: IntegrityProfile, segments: list[FlaggedSegment]) -> list[str]:
    """Distinct segment kinds, then writing flags, then device change (fixed order)."""
    flags: list[str] = []
    for seg in segments:
        if seg.kind not in flags:
            flags.append(seg.kind)
    for w in profile.writing:
        for f in w.flags:
            if f not in flags:
                flags.append(f)
    if profile.device_changed and "device_changed" not in flags:
        flags.append("device_changed")
    return flags


def deterministic_verdict(
    profile: IntegrityProfile, segments: list[FlaggedSegment]
) -> VerdictDraft:
    """The documented formula above, as a `VerdictDraft` with `model_id=None`."""
    writing = writing_assessments(profile)
    hidden_signal = min(profile.total_hidden_ms / HIDDEN_REF_MS, 1.0)
    base = min(
        1.0,
        0.4 * profile.systematicity_rate
        + 0.25 * hidden_signal
        + (0.1 if profile.device_changed else 0.0),
    )
    keep = 1.0 - base
    for w in writing:
        keep *= 1.0 - w.ai_assist_likelihood
    suspicion = _r(min(MAX_LIKELIHOOD, 1.0 - keep))

    has_evidence = bool(profile.question_timings) or any(
        w.has_keystroke_telemetry for w in profile.writing
    )
    confidence = CONFIDENCE_WITH_EVIDENCE if has_evidence else CONFIDENCE_NO_EVIDENCE

    flags = deterministic_flags(profile, segments)
    if not flags:
        summary = "Детерминированная оценка (без LLM): заметных признаков нарушений нет."
    else:
        parts = [_SEGMENT_RU.get(f) or _FLAG_RU.get(f) or f for f in flags]
        if "device_changed" in flags:
            parts[flags.index("device_changed")] = "тест продолжен на другом устройстве"
        summary = (
            "Детерминированная оценка (без LLM): "
            + "; ".join(parts)
            + f". Вне вкладки {profile.total_hidden_ms / 1000:.0f} с. "
            "Это лишь сигналы для проверки, а не доказательство."
        )
    return VerdictDraft(
        suspicion_score=suspicion,
        confidence=confidence,
        flags=tuple(flags),
        summary=summary,
        model_id=None,
        writing=tuple(writing),
    )


def apply_deterministic_floor(
    draft: VerdictDraft, profile: IntegrityProfile, segments: list[FlaggedSegment]
) -> VerdictDraft:
    """Raise an LLM draft's numbers to >= ``LLM_FLOOR_SHARE`` x the formula's.

    ``ai_assist_likelihood_i = max(llm_i, 0.8 * formula_i)`` for every writing item
    the profile knows, and ``suspicion = max(llm, 0.8 * formula_suspicion)``. Never
    lowers anything and never exceeds ``MAX_LIKELIHOOD`` (the formula is capped).
    Pure; returns `draft` itself when nothing had to move.
    """
    det = deterministic_verdict(profile, segments)
    floors = {w.item_id: _r(LLM_FLOOR_SHARE * w.ai_assist_likelihood) for w in det.writing}
    writing = tuple(
        w.model_copy(update={"ai_assist_likelihood": floors[w.item_id]})
        if floors.get(w.item_id, 0.0) > w.ai_assist_likelihood
        else w
        for w in draft.writing
    )
    suspicion = max(draft.suspicion_score, _r(LLM_FLOOR_SHARE * det.suspicion_score))
    if suspicion == draft.suspicion_score and writing == draft.writing:
        return draft
    return replace(draft, suspicion_score=suspicion, writing=writing)
