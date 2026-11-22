"""E2E (Phase 17b): the IQ runner's step-by-step intro, timer prominence and the
responsive layout, in Chromium.

Synthetic test only (`tests/iq_fixture.py`, never /private): three subtests whose
intros carry practice problems of every kind.

* `test_intro_steps_practice_timer_and_time_up` (desktop): Instructions -> Example
  (practice: mark checked wrong / right, kept across Back / Next, nothing saved) ->
  Ready ("6 questions · 34 s · about 6 s per question") -> Start; the sticky big
  clock (still on screen after scrolling), the one-time non-blocking "30 seconds
  left" notice, amber then red, the "Time is up" interstitial before the next
  instructions; a drawing practice (overlay, not saved, "Show answer"); the intro cap
  starts a subtest by itself (`started_by=intro_cap`); text practice with the numeric
  keyboard, choice practice, the study line on Ready.
* `test_responsive` at 390x844 portrait, 844x390 landscape (phones, touch), 768x1024
  (tablet, touch) and 1366x768 (desktop): no horizontal page scroll on any screen,
  key controls visible and >= 44 px, compact header in landscape, mark toggles keep
  their state across a rotation, the drawing overlay fits the screen (and re-fits on
  rotation without losing strokes), touch drawing -> Confirm -> the thumbnail shows
  the strokes -> a touch swipe that starts ON the thumbnail scrolls the page.

`IQ_UX_SHOTS=<dir>` saves a screenshot of every screen per viewport. Skips cleanly
without Chromium.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.persistence.models  # noqa: F401  (register tables on Base.metadata)
from app.content.seed import seed
from app.content.service import ContentService
from app.content.storage import FilesystemStorage
from app.core.db import Base
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import RosterEntry
from tests import iq_fixture as fx
from tests.pin_helpers import enter_pin, roster_pin
from tests.test_e2e_full import _free_port, _wait_for_health
from tests.test_iq_runner_e2e import (
    _png,
    _saved,
    _server_answers,
    _skip_without_chromium,
    to_ready,
)

sync_api = pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TID = "iq-ux"
NO_OVERFLOW = "document.documentElement.scrollWidth <= window.innerWidth"
N_CELLS = 10  # study cells of the responsive run (a memory sheet cut per symbol)


def _clock_seconds(page: Any) -> int:
    m, sec = page.locator("#timer").inner_text().split(":")
    return int(m) * 60 + int(sec)


def _clock_starts_at(page: Any, limit: int) -> None:
    """The answering countdown runs on THIS section's own limit (not the intro cap,
    not another section's deadline): within a few seconds below it."""
    page.wait_for_function(
        "() => (document.querySelector('#iqPhase')?.textContent || '').toLowerCase()"
        " === 'time left'"
    )
    got = _clock_seconds(page)
    assert limit - 5 <= got <= limit, (got, limit)


def _assets() -> dict[str, bytes]:
    w, h = int(fx.W), int(fx.H)
    sq = fx.SQUARE
    return {
        "iq-pic.png": _png(w, h),
        "iq-square.png": _png(w, h, (sq["x"], sq["y"], sq["w"], sq["h"])),
        "iq-sample.png": _png(80, 40),
        "iq-practice.png": _png(160, 100),
        "iq-practice-answer.png": _png(160, 100, (0.1, 0.45, 0.8, 0.1)),
        **{f"iq-cell-{k}.png": _png(90, 130, (0.1, 0.1, 0.8, 0.8)) for k in range(N_CELLS)},
    }


def _boot(
    tmp_path: Path, data: dict[str, Any], names: list[str], cap: int
) -> Iterator[tuple[str, str]]:
    db_url = f"sqlite:///{(tmp_path / 'iqux.db').as_posix()}"
    assets_dir = tmp_path / "assets"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        storage = FilesystemStorage(assets_dir)
        seed(s, storage)
        content = ContentService(ContentRepository(s), storage)
        content.import_test(data, _assets())
        assert content.publish(TID)
        repo = AttemptRepository(s)
        for n in names:
            repo.add_roster_entry(RosterEntry(id=f"ux-{n.lower()}", test_id=TID, display_name=n))
        s.commit()
    engine.dispose()
    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": db_url,
        "ASSETS_DIR": str(assets_dir),
        "LOG_LEVEL": "INFO",
        "TEACHER_PASSWORD": "ux-pw",
        "ENVIRONMENT": "dev",
        "SKIP_STARTUP_SWEEP": "1",
        "GEMINI_API_KEY": "",
        "ANALYSIS_LLM": "off",
        "SECTION_INTRO_MAX_SECONDS": str(cap),
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


