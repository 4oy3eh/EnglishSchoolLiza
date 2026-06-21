"""Telemetry engine: capture-only ingest of the behavioral event stream.

Public surface:

* `TelemetryService` — append a batch of recorder events to the append-only
  store (server stamps `server_ts`).
* `ClientEvent` / `EventBatch` — the browser-recorder wire shape.

Capture only (golden rule #6): this package imports no `app.grading`,
`app.integrity`, or `app.analysis`. It records events; it never judges them.
"""

from __future__ import annotations

from app.telemetry.schema import ClientEvent, EventBatch
from app.telemetry.service import TelemetryService

__all__ = [
    "TelemetryService",
    "ClientEvent",
    "EventBatch",
]
