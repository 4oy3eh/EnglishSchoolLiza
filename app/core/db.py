"""Database session layer (Phase 2).

A single sync SQLAlchemy 2.0 engine + session factory, plus the declarative
`Base` that every ORM model inherits. Keep persistence concerns here; the
contracts in `contracts/` remain the single source of truth for shape
(CLAUDE.md golden rule #4) and the ORM models mirror them.

Dev/test default to a local sqlite file (see `Settings.database_url`); prod
swaps in Postgres purely via the `DATABASE_URL` env var.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _make_engine(url: str, echo: bool) -> Engine:
    # sqlite needs check_same_thread off so a session can move across threads
    # (e.g. the test client / async workers later).
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    log.info("db engine init url=%s", url)
    return create_engine(url, echo=echo, future=True, connect_args=connect_args)


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
