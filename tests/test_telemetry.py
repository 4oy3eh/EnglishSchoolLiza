"""Phase 6: telemetry engine — append-only ingest, capture only.

Gate (docs/PROMPTS.md Prompt 6): events persist with both `client_ts` and a
server-stamped `server_ts`; one WARNING is logged per integrity event; the engine
never reaches into grading/integrity/analysis. The Playwright tab-backgrounding
E2E lives in `test_telemetry_e2e.py`.
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.persistence.repository import EventRepository
from app.telemetry import EventBatch, TelemetryService
from app.telemetry.schema import ClientEvent
from apps.api.main import app

CLIENT_TS = datetime(2026, 6, 21, 12, 0, 30, tzinfo=UTC)


def _batch() -> EventBatch:
    return EventBatch(
        events=[
            ClientEvent(type="visibility_hidden", client_ts=CLIENT_TS, item_id="q-1"),
            ClientEvent(
                type="visibility_visible",
                client_ts=CLIENT_TS,
                item_id="q-1",
                duration_ms=4200,
            ),
        ]
    )


# --------------------------------------------------------------------------- #
# Service layer (direct repository).
# --------------------------------------------------------------------------- #
def test_record_batch_persists_both_timestamps(session: Session) -> None:
    service = TelemetryService(EventRepository(session))
    before = datetime.now(UTC)

    stored = service.record_batch("att-1", _batch())

    assert len(stored) == 2
    for event in stored:
        assert event.client_ts == CLIENT_TS  # captured as sent
        assert event.server_ts is not None  # stamped on ingest
        assert event.server_ts >= before  # server time, not client time
        assert event.attempt_id == "att-1"

    # Round-trips out of the append-only store with both timestamps intact.
    read_back = service.list_events("att-1")
    assert [e.type for e in read_back] == ["visibility_hidden", "visibility_visible"]
    assert read_back[1].duration_ms == 4200
    assert all(e.client_ts == CLIENT_TS and e.server_ts is not None for e in read_back)


def test_append_only_accumulates(session: Session) -> None:
    service = TelemetryService(EventRepository(session))
    service.record_batch("att-1", _batch())
    service.record_batch("att-1", _batch())
    assert len(service.list_events("att-1")) == 4


def test_full_capture_surface_round_trips(session: Session) -> None:
    # The gate names interaction / answer_change / audio_play / audio_seek as
    # required capture surface; assert each persists through the service with its
    # item_id and payload intact (capture only — no interpretation).
    service = TelemetryService(EventRepository(session))
    batch = EventBatch(
        events=[
            ClientEvent(type="interaction", client_ts=CLIENT_TS, item_id="q-2"),
            ClientEvent(
                type="answer_change",
                client_ts=CLIENT_TS,
                item_id="q-2",
                payload={"value": "B"},
            ),
            ClientEvent(
                type="audio_play",
                client_ts=CLIENT_TS,
                item_id="sec-1",
                payload={"position": 0.0},
            ),
            ClientEvent(
                type="audio_seek",
                client_ts=CLIENT_TS,
                item_id="sec-1",
                payload={"position": 12.5},
            ),
            ClientEvent(type="window_blur", client_ts=CLIENT_TS),
            ClientEvent(type="pagehide", client_ts=CLIENT_TS),
        ]
    )
    service.record_batch("att-cap", batch)

    stored = service.list_events("att-cap")
    assert [e.type for e in stored] == [
        "interaction",
        "answer_change",
        "audio_play",
        "audio_seek",
        "window_blur",
        "pagehide",
    ]
    by_type = {e.type: e for e in stored}
    assert by_type["answer_change"].payload == {"value": "B"}
    assert by_type["audio_seek"].payload == {"position": 12.5}
    assert by_type["audio_seek"].item_id == "sec-1"


def test_one_warning_logged_per_event(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    service = TelemetryService(EventRepository(session))
    with caplog.at_level(logging.WARNING):
        service.record_batch("att-1", _batch())
    warnings = [r for r in caplog.records if r.message.startswith("event ingest")]
    assert len(warnings) == 2
    assert all(r.levelno == logging.WARNING for r in warnings)


# --------------------------------------------------------------------------- #
# HTTP ingest endpoint.
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_session] = lambda: session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_ingest_endpoint_persists_events(client: TestClient, session: Session) -> None:
    resp = client.post(
        "/attempts/att-http/events", json=_batch().model_dump(mode="json")
    )
    assert resp.status_code == 200
    assert resp.json() == {"ingested": 2}

    stored = EventRepository(session).list_events("att-http")
    assert len(stored) == 2
    assert stored[0].server_ts is not None


def test_client_cannot_forge_server_ts(client: TestClient) -> None:
    # `ClientEvent` forbids extra fields, so a posted `server_ts` is rejected
    # outright — the client can never supply the trusted timestamp.
    resp = client.post(
        "/attempts/att-http/events",
        json={
            "events": [
                {
                    "type": "visibility_hidden",
                    "client_ts": CLIENT_TS.isoformat(),
                    "server_ts": "2000-01-01T00:00:00+00:00",
                }
            ]
        },
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Invariant #6: telemetry captures only — no grading/integrity/analysis.
# --------------------------------------------------------------------------- #
def test_telemetry_engine_does_not_judge() -> None:
    # Check actual imports (not prose), so the engine's docstrings can freely
    # *name* the downstream layers while never importing them.
    forbidden = ("app.grading", "app.integrity", "app.analysis")
    telemetry_dir = Path(__file__).resolve().parent.parent / "app" / "telemetry"
    for path in telemetry_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(forbidden), path.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden), path.name
