"""Per-part listening audio in the real runner (Playwright/Chromium, live API).

A Movers-like test with two listening parts, each with its OWN short generated
recording (WAV made with the stdlib — no ffmpeg needed), plus a third part that
continues track 2:

* track 1 has a sound-check: Start stays disabled until it has been heard;
* track 1 plays locked (no native controls, a pause resumes it, Start disabled);
* moving on to part 2 while track 1 plays does NOT stop it, and part 2 says its
  recording waits for track 1 (strict order);
* when track 1 ends, track 2 starts by itself (the student is on part 2);
* part 1 then shows "finished"; part 3 (same file) shows track 2 playing;
* every progress POST names its track (`asset_id`); the server keeps one point
  per track;
* a reload in the middle of track 2 resumes TRACK 2 at the stored point (never
  from 0, never track 1 again).

Skips cleanly without Chromium.
"""

from __future__ import annotations

import math
import os
import struct
import subprocess
import sys
import time
import wave
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.persistence.models  # noqa: F401  (register tables on Base.metadata)
from app.content.storage import FilesystemStorage
from app.core.db import Base
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import (
    AudioAssetStimulus,
    PassageTextStimulus,
    RosterEntry,
    Section,
    SingleChoiceItem,
    TextOption,
)
from contracts import Test as ExamTest
from tests.pin_helpers import enter_pin, http_json
from tests.test_e2e_full import _free_port, _wait_for_health

sync_api = pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_ID = "tracks-e2e"
PIN = "ABCD"
TRACK1, TRACK2, CHECK = "part1.wav", "part2.wav", "check.wav"
TRACK1_S, TRACK2_S, CHECK_S = 3.0, 14.0, 0.6


def _wav(seconds: float, freq: float) -> bytes:
    """A quiet sine tone, 8 kHz mono 16-bit (Chromium plays WAV natively)."""
    rate = 8000
    frames = b"".join(
        struct.pack("<h", int(3000 * math.sin(2 * math.pi * freq * n / rate)))
        for n in range(int(seconds * rate))
    )
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


def _sc(item_id: str, prompt: str) -> SingleChoiceItem:
    return SingleChoiceItem(
        id=item_id,
        prompt=prompt,
        options=[TextOption(key="A", text="yes"), TextOption(key="B", text="no")],
        correct="A",
    )


def _test() -> ExamTest:
    return ExamTest(
        id=TEST_ID,
        title="Per-part listening E2E",
        level="A1_MOVERS",
        status="published",
        duration_minutes=30,
        sections=[
            Section(
                id=f"{TEST_ID}-l1",
                title="Listening Part 1",
                skill="listening",
                stimulus=AudioAssetStimulus(asset_id=TRACK1, preview_asset_id=CHECK),
                items=[_sc(f"{TEST_ID}-q1", "Is it a dog?")],
            ),
            Section(
                id=f"{TEST_ID}-l2",
                title="Listening Part 2",
                skill="listening",
                stimulus=AudioAssetStimulus(asset_id=TRACK2),
                items=[_sc(f"{TEST_ID}-q2", "Is it a cat?")],
            ),
            Section(
                id=f"{TEST_ID}-l3",
                title="Listening Part 3",
                skill="listening",
                stimulus=AudioAssetStimulus(asset_id=TRACK2),  # continues track 2
                items=[_sc(f"{TEST_ID}-q3", "Is it a bird?")],
            ),
            Section(
                id=f"{TEST_ID}-r1",
                title="Reading Part 1",
                skill="reading",
                stimulus=PassageTextStimulus(text="Read."),
                items=[_sc(f"{TEST_ID}-q4", "Is it red?")],
            ),
        ],
    )


