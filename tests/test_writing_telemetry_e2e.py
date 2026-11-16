"""Playwright E2E: the real exam runner captures writing telemetry — never content.

Drives `apps/web/exam.html` + `exam.js` + `recorder.js` in headless Chromium against
a live uvicorn seeded with the demo test: focuses the open_writing textarea, types,
attempts a (blocked) paste of a 300-char string, blurs, then submits. Asserts:

* a `paste_attempt` event lands with the LENGTH (300) and no text, the paste was
  blocked (textarea unchanged), and the pasted string appears in NO stored payload;
* `field_focus`, `writing_stats` (keystrokes + insertText chars) and `field_blur`
  land in `integrity_events`;
* the submit-time finalize stores a report whose integrity profile has the writing
  item flagged `large_paste_attempt` and a deterministic verdict (no key in the
  server env -> `model_id` None);
* the teacher dashboard then READS that stored verdict: the results row carries the
  suspicion chip, and the attempt detail shows the Russian "Анализ честности"
  section — source "формула без LLM", the writing card with the translated
  large-paste signal and the key writing features — right above the timeline (with
  the "Попытка вставить текст (300 символов) … заблокирована" sentence) and the raw
  events (golden rule #6). A second pass feeds the same page a Gemini-shaped verdict
  (intercepted API responses) to render the "✍ AI?" marker, the high-likelihood card
  and an unknown model flag; the printable student report shows none of it.

Skips cleanly when Playwright / Chromium is not installed.
"""

from __future__ import annotations

import json
import os
import re
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
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.persistence.models  # noqa: F401  (register tables on Base.metadata)
from app.content.seed import SEED_TEST_ID, seed
from app.content.storage import FilesystemStorage
from app.core.db import Base

sync_api = pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "e2e-teacher-pw"
WRITE_ID = "seed-q-write"
TYPED = "Hello Tom, come to my party"
PASTED = "Q" * 300


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


@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
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
        **os.environ,
        "DATABASE_URL": db_url,
        "ASSETS_DIR": str(assets_dir),
        "LOG_LEVEL": "INFO",
        "TEACHER_PASSWORD": PASSWORD,
        "ENVIRONMENT": "dev",
        "SKIP_STARTUP_SWEEP": "1",  # no recovery loop: only the submit-time finalize
        "GEMINI_API_KEY": "",  # never call a paid API from a test: deterministic verdict
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


def _start(base_url: str, name: str) -> tuple[str, str]:
    with urllib.request.urlopen(f"{base_url}/exam/tests/{SEED_TEST_ID}/roster", timeout=5) as r:
        roster = json.load(r)
    entry = next(e for e in roster if e["display_name"] == name)
    req = urllib.request.Request(
        f"{base_url}/exam/roster/{entry['roster_entry_id']}/start", data=b"", method="POST"
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return str(json.load(r)["attempt_id"]), str(entry["roster_entry_id"])


def _events(db: Path, attempt_id: str) -> list[tuple[str, str | None, dict]]:
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT type, item_id, payload FROM integrity_events WHERE attempt_id = ? ORDER BY id",
            (attempt_id,),
        ).fetchall()
    finally:
        conn.close()
    return [(t, i, json.loads(p) if p else {}) for t, i, p in rows]


def _report(db: Path, attempt_id: str) -> tuple[str, dict | None, dict | None] | None:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT status, integrity, verdict FROM attempt_reports WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return row[0], json.loads(row[1]) if row[1] else None, json.loads(row[2]) if row[2] else None


def _poll(fn, timeout: float = 10.0):  # noqa: ANN001, ANN202
    deadline = time.monotonic() + timeout
    value = fn()
    while not value and time.monotonic() < deadline:
        time.sleep(0.2)
        value = fn()
    return value


