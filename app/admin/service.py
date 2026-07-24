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

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.admin.models import (
    AttemptOverview,
    AttemptResult,
    DeviceCapture,
    ItemResponse,
    ResponseChange,
    ReviewDraft,
    RosterStatus,
    TimelineEntry,
)
from app.analysis import AnalysisService
from app.analysis.llm import AnalysisLLM
from app.content import (
    AttemptLayout,
    ContentService,
    RosterService,
    SectionPool,
    TestBank,
    build_attempt_layout,
)
from app.content.storage import StorageBackend
from app.core.logging import get_logger
from app.feedback import FeedbackService
from app.feedback.llm import FeedbackLLM
from app.grading.llm import LLMGrader
from app.grading.service import GradingError, GradingService
from app.integrity import IntegrityService
from app.persistence.repository import (
    AttemptRepository,
    ContentRepository,
    EventRepository,
)
from app.results import ResultsService
from contracts import (
    GapFillItem,
    Item,
    ManualGrade,
    MatchingItem,
    MatchingPoolStimulus,
    OpenWritingItem,
    RosterEntry,
    SingleChoiceItem,
    Test,
)

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Per-question review rendering (teacher surface — the key is allowed here).
# --------------------------------------------------------------------------- #
def _render_option(item: SingleChoiceItem, key: str) -> str:
    for opt in item.options:
        if opt.key == key:
            return opt.text if opt.kind == "text" else f"[image {opt.asset_id}]"
    return key


def _render_correct(item: Item, pool_by_key: dict[str, str]) -> str | None:
    if isinstance(item, SingleChoiceItem):
        return f"{item.correct} — {_render_option(item, item.correct)}"
    if isinstance(item, GapFillItem):
        return " / ".join([*item.accepted, *item.accepted_variants])
    if isinstance(item, MatchingItem):
        return f"{item.correct} — {pool_by_key.get(item.correct, '')}"
    return None  # open_writing: no key (LLM/manual)


def _render_student(item: Item, resp: str | None, pool_by_key: dict[str, str]) -> str | None:
    if resp is None or resp == "":
        return None
    if isinstance(item, SingleChoiceItem):
        return f"{resp} — {_render_option(item, resp)}"
    if isinstance(item, MatchingItem):
        return f"{resp} — {pool_by_key.get(resp, '')}"
    return resp  # gap_fill / open_writing: stored response is the text itself


def _decode_change(
    item: Item, layout: AttemptLayout, raw: object, pool_by_key: dict[str, str]
) -> str:
    """Render one `answer_change` value as the teacher should read it.

    `single_choice` telemetry carries the *displayed* index, so de-shuffle it back
    to the canonical key first; other item types already report canonical text/key.
    """
    if isinstance(item, SingleChoiceItem):
        try:
            key = layout.to_canonical(item.id, int(raw))  # type: ignore[arg-type]
            return f"{key} — {_render_option(item, key)}"
        except (ValueError, TypeError, IndexError, KeyError):
            return str(raw)
    if isinstance(item, MatchingItem):
        return f"{raw} — {pool_by_key.get(str(raw), '')}"
    return str(raw)


@dataclass
class _ReviewContext:
    """Shared lookups for building the per-question review and the timeline."""

    test: Test
    layout: AttemptLayout
    item_no: dict[str, str]  # item_id -> printed question number
    item_by_id: dict[str, tuple]  # item_id -> (section, item)
    pool_by_item: dict[str, dict]  # item_id -> {pool key: text}


# Events that signal where attention went (highlighted in the human timeline).
_INTEGRITY_EVENT_TYPES = frozenset(
    {
        "visibility_hidden",
        "visibility_visible",
        "window_blur",
        "window_focus",
        "pagehide",
        "device_info",
    }
)


def _fmt_pos(seconds) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "?"
    return f"{total // 60}:{total % 60:02d}"