def _shot(page: Any, tag: str, name: str) -> None:
    out = os.environ.get("IQ_UX_SHOTS")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(out) / f"{tag}-{name}.png"))


def _ink(page: Any, selector: str) -> int:
    """Dark pixels painted on a canvas (the strokes)."""
    return page.evaluate(
        """(sel) => { const c = document.querySelector(sel);
             const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
             let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i] > 128) n++; return n; }""",
        selector,
    )


def _sections(page: Any) -> dict[str, Any]:
    return page.evaluate(
        """async () => { const a = JSON.parse(localStorage.getItem('exam:active'));
             const r = await fetch(`/exam/attempts/${a.attemptId}/sections`,
                                   {headers: {'X-Attempt-Token': a.token}});
             return r.json(); }"""
    )


# --------------------------------------------------------------------------- #
# Intro steps + timer.
# --------------------------------------------------------------------------- #
@pytest.fixture
def timer_server(tmp_path: Path) -> Iterator[tuple[str, str]]:
    data = fx.iq_test_json(TID)
    s1, _s2, s3 = data["sections"]
    s1["time_limit_seconds"] = 34  # > 30: the notice, amber, red, expiry in one run
    s3["time_limit_seconds"] = 6
    s3["study"]["study_seconds"] = 6
    yield from _boot(tmp_path, data, ["Ann"], cap=20)


