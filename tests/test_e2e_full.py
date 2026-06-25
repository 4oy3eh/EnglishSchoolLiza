"""Phase 12 gate: full-stack honest-vs-cheat E2E.

Boots the whole API against a throwaway DB seeded with the human-approved demo
test, then drives two real attempts through real browser telemetry:

* **honest** — works each question with human-like pauses, never leaves the tab.
* **cheat** — for every question, backgrounds the tab, then returns and answers
  instantly (the "left, came back, answered immediately" pattern).

Both feed the *real* pipeline end to end: recorder.js -> ingest endpoint ->
append-only store -> deterministic integrity features (Phase 7) -> advisory
analysis verdict (Phase 8, deterministic `MockAnalysisLLM`). The gate
(docs/PROMPTS.md Prompt 12): the cheat attempt yields a **higher
`suspicion_score`** than the honest one, and a non-trivial verdict.

Skips cleanly when Playwright/Chromium isn't installed (like the Phase 6 E2E), so
the rest of `make test` stays green in a bare environment.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.persistence.models  # noqa: F401  (register tables on Base.metadata)
from app.analysis.llm import MockAnalysisLLM
from app.analysis.service import AnalysisService
from app.content.seed import SEED_TEST_ID, build_sample_test, seed
from app.content.storage import FilesystemStorage
from app.core.db import Base
from app.integrity import IntegrityService
from app.persistence.repository import EventRepository
from contracts import AnalysisVerdict

sync_api = pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# The seeded test's question item ids, kept in sync with the seed itself.
QIDS = [item.id for s in build_sample_test().sections for item in s.items]


# --------------------------------------------------------------------------- #
# Tiny stdlib HTTP helpers (avoid a test dependency on requests/httpx here).
# --------------------------------------------------------------------------- #
def _get_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.load(resp)


def _post_json(url: str) -> dict:
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = resp.read()
    return json.loads(body) if body else {}


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


# --------------------------------------------------------------------------- #
# Live server seeded with the demo test.
# --------------------------------------------------------------------------- #
@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[tuple[str, str]]:
    """Boot uvicorn against a throwaway sqlite DB seeded with the demo; yield
    (base_url, db_url)."""
    db_path = tmp_path / "e2e.db"
    db_url = f"sqlite:///{db_path.as_posix()}"
    assets_dir = tmp_path / "assets"

    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        seed(s, FilesystemStorage(assets_dir))
        s.commit()
    engine.dispose()

    port = _free_port()
    env = {
        **__import__("os").environ,
        "DATABASE_URL": db_url,
        "ASSETS_DIR": str(assets_dir),
        "LOG_LEVEL": "INFO",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "apps.api.main:app", "--port", str(port)],
        cwd=REPO_ROOT,
        env=env,
    )
    try:
        _wait_for_health(f"http://127.0.0.1:{port}/health")
        yield f"http://127.0.0.1:{port}", db_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


def _start_attempt(base_url: str, display_name: str) -> str:
    """Pick a name off the seeded roster and start (real lifecycle) an attempt."""
    roster = _get_json(f"{base_url}/exam/tests/{SEED_TEST_ID}/roster")
    assert isinstance(roster, list)
    entry = next(e for e in roster if e["display_name"] == display_name)
    started = _post_json(f"{base_url}/exam/roster/{entry['roster_entry_id']}/start")
    return str(started["attempt_id"])


# --------------------------------------------------------------------------- #
# Browser-driven personas (real recorder.js -> real ingest endpoint).
# --------------------------------------------------------------------------- #
_HIDE = """() => {
    Object.defineProperty(document, 'visibilityState',
        { configurable: true, get: () => 'hidden' });
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
    document.dispatchEvent(new Event('visibilitychange'));
}"""
_SHOW = """() => {
    Object.defineProperty(document, 'visibilityState',
        { configurable: true, get: () => 'visible' });
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
    document.dispatchEvent(new Event('visibilitychange'));
}"""


def _open_recorder(context: object, base_url: str, attempt_id: str) -> object:
    page = context.new_page()  # type: ignore[attr-defined]
    page.goto(f"{base_url}/web/recorder-test.html?attempt={attempt_id}")
    page.wait_for_function("() => window.__recorder !== undefined")
    return page


def _drive_honest(page: object, qids: list[str]) -> None:
    """Work through questions with varied human pauses; never leave the tab."""
    pauses = [350, 600, 450, 700, 500]
    for qid, pause in zip(qids, pauses, strict=False):
        page.evaluate(  # type: ignore[attr-defined]
            "(q) => { window.__recorder.setItem(q); window.__recorder.interaction(q, {}); }",
            qid,
        )
        page.wait_for_timeout(pause)  # type: ignore[attr-defined]
        page.evaluate("(q) => window.__recorder.answerChange(q, 'A')", qid)  # type: ignore[attr-defined]
    page.evaluate("() => window.__recorder.flush()")  # type: ignore[attr-defined]


def _drive_cheat(page: object, qids: list[str]) -> None:
    """For each question: background the tab, return, answer instantly."""
    for qid in qids:
        page.evaluate("(q) => window.__recorder.setItem(q)", qid)  # type: ignore[attr-defined]
        page.evaluate(_HIDE)  # type: ignore[attr-defined]  # records visibility_hidden + beacon
        page.wait_for_timeout(1200)  # type: ignore[attr-defined]  # real hidden span (server_ts gap)
        page.evaluate(_SHOW)  # type: ignore[attr-defined]  # records visibility_visible (queued)
        # Answer immediately, flushing visible+answer together -> tiny post-return gap.
        page.evaluate(  # type: ignore[attr-defined]
            "(q) => { window.__recorder.answerChange(q, 'A'); window.__recorder.flush(); }",
            qid,
        )
        page.wait_for_timeout(150)  # type: ignore[attr-defined]


def _count_events(db_url: str, attempt_id: str, event_type: str) -> int:
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            from sqlalchemy import text

            row = conn.execute(
                text(
                    "SELECT COUNT(*) FROM integrity_events "
                    "WHERE attempt_id = :a AND type = :t"
                ),
                {"a": attempt_id, "t": event_type},
            ).scalar_one()
        return int(row)
    finally:
        engine.dispose()


def _wait_for_events(db_url: str, attempt_id: str, event_type: str, want: int) -> None:
    deadline = time.monotonic() + 8.0
    seen = 0
    while time.monotonic() < deadline:
        seen = _count_events(db_url, attempt_id, event_type)
        if seen >= want:
            return
        time.sleep(0.2)
    raise AssertionError(
        f"attempt {attempt_id}: only {seen}/{want} {event_type} events landed"
    )


def _verdict(db_url: str, attempt_id: str) -> AnalysisVerdict:
    """Run the real integrity + analysis pipeline over the stored events."""
    engine = create_engine(db_url)
    try:
        with sessionmaker(bind=engine)() as session:
            events = EventRepository(session)
            analysis = AnalysisService(
                events, IntegrityService(events), llm=MockAnalysisLLM()
            )
            return analysis.analyze(attempt_id)
    finally:
        engine.dispose()


def test_cheat_path_scores_higher_suspicion_than_honest(
    live_server: tuple[str, str],
) -> None:
    base_url, db_url = live_server

    honest_attempt = _start_attempt(base_url, "Anna")
    cheat_attempt = _start_attempt(base_url, "Bao")

    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            honest_ctx = browser.new_context()
            honest_page = _open_recorder(honest_ctx, base_url, honest_attempt)
            _drive_honest(honest_page, QIDS)
            honest_page.wait_for_timeout(300)
            honest_ctx.close()

            cheat_ctx = browser.new_context()
            cheat_page = _open_recorder(cheat_ctx, base_url, cheat_attempt)
            _drive_cheat(cheat_page, QIDS)
            cheat_page.wait_for_timeout(300)
            cheat_ctx.close()

            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed: {exc}")
        raise

    # Both attempts finalize through the real delivery lifecycle.
    _post_json(f"{base_url}/exam/attempts/{honest_attempt}/submit")
    _post_json(f"{base_url}/exam/attempts/{cheat_attempt}/submit")

    # Wait for the telemetry to land before profiling.
    _wait_for_events(db_url, honest_attempt, "answer_change", len(QIDS))
    _wait_for_events(db_url, cheat_attempt, "visibility_hidden", len(QIDS))
    _wait_for_events(db_url, cheat_attempt, "answer_change", len(QIDS))

    honest_verdict = _verdict(db_url, honest_attempt)
    cheat_verdict = _verdict(db_url, cheat_attempt)

    # The gate: cheat is more suspicious, and non-trivially so.
    assert cheat_verdict.suspicion_score > honest_verdict.suspicion_score
    assert cheat_verdict.suspicion_score >= 0.5  # non-trivial verdict
    assert honest_verdict.suspicion_score < 0.2  # honest stays low
    # The cheat verdict surfaces the raw signal it was built on (rule #6).
    assert "fast_post_return" in cheat_verdict.flags
    # Advisory only (golden rule #2): a verdict never carries a score field.
    assert not hasattr(cheat_verdict, "score")
