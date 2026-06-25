"""Asset-serving HTTP surface (Phase 12).

Streams stimulus blobs (listening mp3 / option & sign images) to the browser by
their opaque `asset_id`, resolving the bytes through the same `StorageBackend`
the rest of the platform writes to (`FilesystemStorage` now, MinIO/S3 later
behind the identical interface). This closes the Phase-11 known gap: the student
runner references `/assets/{asset_id}` for `<img>`/`<audio>`, but no route
resolved them until now.

Access: **unauthenticated**, like the rest of delivery (students reach the exam
behind a per-test share link, not a login). This does not breach golden rule #1
— assets are *stimulus* content (the question paper a student already sees),
never the answer key. Only `correct` is secret, and it lives nowhere near a blob.
"""

from __future__ import annotations

import mimetypes
from collections.abc import Iterator
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.content.storage import FilesystemStorage, StorageBackend
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["assets"])

_FALLBACK_MEDIA_TYPE = "application/octet-stream"

# Magic-byte sniffing for the common stimulus types, used only when the
# `asset_id` carries no usable extension (ingested ids may be opaque hashes).
_SNIFFERS: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"ID3", "audio/mpeg"),  # mp3 with an ID3 tag
    (b"RIFF", "audio/wav"),  # wav (RIFF container)
    (b"OggS", "audio/ogg"),
)


@lru_cache(maxsize=1)
def _default_storage() -> StorageBackend:
    """The process-wide filesystem asset store (created once)."""
    return FilesystemStorage(settings.assets_dir)


def get_storage() -> Iterator[StorageBackend]:
    """FastAPI dependency yielding the shared `StorageBackend`."""
    yield _default_storage()


StorageDep = Annotated[StorageBackend, Depends(get_storage)]


def _media_type(asset_id: str, data: bytes) -> str:
    """Best-effort content type: extension first, then a magic-byte sniff."""
    guessed, _ = mimetypes.guess_type(asset_id)
    if guessed:
        return guessed
    for prefix, media_type in _SNIFFERS:
        if data.startswith(prefix):
            return media_type
    # mp3 without an ID3 tag starts with a frame sync (0xFFE...).
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "audio/mpeg"
    return _FALLBACK_MEDIA_TYPE


def _parse_range(range_header: str, total: int) -> tuple[int, int] | None:
    """Parse a single `bytes=start-end` range; clamp to `total`. None if unusable."""
    units, _, spec = range_header.partition("=")
    if units.strip().lower() != "bytes" or "," in spec:  # multi-range unsupported
        return None
    start_s, _, end_s = spec.strip().partition("-")
    try:
        if not start_s:  # suffix range: bytes=-N (last N bytes)
            length = int(end_s)
            start, end = max(0, total - length), total - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else total - 1
    except ValueError:
        return None
    end = min(end, total - 1)
    if start > end or start >= total:
        return None
    return start, end


@router.get("/assets/{asset_id}")
def serve_asset(asset_id: str, request: Request, storage: StorageDep) -> Response:
    """Serve a stimulus blob by id, with HTTP Range support.

    Range support matters for the listening audio: it lets `<audio>` seek (e.g.
    resume forward to the furthest position) instead of only playing what has
    streamed from the start. Open behind the share link; never an answer key.
    """
    try:
        data = storage.get(asset_id)
    except (FileNotFoundError, ValueError) as exc:
        # ValueError = a traversal-unsafe id rejected by the backend; treat both
        # as "no such asset" so the boundary never leaks why (WARNING per CLAUDE).
        log.warning("asset request rejected -> 404: id=%s (%s)", asset_id, exc)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "asset not found") from exc

    media_type = _media_type(asset_id, data)
    total = len(data)
    base_headers = {"Accept-Ranges": "bytes", "Cache-Control": "public, max-age=3600"}

    range_header = request.headers.get("range")
    if range_header:
        rng = _parse_range(range_header, total)
        if rng is None:
            log.warning("GET /assets/%s -> 416 range=%r total=%d", asset_id, range_header, total)
            return Response(
                status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
                headers={**base_headers, "Content-Range": f"bytes */{total}"},
            )
        start, end = rng
        chunk = data[start : end + 1]
        log.info("GET /assets/%s -> 206 bytes=%d-%d/%d", asset_id, start, end, total)
        return Response(
            content=chunk,
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            media_type=media_type,
            headers={**base_headers, "Content-Range": f"bytes {start}-{end}/{total}"},
        )

    log.info("GET /assets/%s -> 200 bytes=%d type=%s", asset_id, total, media_type)
    return Response(content=data, media_type=media_type, headers=base_headers)
