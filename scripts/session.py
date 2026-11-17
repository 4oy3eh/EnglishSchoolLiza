"""Run the platform for an exam session: checks -> migrate -> backup -> serve.

Usage (from the repo root):
    python -m scripts.session [--port 8000] [--every 15] [--tunnel cloudflared|ngrok]
    make session [every=15] [tunnel=cloudflared]

What it does, in order (every step logs one clean console line):
1. `.env` sanity (`check_env`): with a tunnel, TEACHER_PASSWORD / ADMIN_TOKEN_SECRET
   must not be the public dev defaults (the dashboard would refuse remote logins
   anyway — better to fail now than in front of the class); no GEMINI_API_KEY ->
   warning (writing goes to the manual review queue); non-SQLite DB -> error
   (the automatic backups need SQLite); the port must be free.
2. `alembic upgrade head` in a subprocess (alembic's env.py reconfigures logging).
3. A `-presession` backup (labeled = never rotated).
4. uvicorn as a CHILD process with exactly ONE worker (`--workers 1`): the login
   rate limiter, finalize locks and the recovery loop are in-memory, per process.
   With `--tunnel` it also gets `--proxy-headers --forwarded-allow-ips=127.0.0.1`
   (the local ngrok/cloudflared agent), and the tunnel command to run in a SECOND
   terminal is logged — this script never launches third-party binaries.
5. A rotated backup every `--every` minutes (default 15) from THIS process while
   the server runs, until Ctrl+C or the server exits; then a `-final` backup.

Windows: no fork, no signals sent to other processes. Ctrl+C in the console
reaches both this process and uvicorn (same console group); uvicorn shuts down
gracefully on its own, we wait for it (then terminate it if it hangs) and take the
final backup. While running, `var/session.pid` tells `make restore` to refuse.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.config import (
    DEV_ADMIN_TOKEN_SECRET,
    DEV_ENVIRONMENTS,
    DEV_TEACHER_PASSWORD,
    Settings,
    settings,
)
from app.core.logging import configure_logging, get_logger
from scripts.backup_db import BackupError, BackupSettings, backup_database, sqlite_path_from_url
from scripts.runtime_guard import port_in_use, remove_pid_file, write_pid_file

log = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
TUNNELS = ("cloudflared", "ngrok")
# The tunnel agent runs on this machine, so the only proxy to trust is loopback
# (uvicorn's own default; passed explicitly so the intent is visible in the log).
DEFAULT_FORWARDED_ALLOW_IPS = "127.0.0.1"
DEFAULT_BACKUP_EVERY_MIN = 15.0
MIN_SECRET_LENGTH = 16
SHUTDOWN_WAIT_S = 30.0
TICK_S = 1.0


@dataclass(frozen=True)
class Problem:
    level: str  # "error" (session refuses to start) | "warning"
    message: str


def check_env(cfg: Settings, tunnel: str | None) -> list[Problem]:
    """Configuration problems for an exam session. Pure: reads only `cfg`."""
    problems: list[Problem] = []
    dev_env = cfg.environment.strip().lower() in DEV_ENVIRONMENTS
    weak_password = cfg.teacher_password in ("", DEV_TEACHER_PASSWORD)
    weak_secret = cfg.admin_token_secret in ("", DEV_ADMIN_TOKEN_SECRET)
    short_secret = not weak_secret and len(cfg.admin_token_secret) < MIN_SECRET_LENGTH

    if tunnel:
        if weak_password:
            problems.append(
                Problem(
                    "error",
                    "TEACHER_PASSWORD is not set (still the default) — with a "
                    "tunnel anyone on the internet reaches the login page; set it in .env",
                )
            )
        if weak_secret:
            problems.append(
                Problem(
                    "error",
                    "ADMIN_TOKEN_SECRET is not set (still the public default) — "
                    'generate one: python -c "import secrets; '
                    'print(secrets.token_urlsafe(32))"',
                )
            )
        if dev_env:
            problems.append(
                Problem(
                    "warning",
                    f"ENVIRONMENT={cfg.environment} with a tunnel — set "
                    "ENVIRONMENT=production in .env for the exam session",
                )
            )
    elif weak_password or weak_secret:
        if dev_env:
            problems.append(
                Problem(
                    "warning",
                    "TEACHER_PASSWORD / ADMIN_TOKEN_SECRET are the dev "
                    "defaults: the dashboard works only on this computer "
                    "(http://127.0.0.1); set both before opening it elsewhere",
                )
            )
        else:
            problems.append(
                Problem(
                    "error",
                    f"ENVIRONMENT={cfg.environment} but TEACHER_PASSWORD / "
                    "ADMIN_TOKEN_SECRET are the defaults — the dashboard will refuse "
                    "to log in (503); set both in .env",
                )
            )
    if short_secret:
        problems.append(
            Problem(
                "warning",
                f"ADMIN_TOKEN_SECRET is shorter than {MIN_SECRET_LENGTH} "
                "characters — use secrets.token_urlsafe(32)",
            )
        )

    if not cfg.gemini_api_key.strip():
        problems.append(
            Problem(
                "warning",
                "GEMINI_API_KEY is empty — writing will NOT be auto-graded "
                "(it goes to the manual review queue)",
            )
        )
    try:
        sqlite_path_from_url(cfg.database_url)
    except BackupError as exc:
        problems.append(Problem("error", f"{exc} — automatic session backups need SQLite"))
    return problems


def uvicorn_command(
    host: str,
    port: int,
    *,
    tunnel: str | None = None,
    forwarded_allow_ips: str = DEFAULT_FORWARDED_ALLOW_IPS,
    python: str = sys.executable,
) -> list[str]:
    """The uvicorn command line: ONE worker, no reload, optional proxy headers."""
    cmd = [
        python,
        "-m",
        "uvicorn",
        "apps.api.main:app",
        "--host",
        host,
        "--port",
        str(port),
        "--workers",
        "1",
        "--log-level",
        "info",
    ]
    if tunnel:
        cmd += ["--proxy-headers", f"--forwarded-allow-ips={forwarded_allow_ips}"]
    return cmd


def tunnel_command(tunnel: str, port: int) -> str:
    """The exact command for the SECOND terminal (we never spawn it ourselves)."""
    if tunnel == "cloudflared":
        return f"cloudflared tunnel --url http://localhost:{port}"
    if tunnel == "ngrok":
        return f"ngrok http {port}"
    raise ValueError(f"unknown tunnel {tunnel!r} (choose: {', '.join(TUNNELS)})")


def migrate_command(python: str = sys.executable) -> list[str]:
    return [python, "-m", "alembic", "upgrade", "head"]


class Proc(Protocol):
    def wait(self, timeout: float | None = None) -> int: ...
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...


def supervise(
    proc: Proc,
    backup: Callable[[], object],
    every_s: float,
    *,
    tick_s: float = TICK_S,
    clock: Callable[[], float] = time.monotonic,
) -> int | None:
    """Run `backup` every `every_s` seconds while `proc` is alive.

    Returns the server's exit code when it exits by itself; lets KeyboardInterrupt
    propagate (the caller handles shutdown). `proc.wait(timeout=tick)` is the sleep:
    it wakes immediately when the server dies and returns to Python every tick so
    Ctrl+C is handled promptly on Windows too. A failed backup is logged, never fatal.
    """
    next_at = clock() + every_s
    while True:
        try:
            return proc.wait(timeout=tick_s)
        except subprocess.TimeoutExpired:
            pass
        if clock() >= next_at:
            try:
                backup()
            except Exception:  # keep serving the exam whatever happens to a backup
                log.exception("periodic backup failed — the session keeps running")
            next_at = clock() + every_s


def stop_server(proc: Proc, wait_s: float = SHUTDOWN_WAIT_S) -> int | None:
    """Wait for uvicorn's own graceful Ctrl+C shutdown; terminate it if it hangs."""
    try:
        return proc.wait(timeout=wait_s)
    except subprocess.TimeoutExpired:
        log.warning("server did not stop within %ss — terminating it", wait_s)
        proc.terminate()
        try:
            return proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log.error("server still alive after terminate")
            return None


