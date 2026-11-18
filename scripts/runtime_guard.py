"""Is the exam server running right now? — the heuristic shared by the ops scripts.

Two independent signals; either one means "running":

1. **The API port answers.** A TCP connect to `127.0.0.1:<port>` (default
   `API_PORT`, 8000) succeeds -> something is listening there: `make run`,
   `make session`, docker, or another app. Restoring under a live server would
   swap the database out from under open connections, so we refuse.
2. **The session pid file.** `scripts/session.py` writes `var/session.pid`
   (`{"pid": ..., "port": ..., "started": <process start, epoch s>}`) while it runs
   and removes it on exit. If that file names a process that is still alive AND
   started at the recorded time we refuse too — this catches a session started on a
   different `--port`. A stale file (process gone after a power cut or a console
   closed with the X button) is logged and ignored, including when Windows has
   meanwhile REUSED that pid for an unrelated process: its start time differs, so
   the file cannot block a restore forever. (A file written before `started` was
   recorded falls back to the plain "pid alive" check; the teacher can delete
   `var/session.pid` by hand — docs/OPERATIONS.md.)

Limits (documented in docs/OPERATIONS.md): a server bound only to a non-loopback
address on another port is invisible to both checks — stop the server first.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

from app.core.logging import get_logger

log = get_logger(__name__)

SESSION_PID_FILE = Path("var") / "session.pid"
PORT_PROBE_TIMEOUT_S = 0.5


def port_in_use(port: int, host: str = "127.0.0.1", timeout: float = PORT_PROBE_TIMEOUT_S) -> bool:
    """True when something accepts TCP connections on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pid_alive(pid: int) -> bool:
    """Best-effort "does this process exist" without ever signalling it.

    Windows: `os.kill(pid, 0)` would TERMINATE the process, so ask the kernel via
    OpenProcess + GetExitCodeProcess instead.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined,unused-ignore]
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# Two readings of one process's start time may differ by rounding only.
START_TIME_TOLERANCE_S = 2.0


def process_start_time(pid: int) -> float | None:
    """When process `pid` started (epoch seconds), or None if unknown / gone.

    Windows: GetProcessTimes' creation FILETIME. Linux: /proc/<pid>/stat field 22
    + the boot time. Elsewhere None (callers fall back to `pid_alive`).
    """
    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined,unused-ignore]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            return created / 1e7 - 11_644_473_600  # 100 ns since 1601 -> epoch s
        finally:
            kernel32.CloseHandle(handle)
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        ticks = int(fields[19])  # field 22 overall: starttime in clock ticks
        btime = next(
            int(line.split()[1])
            for line in Path("/proc/stat").read_text().splitlines()
            if line.startswith("btime ")
        )
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def write_pid_file(port: int, path: Path = SESSION_PID_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    info: dict[str, float | int] = {"pid": pid, "port": port}
    started = process_start_time(pid)
    if started is not None:
        info["started"] = started
    path.write_text(json.dumps(info), encoding="utf-8")


def remove_pid_file(path: Path = SESSION_PID_FILE) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def read_pid_file(path: Path = SESSION_PID_FILE) -> dict[str, float] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        info: dict[str, float] = {"pid": int(data["pid"]), "port": int(data["port"])}
        if data.get("started") is not None:
            info["started"] = float(data["started"])
        return info
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("ignoring unreadable session pid file %s: %s", path, exc)
        return None


def server_running_reason(port: int, pid_file: Path = SESSION_PID_FILE) -> str | None:
    """Why we think the server is running, or None if both checks are clear."""
    if port_in_use(port):
        return f"port {port} is in use (an API server seems to be running)"
    info = read_pid_file(pid_file)
    if info is not None:
        pid = int(info["pid"])
        if pid_alive(pid):
            recorded = info.get("started")
            actual = process_start_time(pid)
            if (
                recorded is not None
                and actual is not None
                and abs(actual - recorded) > START_TIME_TOLERANCE_S
            ):
                log.warning(
                    "stale session pid file %s: pid %d now belongs to another process "
                    "(started at a different time) — ignoring",
                    pid_file,
                    pid,
                )
                return None
            return (
                f"exam session pid {pid} (port {int(info['port'])}) is still running "
                f"({pid_file}; if no session window is open, delete that file)"
            )
        log.warning("stale session pid file %s (pid %d is gone) — ignoring", pid_file, pid)
    return None
