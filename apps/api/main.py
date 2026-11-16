"""FastAPI application entrypoint.

Wires the shared logger at startup, the `GET /health` probe (Phase 0), the
telemetry ingest router (Phase 6), the asset-serving route (Phase 12), and serves
the static `apps/web` assets (the
browser recorder + any runner pages) so they post telemetry same-origin.

Startup migration check (Phase 13): the lifespan compares the DB's
`alembic_version` with the migration head and refuses to start a server whose
schema is behind (`RuntimeError` + the fix, `make migrate`). Two escapes:

* a DB with **no** `alembic_version` table but existing tables was built with
  `Base.metadata.create_all` (tests / e2e / the loaders) — warn and continue;
* env `ALLOW_UNMIGRATED_DB=1` downgrades the hard failure to a warning (break-glass
  for a deploy that must come up before its migration can be run).

Recovery loop (Phase 13): a daemon thread runs `sweep_missing_reports` shortly
after startup (`RECOVERY_INITIAL_DELAY_S`, 5 s, interruptible by shutdown) and then
every `RECOVERY_SWEEP_INTERVAL_S` seconds (default 300): it expires
abandoned past-deadline attempts and finalizes every finished attempt still
missing a report (the results engine's in-flight lock and failed-report cooldown
apply). It never raises and stops on shutdown via a `threading.Event`.
`SKIP_STARTUP_SWEEP=1` disables it entirely (tests/tools).
"""

from __future__ import annotations

import os
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine, inspect

from app.core.config import settings
from app.core.db import engine
from app.core.logging import configure_logging, get_logger
from app.results import sweep_missing_reports
from apps.api.admin import UploadLimitMiddleware
from apps.api.admin import router as admin_router
from apps.api.assets import router as assets_router
from apps.api.delivery import router as delivery_router
from apps.api.telemetry import router as telemetry_router

configure_logging(settings.log_level)
log = get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = _REPO_ROOT / "alembic.ini"
ALLOW_UNMIGRATED_ENV = "ALLOW_UNMIGRATED_DB"
# Set to "1" to skip the startup + periodic recovery sweep (tests/tools).
SKIP_SWEEP_ENV = "SKIP_STARTUP_SWEEP"
# Seconds between recovery sweeps (expire overdue attempts + finalize missing reports).
RECOVERY_INTERVAL_ENV = "RECOVERY_SWEEP_INTERVAL_S"
DEFAULT_RECOVERY_INTERVAL_S = 300.0
# Delay before the first sweep: let startup finish and in-flight requests that a
# restart cut short re-arrive before recovery competes with them for the DB.
RECOVERY_INITIAL_DELAY_S = 5.0
# How long shutdown waits for a sweep that is mid-run (it is a daemon thread anyway).
RECOVERY_JOIN_TIMEOUT_S = 5.0
MIGRATE_HINT = "run `make migrate` (alembic upgrade head)"


def script_head(ini_path: Path = ALEMBIC_INI) -> str | None:
    """The newest migration revision under `migrations/versions` (no env.py run)."""
    return ScriptDirectory.from_config(Config(str(ini_path))).get_current_head()


def check_db_migrated(
    db_engine: Engine,
    *,
    head: str | None = None,
    allow_unmigrated: bool | None = None,
) -> str:
    """Compare the DB revision with the script head; refuse to start when behind.

    Returns one of `"ok"` (at head), `"unversioned"` (no `alembic_version` table —
    schema created via `create_all`, warning only) or `"behind"` (only reachable
    with `allow_unmigrated`). Raises `RuntimeError` when the DB is versioned but
    behind head and the override is not set.
    """
    if head is None:
        head = script_head()
    if allow_unmigrated is None:
        allow_unmigrated = os.environ.get(ALLOW_UNMIGRATED_ENV, "").strip() == "1"

    with db_engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
        tables = set(inspect(conn).get_table_names())

    if current == head:
        log.info("db migration check: at head %s", head)
        return "ok"

    if "alembic_version" not in tables and tables:
        # Schema exists but was never stamped: Base.metadata.create_all (tests,
        # e2e, the loaders). Not a data-loss risk for the running app, so warn only.
        log.warning(
            "db migration check: no alembic_version table (tables=%d, head=%s) — "
            "schema was created outside Alembic (create_all); %s to version it",
            len(tables),
            head,
            MIGRATE_HINT,
        )
        return "unversioned"

    log.error(
        "db migration check: database at revision %s (None = empty/unstamped) but "
        "migrations head is %s — %s",
        current,
        head,
        MIGRATE_HINT,
    )
    if allow_unmigrated:
        log.warning("%s=1 set — starting on an unmigrated database", ALLOW_UNMIGRATED_ENV)
        return "behind"
    raise RuntimeError(
        f"database schema is behind: db={current} head={head}. {MIGRATE_HINT}, "
        f"or set {ALLOW_UNMIGRATED_ENV}=1 to start anyway."
    )


