"""Deterministic grading: single_choice, matching, gap_fill.

Pure functions — given an authoring item and the canonical stored response, return
an `ItemGrade`. No clock, no randomness, no I/O (golden rule #2: they never look at
integrity data either). The response is already canonical: delivery de-shuffled the
single_choice option index and resolved the matching pool key before persisting, so
grading is a key/text comparison, never a display-order one.
"""

from __future__ import annotations

from rapidfuzz import fuzz

from app.core.logging import get_logger
from app.grading.normalize import normalize
from contracts import GapFillItem, ItemGrade, MatchingItem, SingleChoiceItem

log = get_logger(__name__)

# One point per objective item. Writing carries its own max_points (see llm.py).
DETERMINISTIC_POINTS = 1.0

# Default rapidfuzz acceptance ratio (0-100) for "acceptable misspellings".
DEFAULT_FUZZY_THRESHOLD = 85.0

# Below this length a fuzzy match is too easy to hit by accident (e.g. "to"/"do"),
# so short answers must match exactly via accepted / accepted_variants.
MIN_FUZZY_LEN = 4


def _grade(item_id: str, correct: bool, method: str) -> ItemGrade:
    return ItemGrade(
        item_id=item_id,
        awarded=DETERMINISTIC_POINTS if correct else 0.0,
        max_points=DETERMINISTIC_POINTS,
        method=method,  # type: ignore[arg-type]
    )


def grade_single_choice(item: SingleChoiceItem, response: str | None) -> ItemGrade:
    """Correct iff the canonical option key equals the authored `correct`."""
    correct = response is not None and response == item.correct
    log.debug("grade single_choice item=%s -> %s", item.id, correct)
    return _grade(item.id, correct, "single_choice")


def grade_matching(item: MatchingItem, response: str | None) -> ItemGrade:
    """Correct iff the chosen pool key equals the authored `correct`."""
    correct = response is not None and response == item.correct
    log.debug("grade matching item=%s -> %s", item.id, correct)
    return _grade(item.id, correct, "matching")


def grade_gap_fill(
    item: GapFillItem,
    response: str | None,
    *,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
) -> ItemGrade:
    """Normalize, then: exact `accepted` -> exact `accepted_variants` -> fuzzy.

    The fuzzy step (rapidfuzz ratio over the normalized `accepted` list) catches
    Cambridge "acceptable misspellings" the author did not enumerate. Short answers
    skip it so a one-character word can't fuzzily match an unrelated one.
    """
    if response is None or not response.strip():
        log.debug("grade gap_fill item=%s -> blank", item.id)
        return _grade(item.id, False, "gap_fill")

    norm = normalize(response)
    accepted = [normalize(a) for a in item.accepted]
    variants = [normalize(v) for v in item.accepted_variants]

    if norm in accepted or norm in variants:
        log.debug("grade gap_fill item=%s -> exact", item.id)
        return _grade(item.id, True, "gap_fill")

    if len(norm) >= MIN_FUZZY_LEN and accepted:
        best = max(fuzz.ratio(norm, a) for a in accepted)
        if best >= fuzzy_threshold:
            log.debug("grade gap_fill item=%s -> fuzzy %.1f", item.id, best)
            return _grade(item.id, True, "gap_fill")

    log.debug("grade gap_fill item=%s -> wrong", item.id)
    return _grade(item.id, False, "gap_fill")
