"""PDF extraction seam: question paper / answer key PDF -> text + image crops.

The engine depends only on the `PdfExtractor` *protocol*. The real PyMuPDF-backed
implementation lives in `extract_pymupdf.py` (lazy `import fitz`) and tests inject
`MockPdfExtractor`, so the pipeline never hard-depends on PyMuPDF. Mirrors the
grading/analysis LLM seam.

The extractor returns the page text/layout (for the LLM to structure) plus any
image crops (A/B/C option pictures, listening sign images) as raw bytes tagged with
a stable `asset_id`, so the pipeline can store them via the `StorageBackend` and
reference them from the draft by `asset_id` (golden rule: assets live outside the
relational store).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ImageCrop:
    """One cropped image from the PDF, to be stored as an asset."""

    asset_id: str
    data: bytes
    page: int
    bbox: tuple[float, float, float, float]
    content_type: str = "image/png"


@dataclass(frozen=True)
class ExtractedPage:
    number: int
    text: str


@dataclass(frozen=True)
class ExtractedDocument:
    """Text + layout + image crops extracted from a single PDF."""

    pages: tuple[ExtractedPage, ...]
    crops: tuple[ImageCrop, ...] = field(default_factory=tuple)

    @property
    def text(self) -> str:
        """All page text joined in page order (what the structurer reads)."""
        return "\n\n".join(p.text for p in self.pages)


@runtime_checkable
class PdfExtractor(Protocol):
    """Turn PDF bytes into text + layout + image crops."""

    def extract(self, pdf: bytes) -> ExtractedDocument: ...


class MockPdfExtractor:
    """Deterministic stand-in for tests / offline runs.

    Returns a pre-canned `ExtractedDocument` so a golden pipeline run is hermetic (no
    real PDF parsing). A pipeline extracts more than one PDF (question paper *and*
    answer key), so `by_input` maps specific input bytes to their document; anything
    unmapped falls back to `document`.
    """

    def __init__(
        self,
        document: ExtractedDocument,
        *,
        by_input: dict[bytes, ExtractedDocument] | None = None,
    ) -> None:
        self._document = document
        self._by_input = by_input or {}

    def extract(self, pdf: bytes) -> ExtractedDocument:
        return self._by_input.get(pdf, self._document)
