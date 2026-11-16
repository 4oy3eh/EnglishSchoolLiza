"""Teacher dashboard smoke test in a real browser (Phase 13 hardening).

Boots uvicorn on a throwaway sqlite DB seeded with the published demo test (the
`tests/test_e2e_full.py` pattern, `SKIP_STARTUP_SWEEP=1` via conftest), starts two
real attempts, then drives `apps/web/teacher.html`: sign in, open the test, check the
"copy student link" button exists, bulk-add two students, sort the results table,
and trigger a server refusal (delete a test that has attempts without force) to
assert the Russian error toast appears.

Skips cleanly when Playwright/Chromium isn't installed.
"""

from __future__ import annotations

import json
import os
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
from app.content.seed import SEED_TEST_ID, seed
from app.content.storage import FilesystemStorage
from app.core.db import Base

sync_api = pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "e2e-teacher-pw"


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


def _get_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.load(resp)


def _post_json(url: str) -> dict:
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = resp.read()
    return json.loads(body) if body else {}


@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[str]:
    db_path = tmp_path / "dash.db"
    db_url = f"sqlite:///{db_path.as_posix()}"
    assets_dir = tmp_path / "assets"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
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
        "SKIP_STARTUP_SWEEP": "1",
        "GEMINI_API_KEY": "",  # never a paid call from the smoke test
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


def _start(base_url: str, name: str) -> str:
    roster = _get_json(f"{base_url}/exam/tests/{SEED_TEST_ID}/roster")
    assert isinstance(roster, list)
    entry = next(e for e in roster if e["display_name"] == name)
    return str(_post_json(f"{base_url}/exam/roster/{entry['roster_entry_id']}/start")["attempt_id"])


def _names(page: object) -> list[str]:
    return page.eval_on_selector_all(  # type: ignore[attr-defined]
        "#resultsTable tbody td.name", "els => els.map(e => e.textContent.trim())"
    )


def test_teacher_dashboard_smoke(live_server: str) -> None:
    base_url = live_server
    bao = _start(base_url, "Bao")
    _start(base_url, "Anna")
    _post_json(f"{base_url}/exam/attempts/{bao}/submit")

    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            dialogs: list[str] = []

            def _accept(dialog) -> None:  # noqa: ANN001
                dialogs.append(dialog.message)
                dialog.accept()

            page.on("dialog", _accept)
            page_errors: list[str] = []
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))

            page.goto(f"{base_url}/web/teacher.html")
            # A wrong password says so (not a network error).
            page.fill("#password", "nope")
            page.click("#loginBtn")
            page.wait_for_function(
                "() => document.querySelector('#loginErr').textContent.includes('Неверный пароль')"
            )
            page.fill("#password", PASSWORD)
            page.click("#loginBtn")

            row = page.locator(f'#testsTable tr[data-test-id="{SEED_TEST_ID}"]')
            row.wait_for()
            # Published test -> the student-link button exists (we don't copy).
            link_btn = row.locator('[data-act="link"]')
            assert link_btn.count() == 1
            assert "Скопировать ссылку для учеников" in link_btn.inner_text()

            row.locator('[data-act="open"]').click()
            page.locator("#rosterSection").wait_for(state="visible")
            assert page.locator("#copyLinkBtn").is_visible()

            # Bulk add two students (one name per line).
            page.click("#bulkToggleBtn")
            page.fill("#bulkNames", "Пётр Иванов\n\nЗоя\n")
            page.click("#bulkAddBtn")
            page.wait_for_function(
                "() => [...document.querySelectorAll('#rosterTable tbody td:first-child')]"
                ".map(td => td.textContent).filter(n => n === 'Пётр Иванов' || n === 'Зоя')"
                ".length === 2"
            )
            page.locator(".toast.ok", has_text="Добавлено учеников: 2").wait_for()

            # Results: two attempts; sort by name asc, then desc.
            page.wait_for_function(
                "() => document.querySelectorAll('#resultsTable tbody tr').length === 2"
            )
            page.click('#resultsTable th[data-sort="name"]')
            assert _names(page) == ["Anna", "Bao"]
            page.click('#resultsTable th[data-sort="name"]')
            assert _names(page) == ["Bao", "Anna"]
            assert "▼" in page.inner_text('#resultsTable th[data-sort="name"]')
            # Search filters client-side.
            page.fill("#resultsSearch", "ann")
            assert _names(page) == ["Anna"]
            page.fill("#resultsSearch", "")

            # Trigger a server refusal: the stats read is stale (says no attempts), so
            # the UI deletes WITHOUT force -> the server refuses (409) -> Russian toast.
            page.route(
                f"**/admin/tests/{SEED_TEST_ID}/stats",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "test_id": SEED_TEST_ID,
                            "roster_count": 0,
                            "attempt_count": 0,
                            "active_count": 0,
                        }
                    ),
                ),
            )
            row.locator('[data-act="delete"]').click()
            toast = page.locator(".toast.error", has_text="В тесте уже есть работы учеников")
            toast.wait_for()
            assert "Не удалось удалить тест" in toast.inner_text()
            assert any("Удалить тест" in d for d in dialogs)
            # Nothing was deleted, and the toast is dismissible.
            assert page.locator(f'#testsTable tr[data-test-id="{SEED_TEST_ID}"]').count() == 1
            toast.locator("button").click()
            assert page.locator(".toast.error").count() == 0
            assert page_errors == []

            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed: {exc}")
        raise


