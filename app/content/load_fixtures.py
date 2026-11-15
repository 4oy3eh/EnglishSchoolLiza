"""Load the committed ``deploy/fixtures/`` tests into the DB (container boot).

Each `deploy/fixtures/<id>.json` is a `contracts.Test`; its blobs live in
`deploy/fixtures/assets/`. They are loaded **published with a demo roster** so a
fresh deploy's student runner has working content immediately — the deploy stands
in for the human-approval gate, exactly like `app/content/seed.py` does for the
self-contained demo. **Non-destructive on re-run**: a test already in the DB is
kept (so a restart never wipes student attempts/answers); set `RELOAD_FIXTURES=1`
to force a clean reload when a fixture's content actually changed. Even then a
test that already has **attempts** is skipped with a WARNING unless
`RELOAD_FIXTURES_FORCE=1` is also set (the guard lives in `load_test`).

Run in the container after `alembic upgrade head`:
    python -m app.content.load_fixtures
"""

from __future__ import annotations

import os
from pathlib import Path

from app.content.load_test import RELOAD_FORCE_ENV, _assets_from_dir, load_test
from app.content.storage import FilesystemStorage
from app.core.config import settings
from app.core.db import Base, SessionLocal, engine
from app.core.logging import configure_logging, get_logger
from app.persistence.repository import ContentRepository
from contracts import Test

log = get_logger(__name__)

FIXTURES_DIR = Path("deploy/fixtures")
DEMO_ROSTER = ["Anna", "Bao", "Carlos", "Mia", "Leo"]


def main() -> None:
    configure_logging(settings.log_level)
    Base.metadata.create_all(engine)  # safe if migrations already ran
    storage = FilesystemStorage(settings.assets_dir)
    assets_dir = FIXTURES_DIR / "assets"
    # Default is NON-destructive: a test already in the DB is kept as-is, so a
    # restart never wipes student attempts/answers. RELOAD_FIXTURES forces a clean
    # reload when content actually changed: "1"/"all" for every test, or a
    # comma/space list of test ids (e.g. "b1-2022") to reload only those.
    force_raw = os.environ.get("RELOAD_FIXTURES", "").strip().lower()
    force_all = force_raw in ("1", "true", "yes", "all")
    force_ids = {t for t in force_raw.replace(",", " ").split() if t} if not force_all else set()
    # Second, separate guard: a reload of a test that already has attempts needs
    # RELOAD_FIXTURES_FORCE=1 too, otherwise load_test skips it (WARNING).
    wipe_attempts = os.environ.get(RELOAD_FORCE_ENV, "").strip() == "1"

    json_files = sorted(FIXTURES_DIR.glob("*.json"))
    if not json_files:
        log.warning("load_fixtures: no fixtures under %s — nothing to load", FIXTURES_DIR)
        return

    session = SessionLocal()
    content = ContentRepository(session)
    try:
        loaded = 0
        skipped = 0
        for jf in json_files:
            test = Test.model_validate_json(jf.read_text(encoding="utf-8"))
            forced = force_all or test.id in force_ids
            if not forced and content.get_test(test.id) is not None:
                log.info("load_fixtures: %s already present — keeping data (no reload)", test.id)
                continue
            assets = _assets_from_dir(test, assets_dir)
            result = load_test(
                session,
                storage,
                test,
                assets,
                roster=DEMO_ROSTER,
                publish=True,
                force=wipe_attempts,
            )
            if result is None:
                skipped += 1  # has attempts; load_test already warned
                continue
            loaded += 1
        session.commit()
        log.info(
            "load_fixtures: %d loaded, %d skipped (attempts present), %d kept",
            loaded,
            skipped,
            len(json_files) - loaded - skipped,
        )
    except Exception:
        session.rollback()
        log.exception("load_fixtures failed")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
