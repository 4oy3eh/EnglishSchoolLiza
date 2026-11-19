"""gap_fill number equivalence: a number written as digits or as words.

The teacher's global rule: "12" == "twelve", "21" == "twenty-one" == "twenty one",
"105" == "one hundred and five", "3rd" == "third" — for every accepted answer, in
every spelling mode, without per-item variants. Numbers compare EXACTLY BY VALUE, so
a different number never matches ("seven"/"eleven", "13"/"30", "fifteen"/"fifty"),
the `rejected` list still wins, and a misspelt number word ("twelv") is accepted only
in tolerant mode and only when it unambiguously misspells the same number.
"""

from __future__ import annotations

import logging

import pytest

from app.grading import grade_gap_fill
from app.grading.deterministic import allowed_typos
from app.grading.numbers import (
    NUMBER_VOCAB,
    canonical_numbers,
    nearest_number_word,
    ordinal_suffix,
)
from app.grading.vocabulary import is_known_word
from contracts import GapFillItem
from contracts.content import Skill


def _gap(*accepted: str, **kw: object) -> GapFillItem:
    return GapFillItem(id="n", prompt="p", accepted=list(accepted), **kw)  # type: ignore[arg-type]


def _ok(
    item: GapFillItem, response: str, skill: Skill | None = "listening", fuzzy: bool = True
) -> bool:
    return grade_gap_fill(item, response, skill=skill, fuzzy=fuzzy).awarded > 0


# --------------------------------------------------------------------------- #
# canonical_numbers — the pure rewrite.
# --------------------------------------------------------------------------- #
CANONICAL = [
    ("zero", "0"),
    ("twelve", "12"),
    ("twenty", "20"),
    ("twenty-one", "21"),
    ("twenty one", "21"),
    ("forty five", "45"),
    ("ninety-nine", "99"),
    ("hundred", "100"),
    ("a hundred", "100"),
    ("one hundred", "100"),
    ("one-hundred", "100"),
    ("one hundred and five", "105"),
    ("a hundred and five", "105"),
    ("one hundred five", "105"),
    ("two hundred and fifty", "250"),
    ("nine hundred and ninety-nine", "999"),
    ("first", "1st"),
    ("second", "2nd"),
    ("third", "3rd"),
    ("eleventh", "11th"),
    ("twelfth", "12th"),
    ("twentieth", "20th"),
    ("twenty-first", "21st"),
    ("twenty second", "22nd"),
    ("one hundred and third", "103rd"),
    ("hundredth", "100th"),
    ("two hundredth", "200th"),
    ("twelve apples", "12 apples"),
    ("sixty-five pounds", "65 pounds"),
    ("the twentieth of august", "the 20th of august"),
    ("five and six", "5 and 6"),  # "and" joins only after "hundred"
    ("hundred and apples", "100 and apples"),  # a dangling "and" stays a word
    ("twenty, one", "20, 1"),  # a comma splits two numbers
    ("twenty zero", "20 0"),
    ("someone", "someone"),  # only whole words
    ("a", "a"),
    ("a boy", "a boy"),
    ("7.30", "7.30"),  # digits are never rewritten
    ("05", "05"),
    ("21/06", "21/06"),
    ("", ""),
]


@pytest.mark.parametrize(("text", "expected"), CANONICAL)
def test_canonical_numbers(text: str, expected: str) -> None:
    assert canonical_numbers(text) == expected


