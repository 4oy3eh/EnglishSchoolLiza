"""Phase 12: demo seed.

The seed loads one human-approved demo test (the shape ingestion produces) so the
runner/dashboard have real content for the wire-up E2E. These guard that it
publishes (golden rule #5 — never auto-publish raw), stores its blobs through the
`StorageBackend` the asset route reads, builds a roster, and is idempotent.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.content.seed import SEED_ROSTER, SEED_TEST_ID, sample_assets, seed
from app.content.storage import FilesystemStorage
from app.persistence.repository import AttemptRepository, ContentRepository


def test_seed_publishes_test_with_all_item_types(
    session: Session, tmp_path: Path
) -> None:
    storage = FilesystemStorage(tmp_path)

    seed(session, storage)

    test = ContentRepository(session).get_test(SEED_TEST_ID)
    assert test is not None
    assert test.status == "published"  # human-approval gate ran
    item_types = {item.item_type for s in test.sections for item in s.items}
    assert item_types == {"single_choice", "gap_fill", "matching", "open_writing"}


def test_seed_stores_assets_for_the_asset_route(
    session: Session, tmp_path: Path
) -> None:
    storage = FilesystemStorage(tmp_path)

    seed(session, storage)

    for asset_id, (data, _ct) in sample_assets().items():
        assert storage.exists(asset_id)
        assert storage.get(asset_id) == data


def test_seed_builds_roster(session: Session, tmp_path: Path) -> None:
    seed(session, FilesystemStorage(tmp_path))

    roster = AttemptRepository(session).list_roster_entries(SEED_TEST_ID)
    assert sorted(e.display_name for e in roster) == sorted(SEED_ROSTER)


def test_seed_is_idempotent(session: Session, tmp_path: Path) -> None:
    storage = FilesystemStorage(tmp_path)

    seed(session, storage)
    seed(session, storage)  # re-run must replace, not duplicate

    assert len(ContentRepository(session).list_tests()) == 1
    roster = AttemptRepository(session).list_roster_entries(SEED_TEST_ID)
    assert len(roster) == len(SEED_ROSTER)
