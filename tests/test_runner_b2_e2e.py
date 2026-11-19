"""Student runner E2E for the B2 / C1 / C2 item shapes (Playwright, live API).

A small synthetic B2 First test on a throwaway DB:

* key word transformation — lead-in sentence, the KEY WORD as a bold capital chip,
  the second sentence with ONE inline text box, the rubric "Use between 2 and 5
  words, including the word given." and an informational word count (never
  blocking); saved as a plain string, navigator answered, a reload restores it, a
  blocked paste is recorded as `paste_attempt` (field `gap_fill`, length only);
* word formation (gap_fill `stem_word` + `max_words` = 1) in a `passage_text`
  part — "(COMPETE)" beside the box AND in the inline passage slot, "One word
  only.", the slot mirrors typing;
* a gapped text with `each_option_once` — the second use of an option is allowed
  but greyed + a gentle "also your answer to question N" note on both questions;
* C1/C2 Listening Part 4 — two matching pools with their own `audio` (same file =
  ONE track) and a part with a different recording (= the next track).

Skips cleanly without Chromium.
"""

from __future__ import annotations

import json
import math
import os
import struct
import subprocess
import sys
import wave
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import app.persistence.models  # noqa: F401  (register tables on Base.metadata)
from app.content.storage import FilesystemStorage
from app.core.db import Base
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import (
    AudioAssetStimulus,
    GapFillItem,
    KeyWordTransformationItem,
    KwtAnswer,
    MatchingItem,
    MatchingPoolStimulus,
    PassageTextStimulus,
    PoolOption,
    RosterEntry,
    Section,
    Test,
)
from tests.pin_helpers import http_json, resume_script, start_with_pin
from tests.test_e2e_full import _free_port, _wait_for_health

sync_api = pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_ID = "b2-aids"
TRACK_A, TRACK_B = "b2-l4.wav", "b2-l5.wav"
_STATUS_JS = "() => document.querySelector('#saveStatus').textContent"


def _wav(seconds: float, freq: float) -> bytes:
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


def build_b2_test(test_id: str = TEST_ID) -> Test:
    sentences = [
        "That was the moment I decided to change my plans completely.",
        "Nobody in the village had ever seen anything like it before.",
        "It turned out to be the best decision of my life.",
    ]
    pool_audio = AudioAssetStimulus(asset_id=TRACK_A, plays=2)
    return Test(
        id=test_id,
        title="B2 First — answering demo",
        level="B2_FIRST",
        status="published",
        duration_minutes=60,
        sections=[
            Section(
                id=f"{test_id}-p3",
                title="Reading and Use of English Part 3",
                skill="reading",
                stimulus=PassageTextStimulus(
                    text="For questions 1, use the word given in capitals.\n\n"
                    "The (1) ......... was won by a team from a small school."
                ),
                items=[
                    GapFillItem(
                        id=f"{test_id}-q1",
                        prompt="(1)",
                        accepted=["competition"],
                        stem_word="COMPETE",
                        max_words=1,
                    )
                ],
            ),
            Section(
                id=f"{test_id}-p4",
                title="Reading and Use of English Part 4",
                skill="reading",
                stimulus=PassageTextStimulus(text=""),
                items=[
                    KeyWordTransformationItem(
                        id=f"{test_id}-q2",
                        prompt="I have never been to Paris before.",
                        key_word="first",
                        gapped_sentence="It is the ____ been to Paris.",
                        answers=[KwtAnswer(part_a=["first time"], part_b=["I have"])],
                    )
                ],
            ),
            Section(
                id=f"{test_id}-p6",
                title="Reading and Use of English Part 6",
                skill="reading",
                stimulus=MatchingPoolStimulus(
                    text="A new life\n\nI left the city. (3) .........\n\n"
                    "It was hard. (4) .........",
                    options=[
                        PoolOption(key=k, text=s) for k, s in zip("ABC", sentences, strict=True)
                    ],
                    each_option_once=True,
                ),
                items=[
                    MatchingItem(id=f"{test_id}-q3", prompt="(3)", correct="A", points=2),
                    MatchingItem(id=f"{test_id}-q4", prompt="(4)", correct="C", points=2),
                ],
            ),
            Section(
                id=f"{test_id}-l4a",
                title="Listening Part 4 Task 1",
                skill="listening",
                stimulus=MatchingPoolStimulus(
                    options=[PoolOption(key=k, text=f"reason {k}") for k in "ABC"],
                    audio=pool_audio,
                ),
                items=[MatchingItem(id=f"{test_id}-q5", prompt="Speaker 1", correct="A")],
            ),
            Section(
                id=f"{test_id}-l4b",
                title="Listening Part 4 Task 2",
                skill="listening",
                stimulus=MatchingPoolStimulus(
                    options=[PoolOption(key=k, text=f"feeling {k}") for k in "ABC"],
                    audio=pool_audio,
                ),
                items=[MatchingItem(id=f"{test_id}-q6", prompt="Speaker 1", correct="C")],
            ),
            Section(
                id=f"{test_id}-l5",
                title="Listening Part 5",
                skill="listening",
                stimulus=AudioAssetStimulus(asset_id=TRACK_B, plays=2),
                items=[GapFillItem(id=f"{test_id}-q7", prompt="Name:", accepted=["Tom"])],
            ),
        ],
    )


