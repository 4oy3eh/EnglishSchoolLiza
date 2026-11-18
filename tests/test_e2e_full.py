"""Phase 12 gate: full-stack honest-vs-cheat E2E.

Boots the whole API against a throwaway DB seeded with the human-approved demo
test, then drives two real attempts through real browser telemetry:

* **honest** — works each question with human-like pauses, never leaves the tab.
* **cheat** — for every question, backgrounds the tab, then returns and answers
  instantly (the "left, came back, answered immediately" pattern).

Both feed the *real* pipeline end to end: recorder.js -> ingest endpoint ->
append-only store -> deterministic integrity features + rules (Phase 7) -> advisory
verdict (Phase 8; the rule engine is the verdict of record, an injected
`MockAnalysisLLM` cannot change it). The gate (docs/PROMPTS.md Prompt 12): the
cheat attempt yields a **higher `suspicion_score`** than the honest one, and a
non-trivial verdict. The same two attempts are then finalized by the real submit
and opened in the teacher dashboard: the cheat attempt ranks first and shows the
strong "systematic tab switching" rule card in Russian with its numbers and a
jump to the raw moments in the timeline; the honest one shows that no rule fired.

Skips cleanly when Playwright/Chromium isn't installed (like the Phase 6 E2E), so
the rest of `make test` stays green in a bare environment.
"""

from __future__ import annotations

import json
import re
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
from tests.pin_helpers import TOKEN_HEADER, start_with_pin

sync_api = pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "e2e-full-teacher-pw"

# The seeded test's question item ids, kept in sync with the seed itself.
QIDS = [item.id for s in build_sample_test().sections for item in s.items]


# --------------------------------------------------------------------------- #
# Tiny stdlib HTTP helpers (avoid a test dependency on requests/httpx here).
# --------------------------------------------------------------------------- #
# Phase 14: every `/attempts/{id}/...` call needs the session token the PIN start
# returned. `_start_attempt*` record it here and the helpers below attach it to any
# URL of that attempt (the real token, exactly as the runner sends it).
_TOKENS: dict[str, str] = {}
_ATTEMPT_IN_URL = re.compile(r"/attempts/([^/?]+)")


def _request(url: str, method: str = "GET") -> urllib.request.Request:
    req = urllib.request.Request(url, data=b"" if method != "GET" else None, method=method)
    m = _ATTEMPT_IN_URL.search(url)
    if m and m.group(1) in _TOKENS:
        req.add_header(TOKEN_HEADER, _TOKENS[m.group(1)])
    return req


def _get_json(url: str) -> object:
    with urllib.request.urlopen(_request(url), timeout=5) as resp:
        return json.load(resp)


def _post_json(url: str) -> dict:
    with urllib.request.urlopen(_request(url, "POST"), timeout=5) as resp:
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
        "TEACHER_PASSWORD": PASSWORD,  # the dashboard part logs in for real
        "ENVIRONMENT": "dev",
        "SKIP_STARTUP_SWEEP": "1",  # only the submit-time finalize writes reports
        # Never call a paid API from a test (a developer's `.env` may carry a key):
        # no Gemini grading / feedback, and the integrity verdict is rules-only.
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


def _start_attempt(base_url: str, display_name: str, db_url: str) -> str:
    """Pick a name off the seeded roster, type its PIN and start (real lifecycle)."""
    started = start_with_pin(base_url, db_url, SEED_TEST_ID, display_name)
    _TOKENS[started["attempt_id"]] = started["token"]
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


def _recorder_url(base_url: str, attempt_id: str) -> str:
    """The recorder harness for an attempt, carrying that attempt's session token."""
    return f"{base_url}/web/recorder-test.html?attempt={attempt_id}&token={_TOKENS[attempt_id]}"


def _open_recorder(context: object, base_url: str, attempt_id: str) -> object:
    page = context.new_page()  # type: ignore[attr-defined]
    page.goto(_recorder_url(base_url, attempt_id))
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


