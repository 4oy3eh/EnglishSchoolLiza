"""Thin repositories: translate between `contracts/` Pydantic models and rows.

One repository per aggregate (content, attempts, events). They are deliberately
thin — create + read round-trips with INFO logging at the persistence boundary
(CLAUDE.md logging rules). Higher-level engines (content, delivery, telemetry)
build their domain logic on top of these in later phases.

Every method takes/returns contract models, never ORM rows, so the rest of the
app only ever speaks contracts and the source-of-truth shape can't leak.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.persistence import models as m
from contracts import (
    Answer,
    Attempt,
    IntegrityEvent,
    Item,
    RosterEntry,
    Section,
    Stimulus,
    Test,
)
from contracts.content import PublishStatus

log = get_logger(__name__)

# Validators for the polymorphic parts persisted as JSON.
_ITEM: TypeAdapter[Item] = TypeAdapter(Item)
_STIMULUS: TypeAdapter[Stimulus] = TypeAdapter(Stimulus)

# Shared item fields promoted to columns; everything else is the answer-key data.
_ITEM_COLUMN_FIELDS = ("id", "item_type", "prompt")


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

    # -- row <-> contract helpers ------------------------------------------- #
    def _test_from_row(self, row: m.TestRow) -> Test:
        return Test(
            id=row.id,
            title=row.title,
            level=row.level,  # type: ignore[arg-type]
            status=row.status,  # type: ignore[arg-type]
            duration_minutes=row.duration_minutes,
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
        self.session.add(
            m.RosterEntryRow(
                id=entry.id,
                test_id=entry.test_id,
                display_name=entry.display_name,
                status=entry.status,
                attempt_id=entry.attempt_id,
            )
        )
        self.session.flush()
        log.info("roster add id=%s test=%s", entry.id, entry.test_id)
        return entry.id

    def get_roster_entry(self, entry_id: str) -> RosterEntry | None:
        row = self.session.get(m.RosterEntryRow, entry_id)
        if row is None:
            return None
        return RosterEntry(
            id=row.id,
            test_id=row.test_id,
            display_name=row.display_name,
            status=row.status,  # type: ignore[arg-type]
            attempt_id=row.attempt_id,
        )

    def list_roster_entries(self, test_id: str) -> list[RosterEntry]:
        rows = self.session.scalars(
            select(m.RosterEntryRow)
            .where(m.RosterEntryRow.test_id == test_id)
            .order_by(m.RosterEntryRow.display_name)
        ).all()
        log.info("roster list test=%s -> %d", test_id, len(rows))
        return [
            RosterEntry(
                id=r.id,
                test_id=r.test_id,
                display_name=r.display_name,
                status=r.status,  # type: ignore[arg-type]
                attempt_id=r.attempt_id,
            )
            for r in rows
        ]

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

    def get_attempt(self, attempt_id: str) -> Attempt | None:
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
        )

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