def test_canonical_numbers_covers_every_value_up_to_999() -> None:
    units = "one two three four five six seven eight nine".split()
    teens = (
        "ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen"
    ).split()
    tens = "twenty thirty forty fifty sixty seventy eighty ninety".split()

    def words(n: int) -> list[str]:
        out = []
        if n >= 100:
            out.append(f"{units[n // 100 - 1]} hundred")
            n %= 100
            if n:
                out.append("and")
        if n >= 20:
            out.append(tens[n // 10 - 2] + (f"-{units[n % 10 - 1]}" if n % 10 else ""))
        elif n >= 10:
            out.append(teens[n - 10])
        elif n:
            out.append(units[n - 1])
        return out

    for n in range(1, 1000):
        assert canonical_numbers(" ".join(words(n))) == str(n), n


def test_ordinal_suffix() -> None:
    cases = {1: "st", 2: "nd", 3: "rd", 4: "th", 11: "th", 12: "th", 13: "th",
             21: "st", 22: "nd", 23: "rd", 101: "st", 111: "th", 112: "th"}
    for value, suffix in cases.items():
        assert ordinal_suffix(value) == suffix, value


# --------------------------------------------------------------------------- #
# Grading: digits <-> words, both directions, every spelling mode.
# --------------------------------------------------------------------------- #
EQUIVALENT = [
    ("12", "twelve"),
    ("twelve", "12"),
    ("20", "twenty"),
    ("twenty", "20"),
    ("21", "twenty-one"),
    ("21", "twenty one"),
    ("21", "Twenty-One"),
    ("twenty-one", "twenty one"),
    ("twenty one", "21"),
    ("100", "a hundred"),
    ("100", "one hundred"),
    ("105", "one hundred and five"),
    ("one hundred and five", "105"),
    ("0", "zero"),
    ("3rd", "third"),
    ("third", "3rd"),
    ("21st", "twenty-first"),
    ("12 apples", "twelve apples"),
    ("twelve apples", "12 apples"),
]


@pytest.mark.parametrize(("key", "response"), EQUIVALENT)
@pytest.mark.parametrize("skill", ["listening", "reading", "writing", None])
def test_digits_and_words_are_equivalent(key: str, response: str, skill: Skill | None) -> None:
    assert _ok(_gap(key), response, skill)


@pytest.mark.parametrize(("key", "response"), EQUIVALENT)
def test_number_equivalence_is_not_a_spelling_tolerance(key: str, response: str) -> None:
    """`fuzzy=False` (exact only everywhere) still applies the number rule."""
    assert _ok(_gap(key), response, fuzzy=False)


def test_number_equivalence_applies_to_variants_too() -> None:
    item = _gap("august", accepted_variants=["20th of August"])
    assert _ok(item, "twentieth of august")


DIFFERENT_NUMBERS = [
    ("seven", "eleven"),
    ("eleven", "seven"),
    ("7", "eleven"),
    ("11", "seven"),
    ("13", "30"),
    ("13", "thirty"),
    ("thirteen", "30"),
    ("30", "thirteen"),
    ("15", "fifty"),
    ("fifteen", "fifty"),
    ("fifty", "15"),
    ("19", "ninety"),
    ("19", "nine"),
    ("21", "twenty"),
    ("21", "twenty-two"),
    ("21", "twenty first"),  # 21st is not 21
    ("3rd", "three"),
    ("third", "3"),
    ("12", "twelfth"),
    ("12", "120"),
    ("105", "one hundred fifty"),
    ("12 apples", "eleven apples"),
    ("12", "twelve apples"),  # an extra word is still an extra word
    ("12", "1 2"),
]


@pytest.mark.parametrize(("key", "response"), DIFFERENT_NUMBERS)
@pytest.mark.parametrize("skill", ["listening", "reading"])
def test_a_different_number_never_matches(key: str, response: str, skill: Skill) -> None:
    assert not _ok(_gap(key), response, skill)


# --------------------------------------------------------------------------- #
# rejected still wins.
# --------------------------------------------------------------------------- #
def test_rejected_word_form_beats_the_number_rule() -> None:
    """A teacher who wants digits only rejects the word explicitly."""
    item = _gap("12", rejected=["twelve"])
    assert _ok(item, "12")
    assert not _ok(item, "twelve")
    assert not _ok(item, "Twelve ")


def test_rejected_value_beats_an_accepted_spelling_of_it() -> None:
    """A rejected form rejects every spelling of its value, except the literal key."""
    item = _gap("twelve", rejected=["12"])
    assert _ok(item, "twelve")
    assert not _ok(item, "12")
    item = _gap("21", rejected=["twenty one"])
    assert _ok(item, "21")
    assert not _ok(item, "twenty-one")


def test_rejected_value_beats_a_misspelt_number_word() -> None:
    item = _gap("12", rejected=["twelve"])
    assert not _ok(item, "twelv")


# --------------------------------------------------------------------------- #
# Misspelt number words: tolerant only, and only when unambiguous.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("key", "response"),
    [
        ("12", "twelv"),
        ("19", "ninteen"),
        ("15", "fivteen"),
        ("40", "fourty"),  # forty, not fourth (Indel tie-break)
        ("45", "fourty-five"),
        ("45", "fourty five"),
        ("9th", "nineth"),
        ("12th", "twelth"),
        ("17", "seventeeen"),
        ("twelve", "twelv"),  # the literal-key typo path agrees
        ("forty", "fourty"),
    ],
)
def test_misspelt_number_word_is_accepted_when_tolerant(key: str, response: str) -> None:
    assert _ok(_gap(key), response, "listening")


@pytest.mark.parametrize(("key", "response"), [("12", "twelv"), ("twelve", "twelv"),
                                               ("40", "fourty"), ("19", "ninteen")])
def test_misspelt_number_word_is_wrong_when_exact(key: str, response: str) -> None:
    assert not _ok(_gap(key), response, "reading")
    assert not _ok(_gap(key, spelling="exact"), response, "listening")


@pytest.mark.parametrize(
    ("key", "response"),
    [
        ("4th", "fourty"),  # "fourty" reads as forty, never fourth
        ("fourth", "fourty"),
        ("40", "fourtee"),  # reads as fourteen
        ("15", "fifty"),  # a real number word is never a typo
        ("15", "fiftey"),  # reads as fifty
        ("50", "fiften"),  # reads as fifteen
        ("16", "sixtey"),  # reads as sixty
        ("13", "thirtey"),  # reads as thirty
        ("11", "sevn"),  # reads as seven
        ("7", "elevn"),  # reads as eleven
        ("10", "tenn"),  # 3-letter number words are exact
        ("21", "twentyone"),  # one word: not parsed (documented)
    ],
)
def test_misspelt_number_word_never_becomes_another_number(key: str, response: str) -> None:
    assert not _ok(_gap(key), response, "listening")


def test_nearest_number_word_is_unambiguous_and_skips_real_words() -> None:
    assert nearest_number_word("twelv", allowed_typos) == "twelve"
    assert nearest_number_word("fourty", allowed_typos) == "forty"
    # Real words are never re-read as a number ("for", "tree", "won", "ate").
    for word in ("for", "tree", "won", "ate", "to", "tin", "fiend", "seven", "eleven"):
        assert nearest_number_word(word, allowed_typos) is None, word
    # Short number words (<= 4 letters) tolerate nothing.
    assert nearest_number_word("fiv", allowed_typos) is None
    assert nearest_number_word("nin", allowed_typos) is None
    # First-letter changes are never typos.
    assert nearest_number_word("welve", allowed_typos) is None


def test_no_number_word_is_a_typo_of_another() -> None:
    """Every number word is a known word, so it is never re-read as another number."""
    for word in NUMBER_VOCAB:
        assert is_known_word(word), word
        assert nearest_number_word(word, allowed_typos) is None


def test_number_typo_needs_the_key_to_be_a_number() -> None:
    """The rewrite can only ever help a key that holds that number."""
    assert not _ok(_gap("apples"), "twelv")
    assert not _ok(_gap("tree"), "three")


# --------------------------------------------------------------------------- #
# Real keys + logging.
# --------------------------------------------------------------------------- #
def test_starters_vol2_listening_keys_accept_words() -> None:
    """Starters 2018 Vol 2 L2 Q1 `12` / Q4 `20` (listening, no `spelling` set)."""
    for key, word, typo, other in (
        ("12", "twelve", "twelv", "eleven"),
        ("20", "twenty", "tweny", "twenty one"),
    ):
        item = _gap(key)
        assert _ok(item, word)
        assert _ok(item, key)
        assert _ok(item, typo)  # tolerant listening default
        assert not _ok(item, typo, "reading")
        assert not _ok(item, other)


def test_a2_key_2020_q7_q9_keys() -> None:
    """A2 Key 2020 L2 Q7 `19` / Q9 `65` accept the words with no variants needed."""
    assert _ok(_gap("19"), "nineteen")
    assert not _ok(_gap("19"), "ninety")
    assert not _ok(_gap("19"), "18")
    assert _ok(_gap("65"), "sixty-five")
    assert _ok(_gap("65"), "sixty five")
    assert not _ok(_gap("65"), "fifty-six")


def test_number_equivalence_is_logged_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="app.grading.deterministic"):
        grade_gap_fill(_gap("12"), "twelve", skill="reading")
        grade_gap_fill(_gap("12"), "twelv", skill="listening")
    messages = [r.getMessage() for r in caplog.records]
    assert any("number equivalence" in m and "'12'" in m for m in messages)
    assert any("typo of a number word" in m for m in messages)
