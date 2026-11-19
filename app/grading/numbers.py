"""Number equivalence for `gap_fill`: a number written as digits or as words.

The teacher's rule: when a gap_fill answer is a number, writing it as a word must
count too, and vice versa — "12" == "twelve", "21" == "twenty-one" == "twenty one",
"105" == "one hundred and five" == "a hundred and five". It is a global rule, so no
item needs a per-item variant for it.

`canonical_numbers(text)` rewrites every number written in words inside an already
normalized string (`app.grading.normalize`) into digits; everything else is kept
character for character. Grading compares the canonical response with the
canonical keys, so the comparison is EXACT BY VALUE: "seven" never equals "11",
"fifteen" never equals "50", "13" never equals "30".

Supported (pure, deterministic):

* cardinals 0..999: "zero".."nineteen", the tens, tens + unit joined by a space or a
  hyphen ("twenty one", "twenty-one"), "hundred" with an optional "one" / "a" / unit
  before it and an optional "and" after it ("one hundred and five", "a hundred",
  "two hundred fifty");
* ordinals up to 999th: the last word of a number may be an ordinal ("third" ->
  "3rd", "twenty-first" -> "21st", "one hundred and second" -> "102nd",
  "hundredth" -> "100th"), so "3rd" == "third";
* number words inside longer answers ("twelve apples" -> "12 apples").

Not supported (documented, stays a key-literal comparison): "thousand" and larger,
fractions / decimals, a tens + unit written as one word ("twentyone"), digits are
never rewritten (so "05" stays "05" and "7.30" stays "7.30").

`typo_budget` (optional): with a budget function (the caller's `allowed_typos`), a
word that is NOT a number word and NOT a known word is read as the number word it
misspells — but only when that is unambiguous: same first letter, within that number
word's own budget, and the single closest number word (Damerau-Levenshtein, then
Indel distance to break a tie: "fourty" -> "forty", not "fourth"). A known word is
never re-read ("seven" stays seven, "for" stays for), so a typo can never turn into
a different number. The caller only passes a budget in "tolerant" spelling mode.

`prefer` ("cardinal" / "ordinal", from the KEY being compared): a typo equally close
(Damerau-Levenshtein) to a cardinal and to ITS OWN ordinal — "eigth" (eight / eighth),
"ninty" is not one (ninety only), "twelth" (twelfth only) — resolves to the form the
key uses: key "eight" / "8" -> eight, key "eighth" / "8th" -> eighth. Without a
preference the Indel tie-break applies as before. Ties between DIFFERENT numbers are
never resolved by `prefer`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from functools import lru_cache

from rapidfuzz.distance import DamerauLevenshtein, Indel

from app.grading.vocabulary import is_known_word

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9,
}
_TEENS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_UNIT_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9,
}
_TEEN_ORDINALS = {
    "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
    "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
    "nineteenth": 19,
}
_TENS_ORDINALS = {
    "twentieth": 20, "thirtieth": 30, "fortieth": 40, "fiftieth": 50,
    "sixtieth": 60, "seventieth": 70, "eightieth": 80, "ninetieth": 90,
}
_HUNDRED = "hundred"
_HUNDREDTH = "hundredth"
_AND = "and"
_A = "a"

# Every word the parser reads as (part of) a number — the typo targets.
NUMBER_VOCAB: frozenset[str] = frozenset(
    {*_UNITS, *_TEENS, *_TENS, *_UNIT_ORDINALS, *_TEEN_ORDINALS, *_TENS_ORDINALS,
     _HUNDRED, _HUNDREDTH}
)

# Split into letter runs (odd indices) and everything between them (even indices).
_LETTERS = re.compile(r"([a-z]+)")
# Separators that join two words of ONE number ("twenty one", "twenty-one").
_JOINERS = frozenset({" ", "-"})

TypoBudget = Callable[[str], int]
NumberForm = str  # "cardinal" | "ordinal"

_ORDINAL_WORDS = frozenset({*_UNIT_ORDINALS, *_TEEN_ORDINALS, *_TENS_ORDINALS, _HUNDREDTH})
_WORD_VALUE: dict[str, int] = {
    **_UNITS, **_TEENS, **_TENS, **_UNIT_ORDINALS, **_TEEN_ORDINALS, **_TENS_ORDINALS,
    _HUNDRED: 100, _HUNDREDTH: 100,
}  # fmt: skip
_ORDINAL_DIGITS = re.compile(r"\d+(?:st|nd|rd|th)\b")
_CARDINAL_DIGITS = re.compile(r"\d+(?!\d|st|nd|rd|th)")


def number_form(text: str) -> NumberForm | None:
    """Whether a (normalized) key spells its number as a cardinal or an ordinal
    ("8" / "eight" -> cardinal, "8th" / "eighth" -> ordinal); None when it holds no
    number or both forms."""
    words = _LETTERS.findall(text)
    ordinal = any(w in _ORDINAL_WORDS for w in words) or bool(_ORDINAL_DIGITS.search(text))
    cardinal = any(w in NUMBER_VOCAB and w not in _ORDINAL_WORDS for w in words) or bool(
        _CARDINAL_DIGITS.search(_ORDINAL_DIGITS.sub(" ", text))
    )
    if ordinal == cardinal:
        return None
    return "ordinal" if ordinal else "cardinal"
# (value, is_ordinal, index of the first word after the number)
_Parsed = tuple[int, bool, int]


def ordinal_suffix(value: int) -> str:
    """English ordinal suffix: 1st 2nd 3rd 4th … 11th 12th 13th … 21st 101st 111th."""
    if 11 <= value % 100 <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")


def _format(value: int, ordinal: bool) -> str:
    return f"{value}{ordinal_suffix(value)}" if ordinal else str(value)


def _below_hundred(words: list[str], i: int, joined: Callable[[int], bool]) -> _Parsed | None:
    """Parse 0..99 (cardinal or ordinal) starting at word `i`."""
    w = words[i]
    if w in _TENS:
        value = _TENS[w]
        if i + 1 < len(words) and joined(i + 1):
            nxt = words[i + 1]
            if nxt in _UNITS and _UNITS[nxt] > 0:
                return value + _UNITS[nxt], False, i + 2
            if nxt in _UNIT_ORDINALS:
                return value + _UNIT_ORDINALS[nxt], True, i + 2
        return value, False, i + 1
    for table, ordinal in (
        (_UNITS, False), (_TEENS, False),
        (_UNIT_ORDINALS, True), (_TEEN_ORDINALS, True), (_TENS_ORDINALS, True),
    ):
        if w in table:
            return table[w], ordinal, i + 1
    return None


def _parse(words: list[str], i: int, joined: Callable[[int], bool]) -> _Parsed | None:
    """Parse the longest number (0..999, last word may be an ordinal) at word `i`."""
    w = words[i]
    hundreds: int | None = None
    j = i
    if (w == _A or (w in _UNITS and _UNITS[w] > 0)) and i + 1 < len(words) and joined(
        i + 1
    ) and words[i + 1] in (_HUNDRED, _HUNDREDTH):
        hundreds, j = (1 if w == _A else _UNITS[w]), i + 1
    elif w in (_HUNDRED, _HUNDREDTH):
        hundreds = 1
    if hundreds is None:
        return None if w == _A else _below_hundred(words, i, joined)

    value = hundreds * 100
    if words[j] == _HUNDREDTH:
        return value, True, j + 1
    k = j + 1  # first word after "hundred"
    if k < len(words) and joined(k) and words[k] == _AND:
        k += 1
    if k < len(words) and joined(k):
        rest = _below_hundred(words, k, joined)
        if rest is not None and rest[0] > 0:
            return value + rest[0], rest[1], rest[2]
    return value, False, j + 1  # a dangling "and" is not part of the number


@lru_cache(maxsize=4096)
def nearest_number_word(
    token: str, budget: TypoBudget, prefer: NumberForm | None = None
) -> str | None:
    """The number word `token` unambiguously misspells, else None.

    Only for a token that is neither a number word nor a known word. Candidates keep
    the first letter and lie within their own typo budget; the closest one wins
    (Damerau-Levenshtein, then Indel), and a tie means "ambiguous" -> None. With
    `prefer`, when the closest candidates (by Damerau-Levenshtein) are the cardinal
    and the ordinal of ONE number ("eigth": eight / eighth), the preferred form wins.
    """
    if token in NUMBER_VOCAB or is_known_word(token):
        return None
    ranked: list[tuple[int, int, str]] = []
    for word in NUMBER_VOCAB:
        if word[0] != token[0]:
            continue
        dist = DamerauLevenshtein.distance(token, word)
        if dist <= budget(word):
            ranked.append((dist, Indel.distance(token, word), word))
    if not ranked:
        return None
    ranked.sort()
    if prefer is not None:
        top = [w for d, _, w in ranked if d == ranked[0][0]]
        if len(top) > 1 and len({_WORD_VALUE[w] for w in top}) == 1:
            want = [w for w in top if (w in _ORDINAL_WORDS) == (prefer == "ordinal")]
            if len(want) == 1:
                return want[0]
    if len(ranked) > 1 and ranked[0][:2] == ranked[1][:2]:
        return None
    return ranked[0][2]


def canonical_numbers(
    text: str, typo_budget: TypoBudget | None = None, prefer: NumberForm | None = None
) -> str:
    """Rewrite numbers written in words into digits (see module docstring).

    `prefer` (with `typo_budget`) is the key's form, see `nearest_number_word`."""
    pieces = _LETTERS.split(text)
    raw = pieces[1::2]
    if not raw:
        return text
    seps = pieces[0::2]  # seps[k] precedes raw[k]; seps[-1] trails the last word
    words = list(raw)
    if typo_budget is not None:
        words = [nearest_number_word(w, typo_budget, prefer) or w for w in raw]

    def joined(k: int) -> bool:
        """Word k continues the number of word k-1 (single space / hyphen between)."""
        return seps[k] in _JOINERS

    out = [seps[0]]
    k = 0
    while k < len(words):
        hit = _parse(words, k, joined)
        if hit is None:
            out += [raw[k], seps[k + 1]]
            k += 1
            continue
        value, ordinal, nxt = hit
        out += [_format(value, ordinal), seps[nxt]]
        k = nxt
    return "".join(out)
