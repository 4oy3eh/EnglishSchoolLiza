"""Analysis engine: assemble an attempt's advisory integrity verdict.

Layer 3 of the three integrity layers (golden rule #6): `telemetry` (capture) ->
`integrity` (deterministic features + rules) -> **`analysis` (verdict assembly +
optional LLM judgment, advisory)**.

**The deterministic rule engine is the verdict of record.** `suspicion_score`,
`confidence`, `rules`, `flags` and `summary` always come from
`app.integrity.rules` (via `IntegrityService.rules`) — same events, same verdict,
no model involved. This service does not re-derive features.

**The LLM is an optional second opinion on WRITING only.** When an analyst is
injected (the results layer passes one only when `ANALYSIS_LLM=writing` and a key
is set) AND the attempt has a non-empty writing answer, it is asked about the
writing items alone: it receives a writing-only profile and the flagged writing
segments, and only its per-item `WritingAssessment`s (+ `model_id`) are kept —
floored by the deterministic evidence (`fallback.apply_deterministic_floor`,
>= 0.8 x the rules' per-item likelihood). Nothing it returns can change the
overall suspicion or the ranking. No analyst, no writing, or an analyst failure
(`error` note, credentials scrubbed by `app.analysis.redact`) all leave the
rule-engine verdict intact with the rules' own per-item assessments.

Golden rule #2 (Grading ⊥ Integrity): this engine never imports `app.grading`, never
sees an item grade, and an `AnalysisVerdict` can never carry or change a score — it
is advisory input for the teacher only. The teacher always sees the raw replay next
to it.
"""

from __future__ import annotations

from app.analysis.fallback import apply_deterministic_floor, deterministic_verdict
from app.analysis.llm import AnalysisContext, AnalysisLLM
from app.analysis.redact import redact
from app.analysis.segments import WRITING_SEGMENT_KIND, flag_segments
from app.core.logging import get_logger
from app.integrity import RULES_VERSION, IntegrityService
from app.persistence.repository import EventRepository
from contracts import AnalysisVerdict, IntegrityProfile

log = get_logger(__name__)

ERROR_MAX_CHARS = 500


def _describe(exc: BaseException, llm: object = None) -> str:
    """`<Type>: message`, credentials scrubbed, truncated — safe to log and store.

    Generic patterns (`key=<...>`, Google-key shapes) are always removed; an analyst
    exposing `redact(text)` (the Gemini one) also removes its own literal key.
    """
    text = redact(f"{type(exc).__name__}: {exc}".strip())
    scrub = getattr(llm, "redact", None)
    if callable(scrub):
        try:
            text = str(scrub(text))
        except Exception:  # a broken scrubber must not break the fallback
            log.warning("analyst redact() failed; generic redaction only")
    return text if len(text) <= ERROR_MAX_CHARS else text[: ERROR_MAX_CHARS - 1] + "…"


class AnalysisService:
    """Build an attempt's advisory `AnalysisVerdict` from its integrity profile."""

    def __init__(
        self,
        events: EventRepository,
        integrity: IntegrityService,
        *,
        llm: AnalysisLLM | None = None,
    ) -> None:
        self.events = events
        self.integrity = integrity
        self.llm = llm

    def analyze(
        self, attempt_id: str, *, context: AnalysisContext | None = None
    ) -> AnalysisVerdict:
        """Profile the attempt, flag raw segments, and build the advisory verdict."""
        return self.assess(attempt_id, context=context)[1]

    def assess(
        self, attempt_id: str, *, context: AnalysisContext | None = None
    ) -> tuple[IntegrityProfile, AnalysisVerdict]:
        """Like `analyze`, but also return the profile the verdict was built from.

        `context` (from the results layer) supplies the writing tasks + submitted
        texts, the test level and the opaque objective percentage. With a context,
        the writing features cover exactly the test's open_writing items.

        The overall verdict is the rule engine's, whatever the analyst does. The
        analyst (when injected) is asked only about writing and only when there is
        a non-empty writing answer; it never raises through here: an analyst error
        -> the rules' own per-item assessments + `error` (ERROR log).
        """
        # One read of the stream: the profile and the rule evidence must agree even
        # when an event lands while the verdict is being assembled.
        events = self.events.list_events(attempt_id)
        if context is not None:
            profile = self.integrity.profile(
                attempt_id,
                answers={t.item_id: t.text for t in context.writing},
                writing_items=[t.item_id for t in context.writing],
                events=events,
            )
        else:
            profile = self.integrity.profile(attempt_id, events=events)
        report = self.integrity.rules(profile, events)
        draft = deterministic_verdict(profile, report)

        flags = list(draft.flags)
        writing = list(draft.writing)
        model_id: str | None = None
        error: str | None = None
        has_text = any(w.final_length > 0 for w in profile.writing)
        if self.llm is None:
            log.info(
                "analyze attempt=%s no writing analyst (ANALYSIS_LLM=off or no key) -> "
                "rule-engine verdict only",
                attempt_id,
            )
        elif not has_text:
            log.info(
                "analyze attempt=%s no writing answer -> analyst not called "
                "(rule-engine verdict only)",
                attempt_id,
            )
        else:
            # The analyst sees writing and nothing else: no tab / device / pacing
            # behaviour (that is the rule engine's job, and not a model's to judge).
            writing_profile = IntegrityProfile(attempt_id=attempt_id, writing=profile.writing)
            segments = [
                s for s in flag_segments(profile, events) if s.kind == WRITING_SEGMENT_KIND
            ]
            try:
                raw = self.llm.analyze(writing_profile, segments, context)
                # Deterministic evidence dominates the model's opinion.
                opinion = apply_deterministic_floor(raw, profile)
                if opinion is not raw:
                    log.info(
                        "analyze attempt=%s deterministic floor applied to writing: %s -> %s",
                        attempt_id,
                        [w.ai_assist_likelihood for w in raw.writing],
                        [w.ai_assist_likelihood for w in opinion.writing],
                    )
                by_id = {w.item_id: w for w in opinion.writing}
                # Exactly the items the rules know: the model's assessment where it
                # gave one, else the rules' own (an unknown id is dropped).
                writing = [by_id.get(w.item_id, w) for w in draft.writing]
                model_id = raw.model_id
                flags += [f for f in raw.flags if f not in flags]
            except Exception as exc:  # LLM outage must never break the caller
                error = _describe(exc, self.llm)
                log.error(
                    "analyze attempt=%s writing analyst failed (%s) -> rule-engine "
                    "verdict without a second opinion",
                    attempt_id,
                    error,
                )

        verdict = AnalysisVerdict(
            attempt_id=attempt_id,
            suspicion_score=draft.suspicion_score,
            confidence=draft.confidence,
            flags=flags,
            summary=draft.summary,
            model_id=model_id,
            writing=writing,
            rules=list(report.hits),
            rules_version=RULES_VERSION,
            error=error,
        )
        log.info(
            "analyze attempt=%s suspicion=%.3f confidence=%.3f rules=%d "
            "writing=%d second_opinion=%s error=%s",
            attempt_id,
            verdict.suspicion_score,
            verdict.confidence,
            len(verdict.rules),
            len(verdict.writing),
            verdict.model_id or "-",
            verdict.error or "-",
        )
        return profile, verdict
