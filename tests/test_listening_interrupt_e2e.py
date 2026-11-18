"""Listening tracks that stop / fail / have replays, in the real runner (Playwright).

* a locked track whose resume is refused after a pause (iOS screen lock / audio
  interruption: `play()` rejects with NotAllowedError) is NOT left "playing"
  forever: the bar shows Resume, which continues the same play from where it
  stopped; the refusal is noted in telemetry;
* a track whose file cannot be loaded (404) shows Resume after the first error and
  is skipped after the second one ("could not be played — tell your teacher"),
  so the next part's recording can still start;
* with plays=2 the next recording never starts by itself while the previous one
  still has a replay: the student chooses (Replay / "Go to the next recording"),
  and the next one starts by itself only once the replay is spent.

Skips cleanly without Chromium.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.persistence.models  # noqa: F401  (register tables on Base.metadata)
from app.content.storage import FilesystemStorage
from app.core.db import Base
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import AudioAssetStimulus, RosterEntry, Section
from contracts import Test as ExamTest
from tests.pin_helpers import enter_pin
from tests.test_e2e_full import _free_port, _wait_for_health
from tests.test_listening_tracks_e2e import (
    _TRACK_JS,
    _bar,
    _sc,
    _skip_without_chromium,
    _wait_state,
    _wav,
)

sync_api = pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PIN = "ABCD"
T1, T2 = "one.wav", "two.wav"


def _test(test_id: str, *, plays: int = 1) -> ExamTest:
    return ExamTest(
        id=test_id,
        title="Listening interruptions E2E",
        level="A1_MOVERS",
        status="published",
        duration_minutes=30,
        sections=[
            Section(
                id=f"{test_id}-l1",
                title="Listening Part 1",
                skill="listening",
                stimulus=AudioAssetStimulus(asset_id=T1, plays=plays),
                items=[_sc(f"{test_id}-q1", "Is it a dog?")],
            ),
            Section(
                id=f"{test_id}-l2",
                title="Listening Part 2",
                skill="listening",
                stimulus=AudioAssetStimulus(asset_id=T2, plays=plays),
                items=[_sc(f"{test_id}-q2", "Is it a cat?")],
            ),
        ],
    )


@contextmanager
def _serve(tmp_path: Path, test: ExamTest, t1_seconds: float) -> Iterator[str]:
    db_url = f"sqlite:///{(tmp_path / 'interrupt.db').as_posix()}"
    assets = tmp_path / "assets"
    storage = FilesystemStorage(assets)
    storage.put(T1, _wav(t1_seconds, 440))
    storage.put(T2, _wav(8.0, 660))
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        ContentRepository(s).add_test(test)
        AttemptRepository(s).add_roster_entry(
            RosterEntry(id=f"{test.id}-e1", test_id=test.id, display_name="Tess", pin=PIN)
        )
        s.commit()
    engine.dispose()
    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": db_url,
        "ASSETS_DIR": str(assets),
        "LOG_LEVEL": "INFO",
        "ENVIRONMENT": "dev",
        "SKIP_STARTUP_SWEEP": "1",
        "GEMINI_API_KEY": "",
        "ANALYSIS_LLM": "off",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "apps.api.main:app", "--port", str(port)],
        cwd=REPO_ROOT,
        env=env,
    )
    try:
        _wait_for_health(f"http://127.0.0.1:{port}/health")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


def _open(p: object, base_url: str, test_id: str, posts: list[str] | None = None):
    browser = p.chromium.launch(headless=True)  # type: ignore[attr-defined]
    page = browser.new_context().new_page()
    if posts is not None:
        page.on(
            "request",
            lambda r: posts.append(r.post_data or "") if r.method == "POST" else None,
        )
    page.goto(f"{base_url}/web/exam.html?test={test_id}")
    enter_pin(page, "Tess", PIN)
    page.wait_for_selector("#runner:not(.hidden)")
    return browser, page


def _wait_posted(page: object, posts: list[str], needle: str) -> None:
    """The runner's telemetry note reached the network (a POST body carries it)."""
    for _ in range(50):
        if any(needle in body for body in posts):
            return
        page.wait_for_timeout(100)  # type: ignore[attr-defined]
    raise AssertionError(f"no telemetry note {needle!r} posted")


# Make the NEXT play() call reject like iOS does after a screen lock, then pause
# the track (the locked track's pause handler tries to resume it).
_REFUSE_ONCE_AND_PAUSE = """() => {
  const orig = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    HTMLMediaElement.prototype.play = orig;
    window.__refused = (window.__refused || 0) + 1;
    return Promise.reject(new DOMException("interrupted", "NotAllowedError"));
  };
  document.querySelector('#audioPool audio.track[data-track="0"]').pause();
}"""


