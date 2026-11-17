"""Shared helpers for tests that start attempts through the Phase-14 PIN flow.

Students now type a 4-character PIN after picking their name, and every
`/exam/attempts/{id}/...` call (plus the telemetry ingest) needs the per-attempt
session token `/start` returns (`X-Attempt-Token`). These helpers keep the E2E
files honest without copy-pasting the flow: they read the seeded PIN straight from
the test DB (as the teacher would read it off the dashboard), start for real over
HTTP, and drive the runner's PIN screen in the browser.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

from sqlalchemy import create_engine, select

from app.persistence import models as m

TOKEN_HEADER = "X-Attempt-Token"
ACTIVE_KEY = "exam:active"


def roster_pin(db_url: str, roster_entry_id: str) -> str:
    """The stored PIN of a roster entry (straight from the DB)."""
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            pin = conn.execute(
                select(m.RosterEntryRow.pin).where(m.RosterEntryRow.id == roster_entry_id)
            ).scalar_one()
    finally:
        engine.dispose()
    assert pin, f"roster entry {roster_entry_id} has no PIN"
    return str(pin)


def http_json(
    url: str, *, method: str = "GET", body: Any = None, token: str | None = None
) -> Any:
    """Tiny stdlib JSON client (no requests/httpx dependency in the E2Es)."""
    data = json.dumps(body).encode() if body is not None else (b"" if method != "GET" else None)
    req = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header(TOKEN_HEADER, token)
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def roster_entry(base_url: str, test_id: str, display_name: str) -> dict:
    roster = http_json(f"{base_url}/exam/tests/{test_id}/roster")
    return next(e for e in roster if e["display_name"] == display_name)


def start_with_pin(base_url: str, db_url: str, test_id: str, display_name: str) -> dict:
    """Pick a name + type its PIN over HTTP; returns the `/start` JSON (with token)
    plus `roster_entry_id` / `pin`."""
    entry = roster_entry(base_url, test_id, display_name)
    pin = roster_pin(db_url, entry["roster_entry_id"])
    started = http_json(
        f"{base_url}/exam/roster/{entry['roster_entry_id']}/start",
        method="POST",
        body={"pin": pin},
    )
    assert started.get("token"), started
    return {**started, "roster_entry_id": entry["roster_entry_id"], "pin": pin}


def resume_script(
    test_id: str, attempt_id: str, roster_entry_id: str, token: str, name: str = "Student"
) -> str:
    """Init script: make exam.js resume `attempt_id` with its session token."""
    active = json.dumps(
        {
            "testId": test_id,
            "attemptId": attempt_id,
            "rosterEntryId": roster_entry_id,
            "name": name,
            "token": token,
        }
    )
    return f"localStorage.setItem({json.dumps(ACTIVE_KEY)}, {json.dumps(active)});"


def enter_pin(page: Any, display_name: str, pin: str) -> None:
    """In the runner: click the student's name, type the PIN, press Start."""
    page.locator(".name-btn", has_text=display_name).click()
    page.wait_for_selector("#pinScreen:not(.hidden)")
    page.fill("#pinInput", pin)
    page.click("#pinStartBtn")