@pytest.fixture
def server(tmp_path: Path) -> Iterator[str]:
    db_url = f"sqlite:///{(tmp_path / 'tracks.db').as_posix()}"
    assets = tmp_path / "assets"
    storage = FilesystemStorage(assets)
    storage.put(TRACK1, _wav(TRACK1_S, 440))
    storage.put(TRACK2, _wav(TRACK2_S, 660))
    storage.put(CHECK, _wav(CHECK_S, 880))
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        ContentRepository(s).add_test(_test())
        AttemptRepository(s).add_roster_entry(
            RosterEntry(id="tracks-e1", test_id=TEST_ID, display_name="Tess", pin=PIN)
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


def _skip_without_chromium(exc: PlaywrightError) -> None:
    if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
        pytest.skip(f"Chromium not installed: {exc}")
    raise exc


_STATE = "() => (document.querySelector('#audioBar .audiobar') || {dataset: {}}).dataset.state"
_TRACK_JS = """(k) => {
  const el = document.querySelector(`#audioPool audio.track[data-track="${k}"]`);
  return el ? {paused: el.paused, ended: el.ended, t: el.currentTime,
               controls: el.controls, inBar: !!el.closest('#audioBar')} : null;
}"""


def _bar(page: object) -> str:
    return page.evaluate(_STATE)  # type: ignore[attr-defined]


def _wait_state(page: object, state: str, timeout: float = 15000) -> None:
    page.wait_for_function(  # type: ignore[attr-defined]
        f"() => ({_STATE})() === {state!r}", timeout=timeout
    )


def _server_progress(base_url: str, page: object) -> dict:
    active = page.evaluate("() => JSON.parse(localStorage.getItem('exam:active'))")  # type: ignore[attr-defined]
    state = http_json(
        f"{base_url}/exam/attempts/{active['attemptId']}/state", token=active["token"]
    )
    return state["audio_progress"]


def test_two_tracks_play_in_order_and_reload_resumes_track_two(server: str) -> None:
    base_url = server
    posts: list[str] = []
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_context().new_page()
            page.on(
                "request",
                lambda r: posts.append(r.post_data or "")
                if r.method == "POST" and r.url.endswith("/audio-progress")
                else None,
            )
            page.goto(f"{base_url}/web/exam.html?test={TEST_ID}")
            enter_pin(page, "Tess", PIN)
            page.wait_for_selector("#runner:not(.hidden)")

            # Part 1: its own track, gated by the sound-check, "Recording 1 of 2".
            _wait_state(page, "ready")
            assert page.text_content("#audioBar .audio-track") == "Recording 1 of 2"
            assert "Each part has its own recording" in page.text_content(".audio-warn")
            assert page.is_disabled("#mainBtn")
            page.click("#previewBtn")
            page.wait_for_function("() => !document.querySelector('#mainBtn').disabled")
            page.click("#mainBtn")
            _wait_state(page, "playing")
            page.wait_for_function(f"() => !({_TRACK_JS})(0).paused")
            t0 = page.evaluate(_TRACK_JS, 0)
            assert t0["controls"] is False and t0["inBar"] is False  # no native controls
            assert page.is_disabled("#mainBtn")
            # Locked: a pause is undone at once.
            page.evaluate("() => document.querySelector('#audioPool audio.track').pause()")
            page.wait_for_function(f"() => !({_TRACK_JS})(0).paused", timeout=3000)

            # Move on while track 1 plays: it keeps playing; part 2 waits for it.
            page.click("#nextBtn")
            assert page.text_content("#partLabel") == "Listening Part 2"
            _wait_state(page, "waiting")
            assert "plays after the recording for “Listening Part 1” ends" in (
                page.text_content("#mainStatus") or ""
            )
            assert page.is_visible("#trackGoBtn")
            assert page.evaluate(_TRACK_JS, 0)["paused"] is False
            assert page.evaluate(_TRACK_JS, 1) is None  # track 2 not even loaded yet

            # Track 1 ends -> track 2 starts by itself (the student is on part 2).
            _wait_state(page, "playing", timeout=int((TRACK1_S + 8) * 1000))
            assert page.text_content("#audioBar .audio-track") == "Recording 2 of 2"
            page.wait_for_function(f"() => ({_TRACK_JS})(1) && !({_TRACK_JS})(1).paused")
            assert page.evaluate(_TRACK_JS, 0)["ended"] is True

            # Part 1 now shows its recording as finished; part 3 continues track 2.
            page.click("#prevBtn")
            _wait_state(page, "done")
            assert "has finished" in (page.text_content("#mainStatus") or "")
            page.click("#nextBtn")
            page.click("#nextBtn")
            assert page.text_content("#partLabel") == "Listening Part 3"
            assert _bar(page) == "playing"
            assert page.evaluate(_TRACK_JS, 1)["paused"] is False
            page.click("#nextBtn")  # reading: no audio bar, the track keeps playing
            assert page.is_hidden("#audioBar")
            assert page.evaluate(_TRACK_JS, 1)["paused"] is False

            # Wait until the server holds >= 5 s of track 2, then reload mid-track.
            deadline = time.time() + 20
            while time.time() < deadline:
                prog = _server_progress(base_url, page)
                if prog.get(TRACK2, 0) >= 5:
                    break
                time.sleep(0.5)
            prog = _server_progress(base_url, page)
            assert prog.get(TRACK1, 0) >= int(TRACK1_S) - 1
            assert prog.get(TRACK2, 0) >= 5
            assert posts and all('"asset_id"' in body for body in posts), posts

            page.reload()
            page.wait_for_selector("#runner:not(.hidden)")
            # Back on part 1 after the reload: finished (track 2 already started).
            _wait_state(page, "done")
            page.click("#nextBtn")
            _wait_state(page, "ready")  # a reload needs a gesture: Resume, no sound-check
            assert page.text_content("#mainBtn") == "▶ Resume listening"
            assert page.locator("#previewBtn").count() == 0
            resume_at = _server_progress(base_url, page)[TRACK2]
            page.click("#mainBtn")
            _wait_state(page, "playing")
            page.wait_for_function(
                f"() => ({_TRACK_JS})(1) && !({_TRACK_JS})(1).paused "
                f"&& ({_TRACK_JS})(1).t >= {resume_at - 0.5}"
            )
            first = page.evaluate(_TRACK_JS, 1)
            assert first["t"] >= resume_at - 0.5  # resumed, never from 0
            t_track0 = page.evaluate(_TRACK_JS, 0)
            assert t_track0 is None or t_track0["paused"]  # track 1 never replays
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        _skip_without_chromium(exc)
