"""FastAPI application entrypoint.

Wires the shared logger at startup, the `GET /health` probe (Phase 0), the
telemetry ingest router (Phase 6), and serves the static `apps/web` assets (the
browser recorder + any runner pages) so they post telemetry same-origin.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from apps.api.admin import router as admin_router
from apps.api.telemetry import router as telemetry_router

configure_logging(settings.log_level)
log = get_logger(__name__)

app = FastAPI(title=settings.app_name)

app.include_router(telemetry_router)
app.include_router(admin_router)

# Static frontend assets (recorder.js + harness/runner pages). Served same-origin
# so the recorder can post telemetry without CORS.
_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
app.mount("/web", StaticFiles(directory=_WEB_DIR, html=True), name="web")


@app.get("/health")
def health() -> dict[str, str]:
    log.info("GET /health -> ok")
    return {"status": "ok"}
