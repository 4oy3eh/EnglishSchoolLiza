"""Restore the SQLite database from a backup made by `scripts/backup_db.py`.

Usage (from the repo root, with the server STOPPED):
    python -m scripts.restore_db --file backups/english_exam-20261117-101500.db
    make restore file=backups/english_exam-20261117-101500.db

Steps, each logged; any failure stops before the live database is touched:
1. the backup file must pass `PRAGMA integrity_check`;
2. refuse while the API seems to be running (`scripts/runtime_guard.py`: the API
   port answers, or a live `make session` pid file) — never swap a DB under a
   running server;
3. back up the CURRENT database first as `...-prerestore.db` (labeled = never
   rotated), so a wrong restore can itself be undone;
4. copy the backup into the live database through the sqlite3 backup API (it
   also folds in / overwrites a stale `-wal` left by a crash, which a plain file
   copy would replay over the restored data), then verify + log row counts.

Asset blobs (`--with-assets` copies) are not restored by this script: copy the
`backups/assets-.../` folder back to `var/assets` by hand if needed.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from scripts.backup_db import (
    SQLITE_TIMEOUT_S,
    BackupError,
    BackupSettings,
    backup_database,
    format_counts,
    sqlite_path_from_url,
    verify_database,
)
from scripts.runtime_guard import SESSION_PID_FILE, server_running_reason

log = get_logger(__name__)


def restore_database(
    backup_file: Path,
    db_path: Path,
    backup_dir: Path,
    *,
    port: int,
    pid_file: Path = SESSION_PID_FILE,
) -> Path | None:
    """Restore `backup_file` over `db_path`. Returns the pre-restore backup path
    (None when there was no current database). Raises BackupError on refusal."""
    if not backup_file.is_file():
        raise BackupError(f"backup file not found: {backup_file}")
    if backup_file.resolve() == db_path.resolve():
        raise BackupError("the backup file IS the live database — nothing to restore")
    source_counts = verify_database(backup_file)
    log.info("restore source ok: %s integrity=ok %s", backup_file, format_counts(source_counts))

    reason = server_running_reason(port, pid_file)
    if reason is not None:
        raise BackupError(f"refusing to restore: {reason}. Stop the server (Ctrl+C) first.")

    pre: Path | None = None
    if db_path.is_file():
        pre = backup_database(db_path, backup_dir, label="prerestore").path
        log.info("current database saved as %s (undo = restore that file)", pre)
    else:
        log.warning("no current database at %s — restoring into a new file", db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        source = sqlite3.connect(str(backup_file), timeout=SQLITE_TIMEOUT_S)
        try:
            target = sqlite3.connect(str(db_path), timeout=SQLITE_TIMEOUT_S)
            try:
                source.backup(target)
                # Leave the live file self-contained; the app re-enables WAL on connect.
                target.execute("PRAGMA journal_mode=DELETE")
                target.commit()
            finally:
                target.close()
        finally:
            source.close()
    except sqlite3.Error as exc:
        raise BackupError(f"restore into {db_path} failed: {exc}") from exc

    counts = verify_database(db_path)
    log.info("restore ok: %s <- %s integrity=ok %s", db_path, backup_file, format_counts(counts))
    return pre


def main(argv: list[str] | None = None) -> int:
    cfg = BackupSettings()
    parser = argparse.ArgumentParser(description="Restore the SQLite DB from a backup.")
    parser.add_argument("--file", required=True, help="Backup .db file to restore.")
    parser.add_argument("--dir", default=cfg.backup_dir, help="Where the pre-restore copy goes.")
    parser.add_argument(
        "--port", type=int, default=settings.api_port, help="API port to probe (API_PORT)."
    )
    parser.add_argument("--database-url", default=None, help="Override DATABASE_URL.")
    args = parser.parse_args(argv)
    configure_logging(settings.log_level)
    try:
        db_path = sqlite_path_from_url(args.database_url or settings.database_url)
        restore_database(Path(args.file), db_path, Path(args.dir), port=args.port)
    except BackupError as exc:
        log.error("restore failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
