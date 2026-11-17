"""Deterministic grading: single_choice, matching, gap_fill.

Pure functions — given an authoring item and the canonical stored response, return
an `ItemGrade`. No clock, no randomness, no I/O (golden rule #2: they never look at
integrity data either). The response is already canonical: delivery de-shuffled the
single_choice option index and resolved the matching pool key before persisting, so
grading is a key/text comparison, never a display-order one.

gap_fill "acceptable misspellings" use a **length-aware edit distance**
(Damerau-Levenshtein: a transposition counts as one edit), guarded so that a
*different real word* is never accepted ("rain" for "train", "boats" for "boots"):
the response word must not be a known word (`app.grading.vocabulary`), not a form
of the key word, and must keep the key's first letter. Typo tolerance applies only
where Cambridge accepts recognisable spelling (listening); reading / writing open
cloze must be spelled correctly (`spelling_mode`). The teacher's manual override
stays the safety net for anything the rule gets wrong.
"""

from __future__ import annotations

import re
from typing import Literal

from rapidfuzz.distance import DamerauLevenshtein

from app.core.logging import get_logger
from app.grading.normalize import normalize
from app.grading.vocabulary import is_inflection_of, is_known_word
from contracts import GapFillItem, ItemGrade, MatchingItem, SingleChoiceItem
from contracts.content import Skill

log = get_logger(__name__)

# One point per objective item unless the item sets `points` (`objective_points`).
# Writing carries its own max_points (see llm.py / `GradingService`).
DETERMINISTIC_POINTS = 1.0

# Keys of up to this many letters must match exactly ("to"/"do", "most"/"must",
# "wild"/"mild", "coat"/"goat"): one edit on a short word is almost always another
# word, and the known-word list can't be complete.
MAX_EXACT_LEN = 4
# Keys of this many letters or more tolerate two edits ("bussines" for
# "business"); between the two bounds (5-7), one edit ("jaket" for "jacket").
TWO_TYPO_LEN = 8

# Skills whose gap_fill items are graded with exact spelling when the item does not
# say otherwise: Cambridge Reading open cloze (A2 Part 5, B1 Part 6) and Writing
# require correct spelling; Listening accepts recognisable misspellings.
EXACT_SPELLING_SKILLS = frozenset({"reading", "writing"})

_DIGIT = re.compile(r"\d")


def objective_points(item: SingleChoiceItem | MatchingItem | GapFillItem) -> float:
    """What one objective item is worth: its authored `points`, else 1."""
    return float(item.points) if item.points is not None else DETERMINISTIC_POINTS


def _grade(
    item: SingleChoiceItem | MatchingItem | GapFillItem, correct: bool, method: str
) -> ItemGrade:
    """All-or-nothing: a right answer earns every point of the item."""
    points = objective_points(item)
    return ItemGrade(
        item_id=item.id,
        awarded=points if correct else 0.0,
        max_points=points,
        method=method,  # type: ignore[arg-type]
    )


def grade_single_choice(item: SingleChoiceItem, response: str | None) -> ItemGrade:
    """Correct iff the canonical option key equals the authored `correct`."""
    correct = response is not None and response == item.correct
    log.debug("grade single_choice item=%s -> %s", item.id, correct)
    return _grade(item, correct, "single_choice")


def grade_matching(item: MatchingItem, response: str | None) -> ItemGrade:
    """Correct iff the chosen pool key equals the authored `correct`."""
    correct = response is not None and response == item.correct
    log.debug("grade matching item=%s -> %s", item.id, correct)
    return _grade(item, correct, "matching")


# --------------------------------------------------------------------------- #
# gap_fill typo tolerance (pure helpers).
# --------------------------------------------------------------------------- #
def allowed_typos(key: str) -> int:
    """Edits tolerated against one normalized key, by the key's length in letters.

    <= 4 letters: exact only; 5-7: one edit; >= 8: two edits. A key containing a
    digit (a time, a price, a date) is always exact — "7.40" is not "7.30".
    """
    if _DIGIT.search(key):
        return 0
    n = len(key.replace(" ", ""))
    if n <= MAX_EXACT_LEN:
        return 0
    return 2 if n >= TWO_TYPO_LEN else 1


