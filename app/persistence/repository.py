"""Thin repositories: translate between `contracts/` Pydantic models and rows.

One repository per aggregate (content, attempts, events). They are deliberately
thin — create + read round-trips with INFO logging at the persistence boundary
(CLAUDE.md logging rules). Higher-level engines (content, delivery, telemetry)
build their domain logic on top of these in later phases.

Every method takes/returns contract models, never ORM rows, so the rest of the
app only ever speaks contracts and the source-of-truth shape can't leak.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from typing import NamedTuple

from pydantic import TypeAdapter
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.persistence import models as m
from contracts import (
    AnalysisVerdict,
    Answer,
    Attempt,
    AttemptReport,
    GradingResult,
    IntegrityEvent,
    IntegrityProfile,
    Item,
    ManualGrade,
    RosterEntry,
    Section,
    SkillReport,
    Stimulus,
    Test,
)
from contracts.content import PublishStatus
from contracts.runtime import PIN_ALPHABET, PIN_LENGTH

log = get_logger(__name__)

# Validators for the polymorphic parts persisted as JSON.
_ITEM: TypeAdapter[Item] = TypeAdapter(Item)
_STIMULUS: TypeAdapter[Stimulus] = TypeAdapter(Stimulus)
_SKILLS: TypeAdapter[list[SkillReport]] = TypeAdapter(list[SkillReport])


class MissingReportCandidate(NamedTuple):
    """A finished attempt whose report is absent (`report_status=None`) or `failed`.

    `finished_at` is when the attempt ended: `submitted_at`, or the `deadline` for
    an expired attempt (which has no `submitted_at`).
    """

    attempt_id: str
    report_status: str | None
    graded_at: datetime | None
    finished_at: datetime | None = None


class ReportMarker(NamedTuple):
    """Fresh-from-the-DB finalize state of one attempt (no ORM identity map).

    `report_status` / `graded_at` are None when there is no report; `finished_at`
    is `submitted_at`, else the `deadline` (expired attempts), else None.
    """

    attempt_status: str
    report_status: str | None
    graded_at: datetime | None
    finished_at: datetime | None

# Shared item fields promoted to columns; everything else is the answer-key data.
_ITEM_COLUMN_FIELDS = ("id", "item_type", "prompt")


def new_pin(taken: set[str] | frozenset[str] = frozenset()) -> str:
    """A fresh random classroom PIN (`secrets`, unambiguous alphabet) not in `taken`.

    31^4 = 923 521 combinations, so any classroom-sized roster draws a free one on
    the first try; the loop only guards the rare collision.
    """
    while True:
        pin = "".join(secrets.choice(PIN_ALPHABET) for _ in range(PIN_LENGTH))
        if pin not in taken:
            return pin


def hash_session_token(token: str) -> str:
    """SHA-256 hex of an attempt session token — the only form that is stored."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _roster_from_row(row: m.RosterEntryRow) -> RosterEntry:
    return RosterEntry(
        id=row.id,
        test_id=row.test_id,
        display_name=row.display_name,
        status=row.status,  # type: ignore[arg-type]
        attempt_id=row.attempt_id,
        pin=row.pin,
    )


