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

from app.content.storage import FilesystemStorage, StorageBackend
from apps.api.assets import get_storage
from apps.api.main import app

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # PNG magic + filler
_MP3 = b"ID3\x04\x00" + b"\x00" * 32  # mp3 with an ID3 tag


@pytest.fixture
def client(tmp_path: Path) -> Iterator[tuple[TestClient, StorageBackend]]:
    storage = FilesystemStorage(tmp_path)

    def _override() -> Iterator[StorageBackend]:
        yield storage

    app.dependency_overrides[get_storage] = _override
    try:
        yield TestClient(app), storage
    finally:
        app.dependency_overrides.pop(get_storage, None)


def test_serves_png_with_sniffed_content_type(
    client: tuple[TestClient, StorageBackend],
) -> None:
    c, storage = client
    storage.put("opt-a", _PNG)  # opaque id, no extension -> sniffed

    resp = c.get("/assets/opt-a")

    assert resp.status_code == 200
    assert resp.content == _PNG
    assert resp.headers["content-type"] == "image/png"
    assert "max-age" in resp.headers.get("cache-control", "")


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
