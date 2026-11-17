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

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

# Cambridge levels, easiest first. The three Young Learners (YLE) levels are
# PRE_A1_STARTERS / A1_MOVERS / A2_FLYERS (the runner offers a uk/ru UI for them);
# their "Reading & Writing" paper is authored as `skill="reading"` sections, like
# Movers (docs/INGEST_VIA_CLAUDE_CODE.md).
Level = Literal["PRE_A1_STARTERS", "A1_MOVERS", "A2_FLYERS", "A2_KEY", "B1_PRELIMINARY"]
PublishStatus = Literal["draft", "published"]
Skill = Literal["reading", "listening", "writing"]


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
            "tolerant (set 'exact' for a word spelled out letter by letter). "
            "Authoring-only, never served."
        ),
    )
    points: int | None = _points_field()


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
    SingleChoiceItem | GapFillItem | MatchingItem | OpenWritingItem | ColourTaskItem,
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
    | ClientColourTaskItem,
    Field(discriminator="item_type"),
]


# --------------------------------------------------------------------------- #
# Section & Test containers.
# --------------------------------------------------------------------------- #
class Section(_Base):
    id: str
    title: str | None = None
    skill: Skill | None = None
    stimulus: Stimulus
    items: list[Item] = Field(min_length=1)


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