# --------------------------------------------------------------------------- #
# Content.
# --------------------------------------------------------------------------- #
class ContentRepository:
    """Persist and load whole `Test` aggregates (sections + items)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add_test(self, test: Test) -> str:
        row = m.TestRow(
            id=test.id,
            title=test.title,
            level=test.level,
            status=test.status,
            duration_minutes=test.duration_minutes,
            opens_at=test.opens_at,
            closes_at=test.closes_at,
            late_start_grace_minutes=test.late_start_grace_minutes,
        )
        for s_pos, section in enumerate(test.sections):
            row.sections.append(self._section_row(section, s_pos))
        self.session.add(row)
        self.session.flush()
        log.info(
            "content add_test id=%s sections=%d items=%d",
            test.id,
            len(test.sections),
            sum(len(s.items) for s in test.sections),
        )
        return test.id

    def get_test(self, test_id: str) -> Test | None:
        row = self.session.get(m.TestRow, test_id)
        if row is None:
            log.info("content get_test id=%s -> miss", test_id)
            return None
        test = self._test_from_row(row)
        log.info("content get_test id=%s -> hit (%d sections)", test_id, len(test.sections))
        return test

    def list_tests(self) -> list[Test]:
        rows = self.session.scalars(select(m.TestRow).order_by(m.TestRow.id)).all()
        log.info("content list_tests -> %d", len(rows))
        return [self._test_from_row(r) for r in rows]

    def set_status(self, test_id: str, status: PublishStatus) -> bool:
        """Flip a test's publish status (draft <-> published). Returns hit/miss."""
        row = self.session.get(m.TestRow, test_id)
        if row is None:
            log.info("content set_status id=%s -> miss", test_id)
            return False
        row.status = status
        self.session.flush()
        log.info("content set_status id=%s -> %s", test_id, status)
        return True

    def set_schedule(
        self,
        test_id: str,
        *,
        opens_at: datetime | None,
        closes_at: datetime | None,
        late_start_grace_minutes: int,
    ) -> bool:
        """Update ONLY a test's exam window (three columns; sections/items untouched).

        The window is schedule meta, not key-bearing, so it may change on a published
        test with attempts; the caller (`ContentService.set_schedule`) validates it.
        Returns hit/miss."""
        row = self.session.get(m.TestRow, test_id)
        if row is None:
            log.info("content set_schedule id=%s -> miss", test_id)
            return False
        row.opens_at = opens_at
        row.closes_at = closes_at
        row.late_start_grace_minutes = late_start_grace_minutes
        self.session.flush()
        log.info(
            "content set_schedule id=%s opens=%s closes=%s grace=%d",
            test_id,
            opens_at.isoformat() if opens_at else None,
            closes_at.isoformat() if closes_at else None,
            late_start_grace_minutes,
        )
        return True

    def delete_test(self, test_id: str) -> bool:
        """Delete a test and its sections/items (cascade). Returns hit/miss."""
        row = self.session.get(m.TestRow, test_id)
        if row is None:
            log.info("content delete_test id=%s -> miss", test_id)
            return False
        self.session.delete(row)
        self.session.flush()
        log.info("content delete_test id=%s -> deleted", test_id)
        return True

    # -- editing (Phase 13: content editor / import) ------------------------- #
    def replace_test(self, test: Test) -> str:
        """Overwrite an existing test's metadata + sections/items with `test`.

        Sections and items are rewritten wholesale (delete-orphan, flushed before
        the re-insert so unchanged ids can be reused); the roster and attempts are
        untouched. The caller (`ContentService`) validates and guards the edit."""
        row = self.session.get(m.TestRow, test.id)
        if row is None:
            raise KeyError(test.id)
        row.title = test.title
        row.level = test.level
        row.status = test.status
        row.duration_minutes = test.duration_minutes
        row.opens_at = test.opens_at
        row.closes_at = test.closes_at
        row.late_start_grace_minutes = test.late_start_grace_minutes
        row.sections.clear()
        self.session.flush()
        for s_pos, section in enumerate(test.sections):
            row.sections.append(self._section_row(section, s_pos))
        self.session.flush()
        self.session.expire(row)
        log.info(
            "content replace_test id=%s sections=%d items=%d status=%s",
            test.id,
            len(test.sections),
            sum(len(s.items) for s in test.sections),
            test.status,
        )
        return test.id

    def test_exists(self, test_id: str) -> bool:
        return self.session.get(m.TestRow, test_id) is not None

    def count_attempts(self, test_id: str) -> int:
        """Attempts (any status) on `test_id` — the edit / replace guard input."""
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(m.AttemptRow)
                .where(m.AttemptRow.test_id == test_id)
            )
            or 0
        )

    def ids_in_use_elsewhere(
        self, test_id: str, section_ids: set[str], item_ids: set[str]
    ) -> set[str]:
        """Section / item ids already owned by ANOTHER test (they are global PKs)."""
        taken: set[str] = set()
        if section_ids:
            taken.update(
                self.session.scalars(
                    select(m.SectionRow.id).where(
                        m.SectionRow.id.in_(section_ids), m.SectionRow.test_id != test_id
                    )
                )
            )
        if item_ids:
            taken.update(
                self.session.scalars(
                    select(m.ItemRow.id)
                    .join(m.SectionRow, m.ItemRow.section_id == m.SectionRow.id)
                    .where(m.ItemRow.id.in_(item_ids), m.SectionRow.test_id != test_id)
                )
            )
        return taken

    # -- row <-> contract helpers ------------------------------------------- #
    def _test_from_row(self, row: m.TestRow) -> Test:
        return Test(
            id=row.id,
            title=row.title,
            level=row.level,  # type: ignore[arg-type]
            status=row.status,  # type: ignore[arg-type]
            duration_minutes=row.duration_minutes,
            opens_at=row.opens_at,
            closes_at=row.closes_at,
            late_start_grace_minutes=row.late_start_grace_minutes or 0,
            sections=[self._section_from_row(s) for s in row.sections],
        )

    def _section_row(self, section: Section, position: int) -> m.SectionRow:
        srow = m.SectionRow(
            id=section.id,
            position=position,
            title=section.title,
            skill=section.skill,
            stimulus=section.stimulus.model_dump(mode="json"),
        )
        for i_pos, item in enumerate(section.items):
            dumped = item.model_dump(mode="json")
            data = {k: v for k, v in dumped.items() if k not in _ITEM_COLUMN_FIELDS}
            srow.items.append(
                m.ItemRow(
                    id=item.id,
                    position=i_pos,
                    item_type=item.item_type,
                    prompt=item.prompt,
                    data=data,
                )
            )
        return srow

    def _section_from_row(self, srow: m.SectionRow) -> Section:
        items = [
            _ITEM.validate_python(
                {"item_type": i.item_type, "id": i.id, "prompt": i.prompt, **i.data}
            )
            for i in srow.items
        ]
        return Section(
            id=srow.id,
            title=srow.title,
            skill=srow.skill,  # type: ignore[arg-type]
            stimulus=_STIMULUS.validate_python(srow.stimulus),
            items=items,
        )


