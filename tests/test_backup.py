"""Ops scripts: online SQLite backup, retention, restore guard, session helper.

The backup test writes to a WAL-mode database from a second thread WHILE backups
are taken — the situation during a live exam — and asserts every copy is a
self-contained, integrity-checked snapshot.
"""

from __future__ import annotations

import socket
import sqlite3
import subprocess
import threading
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from app.core.config import DEV_ADMIN_TOKEN_SECRET, DEV_TEACHER_PASSWORD, Settings
from scripts import backup_db, restore_db, session
from scripts.backup_db import BackupError, backup_database, prune_backups, verify_database
from scripts.restore_db import restore_database
from scripts.runtime_guard import pid_alive, server_running_reason, write_pid_file

STRONG_SECRET = "x" * 43


def _make_wal_db(path: Path, rows: int = 0) -> None:
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    conn.executescript(
        """
        CREATE TABLE attempts (id INTEGER PRIMARY KEY, who TEXT);
        CREATE TABLE answers (id INTEGER PRIMARY KEY, attempt_id INTEGER, payload TEXT);
        CREATE TABLE attempt_reports (id INTEGER PRIMARY KEY);
        """
    )
    for i in range(rows):
        conn.execute("INSERT INTO attempts (who) VALUES (?)", (f"s{i}",))
    conn.commit()
    conn.close()


def _count(path: Path, table: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


@pytest.fixture
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def busy_port() -> Iterator[int]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        yield int(s.getsockname()[1])


# --------------------------------------------------------------------------- #
# backup
# --------------------------------------------------------------------------- #


def test_sqlite_path_from_url() -> None:
    assert backup_db.sqlite_path_from_url("sqlite:///./english_exam.db") == Path(
        "./english_exam.db"
    )
    with pytest.raises(BackupError, match="SQLite only"):
        backup_db.sqlite_path_from_url("postgresql+psycopg://u:p@h/db")
    with pytest.raises(BackupError, match="in-memory"):
        backup_db.sqlite_path_from_url("sqlite://")


def test_backup_of_wal_db_under_concurrent_writes(tmp_path: Path) -> None:
    db = tmp_path / "live.db"
    _make_wal_db(db, rows=5)
    stop = threading.Event()
    written = 0
    errors: list[BaseException] = []

    def writer() -> None:
        nonlocal written
        conn = sqlite3.connect(db, timeout=10)
        try:
            while not stop.is_set():
                conn.execute("INSERT INTO attempts (who) VALUES ('w')")
                conn.execute(
                    "INSERT INTO answers (attempt_id, payload) VALUES (last_insert_rowid(), ?)",
                    ("x" * 200,),
                )
                conn.commit()
                written += 1
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            conn.close()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        while written < 50:  # make sure the writer is really running
            threading.Event().wait(0.01)
        results = [
            backup_database(db, tmp_path / "backups", keep=10, now=datetime(2026, 11, 17, 10, 0))
            for _ in range(3)
        ]
    finally:
        stop.set()
        thread.join(10)
    assert not errors
    assert written > 50

    for res in results:
        assert res.path.is_file()
        # Self-contained single file: DELETE journal, no -wal sidecar needed.
        assert not Path(f"{res.path}-wal").exists()
        conn = sqlite3.connect(res.path)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            attempts = conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
            answers = conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
        finally:
            conn.close()
        # A consistent snapshot: every attempt the writer inserted has its answer.
        assert attempts - 5 == answers
        assert res.counts["attempts"] == attempts
        assert res.counts["integrity_events"] is None  # absent table is reported, not fatal
    # Same second three times -> unique names, none overwritten.
    names = sorted(r.path.name for r in results)
    assert names == [
        "english_exam-20261117-100000-2.db",
        "english_exam-20261117-100000-3.db",
        "english_exam-20261117-100000.db",
    ]
    assert not list((tmp_path / "backups").glob("*.partial"))


def test_backup_includes_uncheckpointed_wal_content(tmp_path: Path) -> None:
    db = tmp_path / "live.db"
    _make_wal_db(db)
    holder = sqlite3.connect(db)  # keeps the WAL from being checkpointed away on close
    holder.execute("PRAGMA wal_autocheckpoint=0")
    holder.execute("INSERT INTO attempts (who) VALUES ('only-in-wal')")
    holder.commit()
    try:
        assert Path(f"{db}-wal").stat().st_size > 0
        res = backup_database(db, tmp_path / "b", keep=5)
    finally:
        holder.close()
    assert res.counts["attempts"] == 1


def test_verify_rejects_a_corrupt_copy(tmp_path: Path) -> None:
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4000)
    with pytest.raises(BackupError):
        verify_database(bad)