def _wait_for_events(
    db_url: str, attempt_id: str, event_type: str, want: int, timeout: float = 8.0
) -> None:
    deadline = time.monotonic() + timeout
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

    honest_attempt = _start_attempt(base_url, "Anna", db_url)
    cheat_attempt = _start_attempt(base_url, "Bao", db_url)

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

    # Wait for the telemetry to land, THEN finalize both attempts through the real
    # delivery lifecycle (the submit-time finalize stores the verdict the dashboard
    # reads, so it must see the whole stream).
    _wait_for_events(db_url, honest_attempt, "answer_change", len(QIDS))
    _wait_for_events(db_url, cheat_attempt, "visibility_hidden", len(QIDS))
    _wait_for_events(db_url, cheat_attempt, "visibility_visible", len(QIDS))
    _wait_for_events(db_url, cheat_attempt, "answer_change", len(QIDS))
    _post_json(f"{base_url}/exam/attempts/{honest_attempt}/submit")
    _post_json(f"{base_url}/exam/attempts/{cheat_attempt}/submit")

    honest_verdict = _verdict(db_url, honest_attempt)
    cheat_verdict = _verdict(db_url, cheat_attempt)

    # The gate: cheat is more suspicious, and non-trivially so.
    assert cheat_verdict.suspicion_score > honest_verdict.suspicion_score
    assert cheat_verdict.suspicion_score >= 0.5  # non-trivial verdict
    assert honest_verdict.suspicion_score < 0.2  # honest stays low
    # The cheat verdict names the rule it rests on and points at the raw moments
    # behind it (rule #6); the injected mock analyst changed none of it.
    assert "systematic_tab_switching" in cheat_verdict.flags
    systematic = next(r for r in cheat_verdict.rules if r.code == "systematic_tab_switching")
    assert systematic.severity == "strong" and systematic.evidence.events
    assert not [r for r in honest_verdict.rules if r.severity == "strong"]
    # Advisory only (golden rule #2): a verdict never carries a score field.
    assert not hasattr(cheat_verdict, "score")

    # The same attempts in the teacher dashboard (the verdict STORED at submit).
    stored = _stored_verdict(db_url, cheat_attempt)
    _stored_verdict(db_url, honest_attempt)
    try:
        _dashboard_shows_the_rule_cards(base_url, cheat_attempt, honest_attempt, stored)
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed: {exc}")
        raise


