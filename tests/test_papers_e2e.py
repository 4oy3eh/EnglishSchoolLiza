"""E2E: the teacher assigns «Listening only», the student sees only Listening.

Boots the API on a throwaway DB with the seeded A2 demo test (Reading & Writing +
Listening), then in Chromium: the teacher signs in, opens the test, in «Части теста»
unticks «Все части» and ticks only Listening, adds a student (the roster row says
«Только: Listening» with the listening minutes) and switches an existing student to
Listening with the per-row editor; the student picks the name, types the PIN and the
runner shows ONLY the listening part ("Part 1 of 1", the navigator and the review
list nothing else) with a timer of the listening minutes; the served test over the
wire holds no reading section and no answer key. After the student starts, the
row's parts button is locked.

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
from app.content.seed import SEED_TEST_ID, build_sample_test, seed
from app.content.storage import FilesystemStorage
from app.core.db import Base
from contracts.papers import paper_minutes
from tests.pin_helpers import enter_pin, http_json
from tests.test_e2e_full import _free_port, _wait_for_health

sync_api = pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "papers-e2e-pw"
LISTENING_MIN = paper_minutes(build_sample_test())["listening"]


@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[str]:
    db_url = f"sqlite:///{(tmp_path / 'papers.db').as_posix()}"
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


def _teacher_roster(base_url: str, token: str) -> list[dict]:
    import urllib.request

    req = urllib.request.Request(f"{base_url}/admin/tests/{SEED_TEST_ID}/roster")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def test_teacher_assigns_listening_only_and_student_sees_only_listening(
    live_server: str,
) -> None:
    base_url = live_server
    token = http_json(f"{base_url}/admin/login", method="POST", body={"password": PASSWORD})[
        "token"
    ]
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            teacher = browser.new_page()
            teacher.on("dialog", lambda d: d.accept())
            errors: list[str] = []
            teacher.on("pageerror", lambda exc: errors.append(str(exc)))
            teacher.goto(f"{base_url}/web/teacher.html")
            teacher.fill("#password", PASSWORD)
            teacher.click("#loginBtn")
            row = teacher.locator(f'#testsTable tr[data-test-id="{SEED_TEST_ID}"]')
            row.wait_for()
            row.locator('[data-act="open"]').click()
            teacher.locator("#rosterSection").wait_for(state="visible")

            # «Части теста»: untick «Все части», keep only Listening.
            box = teacher.locator("#papersBox")
            box.wait_for(state="visible")
            box.locator("summary").click()
            chooser = box.locator(".papers-chooser")
            chooser.locator("[data-paper-all]").uncheck()
            chooser.locator('[data-paper="reading_writing"]').uncheck()
            assert chooser.locator('[data-paper="listening"]').is_checked()
            assert f"= {LISTENING_MIN} мин" in chooser.inner_text()
            assert "только Listening" in teacher.text_content("#papersSummary")

            teacher.fill("#studentName", "Lena")
            teacher.click("#addStudentBtn")
            lena_row = teacher.locator("#rosterTable tbody tr", has_text="Lena")
            lena_row.wait_for()
            teacher.wait_for_function(
                "() => [...document.querySelectorAll('#rosterTable tbody tr')]"
                ".some(r => r.textContent.includes('Lena') && "
                "r.textContent.includes('Только: Listening'))"
            )
            assert f"({LISTENING_MIN} мин)" in lena_row.locator(".papers-cell").inner_text()

            # Per-row editor: an existing student gets Listening only too.
            roster = _teacher_roster(base_url, token)
            other = next(r for r in roster if r["display_name"] != "Lena")
            other_row = teacher.locator(
                "#rosterTable tbody tr", has_text=other["display_name"]
            ).first
            assert "Все части" in other_row.locator(".papers-cell").inner_text()
            other_row.locator('[data-act="papers"]').click()
            editor = teacher.locator("tr.papers-edit")
            editor.wait_for()
            editor.locator("[data-paper-all]").uncheck()
            editor.locator('[data-paper="reading_writing"]').uncheck()
            editor.locator('[data-act="papers-save"]').click()
            teacher.wait_for_function(
                "(n) => [...document.querySelectorAll('#rosterTable tbody tr')]"
                ".some(r => r.textContent.includes(n) && "
                "r.textContent.includes('Только: Listening'))",
                arg=other["display_name"],
            )

            lena = next(r for r in _teacher_roster(base_url, token) if r["display_name"] == "Lena")
            assert lena["papers"] == ["listening"] and lena["minutes"] == LISTENING_MIN

            # The student: only the listening part, nothing else in the client.
            student = browser.new_page()
            student.on("dialog", lambda d: d.dismiss())
            served: list[str] = []
            student.on(
                "response",
                lambda r: served.append(r.text()) if r.url.endswith("/test") else None,
            )
            student.goto(f"{base_url}/web/exam.html?test={SEED_TEST_ID}")
            student.wait_for_selector(".name-btn")
            enter_pin(student, "Lena", lena["pin"])
            student.wait_for_selector("#runner:not(.hidden)")
            student.wait_for_function(
                "() => (document.querySelector('#progress')?.textContent || '').includes('of 1')"
            )
            assert student.text_content("#partLabel") == "Listening Part 2"
            assert "Part 1 of 1" in student.text_content("#progress")
            nav_text = student.evaluate(
                "() => document.querySelector('#navigator')?.textContent || ''"
            )
            assert "Reading" not in nav_text and "Writing" not in nav_text
            assert served, "the runner did not load the served test"
            test_json = json.loads(served[-1])
            assert [s["skill"] for s in test_json["sections"]] == ["listening"]
            assert "correct" not in served[-1] and "accepted" not in served[-1]
            # Timer = the listening minutes (server deadline).
            state = student.evaluate(
                "async () => { const a = JSON.parse(localStorage.getItem('exam:active'));"
                " const r = await fetch(`/exam/attempts/${a.attemptId}/state`,"
                " {headers: {'X-Attempt-Token': a.token}}); return r.json(); }"
            )
            assert (LISTENING_MIN - 1) * 60 <= state["remaining_seconds"] <= LISTENING_MIN * 60

            # Back on the dashboard, Lena's parts are locked now.
            teacher.reload()
            teacher.locator(
                f'#testsTable tr[data-test-id="{SEED_TEST_ID}"] [data-act="open"]'
            ).click()
            lena_row = teacher.locator("#rosterTable tbody tr", has_text="Lena")
            lena_row.wait_for()
            teacher.wait_for_function(
                "() => [...document.querySelectorAll('#rosterTable tbody tr')]"
                ".some(r => r.textContent.includes('Lena') && "
                "r.querySelector('[data-act=papers]')?.disabled)"
            )
            assert not errors, errors
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed: {exc}")
        raise
