"""Per-part listening audio: tracks, per-track progress, the endpoint, the editor.

Each listening section may carry its own recording (Cambridge YLE ships one mp3
per part). A different `asset_id` starts a new track, the same one (or a
non-audio stimulus) continues it — so every existing fixture (one long mp3
repeated on each part) is still exactly ONE track. Progress is stored per track
(`attempt_audio_progress`, contract `AudioTrackProgress`); the old single value
keeps working for an old runner.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.content.editor import ContentValidationError, validate_test_data
from app.core.db import get_session
from app.delivery import (
    DeliveryService,
    ExamWindow,
    UnknownTrackError,
    listening_tracks,
    project_test,
)
from app.delivery.audio import track_asset_ids
from app.persistence.repository import AttemptRepository, ContentRepository
from apps.api.main import app
from contracts import (
    AudioAssetStimulus,
    MatchingItem,
    MatchingPoolStimulus,
    PassageTextStimulus,
    PoolOption,
    RosterEntry,
    Section,
    SingleChoiceItem,
    TextOption,
)
from contracts import Test as ExamTest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "deploy" / "fixtures"
BASE = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
TOKEN = "X-Attempt-Token"
PIN = "ABCD"


def _sc(item_id: str) -> SingleChoiceItem:
    return SingleChoiceItem(
        id=item_id,
        prompt="?",
        options=[TextOption(key="A", text="a"), TextOption(key="B", text="b")],
        correct="A",
    )


def _audio(asset: str, preview: str | None = None) -> AudioAssetStimulus:
    return AudioAssetStimulus(asset_id=asset, preview_asset_id=preview)


def per_part_test(test_id: str = "pp") -> ExamTest:
    """Reading, then Listening: part 1 (track 1 + sound-check), part 2 (track 2),
    part 3 (track 2 again = continues), part 4 (matching pool = continues)."""
    return ExamTest(
        id=test_id,
        title="Per-part listening",
        level="A1_MOVERS",
        status="published",
        duration_minutes=30,
        sections=[
            Section(
                id=f"{test_id}-r",
                skill="reading",
                stimulus=PassageTextStimulus(text="Read."),
                items=[_sc(f"{test_id}-r1")],
            ),
            Section(
                id=f"{test_id}-l1",
                skill="listening",
                stimulus=_audio("part1.mp3", "check.mp3"),
                items=[_sc(f"{test_id}-l1q")],
            ),
            Section(
                id=f"{test_id}-l2",
                skill="listening",
                stimulus=_audio("part2.mp3"),
                items=[_sc(f"{test_id}-l2q")],
            ),
            Section(
                id=f"{test_id}-l3",
                skill="listening",
                stimulus=_audio("part2.mp3"),
                items=[_sc(f"{test_id}-l3q")],
            ),
            Section(
                id=f"{test_id}-l4",
                skill="listening",
                stimulus=MatchingPoolStimulus(
                    options=[PoolOption(key="A", text="x"), PoolOption(key="B", text="y")]
                ),
                items=[MatchingItem(id=f"{test_id}-l4q", prompt="?", correct="A")],
            ),
        ],
    )


# --------------------------------------------------------------------------- #
# Track derivation (pure) + backward compatibility with every bundled test.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["a2-2022.json", "b1-2022.json", "movers-vol2.json"])
def test_every_bundled_fixture_is_still_one_track(name: str) -> None:
    test = ExamTest.model_validate(json.loads((FIXTURES / name).read_text("utf-8")))
    tracks = listening_tracks(test.sections)
    assert len(tracks) == 1
    listening = [s.id for s in test.sections if s.skill == "listening"]
    assert list(tracks[0].section_ids) == listening  # incl. A2 Part 5's matching pool
    assert tracks[0].preview_asset_id  # the sound-check stays on the first part
    assert tracks[0].first_section_id == listening[0]
    validate_test_data(test)  # the new editor rules accept them unchanged


def test_tracks_split_on_a_new_file_and_continue_on_the_same_file() -> None:
    tracks = listening_tracks(per_part_test().sections)
    assert [(t.asset_id, t.section_ids) for t in tracks] == [
        ("part1.mp3", ("pp-l1",)),
        ("part2.mp3", ("pp-l2", "pp-l3", "pp-l4")),
    ]
    assert tracks[0].preview_asset_id == "check.mp3"
    assert tracks[1].preview_asset_id is None
    assert tracks[0].plays == 1 and tracks[0].locked
    assert track_asset_ids(per_part_test().sections) == {"part1.mp3", "part2.mp3"}


def test_a_new_listening_block_never_continues_the_previous_track() -> None:
    test = per_part_test()
    reading = test.sections[0].model_copy(update={"id": "pp-r2"})
    again = test.sections[1].model_copy(update={"id": "pp-l5"})
    sections = [*test.sections, reading, again]
    tracks = listening_tracks(sections)
    assert [t.asset_id for t in tracks] == ["part1.mp3", "part2.mp3", "part1.mp3"]
    # ...which the editor refuses (one file = one run of consecutive parts).
    data = test.model_copy(update={"sections": sections}).model_dump(mode="json")
    data["sections"][-2]["items"][0]["id"] = "pp-r2q"
    data["sections"][-1]["items"][0]["id"] = "pp-l5q"
    with pytest.raises(ContentValidationError, match="«part1.mp3» уже звучала"):
        validate_test_data(data)


def test_listening_part_before_any_audio_has_no_track() -> None:
    test = per_part_test()
    pool = test.sections[4].model_copy(update={"id": "pp-l0"})
    sections = [test.sections[0], pool, *test.sections[1:4]]
    tracks = listening_tracks(sections)
    assert "pp-l0" not in {sid for t in tracks for sid in t.section_ids}


def test_editor_refuses_a_b_a_and_preview_equal_to_track() -> None:
    data = per_part_test().model_dump(mode="json")
    validate_test_data(data)
    data["sections"][3]["stimulus"]["asset_id"] = "part1.mp3"  # A, B, A
    with pytest.raises(ContentValidationError, match="одна запись может идти только"):
        validate_test_data(data)
    data = per_part_test().model_dump(mode="json")
    data["sections"][1]["stimulus"]["preview_asset_id"] = "part1.mp3"
    with pytest.raises(ContentValidationError, match="проверка звука не может быть"):
        validate_test_data(data)


def test_projection_serves_each_part_its_own_track_and_no_key() -> None:
    from app.content import build_attempt_layout
    from app.delivery.service import _default_bank

    test = per_part_test()
    client = project_test(test, build_attempt_layout(_default_bank(test), 7))
    by_id = {s.id: s for s in client.sections}
    assert by_id["pp-l1"].stimulus.asset_id == "part1.mp3"  # type: ignore[union-attr]
    assert by_id["pp-l2"].stimulus.asset_id == "part2.mp3"  # type: ignore[union-attr]
    assert listening_tracks(client.sections) == listening_tracks(test.sections)
    assert '"correct"' not in client.model_dump_json()


# --------------------------------------------------------------------------- #
# Storage + service.
# --------------------------------------------------------------------------- #
def _seed(session: Session, test: ExamTest | None = None) -> tuple[DeliveryService, str]:
    test = test or per_part_test()
    content = ContentRepository(session)
    attempts = AttemptRepository(session)
    content.add_test(test)
    attempts.add_roster_entry(
        RosterEntry(id="e1", test_id=test.id, display_name="Alice", pin=PIN)
    )
    session.commit()
    svc = DeliveryService(content, attempts)
    window = ExamWindow(opens_at=BASE - timedelta(hours=1), closes_at=BASE + timedelta(hours=2))
    attempt = svc.start("e1", window, now=BASE)
    session.commit()
    return svc, attempt.id


def test_track_progress_is_per_asset_monotonic_and_in_state(session: Session) -> None:
    svc, attempt_id = _seed(session)
    assert svc.get_state(attempt_id, now=BASE).audio_progress == {}
    assert svc.report_track_progress(attempt_id, "part1.mp3", 12, now=BASE) == {"part1.mp3": 12}
    svc.report_track_progress(attempt_id, "part1.mp3", 4, now=BASE)  # backwards: ignored
    svc.report_track_progress(attempt_id, "part2.mp3", 7, now=BASE)
    state = svc.get_state(attempt_id, now=BASE)
    assert state.audio_progress == {"part1.mp3": 12, "part2.mp3": 7}
    assert state.audio_progress_seconds == 0  # the legacy column is untouched
    # The legacy single value still works (an old runner) and stays separate.
    svc.report_audio_progress(attempt_id, 30, now=BASE)
    state = svc.get_state(attempt_id, now=BASE)
    assert state.audio_progress_seconds == 30
    assert state.audio_progress == {"part1.mp3": 12, "part2.mp3": 7}


def test_unknown_track_is_refused(session: Session) -> None:
    svc, attempt_id = _seed(session)
    for bad in ("check.mp3", "nope.mp3", "../x"):  # a sound-check is not a track
        with pytest.raises(UnknownTrackError):
            svc.report_track_progress(attempt_id, bad, 5, now=BASE)
    assert AttemptRepository(session).get_track_progress(attempt_id) == {}


def test_track_progress_ignored_after_submit_and_wiped_with_the_attempt(
    session: Session,
) -> None:
    svc, attempt_id = _seed(session)
    svc.report_track_progress(attempt_id, "part1.mp3", 9, now=BASE)
    svc.submit(attempt_id, now=BASE)
    assert svc.report_track_progress(attempt_id, "part1.mp3", 50, now=BASE) == {"part1.mp3": 9}
    repo = AttemptRepository(session)
    assert repo.delete_attempt(attempt_id)
    assert repo.get_track_progress(attempt_id) == {}


def test_set_track_progress_upsert_takes_the_max(session: Session) -> None:
    _, attempt_id = _seed(session)
    repo = AttemptRepository(session)
    assert repo.set_track_progress(attempt_id, "part2.mp3", 10) == 10
    assert repo.set_track_progress(attempt_id, "part2.mp3", 3) == 10
    assert repo.set_track_progress(attempt_id, "part2.mp3", -5) == 10
    assert repo.set_track_progress(attempt_id, "part2.mp3", 11) == 11


def test_track_progress_logs_info_once_per_track_then_debug(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """The ~5 s progress posts must not flood the session console: one INFO when a
    track's first point lands, DEBUG afterwards (and DEBUG on the legacy path)."""
    _, attempt_id = _seed(session)
    repo = AttemptRepository(session)
    with caplog.at_level("DEBUG", logger="app.persistence.repository"):
        for sec in (5, 10, 15):
            repo.set_track_progress(attempt_id, "part1.mp3", sec)
        repo.set_audio_progress(attempt_id, 20)
    audio = [r for r in caplog.records if "attempt audio" in r.getMessage()]
    infos = [r for r in audio if r.levelname == "INFO"]
    assert len(infos) == 1 and "started" in infos[0].getMessage()
    assert sum(r.levelname == "DEBUG" for r in audio) == 3