def test_writing_field_telemetry_and_blocked_paste(live_server: tuple[str, Path]) -> None:
    base_url, db_path = live_server
    attempt_id, entry_id = _start(base_url, "Anna")
    active = json.dumps(
        {"testId": SEED_TEST_ID, "attemptId": attempt_id, "rosterEntryId": entry_id, "name": "Anna"}
    )
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            context.add_init_script(f"localStorage.setItem('exam:active', {json.dumps(active)});")
            page = context.new_page()
            page.on("dialog", lambda d: d.accept())  # "unanswered questions" confirm
            page.goto(f"{base_url}/web/exam.html?test={SEED_TEST_ID}")
            page.wait_for_selector(f".navsq[data-item='{WRITE_ID}']")
            page.click(f".navsq[data-item='{WRITE_ID}']")
            ta = f"#ctl-{WRITE_ID}"
            page.wait_for_selector(ta)

            page.click(ta)  # field_focus
            page.keyboard.type(TYPED, delay=25)  # trusted keydown + insertText

            # Attempt a paste of a long string: blocked, only its length recorded.
            blocked = page.evaluate(
                """([sel, text]) => {
                    const ta = document.querySelector(sel);
                    const dt = new DataTransfer();
                    dt.setData('text/plain', text);
                    const e = new ClipboardEvent('paste',
                        {clipboardData: dt, bubbles: true, cancelable: true});
                    ta.dispatchEvent(e);
                    return [e.defaultPrevented, ta.value];
                }""",
                [ta, PASTED],
            )
            assert blocked == [True, TYPED]

            page.click("#examTitle")  # blur -> writing_stats + field_blur
            page.wait_for_timeout(800)

            types = _poll(
                lambda: (
                    {t for t, _i, _p in _events(db_path, attempt_id)}
                    >= {"field_focus", "paste_attempt", "writing_stats", "field_blur"}
                    and {t for t, _i, _p in _events(db_path, attempt_id)}
                )
            )
            assert types, [t for t, _i, _p in _events(db_path, attempt_id)]

            page.click("#submitBtn")
            page.wait_for_selector("#done:not(.hidden)", timeout=15000)
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed: {exc}")
        raise

    events = _events(db_path, attempt_id)
    pastes = [(i, p) for t, i, p in events if t == "paste_attempt"]
    assert pastes == [(WRITE_ID, {"kind": "paste", "length": 300, "field": "open_writing"})]
    # The pasted text is stored NOWHERE (only its length).
    assert all(PASTED[:20] not in json.dumps(p) for _t, _i, p in events)

    focus = [p for t, i, p in events if t == "field_focus" and i == WRITE_ID]
    assert focus and focus[0]["field"] == "open_writing"
    stats = [p for t, i, p in events if t == "writing_stats" and i == WRITE_ID]
    assert stats
    assert sum(s["keystrokes"] for s in stats) >= len(TYPED)
    assert sum(s["added"].get("insertText", 0) for s in stats) == len(TYPED)
    assert sum(s["ik_count"] for s in stats) >= len(TYPED) - 1
    assert any(t == "field_blur" for t, _i, _p in events)

    # Submit-time finalize stored the integrity profile + deterministic verdict.
    report = _poll(lambda: _report(db_path, attempt_id), timeout=20.0)
    assert report is not None
    status, integrity, verdict = report
    assert status in ("complete", "failed")
    assert integrity is not None and verdict is not None
    writing = {w["item_id"]: w for w in integrity["writing"]}
    assert WRITE_ID in writing
    assert writing[WRITE_ID]["typed_chars"] == len(TYPED)
    assert writing[WRITE_ID]["max_paste_attempt_length"] == 300
    assert "large_paste_attempt" in writing[WRITE_ID]["flags"]
    assert verdict["model_id"] is None
    assert "large_paste_attempt" in verdict["flags"]

    # The teacher dashboard reads that stored verdict back (no LLM on a read).
    try:
        _dashboard_shows_the_stored_verdict(base_url, attempt_id, verdict)
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed: {exc}")
        raise


