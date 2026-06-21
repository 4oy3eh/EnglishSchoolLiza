"""Phase 6 gate: Playwright E2E — backgrounding the tab logs a visibility event.

Drives the real recorder (`apps/web/recorder.js`) in headless Chromium against a
live uvicorn server, backgrounds the tab, and asserts a `visibility_hidden` event
lands in the append-only store via the ingest endpoint.

Skips cleanly when Playwright (or its Chromium build) is not installed, so the
rest of `make test` stays green in a bare environment. Install with:
    pip install playwright pytest-playwright && python -m playwright install chromium
"""

from __future__ import annotations

import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

# Skip the whole module if Playwright isn't available.
sync_api = pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_health(url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.2)
    raise RuntimeError(f"server at {url} did not become healthy in {timeout}s")


def _event_types(db_path: Path, attempt_id: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT type FROM integrity_events WHERE attempt_id = ? ORDER BY id",
            (attempt_id,),
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """Boot uvicorn against a throwaway sqlite DB; yield (base_url, db_path)."""
    db_path = tmp_path / "e2e.db"
    db_url = f"sqlite:///{db_path.as_posix()}"

    # Create the schema the server will write into (migrations aren't run here).
    from sqlalchemy import create_engine

    import app.persistence.models  # noqa: F401  (register tables)
    from app.core.db import Base

    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    engine.dispose()

    port = _free_port()
    env = {
        **__import__("os").environ,
        "DATABASE_URL": db_url,
        "LOG_LEVEL": "INFO",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "apps.api.main:app", "--port", str(port)],
        cwd=REPO_ROOT,
        env=env,
    )
    try:
        _wait_for_health(f"http://127.0.0.1:{port}/health")
        yield f"http://127.0.0.1:{port}", db_path
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


def test_backgrounding_tab_produces_visibility_hidden(
    live_server: tuple[str, Path],
) -> None:
    base_url, db_path = live_server
    attempt_id = "e2e-attempt"

    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.goto(f"{base_url}/web/recorder-test.html?attempt={attempt_id}")
            page.wait_for_load_state("networkidle")
            page.wait_for_function("() => window.__recorder !== undefined")

            # Background the tab. Headless Chromium can't natively hide a tab
            # (old-headless ignores focus, and Emulation.setVisibilityState was
            # removed), so we force `visibilityState` to "hidden" and fire the
            # real `visibilitychange` event. This drives the recorder's actual
            # listener → sendBeacon → ingest endpoint → persistence, exactly as a
            # real backgrounding would; only the browser-native trigger is faked.
            page.evaluate(
                """() => {
                    Object.defineProperty(document, 'visibilityState',
                        { configurable: true, get: () => 'hidden' });
                    Object.defineProperty(document, 'hidden',
                        { configurable: true, get: () => true });
                    document.dispatchEvent(new Event('visibilitychange'));
                }"""
            )

            # The recorder flushes the hidden event via sendBeacon synchronously;
            # give the round-trip a moment before closing.
            page.wait_for_timeout(1500)
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed: {exc}")
        raise

    # Poll the append-only store for the backgrounding event.
    deadline = time.monotonic() + 5.0
    types: list[str] = []
    while time.monotonic() < deadline:
        types = _event_types(db_path, attempt_id)
        if "visibility_hidden" in types:
            break
        time.sleep(0.2)

    assert "visibility_hidden" in types, f"events seen: {types}"
