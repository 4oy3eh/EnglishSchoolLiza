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

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Dialect,
    Float,
    ForeignKey,
    Index,
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
    # Exam window (schedule meta, not key-bearing — contracts `Test.opens_at` ...).
    # Dedicated columns, not section JSON: the dashboard list reads them with the
    # row, and the teacher changes them with a one-row UPDATE that never rewrites
    # the sections/items of a locked test (`ContentRepository.set_schedule`).
    opens_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    closes_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    late_start_grace_minutes: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    # Explicit minutes per paper (contracts `Test.paper_minutes`, {paper: minutes});
    # NULL = proportional split of duration_minutes. Migration d0e1f2a3b4c5.
    paper_minutes: Mapped[dict[str, int] | None] = mapped_column(JSON, nullable=True)
    # Norm-referenced scoring (contracts `Test.norm_scoring`, IQ family): the
    # raw->scaled / by-age tables. Authoring-only (never served). NULL = none.
    # Migration e1f2a3b4c5d6.
    norm_scoring: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # The runner's chrome language (contracts `Test.ui_language`: en | ru | uk).
    # Migration a8b9c0d1e2f3; existing rows read "en".
    ui_language: Mapped[str] = mapped_column(String, default="en", server_default="en")

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
    # ReportedSkill literal (B2-C2 "use_of_english"); None = `skill`. Migration c9d0e1f2a3b4.
    reported_skill: Mapped[str | None] = mapped_column(String, nullable=True)
    # Stimulus discriminated union, validated through contracts on read.
    stimulus: Mapped[dict[str, Any]] = mapped_column(JSON)
    # IQ family (contracts `Section.time_limit_seconds` / `intro` / `study`;
    # migration e1f2a3b4c5d6). NULL = not used. `study` is authoring data that the
    # bulk client payload never carries (see app/delivery/CLAUDE.md).
    time_limit_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    intro: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    study: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

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
    # 4-char classroom PIN (contracts.runtime.PIN_PATTERN), unique within a test.
    # Plaintext on purpose: the teacher reads it back to print the PIN cards (see
    # app/delivery/CLAUDE.md). Nullable only at the schema level — the repository
    # assigns one on every insert and migration f6a7b8c9d0e1 backfilled old rows.
    pin: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # The assigned papers (contracts `RosterEntry.papers`, a JSON list of Paper
    # codes); NULL = the whole test. Migration d0e1f2a3b4c5.
    papers: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # Teacher-only (contracts `RosterEntry.birth_date`): IQ norms by age. Never
    # served by the student roster list. Migration e1f2a3b4c5d6.
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    __table_args__ = (
        Index("ix_roster_entries_test_pin", "test_id", "pin", unique=True),
    )


# --------------------------------------------------------------------------- #
# Attempt + answers (server-authoritative; timer never pauses, rule #3).
# --------------------------------------------------------------------------- #


class AttemptRow(Base):
    __tablename__ = "attempts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    test_id: Mapped[str] = mapped_column(ForeignKey("tests.id", ondelete="CASCADE"))
    # Unique: one attempt per roster entry (golden invariant — a roster entry
    # never gets a second attempt). This makes a concurrent double-start fail
    # loudly at the DB instead of silently forking two attempts; delivery catches
    # the violation and resumes the winner (see `DeliveryService.start`).
    roster_entry_id: Mapped[str] = mapped_column(
        ForeignKey("roster_entries.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[str] = mapped_column(String, default="not_started")  # AttemptStatus
    seed: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    deadline: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # Furthest listening-audio position reached (server-side, monotonic anti-replay).
    audio_progress_seconds: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    # The papers served (contracts `Attempt.papers`, copied from the roster entry at
    # start); NULL = the whole test. Migration d0e1f2a3b4c5.
    papers: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    answers: Mapped[list[AnswerRow]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan"
    )


class AttemptSessionRow(Base):
    """A per-attempt session token issued on a successful PIN start (Phase 14).

    Only the SHA-256 of the token is stored: a DB leak does not hand out live
    sessions. Several rows per attempt are allowed (the same student re-entering
    the PIN on another device / after clearing storage); the teacher's reset
    deletes them with the attempt. Server-internal auth state — no contract.
    """

    __tablename__ = "attempt_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)


class AttemptAudioProgressRow(Base):
    """Furthest point reached per listening TRACK of an attempt (contract
    `AudioTrackProgress`). One row per (attempt, track asset id); the repository
    only ever raises `seconds` (atomic upsert with max), so a reload resumes the
    right part's recording and can never restart it."""

    __tablename__ = "attempt_audio_progress"

    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), primary_key=True
    )
    asset_id: Mapped[str] = mapped_column(String, primary_key=True)
    seconds: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class AttemptSectionRow(Base):
    """The server clock of one timed section of an attempt (contract
    `AttemptSection`, IQ family; migration f2a3b4c5d6e7). One row per (attempt,
    section), created when the section becomes current; `started_at` / `closed_at`
    (+ the study-end `answering_started_at`) are written once each by conditional
    UPDATEs (`... IS NULL`), so two requests can never both own a transition."""

    __tablename__ = "attempt_sections"

    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), primary_key=True
    )
    section_id: Mapped[str] = mapped_column(String, primary_key=True)
    position: Mapped[int] = mapped_column(Integer)
    intro_started_at: Mapped[datetime] = mapped_column(UtcDateTime)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    started_by: Mapped[str | None] = mapped_column(String, nullable=True)
    study_started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    study_ends_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    answering_started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    deadline: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String, nullable=True)


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


class ManualGradeRow(Base):
    __tablename__ = "manual_grades"
    # One teacher mark per (attempt, item): composite PK, upserted on re-grade.

    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), primary_key=True
    )
    item_id: Mapped[str] = mapped_column(String, primary_key=True)
    awarded: Mapped[float] = mapped_column(Float)
    graded_at: Mapped[datetime] = mapped_column(UtcDateTime)


class AttemptReportRow(Base):
    __tablename__ = "attempt_reports"
    # One frozen report per attempt: the auto-grade + the per-skill pedagogical
    # feedback computed ONCE when the attempt is finalized (on submit), so the
    # dashboard reads it instead of recomputing (no per-load LLM calls). The
    # polymorphic grading + skills payloads are stored as JSON governed by the
    # contracts (GradingResult / list[SkillReport]) and re-validated on read.

    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("attempts.id", ondelete="CASCADE"), primary_key=True
    )
    graded_at: Mapped[datetime] = mapped_column(UtcDateTime)
    status: Mapped[str] = mapped_column(String, default="complete")  # ReportStatus
    grading: Mapped[dict[str, Any]] = mapped_column(JSON)  # GradingResult
    skills: Mapped[list[Any]] = mapped_column(JSON, default=list)  # list[SkillReport]
    summary: Mapped[str] = mapped_column(Text, default="")
    model_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Why finalize failed (status="failed"); NULL on a complete report.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Integrity axis, stored NEXT TO the grade (never mixed into it — rules #2/#6):
    # the deterministic IntegrityProfile the verdict was computed from, and the
    # advisory AnalysisVerdict (Gemini or deterministic-only). NULL on reports
    # written before this column existed / when analysis could not run at all.
    integrity: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    verdict: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


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
