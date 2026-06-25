"""Authoring -> student-facing projection (golden rule #1).

Pure functions that turn the internal authoring `Test`/`Section`/`Item` (which
carry the answer key) into the `Client*` family that is *structurally* incapable
of holding a `correct` field. This is the layer that enforces "correct answers
never reach the client": delivery serves only the output of these functions.

The per-attempt `AttemptLayout` (from `app/content` pooling) decides:

* which sections appear, and in which order (`layout.section_ids`); and
* the displayed order of `single_choice` options (`OptionShuffle`) — options are
  re-emitted in displayed order and stripped of their canonical `key`, so the
  student answers by displayed index and delivery maps it back (see `service`).

Item order *within* a section is never touched (golden rule #7).
"""

from __future__ import annotations

from app.content import AttemptLayout, OptionShuffle
from contracts import (
    ClientColourTaskItem,
    ClientGapFillItem,
    ClientImageOption,
    ClientItem,
    ClientMatchingItem,
    ClientOpenWritingItem,
    ClientOption,
    ClientSection,
    ClientSingleChoiceItem,
    ClientTest,
    ClientTextOption,
    ColourTaskItem,
    GapFillItem,
    ImageOption,
    Item,
    MatchingItem,
    OpenWritingItem,
    Section,
    SingleChoiceItem,
    Test,
    TextOption,
)


def project_option(option: TextOption | ImageOption) -> ClientOption:
    """Drop the canonical `key`; keep only what the student needs to render."""
    if isinstance(option, TextOption):
        return ClientTextOption(text=option.text)
    return ClientImageOption(asset_id=option.asset_id, alt=option.alt)


def project_item(item: Item, shuffle: OptionShuffle | None) -> ClientItem:
    """Project one authoring item to its client shape, stripped of the answer key.

    For `single_choice`, options are re-emitted in the per-attempt *displayed*
    order given by `shuffle`; everything answer-key (`correct`, `accepted`,
    `accepted_variants`, `rubric`, `grade_mode`) is dropped by construction.
    """
    if isinstance(item, SingleChoiceItem):
        by_key = {opt.key: opt for opt in item.options}
        order = shuffle.order if shuffle is not None else tuple(by_key)
        return ClientSingleChoiceItem(
            id=item.id,
            prompt=item.prompt,
            image=item.image,
            options=[project_option(by_key[key]) for key in order],
        )
    if isinstance(item, GapFillItem):
        return ClientGapFillItem(id=item.id, prompt=item.prompt)
    if isinstance(item, MatchingItem):
        return ClientMatchingItem(id=item.id, prompt=item.prompt)
    if isinstance(item, OpenWritingItem):
        return ClientOpenWritingItem(
            id=item.id,
            prompt=item.prompt,
            word_min=item.word_min,
            bullet_points=item.bullet_points,
        )
    if isinstance(item, ColourTaskItem):
        # Drop the authoring `key` (the colouring solution); keep what the student
        # needs to render the canvas.
        return ClientColourTaskItem(
            id=item.id,
            prompt=item.prompt,
            asset_id=item.asset_id,
            palette=item.palette,
        )
    raise TypeError(f"unknown item type: {item!r}")  # pragma: no cover


def project_section(section: Section, layout: AttemptLayout) -> ClientSection:
    """Project a section, applying option shuffles to its single_choice items."""
    return ClientSection(
        id=section.id,
        title=section.title,
        skill=section.skill,
        stimulus=section.stimulus,
        items=[
            project_item(item, layout.option_shuffles.get(item.id))
            for item in section.items
        ],
    )


def project_test(test: Test, layout: AttemptLayout) -> ClientTest:
    """Project the drawn subset of sections, in the layout's order.

    Only the sections in `layout.section_ids` are served, in that exact order, so
    the student sees their own reproducible draw.
    """
    by_id = {section.id: section for section in test.sections}
    sections = [by_id[sid] for sid in layout.section_ids]
    return ClientTest(
        id=test.id,
        title=test.title,
        level=test.level,
        duration_minutes=test.duration_minutes,
        sections=[project_section(section, layout) for section in sections],
    )