def recovery_interval_s() -> float:
    """`RECOVERY_SWEEP_INTERVAL_S` as positive seconds; the default on unset/invalid."""
    raw = os.environ.get(RECOVERY_INTERVAL_ENV, "").strip()
    if not raw:
        return DEFAULT_RECOVERY_INTERVAL_S
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        log.warning(
            "%s=%r is not a positive number — using %ss",
            RECOVERY_INTERVAL_ENV,
            raw,
            DEFAULT_RECOVERY_INTERVAL_S,
        )
        return DEFAULT_RECOVERY_INTERVAL_S
    return value


def run_recovery_loop(
    stop: threading.Event,
    interval_s: float,
    sweep: Callable[[], object] | None = None,
    initial_delay_s: float | None = None,
) -> None:
    """Wait `initial_delay_s` (default `RECOVERY_INITIAL_DELAY_S`), sweep, then
    sweep every `interval_s` until `stop` is set. Never raises.

    `sweep` defaults to `sweep_missing_reports` (looked up at call time): expire
    abandoned past-deadline attempts, then finalize every finished attempt missing
    a report — one transaction per attempt, skipping in-flight finalizes and
    `failed` reports younger than the retry cooldown. `stop.wait` doubles as the
    sleep, so shutdown interrupts the wait immediately — the initial delay too.
    """
    delay = RECOVERY_INITIAL_DELAY_S if initial_delay_s is None else initial_delay_s
    log.info("recovery loop started interval=%ss first sweep in %ss", interval_s, delay)
    run = 0
    if delay > 0 and stop.wait(delay):
        log.info("recovery loop stopped before its first sweep")
        return
    while not stop.is_set():
        run += 1
        try:
            (sweep or sweep_missing_reports)()
        except Exception:  # never let the loop (and its thread) die
            log.exception("recovery sweep #%d failed", run)
        if stop.wait(interval_s):
            break
    log.info("recovery loop stopped after %d sweep(s)", run)


def start_recovery_loop(
    stop: threading.Event,
    interval_s: float,
    sweep: Callable[[], object] | None = None,
    initial_delay_s: float | None = None,
) -> threading.Thread:
    """Start `run_recovery_loop` on a daemon thread (slow LLM calls never block
    request serving; a daemon never blocks interpreter exit)."""
    thread = threading.Thread(
        target=run_recovery_loop,
        args=(stop, interval_s, sweep, initial_delay_s),
        name="report-recovery-loop",
        daemon=True,
    )
    thread.start()
    return thread


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    check_db_migrated(engine)
    # Recovery: a restart right after a submit loses the in-flight background
    # finalize, and a student who closes the tab leaves an attempt `in_progress`
    # past its deadline. The loop sweeps both at startup and then periodically on
    # a daemon thread; each attempt is finalized in its own transaction.
    stop = threading.Event()
    thread: threading.Thread | None = None
    if os.environ.get(SKIP_SWEEP_ENV) != "1":
        interval = recovery_interval_s()
        log.info("startup: starting recovery loop (every %ss)", interval)
        thread = start_recovery_loop(stop, interval)
    try:
        yield
    finally:
        stop.set()
        if thread is not None:
            thread.join(RECOVERY_JOIN_TIMEOUT_S)
            if thread.is_alive():
                log.warning(
                    "shutdown: recovery sweep still running after %ss (daemon, abandoned)",
                    RECOVERY_JOIN_TIMEOUT_S,
                )
            else:
                log.info("shutdown: recovery loop stopped")


app = FastAPI(title=settings.app_name, lifespan=lifespan)
# Body-size cap on the admin upload routes (413 before the multipart body is
# buffered; see `apps/api/admin.py`). Every other route passes straight through.
app.add_middleware(UploadLimitMiddleware)

app.include_router(telemetry_router)
app.include_router(delivery_router)
app.include_router(admin_router)
app.include_router(assets_router)

# Static frontend assets (recorder.js + harness/runner pages). Served same-origin
# so the recorder can post telemetry without CORS.
_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
app.mount("/web", StaticFiles(directory=_WEB_DIR, html=True), name="web")


@app.get("/health")
def health() -> dict[str, str]:
    log.info("GET /health -> ok")
    return {"status": "ok"}
