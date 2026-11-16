"""Admin-facing view models (Phase 10).

These are *read-side DTOs* the teacher dashboard renders — they compose existing
contract types (`GradingResult`, `AnalysisVerdict`, `IntegrityProfile`,
`IntegrityEvent`) rather than introduce new persisted shapes. Nothing here is
stored, so (like `app/ingestion/models.py`) it lives in the engine, not in
`contracts/` — no schema/migration is implied (golden rule #4).

The result models deliberately keep grading and integrity as *separate fields*
side by side (golden rule #2): the score is computed independently of any
cheating signal, and the advisory verdict never mutates it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from contracts import (
    AnalysisVerdict,
    GradingResult,
    IntegrityEvent,
    IntegrityProfile,
    RosterEntry,
    SkillReport,
)


class ReviewDraft(BaseModel):
    """One ingested draft awaiting human approval in the review queue."""

    model_config = ConfigDict(extra="forbid")

    test_id: str
    title: str
    level: str
    section_count: int
    item_count: int


class RosterStatus(BaseModel):
    """Live status of one roster entry for the teacher's roster view."""

    model_config = ConfigDict(extra="forbid")

    roster_entry_id: str
    display_name: str
    status: str
    attempt_id: str | None = None
    # The attempt's server deadline (None before a start): lets the dashboard offer
    # extra time on re-open when the original deadline has already passed.
    deadline: datetime | None = None


