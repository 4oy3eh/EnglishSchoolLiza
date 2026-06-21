"""Analyst interface + a deterministic mock.

The analysis engine depends only on the `AnalysisLLM` *protocol* — the real
Anthropic-backed implementation lives in `llm_anthropic.py` (lazy SDK import) and
the tests inject `MockAnalysisLLM`, so the engine never hard-depends on a network
call. Mirrors grading's `LLMGrader` seam.

Every verdict is **advisory only** (golden rule #2): an `AnalysisLLM` returns a
suspicion judgment over the integrity signals and **never sees or returns a score**.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.analysis.segments import FlaggedSegment
from contracts import IntegrityProfile

# Reference points that turn raw signals into a [0, 1] suspicion in the mock.
HIDDEN_REF_MS = 60_000  # ~1 min total hidden saturates the "left the page" signal
CONFIDENCE_REF_QUESTIONS = 10  # more answered questions -> more to go on


@dataclass(frozen=True)
class VerdictDraft:
    """An analyst's advisory judgment over an attempt's integrity signals.

    Deliberately carries **no score and no answer correctness** — analysis is
    orthogonal to grading (golden rule #2). The service maps this onto the
    `AnalysisVerdict` contract.
    """

    suspicion_score: float
    confidence: float
    flags: tuple[str, ...]
    summary: str
    model_id: str | None = None


@runtime_checkable
class AnalysisLLM(Protocol):
    """Judges cheating likelihood from the deterministic profile + raw segments."""

    def analyze(
        self, profile: IntegrityProfile, segments: list[FlaggedSegment]
    ) -> VerdictDraft: ...


class MockAnalysisLLM:
    """Deterministic stand-in for tests and offline runs.

    Blends the two strongest integrity signals into a reproducible suspicion:
    systematicity (the "left, came back, answered instantly" pattern repeated) and
    total hidden time. Flags are the distinct segment kinds, in first-seen order.
    No real reasoning — that is the analyst's job; this only needs to be stable and
    monotone (a cheatier profile scores higher).
    """

    model_id = "mock-analysis"

    def analyze(
        self, profile: IntegrityProfile, segments: list[FlaggedSegment]
    ) -> VerdictDraft:
        hidden_signal = min(profile.total_hidden_ms / HIDDEN_REF_MS, 1.0)
        suspicion = round(
            min(1.0, 0.6 * profile.systematicity_rate + 0.4 * hidden_signal), 4
        )
        confidence = round(
            min(1.0, len(profile.question_timings) / CONFIDENCE_REF_QUESTIONS), 4
        )

        flags: list[str] = []
        for segment in segments:  # distinct kinds, first-seen order (deterministic)
            if segment.kind not in flags:
                flags.append(segment.kind)

        if not flags:
            summary = "No notable integrity signals; behaviour looks ordinary."
        else:
            summary = (
                f"Advisory: {len(segments)} flagged segment(s) across "
                f"{len(flags)} signal type(s) ({', '.join(flags)}); "
                f"systematicity {profile.systematicity_rate:.0%}, "
                f"{profile.total_hidden_ms / 1000:.0f}s hidden."
            )

        return VerdictDraft(
            suspicion_score=suspicion,
            confidence=confidence,
            flags=tuple(flags),
            summary=summary,
            model_id=self.model_id,
        )
