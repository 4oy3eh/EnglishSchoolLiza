"""Telemetry ingest HTTP surface (Phase 6).

A single append-only endpoint the browser recorder posts to. It owns no logic of
its own beyond wiring `EventBatch` → `TelemetryService` → `EventRepository`; the
server stamps `server_ts` and the repository logs a WARNING per event.

Capture only (golden rule #6): this router never reads grading or integrity.

Phase 14: the batch must carry the attempt's session token (issued by the PIN
start in `/exam`), in the X-Attempt-Token header (fetch) or the `token` query
parameter (`navigator.sendBeacon` cannot set headers). Missing / wrong -> 401 and
the batch is dropped. The check is a hash lookup through `AttemptRepository` (no
delivery import). The query form would otherwise land in uvicorn's access log, so
`_RedactTokenFilter` masks `token=` there.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.core.logging import get_logger
from app.persistence import models as m
from app.persistence.repository import AttemptRepository, EventRepository
from app.telemetry import EventBatch, TelemetryService

log = get_logger(__name__)

router = APIRouter(tags=["telemetry"])

_TOKEN_IN_QUERY = re.compile(r"([?&]token=)[^&\s\"]+")


class _RedactTokenFilter(logging.Filter):
    """Mask `token=...` in uvicorn access-log lines (the beacon's query token)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _TOKEN_IN_QUERY.sub(r"\g<1>***", a) if isinstance(a, str) else a
                for a in record.args
            )
        return True


_access_log = logging.getLogger("uvicorn.access")
if not any(isinstance(f, _RedactTokenFilter) for f in _access_log.filters):
    _access_log.addFilter(_RedactTokenFilter())


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
    x_attempt_token: Annotated[str | None, Header(alias="X-Attempt-Token")] = None,
    token: Annotated[str | None, Query()] = None,
) -> IngestResponse:
    """Append a batch of recorder events for an attempt (append-only).

    Checks that the caller holds one of the attempt's session tokens (header, or
    `?token=` for sendBeacon) — 401 otherwise, nothing stored; an unknown attempt is
    the same 401 (checked token-first, so the response never reveals which attempt
    ids exist). Only then the attempt row (the event stream has a foreign key). Imports
    no delivery/grading/integrity code (golden rule #6).

    The server-observed client IP is stamped onto `device_info` events only (it is
    server-side, like `server_ts`, never read from the event body).
    """
    log.info("POST /attempts/%s/events count=%d", attempt_id, len(batch.events))
    # Token FIRST: an unknown attempt and a bad token answer the same 401, so the
    # ingest never tells an anonymous caller which attempt ids exist.
    if not AttemptRepository(session).attempt_session_valid(attempt_id, x_attempt_token or token):
        log.warning(
            "event ingest rejected: %s token attempt=%s%s -> dropped %d event(s)",
            "missing" if not (x_attempt_token or token) else "wrong",
            attempt_id,
            "" if session.get(m.AttemptRow, attempt_id) is not None else " (unknown attempt)",
            len(batch.events),
        )
        raise HTTPException(status_code=401, detail="invalid attempt token")
    if session.get(m.AttemptRow, attempt_id) is None:  # pragma: no cover - FK keeps them paired
        log.warning("event ingest rejected: unknown attempt=%s", attempt_id)
        raise HTTPException(status_code=404, detail="attempt not found")
    stored = service.record_batch(attempt_id, batch, client_ip=_client_ip(request))
    return IngestResponse(ingested=len(stored))
