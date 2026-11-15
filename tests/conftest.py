"""Shared test fixtures.

`session` gives each test an isolated, in-memory sqlite DB with the full schema
created from `Base.metadata` (the same metadata the Alembic migration is
generated from), so repository round-trips run against a real DB engine.

`_no_recovery_sweep` (autouse) sets `SKIP_STARTUP_SWEEP=1` for every test, so no
`TestClient(app)` lifespan (nor a uvicorn subprocess inheriting the environment)
can start the real recovery loop against the developer's `english_exam.db`. Tests
that exercise the loop delete/override the variable themselves via `monkeypatch`.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.persistence.models  # noqa: F401  (register models on Base.metadata)
from app.core.db import Base


@pytest.fixture(autouse=True)
def _no_recovery_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKIP_STARTUP_SWEEP", "1")


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
