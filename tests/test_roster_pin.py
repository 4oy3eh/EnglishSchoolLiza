"""Phase 14: classroom PINs + per-attempt session tokens.

* PIN generation: unambiguous alphabet, 4 characters, `secrets`, unique within a
  test, assigned on every roster insert (teacher add, bulk add, demo seed).
* `normalize_pin` / `pin_matches` (case-insensitive, spaces ignored, constant-time).
* `PinRateLimiter`: per entry (5 wrong -> 5-min lock), a class of 30 on one IP never
  throttled, per-IP spray throttle, per-test monitor, clear, atomic, no leaked keys.
* The Alembic migration on a scratch DB: backfill, unique per test, down + up again.
* Teacher side: PIN in the roster view, "new PIN" (old PIN refused, open sessions
  kept, lock lifted), no PIN in the results CSV, teacher reset drops the sessions.

The HTTP student flow (start with / without / wrong PIN, lockout over HTTP, token
on every attempt endpoint) lives in `tests/test_delivery_api.py`; telemetry's token
check in `tests/test_telemetry.py`.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.content import FilesystemStorage, RosterService
from app.content.roster import is_well_formed_pin, normalize_pin, pin_matches
from app.content.seed import SEED_ROSTER, seed
from app.delivery import PinRateLimiter
from app.persistence import models as m
from app.persistence import repository as repo_mod
from app.persistence.repository import AttemptRepository, ContentRepository, new_pin
from contracts import RosterEntry
from contracts.runtime import PIN_ALPHABET, PIN_LENGTH, PIN_PATTERN
from tests.test_admin_dashboard import _headers, _seed, _test, client  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parent.parent
PIN_RE = re.compile(PIN_PATTERN)


# --------------------------------------------------------------------------- #
# Generation.
# --------------------------------------------------------------------------- #
def test_pin_alphabet_has_no_lookalikes() -> None:
    assert PIN_LENGTH == 4
    assert len(PIN_ALPHABET) == len(set(PIN_ALPHABET)) == 31
    assert not set("0O1IL") & set(PIN_ALPHABET)
    for ch in PIN_ALPHABET:  # the contract pattern accepts exactly this alphabet
        assert PIN_RE.match(ch * 4)
    for ch in "0O1IL":
        assert not PIN_RE.match(ch * 4)


def test_new_pin_shape_and_spread() -> None:
    pins = [new_pin() for _ in range(2000)]
    assert all(PIN_RE.match(p) for p in pins)
    assert len(set(pins)) > 1900  # 923 521 combinations: collisions are rare


def test_new_pin_skips_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    draws = iter("AAAA" + "BBBB")
    monkeypatch.setattr(repo_mod.secrets, "choice", lambda _alphabet: next(draws))
    assert new_pin({"AAAA"}) == "BBBB"


def test_repository_assigns_unique_pins_per_test(session: Session) -> None:
    ContentRepository(session).add_test(_test("t1"))
    ContentRepository(session).add_test(_test("t2"))
    repo = AttemptRepository(session)
    for i in range(300):
        entry = RosterEntry(id=f"e{i}", test_id="t1", display_name=f"S{i}")
        repo.add_roster_entry(entry)
        assert entry.pin and PIN_RE.match(entry.pin)  # the caller's contract carries it
    pins = [e.pin for e in repo.list_roster_entries("t1")]
    assert len(pins) == 300 == len(set(pins))
    assert repo.get_roster_entry("e7").pin == next(
        e.pin for e in repo.list_roster_entries("t1") if e.id == "e7"
    )
    # An explicit PIN is kept; a duplicate inside the test is refused, but the same
    # PIN on ANOTHER test is fine (unique per test, not globally).
    taken = repo.get_roster_entry("e0").pin
    with pytest.raises(ValueError):
        repo.add_roster_entry(RosterEntry(id="dup", test_id="t1", display_name="D", pin=taken))
    repo.add_roster_entry(RosterEntry(id="other", test_id="t2", display_name="O", pin=taken))
    assert repo.get_roster_entry("other").pin == taken


def test_unique_index_on_test_and_pin(session: Session) -> None:
    ContentRepository(session).add_test(_test("t1"))
    session.add(m.RosterEntryRow(id="a", test_id="t1", display_name="A", pin="ABCD"))
    session.add(m.RosterEntryRow(id="b", test_id="t1", display_name="B", pin="ABCD"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_contract_rejects_a_malformed_pin() -> None:
    with pytest.raises(ValueError):
        RosterEntry(id="x", test_id="t", display_name="X", pin="ab0O")


def test_normalize_and_match() -> None:
    assert normalize_pin("  ab 3d\n") == "AB3D"
    assert normalize_pin(None) == ""
    assert is_well_formed_pin("AB3D")
    assert not is_well_formed_pin("AB3")
    assert not is_well_formed_pin("AB0D")  # 0 is not in the alphabet
    assert pin_matches("ab3d", "AB3D")
    assert pin_matches(" A B 3 D ", "AB3D")
    assert not pin_matches("AB3E", "AB3D")
    assert not pin_matches("AB3D", None)


def test_roster_service_regenerate(session: Session, caplog: pytest.LogCaptureFixture) -> None:
    ContentRepository(session).add_test(_test("t1"))
    roster = RosterService(AttemptRepository(session))
    with caplog.at_level(logging.INFO):
        entry = roster.add_student("t1", "Anna")
        old = entry.pin
        new = roster.regenerate_pin(entry.id)
    assert old and new and old != new and PIN_RE.match(new)
    assert AttemptRepository(session).get_roster_entry(entry.id).pin == new
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert old not in text and new not in text  # PINs are never logged


def test_demo_seed_gives_every_student_a_pin(session: Session, tmp_path: Path) -> None:
    test_id = seed(session, FilesystemStorage(tmp_path))
    entries = AttemptRepository(session).list_roster_entries(test_id)
    assert {e.display_name for e in entries} == set(SEED_ROSTER)
    assert all(e.pin and PIN_RE.match(e.pin) for e in entries)
    assert len({e.pin for e in entries}) == len(entries)


# --------------------------------------------------------------------------- #
# Wrong-PIN limiter.
# --------------------------------------------------------------------------- #
def test_limiter_locks_an_entry_after_five_wrong() -> None:
    lim = PinRateLimiter(entry_max_failures=5, lock_seconds=300)
    for left in (4, 3, 2, 1):
        d = lim.attempt("e1", "ip", lambda: False, now=0.0)
        assert not d.ok and d.tries_left == left and d.retry_after == 0
    fifth = lim.attempt("e1", "ip", lambda: False, now=1.0)
    assert fifth.just_locked and fifth.scope == "entry" and fifth.retry_after == 300
    checked = []
    locked = lim.attempt("e1", "ip", lambda: checked.append(1) or True, now=100.0)
    assert not locked.ok and locked.scope == "entry" and 0 < locked.retry_after <= 201
    assert checked == []  # while locked the PIN is not even compared
    assert lim.attempt("e2", "ip", lambda: True, now=100.0).ok  # other entries unaffected
    assert lim.attempt("e1", "ip", lambda: True, now=302.0).ok  # lock over


def _class_start(seed: int, *, spread: float, n: int = 30) -> list:
    """A realistic class of `n` children on ONE NAT address typing PINs from cards.

    Each child starts within `spread` seconds, mistypes 1-2 times (3-20 s per retype)
    and then types the right PIN; 4 of them first try their PIN on a classmate's
    name (each on a different classmate), 2 give up after 3 typos and get a new PIN
    from the teacher 3 minutes later. Returns every right-PIN decision.
    """
    import heapq
    import random

    rng = random.Random(seed)
    lim = PinRateLimiter()
    victims = rng.sample(range(6, n), 4)
    events: list = []
    for k in range(n):
        seq = ["typo"] * rng.choice((1, 2)) + ["ok"]
        if k < 4:
            seq = [f"name:{victims[k]}", *seq]
        elif k < 6:
            seq = ["typo", "typo", "typo", "teacher", "ok"]
        heapq.heappush(events, (rng.uniform(0, spread), k, seq))
    right = []
    while events:
        t, k, seq = heapq.heappop(events)
        step, rest = seq[0], seq[1:]
        if step == "teacher":
            lim.reset_entry(f"e{k}")
            heapq.heappush(events, (t + 180, k, rest))
            continue
        target = step.split(":")[1] if step.startswith("name:") else k
        decision = lim.attempt(f"e{target}", "school-nat", lambda s=step: s == "ok", now=t)
        assert not decision.ip_throttled, (seed, t, k)
        if step == "ok":
            right.append(decision)
        if rest:
            heapq.heappush(events, (t + rng.uniform(3, 20), k, rest))
    return right


@pytest.mark.parametrize("spread", [20.0, 60.0, 180.0])
def test_class_of_30_on_one_ip_with_typos_never_blocks_a_right_pin(spread: float) -> None:
    for run in range(40):
        right = _class_start(run, spread=spread)
        assert len(right) == 30
        assert all(d.ok for d in right), run


def test_limiter_throttles_an_ip_spraying_many_students(
    caplog: pytest.LogCaptureFixture,
) -> None:
    lim = PinRateLimiter()  # 5 students unresolved > 120 s -> throttle 30 s, doubling
    for i in range(6):  # one guess on each of six names: no throttle yet (grace)
        assert lim.attempt(f"e{i}", "1.2.3.4", lambda: False, now=float(i)).retry_after == 0
    with caplog.at_level(logging.WARNING, logger="app.delivery.access"):
        strike = lim.attempt("e0", "1.2.3.4", lambda: False, now=130.0)
    assert strike.ip_throttled and strike.scope == "ip" and strike.retry_after == 30
    assert not strike.entry_locked  # e0 has only 2 wrong PINs
    throttle_logs = [r for r in caplog.records if "PIN THROTTLE ip=1.2.3.4" in r.getMessage()]
    assert len(throttle_logs) == 1 and throttle_logs[0].levelno == logging.WARNING
    # While throttled every PIN from that IP is refused unchecked, the right one too.
    checked: list[int] = []
    refused = lim.attempt("e9", "1.2.3.4", lambda: checked.append(1) or True, now=140.0)
    assert refused.scope == "ip" and 0 < refused.retry_after <= 20 and checked == []
    assert lim.attempt("e9", "5.6.7.8", lambda: True, now=140.0).ok  # another IP is fine
    assert lim.attempt("e9", "1.2.3.4", lambda: True, now=161.0).ok  # throttle over
    # It needs fresh unresolved students to strike again; the second strike doubles.
    for i in range(10, 15):
        lim.attempt(f"e{i}", "1.2.3.4", lambda: False, now=170.0)
    again = lim.attempt("e15", "1.2.3.4", lambda: False, now=300.0)
    assert again.ip_throttled and again.retry_after == 60


def test_limiter_right_pin_resolves_and_forgives_once_per_student() -> None:
    lim = PinRateLimiter()
    for i in range(4):  # four names fail from one IP...
        lim.attempt(f"e{i}", "nat", lambda: False, now=0.0)
    lim.attempt("e0", "nat", lambda: True, now=5.0)  # e0 resolved + forgives e1
    lim.attempt("e0", "nat", lambda: True, now=6.0)  # once per student: nothing more
    assert set(lim._ip_unresolved["nat"]) == {"e2", "e3"}
    # A pupil who knows their own PIN cannot launder spraying: still throttled.
    for i in range(10, 14):
        lim.attempt(f"e{i}", "nat", lambda: False, now=7.0)
    lim.attempt("own", "nat", lambda: True, now=8.0)  # forgives one (e2)
    lim.attempt("own", "nat", lambda: True, now=9.0)  # ...and only one
    assert len(lim._ip_unresolved["nat"]) == 5  # e3 + e10..e13
    assert lim.attempt("e20", "nat", lambda: False, now=200.0).ip_throttled


def test_limiter_per_test_monitor_alerts_once_and_never_locks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    lim = PinRateLimiter()  # alert at 40 wrong PINs / 10 min per test
    with caplog.at_level(logging.WARNING, logger="app.delivery.access"):
        for i in range(45):  # 45 different IPs: nothing locks, only the monitor counts
            d = lim.attempt(f"e{i % 15}", f"10.0.0.{i}", lambda: False, test_id="T", now=1.0)
            assert d.retry_after == 0
    alerts = [r for r in caplog.records if "PIN ALERT test=T" in r.getMessage()]
    assert len(alerts) == 1 and alerts[0].levelno == logging.WARNING
    act = lim.test_activity("T", [f"e{i}" for i in range(15)], now=2.0)
    assert act.alert and act.wrong_pins == 45 and act.threshold == 40
    assert act.per_entry["e0"] == 3 and act.locked == {}
    assert lim.test_activity("other", [], now=2.0).wrong_pins == 0
    assert lim.attempt("e0", "10.0.0.99", lambda: True, test_id="T", now=3.0).ok
    later = lim.test_activity("T", [], now=700.0)
    assert not later.alert and later.wrong_pins == 0


def test_limiter_clear_test_lifts_student_and_ip_locks() -> None:
    lim = PinRateLimiter()
    for _ in range(5):
        lim.attempt("victim", "nat", lambda: False, test_id="T", now=0.0)
    for i in range(5):
        lim.attempt(f"s{i}", "nat", lambda: False, test_id="T", now=0.0)
    assert lim.attempt("s9", "nat", lambda: False, test_id="T", now=130.0).ip_throttled
    act = lim.test_activity("T", ["victim", "s0"], now=131.0)
    assert act.locked.keys() == {"victim"} and act.throttled_ips == 1
    assert lim.clear_test("T", ["victim", "s0"], now=131.0) == (1, 1)
    assert lim.attempt("victim", "nat", lambda: True, test_id="T", now=132.0).ok
    assert lim.test_activity("T", ["victim"], now=132.0).locked == {}


def test_limiter_right_pin_clears_the_entry_and_reset_entry_lifts_a_lock() -> None:
    lim = PinRateLimiter()
    for _ in range(4):
        lim.attempt("e1", "ip", lambda: False, now=0.0)
    assert lim.attempt("e1", "ip", lambda: True, now=0.0).ok
    assert lim.attempt("e1", "ip", lambda: False, now=0.0).tries_left == 4
    for _ in range(4):
        lim.attempt("e1", "ip", lambda: False, now=0.0)
    assert lim.attempt("e1", "ip", lambda: True, now=0.0).retry_after > 0
    lim.reset_entry("e1")
    assert lim.attempt("e1", "ip", lambda: True, now=0.0).ok


def test_limiter_is_atomic_under_threads() -> None:
    lim = PinRateLimiter(entry_max_failures=5)
    compared = []
    barrier = threading.Barrier(20)

    def guess() -> None:
        barrier.wait()
        lim.attempt("e1", "ip", lambda: compared.append(1) or False)

    threads = [threading.Thread(target=guess) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(compared) == 5  # exactly the budget, never more


def test_limiter_drops_stale_keys() -> None:
    lim = PinRateLimiter(window_seconds=300, lock_seconds=300)
    lim.attempt("e1", "ip", lambda: False, now=0.0)
    assert lim.tracked_keys() == 2
    lim.attempt("e2", "ip2", lambda: True, now=1000.0)
    assert lim.tracked_keys() == 0


# --------------------------------------------------------------------------- #
# Migration f6a7b8c9d0e1 on a scratch DB.
# --------------------------------------------------------------------------- #
def _alembic(db: Path, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db.as_posix()}"}
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        timeout=120,
    )


def _columns(db: Path, table: str) -> set[str]:
    with sqlite3.connect(db) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_migration_backfills_pins_and_round_trips(tmp_path: Path) -> None:
    db = tmp_path / "mig.db"
    _alembic(db, "upgrade", "e5f6a7b8c9d0")  # the schema before PINs
    with sqlite3.connect(db) as conn:
        for t in ("t1", "t2"):
            conn.execute(
                "INSERT INTO tests (id, title, level, status, duration_minutes) "
                "VALUES (?, 'T', 'A2_KEY', 'published', 30)",
                (t,),
            )
            for i in range(40):
                conn.execute(
                    "INSERT INTO roster_entries (id, test_id, display_name, status) "
                    "VALUES (?, ?, ?, 'not_started')",
                    (f"{t}-{i}", t, f"S{i}"),
                )
    _alembic(db, "upgrade", "head")
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT test_id, pin FROM roster_entries").fetchall()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert len(rows) == 80 and all(pin and PIN_RE.match(pin) for _, pin in rows)
    for t in ("t1", "t2"):
        pins = [pin for test_id, pin in rows if test_id == t]
        assert len(pins) == len(set(pins)) == 40
    assert "attempt_sessions" in tables

    _alembic(db, "downgrade", "-1")
    assert "pin" not in _columns(db, "roster_entries")
    with sqlite3.connect(db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert conn.execute("SELECT COUNT(*) FROM roster_entries").fetchone()[0] == 80
    assert "attempt_sessions" not in tables

    _alembic(db, "upgrade", "head")
    assert {"pin"} <= _columns(db, "roster_entries")
    assert {"token_hash", "attempt_id", "created_at"} == _columns(db, "attempt_sessions")
    with sqlite3.connect(db) as conn:
        nulls = conn.execute("SELECT COUNT(*) FROM roster_entries WHERE pin IS NULL").fetchone()
    assert nulls[0] == 0


# --------------------------------------------------------------------------- #
# Teacher side (HTTP, reusing the admin dashboard fixtures).
# --------------------------------------------------------------------------- #
def _add(client: TestClient, name: str) -> dict:  # noqa: F811
    resp = client.post(
        "/admin/tests/t1/roster", json={"display_name": name}, headers=_headers(client)
    )
    assert resp.status_code == 200
    return resp.json()


def test_teacher_sees_pins_in_add_bulk_and_roster(client: TestClient, session: Session) -> None:  # noqa: F811
    ContentRepository(session).add_test(_test("t1"))
    session.commit()
    one = _add(client, "Anna")
    assert PIN_RE.match(one["pin"])
    bulk = client.post(
        "/admin/tests/t1/roster/bulk", json={"names": ["Bao", "Carlos"]}, headers=_headers(client)
    ).json()
    assert all(PIN_RE.match(e["pin"]) for e in bulk["added"])
    roster = client.get("/admin/tests/t1/roster", headers=_headers(client)).json()
    assert {r["display_name"]: r["pin"] for r in roster}["Anna"] == one["pin"]
    assert len({r["pin"] for r in roster}) == 3
    assert client.get("/admin/tests/t1/roster").status_code == 401  # teacher-only
    # ...and the student share-link list still shows names only.
    student_view = client.get("/exam/tests/t1/roster")
    assert one["pin"] not in student_view.text and "pin" not in student_view.text


def test_regenerate_pin_affects_only_future_starts(
    client: TestClient, session: Session, caplog: pytest.LogCaptureFixture  # noqa: F811
) -> None:
    ContentRepository(session).add_test(_test("t1"))
    session.commit()
    entry = _add(client, "Anna")
    old_pin = entry["pin"]
    started = client.post(f"/exam/roster/{entry['id']}/start", json={"pin": old_pin}).json()

    assert client.post(f"/admin/roster/{entry['id']}/pin").status_code == 401
    with caplog.at_level(logging.INFO):
        resp = client.post(f"/admin/roster/{entry['id']}/pin", headers=_headers(client))
    assert resp.status_code == 200
    new_pin = resp.json()["pin"]
    assert PIN_RE.match(new_pin) and new_pin != old_pin
    assert any("admin regenerate_pin" in r.getMessage() for r in caplog.records)
    assert all(new_pin not in r.getMessage() for r in caplog.records)

    # The old PIN no longer starts; the new one does (same attempt).
    start_url = f"/exam/roster/{entry['id']}/start"
    assert client.post(start_url, json={"pin": old_pin}).status_code == 403
    again = client.post(f"/exam/roster/{entry['id']}/start", json={"pin": new_pin})
    assert again.status_code == 200 and again.json()["attempt_id"] == started["attempt_id"]
    # The session opened with the OLD PIN keeps working (no mid-test kick-out).
    state = client.get(
        f"/exam/attempts/{started['attempt_id']}/state",
        headers={"X-Attempt-Token": started["token"]},
    )
    assert state.status_code == 200 and state.json()["status"] == "in_progress"
    assert client.post("/admin/roster/nope/pin", headers=_headers(client)).status_code == 404


def test_regenerate_pin_lifts_a_wrong_pin_lock(client: TestClient, session: Session) -> None:  # noqa: F811
    ContentRepository(session).add_test(_test("t1"))
    session.commit()
    entry = _add(client, "Anna")
    codes = [
        client.post(f"/exam/roster/{entry['id']}/start", json={"pin": "ZZZZ"}).status_code
        for _ in range(5)
    ]
    assert codes[-1] == 429
    regen = client.post(f"/admin/roster/{entry['id']}/pin", headers=_headers(client))
    new_pin = regen.json()["pin"]
    start_url = f"/exam/roster/{entry['id']}/start"
    assert client.post(start_url, json={"pin": new_pin}).status_code == 200


def test_regenerate_pin_lifts_the_lock_only_after_the_commit(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The lock is lifted AFTER the new PIN is committed; a failed commit lifts nothing."""
    from sqlalchemy import event

    import app.delivery.access as access

    ContentRepository(session).add_test(_test("t1"))
    session.commit()
    entry = _add(client, "Anna")
    for _ in range(5):
        client.post(f"/exam/roster/{entry['id']}/start", json={"pin": "ZZZZ"})
    order: list[str] = []
    event.listen(session, "after_commit", lambda _s: order.append("commit"))
    real_reset = access.PIN_LIMITER.reset_entry

    def spy(entry_id: str) -> None:
        order.append("reset_entry")
        real_reset(entry_id)

    monkeypatch.setattr(access.PIN_LIMITER, "reset_entry", spy)
    regen = client.post(f"/admin/roster/{entry['id']}/pin", headers=_headers(client))
    assert regen.status_code == 200
    assert order[:2] == ["commit", "reset_entry"]

    # A commit that fails leaves the lock in place (nothing changed, nothing lifted).
    for _ in range(5):
        client.post(f"/exam/roster/{entry['id']}/start", json={"pin": "ZZZZ"})
    order.clear()

    def boom() -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(session, "commit", boom)
    with pytest.raises(RuntimeError):
        client.post(f"/admin/roster/{entry['id']}/pin", headers=_headers(client))
    assert "reset_entry" not in order
    assert access.PIN_LIMITER.test_activity("t1", [entry["id"]]).locked