def _stored_verdict(db_url: str, attempt_id: str, timeout: float = 20.0) -> dict:
    """The verdict JSON the submit-time finalize froze on the attempt's report."""
    from sqlalchemy import text

    engine = create_engine(db_url)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT verdict FROM attempt_reports WHERE attempt_id = :a"),
                    {"a": attempt_id},
                ).first()
            if row is not None and row[0]:
                return json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])
            time.sleep(0.2)
    finally:
        engine.dispose()
    raise AssertionError(f"attempt {attempt_id}: no stored verdict after {timeout}s")


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _dashboard_shows_the_rule_cards(
    base_url: str, cheat_attempt: str, honest_attempt: str, stored: dict
) -> None:
    """Teacher view of the tab-switch cheat: ranked first, the strong rule card in
    Russian with this attempt's numbers, and a jump to the raw timeline moments."""
    # What finalize stored (rules only: the server runs with ANALYSIS_LLM=off).
    assert stored["model_id"] is None and stored["rules_version"] == 1
    rule = next(r for r in stored["rules"] if r["code"] == "systematic_tab_switching")
    assert rule["severity"] == "strong"
    n = len(QIDS)
    sentence = re.fullmatch(
        rf"Ушёл со вкладки {n} раз; (\d+) из {n} ответов дал в течение 2 с после "
        r"возврата \((\d+) %\)\.",
        rule["explanation_ru"],
    )
    assert sentence, rule["explanation_ru"]
    fast = int(sentence.group(1))
    assert fast >= 3 and int(sentence.group(2)) == round(100 * fast / n)
    assert len(rule["evidence"]["item_ids"]) == fast
    assert len(rule["evidence"]["events"]) == 3 * fast  # leave + return + answer each
    assert stored["suspicion_score"] >= 0.65  # one strong rule alone

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

        # Results: ranked by the rule-engine suspicion -> the cheat attempt is first.
        cheat_row = page.locator(f'#resultsTable tbody tr[data-attempt="{cheat_attempt}"]')
        cheat_row.wait_for()
        first_row = page.locator("#resultsTable tbody tr").first
        assert first_row.get_attribute("data-attempt") == cheat_attempt
        chip = cheat_row.locator(".susp-chip")
        assert chip.inner_text().strip() == f"{stored['suspicion_score']:.2f}"
        assert "bad" in (chip.get_attribute("class") or "")  # "высокая"
        title = chip.get_attribute("title") or ""
        assert "Подозрение высокая (по правилам, без ИИ)" in title
        assert "Главное: Систематические уходы со вкладки перед ответами" in title
        honest_row = page.locator(f'#resultsTable tbody tr[data-attempt="{honest_attempt}"]')
        honest_chip = honest_row.locator(".susp-chip")
        assert "ok" in (honest_chip.get_attribute("class") or "")  # "низкая"

        # Detail: the strong rule card, in Russian, with the stored sentence verbatim.
        cheat_row.locator('[data-act="detail"]').click()
        section = page.locator("#detailBody section.integrity")
        section.wait_for()
        text = _flat(section.inner_text())
        assert "Источник: детерминированные правила (без ИИ)" in text
        assert "Подозрение: высокая" in text
        assert "Второе мнение" not in text  # no LLM was involved
        card = section.locator(".rule-card").first  # strongest first
        assert card.get_attribute("data-rule") == "systematic_tab_switching"
        assert card.locator(".rule-severity").inner_text() == "сильный сигнал"
        assert (
            card.locator("h3").text_content()
            == "Систематические уходы со вкладки перед ответами"
        )
        assert card.locator(".rule-explanation").inner_text() == rule["explanation_ru"]
        assert "systematic_tab_switching" not in _flat(card.inner_text())  # no raw code

        # The button highlights exactly the raw moments behind the rule in the
        # timeline: every leave, return and instant answer it counted.
        highlighted = page.locator("#detailBody table.timeline tr.tl-evidence")
        assert highlighted.count() == 0
        button = card.locator("button.rule-evidence")
        assert _flat(button.inner_text()) == f"Показать в хронологии ({3 * fast})"
        button.click()
        assert highlighted.count() == 3 * fast
        lines = [_flat(t) for t in highlighted.all_inner_texts()]
        assert sum("Ушёл со вкладки теста" in line for line in lines) == fast
        assert sum("Вернулся на вкладку" in line for line in lines) == fast
        assert sum("ответ «" in line for line in lines) == fast
        assert "active" in (card.get_attribute("class") or "")

        # The honest attempt: nothing fired, and the page says so.
        honest_row.locator('[data-act="detail"]').click()
        page.wait_for_function(
            "() => { const s = document.querySelector('#detailBody section.integrity');"
            " return s && s.querySelector('.rules-none, .rule-card') &&"
            " !s.querySelector('.rule-card.sev-strong'); }"
        )
        honest_section = page.locator("#detailBody section.integrity")
        assert honest_section.locator(".rule-card.sev-strong").count() == 0
        assert page.locator("#detailBody table.timeline tr.tl-evidence").count() == 0

        assert page_errors == []
        browser.close()


# --------------------------------------------------------------------------- #
# Phase 13 P0 persistence: answers and telemetry survive a network blip.
# --------------------------------------------------------------------------- #
_ACTIVE_KEY = "exam:active"


