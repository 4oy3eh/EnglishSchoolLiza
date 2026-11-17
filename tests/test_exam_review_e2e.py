"""Student runner: review-before-submit panel, timer warnings, basic accessibility.

Real browser (Playwright/Chromium) against the live API seeded with the demo test
(the `live_server` fixture of `test_e2e_full`). Skips cleanly without Chromium.

* The review panel lists every part with its answered / unanswered questions, the
  save state and the time left; "Go to question" jumps back into the runner; its
  Submit finalizes the attempt (same confirms as before).
* Display-only timer warnings at 10 and 2 minutes left (fake page clock — the
  server deadline is untouched), announced through an assertive live region.
* The deadline auto-submit does not wait for the review panel (rule #3).
"""

from __future__ import annotations

import re

import pytest

from tests import test_e2e_full as _full
from tests.test_e2e_full import QIDS, _get_json, _resume_script, _start_attempt_entry

live_server = _full.live_server  # the same seeded live API (a pytest fixture)

sync_api = pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402

from app.content.seed import SEED_TEST_ID  # noqa: E402

_STATUS_JS = "() => document.querySelector('#saveStatus').textContent"
_UNANSWERED_JS = "() => document.querySelector('#reviewCount').dataset.unanswered"
_SAVED_ALL_JS = (
    "() => document.querySelector('#reviewSave').textContent"
    ".includes('All your answers are saved')"
)
_WARN2_JS = (
    "() => document.querySelector('#timeWarnText').textContent.includes('2 minutes left')"
)


def _cls(page: object, selector: str) -> str:
    return page.get_attribute(selector, "class") or ""  # type: ignore[attr-defined]


def _skip_without_chromium(exc: PlaywrightError) -> None:
    if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
        pytest.skip(f"Chromium not installed: {exc}")
    raise exc


def _open(p: object, base_url: str, attempt_id: str, entry_id: str, *, clock: bool = False):
    browser = p.chromium.launch(headless=True)  # type: ignore[attr-defined]
    context = browser.new_context()
    context.add_init_script(_resume_script(attempt_id, entry_id))
    page = context.new_page()
    if clock:
        page.clock.install()  # time still flows; fast_forward jumps Date.now + timers
    page.goto(f"{base_url}/web/exam.html?test={SEED_TEST_ID}")
    page.wait_for_selector("input[name='opt-seed-q-read']")
    return browser, page


def test_review_lists_unanswered_jumps_and_submits(live_server: tuple[str, str]) -> None:
    base_url, db_url = live_server
    attempt_id, entry_id = _start_attempt_entry(base_url, "Bao", db_url)
    dialogs: list[str] = []
    total = len(QIDS)
    try:
        with sync_api.sync_playwright() as p:
            browser, page = _open(p, base_url, attempt_id, entry_id)
            page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
            assert page.get_attribute("html", "lang") == "en"

            page.locator("input[name='opt-seed-q-read'][value='0']").check()
            page.wait_for_function(f"() => ({_STATUS_JS})() === 'Saved'", timeout=8000)

            # Open the review from the navigator (any part).
            page.click("#navReviewBtn")
            page.wait_for_selector("#review:not(.hidden)")
            assert page.is_hidden("#runner")
            assert page.evaluate(_UNANSWERED_JS) == str(total - 1)
            assert f"1 of {total} answered" in (page.text_content("#reviewCount") or "")
            assert re.fullmatch(r"\d+:\d\d", page.text_content("#reviewTime") or "")
            assert "All your answers are saved" in (page.text_content("#reviewSave") or "")
            # One block per part; the answered question is marked done, others todo.
            assert page.locator(".review-part").count() == 4
            assert "done" in _cls(page, ".review-q[data-item='seed-q-read']")
            assert "todo" in _cls(page, ".review-q[data-item='seed-q-match']")
            assert page.locator(".review-go[data-item='seed-q-read']").count() == 0

            # "Go to question" lands on the matching question in the runner.
            page.click(".review-go[data-item='seed-q-match']")
            page.wait_for_selector("#runner:not(.hidden)")
            assert page.is_hidden("#review")
            assert page.is_visible("#q-seed-q-match")
            page.locator("#q-seed-q-match label.opt-letter").filter(
                has=page.locator("input[value='A']")
            ).click()
            page.wait_for_function(f"() => ({_STATUS_JS})() === 'Saved'", timeout=8000)

            # Back on the last part, "Review & submit" opens the panel again.
            page.click(".navsq[data-item='seed-q-write']")
            page.click("#reviewBtn")
            page.wait_for_selector("#review:not(.hidden)")
            assert page.evaluate(_UNANSWERED_JS) == str(total - 2)
            assert "done" in _cls(page, ".review-q[data-item='seed-q-match']")

            page.click("#submitBtn")
            page.wait_for_selector("#done:not(.hidden)", timeout=15000)
            assert (page.text_content("#doneTitle") or "") == "Test submitted"
            assert any("unanswered question" in d for d in dialogs), dialogs
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        _skip_without_chromium(exc)

    state = _get_json(f"{base_url}/exam/attempts/{attempt_id}/state")
    assert state["status"] == "submitted"  # type: ignore[index]
    saved = _get_json(f"{base_url}/exam/attempts/{attempt_id}/answers")
    assert saved == {"seed-q-read": 0, "seed-q-match": "A"}