def test_missing_database_leaves_no_partial(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="not found"):
        backup_database(tmp_path / "nope.db", tmp_path / "b")
    assert not list((tmp_path / "b").iterdir())


def test_retention_keeps_newest_n_and_spares_labeled(tmp_path: Path) -> None:
    db = tmp_path / "live.db"
    _make_wal_db(db, rows=1)
    dest = tmp_path / "backups"
    for minute in range(6):
        backup_database(db, dest, keep=0, now=datetime(2026, 11, 17, 9, minute))
    labeled = backup_database(db, dest, label="presession", now=datetime(2026, 1, 1)).path
    (dest / "assets-20260101-000000").mkdir()
    (dest / "assets-20261117-090500").mkdir()

    removed = prune_backups(dest, keep=3)

    left = sorted(p.name for p in dest.iterdir())
    assert left == [
        "assets-20260101-000000",
        "assets-20261117-090500",
        "english_exam-20260101-000000-presession.db",
        "english_exam-20261117-090300.db",
        "english_exam-20261117-090400.db",
        "english_exam-20261117-090500.db",
    ]
    assert len(removed) == 3
    assert labeled.exists()
    # keep applies to asset copies too
    prune_backups(dest, keep=1)
    assert not (dest / "assets-20260101-000000").exists()
    assert (dest / "assets-20261117-090500").exists()


def test_backup_with_assets_and_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "live.db"
    _make_wal_db(db, rows=2)
    assets = tmp_path / "assets"
    (assets / "a").mkdir(parents=True)
    (assets / "a" / "x.png").write_bytes(b"png")
    monkeypatch.setattr(backup_db.settings, "assets_dir", str(assets))
    dest = tmp_path / "out"
    code = backup_db.main(
        ["--dir", str(dest), "--keep", "5", "--with-assets", "--database-url", f"sqlite:///{db}"]
    )
    assert code == 0
    assert len(list(dest.glob("english_exam-*.db"))) == 1
    copied = list(dest.glob("assets-*"))
    assert len(copied) == 1 and (copied[0] / "a" / "x.png").read_bytes() == b"png"
    assert backup_db.main(["--dir", str(dest), "--database-url", "sqlite://"]) == 1


# --------------------------------------------------------------------------- #
# restore
# --------------------------------------------------------------------------- #


def test_restore_refuses_when_port_busy(tmp_path: Path, busy_port: int) -> None:
    db = tmp_path / "live.db"
    _make_wal_db(db, rows=3)
    snap = backup_database(db, tmp_path / "b", keep=5).path
    with pytest.raises(BackupError, match=f"port {busy_port} is in use"):
        restore_database(snap, db, tmp_path / "b", port=busy_port, pid_file=tmp_path / "pid")
    # nothing touched: no pre-restore copy, live DB intact
    assert not list((tmp_path / "b").glob("*prerestore*"))
    assert _count(db, "attempts") == 3


def test_restore_refuses_while_session_pid_alive(tmp_path: Path, free_port: int) -> None:
    db = tmp_path / "live.db"
    _make_wal_db(db, rows=1)
    snap = backup_database(db, tmp_path / "b", keep=5).path
    pid_file = tmp_path / "session.pid"
    write_pid_file(9999, pid_file)  # our own pid: certainly alive
    with pytest.raises(BackupError, match="session pid"):
        restore_database(snap, db, tmp_path / "b", port=free_port, pid_file=pid_file)


