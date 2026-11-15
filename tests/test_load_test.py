"""Tests for the generic item-bank loader (`app/content/load_test.py`).

Guard the contract the manual ingest relies on: it loads as a **draft** by
default (golden rule #5), stores every referenced blob through the
`StorageBackend` the asset route reads, validates that referenced assets exist,
and only publishes / adds a roster when explicitly told to.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.content.load_test import attempt_count, load_test, referenced_asset_ids
from app.content.storage import FilesystemStorage
from app.persistence import models as m
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import (
    Attempt,
    AudioAssetStimulus,
    ImageOption,
    ImageSetStimulus,
    RosterEntry,
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


# --------------------------------------------------------------------------- #
# Reload guard: a test with attempts is never wiped without force.
# --------------------------------------------------------------------------- #
def _add_attempt(session: Session, test_id: str, n: int = 1) -> None:
    repo = AttemptRepository(session)
    for i in range(n):
        repo.add_roster_entry(
            RosterEntry(id=f"{test_id}-r{i}", test_id=test_id, display_name=f"S{i}")
        )
        repo.add_attempt(
            Attempt(
                id=f"{test_id}-a{i}", test_id=test_id, roster_entry_id=f"{test_id}-r{i}", seed=i
            )
        )
    session.flush()


def _attempt_ids(session: Session, test_id: str) -> list[str]:
    return sorted(session.scalars(select(m.AttemptRow.id).where(m.AttemptRow.test_id == test_id)))


def test_reload_skips_test_with_attempts(
    session: Session, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    storage = FilesystemStorage(tmp_path)
    load_test(session, storage, _test(), _assets())
    _add_attempt(session, "t-1", n=2)
    assert attempt_count(session, "t-1") == 2

    changed = _test().model_copy(update={"title": "Changed title"})
    with caplog.at_level(logging.WARNING, logger="app.content.load_test"):
        result = load_test(session, storage, changed, _assets())

    assert result is None
    assert _attempt_ids(session, "t-1") == ["t-1-a0", "t-1-a1"]  # nothing wiped
    test = ContentRepository(session).get_test("t-1")
    assert test is not None and test.title == "Loader test"  # old content kept
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings and "t-1 has 2 attempt(s)" in warnings[0] and "skipping reload" in warnings[0]
    assert "--force" in warnings[0]


def test_reload_force_wipes_attempts_and_replaces(
    session: Session, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    storage = FilesystemStorage(tmp_path)
    load_test(session, storage, _test(), _assets())
    _add_attempt(session, "t-1")

    changed = _test().model_copy(update={"title": "Changed title"})
    with caplog.at_level(logging.WARNING, logger="app.content.load_test"):
        result = load_test(session, storage, changed, _assets(), force=True)

    assert result == "t-1"
    assert attempt_count(session, "t-1") == 0
    test = ContentRepository(session).get_test("t-1")
    assert test is not None and test.title == "Changed title"
    assert any("force=True" in r.getMessage() for r in caplog.records)


def test_reload_without_attempts_needs_no_force(session: Session, tmp_path: Path) -> None:
    storage = FilesystemStorage(tmp_path)
    load_test(session, storage, _test(), _assets())
    changed = _test().model_copy(update={"title": "Changed title"})

    assert load_test(session, storage, changed, _assets()) == "t-1"
    test = ContentRepository(session).get_test("t-1")
    assert test is not None and test.title == "Changed title"


def test_cli_force_flag_is_wired() -> None:
    import argparse

    from app.content import load_test as mod

    captured: dict[str, bool] = {}

    def _fake_parse(self: argparse.ArgumentParser, argv: list[str] | None = None) -> None:
        captured["has_force"] = any("--force" in a.option_strings for a in self._actions)
        raise SystemExit(0)

    original = argparse.ArgumentParser.parse_args
    argparse.ArgumentParser.parse_args = _fake_parse  # type: ignore[method-assign]
    try:
        with pytest.raises(SystemExit):
            mod.main(["--file", "x.json"])
    finally:
        argparse.ArgumentParser.parse_args = original  # type: ignore[method-assign]
    assert captured["has_force"] is True


def test_load_fixtures_env_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RELOAD_FIXTURES=1 alone never wipes attempts; RELOAD_FIXTURES_FORCE=1 does."""
    from sqlalchemy.orm import sessionmaker

    from app.content import load_fixtures as fx
    from app.core.config import settings
    from app.core.db import Base, _make_engine

    fixtures = tmp_path / "fixtures"
    (fixtures / "assets").mkdir(parents=True)
    (fixtures / "t-1.json").write_text(_test().model_dump_json(), encoding="utf-8")
    for asset_id, (data, _ct) in _assets().items():
        (fixtures / "assets" / asset_id).write_bytes(data)

    engine = _make_engine(f"sqlite:///{(tmp_path / 'fx.db').as_posix()}", False)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(fx, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(fx, "engine", engine)
    monkeypatch.setattr(fx, "SessionLocal", factory)
    monkeypatch.setattr(fx, "configure_logging", lambda *_a, **_k: None)  # keep caplog alive
    monkeypatch.setattr(settings, "assets_dir", str(tmp_path / "blobs"))
    monkeypatch.delenv("RELOAD_FIXTURES", raising=False)
    monkeypatch.delenv("RELOAD_FIXTURES_FORCE", raising=False)
    try:
        fx.main()  # first boot: loads + publishes with the demo roster
        with factory() as s:
            test = ContentRepository(s).get_test("t-1")
            assert test is not None and test.status == "published"
            _add_attempt(s, "t-1")
            s.commit()

        fx.main()  # plain restart: kept
        monkeypatch.setenv("RELOAD_FIXTURES", "1")
        fx.main()  # forced reload but attempts exist: skipped by the guard
        with factory() as s:
            assert attempt_count(s, "t-1") == 1

        monkeypatch.setenv("RELOAD_FIXTURES_FORCE", "1")
        fx.main()  # explicit wipe
        with factory() as s:
            assert attempt_count(s, "t-1") == 0
            assert ContentRepository(s).get_test("t-1") is not None
            roster = AttemptRepository(s).list_roster_entries("t-1")
            assert sorted(e.display_name for e in roster) == sorted(fx.DEMO_ROSTER)
    finally:
        engine.dispose()
