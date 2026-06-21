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

The per-attempt layout is *recomputed* from `Attempt.seed` on every call, never
stored, so a resumed attempt reproduces the identical draw (rule #7). The seed is
`derive_seed(test_id, roster_entry_id)` — stable across resumes.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from app.content import AttemptLayout, SectionPool, TestBank, build_attempt_layout, derive_seed
from app.core.logging import get_logger
from app.delivery.projection import project_test
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import (
    Answer,
    Attempt,
    ClientItem,
    ClientTest,
    Item,
    SingleChoiceItem,
    Test,
)

log = get_logger(__name__)


# --------------------------------------------------------------------------- #
# Errors (all distinguishable so callers/tests can assert the failure mode).
# --------------------------------------------------------------------------- #
class DeliveryError(Exception):
    """Base for every delivery-layer rejection."""


class WindowNotOpenError(DeliveryError):
    """Tried to start before the window opens."""


class WindowClosedError(DeliveryError):
    """Tried to start after the window (+ grace) closed."""


class AttemptExpiredError(DeliveryError):
    """Acted on (saved/submitted) an attempt past its deadline."""


class AttemptStateError(DeliveryError):
    """Acted on an attempt in a state that forbids it (e.g. already submitted)."""


class NotFoundError(DeliveryError):
    """Referenced a roster entry / attempt / test / item that does not exist."""


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


@dataclass(frozen=True)
class AttemptState:
    """Server-authoritative snapshot of where an attempt stands right now."""

    attempt_id: str
    status: str
    started_at: datetime | None
    deadline: datetime | None
    remaining_seconds: int
    expired: bool


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
    ) -> None:
        self.content = content_repo
        self.attempts = attempt_repo
        self._bank_builder = bank_builder

    # -- start (create-or-resume, refresh-safe) ---------------------------- #
    def start(
        self,
        roster_entry_id: str,
        window: ExamWindow,
        *,
        now: datetime | None = None,
    ) -> Attempt:
        """Create the attempt for a roster entry, or resume the existing one.

        Window/grace is validated only when creating; a resume reopens whatever
        attempt already exists (its deadline was fixed at first start).
        """
        now = now or datetime.now(UTC)
        entry = self.attempts.get_roster_entry(roster_entry_id)
        if entry is None:
            raise NotFoundError(f"roster entry {roster_entry_id!r} not found")

        if entry.attempt_id is not None:
            return self._resume(roster_entry_id, entry.attempt_id)

        if now < window.opens_at:
            raise WindowNotOpenError(f"window opens at {window.opens_at.isoformat()}")
        if now > window.hard_close:
            raise WindowClosedError(f"window closed at {window.hard_close.isoformat()}")

        test = self._require_test(entry.test_id)
        seed = derive_seed(entry.test_id, entry.id)
        # Build the layout now so the pooling decisions are logged at start.
        self._layout_for(test, seed)
        deadline = min(
            now + timedelta(minutes=test.duration_minutes), window.hard_close
        )
        attempt = Attempt(
            id=str(uuid.uuid4()),
            test_id=entry.test_id,
            roster_entry_id=entry.id,
            status="in_progress",
            seed=seed,
            started_at=now,
            deadline=deadline,
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
        self.attempts.update_roster_entry(
            entry.model_copy(update={"status": "in_progress", "attempt_id": attempt.id})
        )
        log.info(
            "attempt start entry=%s attempt=%s seed=%d deadline=%s",
            roster_entry_id,
            attempt.id,
            seed,
            deadline.isoformat(),
        )
        return attempt

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

    # -- serve (no `correct` ever leaves here) ----------------------------- #
    def client_test(self, attempt_id: str) -> ClientTest:
        """The full student-facing test for an attempt (drawn + shuffled, no key)."""
        client = self._project(self._require_attempt(attempt_id))
        log.info(
            "serve client_test attempt=%s sections=%d", attempt_id, len(client.sections)
        )
        return client

    def serve_item(self, attempt_id: str, item_id: str) -> ClientItem:
        """Serve a single item (one-at-a-time), stripped of its answer key."""
        client = self._project(self._require_attempt(attempt_id))
        for section in client.sections:
            for item in section.items:
                if item.id == item_id:
                    log.info("serve item attempt=%s item=%s", attempt_id, item_id)
                    return item
        raise NotFoundError(f"item {item_id!r} not in attempt {attempt_id!r}")

    def _project(self, attempt: Attempt) -> ClientTest:
        """Build the student-facing projection for an attempt (no logging)."""
        test = self._require_test(attempt.test_id)
        return project_test(test, self._layout_for(test, attempt.seed))

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

        test = self._require_test(attempt.test_id)
        layout = self._layout_for(test, attempt.seed)
        item = self._find_item(test, item_id)
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
        attempt = self._require_attempt(attempt_id)
        expired_now = False
        if (
            attempt.status == "in_progress"
            and attempt.deadline is not None
            and now >= attempt.deadline
        ):
            attempt = attempt.model_copy(update={"status": "expired"})
            self.attempts.update_attempt(attempt)
            expired_now = True
            log.info("attempt expire attempt=%s", attempt_id)

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
        )

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
