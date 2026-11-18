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
from contracts import IntegrityProfile, WritingAssessment

# Reference points that turn raw signals into a [0, 1] suspicion in the mock.
HIDDEN_REF_MS = 60_000  # ~1 min total hidden saturates the "left the page" signal
CONFIDENCE_REF_QUESTIONS = 10  # more answered questions -> more to go on


@dataclass(frozen=True)
class WritingTask:
    """One open_writing task + the student's submitted text (analysis input only).

    Supplied by the results layer (the only place holding the test, the answers
    and the grade). Carries no score and no rubric verdict.
    """

    item_id: str
    prompt: str
    text: str = ""
    bullet_points: tuple[str, ...] = ()
    word_min: int | None = None


@dataclass(frozen=True)
class AnalysisContext:
    """Optional non-telemetry context for the analyst.

    * `level` — the test level (`contracts.content.Level`: `PRE_A1_STARTERS` /
      `A1_MOVERS` / `A2_FLYERS` / `A2_KEY` / `B1_PRELIMINARY`), so the
      analyst can judge whether a text is far above what the level produces.
    * `writing` — the writing tasks and the student's texts.
    * `objective_pct` — the student's objective-section percentage (0..100), an
      OPAQUE number from the results layer used only as level-mismatch evidence
      ("weak reading/listening but native-like essay"). It is analysis input only:
      nothing ever flows back into a score (golden rule #2).
    """

    level: str | None = None
    writing: tuple[WritingTask, ...] = ()
    objective_pct: float | None = None


@dataclass(frozen=True)
class VerdictDraft:
    """An advisory judgment in the shape the service maps onto `AnalysisVerdict`.

    Two producers: `fallback.deterministic_verdict` (the rule engine — every field
    is used) and an `AnalysisLLM` (the optional second opinion on WRITING). From an
    analyst's draft the service takes ONLY `writing`, `model_id` and any extra
    `flags`; its `suspicion_score` / `confidence` / `summary` are ignored, because
    the overall verdict is never a model's (they default to "nothing" for that
    reason).

    Deliberately carries **no score and no answer correctness** — analysis is
    orthogonal to grading (golden rule #2).
    """

    suspicion_score: float = 0.0
    confidence: float = 0.0
    flags: tuple[str, ...] = ()
    summary: str = ""
    model_id: str | None = None
    writing: tuple[WritingAssessment, ...] = ()


@runtime_checkable
class AnalysisLLM(Protocol):
    """The optional SECOND OPINION on the writing items of an attempt.

    Called by `AnalysisService` only when the attempt has a non-empty writing
    answer. `profile` carries the writing features only (no tab / device / pacing
    behaviour) and `segments` only the flagged writing items; of the returned
    draft only `writing` / `model_id` / extra `flags` are kept (see `VerdictDraft`).
    """

    def analyze(
        self,
        profile: IntegrityProfile,
        segments: list[FlaggedSegment],
        context: AnalysisContext | None = None,
    ) -> VerdictDraft: ...


class MockAnalysisLLM:
    """Deterministic stand-in for tests and offline runs.

    Blends the two strongest integrity signals into a reproducible suspicion:
    systematicity (the "left, came back, answered instantly" pattern repeated) and
    total hidden time. Flags are the distinct segment kinds, in first-seen order.
    No real reasoning — that is the analyst's job; this only needs to be stable and
    monotone (a cheatier profile scores higher). `AnalysisService` ignores its
    overall numbers like any analyst's: only `writing` / `model_id` are kept.
    """

    model_id = "mock-analysis"

    def analyze(
        self,
        profile: IntegrityProfile,
        segments: list[FlaggedSegment],
        context: AnalysisContext | None = None,
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

        # Per-writing-item: reuse the documented deterministic formula (stable).
        from app.analysis.fallback import writing_assessments

        return VerdictDraft(
            suspicion_score=suspicion,
            confidence=confidence,
            flags=tuple(flags),
            summary=summary,
            model_id=self.model_id,
            writing=tuple(writing_assessments(profile)),
        )
