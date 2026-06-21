"""Asset storage behind a backend interface (Phase 3.1).

Assets (option/sign images, listening mp3s) are referenced from the contracts
by an opaque `asset_id` only — the bytes live outside the relational store. This
module defines the `StorageBackend` interface the rest of the platform speaks,
with a `FilesystemStorage` implementation for now. MinIO/S3 slots in later
behind the same interface without touching call sites.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from app.core.logging import get_logger

log = get_logger(__name__)


class StorageBackend(ABC):
    """Content-addressed blob store keyed by `asset_id`."""

    @abstractmethod
    def put(self, asset_id: str, data: bytes, *, content_type: str | None = None) -> str:
        """Store `data` under `asset_id`; return a backend reference (path/URI)."""

    @abstractmethod
    def get(self, asset_id: str) -> bytes:
        """Return the bytes stored under `asset_id`; raise if absent."""

    @abstractmethod
    def exists(self, asset_id: str) -> bool:
        """Whether an asset is stored under `asset_id`."""

    @abstractmethod
    def delete(self, asset_id: str) -> None:
        """Remove `asset_id` if present (idempotent)."""


def _validate_asset_id(asset_id: str) -> str:
    """Reject ids that could escape the storage root or aren't safe filenames.

    Screens path separators and `.`/`..` (traversal) plus blank/whitespace-padded
    ids and any non-printable/control characters, so a bad id fails fast with a
    clear `ValueError` instead of a late `OSError` from the filesystem.
    """
    if not asset_id or asset_id != asset_id.strip() or not asset_id.isprintable():
        raise ValueError(f"invalid asset_id: {asset_id!r}")
    if "/" in asset_id or "\\" in asset_id or asset_id in {".", ".."}:
        raise ValueError(f"invalid asset_id: {asset_id!r}")
    return asset_id


class FilesystemStorage(StorageBackend):
    """Stores each asset as a flat file `<root>/<asset_id>`.

    `content_type` is accepted for interface parity (MinIO will use it) but not
    persisted here. Asset ids are validated so they cannot escape `root`.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        log.info("storage init backend=filesystem root=%s", self.root)

    def _path(self, asset_id: str) -> Path:
        return self.root / _validate_asset_id(asset_id)

    def put(self, asset_id: str, data: bytes, *, content_type: str | None = None) -> str:
        path = self._path(asset_id)
        path.write_bytes(data)
        log.info(
            "asset put id=%s bytes=%d content_type=%s", asset_id, len(data), content_type
        )
        return str(path)

    def get(self, asset_id: str) -> bytes:
        path = self._path(asset_id)
        if not path.is_file():
            log.info("asset get id=%s -> miss", asset_id)
            raise FileNotFoundError(f"asset not found: {asset_id}")
        data = path.read_bytes()
        log.info("asset get id=%s -> hit bytes=%d", asset_id, len(data))
        return data

    def exists(self, asset_id: str) -> bool:
        return self._path(asset_id).is_file()

    def delete(self, asset_id: str) -> None:
        path = self._path(asset_id)
        path.unlink(missing_ok=True)
        log.info("asset delete id=%s", asset_id)
