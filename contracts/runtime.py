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

from contracts.content import Skill

AttemptStatus = Literal["not_started", "in_progress", "submitted", "expired"]
ReportStatus = Literal["complete", "failed"]
# `expired`: the attempt ran out of time (server deadline) without a submit — a
# finished state like `submitted` (roster column is a free string: no migration).
RosterStatus = Literal["not_started", "in_progress", "submitted", "expired"]
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
    # Writing-field capture (open_writing). Shapes only, NEVER content: no key
    # identities, no clipboard text (payloads are validated in app/telemetry/schema.py).
    "field_focus",  # a writing field gained focus (+ its current length)
    "field_blur",  # a writing field lost focus (+ its current length)
    "writing_stats",  # aggregated typing stats for one field since the last flush
    "input_burst",  # one input event that added >= 2 chars at once (+ inputType, length)
    "paste_attempt",  # a BLOCKED paste/drop: only the length of what was attempted
    "fullscreen_exit",  # the exam left fullscreen
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
    feedback: str | None = Field(
        default=None,
        description=(
            "Examiner feedback for writing (carried from the LLM grader); "
            "None for objective items."
        ),
    )


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


class WritingFeatures(_Base):
    """Deterministic per-writing-item features (integrity layer, NO judgment of guilt).

    Built from the writing-field telemetry (`field_focus` / `writing_stats` /
    `input_burst` / `paste_attempt`), the `answer_change` snapshots, the visibility /
    blur / fullscreen events overlapping the writing window, and cheap text
    statistics of the submitted text. `flags` are threshold facts documented in
    `app/integrity/CLAUDE.md` (e.g. "text_appeared_without_typing"), not a verdict.
    Carries no score (golden rule #2).
    """

    item_id: str
    has_keystroke_telemetry: bool = Field(
        default=False, description="True when at least one writing_stats event exists."
    )
    final_length: int = Field(default=0, ge=0, description="Chars in the final text.")
    typed_chars: int = Field(default=0, ge=0, description="Chars added by keyboard input.")
    non_keyboard_chars: int = Field(
        default=0, ge=0, description="Chars added by paste/drop/replacement/bulk inserts."
    )
    unexplained_chars: int = Field(
        default=0,
        ge=0,
        description="Final chars not accounted for by ANY captured input event.",
    )
    typed_ratio: float | None = Field(
        default=None, ge=0, le=1, description="typed / (typed + non_keyboard + unexplained)."
    )
    deleted_chars: int = Field(default=0, ge=0)
    revision_ratio: float = Field(
        default=0.0, ge=0, description="deleted / added (how much the student revised)."
    )
    keystrokes: int = Field(default=0, ge=0)
    untrusted_inputs: int = Field(
        default=0, ge=0, description="Input events dispatched by a script (isTrusted=false)."
    )
    paste_attempts: int = Field(default=0, ge=0)
    max_paste_attempt_length: int = Field(default=0, ge=0)
    max_non_keyboard_insert: int = Field(default=0, ge=0)
    max_snapshot_growth: int = Field(
        default=0, ge=0, description="Largest growth between consecutive answer snapshots."
    )
    max_snapshot_growth_ms: int | None = Field(
        default=None, ge=0, description="Time over which that growth happened."
    )
    active_typing_ms: int = Field(default=0, ge=0)
    chars_per_active_minute: float | None = Field(default=None, ge=0)
    inter_key_mean_ms: float | None = Field(default=None, ge=0)
    inter_key_p50_ms: float | None = Field(default=None, ge=0)
    inter_key_p90_ms: float | None = Field(default=None, ge=0)
    pauses_over_2s: int = Field(default=0, ge=0)
    pauses_over_10s: int = Field(default=0, ge=0)
    time_to_first_char_ms: int | None = Field(default=None, ge=0)
    writing_span_ms: int = Field(default=0, ge=0, description="First focus -> last change.")
    hidden_ms_while_writing: int = Field(default=0, ge=0)
    blur_ms_while_writing: int = Field(default=0, ge=0)
    away_count: int = Field(default=0, ge=0, description="Hides/blurs during the window.")
    fullscreen_exits_while_writing: int = Field(default=0, ge=0)
    # Telemetry completeness (a capture fact, not a suspicion): when keystroke
    # telemetry was lost, `unexplained_chars` is unreliable.
    telemetry_gap: bool = Field(
        default=False,
        description=(
            "True when part of this item's keystroke telemetry was probably lost: the "
            "recorder restarted (reload) inside the writing window, or answer snapshots "
            "grew at typing speed with no writing_stats covering the growth."
        ),
    )
    uncovered_snapshot_growth: int = Field(
        default=0,
        ge=0,
        description=(
            "Chars the answer snapshots show arriving at human typing speed that no "
            "captured input event accounts for (lost telemetry, not pasted text)."
        ),
    )
    recorder_restarts_while_writing: int = Field(
        default=0,
        ge=0,
        description="Page reloads / recorder restarts strictly inside the writing window.",
    )
    # Cheap text statistics of the submitted text (level-mismatch evidence only).
    word_count: int = Field(default=0, ge=0)
    sentence_count: int = Field(default=0, ge=0)
    mean_sentence_length: float = Field(default=0.0, ge=0)
    type_token_ratio: float = Field(default=0.0, ge=0, le=1)
    long_word_share: float = Field(
        default=0.0, ge=0, le=1, description="Share of words with >= 8 letters."
    )
    flags: list[str] = Field(default_factory=list)


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
    paste_attempt_count: int = Field(
        default=0, ge=0, description="Blocked paste/drop attempts anywhere in the exam."
    )
    fullscreen_exit_count: int = Field(default=0, ge=0)
    writing: list[WritingFeatures] = Field(
        default_factory=list, description="Per-writing-item features (open_writing)."
    )


