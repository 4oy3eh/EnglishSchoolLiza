"""Known-word vocabulary + word-form rules for gap_fill typo tolerance.

A misspelling is only a misspelling when it is NOT itself a word: "rain" for
"train", "boats" for "boots", "stairs" for "stars" are different words the student
chose, so the typo rule must never accept them. This module answers two questions,
both deterministically (pure functions over bundled static data — no network, no
clock):

* `is_known_word(token)` — is the token a real English word (or a word of the item
  bank)? Backed by two bundled plain-text lists under `app/grading/data/`:

  - `common_words.txt` — a hand-authored list of everyday A1–B2 English words
    (function words, numbers, topic vocabulary, common verbs + irregular forms,
    adjectives, adverbs, confusable pairs). Written for this project from general
    knowledge; NOT copied from any licensed word list — so it is license-clean.
  - `bank_words.txt` — every word occurring in the bundled tests'
    (`deploy/fixtures/*.json`) texts, prompts, options and keys, EXCEPT the
    `accepted_variants` (those are deliberate misspellings). Regenerate with
    `python -m app.grading.vocabulary` after changing the fixtures;
    `tests/test_grading_rules.py` fails when it is stale.

  Each listed word is expanded with its regular forms (`word_forms`: -s/-es,
  -ed, -ing, -er/-est, -ly, -ful, -y, agentive -r), so "boats", "rained",
  "cheaper" are known without being listed one by one.

* `is_inflection_of(a, b)` — are two different tokens forms of ONE word (a
  grammar / word-form error, never a misspelling)? True when some base X
  generates both of them through `word_forms` (e.g. X="produce" -> "producer"
  and "produced"; X="cheap" -> "cheaper" / "cheapest"; X="care" -> "careful" /
  "carefully"), when both are forms of one irregular verb ("throw"/"threw"),
  when they are -man / -men ("woman"/"women"), or when both are number words
  ("eleven"/"eleventh", "thirty"/"thirteen"). Derivational suffixes (-y, -ly,
  -ful, -fully, -th, -r, -st) only count when the base X is a known word, so
  dropping a final letter of an unknown base ("stor" for "story") stays a typo
  question.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)

DATA_DIR = Path(__file__).resolve().parent / "data"
COMMON_WORDS_FILE = DATA_DIR / "common_words.txt"
BANK_WORDS_FILE = DATA_DIR / "bank_words.txt"
FIXTURES_DIR = Path(__file__).resolve().parents[2] / "deploy" / "fixtures"

_VOWELS = frozenset("aeiou")
_WORD = re.compile(r"[a-z]+(?:'[a-z]+)?")
_ALPHA = re.compile(r"[a-z]+")

# Number words. Two different number words are never a typo of each other
# ("eleven"/"eleventh", "thirty"/"thirteen", "fifteen"/"fifty").
_CARDINALS = """
zero one two three four five six seven eight nine ten eleven twelve thirteen
fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty
sixty seventy eighty ninety hundred thousand million billion
""".split()
_ORDINALS = """
first second third fourth fifth sixth seventh eighth ninth tenth eleventh
twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth
nineteenth twentieth thirtieth fortieth fiftieth sixtieth seventieth eightieth
ninetieth hundredth thousandth millionth billionth
""".split()
NUMBER_WORDS = frozenset(_CARDINALS + _ORDINALS)

# Common irregular verbs (base / past / past participle). Two different forms of
# one verb ("throw"/"threw", "sit"/"sat", "run"/"ran") are a tense error.
_IRREGULAR_VERBS = """
be was were been; begin began begun; break broke broken; bring brought;
build built; buy bought; catch caught; choose chose chosen; come came;
do did done; draw drew drawn; drink drank drunk; drive drove driven;
eat ate eaten; fall fell fallen; feel felt; find found; fly flew flown;
forget forgot forgotten; get got gotten; give gave given; go went gone;
grow grew grown; have had; hear heard; hide hid hidden; hold held; keep kept;
know knew known; leave left; lend lent; lose lost; make made; meet met; pay paid;
ride rode ridden; ring rang rung; rise rose risen; run ran; say said; see saw seen;
sell sold; send sent; shake shook shaken; shine shone; shoot shot; sing sang sung;
sink sank sunk; sit sat; sleep slept; speak spoke spoken; spend spent; stand stood;
steal stole stolen; swim swam swum; take took taken; teach taught; tell told;
think thought; throw threw thrown; understand understood; wake woke woken;
wear wore worn; win won; write wrote written
"""
_VERB_FAMILY: dict[str, int] = {
    form: idx for idx, group in enumerate(_IRREGULAR_VERBS.split(";")) for form in group.split()
}

# Suffixes whose base must be a KNOWN word for the pair to count as one word's
# forms (see the module docstring); the rest (-s, -es, -ed, -d, -ing, -er, -est)
# are regular inflections and count for any base.
# Plurals in -oes (the other -o nouns take -s: photos, radios, pianos).
_O_ES = frozenset("go do potato tomato hero echo veto mosquito torpedo volcano".split())
# Multi-syllable verbs stressed on the last syllable double the final consonant
# (begin -> beginning); the generic rule only doubles one-syllable words.
_DOUBLE_FINAL = frozenset(
    """
    begin forget prefer refer occur admit permit regret commit control equip upset
    forbid submit patrol compel propel transfer omit
    """.split()
)
_DERIVATIONAL = ("fully", "ful", "ly", "y", "th", "r", "st")
_INFLECTIONAL = ("ing", "est", "es", "ed", "er", "s", "d")


# --------------------------------------------------------------------------- #
# Regular word forms (pure).
# --------------------------------------------------------------------------- #
def _cvc(word: str) -> bool:
    """One-syllable consonant-vowel-consonant word that doubles its final
    consonant before a vowel suffix: stop -> stopped, run -> running, big -> bigger."""
    if len(word) < 3 or word[-1] in _VOWELS or word[-1] in "wxy":
        return False
    if word[-2] not in _VOWELS or word[-3] in _VOWELS:
        return False
    vowel_groups = re.findall(r"[aeiouy]+", word[:-1])
    return len(vowel_groups) == 1


def _cons_y(word: str) -> bool:
    return len(word) >= 2 and word.endswith("y") and word[-2] not in _VOWELS


def _vowel_suffix(word: str, suffix: str) -> set[str]:
    """`word` + a suffix starting with a vowel (-ed / -ing / -er / -est)."""
    out: set[str] = set()
    if suffix == "ing":
        if word.endswith("ie"):
            out.add(word[:-2] + "ying")
        elif word.endswith("e") and not word.endswith(("ee", "ye", "oe")):
            out.add(word[:-1] + "ing")
        else:
            out.add(word + "ing")
    elif word.endswith("e"):
        out.add(word + suffix[1:])  # like -> liked, nice -> nicer / nicest
    elif _cons_y(word):
        out.add(word[:-1] + "i" + suffix)  # cry -> cried, happy -> happier
    else:
        out.add(word + suffix)
    if _cvc(word) or word in _DOUBLE_FINAL:
        # stop -> stopped, run -> running, begin -> beginning — and ONLY that:
        # "stoped" / "runing" / "begining" are misspellings, not forms.
        out.discard(word + suffix)
        out.add(word + word[-1] + suffix)
    if len(word) > 3 and word.endswith("l") and word[-2] in _VOWELS:
        out.add(word + "l" + suffix)  # travel -> travelled (UK) and traveled (US)
    return out


def _plural(word: str) -> set[str]:
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return {word + "es"}
    if _cons_y(word):
        return {word[:-1] + "ies"}
    if word in _O_ES:
        return {word + "es"}  # potatoes (never "potatos")
    if word.endswith("o"):
        return {word + "s"}  # photos, radios, zoos
    if word.endswith("f"):
        return {word + "s", word[:-1] + "ves"}  # roofs / leaves
    if word.endswith("fe"):
        return {word + "s", word[:-2] + "ves"}  # knives
    return {word + "s"}


def _derived(word: str) -> set[str]:
    """Derivational forms: -ly, -ful / -fully, -y, agentive / comparative -r."""
    out: set[str] = set()
    stem = word[:-1] + "i" if _cons_y(word) else word
    # -ly: happy -> happily, gentle -> gently, careful -> carefully, quick -> quickly
    if word.endswith("le") and len(word) > 3 and word[-3] not in _VOWELS:
        out.add(word[:-1] + "y")
    elif word.endswith("ic"):
        out.add(word + "ally")
    elif not word.endswith("ue"):  # true -> truly (never "truely")
        out.add(stem + "ly")
    # -ful / -fully: care -> careful(ly), beauty -> beautiful(ly)
    if not word.endswith("ful"):
        out.add(stem + "ful")
        out.add(stem + "fully")
    # -y: cloud -> cloudy, noise -> noisy, sun -> sunny
    if not word.endswith("y"):
        if word.endswith("e") and not word.endswith("ee"):
            out.add(word[:-1] + "y")
        else:
            out.add(word + "y")
        if _cvc(word):
            out.add(word + word[-1] + "y")
    return out


def _ordinal(word: str) -> set[str]:
    """Cardinal -> ordinal (+th / -ieth); only for number words."""
    if word not in NUMBER_WORDS:
        return set()
    form = word[:-1] + "ieth" if word.endswith("y") else word + "th"
    # Only a REAL ordinal ("eleventh", "thirtieth"); never "nineth" / "fiveth",
    # which are misspellings of "ninth" / "fifth".
    return {form} if form in NUMBER_WORDS else set()


def word_forms(word: str) -> set[str]:
    """The regular forms one base word generates (the word itself excluded).

    -s/-es, -ed, -ing, -er, -est (spelling rules: e-drop, y->i, CVC doubling),
    plus -ly, -ful, -fully, -y and the ordinal of a number word. Over-generation
    on purpose ("rainer"): a non-word in the set only ever makes the typo rule
    stricter.
    """
    word = word.casefold()
    if not _ALPHA.fullmatch(word) or len(word) < 2:
        return set()
    out = _inflections(word)
    out |= _derived(word)
    out |= _ordinal(word)
    out.discard(word)
    return out


def _bases(word: str) -> set[str]:
    """Candidate bases a suffixed word may come from (over-generated, cheap)."""
    out: set[str] = set()
    for suffix in (*_INFLECTIONAL, *_DERIVATIONAL, "ies", "ied", "ier", "iest", "ves"):
        if not word.endswith(suffix) or len(word) - len(suffix) < 2:
            continue
        stem = word[: -len(suffix)]
        out.add(stem)
        out.add(stem + "e")  # liked -> like, nicer -> nice
        if suffix.startswith("i") or stem.endswith("i"):
            out.add(stem.rstrip("i") + "y")  # cried -> cry, beautiful -> beauty
        if suffix == "ves":
            out |= {stem + "f", stem + "fe"}
        if len(stem) >= 3 and stem[-1] == stem[-2] and stem[-1] not in _VOWELS:
            out.add(stem[:-1])  # stopped -> stop, bigger -> big
        if stem.endswith("ful"):
            out.add(stem)  # carefully -> careful
    for suffix in ("ally", "ieth"):
        if word.endswith(suffix):
            out.add(word[: -len(suffix)] + ("" if suffix == "ally" else "y"))
    out.discard(word)
    return out


def _inflections(base: str) -> set[str]:
    """The regular INFLECTIONAL forms of `base` (-s/-es, -ed, -ing, -er, -est)."""
    out = set(_plural(base))
    for suffix in ("ed", "ing", "er", "est"):
        out |= _vowel_suffix(base, suffix)
    return out


def _derivational_only(base: str, form: str) -> bool:
    """True when `form` is generated from `base` only by a derivational suffix."""
    return form not in _inflections(base)


def _same_word(a: str, b: str) -> bool:
    """Some base X generates both `a` and `b` (each is X or one of X's forms)."""
    for base in {a, b} | _bases(a) | _bases(b):
        family = word_forms(base) | {base}
        if a not in family or b not in family:
            continue
        # A derivational step from an unknown base proves nothing ("stor"/"story").
        derivational = any(w != base and _derivational_only(base, w) for w in (a, b))
        if derivational and base not in KNOWN_WORDS:
            continue
        return True
    return False


def _man_men(a: str, b: str) -> bool:
    return (a.endswith("man") and b == a[:-3] + "men") or (
        b.endswith("man") and a == b[:-3] + "men"
    )


def is_inflection_of(a: str, b: str) -> bool:
    """True when two DIFFERENT tokens are forms of one word (see module docstring)."""
    if a == b:
        return False
    fa, fb = _VERB_FAMILY.get(a), _VERB_FAMILY.get(b)
    if fa is not None and fa == fb:  # "throw"/"threw", "sit"/"sat"
        return True
    if a in NUMBER_WORDS and b in NUMBER_WORDS:  # "eleven"/"eleventh"
        return True
    if _man_men(a, b):  # "woman"/"women", "policeman"/"policemen"
        return True
    return _same_word(a, b)


# --------------------------------------------------------------------------- #
# The bundled word lists.
# --------------------------------------------------------------------------- #
def _read_list(path: Path) -> set[str]:
    words: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            words.add(line.casefold())
    return words


def build_known_words(base_words: Iterable[str]) -> frozenset[str]:
    """Listed words + their regular forms + numbers + irregular verb forms."""
    out: set[str] = set(NUMBER_WORDS) | set(_VERB_FAMILY)
    for word in base_words:
        out.add(word)
        out |= word_forms(word)
    return frozenset(out)


def _load() -> tuple[frozenset[str], int, int]:
    common = _read_list(COMMON_WORDS_FILE)
    bank = _read_list(BANK_WORDS_FILE) if BANK_WORDS_FILE.exists() else set()
    return build_known_words(common | bank), len(common), len(bank)


KNOWN_WORDS, COMMON_COUNT, BANK_COUNT = _load()
log.debug(
    "vocabulary loaded common=%d bank=%d known(with forms)=%d",
    COMMON_COUNT,
    BANK_COUNT,
    len(KNOWN_WORDS),
)


def is_known_word(token: str) -> bool:
    """A real word (listed, a regular form of one, a number, an irregular verb)."""
    return token.casefold() in KNOWN_WORDS


# --------------------------------------------------------------------------- #
# Bank vocabulary (regeneration of bank_words.txt).
# --------------------------------------------------------------------------- #
# JSON keys whose values are identifiers / enums / deliberate misspellings, never
# vocabulary.
_SKIP_KEYS = frozenset(
    {
        "id",
        "asset_id",
        "accepted_variants",
        "correct",
        "key",
        "kind",
        "item_type",
        "skill",
        "level",
        "status",
        "grade_mode",
        "spelling",
        "src",
        "palette",
    }
)


def _strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)
    elif isinstance(node, dict):
        for k, value in node.items():
            if k not in _SKIP_KEYS and not k.endswith("_id"):
                yield from _strings(value)


def bank_words(documents: Iterable[Any]) -> set[str]:
    """Every word in the given test JSON documents (see `_SKIP_KEYS`)."""
    words: set[str] = set()
    for doc in documents:
        for text in _strings(doc):
            for token in _WORD.findall(text.casefold()):
                if len(token) >= 2:
                    words.add(token)
    return words


def fixture_bank_words(fixtures_dir: Path = FIXTURES_DIR) -> set[str]:
    docs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(fixtures_dir.glob("*.json"))]
    return bank_words(docs)


def write_bank_words(path: Path = BANK_WORDS_FILE, fixtures_dir: Path = FIXTURES_DIR) -> int:
    words = sorted(fixture_bank_words(fixtures_dir))
    header = (
        "# Generated by `python -m app.grading.vocabulary` from deploy/fixtures/*.json\n"
        "# (texts, prompts, options, keys; accepted_variants excluded). Do not edit.\n"
    )
    path.write_text(header + "\n".join(words) + "\n", encoding="utf-8")
    log.info("vocabulary wrote %s (%d words)", path, len(words))
    return len(words)


if __name__ == "__main__":  # pragma: no cover - dev tool
    from app.core.logging import configure_logging

    configure_logging("INFO")
    write_bank_words()
    sys.exit(0)