def test_pin_activity_and_clear_locks_for_the_teacher(
    client: TestClient, session: Session, caplog: pytest.LogCaptureFixture  # noqa: F811
) -> None:
    ContentRepository(session).add_test(_test("t1"))
    session.commit()
    anna, bao = _add(client, "Anna"), _add(client, "Bao")
    headers = _headers(client)
    assert client.get("/admin/tests/t1/pin-activity").status_code == 401
    assert client.post("/admin/tests/t1/pin-locks/clear").status_code == 401
    assert client.get("/admin/tests/nope/pin-activity", headers=headers).status_code == 404

    quiet = client.get("/admin/tests/t1/pin-activity", headers=headers).json()
    assert quiet == {
        "window_minutes": 10, "threshold": 40, "wrong_pins": 0, "alert": False,
        "throttled_ips": 0, "students": [],
    }
    for _ in range(5):  # someone locks Anna out
        client.post(f"/exam/roster/{anna['id']}/start", json={"pin": "ZZZZ"})
    client.post(f"/exam/roster/{bao['id']}/start", json={"pin": "ZZZZ"})
    act = client.get("/admin/tests/t1/pin-activity", headers=headers)
    body = act.json()
    assert body["wrong_pins"] == 6 and not body["alert"]
    rows = [(r["display_name"], r["wrong_pins"]) for r in body["students"]]
    assert rows == [("Anna", 5), ("Bao", 1)]
    assert body["students"][0]["locked_seconds"] > 0 and body["students"][1]["locked_seconds"] == 0
    assert anna["pin"] not in act.text and bao["pin"] not in act.text  # never PINs

    with caplog.at_level(logging.INFO):
        cleared = client.post("/admin/tests/t1/pin-locks/clear", headers=headers)
    assert cleared.status_code == 200 and cleared.json()["students_unlocked"] == 1
    assert any(
        r.levelno == logging.INFO
        and "admin clear_pin_locks test=t1 students_unlocked=1" in r.getMessage()
        for r in caplog.records
    )
    back = client.post(f"/exam/roster/{anna['id']}/start", json={"pin": anna["pin"]})
    assert back.status_code == 200
    after = client.get("/admin/tests/t1/pin-activity", headers=headers).json()
    assert all(r["locked_seconds"] == 0 for r in after["students"])


