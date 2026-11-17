"""E2E: the exam window in the runner + dashboard, and the Starters / Flyers UI switch.

Boots the API on a throwaway DB with the seeded A2 demo test plus Starters and
Flyers copies of it (`PRE_A1_STARTERS` / `A2_FLYERS`, same content), then drives the
real pages in Chromium:

* Starters / Flyers offer EN / УКР / РУС like Movers; a Starters student starts in
  Russian (chrome Russian, content untouched).
* Window, in the runner: a not-yet-open test shows «Тест відкриється: …» on the
  picker (uk) and a PIN start answers with the localized "not open yet" message; a
  closed test says so and refuses a new start, while the PIN of a student who
  already started still resumes the SAME attempt.
* Window, in the dashboard: the bank shows the window column, the header the
  server time; «Открыть сейчас» opens the test (the runner flips to startable),
  «Закрыть сейчас» closes it.

Skips cleanly without Chromium, like the other Playwright files.
"""

from __future__ import annotations

import os
import subprocess
import sys
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.persistence.models  # noqa: F401  (register tables on Base.metadata)
from app.content.editor import reid_test
from app.content.seed import SEED_TEST_ID, build_sample_test, seed
from app.content.storage import FilesystemStorage
from app.core.db import Base
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import RosterEntry
from tests.pin_helpers import http_json, roster_pin
from tests.test_e2e_full import _free_port, _wait_for_health

sync_api = pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "window-e2e-pw"
SAMPLE = build_sample_test()
STARTERS = "starters-demo"
FLYERS = "flyers-demo"
LATER = "flyers-later"  # opens tomorrow
CLOSED = "starters-closed"  # closes in 2 min, then the test closes it


