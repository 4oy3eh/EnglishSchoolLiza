"""Load the committed ``deploy/fixtures/`` tests into the DB (container boot).

Each `deploy/fixtures/<id>.json` is a `contracts.Test`; its blobs live in
`deploy/fixtures/assets/`. They are loaded **published with a demo roster** so a
fresh deploy's student runner has working content immediately — the deploy stands
in for the human-approval gate, exactly like `app/content/seed.py` does for the
self-contained demo. Idempotent (the loader replaces on re-run).

Run in the container after `alembic upgrade head`:
    python -m app.content.load_fixtures
"""

from __future__ import annotations

from pathlib import Path

from app.content.load_test import _assets_from_dir, load_test
from app.content.storage import FilesystemStorage
from app.core.config import settings
from app.core.db import Base, SessionLocal, engine
from app.core.logging import configure_logging, get_logger
from contracts import Test

log = get_logger(__name__)

FIXTURES_DIR = Path("deploy/fixtures")
DEMO_ROSTER = ["Anna", "Bao", "Carlos", "Mia", "Leo"]


def main() -> None:
    configure_logging(settings.log_level)
    Base.metadata.create_all(engine)  # safe if migrations already ran
    storage = FilesystemStorage(settings.assets_dir)
    assets_dir = FIXTURES_DIR / "assets"

    json_files = sorted(FIXTURES_DIR.glob("*.json"))
    if not json_files:
        log.warning("load_fixtures: no fixtures under %s — nothing to load", FIXTURES_DIR)
        return

    session = SessionLocal()
    try:
        for jf in json_files:
            test = Test.model_validate_json(jf.read_text(encoding="utf-8"))
            assets = _assets_from_dir(test, assets_dir)
            load_test(session, storage, test, assets, roster=DEMO_ROSTER, publish=True)
        session.commit()
        log.info("load_fixtures: loaded %d test(s) from %s", len(json_files), FIXTURES_DIR)
    except Exception:
        session.rollback()
        log.exception("load_fixtures failed")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
