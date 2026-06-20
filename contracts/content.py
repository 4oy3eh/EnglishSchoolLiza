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

from pydantic import BaseModel, ConfigDict, Field

Level = Literal["A2_KEY", "B1_PRELIMINARY"]
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
class PassageTextStimulus(_Base):
    kind: Literal["passage_text"] = "passage_text"
    text: str


class AudioAssetStimulus(_Base):
    kind: Literal["audio_asset"] = "audio_asset"
    asset_id: str
    plays: int = Field(default=2, ge=1, description="Replay limit for online listening.")
    look_through_seconds: int = Field(default=0, ge=0)


class ImageSetStimulus(_Base):
    kind: Literal["image_set"] = "image_set"
    asset_ids: list[str] = Field(min_length=1)


class GappedTextStimulus(_Base):
    kind: Literal["gapped_text"] = "gapped_text"
    text: str = Field(description="Passage text with gap markers (PET Reading Part 4).")


class PoolOption(_Base):
    """One option in a section-level matching pool (A-H). No correctness here."""

    key: str
    text: str


class MatchingPoolStimulus(_Base):
    kind: Literal["matching_pool"] = "matching_pool"
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
class SingleChoiceItem(_Base):
    item_type: Literal["single_choice"] = "single_choice"
    id: str
    prompt: str
    options: list[Option] = Field(min_length=2)
    correct: str = Field(description="Canonical `key` of the correct option.")


class GapFillItem(_Base):
    item_type: Literal["gap_fill"] = "gap_fill"
    id: str
    prompt: str
    accepted: list[str] = Field(min_length=1, description="Exact acceptable answers.")
    accepted_variants: list[str] = Field(
        default_factory=list, description="Cambridge 'acceptable misspellings'."
    )


class MatchingItem(_Base):
    item_type: Literal["matching"] = "matching"
    id: str
    prompt: str
    correct: str = Field(description="`key` of the correct option in the section pool.")


class OpenWritingItem(_Base):
    item_type: Literal["open_writing"] = "open_writing"
    id: str
    prompt: str
    word_min: int = Field(gt=0)
    bullet_points: list[str] = Field(default_factory=list)
    rubric: str = Field(description="Grading guidance (authoring-only, never served).")
    grade_mode: Literal["llm", "manual"] = "llm"


Item = Annotated[
    SingleChoiceItem | GapFillItem | MatchingItem | OpenWritingItem,
    Field(discriminator="item_type"),
]


# --------------------------------------------------------------------------- #
# Items — CLIENT family (NO answer key, structurally).
# --------------------------------------------------------------------------- #
class ClientSingleChoiceItem(_Base):
    item_type: Literal["single_choice"] = "single_choice"
    id: str
    prompt: str
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


ClientItem = Annotated[
    ClientSingleChoiceItem | ClientGapFillItem | ClientMatchingItem | ClientOpenWritingItem,
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