def _token_typo(r_tok: str, k_tok: str) -> int | None:
    """Edit distance of one differing word if it is a plain typo, else None.

    Never a typo: a key word of <= 4 letters or with a digit; a different first
    letter ("goat"/"coat", "packet"/"jacket"); a response that is itself a known
    word ("rain", "boats", "stairs", "thirsty" — `is_known_word`); another form of
    the key word ("trains", "produce", "cheaper", "women", "eleven" —
    `is_inflection_of`).
    """
    budget = allowed_typos(k_tok)
    if budget == 0 or _DIGIT.search(r_tok):
        return None
    if not r_tok or r_tok[0] != k_tok[0]:
        return None  # first-letter substitution / insertion / deletion
    if is_known_word(r_tok) or is_inflection_of(r_tok, k_tok):
        return None  # a real word / another form of the key word, not a typo
    dist = DamerauLevenshtein.distance(r_tok, k_tok, score_cutoff=budget)
    return dist if dist <= budget else None


def typo_match(response: str, key: str) -> int | None:
    """Edit distance if `response` is an acceptable misspelling of `key`, else None.

    Both arguments are already normalized; deterministic (depends only on the two
    strings). Word-by-word when both have the same number of words: every differing
    word must be a plain typo within ITS OWN length budget ("the elefant" yes,
    "a elephant" / "throw a ball" no). With a different number of words only a
    missing / extra space is tolerated ("news paper", "threwa ball"): an added or
    dropped WORD ("a elephant" for "elephant") is never a typo.
    """
    r_tokens, k_tokens = response.split(), key.split()
    if len(r_tokens) == len(k_tokens):
        total = 0
        for r_tok, k_tok in zip(r_tokens, k_tokens, strict=True):
            if r_tok == k_tok:
                continue
            dist = _token_typo(r_tok, k_tok)
            if dist is None:
                return None
            total += dist
        return total if total <= allowed_typos(key) else None
    if allowed_typos(key) == 0 or "".join(r_tokens) != "".join(k_tokens):
        return None
    return abs(len(r_tokens) - len(k_tokens))  # one edit per missing / extra space


SpellingMode = Literal["exact", "tolerant"]


def spelling_mode(item: GapFillItem, skill: Skill | None) -> SpellingMode:
    """The item's own `spelling`, else derived from its section's skill.

    reading / writing -> "exact"; listening -> "tolerant"; a section without a
    skill keeps the historical "tolerant" behaviour.
    """
    if item.spelling is not None:
        return item.spelling
    return "exact" if skill in EXACT_SPELLING_SKILLS else "tolerant"


def grade_gap_fill(
    item: GapFillItem,
    response: str | None,
    *,
    fuzzy: bool = True,
    skill: Skill | None = None,
) -> ItemGrade:
    """Normalize, then: exact key -> `rejected` -> length-aware typo tolerance.

    1. exact (normalized) `accepted` or `accepted_variants` -> correct;
    2. exact (normalized) `rejected` -> wrong (the key's explicit "not this");
    3. typo tolerance (`typo_match`) against accepted + accepted_variants — unless
       the response is at least as close to a `rejected` form. Step 3 runs only
       when `fuzzy` is on AND the item's spelling mode is "tolerant"
       (`spelling_mode`: the item's `spelling`, else exact for a reading /
       writing section, tolerant for listening).
    """
    if response is None or not response.strip():
        log.debug("grade gap_fill item=%s -> blank", item.id)
        return _grade(item, False, "gap_fill")

    norm = normalize(response)
    keys = [normalize(a) for a in (*item.accepted, *item.accepted_variants)]
    rejected = [normalize(r) for r in item.rejected]

    if norm in keys:
        log.debug("grade gap_fill item=%s -> exact", item.id)
        return _grade(item, True, "gap_fill")

    if norm in rejected:
        log.debug("grade gap_fill item=%s -> rejected form", item.id)
        return _grade(item, False, "gap_fill")

    if fuzzy and spelling_mode(item, skill) == "exact":
        log.debug("grade gap_fill item=%s -> wrong (exact spelling, skill=%s)", item.id, skill)
        return _grade(item, False, "gap_fill")
    if fuzzy:
        hits = [d for d in (typo_match(norm, k) for k in keys) if d is not None]
        if hits:
            best = min(hits)
            near_rejected = [
                DamerauLevenshtein.distance(norm, r) for r in rejected if r
            ]
            if near_rejected and min(near_rejected) <= best:
                log.debug(
                    "grade gap_fill item=%s -> typo of a rejected form", item.id
                )
                return _grade(item, False, "gap_fill")
            log.debug("grade gap_fill item=%s -> typo (%d edit(s))", item.id, best)
            return _grade(item, True, "gap_fill")

    log.debug("grade gap_fill item=%s -> wrong", item.id)
    return _grade(item, False, "gap_fill")