def test_intro_steps_practice_timer_and_time_up(timer_server: tuple[str, str]) -> None:
    base_url, db_url = timer_server
    pin = roster_pin(db_url, "ux-ann")
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1366, "height": 768})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("dialog", lambda d: d.accept())
            page.goto(f"{base_url}/web/exam.html?test={TID}")
            enter_pin(page, "Ann", pin)

            # -- step 1: Instructions ---------------------------------------- #
            page.locator("#iqNextBtn").wait_for()
            cur = page.locator(".iq-steps li.cur")
            assert "Instructions" in cur.inner_text()
            assert page.locator("#iqBackBtn").count() == 0
            assert "Put an X on every pair" in page.locator("#iqStep").inner_text()
            assert page.locator("#iqPhase").inner_text().lower() == "time"
            assert page.locator("#timer").inner_text() == "0:34"  # the part's length
            page.wait_for_function(
                "() => /starts by itself in 0:[0-9]{2}/.test("
                "document.querySelector('#iqAuto')?.textContent || '')"
            )
            # -- step 2: Example + practice --------------------------------- #
            page.click("#iqNextBtn")
            assert "Example" in cur.inner_text()
            body = page.locator("#iqStep").inner_text()
            assert "1234 — 1243" in body and "Do these practice problems now" in body
            p1 = page.locator("#ipr-0")
            p1.locator(".iq-check").click()  # left unmarked, should be marked
            assert (
                p1.locator(".iq-fb.bad")
                .inner_text()
                .startswith("✗ Not quite. The answer is mark it with X.")
            )
            p1.locator(".iq-ptoggle").click()
            p1.locator(".iq-check").click()
            assert "Correct" in p1.locator(".iq-fb.ok").inner_text()
            page.locator("#ipr-1 .iq-check").click()  # identical pair, unmarked = right
            assert page.locator("#ipr-1 .iq-fb.ok").count() == 1
            # Back to the instructions and forward again: the practice is kept.
            _shot(page, "timer", "a-practice-checked")
            page.click("#iqBackBtn")
            assert "Instructions" in cur.inner_text()
            page.click("#iqNextBtn")
            assert page.locator("#ipr-0 .iq-fb.ok").count() == 1
            assert page.locator("#ipr-0 .iq-ptoggle").get_attribute("aria-pressed") == "true"
            # -- step 3: Ready ------------------------------------------------ #
            page.click("#iqNextBtn")
            assert "Ready" in cur.inner_text()
            summary = page.locator("#iqReadySummary").inner_text()
            assert "6 questions" in summary and "34 s" in summary
            assert "about 6 s per question" in summary
            assert "The clock does not stop" in page.locator("#iqStep").inner_text()
            assert page.locator(".iq-ready-study").count() == 0
            assert _server_answers(page) == {}  # practice is never saved
            page.click("#iqBeginBtn")

            # -- answering: big sticky clock, notice, colours, time up ------- #
            page.wait_for_function("() => document.querySelectorAll('.iq-row').length === 6")
            assert page.locator("#iqPhase").inner_text().lower() == "time left"
            _clock_starts_at(page, 34)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            box = page.locator("#timer").bounding_box()
            assert box is not None and 0 <= box["y"] < 80 and box["height"] >= 28
            notice = page.locator("#iqNotice")
            notice.wait_for(state="visible", timeout=10_000)
            assert "30 seconds left" in notice.inner_text()
            _shot(page, "timer", "b-30s-notice")
            assert notice.evaluate("e => getComputedStyle(e).pointerEvents") == "none"
            assert "low" in (page.locator("#timer").get_attribute("class") or "")
            page.click(f"#tg-{TID}-r1")  # the notice never blocks a tap
            notice.wait_for(state="hidden", timeout=8_000)
            page.wait_for_function(
                "() => document.querySelector('#timer').classList.contains('crit')",
                timeout=25_000,
            )
            up = page.locator("#iqTimeUp")
            up.wait_for(state="visible", timeout=20_000)
            _shot(page, "timer", "c-time-up")
            assert "Time is up" in up.inner_text() and "Pairs has ended" in up.inner_text()
            page.locator("#iqTimeUpOk").click()
            up.wait_for(state="hidden")
            assert page.locator("#iqPart").text_content() == "Part 2 of 3"
            assert "Instructions" in cur.inner_text()
            assert _server_answers(page).get(f"{TID}-r1") == "1"

            # -- subtest 2: drawing practice, then the intro cap starts it --- #
            page.click("#iqNextBtn")
            page.click("#dthumb-p-0")
            page.locator("#drawOverlay").wait_for(state="visible")
            assert page.locator("#drawOvClock").inner_text() == "2:00"
            cbox = page.locator("#drawOvCanvas").bounding_box()
            assert cbox is not None
            page.mouse.move(cbox["x"] + cbox["width"] * 0.1, cbox["y"] + cbox["height"] * 0.5)
            page.mouse.down()
            page.mouse.move(
                cbox["x"] + cbox["width"] * 0.9, cbox["y"] + cbox["height"] * 0.5, steps=10
            )
            page.mouse.up()
            page.click("#drawOvOk")
            page.locator("#drawOverlay").wait_for(state="hidden")
            assert _ink(page, "#dcv-p-0") > 0  # the try-it strokes on the thumbnail
            page.click("#ipr-0 .iq-show")
            page.locator("#ipfb-0 img.iq-pans").wait_for()
            assert "One line across the middle." in page.locator("#ipfb-0").inner_text()
            _shot(page, "timer", "d-drawing-practice-answer")
            assert set(_server_answers(page)) == {f"{TID}-r1"}  # practice not saved
            page.click("#iqNextBtn")
            page.locator("#iqBeginBtn").wait_for()
            # Nobody presses Start: the server cap (20 s) begins the subtest.
            page.locator(f"#dthumb-{TID}-d1").wait_for(timeout=30_000)
            _clock_starts_at(page, 120)
            s2 = _sections(page)["sections"][1]
            assert s2["started_by"] == "intro_cap"
            page.click("#iqFinishBtn")

            # -- subtest 3: text + choice practice, study line --------------- #
            page.wait_for_function(
                "() => document.querySelector('#iqPart')?.textContent === 'Part 3 of 3'"
            )
            page.click("#iqNextBtn")
            pin_box = page.locator("#ipr-0 .iq-pin")
            assert pin_box.get_attribute("inputmode") == "numeric"
            pin_box.fill("7")
            pin_box.press("Enter")
            assert "The answer is 8" in page.locator("#ipr-0 .iq-fb.bad").inner_text()
            pin_box.fill(" 8 ")
            page.locator("#ipr-0 .iq-check").click()
            assert page.locator("#ipr-0 .iq-fb.ok").count() == 1
            page.locator("#ipr-1 .iq-popt", has_text="apple").click()
            page.locator("#ipr-1 .iq-check").click()
            assert "(c) chair" in page.locator("#ipr-1 .iq-fb.bad").inner_text()
            page.locator("#ipr-1 .iq-popt", has_text="chair").click()
            page.locator("#ipr-1 .iq-check").click()
            assert page.locator("#ipr-1 .iq-fb.ok").count() == 1
            _shot(page, "timer", "e-text-choice-practice")
            page.click("#iqNextBtn")
            summary = page.locator("#iqReadySummary").inner_text()
            assert "2 questions" in summary and "6 s" in summary
            assert "about 3 s per question" in summary
            assert (
                "First you study for 6 s, then you answer."
                in page.locator(".iq-ready-study").inner_text()
            )
            _shot(page, "timer", "f-ready-study")
            page.click("#iqBeginBtn")
            page.locator(".iq-study-text").wait_for()
            assert page.locator("#iqPhase").inner_text().lower() == "study"
            page.locator("#done").wait_for(state="visible", timeout=30_000)
            assert not errors, errors
            browser.close()
    except PlaywrightError as exc:
        _skip_without_chromium(exc)
        raise


