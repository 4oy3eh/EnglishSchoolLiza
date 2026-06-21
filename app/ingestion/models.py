"""Ingestion-internal **draft** models — the LLM's extraction, before the key.

These mirror the authoring item shapes in `contracts/content.py` but **deliberately
omit the answer key**: a `DraftSingleChoice` has options (with their `A`/`B`/`C` keys)
but no `correct`; a `DraftGapFill` has a prompt but no `accepted`. The multimodal LLM
fills these out from the question paper; it is *structurally incapable* of inventing
the answer key (golden rule #5 — the answer-key PDF is authoritative for `correct`).

`answer_key.merge_key` is the only thing that turns a `DraftTest` into a validated,
authoring `contracts.Test` by attaching the parsed key. Each draft item carries the
Cambridge question `number` so the key can be joined by number.

These types never leave the ingestion engine and are never persisted, so they are not
part of `contracts/` (no JSON Schema / migration — golden rule #4 untouched).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from contracts.content import Level, Option, Skill, Stimulus


class _Base(BaseModel):
    """Strict: unknown fields raise so a malformed LLM extraction fails loudly."""

    model_config = ConfigDict(extra="forbid")


class DraftSingleChoice(_Base):
    item_type: Literal["single_choice"] = "single_choice"
    number: int = Field(gt=0, description="Cambridge question number; joins to the key.")
    prompt: str
    # Options carry their canonical `key` (A/B/C) but NOT the correct answer.
    options: list[Option] = Field(min_length=2)


class DraftGapFill(_Base):
    item_type: Literal["gap_fill"] = "gap_fill"
    number: int = Field(gt=0)
    prompt: str


class DraftMatching(_Base):
    item_type: Literal["matching"] = "matching"
    number: int = Field(gt=0)
    prompt: str


class DraftOpenWriting(_Base):
    item_type: Literal["open_writing"] = "open_writing"
    number: int = Field(gt=0)
    prompt: str
    word_min: int = Field(gt=0)
    bullet_points: list[str] = Field(default_factory=list)
    # Rubric is authoring guidance, not an answer key; writing has no key-PDF entry.
    rubric: str
    grade_mode: Literal["llm", "manual"] = "llm"


DraftItem = Annotated[
    DraftSingleChoice | DraftGapFill | DraftMatching | DraftOpenWriting,
    Field(discriminator="item_type"),
]


class DraftSection(_Base):
    id: str
    title: str | None = None
    skill: Skill | None = None
    stimulus: Stimulus  # shared with contracts; never holds answers
    items: list[DraftItem] = Field(min_length=1)


class DraftTest(_Base):
    """A whole extracted paper, pre-key. `merge_key` -> validated `contracts.Test`."""

    id: str
    title: str
    level: Level
    duration_minutes: int = Field(gt=0)
    sections: list[DraftSection] = Field(min_length=1)
