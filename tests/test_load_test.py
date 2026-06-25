"""Tests for the generic item-bank loader (`app/content/load_test.py`).

Guard the contract the manual ingest relies on: it loads as a **draft** by
default (golden rule #5), stores every referenced blob through the
`StorageBackend` the asset route reads, validates that referenced assets exist,
and only publishes / adds a roster when explicitly told to.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.content.load_test import load_test, referenced_asset_ids
from app.content.storage import FilesystemStorage
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import (
    AudioAssetStimulus,
    ImageOption,
    ImageSetStimulus,
    Section,
    SingleChoiceItem,
    TextOption,
)
from contracts import Test as ExamTest  # aliased so pytest doesn't collect it as a test class

_AUDIO = "t-listen.mp3"
_IMG = "t-opt-a.png"
_SIGN = "t-sign.png"


def _test() -> ExamTest:
    return ExamTest(
        id="t-1",
        title="Loader test",
        level="A2_KEY",
        status="draft",
        duration_minutes=30,
        sections=[
            Section(
                id="t-sec-1",
                skill="listening",
                stimulus=AudioAssetStimulus(asset_id=_AUDIO),
                items=[
                    SingleChoiceItem(
                        id="t-q1",
                        prompt="Which picture?",
                        options=[
                            ImageOption(key="A", asset_id=_IMG),
                            TextOption(key="B", text="None"),
                        ],
                        correct="A",
                    )
                ],
            ),
            Section(
                id="t-sec-2",
                skill="reading",
                stimulus=ImageSetStimulus(asset_ids=[_SIGN]),
                items=[
                    SingleChoiceItem(
                        id="t-q2",
                        prompt="Choose.",
                        options=[TextOption(key="A", text="x"), TextOption(key="B", text="y")],
                        correct="B",
                    )
                ],
            ),
        ],
    )


def _assets() -> dict[str, tuple[bytes, str | None]]:
    return {
        _AUDIO: (b"ID3audio", "audio/mpeg"),
        _IMG: (b"\x89PNG\r\n\x1a\n", "image/png"),
        _SIGN: (b"\x89PNG\r\n\x1a\n", "image/png"),
    }


def test_referenced_asset_ids_finds_audio_image_and_sign() -> None:
    assert referenced_asset_ids(_test()) == {_AUDIO, _IMG, _SIGN}


def test_referenced_asset_ids_includes_audio_preview() -> None:
    test = _test()
    test.sections[0].stimulus = AudioAssetStimulus(
        asset_id=_AUDIO, preview_asset_id="t-preview.mp3"
    )
    assert "t-preview.mp3" in referenced_asset_ids(test)


def test_load_test_defaults_to_draft(session: Session, tmp_path: Path) -> None:
    load_test(session, FilesystemStorage(tmp_path), _test(), _assets())

    test = ContentRepository(session).get_test("t-1")
    assert test is not None
    assert test.status == "draft"  # no auto-publish (golden rule #5)


def test_load_test_stores_referenced_assets(session: Session, tmp_path: Path) -> None:
    storage = FilesystemStorage(tmp_path)

    load_test(session, storage, _test(), _assets())

    for asset_id, (data, _ct) in _assets().items():
        assert storage.exists(asset_id)
        assert storage.get(asset_id) == data


def test_load_test_raises_on_missing_asset(session: Session, tmp_path: Path) -> None:
    incomplete = dict(_assets())
    del incomplete[_SIGN]

    with pytest.raises(ValueError, match="unknown asset"):
        load_test(session, FilesystemStorage(tmp_path), _test(), incomplete)


def test_load_test_publish_and_roster_are_opt_in(session: Session, tmp_path: Path) -> None:
    load_test(
        session,
        FilesystemStorage(tmp_path),
        _test(),
        _assets(),
        roster=["Anna", "Bao"],
        publish=True,
    )

    test = ContentRepository(session).get_test("t-1")
    assert test is not None and test.status == "published"
    roster = AttemptRepository(session).list_roster_entries("t-1")
    assert sorted(e.display_name for e in roster) == ["Anna", "Bao"]


def test_load_test_is_idempotent(session: Session, tmp_path: Path) -> None:
    storage = FilesystemStorage(tmp_path)

    load_test(session, storage, _test(), _assets())
    load_test(session, storage, _test(), _assets())  # replace, not duplicate

    assert len(ContentRepository(session).list_tests()) == 1
