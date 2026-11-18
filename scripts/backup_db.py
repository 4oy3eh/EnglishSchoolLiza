"""Consistent online backup of the SQLite database (+ optional asset blobs).

Usage (from the repo root):
    python -m scripts.backup_db [--dir backups] [--keep 30] [--with-assets]
    make backup [keep=N] [assets=1]

How it stays consistent while the app is running: the copy is made through the
sqlite3 **online backup API** (`Connection.backup`, all pages in one step = one
read transaction). Under WAL a reader never blocks the writer, so students keep
answering while the snapshot is taken, and the snapshot includes every committed
transaction still sitting in `english_exam.db-wal` — a raw file copy of the `.db`
would miss those (or tear mid-write). The copy is switched to `journal_mode=DELETE`
so it is ONE self-contained file, then verified (`PRAGMA integrity_check` + row
counts) BEFORE it is renamed from `*.partial` to its final name: a backup that
fails verification never appears in the backup directory.

Naming + retention (the stamp is **UTC**, so a DST change can never make an older
copy sort as newer and get the wrong file rotated away; the log line also shows
the local time):
    backups/english_exam-YYYYMMDD-HHMMSS.db            rotated: newest `keep` stay
    backups/english_exam-YYYYMMDD-HHMMSS-<label>.db    labeled: never rotated
Labeled copies (`presession`, `final` from `scripts/session.py`, `prerestore` from
`scripts/restore_db.py`) are milestones and are kept until removed by hand.
`--with-assets` also copies `settings.assets_dir` to
`backups/assets-YYYYMMDD-HHMMSS/` (rotated with the same `keep`).

Failure handling: every file-system error on the way (Defender / an indexer
holding the new file, disk full, a Windows rename race) surfaces as `BackupError`,
never a raw `OSError`, so `scripts/session.py` logs it and carries on; the final
rename gets ONE short retry on `PermissionError` (the usual transient Windows
lock). A failed rotation only logs a WARNING: the new copy is already safe.

Config: `BACKUP_DIR` (default `backups`) and `BACKUP_KEEP` (default 30), from the
process env or `.env` — the CLI flags override both.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.core.logging import configure_logging, get_logger

log = get_logger(__name__)

BACKUP_PREFIX = "english_exam"
# Tables whose row counts are logged after every backup / restore (the exam data
# a teacher would lose). A missing table (fresh / partial schema) logs as absent.
COUNTED_TABLES: tuple[str, ...] = ("attempts", "answers", "attempt_reports", "integrity_events")
# Rotated backups: english_exam-20261117-101500.db or ...-101500-2.db (same second).
_ROTATED_RE = re.compile(rf"^{BACKUP_PREFIX}-(\d{{8}}-\d{{6}})(?:-(\d+))?\.db$")
_ASSETS_RE = re.compile(r"^assets-(\d{8}-\d{6})(?:-(\d+))?$")
_LABEL_RE = re.compile(r"^[a-z][a-z0-9]{0,31}$")
# Same lock wait as the app (`app/core/db.py`): wait out a writer instead of failing.
SQLITE_TIMEOUT_S = 10.0


class BackupSettings(BaseSettings):
    """Backup knobs from the env / `.env` (kept out of the app's `Settings`)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    backup_dir: str = "backups"
    backup_keep: int = 30


class BackupError(RuntimeError):
    """A backup / restore step failed; the message says which and why."""


@dataclass
class BackupResult:
    path: Path
    size_bytes: int
    counts: dict[str, int | None]
    seconds: float
    assets_path: Path | None = None
    pruned: list[Path] = field(default_factory=list)


def sqlite_path_from_url(url: str) -> Path:
    """The database file behind a `sqlite:///...` URL; BackupError for anything else."""
    parsed = make_url(url)
    if not parsed.drivername.startswith("sqlite"):
        raise BackupError(
            f"backups support SQLite only, DATABASE_URL uses {parsed.drivername!r} "
            "(for Postgres use pg_dump)"
        )
    database = parsed.database or ""
    if database in ("", ":memory:") or database.startswith("file::memory:"):
        raise BackupError(
            "DATABASE_URL points at an in-memory SQLite database — nothing to back up"
        )
    return Path(database)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=SQLITE_TIMEOUT_S)
    conn.execute(f"PRAGMA busy_timeout={int(SQLITE_TIMEOUT_S * 1000)}")
    return conn