def seed_b2(db_url: str, assets_dir: Path, names: tuple[str, ...] = ("Dee", "Eva")) -> None:
    storage = FilesystemStorage(assets_dir)
    storage.put(TRACK_A, _wav(1.0, 440))
    storage.put(TRACK_B, _wav(1.0, 660))
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        ContentRepository(s).add_test(build_b2_test())
        repo = AttemptRepository(s)
        for name in names:
            repo.add_roster_entry(
                RosterEntry(id=f"{TEST_ID}-{name.lower()}", test_id=TEST_ID, display_name=name)
            )
        s.commit()
    engine.dispose()


@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[tuple[str, str]]:
    db_url = f"sqlite:///{(tmp_path / 'b2.db').as_posix()}"
    assets = tmp_path / "assets"
    seed_b2(db_url, assets)
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
        yield f"http://127.0.0.1:{port}", db_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


def _events(db_url: str, attempt_id: str, kind: str) -> list[tuple[str, dict]]:
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT item_id, payload FROM integrity_events "
                    "WHERE attempt_id = :a AND type = :t ORDER BY id"
                ),
                {"a": attempt_id, "t": kind},
            ).all()
    finally:
        engine.dispose()
    return [(i, json.loads(p) if isinstance(p, str) else p) for i, p in rows]


def _wait_saved(page) -> None:  # noqa: ANN001
    page.wait_for_function(f"() => ({_STATUS_JS})() === 'Saved'", timeout=8000)


def _go(page, item_id: str) -> None:  # noqa: ANN001
    page.click(f".navsq[data-item='{item_id}']")
    page.wait_for_selector(f"#q-{item_id}")


