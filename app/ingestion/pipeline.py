"""Ingestion pipeline: PDFs (+ mp3) -> a validated **draft** `Test`.

A pure orchestrator over the injected seams (`PdfExtractor`, `LLMStructurer`,
optional `Asr`) — it owns no I/O of its own, so a golden run is hermetic with the
mock seams. Steps (each logged to cmd, golden rule logging):

  (a) extract text + image crops from the question paper
  (b) structure -> a `DraftTest` (no answer key)
  (c) parse the answer-key PDF + merge by question number -> authoritative `correct`
  (d) ASR the mp3 -> transcript + align each listening item to an `audio_span`
  (e) validate (the merged `Test` is already a strict contract model) — status=draft

Invariant #5: the result is **always** `status="draft"`; this engine never publishes.
The answer-key PDF — never the LLM — is authoritative for `correct`. A malformed
extraction (missing/invalid key, contract-invalid items) raises and is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.ingestion.answer_key import merge_key, parse_answer_key
from app.ingestion.asr import Asr, AudioSpan, Transcript, align_items_to_spans
from app.ingestion.extract import ImageCrop, PdfExtractor
from app.ingestion.structure import LLMStructurer
from contracts import Test
from contracts.content import Level

log = get_logger(__name__)


@dataclass(frozen=True)
class IngestionRequest:
    test_id: str
    level: Level
    questions_pdf: bytes
    answer_key_pdf: bytes
    audio: bytes | None = None


@dataclass(frozen=True)
class IngestionResult:
    """A validated draft test plus the assets/alignment for the review queue."""

    test: Test  # status == "draft" (never published here)
    crops: tuple[ImageCrop, ...] = field(default_factory=tuple)
    audio_spans: dict[str, AudioSpan] = field(default_factory=dict)
    transcript: Transcript | None = None


class IngestionPipeline:
    """Run the extract -> structure -> key-merge -> ASR -> validate pipeline."""

    def __init__(
        self,
        extractor: PdfExtractor,
        structurer: LLMStructurer,
        *,
        asr: Asr | None = None,
    ) -> None:
        self.extractor = extractor
        self.structurer = structurer
        self.asr = asr

    def run(self, request: IngestionRequest) -> IngestionResult:
        log.info("ingest start test=%s level=%s", request.test_id, request.level)

        # (a) extract the question paper -------------------------------------- #
        document = self.extractor.extract(request.questions_pdf)
        log.info(
            "ingest step=extract test=%s pages=%d crops=%d",
            request.test_id,
            len(document.pages),
            len(document.crops),
        )

        # (b) structure into a draft (no answer key) -------------------------- #
        draft = self.structurer.structure(
            document, level=request.level, test_id=request.test_id
        )
        log.info(
            "ingest step=structure test=%s sections=%d items=%d",
            request.test_id,
            len(draft.sections),
            sum(len(s.items) for s in draft.sections),
        )

        # (c) parse + merge the answer key (authoritative correct) ------------ #
        key_doc = self.extractor.extract(request.answer_key_pdf)
        key = parse_answer_key(key_doc.text)
        test = merge_key(draft, key)  # raises on a missing/invalid key
        log.info("ingest step=key-merge test=%s key_entries=%d", request.test_id, len(key))

        # (d) ASR + align listening items ------------------------------------- #
        transcript: Transcript | None = None
        spans: dict[str, AudioSpan] = {}
        if request.audio is not None and self.asr is not None:
            transcript = self.asr.transcribe(request.audio)
            spans = align_items_to_spans(draft.sections, transcript)
            log.info(
                "ingest step=asr test=%s words=%d aligned=%d",
                request.test_id,
                len(transcript.words),
                len(spans),
            )

        # (e) validate -> draft (never published) ----------------------------- #
        if test.status != "draft":  # defensive: merge_key always sets draft
            raise ValueError(f"ingestion must yield a draft, got status={test.status!r}")
        log.info(
            "ingest done test=%s sections=%d items=%d status=%s",
            test.id,
            len(test.sections),
            sum(len(s.items) for s in test.sections),
            test.status,
        )
        return IngestionResult(
            test=test,
            crops=document.crops,
            audio_spans=spans,
            transcript=transcript,
        )