def test_refused_resume_after_pause_shows_resume_button(tmp_path: Path) -> None:
    test_id = "refuse-e2e"
    posts: list[str] = []
    with _serve(tmp_path, _test(test_id), t1_seconds=8.0) as base_url:
        try:
            with sync_api.sync_playwright() as p:
                browser, page = _open(p, base_url, test_id, posts)
                _wait_state(page, "ready")
                page.click("#mainBtn")
                _wait_state(page, "playing")
                page.wait_for_function(f"() => ({_TRACK_JS})(0).t > 1.2")

                page.evaluate(_REFUSE_ONCE_AND_PAUSE)
                _wait_state(page, "interrupted", timeout=5000)
                assert page.evaluate("() => window.__refused") == 1
                stopped = page.evaluate(_TRACK_JS, 0)
                assert stopped["paused"] is True and stopped["t"] > 1.0
                assert not page.is_disabled("#mainBtn")
                assert page.text_content("#mainBtn") == "▶ Resume listening"
                assert "recording stopped" in (page.text_content("#mainStatus") or "")
                # Part 2 waits for track 1 (not a dead "playing" bar) and offers a way back.
                page.click("#nextBtn")
                _wait_state(page, "waiting")
                page.click("#trackGoBtn")
                _wait_state(page, "interrupted")

                page.click("#mainBtn")  # Resume: same play, from where it stopped
                _wait_state(page, "playing")
                page.wait_for_function(f"() => !({_TRACK_JS})(0).paused")
                assert page.evaluate(_TRACK_JS, 0)["t"] >= stopped["t"] - 0.5
                assert "Continuing" in (page.text_content("#mainStatus") or "")
                # It plays to the end and the order moves on.
                _wait_state(page, "done", timeout=15000)
                _wait_posted(page, posts, "play_refused")
                browser.close()
        except PlaywrightError as exc:  # pragma: no cover - environment-dependent
            _skip_without_chromium(exc)


def test_unloadable_track_is_skipped_after_retry_and_next_track_starts(
    tmp_path: Path,
) -> None:
    test_id = "loaderr-e2e"
    posts: list[str] = []
    with _serve(tmp_path, _test(test_id), t1_seconds=3.0) as base_url:
        try:
            with sync_api.sync_playwright() as p:
                browser, page = _open(p, base_url, test_id, posts)
                hits: list[str] = []

                def _fail(route: object) -> None:
                    hits.append(route.request.url)  # type: ignore[attr-defined]
                    route.fulfill(status=404, body="gone")  # type: ignore[attr-defined]

                page.route(f"**/assets/{T1}", _fail)
                _wait_state(page, "ready")
                page.click("#mainBtn")
                # First load error: Resume offered (not a dead "playing" bar).
                _wait_state(page, "interrupted", timeout=10000)
                assert page.text_content("#mainBtn") == "▶ Resume listening"
                page.click("#mainBtn")
                # Second consecutive error: skipped with a visible note.
                _wait_state(page, "failed", timeout=10000)
                assert "could not be played — tell your teacher" in (
                    page.text_content("#mainStatus") or ""
                )
                assert len(hits) >= 2
                # The next part's recording is reachable: moving on starts it (the
                # skipped track counts as finished in this page, like an ended one).
                page.click("#nextBtn")
                assert page.text_content("#partLabel") == "Listening Part 2"
                _wait_state(page, "playing")
                page.wait_for_function(f"() => ({_TRACK_JS})(1) && !({_TRACK_JS})(1).paused")
                _wait_posted(page, posts, "load_error")
                _wait_posted(page, posts, "skipped")
                browser.close()
        except PlaywrightError as exc:  # pragma: no cover - environment-dependent
            _skip_without_chromium(exc)


def test_plays_two_never_autostarts_over_a_replay(tmp_path: Path) -> None:
    test_id = "replay-e2e"
    with _serve(tmp_path, _test(test_id, plays=2), t1_seconds=2.0) as base_url:
        try:
            with sync_api.sync_playwright() as p:
                browser, page = _open(p, base_url, test_id)
                _wait_state(page, "ready")
                page.click("#mainBtn")
                _wait_state(page, "playing")
                # First play ends: Replay or "Go to the next recording".
                _wait_state(page, "replay", timeout=10000)
                assert page.text_content("#mainBtn") == "↻ Play it again"
                assert "1 play(s) left" in (page.text_content("#mainStatus") or "")
                page.click("#trackNextBtn")
                assert page.text_content("#partLabel") == "Listening Part 2"
                # Track 2 does NOT start by itself: the replay of track 1 is still there.
                _wait_state(page, "ready")
                page.wait_for_timeout(1000)
                assert _bar(page) == "ready"
                assert page.evaluate(_TRACK_JS, 1) is None
                assert "can still be played again (1 left)" in page.text_content("#prevReplayRow")
                # Replay track 1 from here; part 2 waits for it ...
                page.click("#replayPrevBtn")
                _wait_state(page, "waiting")
                page.wait_for_function(f"() => !({_TRACK_JS})(0).paused")
                # ... and once its plays are spent, track 2 starts by itself.
                _wait_state(page, "playing", timeout=10000)
                page.wait_for_function(f"() => ({_TRACK_JS})(1) && !({_TRACK_JS})(1).paused")
                assert page.evaluate(_TRACK_JS, 0)["ended"] is True
                browser.close()
        except PlaywrightError as exc:  # pragma: no cover - environment-dependent
            _skip_without_chromium(exc)
