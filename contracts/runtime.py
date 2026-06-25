"""Runtime contracts: attempt lifecycle, telemetry, grading, integrity, analysis.

These are the moving-part schemas the engines exchange at request/grade time.
None of them is student-facing in the answer-key sense, but per the engine
boundaries (CLAUDE.md): grading output (`GradingResult`) and integrity output
(`IntegrityProfile`) stay separate, and the analysis verdict is advisory only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

AttemptStatus = Literal["not_started", "in_progress", "submitted", "expired"]
RosterStatus = Literal["not_started", "in_progress", "submitted"]
GradeMethod = Literal[
    "single_choice",
    "gap_fill",
    "matching",
    "open_writing_llm",
    "open_writing_manual",
    "colour_manual",
]
EventType = Literal[
    "visibility_hidden",
    "visibility_visible",
    "window_blur",
    "window_focus",
    "pagehide",
    "interaction",
    "answer_change",
    "audio_play",
    "audio_seek",
    "device_info",  # browser/device fingerprint captured at (re)start; IP server-stamped
]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Access / roster.
# --------------------------------------------------------------------------- #
class RosterEntry(_Base):
    id: str
    test_id: str
    display_name: str
    status: RosterStatus = "not_started"
    attempt_id: str | None = None


# --------------------------------------------------------------------------- #
# Attempt + answers (server-authoritative; timer never pauses, rule #3).
# --------------------------------------------------------------------------- #
class Attempt(_Base):
    id: str
    test_id: str
    roster_entry_id: str
    status: AttemptStatus = "not_started"
    seed: int = Field(description="Seed for reproducible pooling + option shuffle.")
    started_at: datetime | None = None
    submitted_at: datetime | None = None
    deadline: datetime | None = Field(
        default=None,
        description="Server-authoritative hard deadline: min(start + duration, window close).",
    )
    audio_progress_seconds: int = Field(
        default=0,
        ge=0,
        description=(
            "Furthest point (seconds) the student has reached in the listening "
            "track. Server-side + monotonic, so a refresh/new device resumes from "
            "here and can never replay the recording from the start."
        ),
    )


class Answer(_Base):
    attempt_id: str
    item_id: str
    response: str = Field(
        description=(
            "Canonical response: option key (single_choice/matching after "
            "de-shuffle), text (gap_fill), or written text (open_writing)."
        )
    )
    answered_at: datetime


class ManualGrade(_Base):
    """A teacher's hand-entered mark for one item (writing score, gap ✓/✗ override).

    Persisted separately from the auto `GradingResult` (which stays pure): the admin
    layer overlays these via `GradingService.apply_override` when assembling results.
    A cheating signal still never moves a score (golden rule #2) — this is the human
    teacher's judgement, not an integrity feature.
    """

    attempt_id: str
    item_id: str
    awarded: float = Field(ge=0, description="Points awarded by the teacher (0..max_points).")
    graded_at: datetime


# --------------------------------------------------------------------------- #
# Telemetry — append-only capture, NO judgment (rule #6).
# --------------------------------------------------------------------------- #
class IntegrityEvent(_Base):
    attempt_id: str
    item_id: str | None = None
    type: EventType
    client_ts: datetime = Field(description="Timestamp from the student's browser.")
    server_ts: datetime | None = Field(
        default=None, description="Stamped on ingest; trusted over client_ts."
    )
    duration_ms: int | None = Field(default=None, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Grading — deterministic + writing; never reads integrity (rule #2).
# --------------------------------------------------------------------------- #
class ItemGrade(_Base):
    item_id: str
    awarded: float = Field(ge=0)
    max_points: float = Field(gt=0)
    method: GradeMethod
    needs_review: bool = False


class GradingResult(_Base):
    attempt_id: str
    items: list[ItemGrade] = Field(default_factory=list)
    score: float = Field(ge=0)
    max_score: float = Field(ge=0)
    needs_review: bool = False


# --------------------------------------------------------------------------- #
# Integrity — deterministic features over the event stream, NO LLM (rule #6).
# --------------------------------------------------------------------------- #
class HiddenInterval(_Base):
    start: datetime
    end: datetime
    duration_ms: int = Field(ge=0)


class QuestionTiming(_Base):
    item_id: str
    latency_ms: int = Field(ge=0, description="Time spent before first answering.")
    interaction_count: int = Field(ge=0)
    post_return_latency_ms: int | None = Field(
        default=None, ge=0, description="visible -> answer latency after a hide."
    )


class IntegrityProfile(_Base):
    attempt_id: str
    question_timings: list[QuestionTiming] = Field(default_factory=list)
    hidden_intervals: list[HiddenInterval] = Field(default_factory=list)
    total_hidden_ms: int = Field(default=0, ge=0)
    pacing_cv: float = Field(
        default=0.0, ge=0, description="Coefficient of variation of per-question pace."
    )
    systematicity_rate: float = Field(
        default=0.0, ge=0, le=1, description="Fraction of questions showing the pattern."
    )
    device_count: int = Field(
        default=0, ge=0, description="Distinct device fingerprints seen across (re)starts."
    )
    device_changed: bool = Field(
        default=False, description="True if the attempt was resumed on a different device."
    )


# --------------------------------------------------------------------------- #
# Analysis — LLM verdict, ADVISORY ONLY; never mutates score (rules #2, #6).
# --------------------------------------------------------------------------- #
class AnalysisVerdict(_Base):
    # `model_id` collides with pydantic's protected `model_` namespace; allow it.
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    attempt_id: str
    suspicion_score: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    flags: list[str] = Field(default_factory=list)
    summary: str
    model_id: str | None = Field(default=None, description="LLM id, for auditability.")