# --------------------------------------------------------------------------- #
# HTTP: /audio-progress (new + legacy shape) and /state.
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    ContentRepository(session).add_test(per_part_test())
    AttemptRepository(session).add_roster_entry(
        RosterEntry(id="e1", test_id="pp", display_name="Alice", pin=PIN)
    )
    session.commit()
    app.dependency_overrides[get_session] = lambda: session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _start(client: TestClient) -> str:
    resp = client.post("/exam/roster/e1/start", json={"pin": PIN})
    assert resp.status_code == 200, resp.text
    client.headers[TOKEN] = resp.json()["token"]
    return resp.json()["attempt_id"]


def test_audio_progress_endpoint_per_track_and_legacy(client: TestClient) -> None:
    attempt_id = _start(client)
    url = f"/exam/attempts/{attempt_id}/audio-progress"
    resp = client.post(url, json={"asset_id": "part1.mp3", "seconds": 20})
    assert resp.status_code == 200 and resp.json() == {"audio_progress": {"part1.mp3": 20}}
    resp = client.post(url, json={"asset_id": "part2.mp3", "seconds": 5})
    assert resp.json() == {"audio_progress": {"part1.mp3": 20, "part2.mp3": 5}}
    # Old runner (no asset_id): the legacy shape, unchanged.
    resp = client.post(url, json={"seconds": 15})
    assert resp.status_code == 200 and resp.json() == {"audio_progress_seconds": 15}

    state = client.get(f"/exam/attempts/{attempt_id}/state").json()
    assert state["audio_progress"] == {"part1.mp3": 20, "part2.mp3": 5}
    assert state["audio_progress_seconds"] == 15