_EVIDENCE_ORDER_JS = """() => {
    const body = document.querySelector('#detailBody');
    const verdict = body.querySelector('section.integrity');
    const timeline = body.querySelector('details[data-key="timeline"]');
    const raw = body.querySelector('details[data-key="raw"]');
    if (!verdict || !timeline || !raw) return false;
    const after = (a, b) =>
        Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    return after(verdict, timeline) && after(timeline, raw)
        && raw.textContent.includes('"paste_attempt"');
}"""


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _dashboard_shows_the_stored_verdict(base_url: str, attempt_id: str, verdict: dict) -> None:
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        page.goto(f"{base_url}/web/teacher.html")
        page.fill("#password", PASSWORD)
        page.click("#loginBtn")
        test_row = page.locator(f'#testsTable tr[data-test-id="{SEED_TEST_ID}"]')
        test_row.wait_for()
        test_row.locator('[data-act="open"]').click()

        # Results row: the STORED suspicion as a chip; a 300-char blocked paste alone
        # is far below the "✍ AI?" threshold.
        row = page.locator(f'#resultsTable tbody tr[data-attempt="{attempt_id}"]')
        row.wait_for()
        chip = row.locator(".susp-chip")
        assert chip.inner_text().strip() == f"{verdict['suspicion_score']:.2f}"
        assert "формула без LLM" in (chip.get_attribute("title") or "")
        assert row.locator(".ai-mark").count() == 0

        row.locator('[data-act="detail"]').click()
        section = page.locator("#detailBody section.integrity")
        section.wait_for()
        # (text_content: the dashboard's h2 is upper-cased by CSS only.)
        assert section.locator("h2").first.text_content() == "Анализ честности (рекомендательно)"
        disclaimer = section.locator(".disclaimer").inner_text()
        assert "никогда не меняет оценку" in disclaimer and "хронологию" in disclaimer
        text = _flat(section.inner_text())
        assert "Источник: формула без LLM" in text
        assert "Подозрение:" in text and "Уверенность анализа:" in text
        assert _flat(verdict["summary"]) in text  # the stored Russian summary, verbatim
        assert "предварительно" not in text  # stored at finalize, not a live fallback

        # The writing card: translated large-paste signal + the key writing features.
        card = section.locator(f'.integrity-writing[data-item="{WRITE_ID}"]')
        assert card.count() == 1
        card_text = _flat(card.inner_text())
        assert card_text.startswith("Письмо 1")
        assert "Вероятность помощи ИИ / посторонней помощи:" in card_text
        signal = card.locator('.chip.flag[title="large_paste_attempt"]')
        assert signal.inner_text() == "попытка вставить большой фрагмент текста"
        assert "large_paste_attempt" not in card_text  # the code only as a tooltip
        assert "Попытки вставки: 1 · максимальная длина 300 симв." in card_text
        assert f"Набрано с клавиатуры: {len(TYPED)} симв." in card_text
        assert "появилось без набора: 0 симв." in card_text
        assert "Скорость печати:" in card_text
        assert "Время вне вкладки во время письма:" in card_text
        assert "Выходы из полноэкранного режима: 0" in card_text

        # Rule #6: the human timeline and the raw events sit right below the verdict.
        assert page.evaluate(_EVIDENCE_ORDER_JS)
        paste_line = page.locator(
            "#detailBody table.timeline tr.tl-integrity",
            has_text="Попытка вставить текст (300 символов)",
        )
        assert paste_line.count() == 1
        assert _flat(paste_line.inner_text()).endswith("— заблокирована")
        assert "⚠︎" in paste_line.inner_text()
        timeline = page.locator("#detailBody table.timeline").inner_text()
        assert "перешёл в поле ответа (поле пустое)" in timeline
        assert "набор текста — +" in timeline  # writing_stats folded into one sentence
        assert "writing_stats" not in timeline and "field_focus" not in timeline
        assert PASTED[:20] not in page.content()  # the clipboard text is nowhere

        # A Gemini-shaped verdict through the SAME page (API responses intercepted):
        # the "✍ AI?" marker, the high-likelihood card, an unknown model flag.
        def _gemini_row(route) -> None:  # noqa: ANN001
            response = route.fetch()
            rows = response.json()
            for r in rows:
                if r["attempt_id"] == attempt_id:
                    r.update(
                        suspicion_score=0.72,
                        confidence=0.8,
                        ai_assist_max=0.81,
                        verdict_model_id="gemini-2.5-flash",
                    )
            route.fulfill(response=response, json=rows)

        def _gemini_detail(route) -> None:  # noqa: ANN001
            response = route.fetch()
            d = response.json()
            d["verdict"].update(
                suspicion_score=0.72,
                confidence=0.8,
                model_id="gemini-2.5-flash",
                flags=[*d["verdict"]["flags"], "vocabulary_above_level"],
                error="TimeoutException: первый запрос не прошёл",
            )
            d["verdict"]["writing"][0].update(
                ai_assist_likelihood=0.81, rationale="Текст заметно сложнее уровня A2."
            )
            route.fulfill(response=response, json=d)

        page.route(f"**/admin/tests/{SEED_TEST_ID}/results", _gemini_row)
        page.route(f"**/admin/results/{attempt_id}", _gemini_detail)
        page.click("#refreshBtn")
        marker = row.locator(".ai-mark")
        marker.wait_for()
        assert marker.inner_text() == "✍ AI?"
        chip = row.locator(".susp-chip.bad")
        assert chip.inner_text().strip() == "0.72"
        assert "Gemini gemini-2.5-flash" in (chip.get_attribute("title") or "")
        page.wait_for_function(
            "() => document.querySelector('#detailBody section.integrity')"
            ".textContent.includes('Источник: Gemini gemini-2.5-flash')"
        )
        text = _flat(section.inner_text())
        assert "Подозрение: высокая (0.72)" in text
        assert "Уверенность анализа: высокая (0.80)" in text
        assert "ИИ-анализ не сработал: TimeoutException" in text
        # An unknown (model-invented) flag falls back to its raw code.
        unknown = section.locator('.chip.flag[title="vocabulary_above_level"]')
        assert unknown.inner_text() == "vocabulary_above_level"
        card_text = _flat(card.inner_text())
        assert "высокая (0.81)" in card_text and "Текст заметно сложнее уровня A2." in card_text

        # The printable student/parent report (same tab, same session): no integrity.
        page.goto(f"{base_url}/web/teacher-report.html?attempt={attempt_id}")
        page.locator("#page h1").wait_for()
        printed = page.locator("body").inner_text()
        assert "Отчёт по тесту" in printed and TYPED in printed
        for word in ("Анализ честности", "Подозрение", "вставить", "Gemini", "формула"):
            assert word not in printed, word

        assert page_errors == []
        browser.close()