def test_review_shows_unsaved_answers(live_server: tuple[str, str]) -> None:
    base_url, db_url = live_server
    attempt_id, entry_id = _start_attempt_entry(base_url, "Dmitri", db_url)
    pattern = "**/exam/attempts/*/answers/*"
    try:
        with sync_api.sync_playwright() as p:
            browser, page = _open(p, base_url, attempt_id, entry_id)
            page.route(pattern, lambda route: route.abort("failed"))
            page.locator("input[name='opt-seed-q-read'][value='1']").check()
            page.click("#navReviewBtn")
            page.wait_for_function(
                "() => document.querySelector('#reviewSave').textContent.includes('not saved yet')",
                timeout=8000,
            )
            assert "warn" in (page.get_attribute("#reviewSave", "class") or "")
            assert page.get_attribute("#reviewSave", "aria-live") == "polite"
            page.unroute(pattern)
            page.wait_for_function(_SAVED_ALL_JS, timeout=15000)
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        _skip_without_chromium(exc)


def test_review_keeps_keyboard_focus_across_save_ticks(live_server: tuple[str, str]) -> None:
    """renderSaveStatus -> renderReview runs on every save tick: unchanged markup is
    not rebuilt (same focused node), changed markup restores focus by data-item."""
    base_url, db_url = live_server
    attempt_id, entry_id = _start_attempt_entry(base_url, "Carlos", db_url)
    try:
        with sync_api.sync_playwright() as p:
            browser, page = _open(p, base_url, attempt_id, entry_id)
            page.click("#navReviewBtn")
            page.wait_for_selector("#review:not(.hidden)")
            page.wait_for_function(_SAVED_ALL_JS, timeout=8000)
            page.focus(".review-q[data-item='seed-q-match']")
            same_node = page.evaluate(
                """() => {
                  const el = document.activeElement;
                  el.__marker = 1;
                  for (let i = 0; i < 5; i++) renderSaveStatus();
                  return document.activeElement === el && el.isConnected;
                }"""
            )
            assert same_node is True
            restored = page.evaluate(
                """() => {
                  const before = document.activeElement;
                  pending['seed-q-match'] = 'A';  // markup changes: "(saving…)"
                  renderReview();
                  const after = document.activeElement;
                  delete pending['seed-q-match'];
                  renderReview();
                  const last = document.activeElement;
                  return [after !== before, after.dataset.item, last.dataset.item,
                          last.classList.contains('review-q')];
                }"""
            )
            assert restored == [True, "seed-q-match", "seed-q-match", True]
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        _skip_without_chromium(exc)


def test_timer_warnings_and_deadline_autosubmit_skip_review(
    live_server: tuple[str, str],
) -> None:
    base_url, db_url = live_server
    attempt_id, entry_id = _start_attempt_entry(base_url, "Anna", db_url)
    try:
        with sync_api.sync_playwright() as p:
            browser, page = _open(p, base_url, attempt_id, entry_id, clock=True)
            assert page.is_hidden("#timeWarn")
            assert page.get_attribute("#timeWarnLive", "aria-live") == "assertive"
            assert page.get_attribute("#saveStatus", "aria-live") == "polite"

            # Basic a11y: labelled controls, alt text on every picture.
            labelled = page.evaluate(
                """() => [...document.querySelectorAll('#sectionBody [aria-labelledby]')]
                    .every(el => el.getAttribute('aria-labelledby').split(' ')
                        .every(id => document.getElementById(id)))"""
            )
            assert labelled
            page.click(".navsq[data-item='seed-q-listen']")
            assert page.evaluate(
                "() => [...document.querySelectorAll('#sectionBody img')].every(i => i.alt.trim())"
            )
            page.click(".navsq[data-item='seed-q-gap']")
            assert page.get_attribute("#ctl-seed-q-gap", "aria-labelledby")

            # 30-min test: jump to ~9.5 min left -> the 10-minute warning.
            page.clock.fast_forward("20:30")
            page.wait_for_selector("#timeWarn:not(.hidden)", timeout=5000)
            assert "10 minutes left" in (page.text_content("#timeWarnText") or "")
            assert "10 minutes left" in (page.text_content("#timeWarnLive") or "")
            assert "crit" not in (page.get_attribute("#timeWarn", "class") or "")

            # ~1.5 min left -> the 2-minute warning (critical style).
            page.clock.fast_forward("08:00")
            page.wait_for_function(_WARN2_JS, timeout=5000)
            assert "crit" in (page.get_attribute("#timeWarn", "class") or "")

            # Review open when the (client) deadline passes: auto-submit anyway.
            page.click("#navReviewBtn")
            page.wait_for_selector("#review:not(.hidden)")
            page.clock.fast_forward("02:00")
            page.wait_for_selector("#done:not(.hidden)", timeout=15000)
            assert page.is_hidden("#review")
            browser.close()
    except PlaywrightError as exc:  # pragma: no cover - environment-dependent
        _skip_without_chromium(exc)

    # The server (real clock, still inside the time) accepted the auto-submit.
    state = _get_json(f"{base_url}/exam/attempts/{attempt_id}/state")
    assert state["status"] == "submitted"  # type: ignore[index]