@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[tuple[str, str]]:
    db_url = f"sqlite:///{(tmp_path / 'window.db').as_posix()}"
    assets_dir = tmp_path / "assets"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    copies = {
        STARTERS: ("PRE_A1_STARTERS", "Starters — Demo", {}),
        FLYERS: ("A2_FLYERS", "Flyers — Demo", {}),
        LATER: (
            "A2_FLYERS",
            "Flyers — Tomorrow",
            {"opens_at": now + timedelta(days=1), "closes_at": now + timedelta(days=1, hours=1)},
        ),
        CLOSED: ("PRE_A1_STARTERS", "Starters — Closing", {"closes_at": now + timedelta(hours=1)}),
    }
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        seed(s, FilesystemStorage(assets_dir))
        repo = AttemptRepository(s)
        for test_id, (level, title, window) in copies.items():
            copy = reid_test(SAMPLE, test_id, status="published").model_copy(
                update={"level": level, "title": title, **window}
            )
            ContentRepository(s).add_test(copy)
            for name in ("Mila", "Oleg"):
                repo.add_roster_entry(
                    RosterEntry(
                        id=f"{test_id}-{name.lower()}", test_id=test_id, display_name=name
                    )
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


def _wait_text(page, sel: str, text: str) -> None:  # noqa: ANN001
    page.wait_for_function(
        "([s, t]) => (document.querySelector(s)?.textContent || '').includes(t)",
        arg=[sel, text],
    )


def _pin(page, name: str, pin: str) -> None:  # noqa: ANN001
    page.locator(".name-btn", has_text=name).click()
    page.wait_for_selector("#pinScreen:not(.hidden)")
    page.fill("#pinInput", pin)
    page.click("#pinStartBtn")


def test_starters_and_flyers_offer_the_uk_ru_switch(live_server: tuple[str, str]) -> None:
    base_url, db_url = live_server
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.on("dialog", lambda d: d.dismiss())
            for test_id in (FLYERS, STARTERS):
                page.goto(f"{base_url}/web/exam.html?test={test_id}")
                page.wait_for_selector(".name-btn")
                page.locator("#langSwitch").wait_for(state="visible")
                assert page.locator("#langSwitch").inner_text().split() == ["EN", "УКР", "РУС"]
                page.click('#langSwitch button[data-lang="uk"]')
                assert page.text_content("#pick h2") == "Вибери своє ім'я, щоб почати"
                assert page.is_hidden("#windowNote")  # no schedule -> no notice
            # A Starters student starts in Russian: chrome Russian, content English.
            page.click('#langSwitch button[data-lang="ru"]')
            _pin(page, "Mila", roster_pin(db_url, f"{STARTERS}-mila"))
            page.wait_for_selector("#runner:not(.hidden)")
            assert page.text_content("#prevBtn") == "Назад"
            assert page.text_content("#examTitle") == "Starters — Demo"
            assert page.text_content("#partLabel") == SAMPLE.sections[0].title
            # The seeded A2 test stays English-only.
            page.evaluate("() => localStorage.removeItem('exam:active')")
            page.goto(f"{base_url}/web/exam.html?test={SEED_TEST_ID}")
            page.wait_for_selector(".name-btn")
            assert page.is_hidden("#langSwitch")
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        _skip_without_chromium(exc)
        raise


def test_runner_shows_the_window_and_refuses_new_starts(
    live_server: tuple[str, str],
) -> None:
    base_url, db_url = live_server
    # Oleg starts the closing test while it is open (over HTTP).
    oleg_pin = roster_pin(db_url, f"{CLOSED}-oleg")
    started = http_json(
        f"{base_url}/exam/roster/{CLOSED}-oleg/start", method="POST", body={"pin": oleg_pin}
    )
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.on("dialog", lambda d: d.dismiss())

            # Not yet open (Flyers, Ukrainian UI).
            page.goto(f"{base_url}/web/exam.html?test={LATER}")
            page.wait_for_selector(".name-btn")
            note = page.locator("#windowNote")
            note.wait_for(state="visible")
            assert note.inner_text().startswith("This test opens:")
            page.click('#langSwitch button[data-lang="uk"]')
            assert note.inner_text().startswith("Тест відкриється:")
            _pin(page, "Mila", roster_pin(db_url, f"{LATER}-mila"))
            _wait_text(page, "#pinErr", "Тест ще не відкрито")
            assert "Неправильний PIN" not in (page.text_content("#pinErr") or "")
            page.click('#langSwitch button[data-lang="ru"]')
            assert "Тест ещё не открыт" in (page.text_content("#pinErr") or "")

            # The test with a schedule that is open shows "start until".
            page.goto(f"{base_url}/web/exam.html?test={CLOSED}")
            page.wait_for_selector(".name-btn")
            _wait_text(page, "#windowNote", "Начать тест можно до")

            # The teacher closes it now (dashboard API).
            token = http_json(
                f"{base_url}/admin/login", method="POST", body={"password": PASSWORD}
            )["token"]
            req = urllib.request.Request(
                f"{base_url}/admin/tests/{CLOSED}/window/close-now",
                data=b"",
                method="POST",
                headers={"Authorization": f"Bearer {token}"},
            )
            urllib.request.urlopen(req, timeout=10).read()

            page.reload()
            page.wait_for_selector(".name-btn")
            _wait_text(page, "#windowNote", "Тест закрыт")
            _pin(page, "Mila", roster_pin(db_url, f"{CLOSED}-mila"))
            _wait_text(page, "#pinErr", "Тест закрыт — начать его сейчас нельзя")
            # Oleg (already writing) continues the SAME attempt after the close.
            page.click("#pinBackBtn")
            _pin(page, "Oleg", oleg_pin)
            page.wait_for_selector("#runner:not(.hidden)")
            active = page.evaluate("() => JSON.parse(localStorage.getItem('exam:active'))")
            assert active["attemptId"] == started["attempt_id"]
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        _skip_without_chromium(exc)
        raise


def test_dashboard_window_panel_open_and_close_now(live_server: tuple[str, str]) -> None:
    base_url, _ = live_server
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.on("dialog", lambda d: d.accept())
            page.goto(f"{base_url}/web/teacher.html")
            page.fill("#password", PASSWORD)
            page.click("#loginBtn")
            row = page.locator(f'#testsTable tr[data-test-id="{LATER}"]')
            row.wait_for()
            assert "откроется" in row.locator(".win-cell").inner_text()
            seed_row = page.locator(f'#testsTable tr[data-test-id="{SEED_TEST_ID}"]')
            assert seed_row.locator(".win-cell").inner_text() == "без ограничений"
            _wait_text(page, "#serverClock", "Время сервера:")

            row.locator('[data-act="open"]').click()
            page.wait_for_selector("#rosterSection:not(.hidden)")
            page.click("#windowBox summary")
            assert page.input_value("#windowOpens")  # filled from the stored window
            # Default N = duration + 10.
            assert page.input_value("#windowNowMinutes") == str(SAMPLE.duration_minutes + 10)
            page.click("#windowOpenNowBtn")
            _wait_text(page, "#windowSummary", "открыт до")
            info = http_json(f"{base_url}/exam/tests/{LATER}/info")
            assert info["window_state"] == "open"
            assert "открыт до" in row.locator(".win-cell").inner_text()

            page.click("#windowCloseNowBtn")
            _wait_text(page, "#windowSummary", "закрыт")
            assert http_json(f"{base_url}/exam/tests/{LATER}/info")["window_state"] == "closed"

            # The PIN cards print the window on the test line (same tab = same token).
            page.goto(f"{base_url}/web/teacher-pins.html?test={LATER}")
            page.wait_for_selector(".card")
            assert " – " in page.locator(".card .test b").first.inner_text()
            page.goto(f"{base_url}/web/teacher.html")
            row = page.locator(f'#testsTable tr[data-test-id="{LATER}"]')
            row.wait_for()
            row.locator('[data-act="open"]').click()
            page.wait_for_selector("#rosterSection:not(.hidden)")
            page.click("#windowBox summary")

            # Clear: no limits again.
            page.click("#windowClearBtn")
            _wait_text(page, "#windowSummary", "без ограничений")
            assert http_json(f"{base_url}/exam/tests/{LATER}/info")["window_state"] == "open"
            assert page.input_value("#windowOpens") == ""
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        _skip_without_chromium(exc)
        raise
