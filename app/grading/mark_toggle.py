"""`mark_toggle` grading — one SECTION at a time (pure, deterministic).

Book rule ("put an X on every pair that is NOT identical"; 1 point per correct X):

* a row with `correct=True` is worth 1 point and earns it when marked;
* a row with `correct=False` is worth 0 points, but every FALSE mark cancels one
  correct mark of the same section; the section never goes below 0.

So marking every row earns nothing (anti "mark everything"). The cancellation is
written into the per-item grades so that `GradingResult.score` (the plain sum of
`awarded`) is right and every point is explained:

* a false mark -> `awarded=0, max_points=0`, feedback "ложная отметка: −1 …";
* the cancelled correct marks are the LAST correctly marked rows of the section
  (authored order — deterministic): `awarded=0, max_points=1`, feedback
  "погашено ложной отметкой (<false row id>)";
* the remaining correct marks keep their 1 point; an unmarked correct row is 0/1.

Section raw = max(0, correct marks - false marks). A teacher override replaces one
row's grade like any objective item (0..max_points).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.core.logging import get_logger
from contracts import ItemGrade, MarkToggleItem

log = get_logger(__name__)

MARKED = "1"
MARK_TOGGLE_POINTS = 1.0


def is_marked(response: str | None) -> bool:
    """ "1" = marked; "0", "" or no answer = unmarked (anything else: unmarked)."""
    return (response or "").strip() == MARKED


def mark_toggle_max_points(item: MarkToggleItem) -> float:
    """1 for a row that should be marked, 0 for a distractor row."""
    return MARK_TOGGLE_POINTS if item.correct else 0.0


def grade_mark_toggle_section(
    items: Sequence[MarkToggleItem], responses: Mapping[str, str | None]
) -> list[ItemGrade]:
    """Grade the mark_toggle rows of ONE section (see the module rule).

    Returns one `ItemGrade` per row, in the given order."""
    marked = {it.id: is_marked(responses.get(it.id)) for it in items}
    hits = [it for it in items if it.correct and marked[it.id]]
    false_marks = [it for it in items if not it.correct and marked[it.id]]
    # The last `len(false_marks)` correct marks are cancelled (floor 0).
    cancelled_ids = [it.id for it in hits[len(hits) - min(len(hits), len(false_marks)) :]]
    cancelled_by = dict(zip(cancelled_ids, (f.id for f in false_marks), strict=False))

    grades: list[ItemGrade] = []
    for it in items:
        max_points = mark_toggle_max_points(it)
        if not it.correct:
            grades.append(
                ItemGrade(
                    item_id=it.id,
                    awarded=0.0,
                    max_points=0.0,
                    method="mark_toggle",
                    feedback="ложная отметка: −1 (гасит одну верную)" if marked[it.id] else None,
                )
            )
        elif it.id in cancelled_by:
            grades.append(
                ItemGrade(
                    item_id=it.id,
                    awarded=0.0,
                    max_points=max_points,
                    method="mark_toggle",
                    feedback=f"погашено ложной отметкой ({cancelled_by[it.id]})",
                )
            )
        else:
            grades.append(
                ItemGrade(
                    item_id=it.id,
                    awarded=max_points if marked[it.id] else 0.0,
                    max_points=max_points,
                    method="mark_toggle",
                )
            )
    raw = max(0, len(hits) - len(false_marks))
    log.debug(
        "grade mark_toggle rows=%d hits=%d false=%d cancelled=%d raw=%d",
        len(items),
        len(hits),
        len(false_marks),
        len(cancelled_ids),
        raw,
    )
    return grades