# --------------------------------------------------------------------------- #
# Responsive.
# --------------------------------------------------------------------------- #
VIEWPORTS = {
    "phone-portrait": {"width": 390, "height": 844, "touch": True},
    "phone-landscape": {"width": 844, "height": 390, "touch": True},
    "tablet": {"width": 768, "height": 1024, "touch": True},
    "desktop": {"width": 1366, "height": 768, "touch": False},
}


LIMITS = (300, 240, 180)  # distinct per section: each countdown must start at its own


@pytest.fixture
def roomy_server(tmp_path: Path) -> Iterator[tuple[str, str]]:
    data = fx.iq_test_json(TID)
    for sec, limit in zip(data["sections"], LIMITS, strict=True):
        sec["time_limit_seconds"] = limit
    data["sections"][2]["study"] = {
        "text": "cat dog sun",
        "images": [f"iq-cell-{k}.png" for k in range(N_CELLS)],
        "study_seconds": 5,
    }
    yield from _boot(tmp_path, data, ["Ann"], cap=600)


def _no_overflow(page: Any, where: str) -> None:
    page.wait_for_timeout(60)
    assert page.evaluate(NO_OVERFLOW), f"horizontal overflow on {where}"


def _tappable(page: Any, selector: str, min_px: float = 44) -> None:
    loc = page.locator(selector).first
    loc.scroll_into_view_if_needed()
    box = loc.bounding_box()
    vw = page.evaluate("window.innerWidth")
    assert box is not None, selector
    assert box["height"] >= min_px - 0.5 and box["width"] >= min_px - 0.5, (selector, box)
    assert box["x"] >= 0 and box["x"] + box["width"] <= vw + 0.5, (selector, box)


def _rotate(page: Any) -> None:
    vp = page.viewport_size
    page.set_viewport_size({"width": vp["height"], "height": vp["width"]})
    page.wait_for_timeout(250)


def _touch_stroke(page: Any, cdp: Any, selector: str, pts: list[tuple[float, float]]) -> None:
    box = page.locator(selector).bounding_box()
    assert box is not None

    def tp(u: float, v: float) -> dict[str, float]:
        return {"x": box["x"] + u * box["width"], "y": box["y"] + v * box["height"]}

    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [tp(*pts[0])]})
    for (u0, v0), (u1, v1) in zip(pts, pts[1:], strict=False):
        for k in range(1, 9):
            f = k / 8
            point = tp(u0 + (u1 - u0) * f, v0 + (v1 - v0) * f)
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [point]})
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


def _touch_swipe(page: Any, cdp: Any, x: float, y: float, dy: float) -> None:
    """A finger drag from (x, y) by dy px (negative = finger moves up = page scrolls
    down), with real pauses so the browser treats it as a pan."""
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
    for k in range(1, 16):
        point = {"x": x, "y": y + dy * k / 15}
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [point]})
        page.wait_for_timeout(16)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.wait_for_timeout(300)


def _stage_inside_viewport(page: Any) -> dict[str, float]:
    box = page.locator("#drawOvStage").bounding_box()
    vw, vh = page.evaluate("[window.innerWidth, window.innerHeight]")
    assert box is not None
    assert box["x"] >= -0.5 and box["y"] >= -0.5
    assert box["x"] + box["width"] <= vw + 0.5 and box["y"] + box["height"] <= vh + 0.5
    assert abs(box["width"] / box["height"] - fx.W / fx.H) < 0.03  # aspect kept
    return box


