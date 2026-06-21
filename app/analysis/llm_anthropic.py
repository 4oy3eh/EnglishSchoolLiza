"""Real `AnalysisLLM` backed by the Anthropic Messages API.

Kept out of `app/analysis/__init__.py` and importing the SDK lazily, so the analysis
engine has no hard runtime dependency on `anthropic` — tests use `MockAnalysisLLM`
and only this module pulls the SDK, and only when actually constructed. Mirrors
`app/grading/llm_anthropic.py`.

The verdict is **advisory only** (golden rule #2): the model judges cheating
likelihood from the deterministic profile + raw segments and never sees or returns a
score. Every call logs model id + token usage + latency (golden rule #8). Uses
structured outputs (`messages.parse`) so the verdict comes back as a validated
object, and adaptive thinking (recommended for a judgment task on Opus 4.8).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.analysis.llm import VerdictDraft
from app.analysis.segments import FlaggedSegment
from app.core.logging import get_logger
from contracts import IntegrityProfile

if TYPE_CHECKING:
    from anthropic import Anthropic

log = get_logger(__name__)

DEFAULT_MODEL = "claude-opus-4-8"

_SYSTEM = (
    "You are an exam-integrity analyst. You are given DETERMINISTIC behavioural "
    "features and the raw event segments behind them for one online test attempt. "
    "Judge the LIKELIHOOD that the candidate cheated, on a 0-1 scale, with a "
    "separate 0-1 confidence reflecting how much evidence you have. Your verdict is "
    "ADVISORY ONLY: never recommend a score change, and reason solely from the "
    "behavioural signals provided — you do not know whether any answer was correct."
)


class _Verdict(BaseModel):
    """Structured advisory verdict the model must return."""

    suspicion_score: float = Field(description="Cheating likelihood in [0, 1].")
    confidence: float = Field(description="Confidence in the judgment, [0, 1].")
    flags: list[str] = Field(description="Short machine-readable signal tags.")
    summary: str = Field(description="One or two sentences for the teacher.")


class AnthropicAnalyst:
    """Advisory cheating-likelihood analyst using Claude."""

    def __init__(
        self,
        *,
        client: Anthropic | None = None,
        model: str = DEFAULT_MODEL,
        # Adaptive thinking tokens count toward the output cap, so leave headroom
        # for the reasoning plus the structured verdict (avoids a max_tokens stop).
        max_tokens: int = 4096,
    ) -> None:
        if client is None:
            from anthropic import Anthropic  # lazy: only needed for the real path

            client = Anthropic()
        self._client = client
        self.model = model
        self.max_tokens = max_tokens

    def analyze(
        self, profile: IntegrityProfile, segments: list[FlaggedSegment]
    ) -> VerdictDraft:
        prompt = self._build_prompt(profile, segments)

        start = time.perf_counter()
        message = self._client.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            thinking={"type": "adaptive"},
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_format=_Verdict,
        )
        latency_ms = (time.perf_counter() - start) * 1000.0

        usage = message.usage
        log.info(
            "llm analyze attempt=%s model=%s in_tokens=%d out_tokens=%d latency=%.0fms",
            profile.attempt_id,
            message.model,
            usage.input_tokens,
            usage.output_tokens,
            latency_ms,
        )

        verdict = self._extract(message)
        return VerdictDraft(
            suspicion_score=_clamp(verdict.suspicion_score),
            confidence=_clamp(verdict.confidence),
            flags=tuple(verdict.flags),
            summary=verdict.summary,
            model_id=message.model,
        )

    @staticmethod
    def _build_prompt(
        profile: IntegrityProfile, segments: list[FlaggedSegment]
    ) -> str:
        seg_lines = (
            "\n".join(f"- [{s.kind}] {s.reason}" for s in segments) or "(none)"
        )
        return (
            f"ATTEMPT: {profile.attempt_id}\n\n"
            "DETERMINISTIC FEATURES:\n"
            f"- questions: {len(profile.question_timings)}\n"
            f"- total hidden ms: {profile.total_hidden_ms}\n"
            f"- hidden intervals: {len(profile.hidden_intervals)}\n"
            f"- pacing CV: {profile.pacing_cv:.4f}\n"
            f"- systematicity rate: {profile.systematicity_rate:.4f}\n\n"
            f"FLAGGED RAW SEGMENTS:\n{seg_lines}\n"
        )

    @staticmethod
    def _extract(message: object) -> _Verdict:
        from anthropic.types import ParsedTextBlock

        content = getattr(message, "content", [])
        for block in content:
            if isinstance(block, ParsedTextBlock) and block.parsed_output is not None:
                parsed = block.parsed_output
                if isinstance(parsed, _Verdict):
                    return parsed
        # Surface the boundary loudly: a malformed response otherwise fails with only
        # a traceback. (Matches the WARNING/ERROR-before-raise style used elsewhere.)
        log.error("LLM analyst returned no parseable verdict")
        raise RuntimeError("LLM analyst returned no parseable verdict")


def _clamp(value: float) -> float:
    """Pin a model-returned probability into the contract's [0, 1] range."""
    return max(0.0, min(1.0, value))
