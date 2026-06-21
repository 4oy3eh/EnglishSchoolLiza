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

from pydantic import BaseModel, ConfigDict

from contracts import AnalysisVerdict, GradingResult, IntegrityEvent, IntegrityProfile


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


class AttemptOverview(BaseModel):
    """A compact results row — score next to the advisory suspicion (rule #2)."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    attempt_id: str
    roster_entry_id: str | None
    display_name: str | None
    score: float
    max_score: float
    needs_review: bool
    suspicion_score: float
    confidence: float
    event_count: int


class AttemptResult(BaseModel):
    """Full results detail: score + advisory verdict + the raw replay (rule #6).

    The teacher always sees the deterministic integrity `profile` and the raw
    `events` next to the LLM `verdict`, so the advice is auditable.
    """

    model_config = ConfigDict(extra="forbid")

    attempt_id: str
    roster_entry_id: str | None
    display_name: str | None
    grading: GradingResult
    profile: IntegrityProfile
    verdict: AnalysisVerdict
    event_count: int
    events: list[IntegrityEvent]
