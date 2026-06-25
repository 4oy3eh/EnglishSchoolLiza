"""Export tests from the DB to ``deploy/fixtures/`` (test.json + asset blobs).

`python -m app.content.export_fixtures` reads the named tests and their
referenced assets and writes `deploy/fixtures/<id>.json` + the blobs into
`deploy/fixtures/assets/`. These are committed (the mp3s via git-lfs) so a fresh
container/web deploy has the real content **without** the local ingest source
files. The container loads them on boot via `app.content.load_fixtures`.

Dev tool: run it after re-ingesting/changing a test, then commit the fixtures.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from app.content.load_test import referenced_asset_ids
from app.core.config import settings
from app.core.db import SessionLocal
from app.core.logging import configure_logging, get_logger
from app.persistence.repository import ContentRepository

log = get_logger(__name__)

FIXTURES_DIR = Path("deploy/fixtures")
# Tests shipped with the deploy (the demo `seed-a2-demo` is seeded separately).
EXPORT_TESTS = ("a2-2022", "b1-2022", "movers-vol2")


def main() -> None:
    configure_logging(settings.log_level)
    assets_src = Path(settings.assets_dir)
    out_assets = FIXTURES_DIR / "assets"
    out_assets.mkdir(parents=True, exist_ok=True)

    session = SessionLocal()
    repo = ContentRepository(session)
    copies = 0
    exported = 0
    try:
        for test_id in EXPORT_TESTS:
            test = repo.get_test(test_id)
            if test is None:
                log.warning("export: test %s not in DB — skipping", test_id)
                continue
            (FIXTURES_DIR / f"{test_id}.json").write_text(
                test.model_dump_json(indent=2), encoding="utf-8"
            )
            ids = referenced_asset_ids(test)
            for asset_id in sorted(ids):
                src = assets_src / asset_id
                if not src.is_file():
                    raise FileNotFoundError(f"asset {asset_id!r} missing under {assets_src}")
                shutil.copyfile(src, out_assets / asset_id)
                copies += 1
            exported += 1
            log.info("export: %s -> json + %d assets", test_id, len(ids))
    finally:
        session.close()
    log.info("export complete: %d test(s), %d asset copies -> %s", exported, copies, FIXTURES_DIR)


if __name__ == "__main__":
    main()
