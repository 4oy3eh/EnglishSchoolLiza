"""Analysis engine: an advisory cheating-likelihood verdict over an attempt.

Layer 3 of the three integrity layers (golden rule #6): `telemetry` (capture) ->
`integrity` (deterministic features) -> **`analysis` (LLM verdict, advisory)**. This
service consumes the `IntegrityProfile` (it does not re-derive features) plus the
raw event segments behind it, and asks an injected `AnalysisLLM` for a verdict.

Without an analyst (no key) or when the analyst fails, it returns the
DETERMINISTIC-ONLY verdict (`app.analysis.fallback`, `model_id=None`, low
confidence; on failure with an `error` note) — the feature works offline and an LLM
outage never breaks the caller. The `error` note is scrubbed of credentials
(`app.analysis.redact`) before it is logged or stored.

The deterministic evidence dominates: whatever an analyst returns is floored by
`fallback.apply_deterministic_floor` (>= 0.8 x the formula, per writing item and
overall), so a model misled by the examinee's text cannot clear a flagged attempt.

Golden rule #2 (Grading ⊥ Integrity): this engine never imports `app.grading`, never
sees an item grade, and an `AnalysisVerdict` can never carry or change a score — it
is advisory input for the teacher only. The teacher always sees the raw replay next
to it.
"""

from __future__ import annotations

from app.analysis.fallback import apply_deterministic_floor, deterministic_verdict
from app.analysis.llm import AnalysisContext, AnalysisLLM, VerdictDraft
from app.analysis.redact import redact
from app.analysis.segments import flag_segments
from app.core.logging import get_logger
from app.integrity import IntegrityService
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

        Never raises because of the analyst: no analyst -> deterministic verdict
        (WARNING); analyst error -> deterministic verdict + `error` (ERROR log).
        """
        if context is not None:
            profile = self.integrity.profile(
                attempt_id,
                answers={t.item_id: t.text for t in context.writing},
                writing_items=[t.item_id for t in context.writing],
            )
        else:
            profile = self.integrity.profile(attempt_id)
        events = self.events.list_events(attempt_id)
        segments = flag_segments(profile, events)

        error: str | None = None
        if self.llm is None:
            log.warning(
                "analyze attempt=%s no analyst configured -> deterministic-only verdict",
                attempt_id,
            )
            draft: VerdictDraft = deterministic_verdict(profile, segments)
        else:
            try:
                raw = self.llm.analyze(profile, segments, context)
                # Deterministic evidence dominates the model's opinion.
                draft = apply_deterministic_floor(raw, profile, segments)
                if draft is not raw:
                    log.info(
                        "analyze attempt=%s deterministic floor applied: "
                        "suspicion %.3f -> %.3f, writing %s -> %s",
                        attempt_id,
                        raw.suspicion_score,
                        draft.suspicion_score,
                        [w.ai_assist_likelihood for w in raw.writing],
                        [w.ai_assist_likelihood for w in draft.writing],
                    )
            except Exception as exc:  # LLM outage must never break the caller
                error = _describe(exc, self.llm)
                log.error(
                    "analyze attempt=%s analyst failed (%s) -> deterministic-only verdict",
                    attempt_id,
                    error,
                )
                draft = deterministic_verdict(profile, segments)

        verdict = AnalysisVerdict(
            attempt_id=attempt_id,
            suspicion_score=draft.suspicion_score,
            confidence=draft.confidence,
            flags=list(draft.flags),
            summary=draft.summary,
            model_id=draft.model_id,
            writing=list(draft.writing),
            error=error,
        )
        log.info(
            "analyze attempt=%s model=%s suspicion=%.3f confidence=%.3f "
            "flags=%d segments=%d writing=%d error=%s",
            attempt_id,
            verdict.model_id,
            verdict.suspicion_score,
            verdict.confidence,
            len(verdict.flags),
            len(segments),
            len(verdict.writing),
            verdict.error or "-",
        )
        return profile, verdict
