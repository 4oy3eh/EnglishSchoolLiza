"""Phase 13 P0 persistence hardening.

* `app/core/db.py` — sqlite engines get WAL / synchronous=NORMAL / busy_timeout /
  foreign_keys=ON, and the declared `ON DELETE CASCADE` therefore really runs,
  so every delete path (`delete_attempt`, `delete_roster_entry`,
  `load_test._clear_existing`) must survive FK enforcement.
* `apps/api/main.py` — the startup migration check: at head -> ok; versioned but
  behind -> RuntimeError (unless ALLOW_UNMIGRATED_DB=1); create_all schema with
  no `alembic_version` table -> warning only.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

import apps.api.main as main_mod
from app.content.load_test import _clear_existing
from app.core.db import SQLITE_PRAGMAS, Base, _make_engine, is_sqlite_url
from app.persistence import models as m
from app.persistence.repository import AttemptRepository, ContentRepository, EventRepository
from apps.api.main import ALLOW_UNMIGRATED_ENV, app, check_db_migrated, script_head
from contracts import (
    Answer,
    Attempt,
    IntegrityEvent,
    PassageTextStimulus,
    RosterEntry,
    Section,
    SingleChoiceItem,
    TextOption,
)
from contracts import Test as ExamTest  # aliased: avoid pytest collecting `Test*`

_INITIAL_REV = "495ca9c7b0ec"  # the first migration — always behind head


# --------------------------------------------------------------------------- #
# helpers / fixtures
# --------------------------------------------------------------------------- #
def _pragma(engine: Engine, name: str) -> object:
    with engine.connect() as conn:
        return conn.exec_driver_sql(f"PRAGMA {name}").scalar()


def _count(session: Session, table: type[Base]) -> int:
    return session.scalar(select(func.count()).select_from(table)) or 0


def _stamp(engine: Engine, revision: str) -> None:
    """Minimal `alembic stamp` without running env.py (which binds to settings)."""
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS alembic_version "
            "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        conn.exec_driver_sql("DELETE FROM alembic_version")
        conn.exec_driver_sql("INSERT INTO alembic_version (version_num) VALUES (?)", (revision,))


@pytest.fixture
def file_engine(tmp_path: Path) -> Iterator[Engine]:
    """A hardened (pragma'd) sqlite *file* engine — WAL only applies on disk."""
    engine = _make_engine(f"sqlite:///{(tmp_path / 'hard.db').as_posix()}", False)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def fk_session(file_engine: Engine) -> Iterator[Session]:
    """Session on a schema where sqlite really enforces the declared FKs."""
    Base.metadata.create_all(file_engine)
    db = sessionmaker(bind=file_engine, autoflush=False, expire_on_commit=False)()
    try:
        yield db
    finally:
        db.close()


def _small_test(test_id: str = "fk-t") -> ExamTest:
    return ExamTest(
        id=test_id,
        title="FK cascade",
        level="A2_KEY",
        status="published",
        duration_minutes=10,
        sections=[
            Section(
                id=f"{test_id}-s1",
                skill="reading",
                stimulus=PassageTextStimulus(text="Read."),
                items=[
                    SingleChoiceItem(
                        id=f"{test_id}-q1",
                        prompt="?",
                        options=[TextOption(key="A", text="a"), TextOption(key="B", text="b")],
                        correct="A",
                    )
                ],
            )
        ],
    )


def _seed_attempt_graph(session: Session, test_id: str = "fk-t") -> str:
    """Test + roster entry + attempt with every per-attempt dependent row."""
    now = datetime.now(UTC)
    ContentRepository(session).add_test(_small_test(test_id))
    attempts = AttemptRepository(session)
    attempts.add_roster_entry(RosterEntry(id=f"{test_id}-r", test_id=test_id, display_name="A"))
    attempt_id = f"{test_id}-a"
    attempts.add_attempt(
        Attempt(id=attempt_id, test_id=test_id, roster_entry_id=f"{test_id}-r", seed=7)
    )
    attempts.update_roster_entry(
        RosterEntry(
            id=f"{test_id}-r",
            test_id=test_id,
            display_name="A",
            status="in_progress",
            attempt_id=attempt_id,
        )
    )
    attempts.save_answer(
        Answer(attempt_id=attempt_id, item_id=f"{test_id}-q1", response="A", answered_at=now)
    )
    EventRepository(session).add_event(
        IntegrityEvent(attempt_id=attempt_id, type="visibility_hidden", client_ts=now)
    )
    session.add(
        m.ManualGradeRow(attempt_id=attempt_id, item_id=f"{test_id}-q1", awarded=1.0, graded_at=now)
    )
    session.add(m.AttemptReportRow(attempt_id=attempt_id, graded_at=now, grading={}, skills=[]))
    session.flush()
    return attempt_id


def _dependent_counts(session: Session) -> dict[str, int]:
    return {
        "answers": _count(session, m.AnswerRow),
        "events": _count(session, m.IntegrityEventRow),
        "grades": _count(session, m.ManualGradeRow),
        "reports": _count(session, m.AttemptReportRow),
        "attempts": _count(session, m.AttemptRow),
    }


# --------------------------------------------------------------------------- #
# 1. sqlite pragmas
# --------------------------------------------------------------------------- #
def test_is_sqlite_url() -> None:
    assert is_sqlite_url("sqlite:///./x.db") and is_sqlite_url("sqlite://")
    assert not is_sqlite_url("postgresql+psycopg://u:p@h/db")


def test_sqlite_file_engine_applies_all_pragmas(file_engine: Engine) -> None:
    assert _pragma(file_engine, "journal_mode") == "wal"
    assert _pragma(file_engine, "synchronous") == 1  # NORMAL
    assert _pragma(file_engine, "busy_timeout") == 10000
    assert _pragma(file_engine, "foreign_keys") == 1
    assert dict(SQLITE_PRAGMAS)["busy_timeout"] == "10000"


def test_sqlite_memory_engine_survives_wal_and_enforces_fks() -> None:
    engine = _make_engine("sqlite://", False)
    try:
        # WAL is meaningless in memory (sqlite answers "memory") — must not error.
        assert _pragma(engine, "journal_mode") == "memory"
        assert _pragma(engine, "foreign_keys") == 1
        assert _pragma(engine, "busy_timeout") == 10000
    finally:
        engine.dispose()


def test_sqlite_engine_init_logs_pragmas_once(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app.core.db"):
        engine = _make_engine("sqlite://", False)
        engine.dispose()
    lines = [r.getMessage() for r in caplog.records if "db engine init" in r.getMessage()]
    assert len(lines) == 1
    assert "foreign_keys=ON" in lines[0] and "journal_mode=WAL" in lines[0]


def test_fk_on_rejects_dangling_attempt(fk_session: Session) -> None:
    fk_session.add(m.AttemptRow(id="dangling", test_id="nope", roster_entry_id="nope", seed=1))
    with pytest.raises(IntegrityError):
        fk_session.flush()
    fk_session.rollback()


# --------------------------------------------------------------------------- #
# 2. delete paths under FK enforcement
# --------------------------------------------------------------------------- #
def test_delete_attempt_with_fk_on_clears_all_dependents(fk_session: Session) -> None:
    attempt_id = _seed_attempt_graph(fk_session)
    assert _dependent_counts(fk_session) == {
        "answers": 1,
        "events": 1,
        "grades": 1,
        "reports": 1,
        "attempts": 1,
    }

    assert AttemptRepository(fk_session).delete_attempt(attempt_id) is True

    assert all(v == 0 for v in _dependent_counts(fk_session).values())
    assert _count(fk_session, m.RosterEntryRow) == 1  # roster untouched by design


def test_delete_roster_entry_with_fk_on(fk_session: Session) -> None:
    _seed_attempt_graph(fk_session)

    assert AttemptRepository(fk_session).delete_roster_entry("fk-t-r") is True

    assert _count(fk_session, m.RosterEntryRow) == 0
    assert all(v == 0 for v in _dependent_counts(fk_session).values())
    assert ContentRepository(fk_session).get_test("fk-t") is not None


def test_clear_existing_with_fk_on_wipes_test_graph(fk_session: Session) -> None:
    _seed_attempt_graph(fk_session)

    _clear_existing(fk_session, "fk-t")

    assert all(v == 0 for v in _dependent_counts(fk_session).values())
    assert _count(fk_session, m.RosterEntryRow) == 0
    assert _count(fk_session, m.SectionRow) == 0
    assert _count(fk_session, m.ItemRow) == 0
    assert ContentRepository(fk_session).get_test("fk-t") is None


def test_orm_delete_test_with_fk_on_cascades_roster_and_attempts(fk_session: Session) -> None:
    """Golden rule #4 side effect: the DDL cascade now does what the models declare."""
    _seed_attempt_graph(fk_session)

    assert ContentRepository(fk_session).delete_test("fk-t") is True

    assert _count(fk_session, m.RosterEntryRow) == 0
    assert all(v == 0 for v in _dependent_counts(fk_session).values())


# --------------------------------------------------------------------------- #
# 3. startup migration check
# --------------------------------------------------------------------------- #
def test_script_head_is_resolved_from_repo_ini() -> None:
    head = script_head()
    assert isinstance(head, str) and head


def test_migration_check_ok_at_head(file_engine: Engine, caplog: pytest.LogCaptureFixture) -> None:
    Base.metadata.create_all(file_engine)
    head = script_head()
    assert head is not None
    _stamp(file_engine, head)

    with caplog.at_level(logging.INFO, logger="apps.api.main"):
        assert check_db_migrated(file_engine, allow_unmigrated=False) == "ok"
    assert any("at head" in r.getMessage() for r in caplog.records)


def test_migration_check_behind_head_refuses_to_start(
    file_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    Base.metadata.create_all(file_engine)
    _stamp(file_engine, _INITIAL_REV)

    with caplog.at_level(logging.ERROR, logger="apps.api.main"):
        with pytest.raises(RuntimeError, match="make migrate") as exc:
            check_db_migrated(file_engine, allow_unmigrated=False)
    assert _INITIAL_REV in str(exc.value) and str(script_head()) in str(exc.value)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and "make migrate" in errors[0].getMessage()


def test_migration_check_behind_head_env_override(
    file_engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    Base.metadata.create_all(file_engine)
    _stamp(file_engine, _INITIAL_REV)
    monkeypatch.setenv(ALLOW_UNMIGRATED_ENV, "1")

    with caplog.at_level(logging.WARNING, logger="apps.api.main"):
        assert check_db_migrated(file_engine) == "behind"
    assert any(ALLOW_UNMIGRATED_ENV in r.getMessage() for r in caplog.records)

    monkeypatch.setenv(ALLOW_UNMIGRATED_ENV, "0")
    with pytest.raises(RuntimeError):
        check_db_migrated(file_engine)


def test_migration_check_create_all_schema_without_version_table_warns_only(
    file_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    Base.metadata.create_all(file_engine)  # what conftest / e2e / loaders do

    with caplog.at_level(logging.WARNING, logger="apps.api.main"):
        assert check_db_migrated(file_engine, allow_unmigrated=False) == "unversioned"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings and "no alembic_version table" in warnings[0].getMessage()
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]


def test_migration_check_empty_db_is_not_the_create_all_case(file_engine: Engine) -> None:
    # No tables at all: nothing to serve from — that is "run make migrate", not a warning.
    with pytest.raises(RuntimeError, match="make migrate"):
        check_db_migrated(file_engine, allow_unmigrated=False)


def test_lifespan_runs_migration_check_against_app_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Engine] = []
    monkeypatch.setattr(main_mod, "check_db_migrated", lambda e: seen.append(e))
    # Never run the real recovery sweep against the developer's configured DB.
    monkeypatch.setenv(main_mod.SKIP_SWEEP_ENV, "1")

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert seen == [main_mod.engine]


def test_lifespan_propagates_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse(_engine: Engine) -> None:
        raise RuntimeError("database schema is behind")

    monkeypatch.setattr(main_mod, "check_db_migrated", _refuse)
    with pytest.raises(RuntimeError, match="behind"):
        with TestClient(app):
            pass