def _timeline_label(ev, no: str | None, item, pool: dict, layout: AttemptLayout) -> str:
    """Render one captured event as a plain sentence for a human reviewer."""
    p = ev.payload or {}
    q = f"Q{no}" if no else (ev.item_id or "")
    t = ev.type
    if t == "device_info":
        bits = [b for b in (p.get("platform"), p.get("user_agent")) if b]
        ip = f" · IP {p['server_ip']}" if p.get("server_ip") else ""
        return "Device: " + (" · ".join(bits) or "unknown") + ip
    if t == "visibility_hidden":
        return "⚠︎ Left the exam tab"
    if t == "visibility_visible":
        if ev.duration_ms:
            return f"Returned to the tab (away {round(ev.duration_ms / 1000)}s)"
        return "Returned to the tab"
    if t == "window_blur":
        return "Window lost focus"
    if t == "window_focus":
        return "Window regained focus"
    if t == "pagehide":
        return "Closed / navigated away"
    if t == "interaction":
        if p.get("blocked"):
            return f"{q}: blocked {p['blocked']} (copy/paste attempt)"
        return f"{q}: interacted"
    if t == "answer_change":
        value = _decode_change(item, layout, p.get("value"), pool) if item else p.get("value")
        return f'{q}: answered "{value}"'
    if t == "audio_play":
        return f"Audio ▶ at {_fmt_pos(p.get('position'))}"
    if t == "audio_seek":
        return f"Audio seek → {_fmt_pos(p.get('position'))}"
    return t


