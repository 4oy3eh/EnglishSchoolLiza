"""Ingestion engine facade: run the pipeline and push the draft to the review queue.

`IngestionService.ingest` runs the `IngestionPipeline`, stores the extracted image
crops as assets via the content engine's `StorageBackend`, and persists the resulting
`Test` as a **draft** through `ContentService` (the review queue). It never publishes:
`draft -> published` is a human-approved Admin/Phase-10 action (golden rule #5).

The teacher later reviews the draft (with the image crops and the audio spans) and
approves it; only that explicit approval flips it to `published`.
"""

from __future__ import annotations

from app.content.service import ContentService
from app.core.logging import get_logger
from app.ingestion.pipeline import IngestionPipeline, IngestionRequest, IngestionResult

log = get_logger(__name__)


class IngestionService:
    """Ingest a Cambridge paper into the item bank as a draft for human review."""

    def __init__(self, content: ContentService, pipeline: IngestionPipeline) -> None:
        self.content = content
        self.pipeline = pipeline

    def ingest(self, request: IngestionRequest) -> IngestionResult:
        result = self.pipeline.run(request)

        # Belt-and-braces: refuse to persist anything that isn't a draft (rule #5).
        if result.test.status != "draft":
            raise ValueError(
                f"refusing to queue non-draft test status={result.test.status!r}"
            )

        for crop in result.crops:
            self.content.add_asset(crop.asset_id, crop.data, content_type=crop.content_type)

        test_id = self.content.create_test(result.test)
        log.info(
            "ingest queued draft test=%s crops=%d audio_spans=%d (awaiting review)",
            test_id,
            len(result.crops),
            len(result.audio_spans),
        )
        return result
