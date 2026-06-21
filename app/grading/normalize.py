"""Answer normalization for `gap_fill` grading.

Cambridge gap-fill answers are a single word / number / date / time. We normalize
both the student response and the authored `accepted` strings the same way before
comparing, so casing and stray whitespace never decide correctness. We deliberately
do NOT strip internal punctuation (a date like ``21/06`` or a contraction like
``don't`` is meaningful), only fold case and collapse surrounding/repeated spaces.
"""

from __future__ import annotations


def normalize(text: str) -> str:
    """Casefold and collapse whitespace; leave internal punctuation intact."""
    return " ".join(text.split()).casefold()
