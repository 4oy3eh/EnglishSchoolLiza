"""Item-bank contracts: Test -> Section (shared stimulus) -> Item.

This is the SOURCE OF TRUTH for the content shape (CLAUDE.md golden rule #4).

Two parallel families live here:

* **Authoring models** (`Test`, `Section`, `Item`, `Option`) — the internal,
  human-/ingestion-authored shape. These carry the answer key (`correct`,
  `accepted`, `accepted_variants`, `rubric`).
* **Client models** (`ClientTest`, `ClientSection`, `ClientItem`, ...) — the
  student-facing shape served by the delivery engine. These are structurally
  incapable of carrying an answer key: there is NO `correct` field anywhere in
  this family (golden rule #1). Options also drop their canonical `key`; the
  student answers by displayed index and delivery maps it back to canonical.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

# Cambridge levels, easiest first, then the non-Cambridge `IQ` family (Phase 17: a
# norm-referenced reasoning test — no Cambridge scale, English UI, one paper "iq").
# The three Young Learners (YLE) levels are PRE_A1_STARTERS / A1_MOVERS / A2_FLYERS
# (the runner offers a uk/ru UI for them);
# their "Reading & Writing" paper is authored as `skill="reading"` sections, like
# Movers (docs/INGEST_VIA_CLAUDE_CODE.md). B2_FIRST covers B2 First AND B2 First for
# Schools (same format, marking and scales; the test title tells them apart).
Level = Literal[
    "PRE_A1_STARTERS",
    "A1_MOVERS",
    "A2_FLYERS",
    "A2_KEY",
    "B1_PRELIMINARY",
    "B2_FIRST",
    "C1_ADVANCED",
    "C2_PROFICIENCY",
    "IQ",
]
PublishStatus = Literal["draft", "published"]
# The PAPER a section belongs to (the runner groups consecutive sections of one
# skill into a block with continuous question numbers: "Reading", "Listening" ...).
Skill = Literal["reading", "listening", "writing"]
# The skill a section's marks are REPORTED under. B2 First / C1 Advanced / C2
# Proficiency split their "Reading and Use of English" paper into two reported
# skills: those parts keep `skill="reading"` (one paper, one block, questions
# 1..N) and set `reported_skill="use_of_english"` (Parts 2-4).
ReportedSkill = Literal["reading", "use_of_english", "listening", "writing"]
# The Cambridge PAPER a section is sat in (derived, never authored per section:
# `contracts.papers.section_paper(level, section)`). YLE + A2 Key: Listening and
# Reading & Writing; B1 Preliminary: Reading, Writing, Listening; B2 First / C1
# Advanced / C2 Proficiency: Reading & Use of English, Writing, Listening. A
# teacher may assign a student only some papers (`RosterEntry.papers`). IQ tests
# have ONE paper, "iq", covering every section.
Paper = Literal[
    "reading_writing",
    "reading",
    "reading_use_of_english",
    "writing",
    "listening",
    "iq",
]

# Young Learners levels (uk/ru runner UI). Every other level is English-only.
YLE_LEVELS: frozenset[str] = frozenset({"PRE_A1_STARTERS", "A1_MOVERS", "A2_FLYERS"})
# Levels reported on five skills (Reading, Use of English, Writing, Listening,
# Speaking) whose overall Cambridge score is their average.
FIVE_SKILL_LEVELS: frozenset[str] = frozenset({"B2_FIRST", "C1_ADVANCED", "C2_PROFICIENCY"})

# Key word transformation: how many words the gap takes INCLUDING the key word
# (Cambridge handbooks: B2 "two to five", C1 "between three and six", C2 "between
# three and eight"). Other levels have no such task; an item there falls back to
# the B2 limits unless it sets its own `min_words` / `max_words`.
KWT_WORD_LIMITS: dict[str, tuple[int, int]] = {
    "B2_FIRST": (2, 5),
    "C1_ADVANCED": (3, 6),
    "C2_PROFICIENCY": (3, 8),
}
KWT_DEFAULT_LIMITS: tuple[int, int] = (2, 5)
# The ONE gap of a KWT `gapped_sentence`: a run of 3+ underscores, 3+ dots or "…".
# The runner splits the sentence at the first match and puts the input there.
KWT_GAP_PATTERN = r"_{3,}|\.{3,}|…+"
KWT_GAP_RE = re.compile(KWT_GAP_PATTERN)


class _Base(BaseModel):
    """Strict base: unknown fields raise, so malformed fixtures fail loudly."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Options (text | image) — authoring carries the canonical `key`.
# --------------------------------------------------------------------------- #
class TextOption(_Base):
    kind: Literal["text"] = "text"
    key: str = Field(description="Canonical option key, e.g. 'A'. Authoring-only.")
    text: str


class ImageOption(_Base):
    kind: Literal["image"] = "image"
    key: str = Field(description="Canonical option key, e.g. 'A'. Authoring-only.")
    asset_id: str
    alt: str | None = None


