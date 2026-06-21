"""arq job entry point for ingestion (runs the pipeline off the request path).

Ingestion is slow (PDF parse + multimodal LLM + ASR), so it runs as an async
background job on an `arq` worker rather than inline. This module is deliberately
**arq-free at import time**: arq invokes `ingest_pdf` by reference and only passes a
plain `ctx` dict, so nothing here needs the `arq` dependency (kept out of the
typecheck/test path, mirroring how the real SDKs stay lazy).

The worker is expected to put a constructed `IngestionService` in the arq context
under `"ingestion_service"` (wired in Phase 12's docker-compose worker). The pipeline
itself is synchronous, so it is offloaded to a thread to avoid blocking the loop.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.logging import get_logger
from app.ingestion.pipeline import IngestionRequest, IngestionResult
from app.ingestion.service import IngestionService

log = get_logger(__name__)

JOB_NAME = "ingest_pdf"


async def ingest_pdf(ctx: dict[str, Any], request: IngestionRequest) -> str:
    """arq task: ingest a paper into the bank as a draft; returns the test id."""
    service: IngestionService = ctx["ingestion_service"]
    log.info("job=%s start test=%s", JOB_NAME, request.test_id)
    result: IngestionResult = await asyncio.to_thread(service.ingest, request)
    log.info("job=%s done test=%s (draft, awaiting review)", JOB_NAME, result.test.id)
    return result.test.id
