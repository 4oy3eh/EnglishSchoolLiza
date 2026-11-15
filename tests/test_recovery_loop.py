"""Periodic recovery loop in the API process (Phase 13).

Gate: the lifespan starts a daemon thread that runs the recovery sweep (expire
overdue attempts + finalize missing reports) at startup and then every
`RECOVERY_SWEEP_INTERVAL_S`; it never dies on a failing sweep, stops promptly when
the lifespan exits, is disabled by `SKIP_STARTUP_SWEEP=1`, and — through
`sweep_missing_reports` — respects the in-flight finalize lock and the
failed-report retry cooldown. The first sweep waits `RECOVERY_INITIAL_DELAY_S`
(interruptible by shutdown); tests pass `initial_delay_s=0`.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import apps.api.main as main_mod
from app.persistence.repository import AttemptRepository
from apps.api.main import app
from tests.test_results import (
    _age_report,
    _bg_on,
    _held_elsewhere,
    _seed,
    _seed_second_attempt,
    _service,
)

LOOP_THREAD = "report-recovery-loop"


def _loop_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == LOOP_THREAD]


def test_loop_runs_repeatedly_survives_errors_and_stops() -> None:
    stop = threading.Event()
    calls: list[int] = []
    enough = threading.Event()

    def sweep() -> None:
        calls.append(1)
        if len(calls) >= 4:
            enough.set()
        if len(calls) == 2:
            raise RuntimeError("sweep blew up")  # must not kill the loop

    thread = main_mod.start_recovery_loop(stop, 0.01, sweep, initial_delay_s=0)
    assert thread.daemon and thread.name == LOOP_THREAD
    assert enough.wait(5), "loop did not keep sweeping after an error"
    stop.set()
    thread.join(5)
    assert not thread.is_alive()
    frozen = len(calls)
    stop.wait(0.05)
    assert len(calls) == frozen  # nothing runs after stop


def test_stop_interrupts_the_wait_immediately() -> None:
    stop = threading.Event()
    ran = threading.Event()
    thread = main_mod.start_recovery_loop(stop, 3600, ran.set, initial_delay_s=0)  # 1 h interval
    assert ran.wait(5)  # first sweep runs at once with no initial delay
    stop.set()
    thread.join(2)
    assert not thread.is_alive()


def test_first_sweep_waits_for_the_initial_delay() -> None:
    """The loop waits `RECOVERY_INITIAL_DELAY_S` (5 s) before its first sweep."""
    assert main_mod.RECOVERY_INITIAL_DELAY_S == 5.0
    stop = threading.Event()
    ran = threading.Event()
    started = time.monotonic()
    thread = main_mod.start_recovery_loop(stop, 3600, ran.set, initial_delay_s=0.3)
    assert not ran.wait(0.1)  # still inside the initial delay
    assert ran.wait(5)
    assert time.monotonic() - started >= 0.3
    stop.set()
    thread.join(2)
    assert not thread.is_alive()


def test_stop_during_initial_delay_never_sweeps() -> None:
    """Shutdown inside the initial delay returns at once, without a sweep."""
    stop = threading.Event()
    calls: list[int] = []
    thread = main_mod.start_recovery_loop(stop, 3600, lambda: calls.append(1))  # default 5 s
    stop.set()
    thread.join(2)
    assert not thread.is_alive()
    assert calls == []


def test_autouse_fixture_disables_the_sweep() -> None:
    """tests/conftest.py: no test can start the real loop on the dev DB by accident."""
    assert os.environ.get(main_mod.SKIP_SWEEP_ENV) == "1"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 300.0), ("", 300.0), ("45", 45.0), ("0.5", 0.5), ("0", 300.0), ("-3", 300.0),
     ("soon", 300.0)],
)
def test_recovery_interval_env(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: float
) -> None:
    if raw is None:
        monkeypatch.delenv(main_mod.RECOVERY_INTERVAL_ENV, raising=False)
    else:
        monkeypatch.setenv(main_mod.RECOVERY_INTERVAL_ENV, raw)
    assert main_mod.recovery_interval_s() == expected


def test_lifespan_starts_periodic_loop_and_stops_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(main_mod.SKIP_SWEEP_ENV, raising=False)
    monkeypatch.setenv(main_mod.RECOVERY_INTERVAL_ENV, "0.02")
    monkeypatch.setattr(main_mod, "RECOVERY_INITIAL_DELAY_S", 0.0)
    monkeypatch.setattr(main_mod, "check_db_migrated", lambda _engine: "ok")
    runs: list[int] = []
    several = threading.Event()

    def fake_sweep() -> list[str]:
        runs.append(1)
        if len(runs) >= 3:
            several.set()
        return []

    monkeypatch.setattr(main_mod, "sweep_missing_reports", fake_sweep)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert several.wait(5), "the sweep did not repeat on its interval"
        assert _loop_threads()
    # Lifespan exit set the stop event and joined the thread.
    assert not [t for t in _loop_threads() if t.is_alive()]
    frozen = len(runs)
    threading.Event().wait(0.1)
    assert len(runs) == frozen


def test_lifespan_skip_env_disables_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(main_mod.SKIP_SWEEP_ENV, "1")
    monkeypatch.setattr(main_mod, "check_db_migrated", lambda _engine: "ok")
    runs: list[int] = []
    monkeypatch.setattr(main_mod, "sweep_missing_reports", lambda: runs.append(1))
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert not _loop_threads()
    assert runs == []


def test_loop_with_real_sweep_respects_lock_and_cooldown(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the real `sweep_missing_reports`: an abandoned attempt is
    expired and reported; an in-flight finalize and a fresh failure are left alone."""
    from app.results import sweep_missing_reports

    busy = _seed(session)  # submitted, no report — but its finalize is in flight
    _seed_second_attempt(session, "att-abandoned", status="in_progress")  # deadline past
    _seed_second_attempt(session, "att-fresh-fail")
    _service(session).record_failure("att-fresh-fail", RuntimeError("just now"))
    session.commit()
    _age_report(session, "att-fresh-fail", datetime.now(UTC) - timedelta(minutes=1))
    _bg_on(session, monkeypatch)

    stop = threading.Event()
    results: list[list[str]] = []

    def one_sweep() -> None:
        results.append(sweep_missing_reports())
        stop.set()  # a single iteration is enough here

    with _held_elsewhere(busy):
        thread = main_mod.start_recovery_loop(stop, 0.01, one_sweep, initial_delay_s=0)
        thread.join(10)
    assert not thread.is_alive()
    assert results == [["att-abandoned"]]
    repo = AttemptRepository(session)
    assert repo.get_attempt("att-abandoned").status == "expired"
    assert repo.get_report("att-abandoned").status == "complete"
    assert repo.get_report(busy) is None  # in flight: no duplicate paid run
    assert repo.get_report("att-fresh-fail").status == "failed"  # cooldown respected