Option = Annotated[TextOption | ImageOption, Field(discriminator="kind")]


class ClientTextOption(_Base):
    kind: Literal["text"] = "text"
    text: str


class ClientImageOption(_Base):
    kind: Literal["image"] = "image"
    asset_id: str
    alt: str | None = None


ClientOption = Annotated[
    ClientTextOption | ClientImageOption, Field(discriminator="kind")
]


# --------------------------------------------------------------------------- #
# Section stimulus union (shared between authoring & client — never holds answers).
# --------------------------------------------------------------------------- #
# Optional context pictures shown alongside a text/audio stimulus (asset ids).
# Young-learner parts (YLE) constantly need a picture next to a passage, a note
# form, or the audio — this carries them without breaking "one stimulus per
# section". Never an answer key; just illustrative blobs served like any asset.
_CONTEXT_IMAGES_DESC = "Optional context image asset_ids shown beside this stimulus."


class PassageTextStimulus(_Base):
    kind: Literal["passage_text"] = "passage_text"
    text: str
    images: list[str] = Field(default_factory=list, description=_CONTEXT_IMAGES_DESC)


class AudioAssetStimulus(_Base):
    """A listening recording. Each listening section may carry its OWN track.

    Per-part audio (Cambridge YLE ships one mp3 per part): consecutive listening
    sections form one *track* while their `asset_id` stays the same; a section whose
    `asset_id` differs from the previous audio section's starts a NEW track, and a
    listening section with a non-audio stimulus (e.g. a matching pool) continues the
    current one. The track's settings (`plays`, `locked`, `preview_asset_id`) come
    from the section that starts it. Tracks play strictly in order (the runner never
    lets a later part's track start before the current one has ended); the
    furthest position per track (`asset_id`) is stored server-side
    (`AudioTrackProgress`). One `asset_id` may only be used by consecutive sections
    (the editor refuses A, B, A).
    """

    kind: Literal["audio_asset"] = "audio_asset"
    asset_id: str
    plays: int = Field(
        default=1,
        ge=1,
        description=(
            "Replay limit for the main track. Default 1: Cambridge recordings already "
            "bake in the double play, so the online track is played once start-to-end."
        ),
    )
    look_through_seconds: int = Field(default=0, ge=0)
    preview_asset_id: str | None = Field(
        default=None,
        description=(
            "Optional sound-check / instructions clip (the recording's opening "
            "explanation), played BEFORE the main track and freely replayable."
        ),
    )
    locked: bool = Field(
        default=True,
        description=(
            "When true the main track cannot be paused, seeked, or replayed once "
            "started (leaving/answering does not stop it); the preview clip stays free."
        ),
    )
    images: list[str] = Field(default_factory=list, description=_CONTEXT_IMAGES_DESC)


class ImageSetStimulus(_Base):
    kind: Literal["image_set"] = "image_set"
    asset_ids: list[str] = Field(min_length=1)


class GappedTextStimulus(_Base):
    kind: Literal["gapped_text"] = "gapped_text"
    text: str = Field(description="Passage text with gap markers (PET Reading Part 4).")
    images: list[str] = Field(default_factory=list, description=_CONTEXT_IMAGES_DESC)


class PoolOption(_Base):
    """One option in a section-level matching pool (A-H). No correctness here."""

    key: str
    text: str
    image: str | None = Field(
        default=None,
        description=(
            "Optional picture of this option (asset_id), e.g. the picture beside each "
            "word in a YLE word box. Served: the runner shows it above the word on the "
            "word-bank chip / letter button (alt = the option text). Not answer data."
        ),
    )


class MatchingPoolStimulus(_Base):
    kind: Literal["matching_pool"] = "matching_pool"
    text: str = Field(
        default="",
        description="Optional passage/intro shown above the A-H pool (e.g. a gapped text).",
    )
    options: list[PoolOption] = Field(min_length=2)
    images: list[str] = Field(default_factory=list, description=_CONTEXT_IMAGES_DESC)
    each_option_once: bool = Field(
        default=False,
        description=(
            "True = every option fits at most ONE question (a gapped text: B2 Reading "
            "Part 6, C1 Part 7, C2 Part 6 - one option is a distractor); the runner "
            "greys out an option already used in this part. False = options are "
            "reusable (multiple matching: 'some options may be correct for more than "
            "one question'). Display hint only - grading compares each item's key."
        ),
    )
    audio: AudioAssetStimulus | None = Field(
        default=None,
        description=(
            "The recording a LISTENING matching part plays (C1/C2 Listening Part 4: "
            "two parallel tasks, each with its own A-H pool, over one recording). "
            "It opens / continues a listening track exactly like an `audio_asset` "
            "stimulus: consecutive sections with the same `audio.asset_id` share ONE "
            "track (played once for both tasks). None = the part continues the "
            "current track."
        ),
    )


