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

Writing-field events (`field_focus`, `field_blur`, `writing_stats`,
`input_burst`, `paste_attempt`, `fullscreen_exit`) get a TYPED payload check
(`extra="forbid"`): the recorder may only send shapes and counts — never key
identities, never the text the student tried to paste. A payload carrying any
other key (e.g. a `text`/`data` field) is rejected with 422, which makes
"no clipboard content is stored" structural too. Validation only — no judgment
(golden rule #6).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contracts.runtime import EventType


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Writing fields the recorder tracks (open_writing textarea; gap_fill reserved).
FieldKind = Literal["open_writing", "gap_fill"]
# Upper bound on any capture count: a sanity cap, not a judgment.
_MAX = 10_000_000
# Upper bound on any duration in ms (24 h): no exam window or inter-key sum is
# longer, and an unbounded int would let one forged event poison every sum the
# integrity layer derives (and overflow a 64-bit column in other backends).
_MAX_MS = 86_400_000
# `duration_ms` (e.g. how long the tab stayed hidden) may legitimately span a
# night; 7 days still fits a 32-bit integer column.
_MAX_DURATION_MS = 7 * 86_400_000


class FieldFocusPayload(_Base):
    """`field_focus` / `field_blur`: which kind of field, and its length (no text)."""

    field: FieldKind = "open_writing"
    length: int = Field(default=0, ge=0, le=_MAX)


class WritingStatsPayload(_Base):
    """Aggregated typing stats for one writing field since the previous flush.

    Every count is a DELTA for the window (additive across flushes and reloads).
    `added` / `inserts` / `max_insert` are keyed by the browser's raw
    `InputEvent.inputType` (e.g. insertText, insertFromDrop, insertReplacementText);
    classifying them as keyboard vs non-keyboard is the integrity layer's job.
    `ik_hist` is the inter-key-interval histogram over the fixed bucket edges
    `IK_BUCKET_EDGES_MS` (ms, upper-exclusive; the last bucket is open-ended).
    """

    field: FieldKind = "open_writing"
    reason: str = Field(default="interval", max_length=32)
    window_ms: int = Field(default=0, ge=0, le=_MAX_MS)
    length: int = Field(default=0, ge=0, le=_MAX)
    added: dict[str, int] = Field(default_factory=dict)
    inserts: dict[str, int] = Field(default_factory=dict)
    max_insert: dict[str, int] = Field(default_factory=dict)
    deleted_chars: int = Field(default=0, ge=0, le=_MAX)
    keystrokes: int = Field(default=0, ge=0, le=_MAX)
    untrusted_inputs: int = Field(default=0, ge=0, le=_MAX)
    ik_count: int = Field(default=0, ge=0, le=_MAX)
    ik_sum_ms: int = Field(default=0, ge=0, le=_MAX_MS)
    ik_mean_ms: float | None = Field(default=None, ge=0, le=_MAX_MS, allow_inf_nan=False)
    ik_p50_ms: float | None = Field(default=None, ge=0, le=_MAX_MS, allow_inf_nan=False)
    ik_p90_ms: float | None = Field(default=None, ge=0, le=_MAX_MS, allow_inf_nan=False)
    pauses_2s: int = Field(default=0, ge=0, le=_MAX)
    pauses_10s: int = Field(default=0, ge=0, le=_MAX)
    ik_hist: list[int] = Field(default_factory=list, max_length=32)
    active_ms: int = Field(default=0, ge=0, le=_MAX_MS)

    @model_validator(mode="after")
    def _bounded_maps(self) -> WritingStatsPayload:
        for name in ("added", "inserts", "max_insert"):
            m = getattr(self, name)
            if len(m) > 32:
                raise ValueError(f"{name}: too many input types")
            for key, val in m.items():
                if len(key) > 48 or not 0 <= val <= _MAX:
                    raise ValueError(f"{name}: bad entry {key!r}")
        if any(not 0 <= v <= _MAX for v in self.ik_hist):
            raise ValueError("ik_hist: bad bucket count")
        return self


class InputBurstPayload(_Base):
    """One input event that added several chars at once: its type + size, no text."""

    field: FieldKind = "open_writing"
    input_type: str = Field(default="", max_length=48)
    length: int = Field(ge=0, le=_MAX, description="Chars added by this one event.")
    field_length: int = Field(default=0, ge=0, le=_MAX)
    trusted: bool = True


class PasteAttemptPayload(_Base):
    """A BLOCKED paste/drop: only the LENGTH of the clipboard text, never the text."""

    kind: Literal["paste", "drop"] = "paste"
    length: int = Field(ge=0, le=_MAX)
    field: FieldKind | None = None


class FullscreenExitPayload(_Base):
    """The document left fullscreen (no extra data)."""


PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "field_focus": FieldFocusPayload,
    "field_blur": FieldFocusPayload,
    "writing_stats": WritingStatsPayload,
    "input_burst": InputBurstPayload,
    "paste_attempt": PasteAttemptPayload,
    "fullscreen_exit": FullscreenExitPayload,
}

# Inter-key-interval histogram bucket upper edges (ms). Shared with the recorder
# (apps/web/recorder.js IK_EDGES) and the integrity layer (percentile estimate).
IK_BUCKET_EDGES_MS: tuple[int, ...] = (50, 100, 150, 200, 300, 500, 750, 1000, 2000, 10000)


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
        default=None,
        ge=0,
        le=_MAX_DURATION_MS,
        description="e.g. how long the tab stayed hidden.",
    )
    payload: dict[str, Any] = Field(
        default_factory=dict, description="Event-specific extras (audio position, etc.)."
    )

    @model_validator(mode="after")
    def _typed_payload(self) -> ClientEvent:
        """Validate the payload of writing-field events against its typed model.

        The stored payload is the validated, normalized dump (defaults filled in),
        so the integrity layer reads a stable shape.
        """
        model = PAYLOAD_MODELS.get(self.type)
        if model is not None:
            self.payload = model.model_validate(self.payload).model_dump()
        return self


class EventBatch(_Base):
    """A batch of recorder events for a single attempt."""

    events: list[ClientEvent] = Field(default_factory=list)
