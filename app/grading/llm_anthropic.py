"""Real `LLMGrader` backed by the Anthropic Messages API.

Kept out of `app/grading/__init__.py` and importing the SDK lazily, so the grading
engine has no hard runtime dependency on `anthropic` — tests use `MockLLMGrader` and
only this module pulls the SDK, and only when actually constructed.

Every call logs model id + token usage + latency (golden rule #8). Uses structured
outputs (`messages.parse`) so the rubric verdict comes back as a validated object,
and adaptive thinking (the recommended setting for a judgment task on Opus 4.8).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.core.logging import get_logger
from app.grading.llm import DEFAULT_WRITING_POINTS, WritingGrade
from contracts import OpenWritingItem

if TYPE_CHECKING:
    from anthropic import Anthropic

log = get_logger(__name__)

DEFAULT_MODEL = "claude-opus-4-8"

_SYSTEM = (
    "You are an examiner for Cambridge A2 Key / B1 Preliminary writing tasks. "
    "Grade the candidate's response ONLY against the provided rubric. Return a "
    "numeric score within the allowed range and one or two sentences of feedback. "
    "Do not penalise or reward anything outside the rubric."
)


class _WritingVerdict(BaseModel):
    """Structured rubric verdict the model must return."""

    score: float = Field(description="Score within [0, max_points].")
    feedback: str = Field(description="One or two sentences of rubric-based feedback.")


class AnthropicWritingGrader:
    """Rubric-based `open_writing` grader using Claude."""

    def __init__(
        self,
        *,
        client: Anthropic | None = None,
        model: str = DEFAULT_MODEL,
        max_points: float = DEFAULT_WRITING_POINTS,
        # Adaptive thinking tokens count toward the output cap, so leave headroom
        # for the reasoning plus the structured verdict (avoids a max_tokens stop).
        max_tokens: int = 4096,
    ) -> None:
        if client is None:
            from anthropic import Anthropic  # lazy: only needed for the real path

            client = Anthropic()
        self._client = client
        self.model = model
        self.max_points = max_points
        self.max_tokens = max_tokens

    def grade_writing(self, item: OpenWritingItem, response: str) -> WritingGrade:
        bullets = "\n".join(f"- {b}" for b in item.bullet_points) or "(none)"
        prompt = (
            f"RUBRIC:\n{item.rubric}\n\n"
            f"TASK PROMPT:\n{item.prompt}\n\n"
            f"REQUIRED POINTS TO COVER:\n{bullets}\n\n"
            f"MINIMUM WORDS: {item.word_min}\n"
            f"SCORE RANGE: 0 to {self.max_points}\n\n"
            f"CANDIDATE RESPONSE:\n{response}"
        )

        start = time.perf_counter()
        message = self._client.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            thinking={"type": "adaptive"},
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_format=_WritingVerdict,
        )
        latency_ms = (time.perf_counter() - start) * 1000.0

        usage = message.usage
        log.info(
            "llm grade item=%s model=%s in_tokens=%d out_tokens=%d latency=%.0fms",
            item.id,
            message.model,
            usage.input_tokens,
            usage.output_tokens,
            latency_ms,
        )

        verdict = self._extract(message)
        score = max(0.0, min(self.max_points, verdict.score))
        return WritingGrade(
            awarded=round(score, 2),
            max_points=self.max_points,
            feedback=verdict.feedback,
            model_id=message.model,
        )

    @staticmethod
    def _extract(message: object) -> _WritingVerdict:
        from anthropic.types import ParsedTextBlock

        content = getattr(message, "content", [])
        for block in content:
            if isinstance(block, ParsedTextBlock) and block.parsed_output is not None:
                parsed = block.parsed_output
                if isinstance(parsed, _WritingVerdict):
                    return parsed
        raise RuntimeError("LLM grader returned no parseable verdict")
