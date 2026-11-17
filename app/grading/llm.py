"""Writing-grader interface + a deterministic mock.

`open_writing` is graded against the item's `rubric`. The grading engine depends
only on the `LLMGrader` *protocol* — the real Anthropic-backed implementation lives
in `llm_anthropic.py` (lazy SDK import) and the tests inject `MockLLMGrader`, so the
engine never hard-depends on a network call.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol, runtime_checkable

from contracts import ColourTaskItem, OpenWritingItem

# Default points an open_writing item is worth (Cambridge writing bands are ~0-5).
DEFAULT_WRITING_POINTS = 5.0


def writing_points(item: OpenWritingItem | ColourTaskItem) -> float:
    """What a writing / colouring item is worth: its authored `points`, else 5."""
    return float(item.points) if item.points is not None else DEFAULT_WRITING_POINTS


def scale_writing_score(awarded: float, grader_max: float, points: float) -> float:
    """Map a grader's `awarded / grader_max` band onto an item worth `points`.

    Deterministic. When the item is worth exactly the grader's scale (every item
    without `points`, or `points == 5` with the 0-5 Gemini band) the grader's mark
    is kept verbatim (e.g. 3.5 / 5). Otherwise the proportional value
    `awarded * points / grader_max` is rounded HALF-UP to a WHOLE mark — Cambridge
    marks are whole numbers and the teacher's mark picker offers 0..points: a
    2-mark Movers sentence gets 5 -> 2, 4 -> 1.6 -> 2, 3.5 -> 1.4 -> 1, 3 -> 1.2 -> 1,
    2.5 -> 1.0 -> 1, 1.25 -> 0.5 -> 1, 1 -> 0.4 -> 0. Clamped to [0, points].
    """
    if grader_max <= 0:
        return 0.0
    if points == grader_max:
        return max(0.0, min(awarded, points))
    exact = Decimal(str(awarded)) * Decimal(str(points)) / Decimal(str(grader_max))
    whole = float(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return max(0.0, min(whole, points))


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