def _resume_script(attempt_id: str, roster_entry_id: str) -> str:
    """Init script: make exam.js resume `attempt_id` (with its session token, as a
    reload after a PIN start would) instead of showing the picker."""
    active = json.dumps(
        {
            "testId": SEED_TEST_ID,
            "attemptId": attempt_id,
            "rosterEntryId": roster_entry_id,
            "name": "Carlos",
            "token": _TOKENS[attempt_id],
        }
    )
    return f"localStorage.setItem({json.dumps(_ACTIVE_KEY)}, {json.dumps(active)});"


def _start_attempt_entry(base_url: str, display_name: str, db_url: str) -> tuple[str, str]:
    started = start_with_pin(base_url, db_url, SEED_TEST_ID, display_name)
    _TOKENS[started["attempt_id"]] = started["token"]
    return str(started["attempt_id"]), str(started["roster_entry_id"])


def test_answer_save_retries_after_network_blip_and_reload(
    live_server: tuple[str, str],
) -> None:
    """Block the PUT answers route, answer, reload (still blocked), unblock: the
    runner's save queue (in memory + localStorage mirror) must resend the answer
    and the header indicator must go back to "Saved"."""
    base_url, db_url = live_server
    attempt_id, roster_entry_id = _start_attempt_entry(base_url, "Carlos", db_url)
    blocked = {"on": True}

    def gate(route: object) -> None:
        if blocked["on"]:
            route.abort("failed")  # type: ignore[attr-defined]
        else:
            route.continue_()  # type: ignore[attr-defined]

    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            context.add_init_script(_resume_script(attempt_id, roster_entry_id))
            page = context.new_page()
            page.route("**/exam/attempts/*/answers/*", gate)
            page.goto(f"{base_url}/web/exam.html?test={SEED_TEST_ID}")
            page.wait_for_selector("input[name='opt-seed-q-read']")
            assert page.text_content("#saveStatus") == "Saved"

            # Answer the first (single_choice) question while the network is down.
            page.locator("input[name='opt-seed-q-read'][value='0']").check()
            page.wait_for_function(
                "() => document.querySelector('#saveStatus').textContent.includes('not saved')",
                timeout=8000,
            )
            assert page.evaluate(
                "() => localStorage.getItem('exam:pending:' + "
                + json.dumps(attempt_id)
                + ")"
            ) == json.dumps({"seed-q-read": 0}, separators=(",", ":"))
            # Nothing reached the server yet.
            assert _get_json(f"{base_url}/exam/attempts/{attempt_id}/answers") == {}

            # Reload while still offline: the dirty answer must come back from
            # localStorage, be shown as selected, and keep retrying.
            page.reload()
            page.wait_for_selector("input[name='opt-seed-q-read']")
            assert page.is_checked("input[name='opt-seed-q-read'][value='0']")
            page.wait_for_function(
                "() => /Saving|not saved/.test(document.querySelector('#saveStatus').textContent)",
                timeout=8000,
            )

            # Network back: the retry (backoff <= 2 s here) lands the answer.
            # (unroute, not just a flag flip: a sync-API route handler only runs
            # while Python is inside a Playwright call, so a plain `time.sleep`
            # poll on our side would otherwise stall every gated request.)
            blocked["on"] = False
            page.unroute("**/exam/attempts/*/answers/*", gate)
            page.wait_for_function(
                "() => document.querySelector('#saveStatus').textContent === 'Saved'",
                timeout=15000,
            )
            assert _get_json(f"{base_url}/exam/attempts/{attempt_id}/answers") == {
                "seed-q-read": 0
            }
            assert page.evaluate(
                "() => localStorage.getItem('exam:pending:' + " + json.dumps(attempt_id) + ")"
            ) is None
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed: {exc}")
        raise


_STATUS_JS = "() => document.querySelector('#saveStatus').textContent"
_REJECTED_ONE = "\u26a0 1 answer rejected by the server"


