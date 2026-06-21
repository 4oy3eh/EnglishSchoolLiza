"""Analysis engine: an advisory cheating-likelihood verdict over an attempt.

Layer 3 of the three integrity layers (golden rule #6): `telemetry` (capture) ->
`integrity` (deterministic features) -> **`analysis` (LLM verdict, advisory)**. This
service consumes the Phase 7 `IntegrityProfile` (it does not re-derive features) plus
the raw event segments behind it, and asks an injected `AnalysisLLM` for a verdict.

Golden rule #2 (Grading ⊥ Integrity): this engine never imports `app.grading`, never
sees a score, and an `AnalysisVerdict` can never carry or change one — it is advisory
input for the teacher only. The teacher always sees the raw replay next to it.
"""

from __future__ import annotations

from app.analysis.llm import AnalysisLLM
from app.analysis.segments import flag_segments
from app.core.logging import get_logger
from app.integrity import IntegrityService
from app.persistence.repository import EventRepository
from contracts import AnalysisVerdict

log = get_logger(__name__)


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

    def analyze(self, attempt_id: str) -> AnalysisVerdict:
        """Profile the attempt, flag raw segments, and ask the analyst for a verdict.

        With no analyst injected, returns a neutral (zero-suspicion, zero-confidence)
        advisory verdict rather than guessing — mirrors grading's no-grader fallback.
        """
        profile = self.integrity.profile(attempt_id)
        events = self.events.list_events(attempt_id)
        segments = flag_segments(profile, events)

        if self.llm is None:
            log.warning(
                "analyze attempt=%s no analyst configured -> neutral advisory verdict",
                attempt_id,
            )
            return AnalysisVerdict(
                attempt_id=attempt_id,
                suspicion_score=0.0,
                confidence=0.0,
                flags=[],
                summary="No analyst configured; advisory verdict unavailable.",
                model_id=None,
            )

        draft = self.llm.analyze(profile, segments)
        verdict = AnalysisVerdict(
            attempt_id=attempt_id,
            suspicion_score=draft.suspicion_score,
            confidence=draft.confidence,
            flags=list(draft.flags),
            summary=draft.summary,
            model_id=draft.model_id,
        )
        log.info(
            "analyze attempt=%s model=%s suspicion=%.3f confidence=%.3f "
            "flags=%d segments=%d",
            attempt_id,
            verdict.model_id,
            verdict.suspicion_score,
            verdict.confidence,
            len(verdict.flags),
            len(segments),
        )
        return verdict