# --------------------------------------------------------------------------- #
# Attempts (roster + attempt + answers).
# --------------------------------------------------------------------------- #
class AttemptRepository:
    """Persist roster entries, attempts, and their answers."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add_roster_entry(self, entry: RosterEntry) -> str:
        """Insert a roster entry; a missing `pin` is generated (unique in the test).

        Every creation path (teacher single / bulk add, demo seed, fixture loader)
        goes through here, so no roster entry is stored without a PIN. The caller's
        `entry.pin` is set to the stored value. The PIN itself is never logged.
        """
        taken = self._pins_in_test(entry.test_id)
        if entry.pin is None:
            entry.pin = new_pin(taken)
        elif entry.pin in taken:
            raise ValueError(f"PIN already used on test {entry.test_id!r}")
        self.session.add(
            m.RosterEntryRow(
                id=entry.id,
                test_id=entry.test_id,
                display_name=entry.display_name,
                status=entry.status,
                attempt_id=entry.attempt_id,
                pin=entry.pin,
            )
        )
        self.session.flush()
        log.info("roster add id=%s test=%s (pin assigned)", entry.id, entry.test_id)
        return entry.id

    def _pins_in_test(self, test_id: str) -> set[str]:
        rows = self.session.scalars(
            select(m.RosterEntryRow.pin).where(m.RosterEntryRow.test_id == test_id)
        )
        return {p for p in rows if p}

    def set_roster_pin(self, entry_id: str) -> str:
        """Give a roster entry a NEW random PIN (unique in its test); returns it.

        Only future starts are affected: session tokens already issued for the
        entry's attempt stay valid (see app/delivery/CLAUDE.md).
        """
        row = self.session.get(m.RosterEntryRow, entry_id)
        if row is None:
            raise KeyError(f"roster entry {entry_id!r} not found")
        taken = self._pins_in_test(row.test_id)  # includes the current PIN
        row.pin = new_pin(taken)
        self.session.flush()
        log.info("roster pin regenerated id=%s test=%s", entry_id, row.test_id)
        return row.pin

    # -- attempt session tokens (Phase 14; only the hash is stored) --------- #
    def add_attempt_session(
        self, attempt_id: str, token: str, *, now: datetime | None = None
    ) -> None:
        """Store the SHA-256 of a freshly issued session token for an attempt."""
        self.session.add(
            m.AttemptSessionRow(
                token_hash=hash_session_token(token),
                attempt_id=attempt_id,
                created_at=now or datetime.now(UTC),
            )
        )
        self.session.flush()
        log.info("attempt session issued attempt=%s", attempt_id)

    def attempt_session_valid(self, attempt_id: str, token: str | None) -> bool:
        """True iff `token` is a session issued for THIS attempt (hash lookup)."""
        if not token:
            return False
        row = self.session.get(m.AttemptSessionRow, hash_session_token(token))
        return row is not None and row.attempt_id == attempt_id

    def count_attempt_sessions(self, attempt_id: str) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(m.AttemptSessionRow)
                .where(m.AttemptSessionRow.attempt_id == attempt_id)
            )
            or 0
        )

    def update_roster_entry(self, entry: RosterEntry) -> None:
        """Persist a roster entry's mutable fields (status + attempt back-pointer).

        Delivery owns the `roster -> attempt` link (see `RosterEntryRow`), so it
        writes it here when an attempt is created/resumed.
        """
        row = self.session.get(m.RosterEntryRow, entry.id)
        if row is None:
            raise KeyError(f"roster entry {entry.id!r} not found")
        row.status = entry.status
        row.attempt_id = entry.attempt_id
        self.session.flush()
        log.info(
            "roster update id=%s status=%s attempt=%s",
            entry.id,
            entry.status,
            entry.attempt_id,
        )

    def get_roster_entry(self, entry_id: str) -> RosterEntry | None:
        row = self.session.get(m.RosterEntryRow, entry_id)
        if row is None:
            return None
        return _roster_from_row(row)

    def list_roster_entries(self, test_id: str) -> list[RosterEntry]:
        rows = self.session.scalars(
            select(m.RosterEntryRow)
            .where(m.RosterEntryRow.test_id == test_id)
            .order_by(m.RosterEntryRow.display_name)
        ).all()
        log.info("roster list test=%s -> %d", test_id, len(rows))
        return [_roster_from_row(r) for r in rows]

    def add_attempt(self, attempt: Attempt) -> str:
        self.session.add(
            m.AttemptRow(
                id=attempt.id,
                test_id=attempt.test_id,
                roster_entry_id=attempt.roster_entry_id,
                status=attempt.status,
                seed=attempt.seed,
                started_at=attempt.started_at,
                submitted_at=attempt.submitted_at,
                deadline=attempt.deadline,
            )
        )
        self.session.flush()
        log.info("attempt add id=%s test=%s seed=%d", attempt.id, attempt.test_id, attempt.seed)
        return attempt.id

    def get_attempt(self, attempt_id: str, *, fresh: bool = False) -> Attempt | None:
        """Load an attempt; `fresh=True` re-reads the row from the DB.

        By default the session's identity map may answer without SQL, so after a
        conditional UPDATE that matched nothing (another writer won the race) the
        cached row can be stale — `fresh` (`populate_existing`) overwrites it with
        the committed/current DB state.
        """
        if fresh:
            row = self.session.get(m.AttemptRow, attempt_id, populate_existing=True)
        else:
            row = self.session.get(m.AttemptRow, attempt_id)
        if row is None:
            log.info("attempt get id=%s -> miss", attempt_id)
            return None
        return Attempt(
            id=row.id,
            test_id=row.test_id,
            roster_entry_id=row.roster_entry_id,
            status=row.status,  # type: ignore[arg-type]
            seed=row.seed,
            started_at=row.started_at,
            submitted_at=row.submitted_at,
            deadline=row.deadline,
            audio_progress_seconds=row.audio_progress_seconds,
        )

    def set_audio_progress(self, attempt_id: str, seconds: int) -> int:
        """Advance the furthest listening position; never moves backwards. Returns it."""
        row = self.session.get(m.AttemptRow, attempt_id)
        if row is None:
            raise KeyError(f"attempt {attempt_id!r} not found")
        row.audio_progress_seconds = max(row.audio_progress_seconds, max(0, seconds))
        self.session.flush()
        # DEBUG: the runner posts every ~5 s per student (legacy path, see the API).
        log.debug("attempt audio id=%s -> %ds", attempt_id, row.audio_progress_seconds)
        return row.audio_progress_seconds

    def set_track_progress(self, attempt_id: str, asset_id: str, seconds: int) -> int:
        """Advance the furthest position in ONE listening track; never moves back.

        An atomic upsert (`INSERT … ON CONFLICT DO UPDATE SET seconds =
        max(seconds, excluded.seconds)`), so two devices / a retried POST can never
        lose an update or move the point backwards. Returns the stored value."""
        seconds = max(0, int(seconds))
        table = m.AttemptAudioProgressRow.__table__
        where = (table.c.attempt_id == attempt_id, table.c.asset_id == asset_id)
        first = self.session.scalar(select(table.c.seconds).where(*where)) is None
        stmt = sqlite_insert(table).values(
            attempt_id=attempt_id, asset_id=asset_id, seconds=seconds
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["attempt_id", "asset_id"],
            set_={"seconds": func.max(table.c.seconds, stmt.excluded.seconds)},
        )
        self.session.execute(stmt)
        self.session.flush()
        stored = self.session.scalar(select(table.c.seconds).where(*where))
        # One INFO when a track's first point lands; the ~5 s updates are DEBUG
        # (a class of 30 would otherwise flood the session console).
        if first:
            log.info("attempt audio id=%s track=%s started -> %ds", attempt_id, asset_id, stored)
        else:
            log.debug("attempt audio id=%s track=%s -> %ds", attempt_id, asset_id, stored)
        return int(stored or 0)

    def get_track_progress(self, attempt_id: str) -> dict[str, int]:
        """`{track asset_id: furthest seconds}` for one attempt (empty = none started)."""
        rows = self.session.execute(
            select(
                m.AttemptAudioProgressRow.asset_id, m.AttemptAudioProgressRow.seconds
            ).where(m.AttemptAudioProgressRow.attempt_id == attempt_id)
        ).all()
        return {asset_id: int(sec) for asset_id, sec in rows}

    def update_attempt(self, attempt: Attempt) -> None:
        """Persist attempt lifecycle changes (status / timestamps / deadline).

        Delivery calls this to record start, expiry, and submit transitions on an
        existing row. The server timer is authoritative (golden rule #3): delivery
        writes the deadline once at start and always passes it back unchanged — it
        never pauses or moves it.

        The single documented exception is the teacher's reopen-with-extension
        (`AdminService.reopen_attempt(..., extend_minutes=N)`), which moves the
        deadline of one attempt to `max(old, now + N)` through this method: an
        explicit, logged teacher override, not the student timer pausing (see the
        golden rule #3 note in `app/admin/CLAUDE.md`). The row's `deadline` is
        written verbatim from `attempt`, so no other caller may pass a changed one.
        """
        row = self.session.get(m.AttemptRow, attempt.id)
        if row is None:
            raise KeyError(f"attempt {attempt.id!r} not found")
        row.status = attempt.status
        row.started_at = attempt.started_at
        row.submitted_at = attempt.submitted_at
        row.deadline = attempt.deadline
        self.session.flush()
        log.info("attempt update id=%s status=%s", attempt.id, attempt.status)

    def mark_expired(self, attempt_id: str) -> bool:
        """Atomically flip an `in_progress` attempt to `expired`; True iff it flipped.

        A conditional UPDATE (`... WHERE status = 'in_progress'`) rather than a
        read-modify-write, so two concurrent callers (a student's state poll and the
        recovery sweep) can never both see themselves as the one that expired it —
        the caller that gets True owns the transition (e.g. schedules the finalize).
        Answers already saved are untouched; the deadline never moves (rule #3).
        """
        result = self.session.execute(
            update(m.AttemptRow)
            .where(m.AttemptRow.id == attempt_id, m.AttemptRow.status == "in_progress")
            .values(status="expired")
        )
        self.session.flush()
        flipped = bool(result.rowcount)
        log.info("attempt mark_expired id=%s -> %s", attempt_id, flipped)
        if flipped:
            # Same transaction: the roster entry leaves `in_progress` too, so the
            # pick-your-name screen shows the student as done (like a submit).
            roster = self.session.execute(
                update(m.RosterEntryRow)
                .where(
                    m.RosterEntryRow.attempt_id == attempt_id,
                    m.RosterEntryRow.status == "in_progress",
                )
                .values(status="expired")
            )
            self.session.flush()
            log.info(
                "roster mark_expired attempt=%s -> %d entr(y/ies)", attempt_id, roster.rowcount
            )
        return flipped

    def sync_expired_roster_status(self, test_id: str | None = None) -> int:
        """Repair roster entries left `in_progress` whose attempt is `expired`.

        Rows written before `mark_expired` also moved the roster entry (or by any
        other path that expired an attempt alone) keep a stale `in_progress`. A
        conditional, idempotent UPDATE run by `DeliveryService.expire_overdue` (the
        recovery sweep / "Дооценить все"), so existing data heals on the next sweep
        without a data migration. Returns the number of entries fixed.
        """
        expired_ids = select(m.AttemptRow.id).where(m.AttemptRow.status == "expired")
        if test_id is not None:
            expired_ids = expired_ids.where(m.AttemptRow.test_id == test_id)
        result = self.session.execute(
            update(m.RosterEntryRow)
            .where(
                m.RosterEntryRow.status == "in_progress",
                m.RosterEntryRow.attempt_id.in_(expired_ids),
            )
            .values(status="expired")
            .execution_options(synchronize_session="fetch")
        )
        self.session.flush()
        fixed = int(result.rowcount or 0)
        if fixed:
            log.info("roster sync_expired test=%s -> %d entr(y/ies) fixed", test_id or "*", fixed)
        return fixed

    def list_overdue_attempt_ids(
        self, now: datetime, test_id: str | None = None
    ) -> list[str]:
        """Ids of `in_progress` attempts whose deadline is at/before `now`.

        The abandoned-tab case: a student who closed the browser never polls
        `/state` again, so the lazy expiry never runs. Recovery expires these.
        Ordered by deadline (oldest first); scoped to one test when given.
        """
        stmt = (
            select(m.AttemptRow.id)
            .where(m.AttemptRow.status == "in_progress")
            .where(m.AttemptRow.deadline.is_not(None))
            .where(m.AttemptRow.deadline <= now)
            .order_by(m.AttemptRow.deadline, m.AttemptRow.id)
        )
        if test_id is not None:
            stmt = stmt.where(m.AttemptRow.test_id == test_id)
        ids = list(self.session.scalars(stmt).all())
        log.info("attempts overdue test=%s -> %d", test_id or "*", len(ids))
        return ids

    def delete_attempt(self, attempt_id: str) -> bool:
        """Wipe an attempt and its dependents (answers + events).

        Dependents are deleted explicitly (same order as
        `app/content/load_test._clear_existing`) so the wipe does not rely on the
        engine's FK cascade — SQLite only enforces it with `PRAGMA foreign_keys=ON`,
        which `app/core/db.py` now sets. Returns False if
        the attempt did not exist. Does NOT touch the roster back-pointer; callers
        that reset/remove a student clear it themselves.
        """
        if self.session.get(m.AttemptRow, attempt_id) is None:
            return False
        self.session.execute(delete(m.AnswerRow).where(m.AnswerRow.attempt_id == attempt_id))
        self.session.execute(
            delete(m.IntegrityEventRow).where(m.IntegrityEventRow.attempt_id == attempt_id)
        )
        self.session.execute(
            delete(m.ManualGradeRow).where(m.ManualGradeRow.attempt_id == attempt_id)
        )
        self.session.execute(
            delete(m.AttemptReportRow).where(m.AttemptReportRow.attempt_id == attempt_id)
        )
        self.session.execute(
            delete(m.AttemptSessionRow).where(m.AttemptSessionRow.attempt_id == attempt_id)
        )
        self.session.execute(
            delete(m.AttemptAudioProgressRow).where(
                m.AttemptAudioProgressRow.attempt_id == attempt_id
            )
        )
        self.session.execute(delete(m.AttemptRow).where(m.AttemptRow.id == attempt_id))
        self.session.flush()
        log.info(
            "attempt delete id=%s (+answers +events +manual grades +report +sessions +audio)",
            attempt_id,
        )
        return True

    def upsert_manual_grade(self, grade: ManualGrade) -> None:
        """Insert or update the teacher's hand-entered mark for one item."""
        row = self.session.get(
            m.ManualGradeRow, {"attempt_id": grade.attempt_id, "item_id": grade.item_id}
        )
        if row is None:
            self.session.add(
                m.ManualGradeRow(
                    attempt_id=grade.attempt_id,
                    item_id=grade.item_id,
                    awarded=grade.awarded,
                    graded_at=grade.graded_at,
                )
            )
        else:
            row.awarded = grade.awarded
            row.graded_at = grade.graded_at
        self.session.flush()
        log.info(
            "manual grade attempt=%s item=%s -> %.2f",
            grade.attempt_id,
            grade.item_id,
            grade.awarded,
        )

    def list_manual_grades(self, attempt_id: str) -> list[ManualGrade]:
        rows = self.session.scalars(
            select(m.ManualGradeRow).where(m.ManualGradeRow.attempt_id == attempt_id)
        ).all()
        return [
            ManualGrade(
                attempt_id=r.attempt_id,
                item_id=r.item_id,
                awarded=r.awarded,
                graded_at=r.graded_at,
            )
            for r in rows
        ]

    # -- attempt reports (frozen grade + per-skill feedback, computed on submit) -- #
    def upsert_report(self, report: AttemptReport) -> None:
        """Insert or replace the frozen report for one attempt (recomputed on re-grade)."""
        grading = report.grading.model_dump(mode="json")
        skills = [s.model_dump(mode="json") for s in report.skills]
        integrity = report.integrity.model_dump(mode="json") if report.integrity else None
        verdict = report.verdict.model_dump(mode="json") if report.verdict else None
        row = self.session.get(m.AttemptReportRow, report.attempt_id)
        if row is None:
            self.session.add(
                m.AttemptReportRow(
                    attempt_id=report.attempt_id,
                    graded_at=report.graded_at,
                    status=report.status,
                    grading=grading,
                    skills=skills,
                    summary=report.summary,
                    model_id=report.model_id,
                    error=report.error,
                    integrity=integrity,
                    verdict=verdict,
                )
            )
        else:
            row.graded_at = report.graded_at
            row.status = report.status
            row.grading = grading
            row.skills = skills
            row.summary = report.summary
            row.model_id = report.model_id
            row.error = report.error
            row.integrity = integrity
            row.verdict = verdict
        self.session.flush()
        log.info(
            "report upsert attempt=%s status=%s skills=%d error=%s suspicion=%s",
            report.attempt_id,
            report.status,
            len(report.skills),
            report.error or "-",
            f"{report.verdict.suspicion_score:.3f}" if report.verdict else "-",
        )

    def _report_from_row(self, row: m.AttemptReportRow) -> AttemptReport:
        return AttemptReport(
            attempt_id=row.attempt_id,
            graded_at=row.graded_at,
            status=row.status,  # type: ignore[arg-type]
            grading=GradingResult.model_validate(row.grading),
            skills=_SKILLS.validate_python(row.skills or []),
            summary=row.summary or "",
            model_id=row.model_id,
            error=row.error,
            integrity=IntegrityProfile.model_validate(row.integrity) if row.integrity else None,
            verdict=AnalysisVerdict.model_validate(row.verdict) if row.verdict else None,
        )

    def get_verdict(self, attempt_id: str) -> AnalysisVerdict | None:
        """The stored advisory verdict for one attempt (None: no report / no verdict).

        A plain column read — no LLM, no recompute (the verdict is computed once at
        finalize). Never touches the grade.
        """
        raw = self.session.execute(
            select(m.AttemptReportRow.verdict).where(
                m.AttemptReportRow.attempt_id == attempt_id
            )
        ).scalar_one_or_none()
        return AnalysisVerdict.model_validate(raw) if raw else None

    def get_report(self, attempt_id: str) -> AttemptReport | None:
        row = self.session.get(m.AttemptReportRow, attempt_id)
        if row is None:
            return None
        return self._report_from_row(row)

    def get_reports_for_test(self, test_id: str) -> dict[str, AttemptReport]:
        """Every frozen report for a test, keyed by attempt_id (one cheap query)."""
        rows = self.session.scalars(
            select(m.AttemptReportRow)
            .join(m.AttemptRow, m.AttemptRow.id == m.AttemptReportRow.attempt_id)
            .where(m.AttemptRow.test_id == test_id)
        ).all()
        log.info("report list test=%s -> %d", test_id, len(rows))
        return {r.attempt_id: self._report_from_row(r) for r in rows}

    def list_attempts_missing_report(
        self, test_id: str | None = None
    ) -> list[MissingReportCandidate]:
        """Finished attempts (submitted / expired) with no report or a `failed` one.

        The recovery query behind `ResultsService.list_missing`: a background
        finalize that crashed or was lost to a process restart leaves exactly this
        shape behind. Each candidate carries the failed report's status + `graded_at`
        (None when there is no report) so the caller can age-filter retries.
        Scoped to one test when `test_id` is given, else platform-wide.
        """
        stmt = (
            select(
                m.AttemptRow.id,
                m.AttemptReportRow.status,
                m.AttemptReportRow.graded_at,
                m.AttemptRow.submitted_at,
                m.AttemptRow.deadline,
            )
            .outerjoin(m.AttemptReportRow, m.AttemptReportRow.attempt_id == m.AttemptRow.id)
            .where(m.AttemptRow.status.in_(("submitted", "expired")))
            .where(
                or_(
                    m.AttemptReportRow.attempt_id.is_(None),
                    m.AttemptReportRow.status == "failed",
                )
            )
            .order_by(m.AttemptRow.submitted_at, m.AttemptRow.id)
        )
        if test_id is not None:
            stmt = stmt.where(m.AttemptRow.test_id == test_id)
        rows = [
            MissingReportCandidate(
                attempt_id=aid,
                report_status=status,
                graded_at=graded_at,
                finished_at=submitted_at or deadline,
            )
            for aid, status, graded_at, submitted_at, deadline in self.session.execute(stmt).all()
        ]
        log.info("attempts missing report test=%s -> %d", test_id or "*", len(rows))
        return rows

    def get_report_marker(self, attempt_id: str) -> ReportMarker | None:
        """The attempt's status + its report's status/`graded_at`, read with SQL.

        A plain column SELECT (never the identity map), so a check made right after
        taking the per-attempt finalize lock sees what a concurrent finalize just
        committed. None when the attempt does not exist.
        """
        row = self.session.execute(
            select(
                m.AttemptRow.status,
                m.AttemptReportRow.status,
                m.AttemptReportRow.graded_at,
                m.AttemptRow.submitted_at,
                m.AttemptRow.deadline,
            )
            .outerjoin(m.AttemptReportRow, m.AttemptReportRow.attempt_id == m.AttemptRow.id)
            .where(m.AttemptRow.id == attempt_id)
        ).first()
        if row is None:
            return None
        attempt_status, report_status, graded_at, submitted_at, deadline = row
        return ReportMarker(
            attempt_status=attempt_status,
            report_status=report_status,
            graded_at=graded_at,
            finished_at=submitted_at or deadline,
        )

    def count_events(self, attempt_id: str) -> int:
        """Cheap event count for the overview (avoids loading the whole stream)."""
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(m.IntegrityEventRow)
                .where(m.IntegrityEventRow.attempt_id == attempt_id)
            )
            or 0
        )

    def delete_roster_entry(self, entry_id: str) -> bool:
        """Remove a roster entry and its attempt (if any). Returns False if absent."""
        row = self.session.get(m.RosterEntryRow, entry_id)
        if row is None:
            return False
        if row.attempt_id:
            self.delete_attempt(row.attempt_id)
        self.session.execute(delete(m.RosterEntryRow).where(m.RosterEntryRow.id == entry_id))
        self.session.flush()
        log.info("roster delete id=%s", entry_id)
        return True

    def save_answer(self, answer: Answer) -> None:
        """Upsert the single answer for (attempt, item)."""
        row = self.session.get(m.AnswerRow, (answer.attempt_id, answer.item_id))
        if row is None:
            self.session.add(
                m.AnswerRow(
                    attempt_id=answer.attempt_id,
                    item_id=answer.item_id,
                    response=answer.response,
                    answered_at=answer.answered_at,
                )
            )
        else:
            row.response = answer.response
            row.answered_at = answer.answered_at
        self.session.flush()
        log.info("answer save attempt=%s item=%s", answer.attempt_id, answer.item_id)

    def get_answers(self, attempt_id: str) -> list[Answer]:
        rows = self.session.scalars(
            select(m.AnswerRow)
            .where(m.AnswerRow.attempt_id == attempt_id)
            .order_by(m.AnswerRow.item_id)
        ).all()
        log.info("answer list attempt=%s -> %d", attempt_id, len(rows))
        return [
            Answer(
                attempt_id=r.attempt_id,
                item_id=r.item_id,
                response=r.response,
                answered_at=r.answered_at,
            )
            for r in rows
        ]


# --------------------------------------------------------------------------- #
# Telemetry events (append-only, rule #6).
# --------------------------------------------------------------------------- #
class EventRepository:
    """Append-only store for integrity events; stamps `server_ts` on ingest."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add_event(self, event: IntegrityEvent) -> IntegrityEvent:
        server_ts = event.server_ts or datetime.now(UTC)
        self.session.add(
            m.IntegrityEventRow(
                attempt_id=event.attempt_id,
                item_id=event.item_id,
                type=event.type,
                client_ts=event.client_ts,
                server_ts=server_ts,
                duration_ms=event.duration_ms,
                payload=event.payload,
            )
        )
        self.session.flush()
        # WARNING level per CLAUDE.md: integrity events are surfaced loudly.
        log.warning("event ingest attempt=%s type=%s", event.attempt_id, event.type)
        return event.model_copy(update={"server_ts": server_ts})

    def list_events(self, attempt_id: str) -> list[IntegrityEvent]:
        rows = self.session.scalars(
            select(m.IntegrityEventRow)
            .where(m.IntegrityEventRow.attempt_id == attempt_id)
            .order_by(m.IntegrityEventRow.id)
        ).all()
        log.info("event list attempt=%s -> %d", attempt_id, len(rows))
        return [
            IntegrityEvent(
                attempt_id=r.attempt_id,
                item_id=r.item_id,
                type=r.type,  # type: ignore[arg-type]
                client_ts=r.client_ts,
                server_ts=r.server_ts,
                duration_ms=r.duration_ms,
                payload=r.payload,
            )
            for r in rows
        ]
