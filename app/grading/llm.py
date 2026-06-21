"""Writing-grader interface + a deterministic mock.

`open_writing` is graded against the item's `rubric`. The grading engine depends
only on the `LLMGrader` *protocol* — the real Anthropic-backed implementation lives
in `llm_anthropic.py` (lazy SDK import) and the tests inject `MockLLMGrader`, so the
engine never hard-depends on a network call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from contracts import OpenWritingItem

# Default points an open_writing item is worth (Cambridge writing bands are ~0-5).
DEFAULT_WRITING_POINTS = 5.0


@dataclass(frozen=True)
class WritingGrade:
    """A grader's verdict on one written response.

    `needs_review` lets a grader escalate a borderline case to a human without
    blocking the rest of the attempt (the score still assembles).
    """

    awarded: float
    max_points: float
    feedback: str
    needs_review: bool = False
    model_id: str | None = None


@runtime_checkable
class LLMGrader(Protocol):
    """Grades one written response against its item's rubric."""

    def grade_writing(self, item: OpenWritingItem, response: str) -> WritingGrade: ...


class MockLLMGrader:
    """Deterministic stand-in for tests and offline runs.

    Awards proportionally to how close the response gets to `word_min` (a crude but
    fully reproducible proxy), and flags anything under the minimum for review. No
    rubric reasoning — that is the real grader's job; this only needs to be stable.
    """

    def __init__(self, *, max_points: float = DEFAULT_WRITING_POINTS) -> None:
        self.max_points = max_points

    def grade_writing(self, item: OpenWritingItem, response: str) -> WritingGrade:
        words = len(response.split())
        if words == 0:
            return WritingGrade(
                awarded=0.0,
                max_points=self.max_points,
                feedback="Empty response.",
                needs_review=True,
                model_id="mock-llm",
            )
        ratio = min(words / item.word_min, 1.0)
        return WritingGrade(
            awarded=round(self.max_points * ratio, 2),
            max_points=self.max_points,
            feedback=f"{words} words (min {item.word_min}).",
            needs_review=words < item.word_min,
            model_id="mock-llm",
        )
