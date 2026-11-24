"""Delivery engine: attempt lifecycle (Phase 4).

The exam runtime. Responsibilities (docs/ARCHITECTURE.md):

* **start** — validate the exam window (+ grace), pick the roster entry, and
  **create-or-resume** the single attempt for that entry. Refresh-safe: a roster
  entry never gets a second attempt; reopening the link resumes the existing one.
* **serve** items one-at-a-time, **stripped of `correct`** (golden rule #1) via
  the `projection` module + the per-attempt `AttemptLayout` from `app/content`.
* **save answer** — map the student's *displayed* option index back to the
  canonical key (via `OptionShuffle`) before persisting.
* **timer** — server-authoritative and it does **not** pause (golden rule #3).
  The deadline is `min(started_at + duration, window close)`, fixed at start.
* **submit** — finalize; a late submit (past the deadline) is rejected.
* **access (Phase 14)** — `authenticate` checks the roster entry's classroom PIN
  through the in-memory `PinRateLimiter` (app/delivery/access.py); `open_session`
  issues the per-attempt session token (stored hashed) and `check_session`
  verifies it on every later attempt call.
* **expire** — a crossed deadline flips `in_progress` -> `expired`: lazily on the
  next state/save/submit, or eagerly via `expire_overdue` (recovery sweep) for an
  attempt whose student closed the tab and never came back.

The per-attempt layout is *recomputed* from `Attempt.seed` on every call, never
stored, so a resumed attempt reproduces the identical draw (rule #7). The seed is
`derive_seed(test_id, roster_entry_id)` — stable across resumes.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from app.content import AttemptLayout, SectionPool, TestBank, build_attempt_layout, derive_seed
from app.content.roster import is_well_formed_pin, normalize_pin, pin_matches
from app.core.config import settings
from app.core.logging import get_logger
from app.delivery.access import PinRateLimiter, new_session_token
from app.delivery.audio import track_asset_ids
from app.delivery.projection import project_section, project_study, project_test
from app.delivery.sections import (
    SectionPlan,
    begun,
    overall_seconds,
    phase_of,
    plan_sections,
    remaining_seconds,
    resolve,
)
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import (
    Answer,
    Attempt,
    AttemptSection,
    ClientItem,
    ClientSection,
    ClientStudyStimulus,
    ClientTest,
    DrawingItem,
    Item,
    RosterEntry,
    Section,
    SingleChoiceItem,
    Test,
)
from contracts.content import DRAWING_MAX_RESPONSE_CHARS
from contracts.papers import is_partial, papers_label, served_papers, served_test
from contracts.runtime import SectionCloseReason, SectionPhase, SectionStartedBy

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Errors (all distinguishable so callers/tests can assert the failure mode).
# --------------------------------------------------------------------------- #
class DeliveryError(Exception):
    """Base for every delivery-layer rejection."""


class WindowNotOpenError(DeliveryError):
    """Tried to start before the window opens (`opens_at` = when it will)."""

    def __init__(self, message: str, *, opens_at: datetime | None = None) -> None:
        super().__init__(message)
        self.opens_at = opens_at


class WindowClosedError(DeliveryError):
    """Tried to start after the window (+ grace) closed (`closed_at` = hard close)."""

    def __init__(self, message: str, *, closed_at: datetime | None = None) -> None:
        super().__init__(message)
        self.closed_at = closed_at


class AttemptExpiredError(DeliveryError):
    """Acted on (saved/submitted) an attempt past its deadline."""


class AttemptStateError(DeliveryError):
    """Acted on an attempt in a state that forbids it (e.g. already submitted)."""


class NotFoundError(DeliveryError):
    """Referenced a roster entry / attempt / test / item that does not exist."""


class PinRequiredError(DeliveryError):
    """No PIN, or not PIN-shaped (400). Not counted as a wrong guess."""


class WrongPinError(DeliveryError):
    """The PIN does not match the roster entry (403); `tries_left` before a lock."""

    def __init__(self, message: str, *, tries_left: int) -> None:
        super().__init__(message)
        self.tries_left = tries_left


class PinLockedError(DeliveryError):
    """Too many wrong PINs for this entry / from this IP (429 + Retry-After)."""

    def __init__(self, message: str, *, retry_after: int, scope: str) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.scope = scope


class SessionTokenError(DeliveryError):
    """Missing / wrong per-attempt session token (401)."""


class AnswerTooLargeError(DeliveryError):
    """A saved answer longer than its item type allows (a drawing: the grader's
    `DRAWING_MAX_RESPONSE_CHARS`; anything else: `settings.answer_max_chars`).
    Nothing is saved; HTTP 413 + `X-Answer-Error: answer_too_large`."""

    code = "answer_too_large"

    def __init__(self, message: str, *, size: int, limit: int) -> None:
        super().__init__(message)
        self.size = size
        self.limit = limit


class UnknownTrackError(DeliveryError):
    """Audio progress for an asset that is not a listening track of the test (400)."""


class SectionStateError(DeliveryError):
    """A timed-section action refused by the section clock (409 + `code`).

    Codes: `not_timed` (the attempt has no section clocks), `section_not_current`
    (a later section while an earlier one is still open), `section_not_begun`
    (still on its intro screen), `section_study` (recall items during the study
    window), `section_closed` (finished early), `section_expired` (its time ran
    out), `study_not_open` (the study stimulus outside its window)."""

    def __init__(self, message: str, *, code: str, section_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.section_id = section_id


# --------------------------------------------------------------------------- #
# Value objects.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ExamWindow:
    """When a test's shareable link is live.

    `grace_seconds` extends the close: a student may *start* within
    `[opens_at, closes_at + grace]`, and that same grace-extended close caps the
    per-attempt deadline. Grace exists so a late arrival (or clock skew near the
    end) is not cut off the instant the nominal close passes.
    """

    opens_at: datetime
    closes_at: datetime
    grace_seconds: int = 0

    @property
    def hard_close(self) -> datetime:
        return self.closes_at + timedelta(seconds=self.grace_seconds)

    @classmethod
    def for_test(cls, test: Test) -> ExamWindow:
        """The teacher's schedule on the test (`Test.opens_at` / `closes_at` /
        `late_start_grace_minutes`); an unset bound is unbounded (`_FAR_PAST` /
        `_FAR_FUTURE`), so a test without a schedule is always open and its deadline
        is driven by `duration_minutes` alone."""
        return cls(
            opens_at=test.opens_at or _FAR_PAST,
            closes_at=test.closes_at or _FAR_FUTURE,
            grace_seconds=test.late_start_grace_minutes * 60,
        )

    def state(self, now: datetime) -> WindowState:
        """`not_yet_open` before `opens_at`, `closed` after `hard_close`, else `open`
        — exactly the check `DeliveryService.start` applies to a NEW attempt."""
        if now < self.opens_at:
            return "not_yet_open"
        if now > self.hard_close:
            return "closed"
        return "open"


WindowState = Literal["open", "not_yet_open", "closed"]
# Unbounded ends of a window without a schedule (timezone-aware, comparable).
_FAR_PAST = datetime(1970, 1, 1, tzinfo=UTC)
_FAR_FUTURE = datetime(9999, 1, 1, tzinfo=UTC)
ALWAYS_OPEN = ExamWindow(opens_at=_FAR_PAST, closes_at=_FAR_FUTURE)


@dataclass(frozen=True)
class AttemptState:
    """Server-authoritative snapshot of where an attempt stands right now."""

    attempt_id: str
    status: str
    started_at: datetime | None
    deadline: datetime | None
    remaining_seconds: int
    expired: bool
    # LEGACY single-track position (only an old runner writes it; see
    # contracts.runtime.AudioTrackProgress) + the per-track map {asset_id: seconds}.
    audio_progress_seconds: int = 0
    audio_progress: dict[str, int] = field(default_factory=dict)
    # True only on the call that flipped the attempt `in_progress` -> `expired`, so
    # the HTTP layer schedules the finalize once per transition, not per poll.
    just_expired: bool = False


class SectionStatus(BaseModel):
    """One timed section's server clock as the runner sees it (no content)."""

    section_id: str
    position: int
    phase: SectionPhase
    time_limit_seconds: int
    study_seconds: int = 0
    intro_started_at: datetime | None = None
    intro_ends_at: datetime | None = Field(
        default=None, description="When the intro begins the section by itself (cap)."
    )
    started_at: datetime | None = None
    started_by: SectionStartedBy | None = None
    study_ends_at: datetime | None = None
    deadline: datetime | None = None
    closed_at: datetime | None = None
    close_reason: SectionCloseReason | None = None
    remaining_seconds: int = Field(
        default=0, description="Seconds left in the CURRENT phase (intro / study / answering)."
    )