# --------------------------------------------------------------------------- #
# Analysis — LLM verdict, ADVISORY ONLY; never mutates score (rules #2, #6).
# --------------------------------------------------------------------------- #
class WritingAssessment(_Base):
    """Advisory likelihood that ONE writing answer was produced with outside help.

    Outside help = ChatGPT / translator / text copied from another device or tab /
    dictation. Advisory only: no score, never moves a grade (golden rule #2).
    """

    item_id: str
    ai_assist_likelihood: float = Field(ge=0, le=1)
    signals: list[str] = Field(
        default_factory=list, description="The evidence tags the judgment rests on."
    )
    rationale: str = Field(default="", description="1-2 sentences for the teacher (Russian).")


class AnalysisVerdict(_Base):
    # `model_id` collides with pydantic's protected `model_` namespace; allow it.
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    attempt_id: str
    suspicion_score: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    flags: list[str] = Field(default_factory=list)
    summary: str
    model_id: str | None = Field(
        default=None,
        description="LLM id, for auditability. None = the deterministic-only verdict.",
    )
    writing: list[WritingAssessment] = Field(
        default_factory=list, description="Per-writing-item outside-help assessments."
    )
    error: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Why the LLM analyst was not used (`<ExceptionType>: message`); the verdict "
            "is then the deterministic-only fallback."
        ),
    )
    input_fingerprint: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Fingerprint of the inputs this verdict was computed from (event count, last "
            "event id, digest of the analysis context), set by the results layer. An "
            "automatic retry reuses a stored LLM verdict whose fingerprint still matches "
            "instead of paying for the same analysis again. Not evidence; never shown."
        ),
    )


# --------------------------------------------------------------------------- #
# Feedback / report — pedagogical strengths & weaknesses per skill.
#
# This is the academic-performance axis (how well the student did), NOT integrity
# (golden rule #2/#6): it is built from the GradingResult + answers and never reads
# any cheating signal. Computed once when the attempt is finalized (on submit) and
# persisted as an `attempt_report`, so the dashboard reads it instead of recomputing
# (no per-load LLM calls).
# --------------------------------------------------------------------------- #
class SkillReport(_Base):
    skill: Skill
    score: float = Field(ge=0)
    max_score: float = Field(ge=0)
    strengths: str = Field(description="What the student did well in this skill.")
    weaknesses: str = Field(description="What the student should work on.")


class AttemptReport(_Base):
    # `model_id` collides with pydantic's protected `model_` namespace; allow it.
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    attempt_id: str
    graded_at: datetime
    status: ReportStatus = "complete"
    grading: GradingResult = Field(
        description="The frozen auto-grade computed once at finalize (objective + LLM writing)."
    )
    skills: list[SkillReport] = Field(default_factory=list)
    summary: str = Field(default="", description="Overall pedagogical summary for the teacher.")
    model_id: str | None = Field(
        default=None, description="LLM id used for the report, for auditability."
    )
    error: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Why finalize failed (`<ExceptionType>: message`, truncated), set only when "
            "status='failed' so the teacher sees the cause and can re-grade."
        ),
    )
    # Integrity axis, stored NEXT TO (never mixed into) the grade — rules #2/#6.
    integrity: IntegrityProfile | None = Field(
        default=None,
        description="The deterministic integrity profile the verdict was computed from.",
    )
    verdict: AnalysisVerdict | None = Field(
        default=None,
        description=(
            "Advisory cheating verdict computed once at finalize (Gemini, or the "
            "deterministic-only fallback). Never feeds a score."
        ),
    )