def test_audio_progress_endpoint_refuses_unknown_track_and_bad_token(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    attempt_id = _start(client)
    url = f"/exam/attempts/{attempt_id}/audio-progress"
    with caplog.at_level("WARNING"):
        resp = client.post(url, json={"asset_id": "elsewhere.mp3", "seconds": 5})
    assert resp.status_code == 400
    assert any("not a listening track" in r.getMessage() for r in caplog.records)
    assert client.post(url, json={"asset_id": "", "seconds": 5}).status_code == 422
    resp = client.post(url, json={"asset_id": "part1.mp3", "seconds": 5}, headers={TOKEN: "x"})
    assert resp.status_code == 401
    assert client.get(f"/exam/attempts/{attempt_id}/state").json()["audio_progress"] == {}


# --------------------------------------------------------------------------- #
# Migration b8c9d0e1f2a3 on a scratch DB.
# --------------------------------------------------------------------------- #
def _alembic(db: Path, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db.as_posix()}"}
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        timeout=120,
    )


def _tables(db: Path) -> set[str]:
    with sqlite3.connect(db) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_migration_creates_and_drops_the_track_progress_table(tmp_path: Path) -> None:
    db = tmp_path / "mig.db"
    _alembic(db, "upgrade", "b8c9d0e1f2a3")
    assert "attempt_audio_progress" in _tables(db)
    with sqlite3.connect(db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(attempt_audio_progress)")}
        assert cols == {"attempt_id", "asset_id", "seconds"}
        # The legacy column stays for an old runner.
        assert "audio_progress_seconds" in {
            r[1] for r in conn.execute("PRAGMA table_info(attempts)")
        }
    _alembic(db, "downgrade", "a7b8c9d0e1f2")
    assert "attempt_audio_progress" not in _tables(db)
    _alembic(db, "upgrade", "head")
    assert "attempt_audio_progress" in _tables(db)


@pytest.mark.parametrize(
    "seconds",
    [-1, 86_401, 10**30, 1e30, "NaN", "Infinity", 2.5, "12", None, True],
)
@pytest.mark.parametrize("asset_id", ["part1.mp3", None])
def test_audio_progress_seconds_is_bounded(
    client: TestClient, seconds: object, asset_id: str | None
) -> None:
    """Out-of-range / non-finite seconds -> 422 on both paths (per-track and the
    legacy `{seconds}`), never an SQLite OverflowError 500; nothing is stored."""
    attempt_id = _start(client)
    url = f"/exam/attempts/{attempt_id}/audio-progress"
    body: dict[str, object] = {"seconds": seconds}
    if asset_id:
        body["asset_id"] = asset_id
    if seconds in ("NaN", "Infinity"):  # raw JSON tokens Python's parser accepts
        extra = f', "asset_id": "{asset_id}"' if asset_id else ""
        raw = f'{{"seconds": {seconds}{extra}}}'
        resp = client.post(url, content=raw, headers={"Content-Type": "application/json"})
    else:
        resp = client.post(url, json=body)
    assert resp.status_code == 422, resp.text
    state = client.get(f"/exam/attempts/{attempt_id}/state").json()
    assert state["audio_progress"] == {} and state["audio_progress_seconds"] == 0


def test_audio_progress_seconds_upper_bound_is_accepted(client: TestClient) -> None:
    attempt_id = _start(client)
    url = f"/exam/attempts/{attempt_id}/audio-progress"
    resp = client.post(url, json={"asset_id": "part1.mp3", "seconds": 86_400})
    assert resp.status_code == 200 and resp.json()["audio_progress"]["part1.mp3"] == 86_400
    assert client.post(url, json={"seconds": 0}).status_code == 200
