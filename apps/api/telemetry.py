"""Telemetry ingest HTTP surface (Phase 6).

A single append-only endpoint the browser recorder posts to. It owns no logic of
its own beyond wiring `EventBatch` → `TelemetryService` → `EventRepository`; the
server stamps `server_ts` and the repository logs a WARNING per event.

Capture only (golden rule #6): this router never reads grading or integrity.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.core.logging import get_logger
from app.persistence import models as m
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


def _client_ip(request: Request) -> str | None:
    """Best-effort client IP: first hop of X-Forwarded-For, else the socket peer.

    Server-observed and never trusted from the event body — the same principle as
    `server_ts` (the client cannot forge it). Only attached to `device_info`.
    """
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


@router.post("/attempts/{attempt_id}/events", response_model=IngestResponse)
def ingest_events(
    attempt_id: str,
    batch: EventBatch,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    service: Annotated[TelemetryService, Depends(get_telemetry_service)],
) -> IngestResponse:
    """Append a batch of recorder events for an attempt (append-only).

    Only checks that the attempt row exists (404 otherwise): the event stream has
    a foreign key to `attempts`, enforced on every engine now, so a bogus id would
    otherwise fail the whole batch with a 500. It still does not auth the caller
    and imports no delivery/grading/integrity code (golden rule #6) — rate-limiting
    and link auth belong to the delivery/admin surface.

    The server-observed client IP is stamped onto `device_info` events only (it is
    server-side, like `server_ts`, never read from the event body).
    """
    log.info("POST /attempts/%s/events count=%d", attempt_id, len(batch.events))
    if session.get(m.AttemptRow, attempt_id) is None:
        log.warning("event ingest rejected: unknown attempt=%s", attempt_id)
        raise HTTPException(status_code=404, detail="attempt not found")
    stored = service.record_batch(attempt_id, batch, client_ip=_client_ip(request))
    return IngestResponse(ingested=len(stored))
