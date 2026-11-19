"""Student runner E2E: word bank, letter + text matching buttons, gap mirrors.

Real browser (Playwright/Chromium) against the live API on a throwaway DB with a
purpose-built published test (reading block, questions 1..8):

* Part "wb" — 3 matching questions over 6 one-word options = a WORD BANK: tap a
  chip into the auto-picked box, the next empty box is picked, reuse is allowed
  ("→ 1, 3"), tapping the placed chip again clears it, ✕ clears, desktop drag-
  and-drop works, keyboard Enter / Escape flow, a polite announcement; the saved
  answer is still the option KEY (same PUT as a radio), telemetry still gets
  `answer_change`; a reload shows the boxes filled. On a phone (touch) chips are
  not draggable, taps work and the page never scrolls sideways.
* Part "gm" — gapped passage with "(4) ........." markers + sentence options:
  buttons show letter + text (a long text clipped, full text in the list above),
  "→ 4" on an option chosen elsewhere, the inline gap mirrors the chosen
  sentence, tapping it focuses the question; "↑ In the text" goes back.
* Part "oc" — open cloze: the inline gap mirrors typing before the debounced save.
* Part "sc" — gapped single choice: the gap shows the chosen option's text.

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
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import app.persistence.models  # noqa: F401  (register tables on Base.metadata)
from app.core.db import Base
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import (
    GapFillItem,
    GappedTextStimulus,
    MatchingItem,
    MatchingPoolStimulus,
    PassageTextStimulus,
    PoolOption,
    RosterEntry,
    Section,
    SingleChoiceItem,
    Test,
    TextOption,
)
from tests.pin_helpers import http_json, resume_script, start_with_pin
from tests.test_e2e_full import _free_port, _wait_for_health

sync_api = pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_ID = "aids-demo"
LONG = (
    "Afterwards, some people were really surprised by my sudden decision to leave, "
    "but I was not worried about it at all."
)
_STATUS_JS = "() => document.querySelector('#saveStatus').textContent"


def _build_test() -> Test:
    words = ["jam", "golf", "stamps", "salt", "sugar", "hockey"]
    sentences = [
        "I decided I needed a break.",
        "They always ask lots of questions.",
        LONG,
    ]
    return Test(
        id=TEST_ID,
        title="Answering aids — Demo",
        level="A2_KEY",
        status="published",
        duration_minutes=30,
        sections=[
            Section(
                id="wb",
                title="Word bank",
                skill="reading",
                stimulus=MatchingPoolStimulus(
                    text="Look and read. Choose the correct word.",
                    options=[
                        PoolOption(key=k, text=w) for k, w in zip("ABCDEF", words, strict=True)
                    ],
                ),
                items=[
                    MatchingItem(id=f"wb-q{i}", prompt=f"Sentence {i}.", correct="A")
                    for i in (1, 2, 3)
                ],
            ),
            Section(
                id="gm",
                title="Gapped sentences",
                skill="reading",
                stimulus=MatchingPoolStimulus(
                    text="A new life\n\nIntro text.\n\n(4) .........\n\nMiddle (5) ......... end.",
                    options=[
                        PoolOption(key=k, text=s) for k, s in zip("ABC", sentences, strict=True)
                    ],
                ),
                items=[
                    MatchingItem(id="gm-q4", prompt="Gap 4", correct="A"),
                    MatchingItem(id="gm-q5", prompt="Gap 5", correct="B"),
                ],
            ),
            Section(
                id="oc",
                title="Open cloze",
                skill="reading",
                stimulus=GappedTextStimulus(
                    text="I hope (0) you are well. We went (6) ........... the beach "
                    "and (7) ........... swam."
                ),
                items=[
                    GapFillItem(id="oc-q6", prompt="Write one word for gap 6.", accepted=["to"]),
                    GapFillItem(id="oc-q7", prompt="Write one word for gap 7.", accepted=["we"]),
                ],
            ),
            Section(
                id="sc",
                title="Gapped choice",
                skill="reading",
                stimulus=GappedTextStimulus(text="It was the (8) ........... of chemistry."),
                items=[
                    SingleChoiceItem(
                        id="sc-q8",
                        prompt="Choose the word for gap 8.",
                        options=[
                            TextOption(key="A", text="subject"),
                            TextOption(key="B", text="topic"),
                        ],
                        correct="A",
                    )
                ],
            ),
            Section(
                id="end",
                title="Notice",
                skill="reading",
                stimulus=PassageTextStimulus(text="NOTICE (1) Closed (2) on Monday."),
                items=[
                    SingleChoiceItem(
                        id="end-q9",
                        prompt="When?",
                        options=[
                            TextOption(key="A", text="Monday"),
                            TextOption(key="B", text="Friday"),
                        ],
                        correct="A",
                    )
                ],
            ),
        ],
    )


@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[tuple[str, str]]:
    db_url = f"sqlite:///{(tmp_path / 'aids.db').as_posix()}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        ContentRepository(s).add_test(_build_test())
        repo = AttemptRepository(s)
        for name in ("Ann", "Bob", "Cat"):
            repo.add_roster_entry(
                RosterEntry(id=f"{TEST_ID}-{name.lower()}", test_id=TEST_ID, display_name=name)
            )
        s.commit()
    engine.dispose()

    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": db_url,
        "ASSETS_DIR": str(tmp_path / "assets"),
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


def _open(p, base_url: str, started: dict, name: str, *, mobile: bool = False):  # noqa: ANN001, ANN202
    browser = p.chromium.launch(headless=True)
    if mobile:
        context = browser.new_context(
            viewport={"width": 360, "height": 740}, is_mobile=True, has_touch=True
        )
    else:
        context = browser.new_context(viewport={"width": 1280, "height": 900})
    context.add_init_script(
        resume_script(
            TEST_ID, started["attempt_id"], started["roster_entry_id"], started["token"], name
        )
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base_url}/web/exam.html?test={TEST_ID}")
    page.wait_for_selector("#runner:not(.hidden)")
    return browser, page, errors


def _saved(base_url: str, started: dict) -> dict:
    return http_json(
        f"{base_url}/exam/attempts/{started['attempt_id']}/answers", token=started["token"]
    )


def _wait_saved(page) -> None:  # noqa: ANN001
    page.wait_for_function(f"() => ({_STATUS_JS})() === 'Saved'", timeout=8000)


def _go(page, item_id: str) -> None:  # noqa: ANN001
    page.click(f".navsq[data-item='{item_id}']")
    page.wait_for_selector(f"#q-{item_id}")


def _answer_changes(db_url: str, attempt_id: str) -> list[tuple[str, object]]:
    """(item_id, value) of every stored `answer_change` (the real ingest store)."""
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT item_id, payload FROM integrity_events "
                    "WHERE attempt_id = :a AND type = 'answer_change' ORDER BY id"
                ),
                {"a": attempt_id},
            ).all()
    finally:
        engine.dispose()
    out = []
    for item_id, payload in rows:
        data = json.loads(payload) if isinstance(payload, str) else payload
        out.append((item_id, data.get("value")))
    return out


def _chip(page, key: str):  # noqa: ANN001, ANN202
    return page.locator(f".wb-chip[data-key='{key}']")


def test_word_bank_tap_reuse_clear_drag_keyboard_and_resume(
    live_server: tuple[str, str],
) -> None:
    base_url, db_url = live_server
    started = start_with_pin(base_url, db_url, TEST_ID, "Ann")

    try:
        with sync_api.sync_playwright() as p:
            browser, page, errors = _open(p, base_url, started, "Ann")
            # The first part is the word bank: no A-F list, a bank of chips instead.
            assert page.locator(".pool-ref").count() == 0
            assert page.locator(".wb-chip").count() == 6
            assert page.get_attribute(".wb-chip[data-key='A']", "draggable") == "true"
            assert page.get_attribute("#slot-wb-q1", "aria-pressed") == "true"  # auto-picked
            assert "Question 1: now tap a word." in page.text_content(".wb-target")

            # Tap a chip: it goes into box 1, box 2 becomes the target.
            _chip(page, "A").click()
            assert page.text_content("#slot-wb-q1") == "jam"
            assert page.get_attribute("#slot-wb-q2", "aria-pressed") == "true"
            assert "→ 1" in page.text_content(".wb-chip[data-key='A']")
            page.wait_for_function(
                "() => document.querySelector('#answerLive').textContent"
                ".includes('“jam” is now in question 1.')"
            )
            _wait_saved(page)
            assert _saved(base_url, started) == {"wb-q1": "A"}  # still the option KEY
            assert "done" in (page.get_attribute(".navsq[data-item='wb-q1']", "class") or "")

            # Reuse: pick box 3 by tapping its card, place the same word again.
            page.click("#q-wb-q3 .prompt")
            assert page.get_attribute("#slot-wb-q3", "aria-pressed") == "true"
            _chip(page, "A").click()
            assert page.text_content("#slot-wb-q3") == "jam"
            assert "→ 1, 3" in page.text_content(".wb-chip[data-key='A']")
            # Tapping the word already in the chosen box takes it out again.
            page.click("#slot-wb-q3")
            assert page.get_attribute(".wb-chip[data-key='A']", "aria-pressed") == "true"
            _chip(page, "A").click()
            assert "tap here" in page.text_content("#slot-wb-q3")
            assert "done" not in (page.get_attribute(".navsq[data-item='wb-q3']", "class") or "")

            # Desktop drag-and-drop onto question 2.
            page.drag_and_drop(".wb-chip[data-key='C']", "#q-wb-q2")
            assert page.text_content("#slot-wb-q2") == "stamps"
            _wait_saved(page)
            assert _saved(base_url, started) == {"wb-q1": "A", "wb-q2": "C", "wb-q3": ""}

            # Keyboard: Enter on a box moves into the bank, Enter places, Escape returns.
            page.focus("#slot-wb-q3")
            page.keyboard.press("Enter")
            assert page.evaluate("() => document.activeElement.classList.contains('wb-chip')")
            page.keyboard.press("Tab")  # next chip (B = golf)
            page.keyboard.press("Enter")
            assert page.text_content("#slot-wb-q3") == "golf"
            assert page.evaluate("() => document.activeElement.classList.contains('wb-chip')")
            page.keyboard.press("Escape")  # back to the box just filled
            assert page.evaluate("() => document.activeElement.id") == "slot-wb-q3"

            # ✕ clears a box.
            page.click("#q-wb-q2 .wb-clear")
            assert "tap here" in page.text_content("#slot-wb-q2")
            _wait_saved(page)
            assert _saved(base_url, started) == {"wb-q1": "A", "wb-q2": "", "wb-q3": "B"}

            # Telemetry still sees every placement as an answer_change.
            page.evaluate("() => rec.flush(true)")
            page.wait_for_function("() => !rec.queue.length && !rec._inflight.length", timeout=8000)
            changes = _answer_changes(db_url, started["attempt_id"])
            assert ("wb-q1", "A") in changes and ("wb-q2", "C") in changes
            assert ("wb-q3", "") in changes and ("wb-q3", "B") in changes

            # A reload resumes with the boxes filled.
            page.reload()
            page.wait_for_selector("#slot-wb-q1")
            assert page.text_content("#slot-wb-q1") == "jam"
            assert page.text_content("#slot-wb-q3") == "golf"
            assert page.get_attribute("#slot-wb-q2", "aria-pressed") == "true"  # the empty one
            assert errors == []
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        _skip_without_chromium(exc)
        raise


def test_word_bank_on_a_phone_taps_and_no_sideways_scroll(live_server: tuple[str, str]) -> None:
    base_url, db_url = live_server
    started = start_with_pin(base_url, db_url, TEST_ID, "Bob")
    try:
        with sync_api.sync_playwright() as p:
            browser, page, errors = _open(p, base_url, started, "Bob", mobile=True)
            assert page.get_attribute(".wb-chip[data-key='A']", "draggable") is None
            _chip(page, "D").tap()
            _chip(page, "D").tap()  # box 2 is the target now: reuse
            assert page.text_content("#slot-wb-q1") == "salt"
            assert page.text_content("#slot-wb-q2") == "salt"
            _wait_saved(page)
            assert page.evaluate("() => document.documentElement.scrollWidth") <= 360
            # Every chip / box is a big enough target for a child's finger.
            heights = page.eval_on_selector_all(
                ".wb-chip, .wb-slot, .wb-clear:not(.hidden)",
                "els => els.map(e => e.getBoundingClientRect().height)",
            )
            assert min(heights) >= 44
            assert errors == []
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        _skip_without_chromium(exc)
        raise


def test_gap_mirrors_letter_text_buttons_and_jumps(live_server: tuple[str, str]) -> None:
    base_url, db_url = live_server
    started = start_with_pin(base_url, db_url, TEST_ID, "Cat")
    try:
        with sync_api.sync_playwright() as p:
            browser, page, errors = _open(p, base_url, started, "Cat")

            # Gapped matching: letter + text buttons, the full list stays above.
            _go(page, "gm-q4")
            assert page.locator(".pool-ref").count() == 1
            assert page.locator(".wb-bank").count() == 0
            texts = page.eval_on_selector_all(
                "#q-gm-q4 .opt-letter .ol-text", "els => els.map(e => e.textContent)"
            )
            assert texts[:2] == [
                "I decided I needed a break.",
                "They always ask lots of questions.",
            ]
            assert texts[2].endswith("…") and LONG.startswith(texts[2][:-1].rstrip())
            assert LONG in page.text_content(".pool-ref")
            gaps = page.eval_on_selector_all(".gap-slot", "els => els.map(e => e.dataset.gapNo)")
            assert gaps == ["4", "5"]
            assert "(4)" not in page.text_content(".stimulus.gapped")
            assert "A new life" in page.text_content(".stimulus.gapped")

            page.locator("#q-gm-q4 label.opt-letter").filter(
                has=page.locator("input[value='A']")
            ).click()
            slot4 = ".gap-slot[data-gap-item='gm-q4']"
            # A sentence option shows as letter + (clipped) text inside the passage.
            assert page.text_content(f"{slot4} .gap-val") == "A — I decided I needed a break."
            assert "filled" in (page.get_attribute(slot4, "class") or "")
            # Used elsewhere: "→ 4" on question 5's option A (still selectable).
            used = page.text_content("#q-gm-q5 label.opt-letter[data-key='A'] .ol-used")
            assert "→ 4" in used and "used in question 4" in used
            # Tapping a gap focuses its question's control.
            page.click(".gap-slot[data-gap-item='gm-q5']")
            assert page.evaluate("() => document.activeElement.closest('.qcard').id") == "q-gm-q5"
            # ...and "↑ In the text" goes back to the gap.
            page.click("#q-gm-q5 .to-gap")
            assert page.evaluate("() => document.activeElement.dataset.gapItem") == "gm-q5"
            _wait_saved(page)

            # Open cloze: the gap mirrors typing before the debounced save.
            _go(page, "oc-q6")
            assert page.locator(".gap-slot").count() == 2  # "(0)" stays text
            assert "(0)" in page.text_content(".stimulus.gapped")
            page.fill("#ctl-oc-q6", "to")
            assert page.text_content(".gap-slot[data-gap-item='oc-q6'] .gap-val") == "to"
            page.click(".gap-slot[data-gap-item='oc-q7']")
            assert page.evaluate("() => document.activeElement.id") == "ctl-oc-q7"
            _wait_saved(page)

            # Gapped single choice: the gap shows the chosen option's text.
            _go(page, "sc-q8")
            choice = page.locator("#q-sc-q8 label.opt").nth(1)
            choice.click()  # (options may be shuffled: compare with what was clicked)
            shown = page.text_content(".gap-slot[data-gap-item='sc-q8'] .gap-val")
            assert shown == (choice.text_content() or "").strip()
            assert shown in ("subject", "topic")
            _wait_saved(page)

            # A plain passage is untouched (no gaps there by design).
            _go(page, "end-q9")
            assert page.locator(".gap-slot").count() == 0
            assert page.text_content(".stimulus") == "NOTICE (1) Closed (2) on Monday."

            # Reload: the mirrors come back from the saved answers.
            page.reload()
            page.wait_for_selector("#runner:not(.hidden)")
            _go(page, "oc-q6")
            assert page.text_content(".gap-slot[data-gap-item='oc-q6'] .gap-val") == "to"
            assert errors == []
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        _skip_without_chromium(exc)
        raise

    saved = _saved(base_url, started)
    assert saved == {"gm-q4": "A", "oc-q6": "to", "sc-q8": 1}