def _nav_done(page: object, item_id: str) -> bool:
    cls = page.get_attribute(f".navsq[data-item='{item_id}']", "class")  # type: ignore[attr-defined]
    return "done" in (cls or "").split()


def test_answer_rejected_by_server_is_surfaced_not_saved(
    live_server: tuple[str, str],
) -> None:
    """A 422 on PUT answer must not look saved: the header says it was rejected,
    the navigator square stays unanswered, a new value clears the rejection, and
    the submit confirm + done screen name the rejected question."""
    base_url, db_url = live_server
    attempt_id, roster_entry_id = _start_attempt_entry(base_url, "Anna", db_url)
    answers_url = f"{base_url}/exam/attempts/{attempt_id}/answers"
    pattern = "**/exam/attempts/*/answers/*"

    def reject(route: object) -> None:
        route.fulfill(  # type: ignore[attr-defined]
            status=422, content_type="application/json", body='{"detail":"bad"}'
        )

    dialogs: list[str] = []

    def on_dialog(dialog: object) -> None:
        dialogs.append(dialog.message)  # type: ignore[attr-defined]
        dialog.accept()  # type: ignore[attr-defined]

    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            context.add_init_script(_resume_script(attempt_id, roster_entry_id))
            page = context.new_page()
            page.on("dialog", on_dialog)
            page.route(pattern, reject)
            page.goto(f"{base_url}/web/exam.html?test={SEED_TEST_ID}")
            page.wait_for_selector("input[name='opt-seed-q-read']")

            # 1) The server refuses the answer: rejected, not saved, not answered.
            page.locator("input[name='opt-seed-q-read'][value='0']").check()
            page.wait_for_function(f"() => ({_STATUS_JS})().includes('rejected')", timeout=8000)
            assert page.evaluate(_STATUS_JS) == f"Saved \u00b7 {_REJECTED_ONE}"
            assert not _nav_done(page, "seed-q-read")
            # The control still shows what the student picked (so they can change it).
            assert page.is_checked("input[name='opt-seed-q-read'][value='0']")
            assert _get_json(answers_url) == {}

            # 2) A new value for that item clears the rejection and gets saved.
            page.unroute(pattern, reject)
            page.locator("input[name='opt-seed-q-read'][value='1']").check()
            page.wait_for_function(f"() => ({_STATUS_JS})() === 'Saved'", timeout=8000)
            assert _nav_done(page, "seed-q-read")
            assert _get_json(answers_url) == {"seed-q-read": 1}

            # 3) Another rejection is listed by question number at submit time.
            page.route(pattern, reject)
            page.click(".navsq[data-item='seed-q-match']")
            # The matching radios are visually hidden behind letter tiles: click the tile.
            page.locator("#q-seed-q-match label.opt-letter").filter(
                has=page.locator("input[value='A']")
            ).click()
            page.wait_for_function(f"() => ({_STATUS_JS})().includes('rejected')", timeout=8000)
            assert not _nav_done(page, "seed-q-match")
            match_no = (page.text_content(".navsq[data-item='seed-q-match']") or "").strip()
            assert match_no

            page.click(".navsq[data-item='seed-q-write']")  # last part -> Review visible
            page.click("#reviewBtn")  # the review panel holds the final Submit
            page.click("#submitBtn")
            page.wait_for_selector("#done:not(.hidden)", timeout=15000)
            rejected_msgs = [m for m in dialogs if "rejected by the server" in m]
            assert len(rejected_msgs) == 1, dialogs
            assert f"(question {match_no})" in rejected_msgs[0]
            done_note = page.text_content("#doneUnsaved") or ""
            assert "rejected by the server" in done_note
            assert f"(question {match_no})" in done_note
            page.unroute(pattern, reject)
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed: {exc}")
        raise

    # The rejected value never reached the store; the accepted one did.
    assert _get_json(answers_url) == {"seed-q-read": 1}


