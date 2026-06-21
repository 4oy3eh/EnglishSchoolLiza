"""Content engine facade: item-bank CRUD + assets (Phase 3.1).

`ContentService` ties the persistence `ContentRepository` (tests/sections/items)
together with a `StorageBackend` (asset blobs) so the rest of the platform has a
single entry point for managing the item bank. It deliberately stays thin: row
<-> contract translation lives in the repository, blob handling in the storage
backend. Publishing only flips `draft -> published`; it never invents content
(human approval gates publish, golden rule #5).
"""

from __future__ import annotations

from app.content.storage import StorageBackend
from app.core.logging import get_logger
from app.persistence.repository import ContentRepository
from contracts import Test

log = get_logger(__name__)


class ContentService:
    """CRUD over the item bank plus asset storage."""

    def __init__(self, repo: ContentRepository, storage: StorageBackend) -> None:
        self.repo = repo
        self.storage = storage

    # -- tests / sections / items ------------------------------------------ #
    def create_test(self, test: Test) -> str:
        return self.repo.add_test(test)

    def get_test(self, test_id: str) -> Test | None:
        return self.repo.get_test(test_id)

    def list_tests(self) -> list[Test]:
        return self.repo.list_tests()

    def publish(self, test_id: str) -> bool:
        """Flip a draft to published (human-approved, golden rule #5)."""
        return self.repo.set_status(test_id, "published")

    def unpublish(self, test_id: str) -> bool:
        return self.repo.set_status(test_id, "draft")

    def delete_test(self, test_id: str) -> bool:
        return self.repo.delete_test(test_id)

    # -- assets ------------------------------------------------------------- #
    def add_asset(
        self, asset_id: str, data: bytes, *, content_type: str | None = None
    ) -> str:
        return self.storage.put(asset_id, data, content_type=content_type)

    def get_asset(self, asset_id: str) -> bytes:
        return self.storage.get(asset_id)

    def has_asset(self, asset_id: str) -> bool:
        return self.storage.exists(asset_id)

    def delete_asset(self, asset_id: str) -> None:
        self.storage.delete(asset_id)
