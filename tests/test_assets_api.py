"""Phase 12: asset-serving HTTP surface.

Gate (docs/PROMPTS.md Prompt 12 wire-up): the student runner references stimulus
blobs as `/assets/{asset_id}` (`<img>`/`<audio>`); this route must resolve them
through the shared `StorageBackend`. Closes the Phase-11 known gap.

Exercised with FastAPI's TestClient over a temp `FilesystemStorage`, so the
wiring (router -> dependency -> backend) is real. No answer key is involved —
assets are stimulus content, served openly behind the share link.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.content.storage import FilesystemStorage, StorageBackend
from app.core.db import get_session
from apps.api.assets import get_storage
from apps.api.main import app

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # PNG magic + filler
_MP3 = b"ID3\x04\x00" + b"\x00" * 32  # mp3 with an ID3 tag


@pytest.fixture
def client(tmp_path: Path, session: Session) -> Iterator[tuple[TestClient, StorageBackend]]:
    storage = FilesystemStorage(tmp_path)

    def _override() -> Iterator[StorageBackend]:
        yield storage

    app.dependency_overrides[get_storage] = _override
    # The route asks the DB which assets are study stimuli (refused here): an
    # isolated in-memory DB, never the developer's english_exam.db.
    app.dependency_overrides[get_session] = lambda: session
    try:
        yield TestClient(app), storage
    finally:
        app.dependency_overrides.pop(get_storage, None)
        app.dependency_overrides.pop(get_session, None)


def test_serves_png_with_sniffed_content_type(
    client: tuple[TestClient, StorageBackend],
) -> None:
    c, storage = client
    storage.put("opt-a", _PNG)  # opaque id, no extension -> sniffed

    resp = c.get("/assets/opt-a")

    assert resp.status_code == 200
    assert resp.content == _PNG
    assert resp.headers["content-type"] == "image/png"
    # Revalidated on every use (an import `replace` may overwrite the blob).
    assert resp.headers["cache-control"] == "public, no-cache"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["etag"].startswith('"')


def test_content_type_from_extension(
    client: tuple[TestClient, StorageBackend],
) -> None:
    c, storage = client
    storage.put("track1.mp3", _MP3)

    resp = c.get("/assets/track1.mp3")

    assert resp.status_code == 200
    assert resp.content == _MP3
    assert resp.headers["content-type"] == "audio/mpeg"


def test_missing_asset_is_404(client: tuple[TestClient, StorageBackend]) -> None:
    c, _ = client
    resp = c.get("/assets/nope")
    assert resp.status_code == 404


def test_traversal_unsafe_id_is_404_not_500(
    client: tuple[TestClient, StorageBackend],
) -> None:
    # A backend `ValueError` (traversal-unsafe id) must surface as 404, never 500,
    # and never reveal why.
    c, _ = client
    resp = c.get("/assets/..%2f..%2fetc%2fpasswd")
    assert resp.status_code == 404


def test_range_request_returns_206_partial(
    client: tuple[TestClient, StorageBackend],
) -> None:
    # Audio seek (resume forward) needs HTTP Range support on the asset route.
    c, storage = client
    blob = bytes(range(256))
    storage.put("clip.mp3", blob, content_type="audio/mpeg")

    resp = c.get("/assets/clip.mp3", headers={"Range": "bytes=100-149"})
    assert resp.status_code == 206
    assert resp.content == blob[100:150]
    assert resp.headers["content-range"] == "bytes 100-149/256"
    assert resp.headers["accept-ranges"] == "bytes"


def test_unsatisfiable_range_returns_416(
    client: tuple[TestClient, StorageBackend],
) -> None:
    c, storage = client
    storage.put("clip.mp3", b"abc", content_type="audio/mpeg")

    resp = c.get("/assets/clip.mp3", headers={"Range": "bytes=999-1000"})
    assert resp.status_code == 416
    assert resp.headers["content-range"] == "bytes */3"


def test_etag_revalidation_and_replaced_blob(
    client: tuple[TestClient, StorageBackend],
) -> None:
    """304 while unchanged; a blob replaced under the same id gets a new ETag, so
    the browser never keeps serving the old picture / recording."""
    c, storage = client
    storage.put("sign.png", _PNG)
    first = c.get("/assets/sign.png")
    etag = first.headers["etag"]
    again = c.get("/assets/sign.png", headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b""
    assert again.headers["etag"] == etag
    assert again.headers["x-content-type-options"] == "nosniff"
    assert c.get("/assets/sign.png", headers={"If-None-Match": f"W/{etag}"}).status_code == 304

    new_blob = _PNG + b"\x01\x02"
    storage.put("sign.png", new_blob)  # e.g. an import with `replace`
    fresh = c.get("/assets/sign.png", headers={"If-None-Match": etag})
    assert fresh.status_code == 200 and fresh.content == new_blob
    assert fresh.headers["etag"] != etag


def test_range_and_416_carry_nosniff_and_etag(
    client: tuple[TestClient, StorageBackend],
) -> None:
    c, storage = client
    storage.put("clip.mp3", bytes(range(64)))
    part = c.get("/assets/clip.mp3", headers={"Range": "bytes=0-9"})
    bad = c.get("/assets/clip.mp3", headers={"Range": "bytes=999-"})
    for resp in (part, bad):
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["etag"]


class _NoVersionStorage(FilesystemStorage):
    def version(self, asset_id: str) -> str | None:
        return None


def test_etag_falls_back_to_content_hash(tmp_path: Path, session: Session) -> None:
    storage = _NoVersionStorage(tmp_path)
    storage.put("a.png", _PNG)
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_session] = lambda: session
    try:
        c = TestClient(app)
        etag = c.get("/assets/a.png").headers["etag"]
        assert c.get("/assets/a.png", headers={"If-None-Match": etag}).status_code == 304
        storage.put("a.png", _PNG + b"x")
        assert c.get("/assets/a.png", headers={"If-None-Match": etag}).status_code == 200
    finally:
        app.dependency_overrides.pop(get_storage, None)
        app.dependency_overrides.pop(get_session, None)


def test_extensionless_aac_is_not_labelled_mp3(
    client: tuple[TestClient, StorageBackend],
) -> None:
    c, storage = client
    storage.put("adts", b"\xff\xf1\x50\x80" + b"\x00" * 16)
    storage.put("mp3frame", b"\xff\xfb\x90\x64" + b"\x00" * 16)
    assert c.get("/assets/adts").headers["content-type"] == "application/octet-stream"
    assert c.get("/assets/mp3frame").headers["content-type"] == "audio/mpeg"
