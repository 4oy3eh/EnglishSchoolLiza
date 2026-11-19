"""Key word transformation grading (B2-C2 Use of English Part 4) + word counting.

Pure functions, no clock / randomness / I/O (golden rule #2: no integrity data).

Cambridge rules implemented here (B2 First / C1 Advanced / C2 Proficiency
handbooks, "Reading and Use of English Part 4"):

* the gap takes `min_words`..`max_words` words INCLUDING the key word (B2 2-5, C1
  3-6, C2 3-8, `contracts.content.kwt_word_limits`); outside the limit -> 0;
* "they must count contracted words (with the exception of can't = cannot) as the
  full form (e.g. didn't = two words did not)" (B2); C1 / C2: "Contractions count as
  two words" — so `can't` is ONE word at B2 only (`count_words`);
* the key word must be used and "must not be changed in any way" -> else 0;
* "The mark scheme splits the answer into two parts and candidates gain one mark for
  each part which is correct": every `KwtAnswer` is a part A | part B pattern; the
  response is tried at every word split point against every pattern and the best
  number of correct halves counts (0, 1 or 2 of 2 -> scaled to `points`);
* punctuation and case are ignored, spelling is strict (no typo tolerance), and a
  response giving two answers ("a / b") scores 0.

Contractions are compared by their full form where it is unambiguous (`didn't` ==
`did not`, `can't` == `cannot` == `can not`, `'ll` / `'re` / `'ve` / `'m`); `'d`
(had / would) and `'s` (is / has / possessive) are compared literally.
"""

from __future__ import annotations

import re

from app.core.logging import get_logger
from app.grading.normalize import fold_typography
from contracts import ItemGrade, KeyWordTransformationItem
from contracts.content import kwt_word_limits

log = get_logger(__name__)

# Default marks of a KWT item (Cambridge: 2 per question, 1 per half).
KWT_DEFAULT_POINTS = 2.0

# Two answers in one gap ("in/on", "a good idea \\ a nice idea"): Cambridge gives no
# mark. Only the separators a student uses to offer alternatives.
TWO_ANSWERS_RE = re.compile(r"[/\\|]")

# Anything that is not a letter, digit, apostrophe, hyphen or space is punctuation
# ("The absence or misuse of punctuation is ignored").
_PUNCT_RE = re.compile(r"[^\w\s'-]|_")
# Pronouns / function words whose `'s` is a contraction of is / has (two words);
# after any other word `'s` is a possessive (one word).
_S_CONTRACTION_BASES = frozenset(
    {
        "it", "he", "she", "that", "there", "what", "who", "here", "where", "how",
        "this", "let", "when", "why", "everyone", "everybody", "everything",
        "someone", "somebody", "something", "nobody", "nothing", "one",
    }
)  # fmt: skip
_TWO_WORD_SUFFIXES = ("'ll", "'re", "'ve", "'d", "'m")
_EXPANSIONS = {"'ll": "will", "'re": "are", "'ve": "have", "'m": "am"}
_NT_BASES = {"can't": "can", "won't": "will", "shan't": "shall", "ain't": "am"}


def kwt_tokens(text: str) -> list[str]:
    """Casefolded words of `text`, punctuation dropped (apostrophes / hyphens inside
    a word kept: "didn't", "well-known")."""
    folded = fold_typography(text).casefold()  # don’t / donʼt == don't
    words = _PUNCT_RE.sub(" ", folded).split()
    out = []
    for w in words:
        w = w.strip("'-")
        if w:
            out.append(w)
    return out


def _is_cant(token: str) -> bool:
    return token in ("can't", "cannot")


def token_words(token: str, level: str | None) -> int:
    """How many words one token counts for (Cambridge counting).

    `didn't` / `I'll` / `we're` / `they've` / `I'd` / `I'm` / `it's` = 2; `can't` = 1
    at B2 only ("with the exception of can't = cannot"), 2 at C1 / C2; `cannot` = 1;
    a possessive `Tom's` = 1.
    """
    if token == "cannot":
        return 1
    if token.endswith("n't"):
        if token == "can't" and level == "B2_FIRST":
            return 1
        return 2
    if token.endswith(_TWO_WORD_SUFFIXES):
        return 2
    if token.endswith("'s") and token[:-2] in _S_CONTRACTION_BASES:
        return 2
    return 1


