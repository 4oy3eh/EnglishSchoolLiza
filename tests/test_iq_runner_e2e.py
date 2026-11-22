"""E2E (Phase 17.5 / 17.6): the IQ runner and the teacher's IQ views in Chromium.

Boots the API on a throwaway DB with the SYNTHETIC IQ test (`tests/iq_fixture.py`,
never /private or /pdf; short clocks: subtest 3 = 6 s study + 6 s recall) plus the
seeded demo test, then drives the real pages:

* student: intro (instructions + auto-start countdown, no items yet) -> Begin ->
  mark toggles on one page -> reload resumes the answering phase with the marks ->
  "Finish this part" -> drawing canvases (mouse strokes, Undo) saved as the grader's
  {"w","h","strokes"} JSON in picture pixels -> study screen (survives a reload),
  then it disappears and the recall items load -> the subtest expires by itself
  -> the runner submits after the last subtest;
* teacher: birth date set in the roster, the IQ block on the result card (age,
  composites, overall, disclaimer), the drawings with the target overlay toggle,
  an override of a drawing changes the IQ, and the printable report shows it;
* regression: a normal test still runs the one-part-at-a-time runner and never
  asks for section clocks.

Set `IQ_E2E_SHOTS=<dir>` to save screenshots. Skips cleanly without Chromium.
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import time
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.persistence.models  # noqa: F401  (register tables on Base.metadata)
from app.content.seed import SEED_TEST_ID, seed
from app.content.service import ContentService
from app.content.storage import FilesystemStorage
from app.core.db import Base
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import RosterEntry
from tests import iq_fixture as fx
from tests.pin_helpers import enter_pin, roster_pin
from tests.test_e2e_full import _free_port, _wait_for_health

sync_api = pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "iq-e2e-pw"
TID = "iq-demo"
STUDY_SECONDS = 6
RECALL_SECONDS = 6


def _png(w: int, h: int, box: tuple[float, float, float, float] | None = None) -> bytes:
    """A real (decodable) white RGB PNG, optionally with a black square outline."""
    rows = []
    for y in range(h):
        row = bytearray([0])  # filter: none
        for x in range(w):
            dark = False
            if box is not None:
                bx, by, bw, bh = (box[0] * w, box[1] * h, box[2] * w, box[3] * h)
                on_x = bx <= x <= bx + bw
                on_y = by <= y <= by + bh
                dark = (on_y and (abs(x - bx) < 2 or abs(x - bx - bw) < 2)) or (
                    on_x and (abs(y - by) < 2 or abs(y - by - bh) < 2)
                )
            row += b"\x00\x00\x00" if dark else b"\xff\xff\xff"
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )


def _iq_json() -> dict[str, Any]:
    data = fx.iq_test_json(TID)
    s3 = data["sections"][2]
    s3["time_limit_seconds"] = RECALL_SECONDS
    s3["study"]["study_seconds"] = STUDY_SECONDS
    s3["study"]["asset_id"] = "iq-study.png"  # a study picture: gated, fetched as a blob
    return data


@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[tuple[str, str]]:
    db_url = f"sqlite:///{(tmp_path / 'iq.db').as_posix()}"
    assets_dir = tmp_path / "assets"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    w, h = int(fx.W), int(fx.H)
    sq = fx.SQUARE
    assets = {
        "iq-pic.png": _png(w, h),
        "iq-square.png": _png(w, h, (sq["x"], sq["y"], sq["w"], sq["h"])),
        "iq-sample.png": _png(80, 40),
        "iq-study.png": _png(120, 60),
    }
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        storage = FilesystemStorage(assets_dir)
        seed(s, storage)
        content = ContentService(ContentRepository(s), storage)
        content.import_test(_iq_json(), assets)
        assert content.publish(TID)
        repo = AttemptRepository(s)
        repo.add_roster_entry(RosterEntry(id="iq-ann", test_id=TID, display_name="Ann"))
        repo.add_roster_entry(
            RosterEntry(id="demo-mila", test_id=SEED_TEST_ID, display_name="Mila")
        )
        s.commit()
    engine.dispose()

    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": db_url,
        "ASSETS_DIR": str(assets_dir),
        "LOG_LEVEL": "INFO",
        "TEACHER_PASSWORD": PASSWORD,
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
        yield f"http://127.0.0.1:{port}", db_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


def _skip_without_chromium(exc: PlaywrightError) -> None:
    if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
        pytest.skip(f"Chromium not installed: {exc}")


def _shot(page: Any, name: str) -> None:
    out = os.environ.get("IQ_E2E_SHOTS")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(out) / f"{name}.png"), full_page=True)


def _saved(page: Any) -> None:
    page.wait_for_function(
        "() => (document.querySelector('#saveStatus')?.textContent || '').startsWith('Saved')"
    )


def _server_answers(page: Any) -> dict[str, Any]:
    return page.evaluate(
        """async () => {
          const a = JSON.parse(localStorage.getItem('exam:active'));
          const r = await fetch(`/exam/attempts/${a.attemptId}/answers`,
                                {headers: {'X-Attempt-Token': a.token}});
          return r.json();
        }"""
    )


def _stroke(page: Any, canvas: Any, pts: list[tuple[float, float]]) -> None:
    """One pen stroke over the canvas; `pts` in normalized picture coordinates."""
    canvas.scroll_into_view_if_needed()
    box = canvas.bounding_box()
    assert box is not None

    def xy(p: tuple[float, float]) -> tuple[float, float]:
        return box["x"] + p[0] * box["width"], box["y"] + p[1] * box["height"]

    page.mouse.move(*xy(pts[0]))
    page.mouse.down()
    for p in pts[1:]:
        page.mouse.move(*xy(p), steps=12)
    page.mouse.up()


def _in_square(s: float, t: float) -> tuple[float, float]:
    sq = fx.SQUARE
    return sq["x"] + s * sq["w"], sq["y"] + t * sq["h"]


def test_iq_runner_then_teacher_iq_block(live_server: tuple[str, str]) -> None:
    base_url, db_url = live_server
    pin = roster_pin(db_url, "iq-ann")
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 900, "height": 900})
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("dialog", lambda d: d.accept())

            # -- subtest 1: intro, then mark toggles on one page ------------------ #
            page.goto(f"{base_url}/web/exam.html?test={TID}")
            enter_pin(page, "Ann", pin)
            page.locator("#iqBeginBtn").wait_for()
            assert page.locator("#runner").is_hidden()
            assert page.locator("#iqPart").text_content() == "Part 1 of 3"
            assert "Put an X on every pair" in page.locator("#iqBody").inner_text()
            assert "1234 — 1243" in page.locator("#iqBody").inner_text()  # a sample
            page.wait_for_function(
                "() => /starts by itself in [0-9]+:[0-9]{2}/.test("
                "document.querySelector('#iqAuto')?.textContent || '')"
            )
            assert page.locator(".iq-row").count() == 0  # no items before Begin
            _shot(page, "1-intro")
            page.click("#iqBeginBtn")
            page.wait_for_function("() => document.querySelectorAll('.iq-row').length === 6")
            for rid in ("r1", "r3", "r5"):
                page.click(f"#tg-{TID}-{rid}")
            _saved(page)
            # Keyboard: ArrowDown moves between rows.
            page.focus(f"#tg-{TID}-r1")
            page.keyboard.press("ArrowDown")
            assert page.evaluate("document.activeElement.id") == f"tg-{TID}-r2"
            _shot(page, "2-toggles")

            # A reload resumes the answering phase with the marks restored.
            page.reload()
            page.wait_for_function("() => document.querySelectorAll('.iq-row').length === 6")
            pressed = page.eval_on_selector_all(
                ".iq-toggle", "els => els.map(e => e.getAttribute('aria-pressed'))"
            )
            assert pressed == ["true", "false", "true", "false", "true", "false"]
            page.click("#iqFinishBtn")  # confirm accepted by the dialog handler

            # -- subtest 2: drawings --------------------------------------------- #
            page.wait_for_function(
                "() => document.querySelector('#iqPart')?.textContent === 'Part 2 of 3'"
            )
            page.click("#iqBeginBtn")
            d1 = page.locator(f"#dcv-{TID}-d1")
            d2 = page.locator(f"#dcv-{TID}-d2")
            d1.wait_for()
            page.wait_for_function(
                f"() => document.querySelector('#dstage-{TID}-d1 .draw-pic').naturalWidth > 0"
            )
            zig = [(0.65, 0.65), (0.85, 0.65), (0.65, 0.72), (0.85, 0.72), (0.65, 0.79),
                   (0.85, 0.79), (0.65, 0.86), (0.85, 0.86)]  # fmt: skip
            _stroke(page, d1, zig)
            _stroke(page, d1, [(0.1, 0.1), (0.3, 0.2)])  # a stray line ...
            page.locator(f"#q-{TID}-d1 [data-act='undo']").click()  # ... undone
            assert page.locator(f"#q-{TID}-d2 .draw-square").count() == 1  # lines frame
            _stroke(page, d2, [_in_square(0.0, 0.5), _in_square(0.5, 0.5), _in_square(0.5, 1.0)])
            _saved(page)
            saved = _server_answers(page)
            region = json.loads(saved[f"{TID}-d1"])
            lines = json.loads(saved[f"{TID}-d2"])
            assert (region["w"], region["h"]) == (fx.W, fx.H)  # picture pixels
            assert len(region["strokes"]) == 1 and len(lines["strokes"]) == 1
            xs = [pt[0] / region["w"] for pt in region["strokes"][0]]
            assert 0.6 < min(xs) and max(xs) < 0.9  # inside the zone, in picture coords
            assert len(region["strokes"][0]) <= 400  # thinned
            _shot(page, "3-drawing")
            page.click("#iqFinishBtn")

            # -- subtest 3: study (survives a reload), then recall, then expiry ---- #
            page.wait_for_function(
                "() => document.querySelector('#iqPart')?.textContent === 'Part 3 of 3'"
            )
            assert "remember" in page.locator("#iqBody").inner_text().lower()
            page.click("#iqBeginBtn")
            page.locator(".iq-study-text", has_text="cat dog sun").wait_for()
            # The study picture: an object URL fetched with the attempt token (the
            # public /assets/ refuses it), really decoded.
            page.wait_for_function(
                "() => { const i = document.querySelector('.iq-study-img');"
                " return i && i.src.startsWith('blob:') && i.complete && i.naturalWidth === 120; }"
            )
            assert page.request.get(f"{base_url}/assets/iq-study.png").status == 404
            _shot(page, "4-study")
            page.reload()
            page.locator(".iq-study-text", has_text="cat dog sun").wait_for()
            page.wait_for_function(
                "() => document.querySelector('.iq-study-img')?.src.startsWith('blob:')"
            )
            page.locator(".iq-card").first.wait_for(timeout=(STUDY_SECONDS + 5) * 1000)
            assert page.locator(".iq-study-text").count() == 0
            assert page.locator(".iq-study-img").count() == 0  # gone with the window
            assert "cat dog sun" not in page.locator("#iqBody").inner_text()
            page.locator(f"#q-{TID}-m1 label", has_text="cat").click()
            page.locator(f"#q-{TID}-m2 label", has_text="sun").click()
            _saved(page)
            _shot(page, "5-recall")
            # Nobody presses Finish: the subtest expires, the runner submits.
            page.locator("#done").wait_for(state="visible", timeout=(RECALL_SECONDS + 10) * 1000)
            assert page.locator("#doneTitle").text_content() == "Test submitted"
            assert not errors, errors

            # -- teacher: birth date, IQ block, drawings, override, report -------- #
            t = ctx.new_page()
            t.on("dialog", lambda d: d.accept())
            t.goto(f"{base_url}/web/teacher.html")
            t.fill("#password", PASSWORD)
            t.click("#loginBtn")
            row = t.locator(f'#testsTable tr[data-test-id="{TID}"]')
            row.wait_for()
            row.locator('[data-act="open"]').click()
            t.locator("#birthTh").wait_for(state="visible")
            ann = t.locator("#rosterTable tbody tr", has_text="Ann")
            ann.locator("input[data-birth]").fill("2010-01-01")
            t.locator(".toast.ok", has_text="Дата рождения «Ann» сохранена").wait_for()

            deadline = time.monotonic() + 30
            while True:  # the finalize runs in the background after the submit
                t.locator("#resultsTable tbody tr").first.wait_for()
                t.locator('#resultsTable [data-act="detail"]').first.click()
                try:
                    t.locator("#iqBlock [data-iq-overall]").wait_for(timeout=2000)
                    if t.locator("#detailBody .dr-card").count() == 2:
                        break
                except PlaywrightError:
                    pass
                assert time.monotonic() < deadline, "no IQ block on the result"
            block = t.locator("#iqBlock")
            text = block.inner_text()
            assert "возраст: 16" in text and "Неклиническая оценка" in text
            assert "«левое полушарие»" in text and "«правое полушарие»" in text
            assert block.locator("table.iq-sections tbody tr").count() == 3
            before = block.locator("[data-iq-overall]").inner_text()
            assert before not in ("", "—")
            card = t.locator(f'#detailBody .dr-card[data-drawing="{TID}-d2"]')
            assert card.locator("polyline").count() == 1  # the student's stroke
            assert card.locator(".dr-target").is_hidden()
            t.check("#drTargetToggle")
            assert card.locator(".dr-target").is_visible()
            assert card.locator(".dr-target line").count() == 2  # the reference "L"
            assert "уверенность" in card.inner_text()
            _shot(t, "6-teacher")

            # Override the drawing the other way -> the IQ is recomputed.
            correct = "✓ автоматически: верно" in card.inner_text()
            want = "✗ не засчитывать" if correct else "✓ засчитать"
            card.locator("button", has_text=want).click()
            t.locator(".toast.ok", has_text="Оценка сохранена").wait_for()
            t.wait_for_function(
                "(b) => (document.querySelector('#iqBlock [data-iq-overall]')?.textContent"
                " || b) !== b",
                arg=before,
            )
            after = t.locator("#iqBlock [data-iq-overall]").inner_text()
            assert (
                t.locator(f'#detailBody .dr-card[data-drawing="{TID}-d2"]')
                .locator("text=вручную")
                .count()
                == 1
            )

            with t.expect_popup() as pop:
                t.click("#printBtn")
            report = pop.value
            report.locator("#iqBlock").wait_for()
            assert after in report.locator("#iqBlock").inner_text()
            assert "Неклиническая оценка" in report.locator("#iqBlock").inner_text()
            _shot(report, "7-report")
            browser.close()
    except PlaywrightError as exc:
        _skip_without_chromium(exc)
        raise


def test_normal_test_keeps_the_part_runner(live_server: tuple[str, str]) -> None:
    base_url, db_url = live_server
    pin = roster_pin(db_url, "demo-mila")
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            errors: list[str] = []
            calls: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("request", lambda r: calls.append(r.url))
            page.goto(f"{base_url}/web/exam.html?test={SEED_TEST_ID}")
            enter_pin(page, "Mila", pin)
            page.locator("#runner").wait_for(state="visible")
            page.locator("#sectionBody .qcard").first.wait_for()
            assert page.locator("#navigator").is_visible()
            assert page.locator("#iqRunner").is_hidden()
            assert page.locator("#nextBtn").is_visible()
            assert not any("/sections" in u for u in calls)  # never asks for clocks
            assert not errors, errors
            browser.close()
    except PlaywrightError as exc:
        _skip_without_chromium(exc)
        raise