def test_pin_activity_alert_over_http(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    import app.delivery.access as access

    monkeypatch.setattr(access, "PIN_LIMITER", PinRateLimiter(test_alert_failures=3))
    ContentRepository(session).add_test(_test("t1"))
    session.commit()
    anna = _add(client, "Anna")
    for _ in range(3):
        client.post(f"/exam/roster/{anna['id']}/start", json={"pin": "ZZZZ"})
    body = client.get("/admin/tests/t1/pin-activity", headers=_headers(client)).json()
    assert body["alert"] and body["wrong_pins"] == 3 and body["threshold"] == 3


def test_results_csv_has_no_pins(client: TestClient, session: Session) -> None:  # noqa: F811
    ContentRepository(session).add_test(_test("t1"))
    session.commit()
    _seed(session, name="Anna")
    _seed(session, name="Bao")
    pins = [e.pin for e in AttemptRepository(session).list_roster_entries("t1")]
    assert len(pins) == 2 and all(pins)
    csv_text = client.get("/admin/tests/t1/results.csv", headers=_headers(client)).text
    assert "Anna" in csv_text
    assert "PIN" not in csv_text.upper().replace("PINYIN", "")
    for pin in pins:
        assert pin not in csv_text


def test_teacher_reset_drops_the_sessions(client: TestClient, session: Session) -> None:  # noqa: F811
    ContentRepository(session).add_test(_test("t1"))
    session.commit()
    entry = _add(client, "Anna")
    started = client.post(f"/exam/roster/{entry['id']}/start", json={"pin": entry["pin"]}).json()
    repo = AttemptRepository(session)
    assert repo.count_attempt_sessions(started["attempt_id"]) == 1
    reset = client.post(f"/admin/roster/{entry['id']}/reset", headers=_headers(client))
    assert reset.status_code == 200
    assert repo.count_attempt_sessions(started["attempt_id"]) == 0
    assert not repo.attempt_session_valid(started["attempt_id"], started["token"])
    # The PIN survives the reset: the student retakes with the same card.
    retake = client.post(f"/exam/roster/{entry['id']}/start", json={"pin": entry["pin"]})
    assert retake.status_code == 200 and retake.json()["attempt_id"] != started["attempt_id"]


def test_pin_cards_page_escapes_and_reads_the_teacher_roster() -> None:
    html = (REPO_ROOT / "apps" / "web" / "teacher-pins.html").read_text(encoding="utf-8")
    assert "/admin/tests/" in html and "/roster" in html
    assert "${esc(r.display_name)}" in html and "${esc(r.pin" in html
    assert "${esc(test.title)}" in html and "${esc(link)}" in html
    assert "@page { size: A4" in html
