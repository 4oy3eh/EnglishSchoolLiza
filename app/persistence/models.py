"""ORM models mirroring the `contracts/` schemas (CLAUDE.md golden rule #4).

Mapping notes (where the relational shape differs from the nested contract):

* Content is a nested document (Test -> Section -> Item). It is normalized into
  three tables joined by FK + an explicit `position` to preserve order. The
  polymorphic, type-specific parts that the relational layer should NOT re-model
  (option lists, the answer key, the writing rubric, the section stimulus union)
  are stored as JSON whose shape stays governed by the contracts. The repository
  validates them back through Pydantic on read, so drift fails loudly.
* The flat runtime models (RosterEntry, Attempt, Answer, IntegrityEvent) map
  field-for-field to columns.

The student-facing `Client*` contracts are intentionally NOT persisted: they are
a projection of authoring content produced at delivery time and must never carry
an answer key (golden rule #1).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Dialect,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.core.db import Base


class UtcDateTime(TypeDecorator[datetime]):
    """A timezone-aware datetime that always round-trips as UTC-aware.

    `DateTime(timezone=True)` silently drops `tzinfo` on sqlite (and is
    backend-dependent elsewhere), which would make a reloaded `Attempt.deadline`
    incomparable to a freshly computed `datetime.now(UTC)`. The server timer is
    authoritative (golden rule #3) and event ordering matters (rule #6), so we
    normalize at the type boundary: a naive value is assumed UTC, anything aware
    is converted to UTC, and reads always come back UTC-aware. DDL is identical
    to `DateTime(timezone=True)`, so migrations are unaffected.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

# --------------------------------------------------------------------------- #
# Content: Test -> Section -> Item.
# --------------------------------------------------------------------------- #


class TestRow(Base):
    __tablename__ = "tests"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    level: Mapped[str] = mapped_column(String)  # Level literal
    status: Mapped[str] = mapped_column(String, default="draft")  # PublishStatus
    duration_minutes: Mapped[int] = mapped_column(Integer)

    sections: Mapped[list[SectionRow]] = relationship(
        back_populates="test",
        cascade="all, delete-orphan",
        order_by="SectionRow.position",
    )


class SectionRow(Base):
    __tablename__ = "sections"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    test_id: Mapped[str] = mapped_column(ForeignKey("tests.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    skill: Mapped[str | None] = mapped_column(String, nullable=True)  # Skill literal
    # Stimulus discriminated union, validated through contracts on read.
    stimulus: Mapped[dict[str, Any]] = mapped_column(JSON)

    test: Mapped[TestRow] = relationship(back_populates="sections")
    items: Mapped[list[ItemRow]] = relationship(
        back_populates="section",
        cascade="all, delete-orphan",
        order_by="ItemRow.position",
    )


class ItemRow(Base):
    __tablename__ = "items"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    section_id: Mapped[str] = mapped_column(
        ForeignKey("sections.id", ondelete="CASCADE")
    )
    position: Mapped[int] = mapped_column(Integer)
    item_type: Mapped[str] = mapped_column(String)  # item_type discriminator
    prompt: Mapped[str] = mapped_column(Text)
    # Type-specific authoring fields incl. the answer key (options/correct/
    # accepted/accepted_variants/word_min/bullet_points/rubric/grade_mode).
    # Stays server-side only (golden rule #1).
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    section: Mapped[SectionRow] = relationship(back_populates="items")


# --------------------------------------------------------------------------- #
# Access / roster.
# --------------------------------------------------------------------------- #


class RosterEntryRow(Base):
    __tablename__ = "roster_entries"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    test_id: Mapped[str] = mapped_column(ForeignKey("tests.id", ondelete="CASCADE"))
    display_name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="not_started")  # RosterStatus
    # Intentionally NOT an FK: a roster entry exists before any attempt, and a
    # roster<->attempt FK pair would be circular. Delivery owns this back-pointer.
    attempt_id: Mapped[str | None] = mapped_column(String, nullable=True)


# --------------------------------------------------------------------------- #
# Attempt + answers (server-authoritative; timer never pauses, rule #3).
# --------------------------------------------------------------------------- #


class AttemptRow(Base):
    __tablename__ = "attempts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    test_id: Mapped[str] = mapped_column(ForeignKey("tests.id", ondelete="CASCADE"))
    roster_entry_id: Mapped[str] = mapped_column(
        ForeignKey("roster_entries.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String, default="not_started")  # AttemptStatus
    seed: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    deadline: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    answers: Mapped[list[AnswerRow]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan"
    )


class AnswerRow(Base):
    __tablename__ = "answers"
    # One answer per (attempt, item): enforced by the composite primary key
    # below; saving again overwrites in the repository.

    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), primary_key=True
    )
    item_id: Mapped[str] = mapped_column(String, primary_key=True)
    response: Mapped[str] = mapped_column(Text)
    answered_at: Mapped[datetime] = mapped_column(UtcDateTime)

    attempt: Mapped[AttemptRow] = relationship(back_populates="answers")


# --------------------------------------------------------------------------- #
# Telemetry — append-only capture, NO judgment (rule #6).
# --------------------------------------------------------------------------- #


class IntegrityEventRow(Base):
    __tablename__ = "integrity_events"

    # Append-only stream: surrogate PK, no natural key.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), index=True
    )
    item_id: Mapped[str | None] = mapped_column(String, nullable=True)
    type: Mapped[str] = mapped_column(String)  # EventType literal
    client_ts: Mapped[datetime] = mapped_column(UtcDateTime)
    server_ts: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
