"""Telemetry engine: append-only event ingest (Phase 6).

The recorder in `apps/web` batches behavioral events (visibility, blur,
pagehide, per-question interaction, answer changes, audio play/seek) and posts
them for an attempt. This service turns that batch into `IntegrityEvent`s and
appends them via `EventRepository`, which stamps the trusted `server_ts`.

**Capture only — NO judgment (golden rule #6).** This engine records the event
stream and nothing else. It computes no features and assigns no score; it does
not import `app.grading`, `app.integrity`, or `app.analysis`. The deterministic
feature extractor (Phase 7, `app/integrity`) and the LLM verdict (Phase 8,
`app/analysis`) read this stream later — they are separate layers.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.persistence.repository import EventRepository
from app.telemetry.schema import ClientEvent, EventBatch
from contracts import IntegrityEvent

log = get_logger(__name__)


class TelemetryService:
    """Append a batch of recorder events to the append-only event stream."""

    def __init__(self, events: EventRepository) -> None:
        self.events = events

    def record_batch(self, attempt_id: str, batch: EventBatch) -> list[IntegrityEvent]:
        """Persist every event in the batch, server-stamping `server_ts`.

        Returns the stored events (with `server_ts` filled in). The repository
        logs a WARNING per event (integrity events are surfaced loudly), so this
        method logs the batch boundary at INFO.
        """
        log.info(
            "telemetry batch attempt=%s events=%d", attempt_id, len(batch.events)
        )
        stored = [self._record_one(attempt_id, event) for event in batch.events]
        return stored

    def list_events(self, attempt_id: str) -> list[IntegrityEvent]:
        """Read back an attempt's event stream in ingest order (capture only)."""
        return self.events.list_events(attempt_id)

    def _record_one(self, attempt_id: str, event: ClientEvent) -> IntegrityEvent:
        # Build the canonical event with `server_ts=None` so the repository is the
        # single place that stamps server time — the client can never supply it.
        ingested = IntegrityEvent(
            attempt_id=attempt_id,
            item_id=event.item_id,
            type=event.type,
            client_ts=event.client_ts,
            server_ts=None,
            duration_ms=event.duration_ms,
            payload=event.payload,
        )
        return self.events.add_event(ingested)
