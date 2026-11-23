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

from urllib.parse import quote

from app.content import AttemptLayout, OptionShuffle
from contracts import (
    ClientColourTaskItem,
    ClientDrawingItem,
    ClientGapFillItem,
    ClientImageOption,
    ClientItem,
    ClientKeyWordTransformationItem,
    ClientMarkToggleItem,
    ClientMatchingItem,
    ClientOpenWritingItem,
    ClientOption,
    ClientSection,
    ClientSingleChoiceItem,
    ClientStudyInfo,
    ClientStudyStimulus,
    ClientTest,
    ClientTextOption,
    ColourTaskItem,
    DrawingItem,
    DrawingLinesTarget,
    GapFillItem,
    ImageOption,
    Item,
    KeyWordTransformationItem,
    MarkToggleItem,
    MatchingItem,
    OpenWritingItem,
    Section,
    SingleChoiceItem,
    Test,
    TextOption,
)
from contracts.content import kwt_word_limits


def project_option(option: TextOption | ImageOption) -> ClientOption:
    """Drop the canonical `key`; keep only what the student needs to render."""
    if isinstance(option, TextOption):
        return ClientTextOption(text=option.text)
    return ClientImageOption(asset_id=option.asset_id, alt=option.alt)


def project_item(
    item: Item, shuffle: OptionShuffle | None, *, level: str | None = None
) -> ClientItem:
    """Project one authoring item to its client shape, stripped of the answer key.

    For `single_choice`, options are re-emitted in the per-attempt *displayed*
    order given by `shuffle`; everything answer-key (`correct`, `accepted`,
    `accepted_variants`, `rubric`, `grade_mode`, KWT `answers`) is dropped by
    construction. `level` (the test's) resolves a key word transformation's word
    limits (`kwt_word_limits`) so the runner can show them.
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
        return ClientGapFillItem(
            id=item.id,
            prompt=item.prompt,
            image=item.image,
            stem_word=item.stem_word,
            max_words=item.max_words,
            numeric=item.numeric,
        )
    if isinstance(item, KeyWordTransformationItem):
        lo, hi = kwt_word_limits(item, level)
        return ClientKeyWordTransformationItem(
            id=item.id,
            prompt=item.prompt,
            key_word=item.key_word,
            gapped_sentence=item.gapped_sentence,
            min_words=lo,
            max_words=hi,
        )
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
    if isinstance(item, MarkToggleItem):
        # Drop `correct` (should this row be marked).
        return ClientMarkToggleItem(id=item.id, prompt=item.prompt, image=item.image)
    if isinstance(item, DrawingItem):
        # Drop the `target` (zones / reference segments / tolerances); a lines item
        # keeps only where the square sits on the picture, to frame the canvas.
        square = item.target.square if isinstance(item.target, DrawingLinesTarget) else None
        return ClientDrawingItem(
            id=item.id,
            prompt=item.prompt,
            asset_id=item.asset_id,
            mode=item.mode,
            square=square.model_copy() if square is not None else None,
        )
    raise TypeError(f"unknown item type: {item!r}")  # pragma: no cover


def project_section(
    section: Section, layout: AttemptLayout, *, level: str | None = None
) -> ClientSection:
    """Project a section, applying option shuffles to its single_choice items.

    `reported_skill` (teacher-side reporting) is not served; the stimulus is served
    verbatim (it never holds a key — incl. a matching pool's `each_option_once` /
    `audio`). IQ extras: `time_limit_seconds`, `item_count` (also kept when the
    items are withheld) and the untimed `intro` (incl. its public practice
    problems) are served;
    a `study` stimulus is NOT — only `ClientStudyInfo` (that it exists + its
    length). Its content goes out via `project_study`, which delivery may call only
    inside the study window (app/delivery/CLAUDE.md).
    """
    return ClientSection(
        id=section.id,
        title=section.title,
        skill=section.skill,
        stimulus=section.stimulus,
        item_count=len(section.items),
        time_limit_seconds=section.time_limit_seconds,
        intro=section.intro,
        study=(
            ClientStudyInfo(study_seconds=section.study.study_seconds)
            if section.study is not None
            else None
        ),
        items=[
            project_item(item, layout.option_shuffles.get(item.id), level=level)
            for item in section.items
        ],
    )


def study_asset_url(attempt_id: str, section_id: str) -> str:
    """The attempt-authenticated, window-gated route of a section's study picture
    (`apps/api/delivery.py`). The raw asset id is never sent to the student."""
    return (
        f"/exam/attempts/{quote(attempt_id, safe='')}"
        f"/sections/{quote(section_id, safe='')}/study/asset"
    )


def study_cell_url(attempt_id: str, section_id: str, index: int) -> str:
    """The window-gated route of study cell `index` (`Section.study.images`)."""
    return f"{study_asset_url(attempt_id, section_id)}/{index}"


def project_study(section: Section, attempt_id: str) -> ClientStudyStimulus | None:
    """The study content of a section (None when it has no study phase).

    NOT part of `project_test`: delivery serves it from its own endpoint only while
    the section's study window is open, never together with the recall items. The
    picture goes out as `image_url` (`study_asset_url`), never as its asset id."""
    study = section.study
    if study is None:
        return None
    return ClientStudyStimulus(
        section_id=section.id,
        image_url=study_asset_url(attempt_id, section.id) if study.asset_id else None,
        image_urls=[study_cell_url(attempt_id, section.id, i) for i in range(len(study.images))],
        text=study.text,
        study_seconds=study.study_seconds,
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
        sections=[project_section(section, layout, level=test.level) for section in sections],
        ui_language=test.ui_language,
    )
