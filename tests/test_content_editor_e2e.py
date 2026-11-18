"""Content editor end-to-end in a real browser (Phase 13).

uvicorn on a throwaway sqlite DB (`SKIP_STARTUP_SWEEP=1`, `ENVIRONMENT=dev`, a real
`TEACHER_PASSWORD`). The teacher signs in, imports a small Test JSON + one image
through the dashboard, opens the editor, changes one gap_fill accepted answer and one
option text, previews the test as a student (the preview response carries no key),
approves the draft in the review queue (the only publish path), and the student
runner `exam.html?test=<id>` then shows the edited option text while none of its
network responses contains a `correct` field or the gap_fill key (golden rule #1).
Once a student has started, the editor shows the "locked" banner (no unpublish
button), a key change is refused even with the typo checkbox ("Создать
черновик-копию"), the copy opens as a free draft, and re-importing over the
approved copy with «заменить» tells the teacher it was unpublished.

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

import app.persistence.models  # noqa: F401  (register tables on Base.metadata)
from app.core.db import Base
from tests.pin_helpers import enter_pin

sync_api = pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "e2e-editor-pw"
TEST_ID = "e2e-edit"
PNG = b"\x89PNG\r\n\x1a\n" + bytes.fromhex(
    "0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8cfc0f01f0005000201a2dd6e840000000049454e44ae426082"
)

TEST_JSON = {
    "id": TEST_ID,
    "title": "E2E editor test",
    "level": "A2_KEY",
    "status": "published",  # ignored: an import is always a draft
    "duration_minutes": 20,
    "sections": [
        {
            "id": f"{TEST_ID}-sec1",
            "title": "Reading Part 1",
            "skill": "reading",
            "stimulus": {"kind": "passage_text", "text": "The museum opens at nine."},
            "items": [
                {
                    "item_type": "single_choice",
                    "id": f"{TEST_ID}-q1",
                    "prompt": "When does the museum open?",
                    "options": [
                        {"kind": "text", "key": "A", "text": "At nine"},
                        {"kind": "text", "key": "B", "text": "Old option text"},
                        {"kind": "text", "key": "C", "text": "At noon"},
                    ],
                    "correct": "A",
                }
            ],
        },
        {
            "id": f"{TEST_ID}-sec2",
            "title": "Reading Part 2",
            "skill": "reading",
            "stimulus": {"kind": "image_set", "asset_ids": ["e2e-sign.png"]},
            "items": [
                {
                    "item_type": "gap_fill",
                    "id": f"{TEST_ID}-q2",
                    "prompt": "The sign shows a ____.",
                    "accepted": ["zebra"],
                    "rejected": ["zebre"],
                }
            ],
        },
    ],
}


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


def _call(url: str, *, method: str = "GET", body: object = None, token: str | None = None):  # noqa: ANN202
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=5) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[str]:
    db_path = tmp_path / "editor.db"
    db_url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": db_url,
        "ASSETS_DIR": str(tmp_path / "assets"),
        "LOG_LEVEL": "INFO",
        "TEACHER_PASSWORD": PASSWORD,
        "ENVIRONMENT": "dev",
        "SKIP_STARTUP_SWEEP": "1",
        "GEMINI_API_KEY": "",
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


def _bodies(responses: list) -> list[str]:
    out = []
    for r in responses:
        try:
            out.append(r.text())
        except PlaywrightError:  # redirects / aborted / no body
            continue
    return out


def test_import_edit_preview_approve_student_sees_edit(live_server: str) -> None:
    base = live_server
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.on("dialog", lambda d: d.accept())
            page_errors: list[str] = []
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            preview_responses: list = []
            page.on(
                "response",
                lambda r: preview_responses.append(r) if "/preview" in r.url else None,
            )

            page.goto(f"{base}/web/teacher.html")
            page.fill("#password", PASSWORD)
            page.click("#loginBtn")
            page.locator("#app").wait_for(state="visible")

            # Import the JSON + its one image through the dashboard.
            page.click("#importToggleBtn")
            page.set_input_files(
                "#importJson",
                {
                    "name": "e2e.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(TEST_JSON).encode(),
                },
            )
            page.set_input_files(
                "#importAssets",
                [{"name": "e2e-sign.png", "mimeType": "image/png", "buffer": PNG}],
            )
            page.click("#importBtn")
            page.locator(".toast.ok", has_text="импортирован как черновик").wait_for()
            # It is a draft waiting in the review queue, not published.
            page.locator("#reviewTable tbody tr", has_text="E2E editor test").wait_for()
            row = page.locator(f'#testsTable tr[data-test-id="{TEST_ID}"]')
            assert "черновик" in row.inner_text()

            # Open the editor and expand every section.
            row.locator('[data-act="edit"]').click()
            page.locator("#editorSection").wait_for(state="visible")
            page.locator(f'details.ed-sec[data-sid="{TEST_ID}-sec2"]').wait_for()
            page.eval_on_selector_all("details.ed-sec", "els => els.forEach(d => d.open = true)")

            # 1) gap_fill accepted answers.
            gap = page.locator(f'.ed-item[data-item-id="{TEST_ID}-q2"]')
            gap.locator('textarea[data-f="accepted"]').fill("zebra\nzebras")
            assert gap.locator('textarea[data-f="rejected"]').input_value() == "zebre"
            gap.locator('textarea[data-f="rejected"]').fill("zebre, zebrra")
            gap.locator('[data-act="save-item"]').click()
            page.locator(".toast.ok", has_text="Вопрос сохранён").first.wait_for()

            # 2) one option's text.
            choice = page.locator(f'.ed-item[data-item-id="{TEST_ID}-q1"]')
            choice.locator('input[data-f="opt-text"]').nth(1).fill("Edited option text")
            choice.locator('[data-act="save-item"]').click()
            page.wait_for_function(
                "() => [...document.querySelectorAll('input[data-f=\"opt-text\"]')]"
                ".some(i => i.value === 'Edited option text')"
            )
            page.locator(".toast.ok", has_text="Вопрос сохранён").first.wait_for()

            # An invalid save shows the Russian error inline (and stores nothing).
            gap.locator('textarea[data-f="accepted"]').fill("  ")
            gap.locator('[data-act="save-item"]').click()
            gap.locator('[data-err="item"]', has_text="принимаемые ответы").wait_for()

            # Preview as a student: the edited text, never a key.
            page.click("#edPreviewBtn")
            page.locator("#edPreview", has_text="Edited option text").wait_for()
            preview_text = page.inner_text("#edPreview")
            assert "zebra" not in preview_text
            bodies = _bodies(preview_responses)
            assert bodies, "the preview request was not observed"
            for body in bodies:
                assert '"correct"' not in body and "zebra" not in body

            # Approve in the review queue (the only publish path).
            page.locator("#reviewTable tbody tr", has_text="E2E editor test").locator(
                '[data-act="approve"]'
            ).click()
            page.locator(".toast.ok", has_text="опубликован").wait_for()
            assert page_errors == []

            # The edits are stored (teacher API, which may show the key).
            token = _call(f"{base}/admin/login", method="POST", body={"password": PASSWORD})[
                "token"
            ]
            stored = _call(f"{base}/admin/tests/{TEST_ID}", token=token)
            assert stored["status"] == "published"
            assert stored["sections"][1]["items"][0]["accepted"] == ["zebra", "zebras"]
            assert stored["sections"][1]["items"][0]["rejected"] == ["zebre", "zebrra"]
            assert stored["sections"][0]["items"][0]["options"][1]["text"] == "Edited option text"
            added = _call(
                f"{base}/admin/tests/{TEST_ID}/roster",
                method="POST",
                body={"display_name": "Student One"},
                token=token,
            )
            pin = added["pin"]  # what the teacher hands the student (Phase 14)

            # The student runner shows the edit; no response carries the key.
            ctx = browser.new_context()
            student = ctx.new_page()
            student.on("dialog", lambda d: d.accept())
            exam_responses: list = []
            student.on("response", lambda r: exam_responses.append(r))
            student.goto(f"{base}/web/exam.html?test={TEST_ID}")
            enter_pin(student, "Student One", pin)
            student.wait_for_function(
                "() => document.body.innerText.includes('Edited option text')", timeout=15000
            )
            assert "Old option text" not in student.inner_text("body")
            api_bodies = _bodies([r for r in exam_responses if "/exam/" in r.url])
            assert api_bodies
            for body in api_bodies:
                assert '"correct"' not in body
                assert "zebra" not in body
            roster_bodies = _bodies([r for r in exam_responses if r.url.endswith("/roster")])
            assert roster_bodies and all(pin not in b for b in roster_bodies)

            # Back in the editor: the test now has an attempt -> locked.
            page.locator(f'#testsTable tr[data-test-id="{TEST_ID}"]').locator(
                '[data-act="edit"]'
            ).click()
            banner = page.locator("#edBanner")
            banner.locator(".notice", has_text="уже есть работы учеников (1)").wait_for()
            assert "Создать черновик-копию" in banner.inner_text()
            assert banner.locator('[data-act="unpub"]').count() == 0
            banner.locator('[data-act="force"]').check()  # confirm() is accepted
            page.eval_on_selector_all("details.ed-sec", "els => els.forEach(d => d.open = true)")
            gap = page.locator(f'.ed-item[data-item-id="{TEST_ID}-q2"]')
            gap.locator('textarea[data-f="accepted"]').fill("zebra\nhorse")
            gap.locator('[data-act="save-item"]').click()
            gap.locator('[data-err="item"]', has_text="Создать черновик-копию").wait_for()
            stored = _call(f"{base}/admin/tests/{TEST_ID}", token=token)
            assert stored["sections"][1]["items"][0]["accepted"] == ["zebra", "zebras"]

            # The way out: a draft copy, freely editable.
            banner.locator('[data-act="copy"]').click()
            page.locator(".toast.ok", has_text=f"{TEST_ID}-v2").wait_for()
            page.locator("#edBanner .notice", has_text="Черновик: можно менять всё").wait_for()

            # Approve the copy, then re-import over it with «заменить»: unpublished.
            # (Dismiss the sticky error toast from the refused save first.)
            page.locator(".toast.error button").first.click()
            page.locator("#reviewTable tbody tr", has_text="E2E editor test").locator(
                '[data-act="approve"]'
            ).click()
            page.locator(".toast.ok", has_text="опубликован").last.wait_for()
            if not page.is_visible("#importJson"):
                page.click("#importToggleBtn")
            page.set_input_files(
                "#importJson",
                {
                    "name": "e2e.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(TEST_JSON).encode(),
                },
            )
            page.set_input_files(
                "#importAssets",
                [{"name": "e2e-sign.png", "mimeType": "image/png", "buffer": PNG}],
            )
            page.fill("#importNewId", f"{TEST_ID}-v2")
            page.check("#importReplace")
            page.click("#importBtn")
            page.locator(".toast", has_text="снят с публикации и ждёт одобрения").wait_for()
            copy_ = _call(f"{base}/admin/tests/{TEST_ID}-v2", token=token)
            assert copy_["status"] == "draft"
            assert page_errors == []
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(f"Chromium not installed: {exc}")
        raise
