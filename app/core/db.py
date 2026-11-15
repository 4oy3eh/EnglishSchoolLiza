"""Database session layer (Phase 2).

A single sync SQLAlchemy 2.0 engine + session factory, plus the declarative
`Base` that every ORM model inherits. Keep persistence concerns here; the
contracts in `contracts/` remain the single source of truth for shape
(CLAUDE.md golden rule #4) and the ORM models mirror them.

Dev/test default to a local sqlite file (see `Settings.database_url`); prod
swaps in Postgres purely via the `DATABASE_URL` env var.

SQLite hardening (Phase 13): every sqlite connection gets WAL journaling (readers
never block the writer), `synchronous=NORMAL` (durable enough under WAL, far
fewer fsyncs), a 10 s `busy_timeout` (concurrent uvicorn workers / the loader
wait instead of failing with "database is locked"), and `foreign_keys=ON` so the
`ON DELETE CASCADE` the models declare is actually enforced. Postgres is untouched.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

# (pragma, value) applied on every new sqlite DBAPI connection, in order.
SQLITE_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("busy_timeout", "10000"),
    ("foreign_keys", "ON"),
)
# Mirrors busy_timeout: the DBAPI-level lock wait, in seconds.
SQLITE_CONNECT_TIMEOUT_S = 10


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def is_sqlite_url(url: str) -> bool:
    return url.startswith("sqlite")


def _apply_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """`connect` listener: set the pragmas on a fresh sqlite connection.

    Each pragma is guarded on its own: an in-memory DB answers `journal_mode=WAL`
    with "memory" (harmless) and a read-only/odd build might reject one, which
    must never take the whole connection down.
    """
    cursor = dbapi_connection.cursor()
    try:
        for name, value in SQLITE_PRAGMAS:
            try:
                cursor.execute(f"PRAGMA {name}={value}")
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("sqlite pragma %s=%s failed: %s", name, value, exc)
    finally:
        cursor.close()


def _make_engine(url: str, echo: bool) -> Engine:
    if not is_sqlite_url(url):
        log.info("db engine init url=%s", url)
        return create_engine(url, echo=echo, future=True)

    # sqlite needs check_same_thread off so a session can move across threads
    # (e.g. the test client / async workers); `timeout` is the DBAPI lock wait.
    connect_args = {"check_same_thread": False, "timeout": SQLITE_CONNECT_TIMEOUT_S}
    engine = create_engine(url, echo=echo, future=True, connect_args=connect_args)
    event.listen(engine, "connect", _apply_sqlite_pragmas)
    log.info(
        "db engine init url=%s sqlite pragmas=[%s] connect_timeout=%ss",
        url,
        ", ".join(f"{k}={v}" for k, v in SQLITE_PRAGMAS),
        SQLITE_CONNECT_TIMEOUT_S,
    )
    return engine


engine: Engine = _make_engine(settings.database_url, settings.db_echo)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on error, always close."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        log.exception("db session rolled back")
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency: yields a session and closes it after the request."""
    with session_scope() as session:
        yield session
