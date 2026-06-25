"""Generic item-bank loader: persist a `contracts.Test` (+ its assets) as a draft.

This is the reusable counterpart to `app/content/seed.py`. Where the seed builds
one hardcoded demo, this loads *any* authored test plus the asset blobs it
references, through the same `ContentService` / `StorageBackend` the asset route
reads. It is the mechanism the manual `pdf`-skill ingest (Option B, see
`docs/INGEST_VIA_CLAUDE_CODE.md`) and any per-test builder hand off to.

Golden rule #5 (human-approval gate): loading defaults to **draft**. Publishing
and roster creation are opt-in (`publish=True` / `roster=...`) and only happen
*after* a human has reviewed the draft.

Two entry points:

* `load_test(session, storage, test, assets, ...)` — the importable API a Python
  builder calls (e.g. `app/content/ingest_a2_2022.py`).
* `python -m app.content.load_test --file <test.json> --assets-dir <dir>` — a CLI
  for tests authored as JSON, reading each referenced asset from `<dir>/<asset_id>`.
"""

from __future__ import annotations

import argparse
import mimetypes
from collections.abc import Iterable, Mapping
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.content.service import ContentService
from app.content.storage import FilesystemStorage, StorageBackend
from app.core.config import settings
from app.core.db import Base, SessionLocal, engine
from app.core.logging import configure_logging, get_logger
from app.persistence import models as m
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import (
    AudioAssetStimulus,
    ColourTaskItem,
    ImageOption,
    ImageSetStimulus,
    RosterEntry,
    SingleChoiceItem,
    Test,
)

log = get_logger(__name__)

# asset_id -> (bytes, content_type). Mirrors `seed.sample_assets()`.
AssetMap = Mapping[str, tuple[bytes, str | None]]


def referenced_asset_ids(test: Test) -> set[str]:
    """Every `asset_id` the test's stimuli and image options point at.

    Used to validate that an authored test does not reference a blob we are not
    about to store (which would 404 in the runner).
    """
    ids: set[str] = set()
    for section in test.sections:
        stimulus = section.stimulus
        # Context images can sit on any text/audio stimulus.
        ids.update(getattr(stimulus, "images", []))
        if isinstance(stimulus, AudioAssetStimulus):
            ids.add(stimulus.asset_id)
            if stimulus.preview_asset_id is not None:
                ids.add(stimulus.preview_asset_id)
        elif isinstance(stimulus, ImageSetStimulus):
            ids.update(stimulus.asset_ids)
        for item in section.items:
            if isinstance(item, SingleChoiceItem):
                if item.image is not None:
                    ids.add(item.image)
                ids.update(o.asset_id for o in item.options if isinstance(o, ImageOption))
            elif isinstance(item, ColourTaskItem):
                ids.add(item.asset_id)
    return ids


def _clear_existing(session: Session, test_id: str) -> None:
    """Drop an existing test plus its roster/attempts so re-loading is idempotent.

    Same dependents the seed wipes (sqlite does not cascade FKs and roster has no
    ORM relationship off `TestRow`): answers + events, attempts, roster, then the
    ORM-cascade delete of the test (which clears its sections + items).
    """
    attempt_ids = list(
        session.scalars(select(m.AttemptRow.id).where(m.AttemptRow.test_id == test_id))
    )
    if attempt_ids:
        session.execute(delete(m.AnswerRow).where(m.AnswerRow.attempt_id.in_(attempt_ids)))
        session.execute(
            delete(m.IntegrityEventRow).where(m.IntegrityEventRow.attempt_id.in_(attempt_ids))
        )
    session.execute(delete(m.AttemptRow).where(m.AttemptRow.test_id == test_id))
    session.execute(delete(m.RosterEntryRow).where(m.RosterEntryRow.test_id == test_id))
    ContentRepository(session).delete_test(test_id)