Stimulus = Annotated[
    PassageTextStimulus
    | AudioAssetStimulus
    | ImageSetStimulus
    | GappedTextStimulus
    | MatchingPoolStimulus,
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------- #
# Items — AUTHORING family (carries the answer key).
# --------------------------------------------------------------------------- #
# Optional per-item weight (authoring-only, never served). None = the historical
# default: 1 for an objective item, 5 (`DEFAULT_WRITING_POINTS`) for open_writing /
# colour_task. E.g. Cambridge YLE Movers R&W Part 6 sentences are worth 2 marks.
_POINTS_DESC = (
    "Marks this item is worth (1-20). None = default (objective 1, writing / "
    "colouring 5). Objective: a right answer earns all of them. Writing: the "
    "grader's 0-5 band is scaled to it. Authoring-only, never served."
)


def _points_field() -> int | None:
    return Field(default=None, ge=1, le=20, description=_POINTS_DESC)  # type: ignore[return-value]


class SingleChoiceItem(_Base):
    item_type: Literal["single_choice"] = "single_choice"
    id: str
    prompt: str
    image: str | None = Field(
        default=None,
        description="Optional picture shown with the prompt (e.g. a picture to label).",
    )
    options: list[Option] = Field(min_length=2)
    correct: str = Field(description="Canonical `key` of the correct option.")
    shuffle_options: bool = Field(
        default=True,
        description=(
            "False = the options keep their authored order for every student (e.g. "
            "'yes / no', 'true / false / doesn't say', A-B-C pictures in a row). True "
            "(default) = per-student option shuffle (golden rule #7). Authoring-only: "
            "the client just receives the options in display order."
        ),
    )
    points: int | None = _points_field()


class GapFillItem(_Base):
    item_type: Literal["gap_fill"] = "gap_fill"
    id: str
    prompt: str
    image: str | None = Field(
        default=None,
        description=(
            "Optional picture shown with the gap (asset_id), e.g. the symbol whose "
            "number the student recalls. Must exist in storage on import."
        ),
    )
    accepted: list[str] = Field(min_length=1, description="Exact acceptable answers.")
    accepted_variants: list[str] = Field(
        default_factory=list, description="Cambridge 'acceptable misspellings'."
    )
    rejected: list[str] = Field(
        default_factory=list,
        description=(
            "Forms the key marks WRONG although they are close to an accepted answer "
            "(a different word form / common grammar error, e.g. 'goes' for 'go'). "
            "Never typo-accepted. Authoring-only, never served."
        ),
    )
    spelling: Literal["exact", "tolerant"] | None = Field(
        default=None,
        description=(
            "Spelling policy for grading. 'exact' = only the listed answers count; "
            "'tolerant' = recognisable misspellings are accepted. None = derive from "
            "the section skill: reading / writing -> exact (open cloze), listening -> "
            "tolerant (set 'exact' for a word spelled out letter by letter); C1 / C2 "
            "listening and Use of English -> exact. Authoring-only, never served."
        ),
    )
    stem_word: str | None = Field(
        default=None,
        description=(
            "Word formation (B2-C2 Use of English Part 3): the stem the answer is "
            "derived from, shown to the student beside the gap, e.g. 'COMPETE'."
        ),
    )
    max_words: int | None = Field(
        default=None,
        ge=1,
        le=10,
        description=(
            "Most words the answer may have (open cloze / word formation = 1). A "
            "longer answer, or two answers given, is wrong: a '/', a backslash or '|' in the "
            "response ('in/on') counts as two answers. Words are split on whitespace "
            "('7.30' is one word); contractions count as two words (B2: except "
            "can't). An answer equal to an accepted key is never failed by this rule. "
            "Shown to the student. None = no limit."
        ),
    )
    numeric: bool = Field(
        default=False,
        description=(
            "The answer is a number: the runner opens the numeric keyboard "
            "(inputmode=numeric). A display hint only — grading is unchanged."
        ),
    )
    points: int | None = _points_field()


class KwtAnswer(_Base):
    """One acceptable answer pattern of a key word transformation, split in two.

    Each half is worth one mark (Cambridge: "The mark scheme splits the answer into
    two parts and candidates gain one mark for each part which is correct"). Each
    half lists its own acceptable texts (`are said` / `is said`); the key's
    `a | b OR c | d` becomes two `KwtAnswer`s, and alternatives may split at
    different points (`may have | forgotten` OR `may | have forgotten`).
    """

    part_a: list[str] = Field(
        min_length=1, description="Acceptable texts of the FIRST half (1 mark)."
    )
    part_b: list[str] = Field(
        min_length=1, description="Acceptable texts of the SECOND half (1 mark)."
    )


class KeyWordTransformationItem(_Base):
    """Key word transformation (B2-C2 Use of English Part 4).

    The student reads the lead-in sentence (`prompt`), sees the KEY WORD and
    completes the second sentence (`gapped_sentence`, one gap) in `min_words` ..
    `max_words` words INCLUDING the unchanged key word. Marked per half (0, 1 or 2;
    `points` defaults to 2). Authoring-only key: `answers`.
    """

    item_type: Literal["key_word_transformation"] = "key_word_transformation"
    id: str
    prompt: str = Field(description="The lead-in sentence the student transforms.")
    key_word: str = Field(min_length=1, description="The given key word, e.g. 'IDEA'.")
    gapped_sentence: str = Field(
        description="The second sentence with exactly ONE gap marker (`____`)."
    )
    answers: list[KwtAnswer] = Field(
        min_length=1, description="Acceptable answer patterns (authoring-only)."
    )
    min_words: int | None = Field(
        default=None, ge=1, le=10, description="Override of the level's minimum."
    )
    max_words: int | None = Field(
        default=None, ge=1, le=12, description="Override of the level's maximum."
    )
    points: int | None = _points_field()


def kwt_word_limits(item: KeyWordTransformationItem | None, level: str | None) -> tuple[int, int]:
    """(min, max) words of a KWT answer, key word included: the item's own
    overrides, else the level's Cambridge limits (`KWT_WORD_LIMITS`), else
    `KWT_DEFAULT_LIMITS`."""
    lo, hi = KWT_WORD_LIMITS.get(level or "", KWT_DEFAULT_LIMITS)
    if item is not None:
        lo = item.min_words if item.min_words is not None else lo
        hi = item.max_words if item.max_words is not None else hi
    return lo, hi


class MatchingItem(_Base):
    item_type: Literal["matching"] = "matching"
    id: str
    prompt: str
    correct: str = Field(description="`key` of the correct option in the section pool.")
    points: int | None = _points_field()


class OpenWritingItem(_Base):
    item_type: Literal["open_writing"] = "open_writing"
    id: str
    prompt: str
    word_min: int = Field(gt=0)
    bullet_points: list[str] = Field(default_factory=list)
    rubric: str = Field(description="Grading guidance (authoring-only, never served).")
    grade_mode: Literal["llm", "manual"] = "llm"
    optional: bool = Field(
        default=False,
        description=(
            "Part of a 'choose one of these' group (e.g. B1 Writing Part 2). A blank "
            "optional task is not graded — the student answered the other one."
        ),
    )
    points: int | None = _points_field()


class ColourTaskItem(_Base):
    """A 'listen and colour' task (YLE). The student paints a line-art scene; it is
    teacher-reviewed (never auto-scored), like writing. The colouring `key` is
    authoring-only guidance and, like `rubric`, never reaches the student."""

    item_type: Literal["colour_task"] = "colour_task"
    id: str
    prompt: str
    asset_id: str = Field(description="Line-art scene to colour (transparent background).")
    palette: list[str] = Field(
        default_factory=lambda: ["blue", "green", "red", "brown"],
        min_length=1,
        description="Selectable brush colours offered to the student.",
    )
    key: str = Field(
        default="",
        description="What to colour what — teacher review guidance (never served).",
    )
    points: int | None = _points_field()


# --------------------------------------------------------------------------- #
# IQ family (Phase 17): mark_toggle + drawing.
# --------------------------------------------------------------------------- #
class MarkToggleItem(_Base):
    """One row the student may mark or leave unmarked (e.g. "put an X on every pair
    that is NOT identical"). Response: "1" = marked, "0" / "" / none = unmarked.

    Graded PER SECTION (`app/grading/mark_toggle.py`): a row with `correct=True`
    is worth 1 point when marked; a `correct=False` row is worth 0, but each FALSE
    mark cancels one correct mark of the same section (floor 0) — marking every
    row earns nothing. `correct` is authoring-only, never served.
    """

    item_type: Literal["mark_toggle"] = "mark_toggle"
    id: str
    prompt: str = Field(description="The row's text, e.g. '3874 — 3847'.")
    image: str | None = Field(default=None, description="Optional picture of the row.")
    correct: bool = Field(description="True = this row SHOULD be marked (authoring-only).")


# Normalized image coordinates: (0, 0) = top-left of the picture, (1, 1) = bottom-right.
_Unit = Annotated[float, Field(ge=0.0, le=1.0)]
Point = tuple[_Unit, _Unit]


class RectZone(_Base):
    """An axis-aligned target rectangle (normalized image coordinates)."""

    kind: Literal["rect"] = "rect"
    x: _Unit
    y: _Unit
    w: float = Field(gt=0.0, le=1.0)
    h: float = Field(gt=0.0, le=1.0)


class PolygonZone(_Base):
    """A target polygon (normalized image coordinates, >= 3 vertices, any winding)."""

    kind: Literal["polygon"] = "polygon"
    points: list[Point] = Field(min_length=3, max_length=200)


Zone = Annotated[RectZone | PolygonZone, Field(discriminator="kind")]


class DrawingRegionTarget(_Base):
    """"Draw what is missing": the ink must fall (mostly) inside the zones.

    Ink length is measured in PICTURE WIDTHS (1.0 = a stroke as long as the picture
    is wide). Correct when total ink >= `min_ink`, ink inside the zones (dilated by
    `margin`) / total ink >= `min_inside_ratio`, and — with `require_all_zones` —
    every zone holds at least `min_ink / len(zones)` of ink. Authoring-only.
    """

    mode: Literal["region"] = "region"
    zones: list[Zone] = Field(min_length=1, max_length=20)
    min_ink: float = Field(default=0.05, gt=0.0, le=20.0)
    min_inside_ratio: float = Field(default=0.7, gt=0.0, le=1.0)
    margin: float = Field(
        default=0.02, ge=0.0, le=0.2, description="Zone dilation (picture widths)."
    )
    require_all_zones: bool = True


class Segment(_Base):
    """A straight reference line inside the square (square-local [0,1] coords)."""

    a: Point
    b: Point


class SquareBox(_Base):
    """Where the reference square sits on the picture (normalized image coords).
    Served to the client (the canvas frame) — it is not answer data."""

    x: _Unit
    y: _Unit
    w: float = Field(gt=0.0, le=1.0)
    h: float = Field(gt=0.0, le=1.0)


class DrawingLinesTarget(_Base):
    """"Divide the square": the student's strokes must reproduce `segments`.

    Segments are in square-local coordinates ((0,0) = the square's top-left,
    (1,1) = its bottom-right). `tolerance` is a distance in square sides. With
    `allow_transforms` the answer may be rotated 90/180/270 or mirrored (the 8
    symmetries of the square). Ink ON the square's border is ignored when
    `ignore_border` (the border is printed). Authoring-only (except `square`,
    which the client twin carries).
    """

    mode: Literal["lines"] = "lines"
    square: SquareBox
    segments: list[Segment] = Field(min_length=1, max_length=50)
    tolerance: float = Field(default=0.08, gt=0.0, le=0.5)
    allow_transforms: bool = True
    ignore_border: bool = True


DrawingTarget = Annotated[DrawingRegionTarget | DrawingLinesTarget, Field(discriminator="mode")]

# The largest drawing response (JSON chars) anyone accepts: delivery refuses a
# bigger save (413 `answer_too_large`), the grader treats one as invalid. The
# runner (`apps/web/exam-iq.js` DRAW_MAX_CHARS) stays well under it.
DRAWING_MAX_RESPONSE_CHARS = 300_000


class DrawingItem(_Base):
    """A free-hand drawing on a canvas over a picture (IQ "draw what is missing",
    "divide the square"). Graded deterministically by `app/grading/drawing.py`;
    a low-confidence verdict goes to the teacher's review queue (`needs_review`).

    Response: a JSON string {"w": W, "h": H, "strokes": [[[x, y], ...], ...]} in
    canvas pixels (the canvas covers the picture). `target` is authoring-only.
    """

    item_type: Literal["drawing"] = "drawing"
    id: str
    prompt: str
    asset_id: str = Field(description="The picture shown under the canvas.")
    mode: Literal["region", "lines"]
    target: DrawingTarget
    points: int | None = _points_field()

    @model_validator(mode="after")
    def _mode_matches_target(self) -> DrawingItem:
        if self.target.mode != self.mode:
            raise ValueError(f"drawing mode {self.mode!r} != target mode {self.target.mode!r}")
        return self


Item = Annotated[
    SingleChoiceItem
    | GapFillItem
    | MatchingItem
    | OpenWritingItem
    | ColourTaskItem
    | KeyWordTransformationItem
    | MarkToggleItem
    | DrawingItem,
    Field(discriminator="item_type"),
]


# --------------------------------------------------------------------------- #
# Items — CLIENT family (NO answer key, structurally).
# --------------------------------------------------------------------------- #
class ClientSingleChoiceItem(_Base):
    item_type: Literal["single_choice"] = "single_choice"
    id: str
    prompt: str
    image: str | None = None
    options: list[ClientOption] = Field(min_length=2)


class ClientGapFillItem(_Base):
    item_type: Literal["gap_fill"] = "gap_fill"
    id: str
    prompt: str
    image: str | None = None
    stem_word: str | None = Field(
        default=None, description="Word-formation stem shown beside the gap, e.g. 'COMPETE'."
    )
    max_words: int | None = Field(
        default=None, description="'Use no more than N word(s)' (None = no limit shown)."
    )
    numeric: bool = Field(
        default=False, description="A number is expected: open the numeric keyboard."
    )


class ClientKeyWordTransformationItem(_Base):
    """The student's view of a key word transformation - no `answers`.

    Render: the lead-in `prompt`, the `key_word` (bold capitals), then
    `gapped_sentence` split at its first `KWT_GAP_RE` match with ONE text input in
    the gap, plus the rubric "Use between `min_words` and `max_words` words,
    including the word given." The answer is saved as plain text (the words typed
    into the gap only).
    """

    item_type: Literal["key_word_transformation"] = "key_word_transformation"
    id: str
    prompt: str
    key_word: str
    gapped_sentence: str
    min_words: int = Field(ge=1)
    max_words: int = Field(ge=1)


class ClientMatchingItem(_Base):
    item_type: Literal["matching"] = "matching"
    id: str
    prompt: str


class ClientOpenWritingItem(_Base):
    item_type: Literal["open_writing"] = "open_writing"
    id: str
    prompt: str
    word_min: int = Field(gt=0)
    bullet_points: list[str] = Field(default_factory=list)


class ClientColourTaskItem(_Base):
    item_type: Literal["colour_task"] = "colour_task"
    id: str
    prompt: str
    asset_id: str
    palette: list[str] = Field(min_length=1)


class ClientMarkToggleItem(_Base):
    """A row to mark / leave unmarked — no `correct`. Save "1" (marked) / "0"."""

    item_type: Literal["mark_toggle"] = "mark_toggle"
    id: str
    prompt: str
    image: str | None = None


class ClientDrawingItem(_Base):
    """The student's drawing canvas — no `target`. For `mode="lines"` the canvas
    frames `square` (where the square sits on the picture, normalized). Save the
    strokes as a JSON string {"w", "h", "strokes": [[[x, y], ...], ...]}."""

    item_type: Literal["drawing"] = "drawing"
    id: str
    prompt: str
    asset_id: str
    mode: Literal["region", "lines"]
    square: SquareBox | None = None


ClientItem = Annotated[
    ClientSingleChoiceItem
    | ClientGapFillItem
    | ClientMatchingItem
    | ClientOpenWritingItem
    | ClientColourTaskItem
    | ClientKeyWordTransformationItem
    | ClientMarkToggleItem
    | ClientDrawingItem,
    Field(discriminator="item_type"),
]


# --------------------------------------------------------------------------- #
# Section extras (IQ family, Phase 17): untimed intro + show-then-hide study.
# --------------------------------------------------------------------------- #
class IntroSample(_Base):
    """One worked sample problem on a subtest's intro screen (text and/or pictures;
    a sample may show its own answer — it is not a scored item)."""

    text: str = ""
    images: list[str] = Field(default_factory=list, description=_CONTEXT_IMAGES_DESC)


class PracticeOption(_Base):
    """One choice of a `choice` practice problem (`text` may be empty when the
    choices are drawn on the practice picture, e.g. "a" .. "e")."""

    key: str = Field(min_length=1, max_length=8)
    text: str = ""


PracticeKind = Literal["text", "choice", "mark", "drawing"]


class IntroPractice(_Base):
    """A booklet PRACTICE problem done on the intro's Example screen, before the
    clock: the student answers, presses Check and sees right / wrong + the printed
    answer.

    NOT a scored item: never graded, never saved as an answer, never counted. Its
    `solution` is PUBLIC by design — the booklet prints it next to the practice —
    so it is served with the intro. Golden rule #1 is about scored items: their
    keys still never reach the client (`tests/test_no_correct_invariant.py` checks
    that `solution` exists only here, under `SectionIntro.practice`).

    kinds: `text` = type the answer (checked case-insensitively, spaces collapsed);
    `choice` = pick one of `options` (`solution` = its key); `mark` = mark the row
    or not (`solution` "1" / "0"); `drawing` = a not-checked "try it" canvas over
    `images[0]` with a "show answer" (`solution_images` and/or `explanation`).
    """

    prompt: str = ""
    images: list[str] = Field(default_factory=list, description=_CONTEXT_IMAGES_DESC)
    kind: PracticeKind = "text"
    options: list[PracticeOption] = Field(default_factory=list)
    solution: str = Field(
        default="",
        description="The printed (public) answer of this practice problem — see the "
        "class doc; scored items never carry one.",
    )
    solution_images: list[str] = Field(
        default_factory=list, description="Pictures of the printed answer (asset ids)."
    )
    explanation: str = ""

    @model_validator(mode="after")
    def _kind_shape(self) -> IntroPractice:
        if self.kind == "choice":
            keys = [o.key for o in self.options]
            if len(keys) < 2 or len(set(keys)) != len(keys):
                raise ValueError("a choice practice needs at least 2 options with unique keys")
            if self.solution not in keys:
                raise ValueError("a choice practice's solution must be one of its option keys")
        elif self.options:
            raise ValueError(f"a {self.kind} practice has no options")
        if self.kind == "mark" and self.solution not in ("1", "0"):
            raise ValueError('a mark practice needs solution "1" (mark) or "0" (leave)')
        if self.kind == "text" and not self.solution.strip():
            raise ValueError("a text practice needs its solution")
        if self.kind == "drawing" and not (
            self.images and (self.solution_images or self.explanation.strip())
        ):
            raise ValueError("a drawing practice needs a picture and an answer to show")
        return self


class SectionIntro(_Base):
    """Untimed instructions + sample problems shown BEFORE the subtest clock starts.
    Not secret: served verbatim with the section. The runner shows it step by step
    (Instructions -> Example: `samples` + interactive `practice` -> Ready)."""

    text: str = ""
    images: list[str] = Field(default_factory=list, description=_CONTEXT_IMAGES_DESC)
    samples: list[IntroSample] = Field(default_factory=list)
    practice: list[IntroPractice] = Field(
        default_factory=list,
        description="Booklet practice problems (not scored, answers public).",
    )


class SectionStudy(_Base):
    """A show-then-hide stimulus (memory subtests): shown for `study_seconds`, then
    removed before the recall items appear. A picture (`asset_id`) and/or `text`.

    Authoring model. NEVER part of the bulk `ClientSection`: the section only says
    that a study phase exists (`ClientStudyInfo`); the content travels as a separate
    `ClientStudyStimulus`, which delivery serves only inside the study window (see
    app/delivery/CLAUDE.md "Study stimulus contract").
    """

    asset_id: str | None = None
    images: list[str] = Field(
        default_factory=list,
        max_length=60,
        description=(
            "Study CELLS (asset ids, reading order) shown as a responsive grid — "
            "legible on a phone where one wide sheet would be tiny. Served like "
            "`asset_id`: only inside the study window, per index, never via /assets."
        ),
    )
    text: str | None = None
    study_seconds: int = Field(ge=1, le=3600)

    @model_validator(mode="after")
    def _has_content(self) -> SectionStudy:
        if self.asset_id is None and not self.images and not (self.text or "").strip():
            raise ValueError("study needs a picture (asset_id), cells (images) or text")
        return self


class ClientStudyInfo(_Base):
    """What the bulk client payload knows about a study phase: only that it exists
    and how long it lasts — never the content."""

    study_seconds: int = Field(ge=1)


class ClientStudyStimulus(_Base):
    """The study content itself, served by its own delivery endpoint ONLY while the
    study window is open.

    The picture is never named by its asset id (the public `/assets/{id}` refuses
    study assets): `image_url` is an attempt-authenticated, window-gated route
    (`GET /exam/attempts/{id}/sections/{sid}/study/asset`, `X-Attempt-Token`,
    `Cache-Control: no-store`) that answers 409 `study_not_open` after the window."""

    section_id: str
    image_url: str | None = Field(
        default=None,
        description=(
            "Attempt-scoped URL of the study picture (send X-Attempt-Token; only "
            "inside the study window). None = text-only study."
        ),
    )
    image_urls: list[str] = Field(
        default_factory=list,
        description=(
            "Attempt-scoped URLs of the study CELLS, in order (`.../study/asset/{i}`; "
            "same token + window gating as `image_url`), laid out as a grid."
        ),
    )
    text: str | None = None
    study_seconds: int = Field(ge=1)


# --------------------------------------------------------------------------- #
# Norm-referenced scoring (IQ family) — authoring-only, never served.
# --------------------------------------------------------------------------- #
# A norm-table cell: an int, None (no norm), or an open-ended bound as a string:
# "130+" (at least 130 — the table's top) or "<70" (below 70 — the table's bottom).
NORM_OPEN_PATTERN = r"^(\d+)\+$|^<(\d+)$"
NormCell = int | Annotated[str, Field(pattern=NORM_OPEN_PATTERN)] | None


class NormComposite(_Base):
    """A composite = the sum of its sections' SCALED scores (e.g. "I" = odd subtests)."""

    key: str = Field(min_length=1)
    label: str = ""
    section_ids: list[str] = Field(min_length=1)


class ScaledBand(_Base):
    """raw_min..raw_max (inclusive) -> scaled."""

    raw_min: int = Field(ge=0)
    raw_max: int = Field(ge=0)
    scaled: int


class AgeGroup(_Base):
    """An age band of the norm table, whole years inclusive; `max_age` None = open."""

    key: str = Field(min_length=1)
    min_age: int = Field(ge=0, le=150)
    max_age: int | None = Field(default=None, ge=0, le=150)


class NormRow(_Base):
    """One row of the sum -> score table: the composite's scaled-score SUM and the
    score per age group (`values[group_key]`)."""

    sum: int
    values: dict[str, NormCell] = Field(default_factory=dict)


class AgeNorms(_Base):
    age_groups: list[AgeGroup] = Field(min_length=1)
    rows: list[NormRow] = Field(min_length=1)


class NormBracket(_Base):
    """A verbal band of the final score: the highest bracket with `min` <= score."""

    label: str = Field(min_length=1)
    min: int


class NormScoring(_Base):
    """Generic norm-referenced scoring (`app/grading/norm_scoring.py`).

    per-section raw -> scaled (`scaled_tables[section_id]`) -> composite sums ->
    composite score by age (`by_age`) -> final (`mean_of_composites`) -> bracket.
    Authoring-only: never in `ClientTest` (tables are the test's key).
    """

    composites: list[NormComposite] = Field(min_length=1)
    scaled_tables: dict[str, list[ScaledBand]] = Field(
        description="section_id -> raw bands (ascending, no gaps / overlaps)."
    )
    by_age: AgeNorms
    final: Literal["mean_of_composites"] = "mean_of_composites"
    brackets: list[NormBracket] = Field(default_factory=list)
    min_age: int = Field(default=14, ge=0, le=150)
    note: str = Field(default="", description="Teacher-facing note (e.g. 'non-clinical').")


# --------------------------------------------------------------------------- #
# Section & Test containers.
# --------------------------------------------------------------------------- #
class Section(_Base):
    id: str
    title: str | None = None
    skill: Skill | None = None
    reported_skill: ReportedSkill | None = Field(
        default=None,
        description=(
            "The skill this part's marks are reported under, when it differs from "
            "the paper `skill`: B2-C2 Reading and Use of English Parts 2-4 keep "
            "skill='reading' and set 'use_of_english'. None = `skill`. Teacher-side "
            "only (results, CSV, feedback); never served."
        ),
    )
    stimulus: Stimulus
    items: list[Item] = Field(min_length=1)
    time_limit_seconds: int | None = Field(
        default=None,
        ge=1,
        le=36000,
        description=(
            "Own server-authoritative clock for this section (IQ subtests); None = "
            "the section shares the test / paper time."
        ),
    )
    intro: SectionIntro | None = Field(
        default=None, description="Untimed instructions + samples before the clock starts."
    )
    study: SectionStudy | None = Field(
        default=None,
        description="Show-then-hide stimulus (requires `time_limit_seconds`). Never bulk-served.",
    )

    @property
    def score_skill(self) -> str | None:
        """The skill this section's marks count toward (`reported_skill` or `skill`)."""
        return self.reported_skill or self.skill


class Test(_Base):
    id: str
    title: str
    level: Level
    status: PublishStatus = "draft"
    duration_minutes: int = Field(gt=0)
    sections: list[Section] = Field(min_length=1)
    # -- exam window (schedule) -------------------------------------------- #
    # NOT key-bearing: a teacher may change it on a published test with attempts
    # (the content lock treats it as schedule meta). Stored as columns on `tests`.
    # Delivery turns it into an `ExamWindow`: a NEW attempt may start in
    # [opens_at, closes_at + grace]; its deadline is min(start + duration,
    # closes_at + grace), fixed at start (golden rule #3). None = no bound.
    opens_at: AwareDatetime | None = Field(
        default=None, description="When new attempts may start (UTC-aware). None = any time."
    )
    closes_at: AwareDatetime | None = Field(
        default=None,
        description="Hard end of the session: no new starts after it (+ grace), and it "
        "caps every attempt's deadline (+ grace). None = never closes.",
    )
    late_start_grace_minutes: int = Field(
        default=0,
        ge=0,
        le=1440,
        description="Extends closes_at for starting late AND for the deadline cap.",
    )
    # -- per-paper time (timing meta, like the window: not key-bearing) ------ #
    paper_minutes: dict[Paper, Annotated[int, Field(ge=1, le=600)]] | None = Field(
        default=None,
        description=(
            "Explicit minutes per paper (keys = the test's papers, see "
            "`contracts.papers.papers_of`); the editor requires every paper of the "
            "test and a sum equal to `duration_minutes`. None = split "
            "`duration_minutes` proportionally to the official Cambridge per-paper "
            "times (`contracts.papers.paper_minutes`). Used for a student assigned "
            "only some papers: their deadline is the sum of those papers' minutes. "
            "The whole test always takes `duration_minutes`."
        ),
    )
    norm_scoring: NormScoring | None = Field(
        default=None,
        description=(
            "Norm-referenced scoring (IQ family): raw -> scaled -> composites -> score "
            "by age -> bracket. Authoring-only, never served; teacher-side report."
        ),
    )

    @model_validator(mode="after")
    def _window_in_order(self) -> Test:
        if (
            self.opens_at is not None
            and self.closes_at is not None
            and self.closes_at <= self.opens_at
        ):
            raise ValueError("closes_at must be after opens_at")
        return self


class ClientSection(_Base):
    id: str
    title: str | None = None
    skill: Skill | None = None
    stimulus: Stimulus
    items: list[ClientItem] = Field(
        default_factory=list,
        description="Never empty, except when `items_withheld` (a timed section not "
        "yet in its answering phase).",
    )
    items_withheld: bool = Field(
        default=False,
        description="Timed sections (IQ): the items are not served before the section "
        "is begun and its study window is over; fetch them from the attempt's "
        "section items endpoint then (app/delivery/CLAUDE.md).",
    )
    item_count: int = Field(
        default=0,
        ge=0,
        description="How many items the section has — also while `items_withheld` "
        "(the intro's Ready screen: 'N questions · about X s per question').",
    )
    time_limit_seconds: int | None = None
    intro: SectionIntro | None = None
    study: ClientStudyInfo | None = Field(
        default=None,
        description="Only that a study phase exists + its length; the content is a "
        "separate `ClientStudyStimulus` served inside the study window.",
    )


class ClientTest(_Base):
    id: str
    title: str
    level: Level
    duration_minutes: int = Field(gt=0)
    sections: list[ClientSection] = Field(min_length=1)
