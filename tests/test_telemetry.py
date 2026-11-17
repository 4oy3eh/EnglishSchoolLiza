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
from app.persistence.repository import AttemptRepository, ContentRepository, EventRepository
from app.telemetry import EventBatch, TelemetryService
from app.telemetry.schema import ClientEvent
from apps.api.main import app
from contracts import Attempt, GapFillItem, PassageTextStimulus, RosterEntry, Section
from contracts import Test as ExamTest

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


def test_one_warning_logged_per_event(session: Session, caplog: pytest.LogCaptureFixture) -> None:
    service = TelemetryService(EventRepository(session))
    with caplog.at_level(logging.WARNING):
        service.record_batch("att-1", _batch())
    warnings = [r for r in caplog.records if r.message.startswith("event ingest")]
    assert len(warnings) == 2
    assert all(r.levelno == logging.WARNING for r in warnings)


# --------------------------------------------------------------------------- #
# HTTP ingest endpoint.
# --------------------------------------------------------------------------- #
def _seed_attempt(session: Session, attempt_id: str = "att-http") -> None:
    """A minimal published test + roster entry + attempt so FK-checked ingest works."""
    ContentRepository(session).add_test(
        ExamTest(
            id="t-tel",
            title="tel",
            level="A2_KEY",
            status="published",
            duration_minutes=10,
            sections=[
                Section(
                    id="s1",
                    skill="reading",
                    stimulus=PassageTextStimulus(text="Read."),
                    items=[GapFillItem(id="i1", prompt="gap", accepted=["a"])],
                )
            ],
        )
    )
    attempts = AttemptRepository(session)
    attempts.add_roster_entry(RosterEntry(id="e-tel", test_id="t-tel", display_name="Ann"))
    attempts.add_attempt(
        Attempt(
            id=attempt_id,
            test_id="t-tel",
            roster_entry_id="e-tel",
            status="in_progress",
            seed=1,
            started_at=CLIENT_TS,
            deadline=CLIENT_TS,
        )
    )
    attempts.add_attempt_session(attempt_id, SESSION_TOKEN)
    session.commit()


SESSION_TOKEN = "tel-session-token"


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    _seed_attempt(session)
    app.dependency_overrides[get_session] = lambda: session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("headers", [{}, {"X-Attempt-Token": SESSION_TOKEN}])
def test_ingest_unknown_attempt_is_401_like_a_bad_token(
    client: TestClient, session: Session, headers: dict
) -> None:
    """Token-first: an unknown attempt answers exactly like a wrong token (401), even
    with another attempt's valid token, so the ingest never confirms an attempt id."""
    resp = client.post(
        "/attempts/no-such/events", json=_batch().model_dump(mode="json"), headers=headers
    )
    bad = client.post(
        "/attempts/att-http/events",
        json=_batch().model_dump(mode="json"),
        headers={"X-Attempt-Token": "nope"},
    )
    assert resp.status_code == bad.status_code == 401
    assert resp.json() == bad.json()
    assert EventRepository(session).list_events("no-such") == []


def test_ingest_endpoint_persists_events(client: TestClient, session: Session) -> None:
    resp = client.post(
        "/attempts/att-http/events",
        json=_batch().model_dump(mode="json"),
        headers={"X-Attempt-Token": SESSION_TOKEN},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ingested": 2}

    stored = EventRepository(session).list_events("att-http")
    assert len(stored) == 2
    assert stored[0].server_ts is not None


@pytest.mark.parametrize("headers", [{}, {"X-Attempt-Token": ""}, {"X-Attempt-Token": "nope"}])
def test_ingest_without_a_valid_session_token_is_401_and_dropped(
    client: TestClient, session: Session, headers: dict, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        resp = client.post(
            "/attempts/att-http/events", json=_batch().model_dump(mode="json"), headers=headers
        )
    assert resp.status_code == 401
    assert EventRepository(session).list_events("att-http") == []
    assert any("event ingest rejected" in r.getMessage() for r in caplog.records)


def test_ingest_accepts_the_beacon_query_token(client: TestClient, session: Session) -> None:
    # navigator.sendBeacon cannot set headers: the recorder puts the token in ?token=.
    resp = client.post(
        f"/attempts/att-http/events?token={SESSION_TOKEN}", json=_batch().model_dump(mode="json")
    )
    assert resp.status_code == 200
    assert len(EventRepository(session).list_events("att-http")) == 2


def test_ingest_refuses_another_attempts_token(client: TestClient, session: Session) -> None:
    repo = AttemptRepository(session)
    repo.add_roster_entry(RosterEntry(id="e-tel2", test_id="t-tel", display_name="Ben"))
    repo.add_attempt(
        Attempt(
            id="att-other", test_id="t-tel", roster_entry_id="e-tel2", status="in_progress", seed=2
        )
    )
    repo.add_attempt_session("att-other", "other-token")
    session.commit()
    resp = client.post(
        "/attempts/att-http/events",
        json=_batch().model_dump(mode="json"),
        headers={"X-Attempt-Token": "other-token"},
    )
    assert resp.status_code == 401
    assert EventRepository(session).list_events("att-http") == []


def test_access_log_masks_the_beacon_token() -> None:
    from apps.api.telemetry import _RedactTokenFilter

    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5", "POST", "/attempts/a1/events?token=SECRETabc_-1", "1.1", 200), None,
    )
    assert _RedactTokenFilter().filter(record)
    assert "SECRET" not in record.getMessage()
    assert "token=***" in record.getMessage()
    access_filters = logging.getLogger("uvicorn.access").filters
    assert any(isinstance(f, _RedactTokenFilter) for f in access_filters)


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