def verify_database(path: Path) -> dict[str, int | None]:
    """`PRAGMA integrity_check` must answer `ok`; returns row counts of COUNTED_TABLES
    (None = table absent). Raises BackupError on a damaged / unreadable file."""
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=SQLITE_TIMEOUT_S)
    except sqlite3.Error as exc:
        raise BackupError(f"cannot open {path}: {exc}") from exc
    try:
        rows = [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
        if rows != ["ok"]:
            raise BackupError(f"integrity_check failed for {path}: {'; '.join(map(str, rows[:5]))}")
        present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts: dict[str, int | None] = {}
        for table in COUNTED_TABLES:
            counts[table] = (
                conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                if table in present
                else None
            )
        return counts
    except sqlite3.Error as exc:
        raise BackupError(f"cannot verify {path}: {exc}") from exc
    finally:
        conn.close()


def format_counts(counts: dict[str, int | None]) -> str:
    return " ".join(f"{k}={'absent' if v is None else v}" for k, v in counts.items())


def _unique_stem(dest_dir: Path, stamp: str, label: str | None) -> str:
    """`english_exam-<stamp>[-label]`, with `-2`, `-3`... if that second is taken."""
    base = f"{BACKUP_PREFIX}-{stamp}" + (f"-{label}" if label else "")
    stem, n = base, 1
    while (dest_dir / f"{stem}.db").exists() or (dest_dir / f"{stem}.db.partial").exists():
        n += 1
        stem = f"{base}-{n}" if label else f"{BACKUP_PREFIX}-{stamp}-{n}"
    return stem


def snapshot_database(src: Path, dest: Path) -> None:
    """Copy `src` into the NEW file `dest` via the online backup API (consistent
    snapshot, WAL content included), as a single-file DELETE-journal database."""
    if not src.is_file():
        raise BackupError(f"database file not found: {src}")
    source = _connect(src)
    try:
        target = sqlite3.connect(str(dest))
        try:
            # pages=-1 (default): the whole DB in one step = one read transaction,
            # so a concurrent writer can never force a restart or a torn copy.
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
            target.commit()
        finally:
            target.close()
    except sqlite3.Error as exc:
        raise BackupError(f"sqlite backup {src} -> {dest} failed: {exc}") from exc
    finally:
        source.close()


def _sort_key(match: re.Match[str]) -> tuple[str, int]:
    return match.group(1), int(match.group(2) or 1)


def prune_backups(dest_dir: Path, keep: int) -> list[Path]:
    """Delete rotated backups (db files + `assets-*` dirs) beyond the newest `keep`.

    Labeled backups (`...-presession.db`, `...-prerestore.db`) never match the
    rotation pattern and are left alone. `keep <= 0` disables pruning.
    """
    if keep <= 0 or not dest_dir.is_dir():
        return []
    removed: list[Path] = []
    for pattern in (_ROTATED_RE, _ASSETS_RE):
        found = [(m, p) for p in dest_dir.iterdir() if (m := pattern.match(p.name))]
        found.sort(key=lambda mp: _sort_key(mp[0]), reverse=True)
        for _match, path in found[keep:]:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed.append(path)
            log.info("backup rotation: removed %s (keep=%d)", path.name, keep)
    return removed


# One short retry for the final rename: on Windows a scanner (Defender, the search
# indexer) briefly opening the fresh `.partial` makes `replace` fail with
# PermissionError (WinError 5 / 32).
RENAME_RETRY_DELAY_S = 0.5


def _replace_with_retry(src: Path, dest: Path) -> None:
    try:
        src.replace(dest)
    except PermissionError as exc:
        log.warning("backup rename %s -> %s refused (%s), retrying once", src.name, dest.name, exc)
        time.sleep(RENAME_RETRY_DELAY_S)
        src.replace(dest)


def _utc_stamp(now: datetime | None) -> str:
    """`YYYYMMDD-HHMMSS` in UTC (a naive `now` is taken as UTC already)."""
    moment = now or datetime.now(UTC)
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    return moment.strftime("%Y%m%d-%H%M%S")


def backup_database(
    db_path: Path,
    dest_dir: Path,
    *,
    keep: int = 30,
    label: str | None = None,
    assets_dir: Path | None = None,
    now: datetime | None = None,
) -> BackupResult:
    """Snapshot + verify + rotate. Returns where the backup landed and its counts.

    The copy is written as `<name>.db.partial` and only renamed after it passed
    `verify_database`; on any failure the partial file is removed and BackupError
    raised (the previous backups are untouched). Every `OSError` (permission,
    disk full, a held file) is raised as BackupError too; the rename is retried
    once on PermissionError. The file name carries a UTC stamp (`now`, naive =
    UTC; default: the current time).
    """
    if label is not None and not _LABEL_RE.match(label):
        raise BackupError(f"invalid backup label {label!r} (lowercase letters/digits)")
    started = time.monotonic()
    stamp = _utc_stamp(now)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        stem = _unique_stem(dest_dir, stamp, label)
    except OSError as exc:
        raise BackupError(f"cannot prepare backup directory {dest_dir}: {exc}") from exc
    final = dest_dir / f"{stem}.db"
    partial = dest_dir / f"{stem}.db.partial"
    log.info(
        "backup start: %s -> %s (local time %s)",
        db_path,
        final,
        datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
    )
    try:
        snapshot_database(db_path, partial)
        counts = verify_database(partial)
        _replace_with_retry(partial, final)
        size = final.stat().st_size
    except OSError as exc:
        raise BackupError(
            f"backup file step failed ({partial.name} -> {final.name}): {exc}"
        ) from exc
    finally:
        try:
            if partial.exists():
                partial.unlink()
                log.error("backup failed: removed incomplete %s", partial.name)
        except OSError as exc:
            log.error("backup failed and the incomplete %s could not be removed: %s", partial, exc)

    assets_copy: Path | None = None
    if assets_dir is not None:
        try:
            assets_copy = copy_assets(assets_dir, dest_dir, stamp)
        except OSError as exc:
            raise BackupError(
                f"database backup {final.name} is OK, but copying the assets failed: {exc}"
            ) from exc

    pruned: list[Path] = []
    if label is None:
        try:
            pruned = prune_backups(dest_dir, keep)
        except OSError as exc:  # the new copy is safe; an old one just stays a while
            log.warning("backup rotation skipped (%s); the new copy is fine", exc)
    result = BackupResult(
        path=final,
        size_bytes=size,
        counts=counts,
        seconds=time.monotonic() - started,
        assets_path=assets_copy,
        pruned=pruned,
    )
    log.info(
        "backup ok: %s (%.1f KB, %.2fs) integrity=ok %s",
        final,
        result.size_bytes / 1024,
        result.seconds,
        format_counts(counts),
    )
    return result


def copy_assets(assets_dir: Path, dest_dir: Path, stamp: str) -> Path | None:
    """Copy the asset blob directory to `dest_dir/assets-<stamp>` (None if absent)."""
    if not assets_dir.is_dir():
        log.warning("assets dir %s does not exist — no assets copied", assets_dir)
        return None
    target = dest_dir / f"assets-{stamp}"
    n = 1
    while target.exists():
        n += 1
        target = dest_dir / f"assets-{stamp}-{n}"
    shutil.copytree(assets_dir, target)
    files = sum(1 for p in target.rglob("*") if p.is_file())
    log.info("assets copied: %s -> %s (%d files)", assets_dir, target, files)
    return target


def build_parser() -> argparse.ArgumentParser:
    cfg = BackupSettings()
    parser = argparse.ArgumentParser(description="Online backup of the SQLite database.")
    parser.add_argument("--dir", default=cfg.backup_dir, help="Backup directory (BACKUP_DIR).")
    parser.add_argument(
        "--keep", type=int, default=cfg.backup_keep, help="Rotated backups to keep (BACKUP_KEEP)."
    )
    parser.add_argument("--with-assets", action="store_true", help="Also copy ASSETS_DIR.")
    parser.add_argument("--label", default=None, help="Milestone label (never rotated).")
    parser.add_argument("--database-url", default=None, help="Override DATABASE_URL.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(settings.log_level)
    try:
        db_path = sqlite_path_from_url(args.database_url or settings.database_url)
        backup_database(
            db_path,
            Path(args.dir),
            keep=args.keep,
            label=args.label,
            assets_dir=Path(settings.assets_dir) if args.with_assets else None,
        )
    except BackupError as exc:
        log.error("backup failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
