"""Answer normalization for `gap_fill` (and key word transformation) grading.

Cambridge gap-fill answers are a single word / number / date / time. We normalize
both the student response and the authored `accepted` / `accepted_variants` /
`rejected` strings the same way before comparing, so casing, stray whitespace and
typographic punctuation never decide correctness. We deliberately do NOT strip
internal punctuation (a date like ``21/06`` or a contraction like ``don't`` is
meaningful), only fold typographic apostrophes / quotes to ASCII, fold case and
collapse surrounding/repeated spaces.

Phones and word processors type "smart" punctuation: ``don’t`` (U+2019),
``‘`` (U+2018), ``ʼ`` (U+02BC modifier letter apostrophe), ``“ ”`` — `fold_typography`
maps them to ``'`` / ``"`` so ``don’t`` == ``don't``.
"""

from __future__ import annotations

# Every apostrophe look-alike -> "'" and every double-quote look-alike -> '"'.
_TYPOGRAPHY = str.maketrans(
    {
        "’": "'",  # right single quotation mark (the phone apostrophe)
        "‘": "'",  # left single quotation mark
        "ʼ": "'",  # modifier letter apostrophe
        "‛": "'",  # single high-reversed-9 quotation mark
        "′": "'",  # prime
        "´": "'",  # acute accent
        "`": "'",  # grave accent
        "＇": "'",  # fullwidth apostrophe
        "“": '"',  # left double quotation mark
        "”": '"',  # right double quotation mark
        "„": '"',  # double low-9 quotation mark
        "‟": '"',  # double high-reversed-9 quotation mark
        "«": '"',  # « left-pointing double angle quotation mark
        "»": '"',  # » right-pointing double angle quotation mark
        "″": '"',  # double prime
        "＂": '"',  # fullwidth quotation mark
    }
)


def fold_typography(text: str) -> str:
    """Typographic apostrophes / quotes -> ASCII ``'`` / ``"`` (nothing else changes)."""
    return text.translate(_TYPOGRAPHY)


def normalize(text: str) -> str:
    """Fold typographic quotes, casefold and collapse whitespace; leave internal
    punctuation intact."""
    return " ".join(fold_typography(text).split()).casefold()
