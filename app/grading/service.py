"""Grading engine: assemble an attempt's score from its stored answers.

Reads the `Attempt`, its `Answer` rows, and the authoring `Test`, grades each item
(deterministic for objective items, an injected `LLMGrader` or manual review for
writing), and returns a `GradingResult`. Plus a manual-override path and a
needs-review queue.

Golden rule #2: this engine never imports or reads integrity/telemetry data — a
cheating signal must never move a score. (`tests/test_grading.py` asserts the import
graph stays clean.)
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.grading.deterministic import (
    grade_gap_fill,
    grade_matching,
    grade_single_choice,
)
from app.grading.llm import DEFAULT_WRITING_POINTS, LLMGrader
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import (
    GapFillItem,
    GradingResult,
    Item,
    ItemGrade,
    MatchingItem,
    OpenWritingItem,
    SingleChoiceItem,
    Test,
)

log = get_logger(__name__)


class GradingError(Exception):
    """Base for grading-layer rejections (e.g. unknown attempt or override target)."""


class GradingService:
    """Grade an attempt on top of the content + attempt repositories."""

    def __init__(
        self,
        content_repo: ContentRepository,
        attempt_repo: AttemptRepository,
        *,
        llm_grader: LLMGrader | None = None,
        fuzzy_threshold: float = 85.0,
    ) -> None:
        self.content = content_repo
        self.attempts = attempt_repo
        self.llm_grader = llm_grader
        self.fuzzy_threshold = fuzzy_threshold

    # -- grade -------------------------------------------------------------- #
    def grade(self, attempt_id: str) -> GradingResult:
        """Grade every item in the attempt's test and assemble the result."""
        attempt = self.attempts.get_attempt(attempt_id)
        if attempt is None:
            raise GradingError(f"attempt {attempt_id!r} not found")
        test = self.content.get_test(attempt.test_id)
        if test is None:
            raise GradingError(f"test {attempt.test_id!r} not found")

        responses = {a.item_id: a.response for a in self.attempts.get_answers(attempt_id)}
        grades = [
            self._grade_item(item, responses.get(item.id)) for item in _items(test)
        ]
        result = _assemble(attempt_id, grades)
        log.info(
            "grade attempt=%s score=%.2f/%.2f needs_review=%s",
            attempt_id,
            result.score,
            result.max_score,
            result.needs_review,
        )
        return result

    def _grade_item(self, item: Item, response: str | None) -> ItemGrade:
        if isinstance(item, SingleChoiceItem):
            return grade_single_choice(item, response)
        if isinstance(item, MatchingItem):
            return grade_matching(item, response)
        if isinstance(item, GapFillItem):
            return grade_gap_fill(item, response, fuzzy_threshold=self.fuzzy_threshold)
        return self._grade_writing(item, response)

    def _grade_writing(self, item: OpenWritingItem, response: str | None) -> ItemGrade:
        # Manual mode, or LLM mode with no grader injected -> needs human review.
        if item.grade_mode == "manual" or self.llm_grader is None:
            if item.grade_mode == "llm":
                log.warning(
                    "grade writing item=%s wants llm but no grader -> review", item.id
                )
            method = "open_writing_manual" if item.grade_mode == "manual" else "open_writing_llm"
            return ItemGrade(
                item_id=item.id,
                awarded=0.0,
                max_points=DEFAULT_WRITING_POINTS,
                method=method,  # type: ignore[arg-type]
                needs_review=True,
            )

        verdict = self.llm_grader.grade_writing(item, response or "")
        log.info(
            "grade writing item=%s model=%s awarded=%.2f/%.2f review=%s",
            item.id,
            verdict.model_id,
            verdict.awarded,
            verdict.max_points,
            verdict.needs_review,
        )
        return ItemGrade(
            item_id=item.id,
            awarded=verdict.awarded,
            max_points=verdict.max_points,
            method="open_writing_llm",
            needs_review=verdict.needs_review,
        )

    # -- manual override + review queue ------------------------------------- #
    def apply_override(
        self,
        result: GradingResult,
        item_id: str,
        *,
        awarded: float,
        max_points: float | None = None,
    ) -> GradingResult:
        """Return a new result with one item regraded by a human.

        The overridden item becomes `open_writing_manual`, its `needs_review` is
        cleared, and the totals are recomputed. The input result is not mutated.
        """
        target = next((g for g in result.items if g.item_id == item_id), None)
        if target is None:
            raise GradingError(f"item {item_id!r} not in grading result")
        max_pts = max_points if max_points is not None else target.max_points
        if not 0.0 <= awarded <= max_pts:
            raise GradingError(
                f"override {awarded} out of range [0, {max_pts}] for {item_id!r}"
            )
        # Relabel a writing item as manually graded; keep an objective item's method
        # (there is no generic "manual" GradeMethod, and relabelling a single_choice
        # grade as open_writing_manual would be wrong).
        method = (
            "open_writing_manual"
            if target.method in ("open_writing_llm", "open_writing_manual")
            else target.method
        )
        overridden = ItemGrade(
            item_id=item_id,
            awarded=awarded,
            max_points=max_pts,
            method=method,
            needs_review=False,
        )
        grades = [overridden if g.item_id == item_id else g for g in result.items]
        log.info(
            "override attempt=%s item=%s -> %.2f/%.2f",
            result.attempt_id,
            item_id,
            awarded,
            max_pts,
        )
        return _assemble(result.attempt_id, grades)

    @staticmethod
    def review_queue(result: GradingResult) -> list[ItemGrade]:
        """The items a human still has to finish (manual writing, LLM-flagged)."""
        return [g for g in result.items if g.needs_review]


# --------------------------------------------------------------------------- #
# Helpers (pure).
# --------------------------------------------------------------------------- #
def _items(test: Test) -> list[Item]:
    return [item for section in test.sections for item in section.items]


def _assemble(attempt_id: str, grades: list[ItemGrade]) -> GradingResult:
    return GradingResult(
        attempt_id=attempt_id,
        items=grades,
        score=round(sum(g.awarded for g in grades), 2),
        max_score=round(sum(g.max_points for g in grades), 2),
        needs_review=any(g.needs_review for g in grades),
    )
