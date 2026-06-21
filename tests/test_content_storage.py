"""Phase 3: asset storage backend (filesystem impl)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.content.storage import FilesystemStorage


def test_put_get_round_trip(tmp_path: Path) -> None:
    storage = FilesystemStorage(tmp_path)
    ref = storage.put("img-1.png", b"\x89PNG\r\n", content_type="image/png")

    assert Path(ref).is_file()
    assert storage.exists("img-1.png")
    assert storage.get("img-1.png") == b"\x89PNG\r\n"


def test_get_missing_raises(tmp_path: Path) -> None:
    storage = FilesystemStorage(tmp_path)
    assert not storage.exists("nope")
    with pytest.raises(FileNotFoundError):
        storage.get("nope")


def test_delete_is_idempotent(tmp_path: Path) -> None:
    storage = FilesystemStorage(tmp_path)
    storage.put("a", b"x")
    storage.delete("a")
    assert not storage.exists("a")
    storage.delete("a")  # no error the second time


def test_put_overwrites(tmp_path: Path) -> None:
    storage = FilesystemStorage(tmp_path)
    storage.put("a", b"one")
    storage.put("a", b"two")
    assert storage.get("a") == b"two"


@pytest.mark.parametrize(
    "bad_id",
    ["../escape", "a/b", "a\\b", "..", ".", "", " spaced ", "a\x08b", "a\x00b", "a\nb"],
)
def test_traversal_blank_and_control_ids_rejected(tmp_path: Path, bad_id: str) -> None:
    # Bad ids fail fast with ValueError, never reaching the filesystem (OSError).
    storage = FilesystemStorage(tmp_path)
    with pytest.raises(ValueError):
        storage.put(bad_id, b"x")
