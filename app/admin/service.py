"""Admin (teacher) engine: the composition root for the dashboard (Phase 10).

This is the one place allowed to hold grading *and* integrity/analysis at once,
because the teacher dashboard shows them side by side. It never feeds one into
the other (golden rule #2): `attempt_result` grades the attempt and profiles its
behaviour on independent paths and merely *bundles* the two — a suspicion score
never moves a grade, and ranking "suspicious-first" reorders rows without
touching any score.

Responsibilities:
- **bank**: list/get/delete authored tests.
- **review queue**: list ingested `draft` tests and **approve** them — the single
  `draft -> published` human-approval gate (golden rule #5). Approval is the only
  way content goes live; Phase 9 ingestion only ever produces drafts.
- **roster**: add students, live roster status.
- **results**: per-attempt score + advisory verdict + raw replay; per-test ranked
  suspicious-first.

Every admin action logs at INFO (auditability).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.admin.models import (
    AttemptOverview,
    AttemptResult,
    ReviewDraft,
    RosterStatus,
)
from app.analysis import AnalysisService
from app.analysis.llm import AnalysisLLM
from app.content import ContentService, RosterService
from app.content.storage import StorageBackend
from app.core.logging import get_logger
from app.grading.llm import LLMGrader
from app.grading.service import GradingService
from app.integrity import IntegrityService
from app.persistence.repository import (
    AttemptRepository,
    ContentRepository,
    EventRepository,
)
from contracts import RosterEntry, Test

log = get_logger(__name__)


class AdminError(Exception):
    """An admin operation referenced something that does not exist."""


class AdminService:
    """Teacher-facing operations over the whole platform (read + the publish gate)."""

    def __init__(
        self,
        *,
        content: ContentService,
        roster: RosterService,
        grading: GradingService,
        integrity: IntegrityService,
        analysis: AnalysisService,
        attempts: AttemptRepository,
        events: EventRepository,
    ) -> None:
        self.content = content
        self.roster = roster
        self.grading = grading
        self.integrity = integrity
        self.analysis = analysis
        self.attempts = attempts
        self.events = events

    # -- bank --------------------------------------------------------------- #
    def list_tests(self) -> list[Test]:
        return self.content.list_tests()

    def get_test(self, test_id: str) -> Test:
        test = self.content.get_test(test_id)
        if test is None:
            raise AdminError(f"test {test_id!r} not found")
        return test

    def delete_test(self, test_id: str) -> None:
        if not self.content.delete_test(test_id):
            raise AdminError(f"test {test_id!r} not found")
        log.info("admin delete_test id=%s", test_id)

    # -- review queue (the draft -> published human gate, rule #5) ---------- #
    def review_queue(self) -> list[ReviewDraft]:
        """Every ingested draft awaiting approval."""
        drafts = [
            ReviewDraft(
                test_id=t.id,
                title=t.title,
                level=t.level,
                section_count=len(t.sections),
                item_count=sum(len(s.items) for s in t.sections),
            )
            for t in self.content.list_tests()
            if t.status == "draft"
        ]
        log.info("admin review_queue -> %d draft(s)", len(drafts))
        return drafts

    def approve(self, test_id: str) -> Test:
        """Publish a draft after human review (golden rule #5).

        This is the ONLY path content goes live. Refuses to "approve" anything
        that is not currently a draft, so the gate can't be a silent no-op.
        """
        test = self.get_test(test_id)
        if test.status != "draft":
            raise AdminError(f"test {test_id!r} is {test.status}, not a draft")
        if not self.content.publish(test_id):  # pragma: no cover - get_test guarded
            raise AdminError(f"test {test_id!r} not found")
        log.info("admin APPROVE id=%s draft->published (human-approved)", test_id)
        return self.get_test(test_id)

    def unpublish(self, test_id: str) -> Test:
        """Pull a published test back to draft (revert an approval).

        Mirrors `approve`'s care: refuses anything not currently `published`, so
        the log line can't misrepresent a no-op as a real state transition.
        """
        test = self.get_test(test_id)
        if test.status != "published":
            raise AdminError(f"test {test_id!r} is {test.status}, not published")
        self.content.unpublish(test_id)
        log.info("admin unpublish id=%s published->draft", test_id)
        return self.get_test(test_id)

    # -- roster ------------------------------------------------------------- #
    def add_student(self, test_id: str, display_name: str) -> RosterEntry:
        self.get_test(test_id)  # 404 on an unknown test rather than a dangling entry
        entry = self.roster.add_student(test_id, display_name)
        log.info("admin add_student test=%s name=%s entry=%s", test_id, display_name, entry.id)
        return entry

    def roster_status(self, test_id: str) -> list[RosterStatus]:
        """Live roster: who is assigned, who is in-progress / submitted."""
        statuses = [
            RosterStatus(
                roster_entry_id=e.id,
                display_name=e.display_name,
                status=e.status,
                attempt_id=e.attempt_id,
            )
            for e in self.roster.list_students(test_id)
        ]
        log.info("admin roster_status test=%s -> %d", test_id, len(statuses))
        return statuses

    # -- results (score + advisory verdict + replay) ------------------------ #
    def attempt_result(self, attempt_id: str) -> AttemptResult:
        """Full results detail for one attempt (rule #6: replay next to verdict).

        Grading and integrity/analysis are computed on independent paths and only
        bundled here (rule #2) — neither influences the other.
        """
        attempt = self.attempts.get_attempt(attempt_id)
        if attempt is None:
            raise AdminError(f"attempt {attempt_id!r} not found")

        grading = self.grading.grade(attempt_id)
        profile = self.integrity.profile(attempt_id)
        verdict = self.analysis.analyze(attempt_id)
        events = self.events.list_events(attempt_id)

        entry = (
            self.attempts.get_roster_entry(attempt.roster_entry_id)
            if attempt.roster_entry_id
            else None
        )
        log.info(
            "admin result attempt=%s score=%.2f/%.2f suspicion=%.3f events=%d",
            attempt_id,
            grading.score,
            grading.max_score,
            verdict.suspicion_score,
            len(events),
        )
        return AttemptResult(
            attempt_id=attempt_id,
            roster_entry_id=attempt.roster_entry_id,
            display_name=entry.display_name if entry else None,
            grading=grading,
            profile=profile,
            verdict=verdict,
            event_count=len(events),
            events=events,
        )

    def results_for_test(self, test_id: str) -> list[AttemptOverview]:
        """All started attempts for a test, ranked most-suspicious first.

        Ranking only reorders rows by the advisory suspicion score; it never
        changes a score (golden rule #2).
        """
        self.get_test(test_id)
        overviews: list[AttemptOverview] = []
        for entry in self.roster.list_students(test_id):
            if entry.attempt_id is None:
                continue
            detail = self.attempt_result(entry.attempt_id)
            overviews.append(
                AttemptOverview(
                    attempt_id=detail.attempt_id,
                    roster_entry_id=detail.roster_entry_id,
                    display_name=detail.display_name,
                    score=detail.grading.score,
                    max_score=detail.grading.max_score,
                    needs_review=detail.grading.needs_review,
                    suspicion_score=detail.verdict.suspicion_score,
                    confidence=detail.verdict.confidence,
                    event_count=detail.event_count,
                )
            )
        overviews.sort(key=lambda o: o.suspicion_score, reverse=True)
        log.info("admin results_for_test test=%s -> %d attempt(s)", test_id, len(overviews))
        return overviews


def build_admin_service(
    session: Session,
    storage: StorageBackend,
    *,
    llm_grader: LLMGrader | None = None,
    analysis_llm: AnalysisLLM | None = None,
) -> AdminService:
    """Construct an `AdminService` and all the engines it composes from a session.

    Kept as a factory so the API layer wires one line and tests can inject mock
    LLM seams (a writing grader / an analyst) for the grading + advisory paths.
    """
    content_repo = ContentRepository(session)
    attempt_repo = AttemptRepository(session)
    event_repo = EventRepository(session)
    integrity = IntegrityService(event_repo)
    return AdminService(
        content=ContentService(content_repo, storage),
        roster=RosterService(attempt_repo),
        grading=GradingService(content_repo, attempt_repo, llm_grader=llm_grader),
        integrity=integrity,
        analysis=AnalysisService(event_repo, integrity, llm=analysis_llm),
        attempts=attempt_repo,
        events=event_repo,
    )