def test_b2_kwt_word_formation_once_pool_and_pool_audio(live_server: tuple[str, str]) -> None:
    base_url, db_url = live_server
    started = start_with_pin(base_url, db_url, TEST_ID, "Dee")
    aid, tok = started["attempt_id"], started["token"]
    q = {n: f"{TEST_ID}-q{n}" for n in range(1, 8)}
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            context.add_init_script(
                resume_script(TEST_ID, aid, started["roster_entry_id"], tok, "Dee")
            )
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"{base_url}/web/exam.html?test={TEST_ID}")
            page.wait_for_selector("#runner:not(.hidden)")
            assert page.is_hidden("#langSwitch")  # B2: English UI, no switcher

            # --- Word formation (passage_text + stem_word + max_words = 1).
            card = f"#q-{q[1]}"
            assert page.text_content(f"{card} .stem-chip") == "(COMPETE)"
            assert page.text_content(f"{card} .word-hint") == "One word only."
            described = page.get_attribute(f"#ctl-{q[1]}", "aria-describedby") or ""
            assert f"st-{q[1]}" in described and f"wl-{q[1]}" in described
            slot = f".gap-slot[data-gap-item='{q[1]}']"
            assert page.text_content(f"{slot} .stem-chip") == "(COMPETE)"
            assert "(1)" not in page.text_content(".stimulus.gapped")
            page.fill(f"#ctl-{q[1]}", "competition")
            assert page.text_content(f"{slot} .gap-val") == "competition"
            _wait_saved(page)

            # --- Key word transformation.
            _go(page, q[2])
            card = f"#q-{q[2]}"
            assert page.text_content(f"{card} .prompt") == "I have never been to Paris before."
            assert page.text_content(f"{card} .kw-chip") == "FIRST"
            sentence = page.text_content(f"{card} .kwt-sentence") or ""
            assert sentence.startswith("It is the ") and sentence.endswith(" been to Paris.")
            assert page.locator(f"{card} .kwt-sentence input#ctl-{q[2]}").count() == 1
            assert page.text_content(f"{card} .kwt-rubric") == (
                "Use between 2 and 5 words, including the word given."
            )
            box = f"#ctl-{q[2]}"
            page.fill(box, "first time I have")
            wc = f"#kwc-{q[2]}"
            assert page.text_content(wc) == "4 words"
            assert "ok" in (page.get_attribute(wc, "class") or "")
            page.fill(box, "very first time that I have ever")  # 7 words: shown, not blocked
            assert page.text_content(wc) == "7 words"
            assert "over" in (page.get_attribute(wc, "class") or "")
            assert page.input_value(box) == "very first time that I have ever"
            page.fill(box, "first time I have")
            page.evaluate("(sel) => document.querySelector(sel).blur()", box)  # flush the save
            page.wait_for_function(
                f"() => document.querySelector(\".navsq[data-item='{q[2]}']\")"
                ".classList.contains('done')"
            )
            _wait_saved(page)
            assert "done" in (page.get_attribute(f".navsq[data-item='{q[2]}']", "class") or "")
            # A paste is blocked and recorded by length only, as for gap_fill.
            page.evaluate(
                """(sel) => { const el = document.querySelector(sel); el.focus();
                   const dt = new DataTransfer(); dt.setData('text/plain', 'pasted words');
                   el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt,
                     bubbles: true, cancelable: true})); }""",
                box,
            )
            assert page.input_value(box) == "first time I have"

            # --- each_option_once gapped text.
            _go(page, q[3])
            page.locator(f"#q-{q[3]} label.opt-letter[data-key='A']").click()
            assert page.is_hidden(f"#q-{q[3]} .once-warn")
            # The passage slot shows letter + a short clip, never the whole sentence.
            slot3 = f".gap-slot[data-gap-item='{q[3]}']"
            assert page.text_content(f"{slot3} .gap-val") == "A — That was the moment I…"
            assert "completely" not in (page.get_attribute(slot3, "aria-label") or "")
            assert "once" in (
                page.get_attribute(f"#q-{q[4]} label.opt-letter[data-key='A']", "class") or ""
            )
            page.locator(f"#q-{q[4]} label.opt-letter[data-key='A']").click()  # allowed
            assert page.is_checked(f"#q-{q[4]} input[value='A']")
            warn4 = page.text_content(f"#q-{q[4]} .once-warn") or ""
            assert "A is also your answer to question 3" in warn4
            assert "question 4" in (page.text_content(f"#q-{q[3]} .once-warn") or "")
            page.locator(f"#q-{q[4]} label.opt-letter[data-key='C']").click()
            assert page.is_hidden(f"#q-{q[4]} .once-warn")
            assert page.is_hidden(f"#q-{q[3]} .once-warn")
            _wait_saved(page)

            # --- Listening Part 4: two pools with ONE recording, then the next track.
            model = page.evaluate(
                "() => ({ tracks: tracks.map(t => t.asset_id),"
                " units: units.map(u => [u.section.id, u.trackIdx]) })"
            )
            assert model["tracks"] == [TRACK_A, TRACK_B]
            by_unit = dict(model["units"])
            assert by_unit[f"{TEST_ID}-l4a"] == 0 and by_unit[f"{TEST_ID}-l4b"] == 0
            assert by_unit[f"{TEST_ID}-l5"] == 1
            _go(page, q[5])
            assert page.is_visible("#audioBar")
            assert page.locator("#audioPool audio, #audioBar").count() >= 1

            # Reload: the typed KWT / word-formation answers come back.
            page.reload()
            page.wait_for_selector("#runner:not(.hidden)")
            _go(page, q[2])
            assert page.input_value(f"#ctl-{q[2]}") == "first time I have"
            assert page.text_content(f"#kwc-{q[2]}") == "4 words"
            _go(page, q[1])
            assert page.text_content(f".gap-slot[data-gap-item='{q[1]}'] .gap-val") == (
                "competition"
            )
            page.evaluate("() => rec.flush(true)")
            page.wait_for_function("() => !rec.queue.length && !rec._inflight.length", timeout=8000)
            assert errors == []
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed: {exc}")
        raise

    saved = http_json(f"{base_url}/exam/attempts/{aid}/answers", token=tok)
    assert saved == {q[1]: "competition", q[2]: "first time I have", q[3]: "A", q[4]: "C"}
    pastes = _events(db_url, aid, "paste_attempt")
    assert (q[2], {"kind": "paste", "length": 12, "field": "gap_fill"}) in [
        (i, {k: p.get(k) for k in ("kind", "length", "field")}) for i, p in pastes
    ]
    assert all("pasted" not in json.dumps(p) for _i, p in pastes)
