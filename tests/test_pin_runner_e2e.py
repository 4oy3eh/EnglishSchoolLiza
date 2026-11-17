"""Phase 14 E2E: the runner's PIN screen + the YLE (Movers) UI-language switch.

Boots the API on a throwaway DB with the seeded A2 demo test and a Movers copy of
it (`A1_MOVERS`, same content), then drives the real runner in Chromium:

* PIN screen — empty / malformed / wrong PIN messages (auto-uppercase, tries
  left), the right PIN typed in lower case starts the exam; every attempt call and
  telemetry batch carries the session token; a reload resumes WITHOUT the PIN; a
  lost / invalid token brings the PIN screen back ("continue"), the same attempt
  resumes; "This is not me" returns to the picker; 5 wrong PINs lock the entry.
* Language — an A2 test stays English with no switcher even when a language is
  remembered; the Movers test shows EN / УКР / РУС, switching changes the chrome
  (headings, buttons, PIN messages, navigator, review, `<html lang>`) and is
  remembered across a reload, while the test content (part titles, prompts,
  options) stays exactly the authored English.

Skips cleanly without Chromium, like the other Playwright files.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
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
from tests.pin_helpers import roster_entry, roster_pin
from tests.test_e2e_full import _free_port, _wait_for_health

sync_api = pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
MOVERS_ID = "movers-demo"
SAMPLE = build_sample_test()
SECTION_TITLES = [s.title for s in SAMPLE.sections]
PROMPTS = [i.prompt for s in SAMPLE.sections for i in s.items]


@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[tuple[str, str]]:
    db_url = f"sqlite:///{(tmp_path / 'pin.db').as_posix()}"
    assets_dir = tmp_path / "assets"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        seed(s, FilesystemStorage(assets_dir))
        movers = reid_test(SAMPLE, MOVERS_ID, status="published").model_copy(
            update={"level": "A1_MOVERS", "title": "Movers — Demo"}
        )
        ContentRepository(s).add_test(movers)
        repo = AttemptRepository(s)
        for name in ("Mila", "Oleg"):
            repo.add_roster_entry(
                RosterEntry(id=f"{MOVERS_ID}-{name.lower()}", test_id=MOVERS_ID, display_name=name)
            )
        s.commit()
    engine.dispose()

    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": db_url,
        "ASSETS_DIR": str(assets_dir),
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


def _pin_err(page) -> str:  # noqa: ANN001
    return page.text_content("#pinErr") or ""


def _wait_pin_err(page, text: str) -> None:  # noqa: ANN001
    page.wait_for_function(
        "(t) => (document.querySelector('#pinErr').textContent || '').includes(t)", arg=text
    )


def _active(page) -> dict:  # noqa: ANN001
    raw = page.evaluate("() => localStorage.getItem('exam:active') || 'null'")
    return json.loads(raw or "null")


# --------------------------------------------------------------------------- #
# PIN screen.
# --------------------------------------------------------------------------- #
def test_pin_screen_start_resume_and_reauth(live_server: tuple[str, str]) -> None:
    base_url, db_url = live_server
    entry = roster_entry(base_url, SEED_TEST_ID, "Anna")
    pin = roster_pin(db_url, entry["roster_entry_id"])
    attempt_requests: list[tuple[str, str | None]] = []
    event_requests: list[tuple[str, str | None]] = []

    def on_request(req) -> None:  # noqa: ANN001
        if "/exam/attempts/" in req.url:
            attempt_requests.append((req.url, req.headers.get("x-attempt-token")))
        elif req.url.split("?")[0].endswith("/events"):
            event_requests.append((req.url, req.headers.get("x-attempt-token")))

    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            # A remembered language must not leak into a non-YLE test.
            context.add_init_script("localStorage.setItem('exam:lang', 'ru');")
            page = context.new_page()
            page.on("request", on_request)
            page_errors: list[str] = []
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            page.goto(f"{base_url}/web/exam.html?test={SEED_TEST_ID}")
            page.wait_for_selector(".name-btn")
            assert page.is_hidden("#langSwitch")
            assert page.evaluate("() => document.documentElement.lang") == "en"
            assert page.text_content("#pick h2") == "Choose your name to begin"

            page.locator(".name-btn", has_text="Anna").click()
            page.wait_for_selector("#pinScreen:not(.hidden)")
            assert page.text_content("#pinHello") == "Hi, Anna!"
            assert page.is_hidden("#pick")
            assert page.is_visible("#disclosure")
            assert page.text_content("#pinStartBtn") == "Start the test"

            page.click("#pinStartBtn")  # empty
            _wait_pin_err(page, "Type your 4-character PIN.")
            page.fill("#pinInput", "zz")
            page.click("#pinStartBtn")
            _wait_pin_err(page, "A PIN has 4 letters or numbers")
            page.fill("#pinInput", "")
            page.type("#pinInput", "zz zz")  # typed: upper-cased, space dropped
            assert page.input_value("#pinInput") == "ZZZZ"
            page.click("#pinStartBtn")
            _wait_pin_err(page, "Wrong PIN")
            assert "Tries left: 4." in _pin_err(page)
            assert _active(page) is None  # nothing started

            page.fill("#pinInput", pin.lower())
            page.click("#pinStartBtn")
            page.wait_for_selector("#runner:not(.hidden)")
            active = _active(page)
            attempt_id = active["attemptId"]
            assert active["token"] and active["rosterEntryId"] == entry["roster_entry_id"]
            assert page.is_hidden("#pinScreen") and page.is_hidden("#disclosure")

            # Reload: the stored token resumes straight into the exam (no PIN).
            page.reload()
            page.wait_for_selector("#runner:not(.hidden)")
            assert page.is_hidden("#pinScreen")
            assert _active(page)["attemptId"] == attempt_id

            # Token gone (e.g. storage partly cleared): the PIN screen comes back.
            page.evaluate(
                "() => { const a = JSON.parse(localStorage.getItem('exam:active'));"
                " a.token = null; localStorage.setItem('exam:active', JSON.stringify(a)); }"
            )
            page.reload()
            page.wait_for_selector("#pinScreen:not(.hidden)")
            assert page.text_content("#pinPrompt") == "Type your PIN again to continue your test."
            assert page.text_content("#pinStartBtn") == "Continue the test"
            page.fill("#pinInput", pin)
            page.click("#pinStartBtn")
            page.wait_for_selector("#runner:not(.hidden)")
            assert _active(page)["attemptId"] == attempt_id  # the same attempt resumed

            # A token the server does not know -> 401 -> PIN screen again.
            page.evaluate(
                "() => { const a = JSON.parse(localStorage.getItem('exam:active'));"
                " a.token = 'forged-token';"
                " localStorage.setItem('exam:active', JSON.stringify(a)); }"
            )
            page.reload()
            page.wait_for_selector("#pinScreen:not(.hidden)")
            assert _active(page)["token"] is None
            # "This is not me" -> back to the picker, resume state dropped.
            page.click("#pinBackBtn")
            page.wait_for_selector("#pick:not(.hidden)")
            assert _active(page) is None
            assert page_errors == []
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        _skip_without_chromium(exc)
        raise

    assert attempt_requests, "the runner made no attempt calls"
    assert all(tok for url, tok in attempt_requests if "forged" not in str(tok)), attempt_requests
    assert any(tok == "forged-token" for _u, tok in attempt_requests)  # the rejected try
    # Telemetry batches carry the token too (header on fetch, ?token= on a beacon).
    assert event_requests
    assert all(tok or "token=" in url for url, tok in event_requests), event_requests


def test_pin_lockout_message(live_server: tuple[str, str]) -> None:
    base_url, db_url = live_server
    entry = roster_entry(base_url, SEED_TEST_ID, "Bao")
    pin = roster_pin(db_url, entry["roster_entry_id"])
    wrong = "ZZZZ" if pin != "ZZZZ" else "YYYY"
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(f"{base_url}/web/exam.html?test={SEED_TEST_ID}")
            page.locator(".name-btn", has_text="Bao").click()
            page.wait_for_selector("#pinScreen:not(.hidden)")
            for left in (4, 3, 2, 1):
                page.fill("#pinInput", wrong)
                page.click("#pinStartBtn")
                _wait_pin_err(page, f"Tries left: {left}.")
            page.fill("#pinInput", wrong)
            page.click("#pinStartBtn")
            _wait_pin_err(page, "Too many wrong tries. Wait 5 min")
            # Locked: even the right PIN is refused for now.
            page.fill("#pinInput", pin)
            page.click("#pinStartBtn")
            page.wait_for_timeout(500)
            assert "Too many wrong tries" in _pin_err(page)
            assert page.is_hidden("#runner")
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        _skip_without_chromium(exc)
        raise


# --------------------------------------------------------------------------- #
# Movers: EN / УКР / РУС for the UI chrome only.
# --------------------------------------------------------------------------- #
def _content(page) -> dict:  # noqa: ANN001
    return {
        "part": page.text_content("#partLabel"),
        "prompts": page.eval_on_selector_all(".prompt", "els => els.map(e => e.textContent)"),
        "options": page.eval_on_selector_all(".opt", "els => els.map(e => e.textContent)"),
        "title": page.text_content("#examTitle"),
    }


def test_movers_language_switch_changes_chrome_not_content(
    live_server: tuple[str, str],
) -> None:
    base_url, db_url = live_server
    pin = roster_pin(db_url, f"{MOVERS_ID}-mila")
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.on("dialog", lambda d: d.dismiss())
            page.goto(f"{base_url}/web/exam.html?test={MOVERS_ID}")
            page.wait_for_selector(".name-btn")
            switch = page.locator("#langSwitch")
            switch.wait_for(state="visible")
            assert switch.inner_text().split() == ["EN", "УКР", "РУС"]
            assert page.text_content("#pick h2") == "Choose your name to begin"  # default EN

            page.click('#langSwitch button[data-lang="uk"]')
            assert page.text_content("#pick h2") == "Вибери своє ім'я, щоб почати"
            assert page.evaluate("() => document.documentElement.lang") == "uk"
            assert "Щоб тест був чесним" in page.text_content("#disclosure")
            uk_btn = '#langSwitch button[data-lang="uk"]'
            assert page.get_attribute(uk_btn, "aria-pressed") == "true"
            assert page.evaluate("() => localStorage.getItem('exam:lang')") == "uk"

            page.locator(".name-btn", has_text="Mila").click()
            page.wait_for_selector("#pinScreen:not(.hidden)")
            assert page.text_content("#pinHello") == "Привіт, Mila!"
            assert page.text_content("#pinStartBtn") == "Почати тест"
            page.fill("#pinInput", "ZZZZ" if pin != "ZZZZ" else "YYYY")
            page.click("#pinStartBtn")
            _wait_pin_err(page, "Неправильний PIN-код")
            # Switching on the PIN screen re-translates the open error too.
            page.click('#langSwitch button[data-lang="ru"]')
            assert page.text_content("#pinHello") == "Привет, Mila!"
            assert "Неверный PIN-код" in _pin_err(page)
            assert "Осталось попыток: 4." in _pin_err(page)
            assert page.evaluate("() => document.documentElement.lang") == "ru"

            page.fill("#pinInput", pin)
            page.click("#pinStartBtn")
            page.wait_for_selector("#runner:not(.hidden)")
            assert page.is_hidden("#langSwitch")  # only on the start screen
            # Chrome is Russian...
            assert page.text_content("#prevBtn") == "Назад"
            assert page.text_content("#nextBtn") == "Дальше"
            assert page.text_content("#skillLabel") == "Чтение"
            assert page.text_content("#progress") == f"Часть 1 из {len(SECTION_TITLES)}"
            assert page.text_content(".navtitle") == "Твои ответы"
            assert page.text_content("#saveStatus") == "Сохранено"
            assert page.get_attribute("#timer", "aria-label") == "Осталось времени"
            # ...the test content is the authored English, untouched.
            ru = _content(page)
            assert ru["part"] == SECTION_TITLES[0]
            assert ru["prompts"] == [i.prompt for i in SAMPLE.sections[0].items]
            assert ru["title"] == "Movers — Demo"
            assert all(o.strip() for o in ru["options"])
            authored_opts = {
                o.text for i in SAMPLE.sections[0].items for o in getattr(i, "options", [])
            }
            assert {o.strip() for o in ru["options"]} <= authored_opts

            page.click("#nextBtn")
            page.click("#nextBtn")
            assert page.text_content("#partLabel") == SECTION_TITLES[2]
            page.click('.navreview button')
            page.wait_for_selector("#review:not(.hidden)")
            assert page.text_content("#reviewTitle") == "Проверь свои ответы"
            assert page.text_content("#submitBtn") == "Сдать тест"
            assert "Ответы: 0 из" in page.text_content("#reviewCount")
            page.click("#reviewBackBtn")

            # Reload: the choice is remembered; content still English.
            page.reload()
            page.wait_for_selector("#runner:not(.hidden)")
            assert page.text_content("#prevBtn") == "Назад"
            assert page.evaluate("() => document.documentElement.lang") == "ru"
            assert _content(page)["prompts"][0] in PROMPTS

            # Same student, English UI: identical content.
            page.evaluate("() => localStorage.setItem('exam:lang', 'en')")
            page.reload()
            page.wait_for_selector("#runner:not(.hidden)")
            assert page.text_content("#prevBtn") == "Back"
            assert page.evaluate("() => document.documentElement.lang") == "en"
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        _skip_without_chromium(exc)
        raise


def test_every_ui_string_exists_in_all_three_languages() -> None:
    """The i18n table is complete: every key of `en` exists in `uk` and `ru`."""
    js = (REPO_ROOT / "apps" / "web" / "exam.js").read_text(encoding="utf-8")
    script = (
        "const src = require('fs').readFileSync(process.argv[1], 'utf8');"
        "const m = src.match(/const I18N = (\\{[\\s\\S]*?\\n\\});/);"
        "const I18N = eval('(' + m[1] + ')');"
        "const en = Object.keys(I18N.en).sort();"
        "for (const l of ['uk', 'ru']) {"
        "  const k = Object.keys(I18N[l]).sort();"
        "  if (JSON.stringify(k) !== JSON.stringify(en)) {"
        "    console.log(l, en.filter(x => !k.includes(x)), k.filter(x => !en.includes(x)));"
        "    process.exit(1);"
        "  }"
        "  for (const key of en) {"
        "    if (typeof I18N[l][key] !== typeof I18N.en[key]) {"
        "      console.log(l, key); process.exit(1);"
        "    }"
        "  }"
        "}"
        "console.log('ok', en.length);"
    )
    assert "const I18N = {" in js
    try:
        out = subprocess.run(
            ["node", "-e", script, str(REPO_ROOT / "apps" / "web" / "exam.js")],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:  # pragma: no cover - environment-dependent
        pytest.skip("node not installed")
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.startswith("ok")


def test_audio_warning_is_escaped_like_every_ui_string() -> None:
    """No raw HTML in `audioWarn` (any language): it marks bold as **...**, and the
    runner escapes the string first (`escBold`) and only then turns the marks into
    <b>. Evaluated with node on the real exam.js (no browser needed)."""
    js_path = REPO_ROOT / "apps" / "web" / "exam.js"
    js = js_path.read_text(encoding="utf-8")
    assert 'escBold(t("audioWarn"' in js
    script = r"""