def _roster_slug(name: str) -> str:
    """A stable, filesystem/url-safe roster-entry id derived from a display name."""
    return "".join(c if c.isalnum() else "-" for c in name.strip().lower()).strip("-")


def load_test(
    session: Session,
    storage: StorageBackend,
    test: Test,
    assets: AssetMap,
    *,
    roster: Iterable[str] = (),
    publish: bool = False,
    replace: bool = True,
) -> str:
    """Validate, store assets, and persist `test` — as a draft by default.

    Raises `ValueError` if the test references an `asset_id` not present in
    `assets` (fail loud rather than ship a 404). `publish`/`roster` are the
    post-review steps and stay opt-in (golden rule #5).
    """
    missing = referenced_asset_ids(test) - set(assets)
    if missing:
        raise ValueError(
            f"test {test.id!r} references {len(missing)} unknown asset(s): {sorted(missing)}"
        )
    if test.status != "draft":
        # The bank's human-approval gate lives in publish(), not in the authored
        # blob: load it as a draft regardless of how it was serialized.
        test = test.model_copy(update={"status": "draft"})

    if replace:
        _clear_existing(session, test.id)

    content = ContentService(ContentRepository(session), storage)
    for asset_id, (data, content_type) in assets.items():
        content.add_asset(asset_id, data, content_type=content_type)
    content.create_test(test)
    log.info(
        "load_test id=%s sections=%d items=%d assets=%d -> draft",
        test.id,
        len(test.sections),
        sum(len(s.items) for s in test.sections),
        len(assets),
    )

    if publish:
        content.publish(test.id)
        log.info("load_test id=%s -> published (human-approved)", test.id)

    names = list(roster)
    if names:
        attempts = AttemptRepository(session)
        for name in names:
            attempts.add_roster_entry(
                RosterEntry(
                    id=f"{test.id}-{_roster_slug(name)}",
                    test_id=test.id,
                    display_name=name,
                )
            )
        log.info("load_test id=%s roster=%d", test.id, len(names))
    return test.id


def _assets_from_dir(test: Test, assets_dir: Path) -> dict[str, tuple[bytes, str | None]]:
    """Read every referenced asset from `<assets_dir>/<asset_id>` for the CLI."""
    out: dict[str, tuple[bytes, str | None]] = {}
    for asset_id in sorted(referenced_asset_ids(test)):
        path = assets_dir / asset_id
        if not path.is_file():
            raise FileNotFoundError(f"asset {asset_id!r} not found at {path}")
        content_type, _ = mimetypes.guess_type(asset_id)
        out[asset_id] = (path.read_bytes(), content_type)
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Load a contracts.Test JSON as a draft.")
    parser.add_argument("--file", required=True, help="Path to the Test JSON file.")
    parser.add_argument(
        "--assets-dir",
        default=settings.assets_dir,
        help="Directory holding each referenced asset as <asset_id> (default: assets_dir).",
    )
    parser.add_argument(
        "--roster",
        default="",
        help="Comma-separated student names to add (only after review).",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish after load (golden rule #5: only after human review).",
    )
    args = parser.parse_args(argv)

    configure_logging(settings.log_level)
    Base.metadata.create_all(engine)
    storage = FilesystemStorage(settings.assets_dir)
    test = Test.model_validate_json(Path(args.file).read_text(encoding="utf-8"))
    assets = _assets_from_dir(test, Path(args.assets_dir))
    roster = [n.strip() for n in args.roster.split(",") if n.strip()]

    session = SessionLocal()
    try:
        test_id = load_test(session, storage, test, assets, roster=roster, publish=args.publish)
        session.commit()
    except Exception:
        session.rollback()
        log.exception("load_test failed")
        raise
    finally:
        session.close()
    log.info(
        "load complete: test=%s status=%s roster=%s",
        test_id,
        "published" if args.publish else "draft",
        ", ".join(roster) or "(none)",
    )


if __name__ == "__main__":
    main()
