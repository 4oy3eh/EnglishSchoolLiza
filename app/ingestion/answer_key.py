"""Answer-key parse + merge — the authoritative source of `correct` (golden rule #5).

The LLM produces a *draft* with no answer key. This module parses the separate
answer-key PDF text into `number -> KeyEntry` and merges it onto a `DraftTest` to
produce a validated, authoring `contracts.Test`. The key PDF — never the LLM — sets
`correct` (single_choice/matching) and `accepted` (+ acceptable-misspelling variants)
for gap_fill.

Both functions are **pure and deterministic** (same text/draft -> same result). A
keyed item with no matching key entry, or a `correct` that isn't among the item's
options, raises `ValueError` — i.e. a malformed extraction is rejected rather than
published with a wrong/empty key.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.ingestion.models import (
    DraftGapFill,
    DraftMatching,
    DraftOpenWriting,
    DraftSection,
    DraftSingleChoice,
    DraftTest,
)
from contracts import (
    GapFillItem,
    Item,
    MatchingItem,
    OpenWritingItem,
    Section,
    SingleChoiceItem,
    Test,
)

log = get_logger(__name__)

# "<number> <answer...>" — one keyed line. Tolerates a trailing ')' / '.' separator.
_LINE = re.compile(r"^\s*(\d{1,3})[.)]?\s+(.+?)\s*$")
# Acceptable-misspelling variants Cambridge prints in parentheses, e.g. "received (recieved)".
_PARENS = re.compile(r"\(([^)]*)\)")
# Word-answer alternates: "station / train station" or "happy OR glad".
_ALT = re.compile(r"\s*/\s*|\s+OR\s+")


@dataclass(frozen=True)
class KeyEntry:
    """The parsed key for one question number."""

    number: int
    answers: tuple[str, ...]  # 1 for choice/matching; 1+ acceptable for gap_fill
    variants: tuple[str, ...] = field(default_factory=tuple)


def parse_answer_key(text: str) -> dict[int, KeyEntry]:
    """Parse answer-key PDF text into `number -> KeyEntry` (pure)."""
    entries: dict[int, KeyEntry] = {}
    for raw in text.splitlines():
        match = _LINE.match(raw)
        if match is None:
            continue
        number = int(match.group(1))
        body = match.group(2).strip()

        variants: list[str] = []
        for parens in _PARENS.findall(body):
            variants.extend(v.strip() for v in _ALT.split(parens) if v.strip())
        body = _PARENS.sub("", body).strip()

        answers = tuple(a.strip() for a in _ALT.split(body) if a.strip())
        if not answers:
            continue
        entries[number] = KeyEntry(
            number=number, answers=answers, variants=tuple(variants)
        )
    log.info("answer-key parsed entries=%d", len(entries))
    return entries


def merge_key(draft: DraftTest, key: Mapping[int, KeyEntry]) -> Test:
    """Attach the parsed key to a draft, returning a validated authoring `Test`.

    The key is authoritative for `correct`/`accepted` (golden rule #5). Raises
    `ValueError` if a keyed item (single_choice / matching / gap_fill) has no key
    entry, or a `correct` option key is not among the item's options.
    """
    consumed: set[int] = set()
    sections = [_merge_section(s, key, consumed) for s in draft.sections]

    leftover = sorted(set(key) - consumed)
    if leftover:  # not fatal: e.g. extra rows the structurer didn't surface
        log.warning("answer-key has %d unmatched entries: %s", len(leftover), leftover)

    test = Test(
        id=draft.id,
        title=draft.title,
        level=draft.level,
        status="draft",  # NEVER auto-publish (golden rule #5)
        duration_minutes=draft.duration_minutes,
        sections=sections,
    )
    log.info(
        "answer-key merged test=%s sections=%d items=%d status=%s",
        test.id,
        len(test.sections),
        sum(len(s.items) for s in test.sections),
        test.status,
    )
    return test


def _merge_section(
    section: DraftSection, key: Mapping[int, KeyEntry], consumed: set[int]
) -> Section:
    # Matching items resolve their `correct` against the section's pool keys (A-H);
    # any other stimulus has no pool, so matching there is unresolvable -> None.
    stimulus = section.stimulus
    pool_keys = (
        frozenset(o.key for o in stimulus.options)
        if stimulus.kind == "matching_pool"
        else None
    )
    items = [_merge_item(item, key, consumed, pool_keys) for item in section.items]
    return Section(
        id=section.id,
        title=section.title,
        skill=section.skill,
        stimulus=section.stimulus,
        items=items,
    )


def _merge_item(
    item: DraftSingleChoice | DraftGapFill | DraftMatching | DraftOpenWriting,
    key: Mapping[int, KeyEntry],
    consumed: set[int],
    pool_keys: frozenset[str] | None,
) -> Item:
    item_id = f"q{item.number}"

    if isinstance(item, DraftOpenWriting):
        # Writing is graded by rubric (LLM/manual); it has no answer-key entry.
        if item.number in key:
            log.warning("answer-key entry for writing q%d ignored", item.number)
        return OpenWritingItem(
            id=item_id,
            prompt=item.prompt,
            word_min=item.word_min,
            bullet_points=item.bullet_points,
            rubric=item.rubric,
            grade_mode=item.grade_mode,
        )

    entry = key.get(item.number)
    if entry is None:
        raise ValueError(f"no answer key for question {item.number} ({item.item_type})")
    consumed.add(item.number)

    if isinstance(item, DraftSingleChoice):
        correct = entry.answers[0]
        option_keys = {o.key for o in item.options}
        if correct not in option_keys:
            raise ValueError(
                f"q{item.number}: key {correct!r} not in options {sorted(option_keys)}"
            )
        return SingleChoiceItem(
            id=item_id, prompt=item.prompt, options=item.options, correct=correct
        )

    if isinstance(item, DraftMatching):
        if len(entry.answers) != 1:
            raise ValueError(
                f"q{item.number}: matching expects a single key, got {entry.answers}"
            )
        correct = entry.answers[0]
        if pool_keys is not None and correct not in pool_keys:
            raise ValueError(
                f"q{item.number}: key {correct!r} not in matching pool "
                f"{sorted(pool_keys)}"
            )
        return MatchingItem(id=item_id, prompt=item.prompt, correct=correct)

    # DraftGapFill
    return GapFillItem(
        id=item_id,
        prompt=item.prompt,
        accepted=list(entry.answers),
        accepted_variants=list(entry.variants),
    )
