"""Ingestion engine: Cambridge PDF (+ mp3) -> a **draft** item-bank `Test`.

Public surface only. The real, heavy backends (PyMuPDF, the Anthropic SDK, WhisperX,
arq) are deliberately NOT imported here — they live in their own modules behind lazy
imports (`extract_pymupdf`, `llm_anthropic`, `asr_whisperx`, `jobs`) so importing this
package pulls no optional dependency. Tests inject the mocks exported below.

Golden rule #5: ingestion only ever produces a **draft**; the answer-key PDF is
authoritative for `correct`; publishing is a separate human-approved Admin step.
"""

from __future__ import annotations

from app.ingestion.answer_key import KeyEntry, merge_key, parse_answer_key
from app.ingestion.asr import (
    Asr,
    AudioSpan,
    MockAsr,
    Transcript,
    Word,
    align_items_to_spans,
)
from app.ingestion.extract import (
    ExtractedDocument,
    ExtractedPage,
    ImageCrop,
    MockPdfExtractor,
    PdfExtractor,
)
from app.ingestion.models import (
    DraftGapFill,
    DraftMatching,
    DraftOpenWriting,
    DraftSection,
    DraftSingleChoice,
    DraftTest,
)
from app.ingestion.pipeline import (
    IngestionPipeline,
    IngestionRequest,
    IngestionResult,
)
from app.ingestion.service import IngestionService
from app.ingestion.structure import LLMStructurer, MockLLMStructurer

__all__ = [
    # draft models
    "DraftTest",
    "DraftSection",
    "DraftSingleChoice",
    "DraftGapFill",
    "DraftMatching",
    "DraftOpenWriting",
    # extraction seam
    "PdfExtractor",
    "MockPdfExtractor",
    "ExtractedDocument",
    "ExtractedPage",
    "ImageCrop",
    # structuring seam
    "LLMStructurer",
    "MockLLMStructurer",
    # answer key
    "parse_answer_key",
    "merge_key",
    "KeyEntry",
    # asr
    "Asr",
    "MockAsr",
    "Transcript",
    "Word",
    "AudioSpan",
    "align_items_to_spans",
    # pipeline + service
    "IngestionPipeline",
    "IngestionRequest",
    "IngestionResult",
    "IngestionService",
]
