"""FastAPI application entrypoint (Phase 0).

Wires the shared logger at startup and exposes a single `GET /health` probe
that logs the incoming request — so a developer running `make run` sees a clean
log line in cmd on every hit.
"""

from fastapi import FastAPI

from app.core.config import settings
from app.core.logging import configure_logging, get_logger

configure_logging(settings.log_level)
log = get_logger(__name__)

app = FastAPI(title=settings.app_name)


@app.get("/health")
def health() -> dict[str, str]:
    log.info("GET /health -> ok")
    return {"status": "ok"}