const src = require('fs').readFileSync(process.argv[1], 'utf8');
const I18N = eval('(' + src.match(/const I18N = (\{[\s\S]*?\n\});/)[1] + ')');
const grab = (name) => src.match(new RegExp('function ' + name + '\\([\\s\\S]*?\\n\\}'))[0];
eval(grab('esc') + ';' + grab('escBold') + ';globalThis.escBold = escBold;');
for (const l of ['en', 'uk', 'ru']) {
  const html = escBold(I18N[l].audioWarn(
    { plays: 2, locked: true, resuming: false, hasPreview: true }));
  if (/<\/?[a-z]/i.test(I18N[l].audioWarn.toString())) {
    console.log('raw html in audioWarn', l); process.exit(1);
  }
  if ((html.match(/<b>/g) || []).length !== 2 || html.includes('**')) {
    console.log('bold', l, html); process.exit(1);
  }
}
if (escBold('<img src=x> **a**') !== '&lt;img src=x&gt; <b>a</b>') process.exit(2);
console.log('ok');
"""
    try:
        out = subprocess.run(
            ["node", "-e", script, str(js_path)], capture_output=True, text=True, timeout=30
        )
    except FileNotFoundError:  # pragma: no cover - environment-dependent
        pytest.skip("node not installed")
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.startswith("ok")
