"""CLI: export / import an authored Test as JSON (+ asset files) — no Python per test.

Thin wrappers over `ContentService.export_test` / `ContentService.import_test` (the
same code the admin endpoints run), so a test produced by Claude Code — or edited by
hand — goes into the bank as a **draft** without writing a builder module:

    python -m app.content.transfer export --id a2-2022 [--out a2-2022.json]
    python -m app.content.transfer import --file test.json [--assets-dir DIR]
                                         [--replace] [--new-id ID]

`make export-test id=...` / `make import-test file=... assets=dir` call these.
Import never publishes (golden rule #5): approve the draft in the teacher review
queue. Asset files are read from `--assets-dir/<asset_id>` for every asset the test
references that is not already stored; a missing one aborts with its id listed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from app.content.editor import ContentEditError, referenced_asset_ids, validate_test_data
from app.content.service import ContentService, ImportOutcome
from app.content.storage import FilesystemStorage, StorageBackend
from app.core.config import settings
from app.core.db import Base, SessionLocal, engine
from app.core.logging import configure_logging, get_logger
from app.persistence.repository import ContentRepository

log = get_logger(__name__)


def export_json(session: Session, storage: StorageBackend, test_id: str) -> str:
    """The authoring Test JSON (with the answer key) for `test_id`."""
    test = ContentService(ContentRepository(session), storage).export_test(test_id)
    return test.model_dump_json(indent=2)


def read_assets_dir(raw: str | bytes, assets_dir: Path | None) -> dict[str, bytes]:
    """Every referenced asset present as `<assets_dir>/<asset_id>` (others skipped —
    they may already be stored; `import_test` reports any still missing)."""
    if assets_dir is None:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}  # import_test reports the bad JSON in Russian
    try:
        test = validate_test_data({**data, "status": "draft"})
    except (ContentEditError, TypeError):
        return {}  # likewise: the import call raises the real validation error
    out: dict[str, bytes] = {}
    for asset_id in sorted(referenced_asset_ids(test)):
        path = assets_dir / asset_id
        if path.is_file():
            out[asset_id] = path.read_bytes()
    log.info("transfer read %d asset file(s) from %s", len(out), assets_dir)
    return out


def import_file(
    session: Session,
    storage: StorageBackend,
    path: Path,
    *,
    assets_dir: Path | None = None,
    replace: bool = False,
    new_id: str | None = None,
) -> ImportOutcome:
    """Import `path` (a Test JSON) as a draft through `ContentService.import_test`."""
    raw = path.read_bytes()
    assets = read_assets_dir(raw, assets_dir)
    return ContentService(ContentRepository(session), storage).import_test(
        raw, assets, replace=replace, new_id=new_id
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export / import a Test JSON (draft).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    exp = sub.add_parser("export", help="Write a test's authoring JSON (with the key).")
    exp.add_argument("--id", required=True, help="Test id.")
    exp.add_argument("--out", default="", help="Output file (default: stdout).")
    imp = sub.add_parser("import", help="Import a Test JSON as a DRAFT.")
    imp.add_argument("--file", required=True, help="Path to the Test JSON.")
    imp.add_argument("--assets-dir", default="", help="Directory with <asset_id> files.")
    imp.add_argument("--replace", action="store_true", help="Replace a test with no attempts.")
    imp.add_argument("--new-id", default="", help="Import under this test id instead.")
    args = parser.parse_args(argv)

    configure_logging(settings.log_level)
    Base.metadata.create_all(engine)
    storage = FilesystemStorage(settings.assets_dir)
    session = SessionLocal()
    try:
        if args.cmd == "export":
            text = export_json(session, storage, args.id)
            if args.out:
                Path(args.out).write_text(text + "\n", encoding="utf-8")
                log.info("export written: test=%s -> %s", args.id, args.out)
            else:
                sys.stdout.write(text + "\n")
            return 0
        outcome = import_file(
            session,
            storage,
            Path(args.file),
            assets_dir=Path(args.assets_dir) if args.assets_dir else None,
            replace=args.replace,
            new_id=args.new_id or None,
        )
        session.commit()
        log.info(
            "import complete: test=%s status=draft replaced=%s assets=%s — approve it in "
            "the teacher review queue to publish",
            outcome.test.id,
            outcome.replaced,
            ", ".join(outcome.stored_assets) or "(none new)",
        )
        return 0
    except ContentEditError as exc:
        session.rollback()
        log.error("%s failed (%d): %s", args.cmd, exc.status, exc)
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
