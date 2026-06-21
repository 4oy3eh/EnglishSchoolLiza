"""Real `PdfExtractor` backed by PyMuPDF (`fitz`).

Kept out of `app/ingestion/__init__.py` and importing PyMuPDF lazily, so the engine
has no hard runtime dependency on `fitz` — tests use `MockPdfExtractor` and only this
module pulls the library, only when actually constructed. Mirrors
`app/analysis/llm_anthropic.py`.

Extracts page text and renders embedded raster images as PNG crops (the A/B/C option
pictures and listening sign images), each tagged with a stable `asset_id` derived
from the page + xref so re-ingesting the same PDF yields the same ids.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.ingestion.extract import ExtractedDocument, ExtractedPage, ImageCrop

log = get_logger(__name__)


class PyMuPdfExtractor:
    """Extract text + image crops from a PDF using PyMuPDF."""

    def __init__(self, *, asset_prefix: str = "img") -> None:
        self.asset_prefix = asset_prefix

    def extract(self, pdf: bytes) -> ExtractedDocument:
        import fitz  # lazy: only needed for the real path

        pages: list[ExtractedPage] = []
        crops: list[ImageCrop] = []
        with fitz.open(stream=pdf, filetype="pdf") as doc:
            for page_index, page in enumerate(doc):
                pages.append(ExtractedPage(number=page_index + 1, text=page.get_text()))
                for img in page.get_images(full=True):
                    xref = img[0]
                    pix = fitz.Pixmap(doc, xref)
                    if pix.n - pix.alpha >= 4:  # CMYK/other -> convert to RGB
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    rects = page.get_image_rects(xref)
                    bbox = tuple(rects[0]) if rects else (0.0, 0.0, 0.0, 0.0)
                    crops.append(
                        ImageCrop(
                            asset_id=f"{self.asset_prefix}-p{page_index + 1}-x{xref}",
                            data=pix.tobytes("png"),
                            page=page_index + 1,
                            bbox=bbox,
                        )
                    )
        log.info(
            "extract pymupdf pages=%d crops=%d bytes_in=%d",
            len(pages),
            len(crops),
            len(pdf),
        )
        return ExtractedDocument(pages=tuple(pages), crops=tuple(crops))