def _safe_backup(db_path: Path, dest: Path, keep: int, label: str | None) -> None:
    """A backup that never takes the session down: `backup_database` turns file-system
    errors into BackupError; anything else unexpected is logged with its traceback."""
    try:
        backup_database(db_path, dest, keep=keep, label=label)
    except BackupError as exc:
        log.error("%s backup failed: %s", label or "periodic", exc)
    except Exception:  # never let a backup bug kill the exam session
        log.exception("%s backup failed unexpectedly", label or "periodic")


def build_parser() -> argparse.ArgumentParser:
    bcfg = BackupSettings()
    p = argparse.ArgumentParser(description="Run an exam session (server + auto backups).")
    p.add_argument("--host", default=settings.api_host, help="Bind address (API_HOST).")
    p.add_argument("--port", type=int, default=settings.api_port, help="Port (API_PORT).")
    p.add_argument(
        "--every",
        type=float,
        default=DEFAULT_BACKUP_EVERY_MIN,
        help="Minutes between automatic backups (default 15).",
    )
    p.add_argument(
        "--tunnel",
        choices=TUNNELS,
        default=None,
        help="Students connect through this tunnel (prints its command).",
    )
    p.add_argument(
        "--forwarded-allow-ips",
        default=DEFAULT_FORWARDED_ALLOW_IPS,
        help="Proxy IPs uvicorn trusts for X-Forwarded-For (never '*').",
    )
    p.add_argument("--dir", default=bcfg.backup_dir, help="Backup directory (BACKUP_DIR).")
    p.add_argument("--keep", type=int, default=bcfg.backup_keep, help="Backups kept (BACKUP_KEEP).")
    p.add_argument("--skip-migrate", action="store_true", help="Do not run alembic upgrade.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(settings.log_level)
    if args.every <= 0:
        log.error("--every must be a positive number of minutes")
        return 2

    log.info("exam session: checking configuration (.env)")
    problems = check_env(settings, args.tunnel)
    if port_in_use(args.port):
        problems.append(
            Problem(
                "error",
                f"port {args.port} is already in use — is the "
                "server already running? (stop it or pass --port)",
            )
        )
    for problem in problems:
        (log.error if problem.level == "error" else log.warning)("config: %s", problem.message)
    if any(p.level == "error" for p in problems):
        log.error("exam session NOT started — fix the errors above")
        return 2
    log.info(
        "config ok: environment=%s writing_grader=%s analysis_llm=%s",
        settings.environment,
        "gemini" if settings.gemini_api_key.strip() else "manual",
        settings.analysis_llm,
    )

    db_path = sqlite_path_from_url(settings.database_url)
    backup_dir = Path(args.dir)

    if not args.skip_migrate:
        log.info("migrations: alembic upgrade head")
        done = subprocess.run(migrate_command(), cwd=REPO_ROOT, check=False)
        if done.returncode != 0:
            log.error("migrations failed (exit %d) — session NOT started", done.returncode)
            return 1
        log.info("migrations: at head")

    try:
        backup_database(db_path, backup_dir, keep=args.keep, label="presession")
    except BackupError as exc:
        log.error("pre-session backup failed: %s — session NOT started", exc)
        return 1

    cmd = uvicorn_command(
        args.host, args.port, tunnel=args.tunnel, forwarded_allow_ips=args.forwarded_allow_ips
    )
    log.info("starting server: %s", " ".join(cmd[1:]))
    log.info("student link: http://localhost:%d/web/exam.html?test=<test id>", args.port)
    log.info("teacher dashboard: http://localhost:%d/web/teacher.html", args.port)
    if args.tunnel:
        log.info("tunnel: run in a SECOND terminal ->  %s", tunnel_command(args.tunnel, args.port))
        log.info("tunnel: give students https://<tunnel host>/web/exam.html?test=<test id>")
        log.info(
            "proxy: uvicorn trusts X-Forwarded-For only from %s (--proxy-headers "
            "--forwarded-allow-ips). A proxy on ANOTHER host needs its IP there — never "
            "'*' on a directly reachable port",
            args.forwarded_allow_ips,
        )
    every_s = args.every * 60
    log.info("auto backup every %g min -> %s (keep %d)", args.every, backup_dir, args.keep)

    proc = subprocess.Popen(cmd, cwd=REPO_ROOT)
    write_pid_file(args.port)
    exit_code: int | None = None
    interrupted = False
    try:
        exit_code = supervise(
            proc, lambda: _safe_backup(db_path, backup_dir, args.keep, None), every_s
        )
        log.error("server exited by itself (exit %s) — see its log lines above", exit_code)
    except KeyboardInterrupt:
        interrupted = True
        log.info("Ctrl+C — stopping the server (waiting up to %ss)", SHUTDOWN_WAIT_S)
        try:
            exit_code = stop_server(proc)
        except KeyboardInterrupt:  # a second Ctrl+C: stop waiting
            proc.terminate()
            exit_code = stop_server(proc, wait_s=10)
    finally:
        remove_pid_file()
        log.info("final backup")
        _safe_backup(db_path, backup_dir, args.keep, "final")
    log.info("exam session ended (server exit %s)", exit_code)
    # Ctrl+C is the normal way to end a session; a server that died on its own is not.
    return 0 if interrupted else 1


if __name__ == "__main__":
    raise SystemExit(main())
