"""Telemetry ingest HTTP surface (Phase 6).

A single append-only endpoint the browser recorder posts to. It owns no logic of
its own beyond wiring `EventBatch` → `TelemetryService` → `EventRepository`; the
server stamps `server_ts` and the repository logs a WARNING per event.

Capture only (golden rule #6): this router never reads grading or integrity.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.core.logging import get_logger
from app.persistence.repository import EventRepository
from app.telemetry import EventBatch, TelemetryService

log = get_logger(__name__)

router = APIRouter(tags=["telemetry"])


class IngestResponse(BaseModel):
    """How many events from the batch were appended to the stream."""

    ingested: int


def get_telemetry_service(
    session: Annotated[Session, Depends(get_session)],
) -> Iterator[TelemetryService]:
    yield TelemetryService(EventRepository(session))


@router.post("/attempts/{attempt_id}/events", response_model=IngestResponse)
def ingest_events(
    attempt_id: str,
    batch: EventBatch,
    service: Annotated[TelemetryService, Depends(get_telemetry_service)],
) -> IngestResponse:
    """Append a batch of recorder events for an attempt (append-only).

    Deliberately does NOT validate that `attempt_id` exists or auth the caller:
    telemetry is decoupled (golden rule #6) and imports no delivery/roster code,
    and the recorder must never be blocked from posting. Caller auth + attempt
    validation + rate-limiting belong to the delivery/admin surface (Phases 10/11)
    that issues attempt links; this endpoint stays a thin capture sink.
    """
    log.info("POST /attempts/%s/events count=%d", attempt_id, len(batch.events))
    stored = service.record_batch(attempt_id, batch)
    return IngestResponse(ingested=len(stored))