def test_telemetry_resends_after_network_blip_and_reload(
    live_server: tuple[str, str],
) -> None:
    """Block the ingest route, background the tab, reload (still blocked), unblock:
    the recorder keeps the event (queue + localStorage) and delivers it later."""
    base_url, db_url = live_server
    attempt_id = _start_attempt(base_url, "Dmitri", db_url)
    blocked = {"on": True}

    def gate(route: object) -> None:
        if blocked["on"]:
            route.abort("failed")  # type: ignore[attr-defined]
        else:
            route.continue_()  # type: ignore[attr-defined]

    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.route("**/attempts/*/events", gate)
            page.goto(_recorder_url(base_url, attempt_id))
            page.wait_for_function("() => window.__recorder !== undefined")

            page.evaluate(_HIDE)  # records visibility_hidden + tries to send
            # The failed send must keep the event queued AND mirror it to storage.
            page.wait_for_function(
                "(k) => (localStorage.getItem(k) || '').includes('visibility_hidden')",
                arg=f"exam:telemetry:{attempt_id}",
                timeout=8000,
            )
            assert page.evaluate(
                "() => window.__recorder.queue.some(e => e.type === 'visibility_hidden')"
            )
            assert _count_events(db_url, attempt_id, "visibility_hidden") == 0

            # Reload while still offline: the new recorder restores the queue.
            page.reload()
            page.wait_for_function("() => window.__recorder !== undefined")
            page.wait_for_function(
                "() => window.__recorder.queue.some(e => e.type === 'visibility_hidden')"
            )

            # Network back: the next tick (backoff + 5 s interval) delivers it.
            # (unroute — see the answer test — so the DB poll below can't stall it.)
            blocked["on"] = False
            page.unroute("**/attempts/*/events", gate)
            _wait_for_events(db_url, attempt_id, "visibility_hidden", 1, timeout=15.0)
            page.wait_for_function(
                "(k) => localStorage.getItem(k) === null",
                arg=f"exam:telemetry:{attempt_id}",
                timeout=15000,
            )
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed: {exc}")
        raise


def _marker_events(db_url: str, attempt_id: str, marker: str) -> int:
    """How many stored answer_change events carry `marker` as their value."""
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            from sqlalchemy import text

            rows = conn.execute(
                text(
                    "SELECT payload FROM integrity_events "
                    "WHERE attempt_id = :a AND type = 'answer_change'"
                ),
                {"a": attempt_id},
            ).all()
    finally:
        engine.dispose()
    return sum(
        1
        for (payload,) in rows
        if (json.loads(payload) if isinstance(payload, str) else payload).get("value") == marker
    )


