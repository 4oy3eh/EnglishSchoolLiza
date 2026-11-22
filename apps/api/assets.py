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

Caching: an import with `replace` may overwrite a blob under the same id, so a
long `max-age` would keep serving the old picture / recording. Responses carry
`Cache-Control: public, no-cache` + a strong `ETag` (the backend's cheap change
token — mtime + size on the filesystem — else the sha256 of the bytes): the
browser revalidates every use and gets a body-less 304 while nothing changed.
`X-Content-Type-Options: nosniff` stops a browser from second-guessing the type.

Study stimuli are the exception (IQ family, Phase 17): an asset referenced by any
test's `Section.study` is a memory stimulus shown only inside its study window, so
this public route answers 404 for it (WARNING) — otherwise the picture could be
re-fetched during the recall. It is served instead by
`GET /exam/attempts/{id}/sections/{sid}/study/asset` (attempt token, window-gated)
and, for the teacher, `GET /admin/tests/{id}/sections/{sid}/study/asset`, both via
`private_asset_response` (`Cache-Control: no-store`).
"""

from __future__ import annotations

import hashlib
import mimetypes
from collections.abc import Iterator
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.content.editor import is_mp3_frame
from app.content.storage import FilesystemStorage, StorageBackend
from app.core.config import settings
from app.core.db import get_session
from app.core.logging import get_logger
from app.persistence.repository import ContentRepository

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


def get_study_asset_ids(
    session: Annotated[Session, Depends(get_session)],
) -> frozenset[str]:
    """FastAPI dependency: every asset id referenced by some test's study stimulus."""
    return ContentRepository(session).study_asset_ids()


StudyAssetsDep = Annotated[frozenset[str], Depends(get_study_asset_ids)]


def _media_type(asset_id: str, data: bytes) -> str:
    """Best-effort content type: extension first, then a magic-byte sniff."""
    guessed, _ = mimetypes.guess_type(asset_id)
    if guessed:
        return guessed
    for prefix, media_type in _SNIFFERS:
        if data.startswith(prefix):
            return media_type
    # mp3 without an ID3 tag starts with a Layer III frame header (not AAC/ADTS).
    if is_mp3_frame(data):
        return "audio/mpeg"
    return _FALLBACK_MEDIA_TYPE


def _etag(storage: StorageBackend, asset_id: str) -> str | None:
    """A strong ETag from the backend's change token (no blob read), or None."""
    try:
        token = storage.version(asset_id)
    except ValueError:
        return None
    return f'"{token}"' if token else None


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    tags = [t.strip().removeprefix("W/") for t in if_none_match.split(",")]
    return "*" in tags or etag in tags


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


def _headers(etag: str) -> dict[str, str]:
    return {
        "Cache-Control": "public, no-cache",
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
    }


def private_asset_response(storage: StorageBackend, asset_id: str, *, who: str) -> Response:
    """The whole blob with `Cache-Control: no-store` (never kept by the browser or a
    proxy) — for assets behind an authenticated, gated route (the study picture).
    Missing / unsafe id -> 404 (WARNING). `who` names the route in the log."""
    try:
        data = storage.get(asset_id)
    except (FileNotFoundError, ValueError) as exc:
        log.warning("%s asset missing -> 404: id=%s (%s)", who, asset_id, exc)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "asset not found") from exc
    media_type = _media_type(asset_id, data)
    log.info("%s asset -> 200 bytes=%d type=%s (no-store)", who, len(data), media_type)
    return Response(
        content=data,
        media_type=media_type,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/assets/{asset_id}")
def serve_asset(
    asset_id: str, request: Request, storage: StorageDep, study_assets: StudyAssetsDep
) -> Response:
    """Serve a stimulus blob by id, with HTTP Range support.

    Range support matters for the listening audio: it lets `<audio>` seek (e.g.
    resume forward to the furthest position) instead of only playing what has
    streamed from the start. Open behind the share link; never an answer key.
    A study stimulus (any test's `Section.study.asset_id`) is NOT served here: 404.
    """
    if asset_id in study_assets:
        log.warning("GET /assets/%s -> 404: study stimulus (gated route only)", asset_id)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "asset not found")
    etag = _etag(storage, asset_id)
    if etag and _etag_matches(request.headers.get("if-none-match"), etag):
        log.info("GET /assets/%s -> 304 (etag %s)", asset_id, etag)
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=_headers(etag))
    try:
        data = storage.get(asset_id)
    except (FileNotFoundError, ValueError) as exc:
        # ValueError = a traversal-unsafe id rejected by the backend; treat both
        # as "no such asset" so the boundary never leaks why (WARNING per CLAUDE).
        log.warning("asset request rejected -> 404: id=%s (%s)", asset_id, exc)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "asset not found") from exc

    media_type = _media_type(asset_id, data)
    total = len(data)
    if etag is None:  # a backend without a change token: hash the bytes we hold
        etag = f'"{hashlib.sha256(data).hexdigest()[:32]}"'
        if _etag_matches(request.headers.get("if-none-match"), etag):
            log.info("GET /assets/%s -> 304 (etag %s)", asset_id, etag)
            return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=_headers(etag))
    base_headers = {"Accept-Ranges": "bytes", **_headers(etag)}

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
