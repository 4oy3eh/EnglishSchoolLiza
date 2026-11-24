"""Phase 17b: study CELLS (`SectionStudy.images`) — a memory sheet cut per symbol so it
stays legible on a phone.

Same gating as the single study picture (tests/test_iq_hardening.py), per index:
`ClientStudyStimulus.image_urls` = `.../study/asset/{i}` (never an asset id); attempt
token; only inside the study window (409 `study_not_open` before / after); no-store;
404 for an index the study does not have; the public `/assets/{id}` refuses every
cell; the teacher preview has its own per-index route. A study with only `asset_id`
keeps working (backward compatibility); the contract needs a picture, cells or text.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.admin import AdminService, build_admin_service
from app.analysis import MockAnalysisLLM
from app.content.editor import referenced_asset_ids, validate_test_data
from app.content.service import ContentService
from app.content.storage import FilesystemStorage, StorageBackend
from app.core.db import get_session
from app.persistence import models as m
from app.persistence.repository import AttemptRepository, ContentRepository
from apps.api import admin as admin_api
from apps.api import assets as assets_api
from apps.api.main import app
from contracts import ClientStudyStimulus, RosterEntry, SectionStudy
from contracts import Test as ExamTest
from tests import iq_fixture as fx
from tests import test_iq_hardening as hard

CELLS = [f"iq-cell-{k}.png" for k in range(3)]
CELL_PNG = {name: b"\x89PNG\r\n\x1a\n" + bytes([k + 2]) * 40 for k, name in enumerate(CELLS)}


def _data() -> dict[str, Any]:
    data = fx.iq_test_json(hard.TID)
    data["sections"][2]["study"] = {"images": list(CELLS), "study_seconds": 20}
    return data


@pytest.fixture
def exam(
    session: Session, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, dict[str, str], str]]:
    """Like tests/test_iq_hardening.py's `exam`, with a cells-only study."""
    storage = FilesystemStorage(tmp_path)
    content = ContentService(ContentRepository(session), storage)
    content.import_test(_data(), {**fx.ASSETS, **CELL_PNG})
    assert content.publish(hard.TID)
    attempts = AttemptRepository(session)
    attempts.add_roster_entry(RosterEntry(id="c-r1", test_id=hard.TID, display_name="Ann"))
    session.commit()
    entry = attempts.get_roster_entry("c-r1")
    assert entry is not None and entry.pin
    monkeypatch.setattr(admin_api.settings, "teacher_password", hard.PASSWORD)
    monkeypatch.setattr(admin_api.settings, "environment", "test")
    admin_api.login_limiter.reset()

    def _storage() -> Iterator[StorageBackend]:
        yield storage

    def _admin() -> Iterator[AdminService]:
        yield build_admin_service(session, storage, analysis_llm=MockAnalysisLLM())

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[assets_api.get_storage] = _storage
    app.dependency_overrides[admin_api.get_admin_service] = _admin
    try:
        api = TestClient(app, client=("127.0.0.1", 50001))
        start = api.post("/exam/roster/c-r1/start", json={"pin": entry.pin}).json()
        teacher = api.post("/admin/login", json={"password": hard.PASSWORD}).json()["token"]
        yield (
            api,
            {"X-Attempt-Token": start["token"], "Authorization": f"Bearer {teacher}"},
            start["attempt_id"],
        )
    finally:
        app.dependency_overrides.clear()


def test_contract_accepts_cells_only_and_keeps_asset_id() -> None:
    assert SectionStudy.model_validate({"images": ["a.png"], "study_seconds": 5}).images
    assert SectionStudy.model_validate({"asset_id": "a.png", "study_seconds": 5}).images == []
    with pytest.raises(ValidationError):
        SectionStudy.model_validate({"study_seconds": 5})
    test = ExamTest.model_validate(_data())
    assert set(CELLS) <= referenced_asset_ids(test)
    bad = _data()
    bad["sections"][2]["study"]["images"] = ["../x.png"]
    with pytest.raises(Exception, match="карточка"):
        validate_test_data(bad)


def test_cells_are_gated_per_index(
    session: Session, exam: tuple[TestClient, dict[str, str], str]
) -> None:
    api, h, a = exam
    base = f"/exam/attempts/{a}"
    sh = hard._student(h)
    cell = f"{base}/sections/{hard.S3}/study/asset"
    assert ContentRepository(session).study_asset_ids() == frozenset(CELLS)
    for name in CELLS:
        assert api.get(f"/assets/{name}").status_code == 404  # no public back door
    early = api.get(f"{cell}/0", headers=sh)
    assert early.status_code == 409 and early.json()["code"] == "study_not_open"

    hard._to_section(api, sh, base, hard.S3)
    begin = api.post(f"{base}/sections/{hard.S3}/begin", headers=sh)
    study = ClientStudyStimulus.model_validate(begin.json()["study"])
    assert study.image_url is None
    assert study.image_urls == [f"{cell}/{i}" for i in range(len(CELLS))]
    assert not any(name in begin.text for name in CELLS)

    assert api.get(f"{cell}/1").status_code == 401  # token required
    for i, name in enumerate(CELLS):
        got = api.get(f"{cell}/{i}", headers=sh)
        assert got.status_code == 200 and got.content == CELL_PNG[name]
        assert got.headers["cache-control"] == "no-store"
    assert api.get(f"{cell}/{len(CELLS)}", headers=sh).status_code == 404
    assert api.get(f"{cell}/-1", headers=sh).status_code == 404
    assert api.get(cell, headers=sh).status_code == 404  # no single picture here

    past = datetime.now(UTC) - timedelta(seconds=1)
    session.execute(
        update(m.AttemptSectionRow)
        .where(m.AttemptSectionRow.section_id == hard.S3)
        .values(study_ends_at=past)
    )
    session.commit()
    late = api.get(f"{cell}/0", headers=sh)
    assert late.status_code == 409 and late.headers["X-Section-Error"] == "study_not_open"


def test_teacher_preview_serves_cells(exam: tuple[TestClient, dict[str, str], str]) -> None:
    api, h, _a = exam
    url = f"/admin/tests/{hard.TID}/sections/{hard.S3}/study/asset"
    assert api.get(f"{url}/0").status_code == 401
    got = api.get(f"{url}/2", headers=hard._teacher(h))
    assert got.status_code == 200 and got.content == CELL_PNG[CELLS[2]]
    assert got.headers["cache-control"] == "no-store"
    assert api.get(f"{url}/3", headers=hard._teacher(h)).status_code == 404


def test_storage_keeps_the_cells(session: Session, tmp_path: Any) -> None:
    content = ContentService(ContentRepository(session), FilesystemStorage(tmp_path))
    content.import_test(_data(), {**fx.ASSETS, **CELL_PNG})
    stored = ContentRepository(session).get_test(hard.TID)
    assert stored is not None and stored.sections[2].study is not None
    assert stored.sections[2].study.images == CELLS