def count_words(tokens: list[str], level: str | None) -> int:
    """Word count of an answer, contractions counted as their full form."""
    return sum(token_words(t, level) for t in tokens)


def answer_word_count(text: str, level: str | None) -> int:
    """Words of a gap_fill answer: split on WHITESPACE only (so "7.30", "2,000",
    "3.5" are one word each), punctuation around each word ignored, contractions
    counted as their full form (`token_words`)."""
    out = 0
    for raw in fold_typography(text).casefold().split():
        word = raw.strip(".,;:!?\"()[]{}'-")
        if word:
            out += token_words(word, level)
    return out


def _expand(token: str) -> list[str]:
    """The full form of an unambiguous contraction (for comparison / the key word)."""
    if token == "cannot":
        return ["can", "not"]
    if token.endswith("n't"):
        base = _NT_BASES.get(token, token[:-3])
        return [base, "not"] if base else ["not"]
    for suffix, full in _EXPANSIONS.items():
        if token.endswith(suffix) and len(token) > len(suffix):
            return [token[: -len(suffix)], full]
    return [token]


def comparable(tokens: list[str]) -> tuple[str, ...]:
    """Tokens with unambiguous contractions expanded: "didn't go" == "did not go"."""
    out: list[str] = []
    for t in tokens:
        out.extend(_expand(t))
    return tuple(out)


def has_key_word(tokens: list[str], key_word: str) -> bool:
    """The key word is used UNCHANGED: as its own word(s) in the answer, or inside an
    unambiguous contraction of it ("didn't" for NOT, "can't" for CAN)."""
    kw = kwt_tokens(key_word)
    if not kw:
        return False
    for seq in (tokens, list(comparable(tokens))):
        n = len(kw)
        if any(seq[i : i + n] == kw for i in range(len(seq) - n + 1)):
            return True
    return False


def _halves(tokens: list[str], item: KeyWordTransformationItem) -> int:
    """The best number of correct halves (0-2) over every pattern and split point."""
    best = 0
    words = comparable(tokens)
    for answer in item.answers:
        part_a = {comparable(kwt_tokens(a)) for a in answer.part_a}
        part_b = {comparable(kwt_tokens(b)) for b in answer.part_b}
        for k in range(len(words) + 1):
            marks = (words[:k] in part_a) + (words[k:] in part_b)
            if marks > best:
                best = marks
                if best == 2:
                    return 2
    return best


def kwt_points(item: KeyWordTransformationItem) -> float:
    """What one KWT item is worth: its `points`, else 2."""
    return float(item.points) if item.points is not None else KWT_DEFAULT_POINTS


def grade_key_word_transformation(
    item: KeyWordTransformationItem, response: str | None, *, level: str | None = None
) -> ItemGrade:
    """Mark one transformation: 1 mark per correct half, 0 on a hard-rule breach.

    Hard rules (-> 0, checked first): blank; two answers given; word count (key word
    included, contractions = full form) outside the level's limits; key word
    missing or changed. Then the halves (`_halves`): awarded = points * halves / 2.
    The `feedback` carries the per-half verdict in Russian for the teacher.
    """
    points = kwt_points(item)

    def grade(halves: int, note: str) -> ItemGrade:
        awarded = round(points * halves / 2, 2)
        log.debug(
            "grade kwt item=%s -> %d/2 halves (%.2f/%.2f) %s",
            item.id, halves, awarded, points, note,
        )  # fmt: skip
        return ItemGrade(
            item_id=item.id,
            awarded=awarded,
            max_points=points,
            method="key_word_transformation",
            feedback=f"{halves}/2: {note}",
        )

    if response is None or not response.strip():
        return grade(0, "нет ответа")
    if TWO_ANSWERS_RE.search(response):
        return grade(0, "дано несколько ответов — не засчитывается")
    tokens = kwt_tokens(response)
    lo, hi = kwt_word_limits(item, level)
    n = count_words(tokens, level)
    if not lo <= n <= hi:
        return grade(0, f"слов {n}, допустимо {lo}–{hi} (с ключевым словом)")
    if not has_key_word(tokens, item.key_word):
        return grade(0, f"ключевое слово {item.key_word.upper()} не использовано или изменено")
    halves = _halves(tokens, item)
    note = {
        2: "обе части верны",
        1: "одна часть верна, другая нет",
        0: "обе части неверны",
    }[halves]
    return grade(halves, note)