def test_restore_makes_prerestore_backup_then_restores(tmp_path: Path, free_port: int) -> None:
    db = tmp_path / "live.db"
    _make_wal_db(db, rows=2)
    dest = tmp_path / "backups"
    snap = backup_database(db, dest, keep=5).path
    # more data after the snapshot, some of it still only in the WAL
    conn = sqlite3.connect(db)
    conn.executemany("INSERT INTO attempts (who) VALUES (?)", [("late",)] * 4)
    conn.commit()
    conn.close()
    assert _count(db, "attempts") == 6

    stale = tmp_path / "stale.pid"
    stale.write_text('{"pid": 999999999, "port": 1}', encoding="utf-8")  # dead -> ignored
    pre = restore_database(snap, db, dest, port=free_port, pid_file=stale)

    assert pre is not None and pre.name.endswith("-prerestore.db")
    assert _count(pre, "attempts") == 6  # the pre-restore copy holds the newer state
    assert _count(db, "attempts") == 2  # live DB is back to the snapshot
    assert snap.exists()  # labeled pre-restore copy never rotates the source away


def test_restore_rejects_corrupt_source(tmp_path: Path, free_port: int) -> None:
    db = tmp_path / "live.db"
    _make_wal_db(db, rows=1)
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database at all" * 100)
    with pytest.raises(BackupError):
        restore_database(bad, db, tmp_path / "b", port=free_port, pid_file=tmp_path / "p")
    assert _count(db, "attempts") == 1


def test_restore_cli_refuses_busy_port(tmp_path: Path, busy_port: int) -> None:
    db = tmp_path / "live.db"
    _make_wal_db(db)
    snap = backup_database(db, tmp_path / "b", keep=5).path
    code = restore_db.main(
        ["--file", str(snap), "--dir", str(tmp_path / "b"), "--port", str(busy_port),
         "--database-url", f"sqlite:///{db}"]
    )  # fmt: skip
    assert code == 1


def test_pid_alive() -> None:
    import os

    assert pid_alive(os.getpid())
    assert not pid_alive(0)
    assert not pid_alive(999999999)


def test_server_running_reason_clear(tmp_path: Path, free_port: int) -> None:
    assert server_running_reason(free_port, tmp_path / "none.pid") is None