class AttemptOverview(BaseModel):
    """A compact results row — score next to the advisory suspicion (rule #2).

    The suspicion / confidence / AI-assist numbers are READ from the verdict stored
    on the frozen `AttemptReport` (computed once at finalize). The suspicion is the
    deterministic rule engine's — it is what the table is ranked by. None = no stored
    verdict yet (still writing, finalize pending, or a report that predates verdict
    persistence) — the dashboard shows "—"; a list read never computes one.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    attempt_id: str
    roster_entry_id: str | None
    display_name: str | None
    score: float
    max_score: float
    needs_review: bool
    suspicion_score: float | None = None
    confidence: float | None = None
    # Highest per-writing-item `ai_assist_likelihood` in the stored verdict (None
    # without a verdict or without writing): drives the "✍ AI?" marker.
    ai_assist_max: float | None = None
    # The stored verdict's `model_id`: the LLM that gave the second opinion on
    # WRITING (None = rule engine only). It never produced `suspicion_score`.
    verdict_model_id: str | None = None
    # Titles of the strongest rule hits behind the suspicion (at most 2, distinct):
    # the chip's tooltip. Empty without a verdict or without hits.
    top_rules: list[str] = []
    # True when the stored verdict predates the rule engine (`rules_version` None):
    # its suspicion came from the old formula / an LLM and a regrade recomputes it.
    verdict_legacy: bool = False
    event_count: int
    # The attempt's own lifecycle state (in_progress / submitted / expired): lets the
    # dashboard show "—" for a student still working instead of "grading…".
    attempt_status: str = "submitted"
    # Finalize state: "complete" / "failed" once the report exists; with no report,
    # "pending" while the submit-time background grade is still expected to land
    # (or the attempt is still in progress) and "missing" once a finished attempt
    # has gone without a report past the grace window — i.e. the finalize was lost
    # (process restart) and the teacher should re-grade. Drives the status chip.
    report_status: str = "pending"
    # Why finalize failed (`AttemptReport.error`), shown as the chip's tooltip.
    report_error: str | None = None
    # When the attempt finished (submit time; the deadline for an expired one) —
    # the dashboard's "submitted" column / sort key. None while still working.
    submitted_at: datetime | None = None


class ResponseChange(BaseModel):
    """One decoded `answer_change` from the replay — what the student tried.

    `value` is rendered for the teacher (a `single_choice` displayed index is
    de-shuffled back to its canonical option key first), so the history reads as
    'tried A → C', not as raw display indices.
    """

    model_config = ConfigDict(extra="forbid")

    at: datetime
    value: str


class ItemResponse(BaseModel):
    """Per-question review row: what the student answered vs the key, graded.

    This is a *teacher* surface, so it carries the correct answer (golden rule #1
    forbids that only on the student runner). Grading and the answer key are read
    straight off the authoring `Item` + the stored canonical `Answer`; no cheating
    signal is consulted here (rule #2).
    """

    model_config = ConfigDict(extra="forbid")

    item_id: str
    item_no: str | None = None  # the printed question number (as in the timeline)
    section_title: str | None
    skill: str | None
    item_type: str
    prompt: str
    bullet_points: list[str]  # writing content points (empty for other types)
    word_min: int | None  # writing minimum word count
    student_answer: str | None  # rendered canonical final answer; None = unanswered
    writing_text: str | None  # full free text for open_writing (read in full)
    correct_answer: str | None  # rendered key/accepted; None for open_writing
    is_correct: bool | None  # None when not auto-graded (writing / manual)
    awarded: float
    max_points: float
    needs_review: bool
    manual: bool  # the teacher hand-entered this item's mark
    history: list[ResponseChange]  # every change the student made, in order


class TimelineEntry(BaseModel):
    """One captured event rendered for a human reviewer.

    The raw `events` on `AttemptResult` stay machine-readable (for scripts /
    analysers); this is the parallel *human* view — each event decoded to a plain
    sentence with the question it touched (`single_choice` indices de-shuffled to
    the option the student saw). Capture only — it judges nothing (rule #6)."""

    model_config = ConfigDict(extra="forbid")

    at: datetime
    type: str
    item_id: str | None
    item_no: str | None  # the printed question number, when the event is question-scoped
    label: str  # human sentence, e.g. 'Q7: answered "C — Louisa"'
    integrity: bool  # True for tab-leave / focus / device events (highlight for review)


class DeviceCapture(BaseModel):
    """One device/session fingerprint captured at a (re)start of the attempt.

    Legally observable client signals (user-agent, platform, locale, screen) plus
    the server-observed IP. Surfaced for the teacher; the deterministic
    `device_changed` flag on the `IntegrityProfile` is what flags a device switch.
    """

    model_config = ConfigDict(extra="forbid")

    at: datetime
    user_agent: str | None
    platform: str | None
    timezone: str | None
    language: str | None
    screen: str | None  # "WxH"
    viewport: str | None  # "WxH"
    server_ip: str | None


class AttemptResult(BaseModel):
    """Full results detail: score + advisory verdict + the raw replay (rule #6).

    The teacher always sees the deterministic integrity `profile` and the raw
    `events` next to the LLM `verdict`, so the advice is auditable. `responses`
    adds the per-question breakdown (answer vs key, graded) and the full writing;
    `devices` the captured device/IP fingerprints.
    """

    model_config = ConfigDict(extra="forbid")

    attempt_id: str
    roster_entry_id: str | None
    display_name: str | None
    grading: GradingResult
    responses: list[ItemResponse]
    devices: list[DeviceCapture]
    timeline: list[TimelineEntry]
    # None only with `verdict_source == "unavailable"` (see `verdict_note`).
    profile: IntegrityProfile | None
    verdict: AnalysisVerdict | None
    # "stored": the verdict frozen on the report at finalize (rule engine, plus the
    # optional LLM second opinion on writing).
    # "live_fallback": no stored verdict yet (in progress / pending / a pre-migration
    # report) — the rule engine run on this read, never an LLM.
    # "unavailable": no stored verdict AND the live computation failed (e.g. a
    # malformed stored event): the dashboard shows "—" and `verdict_note`.
    verdict_source: str = "stored"
    # Russian note for the teacher when the verdict could not be shown.
    verdict_note: str | None = None
    event_count: int
    events: list[IntegrityEvent]
    # Per-skill pedagogical feedback (strengths/weaknesses), computed once on submit
    # and read from the persisted report. Empty until the attempt is finalized.
    skills: list[SkillReport] = []
    summary: str = ""
    report_status: str = "pending"
    report_error: str | None = None


class TestStats(BaseModel):
    """How much student data hangs off a test — shown before delete / unpublish."""

    model_config = ConfigDict(extra="forbid")
    __test__ = False  # not a pytest test class despite the name

    test_id: str
    roster_count: int
    attempt_count: int  # attempts of any status (a delete wipes all of them)
    active_count: int  # attempts still `in_progress` (students writing right now)


class BulkAddResult(BaseModel):
    """Outcome of adding many roster names in one request."""

    model_config = ConfigDict(extra="forbid")

    added: list[RosterEntry]
    # Names not added because the test already had (or the request repeated) that
    # exact display name — reported so the teacher sees nothing was silently lost.
    skipped: list[str]


class SkillScore(BaseModel):
    """One skill's line on the printable student report."""

    model_config = ConfigDict(extra="forbid")

    skill: str
    score: float
    max_score: float
    strengths: str = ""
    weaknesses: str = ""


class WritingTaskReport(BaseModel):
    """One writing task as printed for the student: task, their text, the mark."""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    section_title: str | None
    prompt: str
    bullet_points: list[str]
    word_min: int | None
    text: str | None
    word_count: int
    awarded: float
    max_points: float
    graded: bool  # a mark exists (teacher's, or the LLM grader's not needing review)
    manual: bool  # the teacher set the mark by hand
    feedback: str | None  # the examiner/LLM comment carried on the grade, if any


class StudentReport(BaseModel):
    """The student/parent-facing printable report for one attempt.

    Deliberately carries NO integrity data — no suspicion, verdict, events or
    device info. Grades and the stored per-skill feedback only; built without any
    grading/LLM call (frozen report + the teacher's manual marks)."""

    model_config = ConfigDict(extra="forbid")

    attempt_id: str
    display_name: str | None
    test_title: str
    level: str
    attempt_status: str
    finished_at: datetime | None
    score: float
    max_score: float
    report_status: str
    skills: list[SkillScore]
    summary: str
    writing: list[WritingTaskReport]
