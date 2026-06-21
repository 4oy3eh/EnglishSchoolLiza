"""Telemetry ingest transport models (Phase 6).

The browser recorder posts a *batch* of events for one attempt. These models
describe that wire shape — they are engine-local transport, not cross-engine
contracts, so they live here rather than in `contracts/`.

Crucially, `ClientEvent` has **no `server_ts` field** (`extra="forbid"`): a
client physically cannot supply it. The server stamps `server_ts` on ingest and
it is the trusted timestamp (golden rule: capture-only telemetry, server time is
authoritative). The same way delivery's `Client*` projections make "no answer
key to the client" structural, this makes "no client-forged server time"
structural.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from contracts.runtime import EventType


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClientEvent(_Base):
    """One recorder event as sent by the browser.

    `attempt_id` and `server_ts` are intentionally absent: the attempt comes from
    the URL path, and the server time is stamped on ingest (never trusted from
    the client).
    """

    type: EventType
    client_ts: datetime = Field(description="Timestamp from the student's browser.")
    item_id: str | None = Field(
        default=None, description="The question in focus when the event fired, if any."
    )
    duration_ms: int | None = Field(
        default=None, ge=0, description="e.g. how long the tab stayed hidden."
    )
    payload: dict[str, Any] = Field(
        default_factory=dict, description="Event-specific extras (audio position, etc.)."
    )


class EventBatch(_Base):
    """A batch of recorder events for a single attempt."""

    events: list[ClientEvent] = Field(default_factory=list)