# --------------------------------------------------------------------------- #
# XSS: every server value reaches the DOM escaped (list, detail, test view, print).
# --------------------------------------------------------------------------- #
# A roster name is typed by a person; a model id / summary / rationale / signal comes
# from an LLM; an IP / user-agent comes from the student's device. None is trusted.
XSS_NAME = "<img src=x onerror=window.__xss=1>"
# Breaks out of text, double- and single-quoted attributes if any of them is unescaped.
XSS = (
    '"><img src=x onerror=window.__xss=1><svg onload=window.__xss=1>'
    "' onmouseover='window.__xss=1' x=\""
)
# Discriminators / timestamps the renderers branch on or parse (never interpolated raw).
_KEEP_KEYS = frozenset(
    {"at", "client_ts", "server_ts", "graded_at", "finished_at", "type", "item_type", "kind"}
)
_PROBE = """() => ({
  flag: typeof window.__xss,
  injected: document.querySelectorAll('img[src="x"], svg[onload]').length,
  handlers: [...document.querySelectorAll('*')]
    .filter((el) => [...el.attributes].some((a) => a.name.toLowerCase().startsWith('on')))
    .length,
})"""
_CLEAN = {"flag": "undefined", "injected": 0, "handlers": 0}


def _poison(value: object, key: str | None = None) -> object:
    """Append the payload to every string in a JSON document (except `_KEEP_KEYS`)."""
    if isinstance(value, dict):
        return {k: _poison(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_poison(v, key) for v in value]
    if isinstance(value, str) and key not in _KEEP_KEYS:
        return value + XSS
    return value


def _admin_json(base_url: str, path: str, *, token: str | None = None, body: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{base_url}{path}", data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)


def _hostile_detail(data: dict) -> dict:
    """The real attempt detail with every string poisoned + rule hits and a
    Gemini-shaped second opinion, a timeline line and a device row made of the
    payload."""
    data = _poison(data)  # type: ignore[assignment]
    writing = [r["item_id"] for r in data["responses"] if r["item_type"] == "open_writing"]
    at = "2026-06-21T12:00:00Z"
    data["verdict"].update(
        suspicion_score=0.7,
        confidence=0.5,
        model_id=XSS,
        summary=XSS,
        error=XSS,
        flags=[XSS, "long_hidden", "constructor"],
        rules_version=1,
        rules=[
            {  # every string of a rule hit is hostile, its severity included
                "code": XSS,
                "title_ru": XSS,
                "severity": XSS,
                "explanation_ru": XSS,
                "evidence": {
                    "item_ids": [XSS],
                    "events": [{"type": "x", "at": at, "item_id": None}],
                },
            },
            {  # no title: falls back to the code's label — own keys only
                "code": "constructor",
                "title_ru": "",
                "severity": "constructor",
                "explanation_ru": "",
                "evidence": {"item_ids": [], "events": []},
            },
        ],
        writing=[
            {
                "item_id": writing[0] if writing else XSS,
                "ai_assist_likelihood": 0.9,
                "signals": [XSS, "large_paste_attempt", "constructor"],
                "rationale": XSS,
            }
        ],
    )
    data["timeline"].append(
        {"at": at, "type": "x", "item_id": None, "item_no": XSS, "label": XSS, "integrity": True}
    )
    data["devices"].append(
        {
            "at": at, "user_agent": XSS, "platform": XSS, "timezone": XSS, "language": XSS,
            "screen": XSS, "viewport": XSS, "server_ip": XSS,
        }
    )
    data["profile"]["device_changed"] = True
    data["skills"] = [
        {"skill": XSS, "score": 1, "max_score": 2, "strengths": XSS, "weaknesses": XSS}
    ]
    data.update(summary=XSS, report_status="failed", report_error=XSS, verdict_source="stored")
    return data


def test_dashboard_escapes_hostile_server_values(live_server: str) -> None:
    """A student named `<img onerror>` and a model id with a quote execute nothing.

    The hostile roster name is REAL data (added through the admin API, then a real
    attempt); the LLM-/device-sourced strings are injected by rewriting the admin
    API's JSON on the wire, so every renderer gets a payload in every string field.
    Also checks that a re-opened row shows "—" instead of the stale suspicion chip.
    """
    base_url = live_server
    token = _admin_json(base_url, "/admin/login", body={"password": PASSWORD})["token"]
    _admin_json(
        base_url,
        f"/admin/tests/{SEED_TEST_ID}/roster",
        token=token,
        body={"display_name": XSS_NAME},
    )
    evil = _start(base_url, XSS_NAME)
    _post_json(f"{base_url}/exam/attempts/{evil}/submit")
    bao = _start(base_url, "Bao")

    def _rewrite(route, transform) -> None:  # noqa: ANN001
        response = route.fetch()
        if response.status != 200:
            route.fulfill(response=response)
            return
        route.fulfill(response=response, json=transform(response.json()))

    def _hostile_list(rows: list[dict]) -> list[dict]:
        for row in rows:
            if row["attempt_id"] == evil:
                # A finalized attempt judged by a model whose id carries a quote.
                row.update(
                    suspicion_score=0.7, confidence=0.5, ai_assist_max=0.9,
                    verdict_model_id=XSS, report_status="failed", report_error=XSS,
                    top_rules=[XSS, "second" + XSS], verdict_legacy=False,
                )
            elif row["attempt_id"] == bao:
                # Re-opened by the teacher: the stored verdict is from the last submit.
                row.update(
                    report_status="reopened", attempt_status="in_progress",
                    suspicion_score=0.8, confidence=0.4, ai_assist_max=0.9,
                    verdict_model_id=XSS,
                )
        return rows

    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            context.route(
                f"**/admin/tests/{SEED_TEST_ID}/results",
                lambda route: _rewrite(route, _hostile_list),
            )
            context.route(
                f"**/admin/results/{evil}", lambda route: _rewrite(route, _hostile_detail)
            )
            context.route(
                f"**/admin/results/{evil}/report", lambda route: _rewrite(route, _poison)
            )
            context.route(
                f"**/admin/tests/{SEED_TEST_ID}", lambda route: _rewrite(route, _poison)
            )
            page = context.new_page()
            page_errors: list[str] = []
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            page.on("dialog", lambda dialog: dialog.dismiss())

            def assert_clean(target, where: str) -> None:  # noqa: ANN001
                target.wait_for_timeout(300)  # give a stray onerror / onload time to fire
                assert target.evaluate(_PROBE) == _CLEAN, f"markup injected in {where}"

            page.goto(f"{base_url}/web/teacher.html")
            page.fill("#password", PASSWORD)
            page.click("#loginBtn")
            row = page.locator(f'#testsTable tr[data-test-id="{SEED_TEST_ID}"]')
            row.wait_for()
            row.locator('[data-act="open"]').click()
            page.wait_for_function(
                "() => document.querySelectorAll('#resultsTable tbody tr').length === 2"
            )

            # -- list: the name is text; the model id stays inside the title ------ #
            assert XSS_NAME in _names(page)
            roster_names = page.eval_on_selector_all(
                "#rosterTable tbody td:first-child", "els => els.map(e => e.textContent)"
            )
            assert XSS_NAME in roster_names
            evil_row = page.locator(f'#resultsTable tr[data-attempt="{evil}"]')
            chip = evil_row.locator(".susp-chip")
            assert chip.inner_text() == "0.70"
            # The rule titles (server data) stay inside the tooltip, as text.
            assert f"Главное: {XSS}; second{XSS}." in (chip.get_attribute("title") or "")
            assert evil_row.locator(".ai-mark").count() == 1
            ai_title = evil_row.locator(".ai-mark").get_attribute("title") or ""
            assert f"Gemini {XSS}" in ai_title
            assert evil_row.locator(".chip.bad", has_text="ошибка").get_attribute("title") == XSS
            # A re-opened attempt: "—" + a tooltip, never the previous submit's chip.
            bao_row = page.locator(f'#resultsTable tr[data-attempt="{bao}"]')
            assert bao_row.locator(".susp-chip").count() == 0
            assert bao_row.locator(".ai-mark").count() == 0
            stale = bao_row.locator(".susp-stale")
            assert stale.inner_text() == "—"
            assert "от прошлой сдачи" in (stale.get_attribute("title") or "")
            assert [c.strip() for c in bao_row.locator("td.susp").all_inner_texts()] == ["—", "—"]
            assert_clean(page, "the results list")

            # -- detail: verdict, writing cards, timeline, devices, breakdown ----- #
            evil_row.locator('[data-act="detail"]').click()
            page.locator("#detailBody section.integrity").wait_for()
            detail = page.locator("#detailBody")
            assert detail.locator(".verdict-error").count() == 1
            # Rule cards: title / explanation are text; a hostile severity falls back
            # to the weakest label and class; a card without a title shows its code.
            cards = detail.locator(".integrity .rule-card")
            assert cards.count() == 2
            assert cards.first.locator("h3").inner_text() == XSS
            assert cards.first.locator(".rule-explanation").inner_text() == XSS
            assert cards.first.locator(".rule-severity").inner_text() == "слабый сигнал"
            assert cards.first.get_attribute("data-rule") == XSS
            assert "sev-weak" in (cards.first.get_attribute("class") or "")
            assert cards.nth(1).locator("h3").inner_text() == "constructor"
            assert cards.nth(1).locator("button.rule-evidence").count() == 0  # no evidence
            # Its evidence button highlights the matching timeline row (and nothing runs).
            cards.first.locator("button.rule-evidence").click()
            assert detail.locator("table.timeline tr.tl-evidence").count() == 1
            assert XSS in detail.locator("table.timeline tr.tl-evidence").inner_text()
            # The second opinion: model id, signals and rationale are text too.
            opinion = detail.locator(".integrity .second-opinion")
            assert XSS in opinion.locator(".opinion-note").inner_text()
            flags = opinion.locator(".opinion-writing .chip.flag")
            assert flags.first.get_attribute("title") == XSS
            assert flags.first.inner_text() == XSS  # an unknown code is shown raw, as text
            assert flags.nth(2).inner_text() == "constructor"  # not Object.prototype's
            assert detail.locator(".integrity-writing").count() >= 1
            assert XSS in detail.locator("table.timeline").inner_text()
            assert "<img src=x" in detail.inner_text()
            assert_clean(page, "the attempt detail")

            # -- test review (item prompts, options, stimulus, asset ids) --------- #
            row.locator('[data-act="view"]').click()
            page.locator("#testViewBody .tv-sec").first.wait_for()
            assert "<img src=x" in page.inner_text("#testViewBody")
            for src in page.eval_on_selector_all(
                "#testViewBody img, #testViewBody audio",
                "els => els.map(e => e.getAttribute('src'))",
            ):
                # An asset id is one encoded path segment: it cannot leave /assets/.
                assert src.startswith("/assets/") and "/" not in src[len("/assets/"):]
                assert "<" not in src and '"' not in src
            assert_clean(page, "the test review")

            # -- printable report (its own page + its own esc) -------------------- #
            with page.expect_popup() as popup_info:
                evil_row.locator('[data-act="print"]').click()
            report = popup_info.value
            report.locator("#page h1").wait_for()
            assert XSS_NAME in report.inner_text("#page h1")
            assert "<img src=x" in report.inner_text("#page")
            assert_clean(report, "the print report")
            assert_clean(page, "the dashboard after the report opened")
            assert page_errors == []

            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed: {exc}")
        raise