class SectionsState(BaseModel):
    """Every timed section of an attempt + which one is current (server time)."""

    attempt_id: str
    timed: bool
    attempt_status: str
    server_now: datetime
    intro_max_seconds: int
    current_section_id: str | None = None
    sections: list[SectionStatus] = Field(default_factory=list)


class SectionBegin(BaseModel):
    """The begin response: the section's clock + the study stimulus while its study
    window is open (never together with the recall items)."""

    section: SectionStatus
    study: ClientStudyStimulus | None = None


def _lock_message(retry_after: int, scope: str = "entry") -> str:
    minutes = max(1, (retry_after + 59) // 60)
    where = " from this network" if scope == "ip" else ""
    return (
        f"Too many wrong PINs{where}. Wait {minutes} minute(s) and try again, "
        "or ask your teacher for help."
    )


def _default_bank(test: Test) -> TestBank:
    """One singleton pool per section: deliver the whole authored test in order.

    A single authored `Test` *is* the test; there are no interchangeable
    alternatives to subset, so each section is its own always-included pool. The
    layout still drives the `single_choice` option shuffle. Callers with a real
    multi-section bank (alternative passages) can inject their own builder to get
    section-level divergence.
    """
    return TestBank(
        test_id=test.id,
        pools=[SectionPool(key=s.id, sections=[s], pick=1) for s in test.sections],
    )


# --------------------------------------------------------------------------- #
# Service.
# --------------------------------------------------------------------------- #
class DeliveryService:
    """Drive the attempt lifecycle on top of the content + attempt repositories."""

    def __init__(
        self,
        content_repo: ContentRepository,
        attempt_repo: AttemptRepository,
        *,
        bank_builder: Callable[[Test], TestBank] = _default_bank,
        intro_cap_seconds: int | None = None,
    ) -> None:
        self.content = content_repo
        self.attempts = attempt_repo
        self._bank_builder = bank_builder
        # Timed sections: the longest an intro screen may stay open (then the
        # section begins by itself) — also the per-section allowance in the overall
        # deadline. `settings.section_intro_max_seconds` unless given (tests).
        self.intro_cap = (
            intro_cap_seconds
            if intro_cap_seconds is not None
            else settings.section_intro_max_seconds
        )

    # -- start (create-or-resume, refresh-safe) ---------------------------- #
    def start(
        self,
        roster_entry_id: str,
        window: ExamWindow | None = None,
        *,
        now: datetime | None = None,
    ) -> Attempt:
        """Create the attempt for a roster entry, or resume the existing one.

        Window/grace is validated only when creating; a resume reopens whatever
        attempt already exists (its deadline was fixed at first start), even after
        the window closed. `window=None` uses the test's own schedule
        (`ExamWindow.for_test`) — what the HTTP layer does.
        """
        now = now or datetime.now(UTC)
        entry = self.attempts.get_roster_entry(roster_entry_id)
        if entry is None:
            raise NotFoundError(f"roster entry {roster_entry_id!r} not found")

        if entry.attempt_id is not None:
            return self._resume(roster_entry_id, entry.attempt_id)

        test = self._require_test(entry.test_id)
        if window is None:
            window = ExamWindow.for_test(test)
        if now < window.opens_at:
            log.warning(
                "attempt start REFUSED entry=%s test=%s: window opens at %s",
                roster_entry_id,
                entry.test_id,
                window.opens_at.isoformat(),
            )
            raise WindowNotOpenError(
                f"window opens at {window.opens_at.isoformat()}", opens_at=window.opens_at
            )
        if now > window.hard_close:
            log.warning(
                "attempt start REFUSED entry=%s test=%s: window closed at %s",
                roster_entry_id,
                entry.test_id,
                window.hard_close.isoformat(),
            )
            raise WindowClosedError(
                f"window closed at {window.hard_close.isoformat()}",
                closed_at=window.hard_close,
            )

        seed = derive_seed(entry.test_id, entry.id)
        attempt_id = str(uuid.uuid4())
        # Claim the roster entry FIRST (conditional UPDATE: no attempt yet), THEN read
        # the assigned papers back. The teacher's papers change is conditional on the
        # same "no attempt" state, so it either landed before the claim (and is read
        # here) or fails with 409 — never silently lost in between.
        if not self.attempts.claim_roster_entry(entry.id, attempt_id):
            winner = self.attempts.get_roster_entry(roster_entry_id, fresh=True)
            if winner is not None and winner.attempt_id is not None:
                log.info("attempt start claim lost entry=%s -> resume", roster_entry_id)
                return self._resume(roster_entry_id, winner.attempt_id)
            raise NotFoundError(f"roster entry {roster_entry_id!r} not found")
        claimed = self.attempts.get_roster_entry(roster_entry_id, fresh=True)
        assigned = claimed.papers if claimed is not None else entry.papers
        # The teacher's assignment (whole test, or some papers): snapshot what is
        # served on the attempt. Lenient: a paper no longer in the (edited) test is
        # dropped, and nothing left = the whole test.
        papers = self._papers_for(test, assigned)
        served = served_test(test, papers)
        # Build the layout now so the pooling decisions are logged at start.
        layout = self._layout_for(served, seed)
        # Whole test: duration_minutes; partial: the sum of the served papers'
        # minutes (`contracts.papers`) — still capped by the window (+ grace).
        # Timed sections (every served section has its own clock): the sum of the
        # sections' intro allowance + study + limit instead (Phase 17.2).
        plans = plan_sections(self._in_layout_order(served, layout))
        if plans is not None:
            seconds = overall_seconds(plans, self.intro_cap)
            deadline = min(now + timedelta(seconds=seconds), window.hard_close)
            log.info(
                "attempt start entry=%s timed sections=%d intro_cap=%ds overall=%ds",
                roster_entry_id,
                len(plans),
                self.intro_cap,
                seconds,
            )
        else:
            if any(s.time_limit_seconds is not None for s in served.sections):
                log.warning(
                    "attempt start entry=%s test=%s: only some sections have a time "
                    "limit -> section clocks ignored, one deadline for the test",
                    roster_entry_id,
                    test.id,
                )
            deadline = min(
                now + timedelta(minutes=served.duration_minutes), window.hard_close
            )
        attempt = Attempt(
            id=attempt_id,
            test_id=entry.test_id,
            roster_entry_id=entry.id,
            status="in_progress",
            seed=seed,
            started_at=now,
            deadline=deadline,
            papers=papers,  # type: ignore[arg-type]
        )
        try:
            self.attempts.add_attempt(attempt)
        except IntegrityError:
            # Lost a concurrent double-start race: the unique constraint on
            # attempts.roster_entry_id rejected the second insert. Roll back and
            # resume the attempt the winner created — never fork a second one.
            self.attempts.session.rollback()
            winner = self.attempts.get_roster_entry(roster_entry_id)
            if winner is not None and winner.attempt_id is not None:
                log.info("attempt start race lost entry=%s -> resume", roster_entry_id)
                return self._resume(roster_entry_id, winner.attempt_id)
            raise
        log.info(
            "attempt start entry=%s attempt=%s seed=%d papers=%s minutes=%d "
            "sections=%d deadline=%s",
            roster_entry_id,
            attempt.id,
            seed,
            papers_label(papers) or "all",
            served.duration_minutes,
            len(served.sections),
            deadline.isoformat(),
        )
        return attempt

    @staticmethod
    def _papers_for(test: Test, assigned: list[str] | None) -> list[str] | None:
        """The papers to serve for an assignment: None = the whole test."""
        if assigned is None:
            return None
        kept = served_papers(test, assigned)  # none left -> every paper
        dropped = sorted(set(assigned) - set(kept))
        if dropped:
            log.warning(
                "attempt papers %s: %s not in test %s any more -> serving %s",
                assigned,
                dropped,
                test.id,
                kept,
            )
        return kept if is_partial(test, kept) else None

    def _served(self, attempt: Attempt) -> Test:
        """The attempt's test restricted to its served papers (whole test if None)."""
        return served_test(self._require_test(attempt.test_id), attempt.papers)

    def _resume(self, roster_entry_id: str, attempt_id: str) -> Attempt:
        existing = self.attempts.get_attempt(attempt_id)
        if existing is None:  # pragma: no cover - dangling pointer
            raise NotFoundError(f"attempt {attempt_id!r} not found")
        log.info(
            "attempt resume entry=%s attempt=%s status=%s",
            roster_entry_id,
            existing.id,
            existing.status,
        )
        return existing

    # -- access: classroom PIN + per-attempt session token (Phase 14) ------ #
    def authenticate(
        self,
        roster_entry_id: str,
        pin: str | None,
        *,
        limiter: PinRateLimiter,
        client_ip: str | None = None,
    ) -> RosterEntry:
        """Check the student's PIN for a roster entry (rate-limited); returns the entry.

        Order: unknown entry -> 404; empty / malformed PIN -> `PinRequiredError`
        (not counted); locked entry or throttled IP -> `PinLockedError` (the PIN is not even
        compared); wrong PIN -> `WrongPinError` (counted, WARNING; the failure that
        locks the entry or throttles the IP raises `PinLockedError`; the limiter
        logs each lock event itself). Case-insensitive, spaces
        ignored. Never logs the PIN.
        """
        entry = self.attempts.get_roster_entry(roster_entry_id)
        if entry is None:
            raise NotFoundError(f"roster entry {roster_entry_id!r} not found")
        typed = normalize_pin(pin)
        if not typed:
            raise PinRequiredError("Enter your 4-character PIN.")
        if not is_well_formed_pin(typed):
            raise PinRequiredError(
                "A PIN has 4 letters or numbers. Check the PIN your teacher gave you."
            )
        decision = limiter.attempt(
            entry.id, client_ip, lambda: pin_matches(typed, entry.pin), test_id=entry.test_id
        )
        if decision.ok:
            log.info("pin ok entry=%s ip=%s", entry.id, client_ip)
            return entry
        if decision.just_locked:
            log.warning(
                "pin WRONG entry=%s ip=%s failures=%d -> LOCKED %s for %ds",
                entry.id,
                client_ip,
                decision.failures,
                decision.scope,
                decision.retry_after,
            )
        elif decision.retry_after:
            log.warning(
                "pin refused (locked %s) entry=%s ip=%s retry_after=%ds",
                decision.scope,
                entry.id,
                client_ip,
                decision.retry_after,
            )
        else:
            log.warning(
                "pin WRONG entry=%s ip=%s failures=%d tries_left=%d",
                entry.id,
                client_ip,
                decision.failures,
                decision.tries_left,
            )
            raise WrongPinError(
                "Wrong PIN. Check the PIN your teacher gave you and try again.",
                tries_left=decision.tries_left,
            )
        raise PinLockedError(
            _lock_message(decision.retry_after, decision.scope),
            retry_after=decision.retry_after,
            scope=decision.scope,
        )

    def open_session(self, attempt_id: str) -> str:
        """Issue a new session token for an attempt (only its hash is stored).

        Called after a successful PIN start/resume. Several sessions per attempt may
        coexist (the student re-entered the PIN on another device); a teacher reset
        deletes them with the attempt; a regenerated PIN does not revoke them.
        """
        token = new_session_token()
        self.attempts.add_attempt_session(attempt_id, token)
        log.info("session open attempt=%s", attempt_id)
        return token

    def check_session(self, attempt_id: str, token: str | None) -> None:
        """404 for an unknown attempt, `SessionTokenError` for a missing/wrong token."""
        self._require_attempt(attempt_id)
        if not self.attempts.attempt_session_valid(attempt_id, token):
            log.warning(
                "session REJECTED attempt=%s (%s token)",
                attempt_id,
                "missing" if not token else "wrong",
            )
            raise SessionTokenError(
                "This test session is not valid on this device. Enter your PIN again."
            )

    # -- serve (no `correct` ever leaves here) ----------------------------- #
    def client_test(self, attempt_id: str, *, now: datetime | None = None) -> ClientTest:
        """The full student-facing test for an attempt (drawn + shuffled, no key).

        Timed sections: the items of a section that is not yet in its answering
        phase (pending / intro / study) are withheld (`items_withheld`, `items=[]`)
        — a student cannot read (or solve) a subtest before its clock runs, nor the
        recall questions before the study stimulus is gone. The study stimulus is
        never in this payload (`project_section` emits only its length)."""
        now = now or datetime.now(UTC)
        attempt = self._require_attempt(attempt_id)
        test = self._served(attempt)
        layout = self._layout_for(test, attempt.seed)
        client = project_test(test, layout)
        timed = self._timed(attempt, test, layout)
        withheld = 0
        if timed is not None:
            rows = self._sync_sections(attempt, timed[1], now)
            sections = []
            for cs in client.sections:
                if self._phase(attempt, rows.get(cs.id), now) in ("answering", "closed"):
                    sections.append(cs)
                else:
                    withheld += 1
                    sections.append(cs.model_copy(update={"items": [], "items_withheld": True}))
            client = client.model_copy(update={"sections": sections})
        log.info(
            "serve client_test attempt=%s sections=%d withheld=%d",
            attempt_id,
            len(client.sections),
            withheld,
        )
        return client

    def serve_item(
        self, attempt_id: str, item_id: str, *, now: datetime | None = None
    ) -> ClientItem:
        """Serve a single item (one-at-a-time), stripped of its answer key. Timed
        sections: only while its section is answering (or closed)."""
        now = now or datetime.now(UTC)
        attempt = self._require_attempt(attempt_id)
        test = self._served(attempt)
        layout = self._layout_for(test, attempt.seed)
        client = project_test(test, layout)
        for section in client.sections:
            for item in section.items:
                if item.id == item_id:
                    timed = self._timed(attempt, test, layout)
                    if timed is not None:
                        rows = self._sync_sections(attempt, timed[1], now)
                        phase = self._phase(attempt, rows.get(section.id), now)
                        if phase not in ("answering", "closed"):
                            raise self._not_open(section.id, phase, rows.get(section.id))
                    log.info("serve item attempt=%s item=%s", attempt_id, item_id)
                    return item
        raise NotFoundError(f"item {item_id!r} not in attempt {attempt_id!r}")

    # -- timed sections (IQ family, Phase 17.2; rules in app/delivery/sections.py) #
    @staticmethod
    def _in_layout_order(test: Test, layout: AttemptLayout) -> Test:
        """The served test with its sections in the layout's (served) order."""
        by_id = {s.id: s for s in test.sections}
        return test.model_copy(update={"sections": [by_id[sid] for sid in layout.section_ids]})

    def _timed(
        self,
        attempt: Attempt,
        test: Test | None = None,
        layout: AttemptLayout | None = None,
    ) -> tuple[Test, list[SectionPlan]] | None:
        """(served test in order, plan) when the attempt runs timed sections, else None.
        Pass the already loaded served test / layout to skip reloading them (the hot
        save path of every test)."""
        test = test if test is not None else self._served(attempt)
        if plan_sections(test) is None:  # cheap exit for every non-timed test
            return None
        layout = layout if layout is not None else self._layout_for(test, attempt.seed)
        ordered = self._in_layout_order(test, layout)
        plans = plan_sections(ordered)
        return (ordered, plans) if plans is not None else None

    def _require_timed(self, attempt: Attempt) -> tuple[Test, list[SectionPlan]]:
        timed = self._timed(attempt)
        if timed is None:
            raise SectionStateError(
                "This test has no timed sections.", code="not_timed", section_id=None
            )
        return timed

    @staticmethod
    def _phase(attempt: Attempt, sec: AttemptSection | None, now: datetime) -> SectionPhase:
        """The section phase; once the attempt is over every section counts as closed."""
        if attempt.status != "in_progress" and sec is not None:
            return "closed"
        return phase_of(sec, now)

    def _sync_sections(
        self, attempt: Attempt, plans: list[SectionPlan], now: datetime
    ) -> dict[str, AttemptSection]:
        """Materialize every section transition the clock has reached by `now` and
        return `{section_id: AttemptSection}` (reached sections only).

        Lazy and server-authoritative (rule #3): `resolve` replays the rules, and the
        new facts are written with conditional statements (insert-if-absent, begin /
        study end / close only while still NULL), so a concurrent request can never
        double-own a transition; a lost write re-reads and replays. Logs each
        transition once at INFO. Nothing is written once the attempt is over."""
        stored = self.attempts.list_attempt_sections(attempt.id)
        if attempt.status != "in_progress" or attempt.started_at is None:
            return stored
        for _ in range(3):
            resolved = resolve(
                plans,
                stored,
                attempt_id=attempt.id,
                attempt_started_at=attempt.started_at,
                attempt_deadline=attempt.deadline,
                now=now,
                intro_cap=self.intro_cap,
            )
            if not self._write_transitions(attempt.id, stored, resolved):
                return {s.section_id: s for s in resolved}
            stored = self.attempts.list_attempt_sections(attempt.id)
        return stored  # pragma: no cover - three lost races in a row

    def _write_transitions(
        self,
        attempt_id: str,
        stored: dict[str, AttemptSection],
        resolved: list[AttemptSection],
    ) -> bool:
        """Persist what `resolve` derived beyond `stored`; True iff a write was lost."""
        for sec in resolved:
            old = stored.get(sec.section_id)
            sid = sec.section_id
            if old is None:
                if not self.attempts.insert_attempt_section(sec):
                    return True
                log.info(
                    "section intro attempt=%s section=%s pos=%d at=%s",
                    attempt_id,
                    sid,
                    sec.position,
                    sec.intro_started_at.isoformat(),
                )
                old = AttemptSection(
                    attempt_id=attempt_id,
                    section_id=sid,
                    position=sec.position,
                    intro_started_at=sec.intro_started_at,
                )
            if sec.started_at is not None and old.started_at is None:
                if not self.attempts.begin_attempt_section(sec):
                    return True
                log.info(
                    "section begin attempt=%s section=%s by=%s at=%s study_until=%s deadline=%s",
                    attempt_id,
                    sid,
                    sec.started_by,
                    sec.started_at.isoformat(),
                    sec.study_ends_at.isoformat() if sec.study_ends_at else "-",
                    sec.deadline.isoformat() if sec.deadline else "-",
                )
            if (
                sec.study_ends_at is not None
                and sec.answering_started_at is not None
                and old.answering_started_at is None
                and self.attempts.end_attempt_section_study(
                    attempt_id, sid, sec.answering_started_at
                )
            ):
                log.info(
                    "section study end attempt=%s section=%s at=%s -> recall",
                    attempt_id,
                    sid,
                    sec.answering_started_at.isoformat(),
                )
            if sec.closed_at is not None and old.closed_at is None:
                assert sec.close_reason is not None
                if not self.attempts.close_attempt_section(
                    attempt_id, sid, sec.closed_at, sec.close_reason
                ):
                    return True
                log.info(
                    "section %s attempt=%s section=%s at=%s",
                    "EXPIRE" if sec.close_reason == "expired" else "close (finished)",
                    attempt_id,
                    sid,
                    sec.closed_at.isoformat(),
                )
        return False

    def _status(
        self,
        attempt: Attempt,
        plan: SectionPlan,
        sec: AttemptSection | None,
        now: datetime,
    ) -> SectionStatus:
        phase = self._phase(attempt, sec, now)
        return SectionStatus(
            section_id=plan.section_id,
            position=plan.position,
            phase=phase,
            time_limit_seconds=plan.time_limit_seconds,
            study_seconds=plan.study_seconds,
            intro_started_at=sec.intro_started_at if sec else None,
            intro_ends_at=(
                sec.intro_started_at + timedelta(seconds=self.intro_cap)
                if sec is not None and sec.started_at is None
                else None
            ),
            started_at=sec.started_at if sec else None,
            started_by=sec.started_by if sec else None,
            study_ends_at=sec.study_ends_at if sec else None,
            deadline=sec.deadline if sec else None,
            closed_at=sec.closed_at if sec else None,
            close_reason=sec.close_reason if sec else None,
            remaining_seconds=(
                0 if phase == "closed" else remaining_seconds(sec, now, self.intro_cap)
            ),
        )

    def section_states(self, attempt_id: str, *, now: datetime | None = None) -> SectionsState:
        """Every timed section's clock (server time) + the current one. An attempt
        without timed sections answers `timed=False` and no sections."""
        now = now or datetime.now(UTC)
        attempt = self._require_attempt(attempt_id)
        state = SectionsState(
            attempt_id=attempt_id,
            timed=False,
            attempt_status=attempt.status,
            server_now=now,
            intro_max_seconds=self.intro_cap,
        )
        timed = self._timed(attempt)
        if timed is None:
            return state
        rows = self._sync_sections(attempt, timed[1], now)
        statuses = [self._status(attempt, p, rows.get(p.section_id), now) for p in timed[1]]
        current = next(
            (s.section_id for s in statuses if s.phase in ("intro", "study", "answering")),
            None,
        )
        log.debug(
            "sections attempt=%s current=%s phases=%s",
            attempt_id,
            current,
            ",".join(s.phase for s in statuses),
        )
        return state.model_copy(
            update={"timed": True, "current_section_id": current, "sections": statuses}
        )

    def _section_context(
        self, attempt_id: str, section_id: str, now: datetime
    ) -> tuple[Attempt, Test, SectionPlan, dict[str, AttemptSection]]:
        """Common checks: the attempt is active and timed, the section is served."""
        attempt = self._require_attempt(attempt_id)
        self._require_active(attempt, now)
        test, plans = self._require_timed(attempt)
        plan = next((p for p in plans if p.section_id == section_id), None)
        if plan is None:
            raise NotFoundError(f"section {section_id!r} not in attempt {attempt_id!r}")
        return attempt, test, plan, self._sync_sections(attempt, plans, now)

    def _not_open(
        self, section_id: str, phase: SectionPhase, sec: AttemptSection | None
    ) -> SectionStateError:
        """The refusal for acting on a section that is not answering right now."""
        if phase == "pending":
            return SectionStateError(
                "Finish the current part first: parts run in order.",
                code="section_not_current",
                section_id=section_id,
            )
        if phase == "intro":
            return SectionStateError(
                "This part has not begun yet.", code="section_not_begun", section_id=section_id
            )
        if phase == "study":
            return SectionStateError(
                "Answers open when the study time is over.",
                code="section_study",
                section_id=section_id,
            )
        if sec is not None and sec.close_reason == "expired":
            return SectionStateError(
                "The time for this part is over.", code="section_expired", section_id=section_id
            )
        return SectionStateError(
            "This part is closed.", code="section_closed", section_id=section_id
        )

    def begin_section(
        self, attempt_id: str, section_id: str, *, now: datetime | None = None
    ) -> SectionBegin:
        """Start the CURRENT section's clock (after its untimed intro / samples).

        Idempotent once begun (a refresh mid-section gets the same clock back). A
        later section -> `section_not_current`; a closed one -> `section_closed` /
        `section_expired`. With a study phase the response carries the stimulus
        while the window is open (and only then)."""
        now = now or datetime.now(UTC)
        attempt, test, plan, rows = self._section_context(attempt_id, section_id, now)
        sec = rows.get(section_id)
        phase = self._phase(attempt, sec, now)
        if phase in ("pending", "closed"):
            err = self._not_open(section_id, phase, sec)
            log.warning(
                "section begin REFUSED attempt=%s section=%s code=%s",
                attempt_id,
                section_id,
                err.code,
            )
            raise err
        if phase == "intro":
            assert sec is not None
            started = begun(sec, plan, now, "student", attempt.deadline)
            if self.attempts.begin_attempt_section(started):
                log.info(
                    "section begin attempt=%s section=%s by=student at=%s study_until=%s "
                    "deadline=%s",
                    attempt_id,
                    section_id,
                    now.isoformat(),
                    started.study_ends_at.isoformat() if started.study_ends_at else "-",
                    started.deadline.isoformat() if started.deadline else "-",
                )
            # Re-read (a concurrent intro-cap begin may have won — same outcome).
            rows = self._sync_sections(attempt, self._require_timed(attempt)[1], now)
            sec = rows.get(section_id)
        status = self._status(attempt, plan, sec, now)
        study = None
        if status.phase == "study":
            study = project_study(
                next(s for s in test.sections if s.id == section_id), attempt_id
            )
            log.info("serve study attempt=%s section=%s (with begin)", attempt_id, section_id)
        return SectionBegin(section=status, study=study)

    def _open_study(self, attempt_id: str, section_id: str, now: datetime) -> Section:
        """The section whose study window is open right now; else WARNING +
        `SectionStateError(study_not_open)`."""
        attempt, test, _plan, rows = self._section_context(attempt_id, section_id, now)
        section = next(s for s in test.sections if s.id == section_id)
        phase = self._phase(attempt, rows.get(section_id), now)
        if section.study is None or phase != "study":
            log.warning(
                "study REFUSED attempt=%s section=%s phase=%s", attempt_id, section_id, phase
            )
            raise SectionStateError(
                "The study material is not available now.",
                code="study_not_open",
                section_id=section_id,
            )
        return section

    def section_study(
        self, attempt_id: str, section_id: str, *, now: datetime | None = None
    ) -> ClientStudyStimulus:
        """The study stimulus — ONLY while the section's study window is open."""
        now = now or datetime.now(UTC)
        section = self._open_study(attempt_id, section_id, now)
        study = project_study(section, attempt_id)
        if study is None:  # pragma: no cover - `_open_study` checked `section.study`
            raise ValueError(f"section {section_id!r} has no study phase")
        log.info("serve study attempt=%s section=%s", attempt_id, section_id)
        return study

    def section_study_asset(
        self,
        attempt_id: str,
        section_id: str,
        index: int | None = None,
        *,
        now: datetime | None = None,
    ) -> str:
        """The asset id of the study PICTURE (`index=None`) or of study CELL
        `index` (`Section.study.images`) — only while the study window is open (the
        HTTP layer streams its bytes with `no-store`; the public `/assets/{id}`
        refuses study assets). 409 `study_not_open` outside the window; 404 for a
        missing picture / cell index."""
        now = now or datetime.now(UTC)
        section = self._open_study(attempt_id, section_id, now)
        study = section.study
        asset_id: str | None = None
        if study is not None:
            if index is None:
                asset_id = study.asset_id
            elif 0 <= index < len(study.images):
                asset_id = study.images[index]
        if asset_id is None:
            log.warning(
                "study asset REFUSED attempt=%s section=%s index=%s: no such picture",
                attempt_id,
                section_id,
                index,
            )
            raise NotFoundError(f"section {section_id!r} has no study picture {index}")
        log.info(
            "serve study asset attempt=%s section=%s index=%s", attempt_id, section_id, index
        )
        return asset_id

    def section_items(
        self, attempt_id: str, section_id: str, *, now: datetime | None = None
    ) -> ClientSection:
        """The section with its items (keyless), once it is answering (or closed)."""
        now = now or datetime.now(UTC)
        attempt, test, _plan, rows = self._section_context(attempt_id, section_id, now)
        sec = rows.get(section_id)
        phase = self._phase(attempt, sec, now)
        if phase not in ("answering", "closed"):
            err = self._not_open(section_id, phase, sec)
            log.warning(
                "section items REFUSED attempt=%s section=%s code=%s",
                attempt_id,
                section_id,
                err.code,
            )
            raise err
        layout = self._layout_for(self._served(attempt), attempt.seed)
        section = next(s for s in test.sections if s.id == section_id)
        log.info("serve section items attempt=%s section=%s", attempt_id, section_id)
        return project_section(section, layout, level=test.level)

    def finish_section(
        self, attempt_id: str, section_id: str, *, now: datetime | None = None
    ) -> SectionsState:
        """The student ends the CURRENT, begun section early (`finished`); the next
        section's intro starts now. Refused for a section not begun / not current /
        already closed."""
        now = now or datetime.now(UTC)
        attempt, _test, _plan, rows = self._section_context(attempt_id, section_id, now)
        sec = rows.get(section_id)
        phase = self._phase(attempt, sec, now)
        if phase not in ("study", "answering"):
            err = self._not_open(section_id, phase, sec)
            log.warning(
                "section finish REFUSED attempt=%s section=%s code=%s",
                attempt_id,
                section_id,
                err.code,
            )
            raise err
        if self.attempts.close_attempt_section(attempt_id, section_id, now, "finished"):
            log.info(
                "section close (finished early) attempt=%s section=%s at=%s",
                attempt_id,
                section_id,
                now.isoformat(),
            )
        return self.section_states(attempt_id, now=now)

    def _check_section_open(
        self,
        attempt: Attempt,
        served: Test,
        layout: AttemptLayout,
        item_id: str,
        now: datetime,
    ) -> None:
        """Timed sections: an answer is accepted only while its section is in the
        answering phase (WARNING + `SectionStateError` otherwise)."""
        timed = self._timed(attempt, served, layout)
        if timed is None:
            return
        test, plans = timed
        section_id = next(
            (s.id for s in test.sections if any(i.id == item_id for i in s.items)), None
        )
        if section_id is None:  # pragma: no cover - `_find_item` already 404s
            return
        rows = self._sync_sections(attempt, plans, now)
        sec = rows.get(section_id)
        phase = self._phase(attempt, sec, now)
        if phase == "answering":
            return
        err = self._not_open(section_id, phase, sec)
        log.warning(
            "answer REJECTED attempt=%s item=%s section=%s code=%s",
            attempt.id,
            item_id,
            section_id,
            err.code,
        )
        raise err

    # -- save answer (displayed -> canonical) ------------------------------ #
    def save_answer(
        self,
        attempt_id: str,
        item_id: str,
        response: str | int,
        *,
        now: datetime | None = None,
    ) -> Answer:
        """Persist one answer, mapping a displayed option index to canonical.

        `response` is the *displayed* option index (int) for `single_choice`
        items, and the literal text / pool key (str) for everything else. The
        stored response is always canonical, so grading never sees display order.
        """
        now = now or datetime.now(UTC)
        attempt = self._require_attempt(attempt_id)
        self._require_active(attempt, now)

        test = self._served(attempt)  # an unserved part's item -> 404
        layout = self._layout_for(test, attempt.seed)
        item = self._find_item(test, item_id)
        self._check_answer_size(attempt_id, item, response)
        self._check_section_open(attempt, test, layout, item_id, now)  # timed only
        canonical = self._canonical_response(item, layout, response)
        answer = Answer(
            attempt_id=attempt_id,
            item_id=item_id,
            response=canonical,
            answered_at=now,
        )
        self.attempts.save_answer(answer)
        log.info("answer attempt=%s item=%s -> %r", attempt_id, item_id, canonical)
        return answer

    # -- timer / state ----------------------------------------------------- #
    def get_state(
        self, attempt_id: str, *, now: datetime | None = None
    ) -> AttemptState:
        """Server-authoritative state; expires the attempt if the deadline passed.

        The timer never pauses (golden rule #3): `remaining_seconds` is measured
        against wall-clock `now`, and crossing the deadline flips an in-progress
        attempt to `expired` (persisted) regardless of client activity.
        """
        now = now or datetime.now(UTC)
        attempt, expired_now = self._expire_if_overdue(
            self._require_attempt(attempt_id), now
        )

        remaining = 0
        if attempt.deadline is not None and attempt.status == "in_progress":
            remaining = max(0, int((attempt.deadline - now).total_seconds()))
        return AttemptState(
            attempt_id=attempt_id,
            status=attempt.status,
            started_at=attempt.started_at,
            deadline=attempt.deadline,
            remaining_seconds=remaining,
            expired=attempt.status == "expired" or expired_now,
            audio_progress_seconds=attempt.audio_progress_seconds,
            audio_progress=self.attempts.get_track_progress(attempt_id),
            just_expired=expired_now,
        )

    # -- expiry (lazy per attempt, or eager for abandoned attempts) -------- #
    def expire_if_overdue(
        self, attempt_id: str, *, now: datetime | None = None
    ) -> bool:
        """Expire one attempt if its deadline passed; True iff THIS call flipped it.

        Used by the HTTP layer when a save/submit is rejected as late: the 409 alone
        would roll back, so the caller persists the transition here and (on True)
        schedules the attempt's finalize exactly once.
        """
        now = now or datetime.now(UTC)
        _, flipped = self._expire_if_overdue(self._require_attempt(attempt_id), now)
        return flipped

    def expire_overdue(
        self, now: datetime | None = None, *, test_id: str | None = None
    ) -> list[str]:
        """Expire every `in_progress` attempt past its deadline; returns the ids flipped.

        The abandoned-tab case: a student who closed the browser never polls
        `/state` again, so the lazy expiry never fires and the attempt would stay
        `in_progress` forever. Same transition as the lazy path (status only —
        answers already saved stay, the deadline never moves). Idempotent: a second
        run finds nothing. Scoped to one test when `test_id` is given.
        """
        now = now or datetime.now(UTC)
        flipped: list[str] = []
        for attempt_id in self.attempts.list_overdue_attempt_ids(now, test_id):
            attempt = self.attempts.get_attempt(attempt_id)
            if attempt is None:  # pragma: no cover - deleted between the two reads
                continue
            _, expired_now = self._expire_if_overdue(attempt, now)
            if expired_now:
                flipped.append(attempt_id)
        # Heal roster entries of attempts expired before `mark_expired` also moved
        # the roster (pre-existing rows): idempotent, same short transaction.
        self.attempts.sync_expired_roster_status(test_id)
        log.info(
            "expire_overdue test=%s now=%s -> %d attempt(s)",
            test_id or "*",
            now.isoformat(),
            len(flipped),
        )
        return flipped

    def _expire_if_overdue(self, attempt: Attempt, now: datetime) -> tuple[Attempt, bool]:
        """The one expiry transition (lazy `get_state`, late save/submit, sweep).

        Flips an `in_progress` attempt whose deadline is at/before `now` to
        `expired` via the repository's conditional update, so concurrent callers
        can't both claim the transition; the same UPDATE transaction moves the
        roster entry `in_progress -> expired` (a finished state, like a submit).
        Returns the (possibly updated) attempt and whether this call performed the
        flip; on a lost race the attempt is re-read fresh from the DB.
        """
        if (
            attempt.status != "in_progress"
            or attempt.deadline is None
            or now < attempt.deadline
        ):
            return attempt, False
        if not self.attempts.mark_expired(attempt.id):
            # Someone else moved it on (submitted / expired) in the meantime. Re-read
            # from the DB, not the identity map: the cached row is the stale
            # `in_progress` one (or was rewritten in memory by the no-op UPDATE).
            fresh = self.attempts.get_attempt(attempt.id, fresh=True)
            if fresh is None:  # pragma: no cover - deleted mid-race
                raise NotFoundError(f"attempt {attempt.id!r} not found")
            log.info(
                "attempt expire attempt=%s lost race -> status=%s", attempt.id, fresh.status
            )
            return fresh, False
        log.info(
            "attempt expire attempt=%s deadline=%s now=%s",
            attempt.id,
            attempt.deadline.isoformat(),
            now.isoformat(),
        )
        return attempt.model_copy(update={"status": "expired"}), True

    # -- listening audio progress (server-side, monotonic anti-replay) ----- #
    def report_audio_progress(
        self, attempt_id: str, seconds: int, *, now: datetime | None = None
    ) -> int:
        """Advance the furthest listening position; returns the stored value.

        Only ever moves forward (the repo takes the max), so a refresh or another
        device resumes from here and can never restart the recording. Ignored once
        the attempt is no longer in progress.
        """
        attempt = self._require_attempt(attempt_id)
        if attempt.status != "in_progress":
            return attempt.audio_progress_seconds
        return self.attempts.set_audio_progress(attempt_id, max(0, int(seconds)))

    def report_track_progress(
        self, attempt_id: str, asset_id: str, seconds: int, *, now: datetime | None = None
    ) -> dict[str, int]:
        """Advance the furthest position in ONE listening track (per-part audio).

        `asset_id` must be the main track of one of the test's listening tracks
        (`app.delivery.audio.listening_tracks`) — anything else is refused
        (`UnknownTrackError`, 400) so the table cannot grow with junk keys. Only
        moves forward (atomic max in the repo); ignored once the attempt is no
        longer in progress. Returns the whole `{asset_id: seconds}` map.
        """
        attempt = self._require_attempt(attempt_id)
        if attempt.status != "in_progress":
            return self.attempts.get_track_progress(attempt_id)
        test = self._served(attempt)  # only the served sections' tracks
        if asset_id not in track_asset_ids(test.sections):
            raise UnknownTrackError(
                f"asset {asset_id!r} is not a listening track of test {test.id!r}"
            )
        self.attempts.set_track_progress(attempt_id, asset_id, max(0, int(seconds)))
        return self.attempts.get_track_progress(attempt_id)

    # -- submit ------------------------------------------------------------ #
    def submit(self, attempt_id: str, *, now: datetime | None = None) -> Attempt:
        """Finalize an attempt. A late submit (past the deadline) is rejected."""
        now = now or datetime.now(UTC)
        attempt = self._require_attempt(attempt_id)
        if attempt.status == "submitted":
            raise AttemptStateError(f"attempt {attempt_id!r} already submitted")
        self._require_active(attempt, now)

        attempt = attempt.model_copy(
            update={"status": "submitted", "submitted_at": now}
        )
        self.attempts.update_attempt(attempt)
        entry = self.attempts.get_roster_entry(attempt.roster_entry_id)
        if entry is not None:
            self.attempts.update_roster_entry(
                entry.model_copy(update={"status": "submitted"})
            )
        log.info("attempt submit attempt=%s at=%s", attempt_id, now.isoformat())
        return attempt

    # -- internals --------------------------------------------------------- #
    def _layout_for(self, test: Test, seed: int) -> AttemptLayout:
        return build_attempt_layout(self._bank_builder(test), seed)

    def _require_test(self, test_id: str) -> Test:
        test = self.content.get_test(test_id)
        if test is None:
            raise NotFoundError(f"test {test_id!r} not found")
        return test

    def _require_attempt(self, attempt_id: str) -> Attempt:
        attempt = self.attempts.get_attempt(attempt_id)
        if attempt is None:
            raise NotFoundError(f"attempt {attempt_id!r} not found")
        return attempt

    def _require_active(self, attempt: Attempt, now: datetime) -> None:
        if attempt.status == "submitted":
            raise AttemptStateError(f"attempt {attempt.id!r} already submitted")
        if attempt.status == "expired" or (
            attempt.deadline is not None and now >= attempt.deadline
        ):
            raise AttemptExpiredError(f"attempt {attempt.id!r} past deadline")
        if attempt.status != "in_progress":
            raise AttemptStateError(
                f"attempt {attempt.id!r} not in progress ({attempt.status})"
            )

    def _find_item(self, test: Test, item_id: str) -> Item:
        for section in test.sections:
            for item in section.items:
                if item.id == item_id:
                    return item
        raise NotFoundError(f"item {item_id!r} not in test {test.id!r}")

    @staticmethod
    def answer_size_limit(item: Item) -> int:
        """The longest text answer accepted for this item (chars)."""
        if isinstance(item, DrawingItem):
            return min(DRAWING_MAX_RESPONSE_CHARS, settings.answer_max_chars)
        return settings.answer_max_chars

    def _check_answer_size(self, attempt_id: str, item: Item, response: str | int) -> None:
        """Refuse an over-long text answer before anything else touches it."""
        if not isinstance(response, str):
            return
        limit = self.answer_size_limit(item)
        if len(response) <= limit:
            return
        log.warning(
            "answer REJECTED attempt=%s item=%s type=%s: %d chars > cap %d",
            attempt_id,
            item.id,
            item.item_type,
            len(response),
            limit,
        )
        raise AnswerTooLargeError(
            f"answer for {item.id!r} is too large ({len(response)} chars, max {limit})",
            size=len(response),
            limit=limit,
        )

    def _canonical_response(
        self, item: Item, layout: AttemptLayout, response: str | int
    ) -> str:
        """Translate a raw client response into the canonical stored form."""
        if isinstance(item, SingleChoiceItem):
            if not isinstance(response, int) or isinstance(response, bool):
                raise DeliveryError(
                    f"single_choice item {item.id!r} expects a displayed index (int)"
                )
            # Guard the lower bound explicitly: Python tuples accept negative
            # indices (so -1 would silently map to the last option), so only an
            # over-large index raises IndexError on its own.
            if response < 0:
                raise DeliveryError(
                    f"displayed index {response} is negative for {item.id!r}"
                )
            try:
                return layout.to_canonical(item.id, response)
            except (KeyError, IndexError) as exc:
                raise DeliveryError(
                    f"displayed index {response} out of range for {item.id!r}"
                ) from exc
        if not isinstance(response, str):
            raise DeliveryError(f"item {item.id!r} expects a text response (str)")
        return response

    def _displayed_response(
        self, item: Item, layout: AttemptLayout, canonical: str
    ) -> str | int:
        """Inverse of `_canonical_response`: stored canonical -> client display value."""
        if isinstance(item, SingleChoiceItem):
            shuffle = layout.option_shuffles.get(item.id)
            if shuffle is not None:
                try:
                    return shuffle.to_displayed(canonical)
                except ValueError:
                    pass  # stale/unknown key -> fall through to the raw value
        return canonical

    def get_saved_answers(self, attempt_id: str) -> dict[str, str | int]:
        """Every saved answer for an attempt as its *client display* value.

        The server is the source of truth: on refresh/resume the runner re-loads
        these so nothing is lost. single_choice -> displayed index (mapped back
        through this attempt's shuffle); everything else -> the stored text.
        """
        attempt = self._require_attempt(attempt_id)
        test = self._served(attempt)
        layout = self._layout_for(test, attempt.seed)
        items = {it.id: it for s in test.sections for it in s.items}
        out: dict[str, str | int] = {}
        for ans in self.attempts.get_answers(attempt_id):
            item = items.get(ans.item_id)
            if item is not None:
                out[ans.item_id] = self._displayed_response(item, layout, ans.response)
        log.info("answers attempt=%s -> %d saved", attempt_id, len(out))
        return out