def _build_devices(events) -> list[DeviceCapture]:
    """Parse `device_info` captures into the teacher-facing device list."""
    out: list[DeviceCapture] = []
    for ev in events:
        if ev.type != "device_info":
            continue
        p = ev.payload or {}
        screen = p.get("screen") or {}
        viewport = p.get("viewport") or {}
        out.append(
            DeviceCapture(
                at=ev.server_ts or ev.client_ts,
                user_agent=p.get("user_agent"),
                platform=p.get("platform"),
                timezone=p.get("timezone"),
                language=p.get("language"),
                screen=f"{screen.get('width')}x{screen.get('height')}" if screen else None,
                viewport=f"{viewport.get('width')}x{viewport.get('height')}" if viewport else None,
                server_ip=p.get("server_ip"),
            )
        )
    return out


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
        results: ResultsService,
        attempts: AttemptRepository,
        events: EventRepository,
    ) -> None:
        self.content = content
        self.roster = roster
        # Read-path grader: LLM-FREE on purpose. The dashboard never re-runs the
        # writing LLM on load — it reads the frozen `AttemptReport` written once at
        # submit; this grader only fills the deterministic fallback for an attempt
        # not yet finalized (e.g. a pre-feature submit awaiting a manual re-grade).
        self.grading = grading
        self.integrity = integrity
        self.analysis = analysis
        # LLM-enabled finalize composition (writing grader + feedback analyst), used
        # ONLY for an explicit teacher re-grade — never on a read.
        self.results = results
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

    def reset_attempt(self, roster_entry_id: str) -> RosterStatus:
        """Discard a student's attempt so they can sit the test again.

        Wipes the attempt + its answers/events and clears the roster back-pointer,
        returning the entry to `not_started`. Used for a genuine retake (e.g. a
        technical failure) — it is a destructive teacher action, logged loudly.
        """
        entry = self.attempts.get_roster_entry(roster_entry_id)
        if entry is None:
            raise AdminError(f"roster entry {roster_entry_id!r} not found")
        if entry.attempt_id:
            self.attempts.delete_attempt(entry.attempt_id)
        entry.attempt_id = None
        entry.status = "not_started"
        self.attempts.update_roster_entry(entry)
        log.info("admin reset_attempt entry=%s (student may retake)", roster_entry_id)
        return RosterStatus(
            roster_entry_id=entry.id,
            display_name=entry.display_name,
            status=entry.status,
            attempt_id=None,
        )

    def reopen_attempt(self, roster_entry_id: str) -> RosterStatus:
        """Let a student back in WITHOUT losing progress (non-destructive).

        Flips a submitted attempt back to `in_progress`, keeping every answer and
        the **original deadline** — so the clock keeps running (no extra time).
        The student re-picks their name and resumes the same attempt. Used when a
        student submitted early / got locked out but should keep going.
        """
        entry = self.attempts.get_roster_entry(roster_entry_id)
        if entry is None:
            raise AdminError(f"roster entry {roster_entry_id!r} not found")
        if entry.attempt_id is None:
            raise AdminError(f"roster entry {roster_entry_id!r} has no attempt to reopen")
        attempt = self.attempts.get_attempt(entry.attempt_id)
        if attempt is None:
            raise AdminError(f"attempt {entry.attempt_id!r} not found")
        # Keep answers + deadline; only reopen so the student can resume.
        self.attempts.update_attempt(
            attempt.model_copy(update={"status": "in_progress", "submitted_at": None})
        )
        entry.status = "in_progress"
        self.attempts.update_roster_entry(entry)
        log.info("admin reopen_attempt entry=%s (progress kept, clock running)", roster_entry_id)
        return RosterStatus(
            roster_entry_id=entry.id,
            display_name=entry.display_name,
            status=entry.status,
            attempt_id=entry.attempt_id,
        )

    def remove_student(self, roster_entry_id: str) -> None:
        """Remove a roster entry (and its attempt, if any) from a test."""
        if not self.attempts.delete_roster_entry(roster_entry_id):
            raise AdminError(f"roster entry {roster_entry_id!r} not found")
        log.info("admin remove_student entry=%s", roster_entry_id)

    # -- manual grading (teacher marks writing / overrides a gap ✓/✗) -------- #
    def _apply_manual_grades(self, attempt_id, grading) -> tuple[object, set[str]]:
        """Overlay the teacher's stored marks onto an auto `GradingResult`.

        Each manual mark is applied via the pure `GradingService.apply_override`
        (grading stays auto-only per its boundary), recomputing the totals. A stale
        mark (item removed from the test) is skipped. Returns the merged result and
        the set of item ids a human graded."""
        manual = self.attempts.list_manual_grades(attempt_id)
        ids: set[str] = set()
        for mg in manual:
            try:
                grading = self.grading.apply_override(grading, mg.item_id, awarded=mg.awarded)
                ids.add(mg.item_id)
            except GradingError:
                log.warning("stale manual grade attempt=%s item=%s skipped", attempt_id, mg.item_id)
        return grading, ids

    def set_manual_grade(self, attempt_id: str, item_id: str, awarded: float) -> AttemptResult:
        """Persist a teacher's mark for one item, then return the merged detail.

        Validates the score against the item's `max_points` (5 for writing, 1 for
        an objective gap ✓/✗). This is the human teacher's judgement — independent
        of any integrity signal (golden rule #2)."""
        attempt = self.attempts.get_attempt(attempt_id)
        if attempt is None:
            raise AdminError(f"attempt {attempt_id!r} not found")
        grading = self.grading.grade(attempt_id)
        target = next((g for g in grading.items if g.item_id == item_id), None)
        if target is None:
            raise AdminError(f"item {item_id!r} not part of attempt {attempt_id!r}")
        if not 0.0 <= awarded <= target.max_points:
            raise AdminError(
                f"awarded {awarded} out of range [0, {target.max_points}] for {item_id!r}"
            )
        self.attempts.upsert_manual_grade(
            ManualGrade(
                attempt_id=attempt_id,
                item_id=item_id,
                awarded=awarded,
                graded_at=datetime.now(UTC),
            )
        )
        log.info(
            "admin set_manual_grade attempt=%s item=%s -> %.2f/%.2f",
            attempt_id,
            item_id,
            awarded,
            target.max_points,
        )
        return self.attempt_result(attempt_id)

    # -- results (score + advisory verdict + replay) ------------------------ #
    def _base_grading(self, attempt_id: str):
        """The auto-grade for a read, plus the per-skill report and finalize status.

        Reads the frozen `AttemptReport` written once at submit (no LLM on load). If
        the attempt isn't finalized yet, falls back to a DETERMINISTIC-only grade
        (objective items scored, writing -> needs-review) and a "pending" status, so
        the page still renders and the teacher can trigger a re-grade.
        """
        report = self.attempts.get_report(attempt_id)
        if report is not None:
            return report.grading, report.skills, report.summary, report.status
        return self.grading.grade(attempt_id), [], "", "pending"

    def attempt_result(self, attempt_id: str) -> AttemptResult:
        """Full results detail for one attempt (rule #6: replay next to verdict).

        Grading and integrity/analysis are computed on independent paths and only
        bundled here (rule #2) — neither influences the other. The auto-grade is the
        frozen one from submit; manual marks overlay on top.
        """
        attempt = self.attempts.get_attempt(attempt_id)
        if attempt is None:
            raise AdminError(f"attempt {attempt_id!r} not found")

        base, skills, summary, report_status = self._base_grading(attempt_id)
        grading, manual_ids = self._apply_manual_grades(attempt_id, base)
        profile = self.integrity.profile(attempt_id)
        verdict = self.analysis.analyze(attempt_id)
        events = self.events.list_events(attempt_id)
        ctx = self._review_context(attempt)
        responses = self._build_responses(ctx, grading, events, manual_ids) if ctx else []
        timeline = self._build_timeline(ctx, events) if ctx else []
        devices = _build_devices(events)

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
            responses=responses,
            devices=devices,
            timeline=timeline,
            profile=profile,
            verdict=verdict,
            event_count=len(events),
            events=events,
            skills=skills,
            summary=summary,
            report_status=report_status,
        )

    def regrade(self, attempt_id: str) -> AttemptResult:
        """Re-run the one-shot finalize (grade + skill report) for one attempt.

        The explicit teacher action behind the dashboard's 'Re-grade' button: used
        for an attempt submitted before this feature existed, or to refresh a report
        whose feedback LLM previously failed (`status="failed"`). This is the ONLY
        admin path that runs the writing/feedback LLM; reads never do.
        """
        if self.attempts.get_attempt(attempt_id) is None:
            raise AdminError(f"attempt {attempt_id!r} not found")
        self.results.finalize(attempt_id)
        log.info("admin regrade attempt=%s (re-finalized)", attempt_id)
        return self.attempt_result(attempt_id)

    def _review_context(self, attempt) -> _ReviewContext | None:
        """Authoring test + per-attempt option layout + question lookups.

        Built once per `attempt_result` and shared by the per-question review and
        the human timeline. The layout de-shuffles `single_choice` exactly as
        delivery's default builder did (mirrors `app/delivery/service.py:_default_bank`),
        so a displayed index maps back to the option the student actually saw.
        """
        test = self.content.get_test(attempt.test_id)
        if test is None:  # pragma: no cover - attempt implies a test
            return None
        bank = TestBank(
            test_id=test.id,
            pools=[SectionPool(key=s.id, sections=[s], pick=1) for s in test.sections],
        )
        layout = build_attempt_layout(bank, attempt.seed)

        item_no: dict[str, str] = {}
        item_by_id: dict[str, tuple] = {}
        pool_by_item: dict[str, dict] = {}
        running = 0
        for section in test.sections:
            pool = (
                {p.key: p.text for p in section.stimulus.options}
                if isinstance(section.stimulus, MatchingPoolStimulus)
                else {}
            )
            for item in section.items:
                running += 1
                m = re.search(r"q(\d+)$", item.id)
                item_no[item.id] = m.group(1) if m else str(running)
                item_by_id[item.id] = (section, item)
                pool_by_item[item.id] = pool
        return _ReviewContext(test, layout, item_no, item_by_id, pool_by_item)

    def _build_responses(
        self, ctx: _ReviewContext, grading, events, manual_ids: set[str]
    ) -> list[ItemResponse]:
        """Join the authoring key, the stored answers, grades and the replay into
        one per-question review (answer vs key, ✓/✗, full writing, change history).

        Read-only and key-bearing on purpose: this is the teacher surface, so it
        carries `correct` (golden rule #1 forbids that only on the student runner),
        and no integrity signal is consulted (golden rule #2)."""
        answers = {a.item_id: a.response for a in self.attempts.get_answers(grading.attempt_id)}
        grades = {g.item_id: g for g in grading.items}
        history: dict[str, list] = {}
        for ev in events:
            if ev.type == "answer_change" and ev.item_id:
                history.setdefault(ev.item_id, []).append(ev)

        rows: list[ItemResponse] = []
        for section in ctx.test.sections:
            for item in section.items:
                pool_by_key = ctx.pool_by_item[item.id]
                grade = grades.get(item.id)
                resp = answers.get(item.id)
                is_writing = isinstance(item, OpenWritingItem)
                is_manual = item.id in manual_ids
                rows.append(
                    ItemResponse(
                        item_id=item.id,
                        section_title=section.title,
                        skill=section.skill,
                        item_type=item.item_type,
                        prompt=item.prompt,
                        bullet_points=item.bullet_points if is_writing else [],
                        word_min=item.word_min if is_writing else None,
                        student_answer=_render_student(item, resp, pool_by_key),
                        writing_text=resp if is_writing else None,
                        correct_answer=_render_correct(item, pool_by_key),
                        is_correct=(
                            # Objective items are ✓/✗ from points (gap overrides move
                            # them). Writing is *scored* (0..5), never pass/fail, so it
                            # has no ✓/✗ — its mark shows in the writing section.
                            None
                            if is_writing or grade is None
                            else grade.awarded >= grade.max_points
                        ),
                        awarded=grade.awarded if grade else 0.0,
                        max_points=grade.max_points if grade else 1.0,
                        needs_review=grade.needs_review if grade else False,
                        manual=is_manual,
                        history=[
                            ResponseChange(
                                at=ev.server_ts or ev.client_ts,
                                value=_decode_change(
                                    item, ctx.layout, ev.payload.get("value"), pool_by_key
                                ),
                            )
                            for ev in history.get(item.id, [])
                        ],
                    )
                )
        return rows

    def _build_timeline(self, ctx: _ReviewContext, events) -> list[TimelineEntry]:
        """Decode the raw replay into a human, chronological 'what they did' list."""
        ordered = sorted(events, key=lambda e: (e.server_ts or e.client_ts))
        out: list[TimelineEntry] = []
        for ev in ordered:
            item = ctx.item_by_id.get(ev.item_id, (None, None))[1] if ev.item_id else None
            pool = ctx.pool_by_item.get(ev.item_id, {}) if ev.item_id else {}
            no = ctx.item_no.get(ev.item_id) if ev.item_id else None
            out.append(
                TimelineEntry(
                    at=ev.server_ts or ev.client_ts,
                    type=ev.type,
                    item_id=ev.item_id,
                    item_no=no,
                    label=_timeline_label(ev, no, item, pool, ctx.layout),
                    integrity=ev.type in _INTEGRITY_EVENT_TYPES,
                )
            )
        return out

    def results_for_test(self, test_id: str) -> list[AttemptOverview]:
        """All started attempts for a test, ranked most-suspicious first.

        Cheap on purpose (this is polled live every few seconds): the score comes
        from the frozen per-attempt reports read in ONE query — it never re-runs the
        writing LLM or builds the full replay/per-question breakdown (that heavy work
        happens once at submit). The advisory suspicion is still computed (it's
        deterministic feature extraction over the event stream, no external call), so
        ranking keeps working; it never changes a score (rule #2).
        """
        self.get_test(test_id)
        reports = self.attempts.get_reports_for_test(test_id)
        overviews: list[AttemptOverview] = []
        for entry in self.roster.list_students(test_id):
            if entry.attempt_id is None:
                continue
            report = reports.get(entry.attempt_id)
            base = report.grading if report is not None else self.grading.grade(entry.attempt_id)
            status = report.status if report is not None else "pending"
            grading, _ = self._apply_manual_grades(entry.attempt_id, base)
            verdict = self.analysis.analyze(entry.attempt_id)
            overviews.append(
                AttemptOverview(
                    attempt_id=entry.attempt_id,
                    roster_entry_id=entry.id,
                    display_name=entry.display_name,
                    score=grading.score,
                    max_score=grading.max_score,
                    needs_review=grading.needs_review,
                    suspicion_score=verdict.suspicion_score,
                    confidence=verdict.confidence,
                    event_count=self.attempts.count_events(entry.attempt_id),
                    report_status=status,
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
    feedback_llm: FeedbackLLM | None = None,
) -> AdminService:
    """Construct an `AdminService` and all the engines it composes from a session.

    Kept as a factory so the API layer wires one line and tests can inject mock
    LLM seams (a writing grader / an analyst / a feedback analyst).

    Two grading paths on purpose: the dashboard's read grader is LLM-FREE (it only
    fills the deterministic fallback for an un-finalized attempt — reads never call
    the writing LLM), while the `ResultsService` behind `regrade` carries the
    LLM-enabled writing grader + feedback analyst for the one-shot finalize.
    """
    content_repo = ContentRepository(session)
    attempt_repo = AttemptRepository(session)
    event_repo = EventRepository(session)
    integrity = IntegrityService(event_repo)
    results = ResultsService(
        content=content_repo,
        attempts=attempt_repo,
        grading=GradingService(content_repo, attempt_repo, llm_grader=llm_grader),
        feedback=FeedbackService(llm=feedback_llm),
    )
    return AdminService(
        content=ContentService(content_repo, storage),
        roster=RosterService(attempt_repo),
        grading=GradingService(content_repo, attempt_repo, llm_grader=None),
        integrity=integrity,
        analysis=AnalysisService(event_repo, integrity, llm=analysis_llm),
        results=results,
        attempts=attempt_repo,
        events=event_repo,
    )