def test_stale_pid_file_with_a_reused_pid_does_not_block_restore(
    tmp_path: Path, free_port: int, caplog: pytest.LogCaptureFixture
) -> None:
    """After a hard console close Windows may hand the pid to another process: the
    recorded start time no longer matches, so the file is stale, not "running"."""
    import json
    import os
    import time

    from scripts.runtime_guard import process_start_time

    pid_file = tmp_path / "session.pid"
    write_pid_file(free_port, pid_file)  # our own (alive) pid + its start time
    data = json.loads(pid_file.read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid() and abs(data["started"] - time.time()) < 3600
    assert data["started"] == pytest.approx(process_start_time(os.getpid()), abs=2)
    assert "still running" in (server_running_reason(free_port, pid_file) or "")

    data["started"] -= 3600  # "the session" started an hour before this process
    pid_file.write_text(json.dumps(data), encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert server_running_reason(free_port, pid_file) is None
    assert any("belongs to another process" in r.getMessage() for r in caplog.records)

    del data["started"]  # an old-format file: falls back to "pid alive"
    pid_file.write_text(json.dumps(data), encoding="utf-8")
    assert "delete that file" in (server_running_reason(free_port, pid_file) or "")


def _fail_rename(monkeypatch: pytest.MonkeyPatch, times: int) -> list[str]:
    """Make `Path.replace` raise PermissionError (Defender / indexer) `times` times."""
    calls: list[str] = []
    real = Path.replace

    def flaky(self: Path, target: Path) -> Path:
        calls.append(self.name)
        if len(calls) <= times:
            raise PermissionError(32, "The process cannot access the file", str(self))
        return real(self, target)

    monkeypatch.setattr(Path, "replace", flaky)
    monkeypatch.setattr(backup_db, "RENAME_RETRY_DELAY_S", 0)
    return calls


def test_backup_retries_a_refused_rename_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "live.db"
    _make_wal_db(db, rows=2)
    calls = _fail_rename(monkeypatch, times=1)
    with caplog.at_level("WARNING"):
        res = backup_database(db, tmp_path / "b", keep=5)
    assert res.path.is_file() and res.counts["attempts"] == 2
    assert len(calls) == 2
    assert any("retrying once" in r.getMessage() for r in caplog.records)


def test_backup_rename_failing_twice_is_a_backup_error_without_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "live.db"
    _make_wal_db(db, rows=1)
    _fail_rename(monkeypatch, times=2)
    with pytest.raises(BackupError, match="cannot access"):
        backup_database(db, tmp_path / "b", keep=5)
    assert list((tmp_path / "b").iterdir()) == []  # no .partial, no half backup


def test_backup_disk_full_and_bad_dir_are_backup_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "live.db"
    _make_wal_db(db, rows=1)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    with pytest.raises(BackupError, match="backup directory"):
        backup_database(db, blocker / "b")

    def full(src: Path, dest: Path) -> None:
        dest.write_bytes(b"partial")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(backup_db, "snapshot_database", full)
    with pytest.raises(BackupError, match="No space left"):
        backup_database(db, tmp_path / "b2")
    assert list((tmp_path / "b2").iterdir()) == []


def test_backup_rotation_failure_keeps_the_new_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "live.db"
    _make_wal_db(db, rows=1)

    def locked(*_a: object, **_k: object) -> list[Path]:
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(backup_db, "prune_backups", locked)
    with caplog.at_level("WARNING"):
        res = backup_database(db, tmp_path / "b", keep=1)
    assert res.path.is_file()
    assert any("rotation skipped" in r.getMessage() for r in caplog.records)


def test_backup_names_use_utc(tmp_path: Path) -> None:
    from datetime import UTC, timedelta, timezone

    db = tmp_path / "live.db"
    _make_wal_db(db, rows=1)
    kyiv_summer = datetime(2026, 7, 1, 12, 30, tzinfo=timezone(timedelta(hours=3)))
    res = backup_database(db, tmp_path / "b", now=kyiv_summer)
    assert res.path.name == "english_exam-20260701-093000.db"
    default = backup_database(db, tmp_path / "b").path.name
    assert default.startswith(f"english_exam-{datetime.now(UTC):%Y%m%d}")


def test_session_backup_logs_file_errors_and_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "live.db"
    _make_wal_db(db, rows=1)
    _fail_rename(monkeypatch, times=99)
    with caplog.at_level("INFO"):
        session._safe_backup(db, tmp_path / "b", 5, "final")  # must not raise
    assert any(
        r.levelname == "ERROR" and "final backup failed" in r.getMessage()
        for r in caplog.records
    )

    def bug(*_a: object, **_k: object) -> None:
        raise ValueError("unexpected")

    monkeypatch.setattr(session, "backup_database", bug)
    caplog.clear()
    with caplog.at_level("ERROR"):
        session._safe_backup(db, tmp_path / "b", 5, None)
    assert any("periodic backup failed unexpectedly" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# session helper
# --------------------------------------------------------------------------- #


def _cfg(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "environment": "production",
        "teacher_password": "a-real-password",
        "admin_token_secret": STRONG_SECRET,
        "gemini_api_key": "key",
        "database_url": "sqlite:///./english_exam.db",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def _levels(problems: list[session.Problem]) -> list[str]:
    return [p.level for p in problems]


def test_check_env_clean() -> None:
    assert session.check_env(_cfg(), "cloudflared") == []
    assert session.check_env(_cfg(), None) == []


def test_check_env_tunnel_with_defaults_is_an_error() -> None:
    probs = session.check_env(
        _cfg(teacher_password=DEV_TEACHER_PASSWORD, admin_token_secret=DEV_ADMIN_TOKEN_SECRET),
        "ngrok",
    )
    msgs = " ".join(p.message for p in probs)
    assert _levels(probs).count("error") == 2
    assert "TEACHER_PASSWORD" in msgs and "ADMIN_TOKEN_SECRET" in msgs
    assert "token_urlsafe" in msgs


def test_check_env_tunnel_with_dev_environment_warns() -> None:
    probs = session.check_env(_cfg(environment="dev"), "cloudflared")
    assert _levels(probs) == ["warning"]
    assert "ENVIRONMENT=production" in probs[0].message


def test_check_env_local_defaults() -> None:
    dev = session.check_env(_cfg(environment="dev", teacher_password=DEV_TEACHER_PASSWORD), None)
    assert _levels(dev) == ["warning"]
    prod = session.check_env(_cfg(admin_token_secret=DEV_ADMIN_TOKEN_SECRET), None)
    assert _levels(prod) == ["error"]  # the dashboard would answer 503


def test_check_env_gemini_short_secret_and_postgres() -> None:
    probs = session.check_env(
        _cfg(gemini_api_key=" ", admin_token_secret="short", database_url="postgresql://x/y"),
        None,
    )
    by = {p.message.split()[0]: p.level for p in probs}
    assert by["GEMINI_API_KEY"] == "warning"
    assert by["ADMIN_TOKEN_SECRET"] == "warning"
    assert "error" in _levels(probs)  # non-SQLite -> no automatic backups


def test_uvicorn_command_single_worker() -> None:
    cmd = session.uvicorn_command("0.0.0.0", 8123, python="py")
    assert cmd[:4] == ["py", "-m", "uvicorn", "apps.api.main:app"]
    assert cmd[cmd.index("--workers") + 1] == "1"
    assert cmd[cmd.index("--port") + 1] == "8123"
    assert "--reload" not in cmd
    assert "--proxy-headers" not in cmd

    tunneled = session.uvicorn_command("127.0.0.1", 8000, tunnel="cloudflared")
    assert "--proxy-headers" in tunneled
    assert "--forwarded-allow-ips=127.0.0.1" in tunneled
    assert tunneled[tunneled.index("--workers") + 1] == "1"


def test_tunnel_and_migrate_commands() -> None:
    assert session.tunnel_command("cloudflared", 8000) == (
        "cloudflared tunnel --url http://localhost:8000"
    )
    assert session.tunnel_command("ngrok", 8080) == "ngrok http 8080"
    with pytest.raises(ValueError):
        session.tunnel_command("frp", 8000)
    assert session.migrate_command("py") == ["py", "-m", "alembic", "upgrade", "head"]


class _FakeProc:
    """Stands in for the uvicorn Popen: alive for `ticks` waits, then exits 3."""

    def __init__(self, ticks: int) -> None:
        self.ticks = ticks
        self.terminated = False

    def wait(self, timeout: float | None = None) -> int:
        if self.ticks <= 0:
            return 3
        self.ticks -= 1
        raise subprocess.TimeoutExpired("uvicorn", timeout or 0)

    def poll(self) -> int | None:
        return None if self.ticks > 0 else 3

    def terminate(self) -> None:
        self.terminated = True
        self.ticks = 0


def test_supervise_backs_up_periodically_and_survives_backup_errors() -> None:
    now = [0.0]

    def clock() -> float:
        now[0] += 1.0  # each call advances one "second"
        return now[0]

    calls: list[int] = []

    def backup() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("disk full")  # logged, the session keeps running

    code = session.supervise(_FakeProc(ticks=20), backup, every_s=4.0, tick_s=0, clock=clock)
    assert code == 3
    assert len(calls) >= 3


def test_stop_server_terminates_a_hung_server() -> None:
    class Hung(_FakeProc):
        def wait(self, timeout: float | None = None) -> int:
            if not self.terminated:
                raise subprocess.TimeoutExpired("uvicorn", timeout or 0)
            return 1

    proc = Hung(ticks=1)
    assert session.stop_server(proc, wait_s=0) == 1
    assert proc.terminated


def test_session_main_refuses_on_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, free_port: int
) -> None:
    bad = _cfg(teacher_password=DEV_TEACHER_PASSWORD)
    monkeypatch.setattr(session, "settings", bad)
    started: list[object] = []
    monkeypatch.setattr(session.subprocess, "Popen", lambda *a, **k: started.append(a))
    monkeypatch.setattr(session.subprocess, "run", lambda *a, **k: started.append(a))
    code = session.main(["--tunnel", "ngrok", "--port", str(free_port), "--dir", str(tmp_path)])
    assert code == 2
    assert started == []  # neither migrations nor uvicorn ran
