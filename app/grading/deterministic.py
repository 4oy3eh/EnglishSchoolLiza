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
from app.grading.kwt import TWO_ANSWERS_RE, answer_word_count
from app.grading.normalize import normalize
from app.grading.numbers import (
    NUMBER_VOCAB,
    canonical_numbers,
    nearest_number_word,
    number_form,
)
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
EXACT_SPELLING_SKILLS = frozenset({"reading", "writing", "use_of_english"})
# Levels whose LISTENING gap fills need correct spelling too (C1: "Correct spelling
# is expected at this level"; C2: "They must be spelled correctly"). B2 listening:
# "Minor spelling errors are not penalised" -> tolerant, like A2 / B1 / YLE.
EXACT_LISTENING_LEVELS = frozenset({"C1_ADVANCED", "C2_PROFICIENCY"})
# Punctuation around a one-word answer is ignored ("The absence or misuse of
# punctuation is ignored"): stripped from both ends of a `max_words` answer + keys.
_EDGE_PUNCT = " .,;:!?\"()[]{}“”«»"

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
    `is_inflection_of`); for a number-word key, a response that is not
    unambiguously closest to THAT number word (`nearest_number_word`: "fourty" is
    "forty", never "fourth").
    """
    budget = allowed_typos(k_tok)
    if budget == 0 or _DIGIT.search(r_tok):
        return None
    if not r_tok or r_tok[0] != k_tok[0]:
        return None  # first-letter substitution / insertion / deletion
    if is_known_word(r_tok) or is_inflection_of(r_tok, k_tok):
        return None  # a real word / another form of the key word, not a typo
    if (
        k_tok in NUMBER_VOCAB
        and nearest_number_word(r_tok, allowed_typos, number_form(k_tok)) != k_tok
    ):
        return None  # a misspelt number word must point at THIS number, unambiguously
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


def spelling_mode(
    item: GapFillItem, skill: str | None, *, level: str | None = None
) -> SpellingMode:
    """The item's own `spelling`, else derived from its section's skill + level.

    `skill` is the section's score skill (`Section.score_skill`: `reported_skill`
    or `skill`). reading / writing / use_of_english -> "exact"; listening ->
    "exact" at C1 / C2 (`EXACT_LISTENING_LEVELS`), else "tolerant"; a section
    without a skill keeps the historical "tolerant" behaviour.
    """
    if item.spelling is not None:
        return item.spelling
    if skill in EXACT_SPELLING_SKILLS:
        return "exact"
    if skill == "listening" and level in EXACT_LISTENING_LEVELS:
        return "exact"
    return "tolerant"


def _strip_edges(text: str) -> str:
    return text.strip(_EDGE_PUNCT)


def grade_gap_fill(
    item: GapFillItem,
    response: str | None,
    *,
    fuzzy: bool = True,
    skill: Skill | str | None = None,
    level: str | None = None,
) -> ItemGrade:
    """Normalize, then: exact key -> `rejected` -> number value -> typo tolerance.

    1. exact (normalized) `accepted` or `accepted_variants` -> correct;
    2. exact (normalized) `rejected` -> wrong (the key's explicit "not this");
    2b. number equivalence (`canonical_numbers`, every spelling mode, also with
       `fuzzy` off): numbers written in words are compared as digits, by value —
       a `rejected` form with the same value -> wrong, else an accepted / variant
       with the same value -> correct ("twelve" for "12", "21st" for
       "twenty-first", "12 apples" for "twelve apples");
    3. typo tolerance (`typo_match`) against accepted + accepted_variants — unless
       the response is at least as close to a `rejected` form. Step 3 runs only
       when `fuzzy` is on AND the item's spelling mode is "tolerant"
       (`spelling_mode`: the item's `spelling`, else exact for a reading /
       writing section, tolerant for listening). A misspelt number word counts
       only when it unambiguously misspells the same number
       (`canonical_numbers(..., typo_budget=allowed_typos)`).

    `max_words` (open cloze / word formation = 1) is a hard rule checked first: two
    answers given ("in/on") or more words than allowed (contractions count as two,
    `count_words`; B2: except can't) -> wrong; punctuation around the answer is
    ignored (stripped from both ends of the response and the keys).
    """
    if response is None or not response.strip():
        log.debug("grade gap_fill item=%s -> blank", item.id)
        return _grade(item, False, "gap_fill")

    norm = normalize(response)
    keys = [normalize(a) for a in (*item.accepted, *item.accepted_variants)]
    rejected = [normalize(r) for r in item.rejected]
    if item.max_words is not None:
        norm = _strip_edges(norm)
        keys = [_strip_edges(k) for k in keys]
        rejected = [_strip_edges(r) for r in rejected]
        # An answer that IS an accepted key is never failed by the hard rules.
        if norm not in keys:
            if TWO_ANSWERS_RE.search(norm):
                log.debug("grade gap_fill item=%s -> two answers given", item.id)
                return _grade(item, False, "gap_fill")
            words = answer_word_count(norm, level)
            if words > item.max_words:
                log.debug(
                    "grade gap_fill item=%s -> %d words > max %d",
                    item.id,
                    words,
                    item.max_words,
                )
                return _grade(item, False, "gap_fill")

    if norm in keys:
        log.debug("grade gap_fill item=%s -> exact", item.id)
        return _grade(item, True, "gap_fill")

    if norm in rejected:
        log.debug("grade gap_fill item=%s -> rejected form", item.id)
        return _grade(item, False, "gap_fill")

    num = canonical_numbers(norm)
    num_keys = {canonical_numbers(k) for k in keys}
    num_rejected = {canonical_numbers(r) for r in rejected}
    if num in num_rejected:
        log.debug("grade gap_fill item=%s -> rejected form (number equivalence)", item.id)
        return _grade(item, False, "gap_fill")
    if num in num_keys:
        log.debug(
            "grade gap_fill item=%s -> correct via number equivalence (%r)", item.id, num
        )
        return _grade(item, True, "gap_fill")

    if fuzzy and spelling_mode(item, skill, level=level) == "exact":
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
        for key in keys:  # the key's own form breaks a cardinal/ordinal tie
            num_typo = canonical_numbers(
                norm, typo_budget=allowed_typos, prefer=number_form(key)
            )
            if (
                num_typo != num
                and num_typo == canonical_numbers(key)
                and num_typo not in num_rejected
            ):
                log.debug(
                    "grade gap_fill item=%s -> typo of a number word via number "
                    "equivalence (%r)",
                    item.id,
                    num_typo,
                )
                return _grade(item, True, "gap_fill")

    log.debug("grade gap_fill item=%s -> wrong", item.id)
    return _grade(item, False, "gap_fill")
