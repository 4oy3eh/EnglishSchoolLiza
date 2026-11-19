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

# Cambridge levels, easiest first. The three Young Learners (YLE) levels are
# PRE_A1_STARTERS / A1_MOVERS / A2_FLYERS (the runner offers a uk/ru UI for them);
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


class MatchingPoolStimulus(_Base):
    kind: Literal["matching_pool"] = "matching_pool"
    text: str = Field(
        default="",
        description="Optional passage/intro shown above the A-H pool (e.g. a gapped text).",
    )
    options: list[PoolOption] = Field(min_length=2)
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
    points: int | None = _points_field()


class GapFillItem(_Base):
    item_type: Literal["gap_fill"] = "gap_fill"
    id: str
    prompt: str
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


Item = Annotated[
    SingleChoiceItem
    | GapFillItem
    | MatchingItem
    | OpenWritingItem
    | ColourTaskItem
    | KeyWordTransformationItem,
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
    stem_word: str | None = Field(
        default=None, description="Word-formation stem shown beside the gap, e.g. 'COMPETE'."
    )
    max_words: int | None = Field(
        default=None, description="'Use no more than N word(s)' (None = no limit shown)."
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


ClientItem = Annotated[
    ClientSingleChoiceItem
    | ClientGapFillItem
    | ClientMatchingItem
    | ClientOpenWritingItem
    | ClientColourTaskItem
    | ClientKeyWordTransformationItem,
    Field(discriminator="item_type"),
]


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
    items: list[ClientItem] = Field(min_length=1)


class ClientTest(_Base):
    id: str
    title: str
    level: Level
    duration_minutes: int = Field(gt=0)
    sections: list[ClientSection] = Field(min_length=1)
