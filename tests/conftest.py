"""Shared test fixtures.

`session` gives each test an isolated, in-memory sqlite DB with the full schema
created from `Base.metadata` (the same metadata the Alembic migration is
generated from), so repository round-trips run against a real DB engine.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.persistence.models  # noqa: F401  (register models on Base.metadata)
from app.core.db import Base


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # one shared in-memory connection
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()
