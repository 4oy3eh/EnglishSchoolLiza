"""arq worker entry point (Phase 12 — docker `worker` service).

Run inside the container as `arq app.ingestion.worker.WorkerSettings`. The worker
pulls `ingest_pdf` jobs off Redis and runs the (slow) PDF -> draft pipeline off
the request path. This is the *only* module that depends on `arq` at import time;
the job itself (`jobs.ingest_pdf`) stays arq-free, and `arq` is not in the dev
venv (it's installed in the worker image), so `arq`/`minio` are mypy-ignored.

`on_startup` builds the real ingestion seams (PyMuPDF + Anthropic + WhisperX) and
stashes an `IngestionService` in the arq context where the job reads it. It is
**defensive**: if the heavy backends or `ANTHROPIC_API_KEY` aren't configured,
the worker still starts (so `docker compose up` is healthy) and logs that jobs
will fail until ingestion is configured — real ingestion is exercised separately
from the seeded demo flow.
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.logging import configure_logging, get_logger
from app.ingestion.jobs import ingest_pdf

log = get_logger(__name__)


def _build_ingestion_service() -> Any:
    """Wire the real PDF/LLM/ASR seams into an `IngestionService`.

    Imports the heavy backends lazily so a misconfigured worker fails here (and is
    caught by `on_startup`) rather than at module import.
    """
    from app.content.service import ContentService
    from app.content.storage import FilesystemStorage
    from app.ingestion.asr_whisperx import WhisperXAsr
    from app.ingestion.extract_pymupdf import PyMuPdfExtractor
    from app.ingestion.llm_anthropic import AnthropicStructurer
    from app.ingestion.pipeline import IngestionPipeline
    from app.ingestion.service import IngestionService
    from app.persistence.repository import ContentRepository

    storage = FilesystemStorage(settings.assets_dir)
    content = ContentService(ContentRepository(SessionLocal()), storage)
    pipeline = IngestionPipeline(
        PyMuPdfExtractor(), AnthropicStructurer(), asr=WhisperXAsr()
    )
    return IngestionService(content, pipeline)


async def on_startup(ctx: dict[str, Any]) -> None:
    configure_logging(settings.log_level)
    try:
        ctx["ingestion_service"] = _build_ingestion_service()
        log.info("worker: ingestion service ready")
    except Exception:  # noqa: BLE001 — stay up; surface why jobs will fail
        log.warning(
            "worker: ingestion service unavailable (install PDF/ASR backends and set "
            "ANTHROPIC_API_KEY); jobs will fail until configured",
            exc_info=True,
        )


async def on_shutdown(ctx: dict[str, Any]) -> None:
    log.info("worker: shutting down")


def _redis_settings() -> Any:
    from arq.connections import RedisSettings

    return RedisSettings.from_dsn(settings.redis_url)


class WorkerSettings:
    """arq worker configuration (discovered by attribute name)."""

    functions = [ingest_pdf]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = _redis_settings()