def test_restored_telemetry_survives_a_second_reload_before_any_send(
    live_server: tuple[str, str],
) -> None:
    """Events restored from the localStorage snapshot must not be dropped when the
    page dies again before its first successful send.

    Load 1 records a marker event while ingest is down (snapshot). Load 2 restores
    it and its first send *hangs* (no failure, no backoff re-persist) when the page
    starts unloading - the unload path (visibilitychange persist + pagehide, which
    must not trust a beacon while the network is known-bad) must keep the restored
    events in the snapshot. Load 3 still has the marker, and it lands once ingest
    is back."""
    base_url, db_url = live_server
    attempt_id = _start_attempt(base_url, "Bao", db_url)
    key = f"exam:telemetry:{attempt_id}"
    pattern = "**/attempts/*/events"
    marker = "restored-marker-7f3a"
    mode = {"v": "abort"}

    def gate(route: object) -> None:
        if mode["v"] == "abort":
            route.abort("failed")  # type: ignore[attr-defined]
        # "hang": never answered - the request stays in flight until the page dies.

    in_snapshot = "([k, m]) => (localStorage.getItem(k) || '').includes(m)"
    in_queue = (
        "(m) => window.__recorder.queue.concat(window.__recorder._inflight)"
        ".some(e => e.payload && e.payload.value === m)"
    )

    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.route(pattern, gate)
            page.goto(_recorder_url(base_url, attempt_id))
            page.wait_for_function("() => window.__recorder !== undefined")

            # Load 1: the marker's send fails -> queued + snapshotted.
            page.evaluate("(m) => { window.__recorder.answerChange('q-1', m); }", marker)
            page.evaluate(_HIDE)  # flushes; the abort makes the send fail
            page.wait_for_function(in_snapshot, arg=[key, marker], timeout=8000)

            # Load 2 restores it; its first send hangs (in flight, not failed).
            mode["v"] = "hang"
            page.reload()
            page.wait_for_function("() => window.__recorder !== undefined")
            page.wait_for_function(in_queue, arg=marker)
            page.wait_for_function("() => window.__recorder._sending === true")
            assert page.evaluate(in_snapshot, [key, marker])  # re-persisted on restore

            # Load 2 starts dying with that send still in flight. Fire the unload
            # handlers in-page first so the snapshot can be checked deterministically
            # (on a real reload Playwright cancels the intercepted request, whose
            # rejection would re-persist and mask a bug; a real keepalive request
            # outlives the page and nothing re-persists).
            page.evaluate(_HIDE)  # visibilitychange -> persist
            page.evaluate("() => window.dispatchEvent(new Event('pagehide'))")
            assert page.evaluate(in_snapshot, [key, marker]), page.evaluate(
                "(k) => localStorage.getItem(k)", key
            )

            # Load 2 dies before any successful send: load 3 must still have it.
            mode["v"] = "abort"
            page.reload()
            page.wait_for_function("() => window.__recorder !== undefined")
            page.wait_for_function(in_queue, arg=marker)
            assert _marker_events(db_url, attempt_id, marker) == 0

            # Network back: it lands, and the snapshot goes once the queue drains.
            # (unroute before polling the DB - see the answer-save test.)
            page.unroute(pattern, gate)
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline and not _marker_events(db_url, attempt_id, marker):
                time.sleep(0.2)
            assert _marker_events(db_url, attempt_id, marker) >= 1
            page.wait_for_function(
                "(k) => localStorage.getItem(k) === null", arg=key, timeout=15000
            )
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed: {exc}")
        raise


# --------------------------------------------------------------------------- #
# Roster after auto-expiry: the picker shows the student as done, not "in progress".
# --------------------------------------------------------------------------- #
def test_picker_shows_expired_student_as_done(live_server: tuple[str, str]) -> None:
    """A student whose time ran out (server-side expiry, no submit) is listed as
    "(done)" and cannot be picked again — like a submitted one."""
    from datetime import UTC, datetime, timedelta

    from app.persistence.repository import AttemptRepository

    base_url, db_url = live_server
    attempt_id = _start_attempt(base_url, "Dmitri", db_url)
    engine = create_engine(db_url)
    try:
        with sessionmaker(bind=engine, expire_on_commit=False)() as s:
            repo = AttemptRepository(s)
            attempt = repo.get_attempt(attempt_id)
            repo.update_attempt(
                attempt.model_copy(update={"deadline": datetime.now(UTC) - timedelta(minutes=1)})
            )
            s.commit()
    finally:
        engine.dispose()
    state = _get_json(f"{base_url}/exam/attempts/{attempt_id}/state")
    assert isinstance(state, dict) and state["status"] == "expired"

    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(f"{base_url}/web/exam.html?test={SEED_TEST_ID}")
            page.wait_for_selector("#nameList button")
            btn = page.locator("#nameList button", has_text="Dmitri")
            assert "(done)" in (btn.text_content() or "")
            assert btn.is_disabled()
            other = page.locator("#nameList button", has_text="Carlos")
            assert "(done)" not in (other.text_content() or "")
            assert other.is_enabled()
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed: {exc}")
        raise