@pytest.mark.parametrize("tag", list(VIEWPORTS))
def test_responsive(roomy_server: tuple[str, str], tag: str) -> None:
    base_url, db_url = roomy_server
    vp = VIEWPORTS[tag]
    touch = bool(vp["touch"])
    pin = roster_pin(db_url, "ux-ann")
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(
                viewport={"width": vp["width"], "height": vp["height"]},
                has_touch=touch,
                is_mobile=touch and vp["width"] < 900,
                device_scale_factor=2 if touch else 1,
            )
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("dialog", lambda d: d.accept())
            click = page.tap if touch else page.click

            page.goto(f"{base_url}/web/exam.html?test={TID}")
            page.locator(".name-btn", has_text="Ann").first.wait_for()
            _no_overflow(page, "name list")
            _shot(page, tag, "00-names")
            enter_pin(page, "Ann", pin)

            # -- intro steps -------------------------------------------------- #
            page.locator("#iqNextBtn").wait_for()
            _no_overflow(page, "instructions")
            _tappable(page, "#iqNextBtn")
            head = page.locator("#examHeader").bounding_box()
            assert head is not None
            if tag == "phone-landscape":
                assert head["height"] <= 48, head  # compact header on a short screen
            assert page.locator("#timer").is_visible()
            _shot(page, tag, "01-instructions")
            click("#iqNextBtn")
            _no_overflow(page, "example")
            _tappable(page, "#ipr-0 .iq-ptoggle")
            _tappable(page, "#ipr-0 .iq-check")
            _shot(page, tag, "02-example")
            click("#iqNextBtn")
            _no_overflow(page, "ready")
            _tappable(page, "#iqBeginBtn")
            _shot(page, tag, "03-ready")
            click("#iqBeginBtn")

            # -- subtest 1: mark rows, rotation keeps them --------------------- #
            page.wait_for_function("() => document.querySelectorAll('.iq-row').length === 6")
            _clock_starts_at(page, LIMITS[0])
            _no_overflow(page, "mark rows")
            _tappable(page, f"#tg-{TID}-r2")
            click(f"#tg-{TID}-r2")
            _shot(page, tag, "04-marks")
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            tb = page.locator("#timer").bounding_box()
            assert tb is not None and tb["y"] >= 0 and tb["y"] < 60  # sticky clock
            if touch:
                _rotate(page)
                _no_overflow(page, "mark rows rotated")
                assert page.locator(f"#tg-{TID}-r2").get_attribute("aria-pressed") == "true"
                _rotate(page)
            click("#iqFinishBtn")

            # -- subtest 2: drawings via the overlay --------------------------- #
            page.wait_for_function(
                "() => document.querySelector('#iqPart')?.textContent === 'Part 2 of 3'"
            )
            to_ready(page)
            click("#iqBeginBtn")
            thumb = f"#dthumb-{TID}-d1"
            page.locator(thumb).wait_for()
            page.wait_for_function(
                f"() => document.querySelector('#dstage-{TID}-d1 .draw-pic').naturalWidth > 0"
            )
            _clock_starts_at(page, LIMITS[1])
            _no_overflow(page, "drawing page")
            _tappable(page, thumb)
            _shot(page, tag, "05-drawing-page")
            page.locator(thumb).scroll_into_view_if_needed()
            click(thumb)
            page.locator("#drawOverlay").wait_for(state="visible")
            _no_overflow(page, "drawing overlay")
            before = _stage_inside_viewport(page)
            for sel in ("#drawOvOk", "#drawOvUndo", "#drawOvClear", "#drawOvCancel"):
                _tappable(page, sel)
            assert page.locator("#drawOvClock").is_visible()
            zig = [(0.65, 0.65), (0.85, 0.7), (0.65, 0.78), (0.85, 0.85)]
            if touch:
                cdp = ctx.new_cdp_session(page)
                _touch_stroke(page, cdp, "#drawOvCanvas", zig)
            else:
                box = page.locator("#drawOvCanvas").bounding_box()
                assert box is not None
                page.mouse.move(
                    box["x"] + zig[0][0] * box["width"], box["y"] + zig[0][1] * box["height"]
                )
                page.mouse.down()
                for u, v in zig[1:]:
                    page.mouse.move(
                        box["x"] + u * box["width"], box["y"] + v * box["height"], steps=8
                    )
                page.mouse.up()
            assert _ink(page, "#drawOvCanvas") > 0
            _shot(page, tag, "06-overlay")
            if touch:  # rotate with the overlay open: re-fits, strokes kept
                _rotate(page)
                after = _stage_inside_viewport(page)
                assert abs(after["width"] - before["width"]) > 5
                assert _ink(page, "#drawOvCanvas") > 0
                _shot(page, tag, "07-overlay-rotated")
                _rotate(page)
                _stage_inside_viewport(page)
            click("#drawOvOk")
            page.locator("#drawOverlay").wait_for(state="hidden")
            _saved(page)
            saved = json.loads(_server_answers(page)[f"{TID}-d1"])
            assert len(saved["strokes"]) == 1 and (saved["w"], saved["h"]) == (fx.W, fx.H)
            assert _ink(page, f"#dcv-{TID}-d1") > 0  # the thumbnail shows the strokes
            _no_overflow(page, "drawing page after confirm")
            _shot(page, tag, "08-drawing-done")
            if touch:
                # A finger swipe that starts ON a thumbnail scrolls the page.
                page.evaluate("window.scrollTo(0, 0)")
                if page.evaluate("document.documentElement.scrollHeight > innerHeight + 40"):
                    box = page.locator(f"#dthumb-{TID}-d2").bounding_box()
                    if box is None or box["y"] + box["height"] / 2 > vp["height"]:
                        box = page.locator(thumb).bounding_box()
                    assert box is not None
                    x = box["x"] + box["width"] / 2
                    y = max(box["y"], 0) + min(box["height"], vp["height"] - box["y"]) / 2
                    hit = page.evaluate(
                        "([x, y]) => document.elementFromPoint(x, y)?.closest('.draw-thumb')?.id",
                        [x, y],
                    )
                    assert hit and hit.startswith("dthumb-")
                    _touch_swipe(page, cdp, x, y, -150)
                    assert page.evaluate("window.scrollY") > 20
                    assert page.locator("#drawOverlay").is_hidden()  # a swipe is no tap
                    # A tap during the fling only stops the scroll (as on a real
                    # phone): let it settle, then press Finish with the mouse.
                    page.wait_for_timeout(800)
                    click = page.click  # taps right after a fling may only stop it
            page.click("#iqFinishBtn")

            # -- subtest 3: study, then recall ---------------------------------- #
            page.wait_for_function(
                "() => document.querySelector('#iqPart')?.textContent === 'Part 3 of 3'"
            )
            click("#iqNextBtn")
            _no_overflow(page, "text practice")
            assert page.locator("#ipr-0 .iq-pin").get_attribute("inputmode") == "numeric"
            _tappable(page, "#ipr-1 .iq-popt")
            click("#iqNextBtn")
            click("#iqBeginBtn")
            page.locator(".iq-study-text").wait_for()
            # The study cells: a legible grid, every cell its own gated blob.
            page.wait_for_function(
                f"() => document.querySelectorAll('#iqStudyGrid img').length === {N_CELLS}"
                " && [...document.querySelectorAll('#iqStudyGrid img')]"
                ".every(i => i.src.startsWith('blob:') && i.complete && i.naturalWidth === 90)"
            )
            cols = page.evaluate(
                "getComputedStyle(document.querySelector('#iqStudyGrid'))"
                ".gridTemplateColumns.split(' ').length"
            )
            assert cols == (4 if vp["width"] < 420 else 5 if vp["width"] < 800 else 10), cols
            widths = page.eval_on_selector_all(
                "#iqStudyGrid .iq-study-cell",
                "els => els.map(e => e.getBoundingClientRect().width)",
            )
            assert min(widths) >= 64, widths
            _no_overflow(page, "study")
            _shot(page, tag, "09-study")
            page.locator(".iq-card").first.wait_for(timeout=15_000)
            assert page.locator("#iqStudyGrid").count() == 0  # gone with the window
            _clock_starts_at(page, LIMITS[2])
            _no_overflow(page, "recall")
            _tappable(page, f"#q-{TID}-m1 label.opt")
            _shot(page, tag, "10-recall")
            click("#iqFinishBtn")
            page.locator("#done").wait_for(state="visible", timeout=20_000)
            _no_overflow(page, "done")
            _shot(page, tag, "11-done")
            assert not errors, errors
            browser.close()
    except PlaywrightError as exc:
        _skip_without_chromium(exc)
        raise
