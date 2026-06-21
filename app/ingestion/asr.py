"""ASR seam: listening mp3 -> transcript (+ word timestamps + speakers) and the
deterministic alignment of each listening item to an `audio_span`.

The engine depends only on the `Asr` *protocol*. The real WhisperX-backed
implementation lives in `asr_whisperx.py` (lazy import) and tests inject `MockAsr`,
so the pipeline never hard-depends on WhisperX. Mirrors the other engine LLM seams.

`AudioSpan` is **ingestion-internal metadata** (the [start, end] of the recording a
question is about). It is NOT a `contracts/` field: persisting it on the item would be
a schema change (regen JSON Schema + Alembic migration — golden rule #4) the Phase-9
gate does not require, so — consistent with the deferred-persistence pattern in
Phases 5/7/8 — it is carried in the `IngestionResult` for the review queue and the
real persistence lands with Admin/Phase-10.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.core.logging import get_logger
from app.ingestion.models import DraftSection

log = get_logger(__name__)


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float
    speaker: str | None = None


@dataclass(frozen=True)
class Transcript:
    words: tuple[Word, ...] = field(default_factory=tuple)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def duration_s(self) -> float:
        return self.words[-1].end if self.words else 0.0


@dataclass(frozen=True)
class AudioSpan:
    """The [start, end] seconds of the recording one listening item is about."""

    start_s: float
    end_s: float


@runtime_checkable
class Asr(Protocol):
    """Transcribe audio into words with timestamps + speaker labels."""

    def transcribe(self, audio: bytes) -> Transcript: ...


class MockAsr:
    """Deterministic stand-in for tests / offline runs."""

    model_id = "mock-asr"

    def __init__(self, transcript: Transcript) -> None:
        self._transcript = transcript

    def transcribe(self, audio: bytes) -> Transcript:
        return self._transcript


def align_items_to_spans(
    sections: Sequence[DraftSection], transcript: Transcript
) -> dict[str, AudioSpan]:
    """Map each listening item (`q{number}`) to an `audio_span` over the recording.

    Pure and deterministic. A first-pass heuristic: the items under audio-backed
    sections are spread evenly across the recording's duration in item order (Cambridge
    plays the recording once per part, questions in order). A real WhisperX alignment
    would anchor on spoken question markers; this gives a reproducible span to surface
    in the review queue. Non-listening sections are skipped.

    Assumes a single continuous recording (`IngestionRequest.audio` is one mp3) covering
    all listening sections; `section.stimulus.asset_id` is not consulted. A future
    multi-recording paper would need per-asset transcripts before calling this.
    """
    listening_items = [
        f"q{item.number}"
        for section in sections
        if section.stimulus.kind == "audio_asset"
        for item in section.items
    ]
    spans: dict[str, AudioSpan] = {}
    total = transcript.duration_s
    n = len(listening_items)
    if n and total > 0:
        step = total / n
        for index, item_id in enumerate(listening_items):
            spans[item_id] = AudioSpan(
                start_s=round(index * step, 3), end_s=round((index + 1) * step, 3)
            )
    log.info("asr aligned items=%d duration=%.1fs", len(spans), total)
    return spans
