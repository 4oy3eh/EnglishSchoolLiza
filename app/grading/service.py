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
    objective_points,
)
from app.grading.llm import (
    DEFAULT_WRITING_POINTS,
    LLMGrader,
    scale_writing_score,
    writing_points,
)
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import (
    ColourTaskItem,
    GapFillItem,
    GradingResult,
    Item,
    ItemGrade,
    MatchingItem,
    OpenWritingItem,
    SingleChoiceItem,
)
from contracts.content import Skill

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
        fuzzy: bool = True,
    ) -> None:
        self.content = content_repo
        self.attempts = attempt_repo
        self.llm_grader = llm_grader
        # False = gap_fill exact-only (no typo tolerance); see deterministic.py.
        self.fuzzy = fuzzy
        # test_id -> the choose-one groups (optional writing ids per section).
        self._groups: dict[str, list[list[str]]] = {}

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
        grades: list[ItemGrade] = []
        for section in test.sections:
            group = [
                i for i in section.items if isinstance(i, OpenWritingItem) and i.optional
            ]
            group_ids = {i.id for i in group}
            chosen = {g.item_id: g for g in self._grade_choose_one(group, responses)}
            for item in section.items:
                if item.id in group_ids:
                    if item.id in chosen:  # a blank unchosen task is not graded
                        grades.append(chosen[item.id])
                    continue
                grades.append(self._grade_item(item, responses.get(item.id), section.skill))
        result = _assemble(attempt_id, grades)
        log.info(
            "grade attempt=%s score=%.2f/%.2f needs_review=%s",
            attempt_id,
            result.score,
            result.max_score,
            result.needs_review,
        )
        return result

    def _grade_choose_one(
        self, group: list[OpenWritingItem], responses: dict[str, str | None]
    ) -> list[ItemGrade]:
        """'Choose one of these' (B1 Writing Part 2): exactly ONE task counts.

        The optional writing items of one section form the group. It contributes
        exactly one task to `score` AND `max_score`:

        * one written -> that one is graded; the blank ones are not graded at all;
        * several written -> each is graded and the highest counts (ties: the
          first); every other one is recorded with `awarded=0, max_points=0` (adds
          nothing to the totals), its own mark kept in the note
          "не засчитано: выбрано одно задание", and `needs_review=True` so the
          teacher can choose which task counts: a manual mark on an uncounted
          task makes IT the counted one (`apply_override`);
        * none written -> the first task counts 0 of its full maximum,
          `needs_review=False` (nothing to review), so the maximum never shrinks.
        """
        if not group:
            return []
        ids = [i.id for i in group]
        written = [i for i in group if (responses.get(i.id) or "").strip()]
        if not written:
            first = group[0]
            log.info("grade choose-one group=%s none written -> %s counts 0", ids, first.id)
            return [
                ItemGrade(
                    item_id=first.id,
                    awarded=0.0,
                    max_points=writing_points(first),
                    method=_writing_method(first),  # type: ignore[arg-type]
                    needs_review=False,
                    feedback="не выполнено: ни одно задание на выбор не написано",
                )
            ]
        graded = [self._grade_writing(i, responses.get(i.id)) for i in written]
        if len(graded) == 1:
            return graded
        best = max(range(len(graded)), key=lambda k: (graded[k].awarded, -k))
        out: list[ItemGrade] = []
        for k, grade in enumerate(graded):
            if k == best:
                out.append(grade)
                continue
            note = (
                "не засчитано: выбрано одно задание "
                f"(оценка {grade.awarded:g}/{grade.max_points:g})"
            )
            if grade.feedback:
                note = f"{note}. {grade.feedback}"
            out.append(
                grade.model_copy(
                    update={
                        "awarded": 0.0,
                        "max_points": 0.0,
                        "needs_review": True,
                        "feedback": note,
                    }
                )
            )
        log.warning(
            "grade choose-one group=%s %d written -> %s counts, the rest not counted (review)",
            ids,
            len(graded),
            graded[best].item_id,
        )
        return out

    def _grade_item(
        self, item: Item, response: str | None, skill: Skill | None = None
    ) -> ItemGrade:
        if isinstance(item, SingleChoiceItem):
            return grade_single_choice(item, response)
        if isinstance(item, MatchingItem):
            return grade_matching(item, response)
        if isinstance(item, GapFillItem):
            # The section skill decides exact (reading/writing) vs tolerant spelling.
            return grade_gap_fill(item, response, fuzzy=self.fuzzy, skill=skill)
        if isinstance(item, ColourTaskItem):
            # Colouring can't be auto-scored — always a teacher review (like writing).
            return ItemGrade(
                item_id=item.id,
                awarded=0.0,
                max_points=writing_points(item),
                method="colour_manual",
                needs_review=True,
            )
        return self._grade_writing(item, response)

    def _grade_writing(self, item: OpenWritingItem, response: str | None) -> ItemGrade:
        # Manual mode, or LLM mode with no grader injected -> needs human review.
        if item.grade_mode == "manual" or self.llm_grader is None:
            if item.grade_mode == "llm":
                log.warning(
                    "grade writing item=%s wants llm but no grader -> review", item.id
                )
            return ItemGrade(
                item_id=item.id,
                awarded=0.0,
                max_points=writing_points(item),
                method=_writing_method(item),  # type: ignore[arg-type]
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
        awarded, max_points = verdict.awarded, verdict.max_points
        if item.points is not None and float(item.points) != verdict.max_points:
            # Per-item weight (e.g. a 2-mark Movers sentence): scale the grader's
            # band deterministically (`scale_writing_score`: half-up, whole marks).
            max_points = float(item.points)
            awarded = scale_writing_score(verdict.awarded, verdict.max_points, max_points)
            log.info(
                "grade writing item=%s scaled %.2f/%.2f -> %.2f/%.2f",
                item.id,
                verdict.awarded,
                verdict.max_points,
                awarded,
                max_points,
            )
        return ItemGrade(
            item_id=item.id,
            awarded=awarded,
            max_points=max_points,
            method="open_writing_llm",
            needs_review=verdict.needs_review,
            feedback=verdict.feedback,
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

        Choose-one writing (B1 Writing Part 2, several tasks written): a mark on a
        task of the group is out of the group's full maximum (5), never the
        uncounted task's 0. Marking the currently UNCOUNTED task makes it the
        counted one and the other task becomes uncounted (`awarded=0,
        max_points=0`, its previous mark kept in the feedback note). When several
        tasks of the group carry a stored teacher mark, the most recently marked
        one counts — so overlaying the stored marks in any order gives the same
        result.
        """
        target = next((g for g in result.items if g.item_id == item_id), None)
        if target is None:
            raise GradingError(f"item {item_id!r} not in grading result")
        group = self._choose_one_group(result, item_id)
        if group:
            return self._override_choose_one(result, target, group, awarded, max_points)
        max_pts = max_points if max_points is not None else target.max_points
        if not 0.0 <= awarded <= max_pts:
            raise GradingError(
                f"override {awarded} out of range [0, {max_pts}] for {item_id!r}"
            )
        overridden = _manual(target, awarded, max_pts)
        grades = [overridden if g.item_id == item_id else g for g in result.items]
        log.info(
            "override attempt=%s item=%s -> %.2f/%.2f",
            result.attempt_id,
            item_id,
            awarded,
            max_pts,
        )
        return _assemble(result.attempt_id, grades)

    def choose_one_groups(self, test_id: str) -> list[list[str]]:
        """The test's choose-one groups: the optional writing ids of one section
        (only sections with two or more). Cached per test id."""
        groups = self._groups.get(test_id)
        if groups is None:
            test = self.content.get_test(test_id)
            groups = []
            for section in [] if test is None else test.sections:
                ids = [
                    i.id
                    for i in section.items
                    if isinstance(i, OpenWritingItem) and i.optional
                ]
                if len(ids) > 1:
                    groups.append(ids)
            self._groups[test_id] = groups
        return groups

    def _choose_one_group(self, result: GradingResult, item_id: str) -> list[str]:
        """The graded members of `item_id`'s choose-one group (>= 2), else [].

        Only looked up when the result holds an uncounted writing task (several
        tasks written) — every other attempt costs no query.
        """
        if not any(_uncounted(g) for g in result.items):
            return []
        attempt = self.attempts.get_attempt(result.attempt_id)
        if attempt is None:
            return []
        graded = {g.item_id for g in result.items}
        for ids in self.choose_one_groups(attempt.test_id):
            if item_id in ids:
                members = [i for i in ids if i in graded]
                return members if len(members) > 1 else []
        return []

    def _override_choose_one(
        self,
        result: GradingResult,
        target: ItemGrade,
        group: list[str],
        awarded: float,
        max_points: float | None,
    ) -> GradingResult:
        by_id = {g.item_id: g for g in result.items}
        counted_now = next((i for i in group if not _uncounted(by_id[i])), group[0])
        full = max_points
        if full is None:
            full = by_id[counted_now].max_points or DEFAULT_WRITING_POINTS
        if not 0.0 <= awarded <= full:
            raise GradingError(
                f"override {awarded} out of range [0, {full}] for {target.item_id!r}"
            )
        # Which task counts: the most recently marked one among the group's stored
        # teacher marks; with none stored (a direct call), the task just marked if
        # it was uncounted, else the one already counted.
        stored = [
            mg
            for mg in self.attempts.list_manual_grades(result.attempt_id)
            if mg.item_id in group
        ]
        if stored:
            latest = max(stored, key=lambda mg: (mg.graded_at, -group.index(mg.item_id)))
            counted = latest.item_id
        elif _uncounted(target):
            counted = target.item_id
        else:
            counted = counted_now

        grades: list[ItemGrade] = []
        for g in result.items:
            if g.item_id not in group:
                grades.append(g)
                continue
            if g.item_id == target.item_id:
                g = _manual(g, awarded, full)
            if g.item_id == counted:
                if _uncounted(g):  # swapped in before its own stored mark is laid
                    g = g.model_copy(
                        update={"max_points": full, "needs_review": False, "feedback": None}
                    )
                grades.append(g)
            elif _uncounted(g):
                # Already uncounted: the teacher has now chosen -> nothing to review.
                grades.append(g.model_copy(update={"needs_review": False}))
            else:
                grades.append(
                    g.model_copy(
                        update={
                            "awarded": 0.0,
                            "max_points": 0.0,
                            "needs_review": False,
                            "feedback": (
                                f"{UNCOUNTED_PREFIX} (оценка {g.awarded:g}/{g.max_points:g})"
                            ),
                        }
                    )
                )
        log.info(
            "override choose-one attempt=%s item=%s -> %.2f/%.2f counted=%s group=%s",
            result.attempt_id,
            target.item_id,
            awarded,
            full,
            counted,
            group,
        )
        return _assemble(result.attempt_id, grades)

    @staticmethod
    def review_queue(result: GradingResult) -> list[ItemGrade]:
        """The items a human still has to finish (manual writing, LLM-flagged)."""
        return [g for g in result.items if g.needs_review]


# --------------------------------------------------------------------------- #
# Helpers (pure).
# --------------------------------------------------------------------------- #
_WRITING_METHODS = ("open_writing_llm", "open_writing_manual")
# Feedback of a task the teacher's choice took out of the count.
UNCOUNTED_PREFIX = "не засчитано: выбрано другое задание"


def item_max_points(item: Item) -> float:
    """What one item is worth, from its definition alone (no grading pass).

    The authored `points` when set, else the historical defaults: objective items
    1 (`DETERMINISTIC_POINTS`), open_writing / colour_task 5
    (`DEFAULT_WRITING_POINTS`). The same numbers `GradingService.grade` puts in
    `ItemGrade.max_points` (admin's manual marking without a report uses this)."""
    if isinstance(item, (OpenWritingItem, ColourTaskItem)):
        return writing_points(item)
    return objective_points(item)


def _uncounted(grade: ItemGrade) -> bool:
    """A choose-one writing task that is graded but not counted (max 0)."""
    return grade.max_points == 0 and grade.method in _WRITING_METHODS


def _manual(target: ItemGrade, awarded: float, max_points: float) -> ItemGrade:
    """The teacher's mark replacing one grade.

    Relabel a writing item as manually graded; keep an objective item's method
    (there is no generic "manual" GradeMethod, and relabelling a single_choice
    grade as open_writing_manual would be wrong).
    """
    method = "open_writing_manual" if target.method in _WRITING_METHODS else target.method
    return ItemGrade(
        item_id=target.item_id,
        awarded=awarded,
        max_points=max_points,
        method=method,  # type: ignore[arg-type]
        needs_review=False,
    )


def _writing_method(item: OpenWritingItem) -> str:
    return "open_writing_manual" if item.grade_mode == "manual" else "open_writing_llm"


def _assemble(attempt_id: str, grades: list[ItemGrade]) -> GradingResult:
    return GradingResult(
        attempt_id=attempt_id,
        items=grades,
        score=round(sum(g.awarded for g in grades), 2),
        max_score=round(sum(g.max_points for g in grades), 2),
        needs_review=any(g.needs_review for g in grades),
    )
