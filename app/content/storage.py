"""Asset storage behind a backend interface (Phase 3.1).

Assets (option/sign images, listening mp3s) are referenced from the contracts
by an opaque `asset_id` only — the bytes live outside the relational store. This
module defines the `StorageBackend` interface the rest of the platform speaks,
with a `FilesystemStorage` implementation for now. MinIO/S3 slots in later
behind the same interface without touching call sites.

Large uploads are streamed: `put_stream` copies a file-like object in chunks (the
filesystem backend writes a temp file under `<root>/.incoming/` and moves it into
place, so a reader never sees a half-written blob); `put_stream(exclusive=True)`
is an atomic create-only reservation of the name (`AssetExistsError` when taken —
the filesystem backend uses a hard link, which fails if the target exists);
`digest` hashes a stored blob in chunks; `version` is a cheap change token
(mtime + size) the asset route turns into an ETag.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO

from app.core.logging import get_logger

log = get_logger(__name__)

CHUNK_BYTES = 1024 * 1024
_INCOMING = ".incoming"


class AssetExistsError(FileExistsError):
    """`put_stream(exclusive=True)` found the asset id already taken."""


def stream_digest(src: BinaryIO) -> str:
    """sha256 hex of a seekable binary stream, read in chunks from the start."""
    src.seek(0)
    h = hashlib.sha256()
    while chunk := src.read(CHUNK_BYTES):
        h.update(chunk)
    src.seek(0)
    return h.hexdigest()


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

    # Streaming helpers. The defaults buffer in memory (correct for any backend);
    # `FilesystemStorage` overrides them to stream in chunks.
    def put_stream(
        self,
        asset_id: str,
        src: BinaryIO,
        *,
        content_type: str | None = None,
        exclusive: bool = False,
    ) -> str:
        """Store a seekable binary stream. `exclusive` = create only (else
        `AssetExistsError`); this default is NOT atomic — backends override it."""
        if exclusive and self.exists(asset_id):
            raise AssetExistsError(asset_id)
        src.seek(0)
        return self.put(asset_id, src.read(), content_type=content_type)

    def digest(self, asset_id: str) -> str:
        """sha256 hex of a stored blob (raises like `get` when absent)."""
        return hashlib.sha256(self.get(asset_id)).hexdigest()

    def version(self, asset_id: str) -> str | None:
        """A cheap token that changes whenever the blob changes, or None (unknown)."""
        return None


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

    def put_stream(
        self,
        asset_id: str,
        src: BinaryIO,
        *,
        content_type: str | None = None,
        exclusive: bool = False,
    ) -> str:
        """Copy `src` in chunks to a temp file, then move it into place.

        `exclusive=True` links the temp file to the final name (`os.link` fails if
        it exists — an atomic create-only reservation, so two concurrent uploads of
        the same name can never overwrite each other); otherwise `os.replace`
        swaps the new blob in atomically."""
        path = self._path(asset_id)
        incoming = self.root / _INCOMING
        incoming.mkdir(exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=incoming, suffix=".part")
        tmp = Path(tmp_name)
        try:
            src.seek(0)
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(src, out, CHUNK_BYTES)
            size = tmp.stat().st_size
            if exclusive:
                try:
                    os.link(tmp, path)
                except FileExistsError as exc:
                    raise AssetExistsError(asset_id) from exc
                except OSError:  # no hard links here (e.g. FAT): exclusive create
                    self._create_exclusive(path, tmp, asset_id)
            else:
                os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        log.info(
            "asset put_stream id=%s bytes=%d content_type=%s exclusive=%s",
            asset_id,
            size,
            content_type,
            exclusive,
        )
        return str(path)

    @staticmethod
    def _create_exclusive(path: Path, tmp: Path, asset_id: str) -> None:
        try:
            with path.open("xb") as out, tmp.open("rb") as src_fh:
                shutil.copyfileobj(src_fh, out, CHUNK_BYTES)
        except FileExistsError as exc:
            raise AssetExistsError(asset_id) from exc

    def digest(self, asset_id: str) -> str:
        path = self._path(asset_id)
        if not path.is_file():
            raise FileNotFoundError(f"asset not found: {asset_id}")
        with path.open("rb") as fh:
            return stream_digest(fh)

    def version(self, asset_id: str) -> str | None:
        try:
            st = self._path(asset_id).stat()
        except (FileNotFoundError, ValueError):
            return None
        return f"{st.st_mtime_ns:x}-{st.st_size:x}"

    def delete(self, asset_id: str) -> None:
        path = self._path(asset_id)
        path.unlink(missing_ok=True)
        log.info("asset delete id=%s", asset_id)
