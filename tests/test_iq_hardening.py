"""Phase 17 review hardening: answer size caps, the gated study picture, the IQ
report's `notes`, and the explicit errors that replaced `assert`s.

* **Answer size.** `PUT …/answers/{item}`: a drawing over
  `DRAWING_MAX_RESPONSE_CHARS` -> 413 + `X-Answer-Error: answer_too_large` (WARNING,
  nothing saved); any str over `settings.answer_max_chars` -> 422 (WARNING); a
  colour_task-sized PNG data URL (~1.5 M chars) is still accepted.
* **Study picture.** An asset named by any `Section.study` is refused by the public
  `/assets/{id}` (404); the study payload carries an attempt-scoped `image_url`
  (never the asset id) served with the attempt token, `no-store`, only inside the
  window (409 `study_not_open` after); the teacher previews it through a
  bearer-authenticated admin route.

Synthetic fixture only (`tests/iq_fixture.py`). Nothing under /private or /pdf.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.admin import AdminService, build_admin_service
from app.analysis import MockAnalysisLLM
from app.content.service import ContentService
from app.content.storage import FilesystemStorage, StorageBackend
from app.core.config import settings
from app.core.db import get_session
from app.delivery import AnswerTooLargeError, DeliveryService, NotFoundError
from app.delivery.sections import SectionPlan, resolve
from app.grading.norm_scoring import compute_iq_report
from app.persistence import models as m
from app.persistence.repository import AttemptRepository, ContentRepository
from apps.api import admin as admin_api
from apps.api import assets as assets_api
from apps.api.main import app
from contracts import AttemptSection, ClientStudyStimulus, IQReport, NormScoring, RosterEntry
from contracts.content import DRAWING_MAX_RESPONSE_CHARS
from tests import iq_fixture as fx
from tests.test_iq_family import _norms

TID = "iq-hard"
S1, S2, S3 = f"{TID}-s1", f"{TID}-s2", f"{TID}-s3"
STUDY_PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 48
PASSWORD = "test-teacher-pw"


def _data(*, picture: bool = True) -> dict[str, Any]:
    data = fx.iq_test_json(TID)
    if picture:
        data["sections"][2]["study"]["asset_id"] = "iq-study.png"
    return data


@pytest.fixture
def exam(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, dict[str, str], str]]:
    """A published IQ test whose memory subtest has a study PICTURE, one started
    attempt (token header), the API wired to this session + storage, and a teacher
    bearer token."""
    storage = FilesystemStorage(tmp_path)
    content = ContentService(ContentRepository(session), storage)
    content.import_test(_data(), {**fx.ASSETS, "iq-study.png": STUDY_PNG})
    assert content.publish(TID)
    attempts = AttemptRepository(session)
    attempts.add_roster_entry(RosterEntry(id="h-r1", test_id=TID, display_name="Ann"))
    session.commit()
    entry = attempts.get_roster_entry("h-r1")
    assert entry is not None and entry.pin

    monkeypatch.setattr(admin_api.settings, "teacher_password", PASSWORD)
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
        api = TestClient(app, client=("127.0.0.1", 50000))
        start = api.post("/exam/roster/h-r1/start", json={"pin": entry.pin}).json()
        teacher = api.post("/admin/login", json={"password": PASSWORD}).json()["token"]
        yield (
            api,
            {"X-Attempt-Token": start["token"], "Authorization": f"Bearer {teacher}"},
            start["attempt_id"],
        )
    finally:
        app.dependency_overrides.clear()


def _student(h: dict[str, str]) -> dict[str, str]:
    return {"X-Attempt-Token": h["X-Attempt-Token"]}


def _teacher(h: dict[str, str]) -> dict[str, str]:
    return {"Authorization": h["Authorization"]}


def _to_section(api: TestClient, h: dict[str, str], base: str, target: str) -> None:
    """Begin + finish every section before `target`, then begin `target`."""
    for sid in (S1, S2, S3):
        assert api.post(f"{base}/sections/{sid}/begin", headers=h).status_code == 200
        if sid == target:
            return
        assert api.post(f"{base}/sections/{sid}/finish", headers=h).status_code == 200


# --------------------------------------------------------------------------- #
# 1. Answer size caps.
# --------------------------------------------------------------------------- #
def test_oversized_drawing_is_413_logged_and_not_saved(
    session: Session,
    exam: tuple[TestClient, dict[str, str], str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    api, h, a = exam
    base = f"/exam/attempts/{a}"
    sh = _student(h)
    _to_section(api, sh, base, S2)
    url = f"{base}/answers/{TID}-d1"
    big = '{"w":1,"h":1,"strokes":[]}' + " " * DRAWING_MAX_RESPONSE_CHARS
    with caplog.at_level(logging.WARNING):
        resp = api.put(url, json={"response": big}, headers=sh)
    assert resp.status_code == 413
    assert resp.headers["X-Answer-Error"] == "answer_too_large"
    assert any(
        "answer REJECTED" in r.getMessage() and "cap 300000" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    )
    assert api.get(f"{base}/answers", headers=sh).json() == {}
    # Exactly at the cap is fine (the grader decides what it is worth).
    ok = big[:DRAWING_MAX_RESPONSE_CHARS]
    assert api.put(url, json={"response": ok}, headers=sh).status_code == 204


def test_general_answer_cap_is_422_and_logged(
    exam: tuple[TestClient, dict[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    api, h, a = exam
    base = f"/exam/attempts/{a}"
    sh = _student(h)
    _to_section(api, sh, base, S1)
    monkeypatch.setattr(settings, "answer_max_chars", 1000)
    with caplog.at_level(logging.WARNING):
        resp = api.put(f"{base}/answers/{TID}-r1", json={"response": "x" * 1001}, headers=sh)
    assert resp.status_code == 422 and "answer_too_large" in resp.text
    assert any("answer_max_chars 1000" in r.getMessage() for r in caplog.records)
    ok = api.put(f"{base}/answers/{TID}-r1", json={"response": "1"}, headers=sh)
    assert ok.status_code == 204


def test_colour_task_sized_data_url_is_still_accepted(
    session: Session, tmp_path: Path
) -> None:
    """The general cap sits far above a flattened colour_task PNG (the YLE line
    drawings give 0.5-1.2 M chars; a browser's PNG may be bigger)."""
    assert settings.answer_max_chars >= 3_000_000
    data_url = "data:image/png;base64," + "A" * 1_500_000
    svc = _service(session, tmp_path)
    a = svc.start("h-r1").id
    svc.begin_section(a, S1)
    assert svc.save_answer(a, f"{TID}-r1", data_url).response == data_url  # not a drawing
    svc.finish_section(a, S1)
    svc.begin_section(a, S2)
    with pytest.raises(AnswerTooLargeError) as info:
        svc.save_answer(a, f"{TID}-d1", data_url)  # a drawing: the grader's cap
    assert (info.value.size, info.value.limit) == (len(data_url), DRAWING_MAX_RESPONSE_CHARS)


def _service(session: Session, tmp_path: Path, *, picture: bool = True) -> DeliveryService:
    content = ContentService(ContentRepository(session), FilesystemStorage(tmp_path))
    content.import_test(_data(picture=picture), {**fx.ASSETS, "iq-study.png": STUDY_PNG})
    assert content.publish(TID)
    AttemptRepository(session).add_roster_entry(
        RosterEntry(id="h-r1", test_id=TID, display_name="Ann")
    )
    return DeliveryService(ContentRepository(session), AttemptRepository(session))


# --------------------------------------------------------------------------- #
# 2. The study picture: gated, attempt-scoped, no-store.
# --------------------------------------------------------------------------- #
def test_public_assets_route_refuses_study_assets(
    exam: tuple[TestClient, dict[str, str], str],
) -> None:
    api, _h, _a = exam
    assert api.get("/assets/iq-study.png").status_code == 404
    assert api.get("/assets/iq-pic.png").status_code == 200  # other stimuli unchanged


def test_study_picture_only_through_the_gated_route(
    session: Session,
    exam: tuple[TestClient, dict[str, str], str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    api, h, a = exam
    base = f"/exam/attempts/{a}"
    sh = _student(h)
    pic = f"{base}/sections/{S3}/study/asset"
    early = api.get(pic, headers=sh)  # before the window
    assert early.status_code == 409 and early.json()["code"] == "study_not_open"

    _to_section(api, sh, base, S3)
    begin = api.post(f"{base}/sections/{S3}/begin", headers=sh)
    study = ClientStudyStimulus.model_validate(begin.json()["study"])
    assert study.image_url == pic and "iq-study.png" not in begin.text
    again = api.get(f"{base}/sections/{S3}/study", headers=sh)
    assert again.json()["image_url"] == pic and "asset_id" not in again.json()

    assert api.get(pic).status_code == 401  # the attempt token is required
    got = api.get(pic, headers=sh)
    assert got.status_code == 200 and got.content == STUDY_PNG
    assert got.headers["cache-control"] == "no-store"
    assert got.headers["content-type"] == "image/png"

    past = datetime.now(UTC) - timedelta(seconds=1)
    session.execute(
        update(m.AttemptSectionRow)
        .where(m.AttemptSectionRow.section_id == S3)
        .values(study_ends_at=past)
    )
    session.commit()
    with caplog.at_level(logging.WARNING):
        late = api.get(pic, headers=sh)
    assert late.status_code == 409 and late.json()["code"] == "study_not_open"
    assert late.headers["X-Section-Error"] == "study_not_open"
    assert any("study REFUSED" in r.getMessage() for r in caplog.records)
    assert api.get("/assets/iq-study.png").status_code == 404  # still no back door


def test_text_only_study_has_no_picture_route(session: Session, tmp_path: Path) -> None:
    svc = _service(session, tmp_path, picture=False)
    a = svc.start("h-r1").id
    for sid in (S1, S2):
        svc.begin_section(a, sid)
        svc.finish_section(a, sid)
    begin = svc.begin_section(a, S3)
    assert begin.study is not None and begin.study.image_url is None
    with pytest.raises(NotFoundError):
        svc.section_study_asset(a, S3)
    assert ContentRepository(session).study_asset_ids() == frozenset()


def test_teacher_preview_route_serves_the_study_picture(
    exam: tuple[TestClient, dict[str, str], str],
) -> None:
    api, h, _a = exam
    url = f"/admin/tests/{TID}/sections/{S3}/study/asset"
    assert api.get(url).status_code == 401
    got = api.get(url, headers=_teacher(h))
    assert got.status_code == 200 and got.content == STUDY_PNG
    assert got.headers["cache-control"] == "no-store"
    for other in (f"/admin/tests/{TID}/sections/{S1}", f"/admin/tests/nope/sections/{S3}"):
        assert api.get(f"{other}/study/asset", headers=_teacher(h)).status_code == 404
    # The preview projection still carries only the study length, never the asset.
    preview = api.get(f"/admin/tests/{TID}/preview", headers=_teacher(h))
    assert preview.status_code == 200 and "iq-study.png" not in preview.text


def test_study_asset_ids_lists_every_test(session: Session, tmp_path: Path) -> None:
    _service(session, tmp_path)
    assert ContentRepository(session).study_asset_ids() == frozenset({"iq-study.png"})


# --------------------------------------------------------------------------- #
# 3. IQReport.notes (not "flags").
# --------------------------------------------------------------------------- #
def test_iq_report_says_notes_not_flags() -> None:
    assert "notes" in IQReport.model_fields and "flags" not in IQReport.model_fields
    r = compute_iq_report({}, _norms(), None)
    assert {"no_birth_date", "no_norms_for_age"} <= set(r.notes)
    assert "flags" not in r.model_dump_json()


# --------------------------------------------------------------------------- #
# 4. Explicit errors instead of `assert`, empty norm rows.
# --------------------------------------------------------------------------- #
def test_norm_table_without_rows_is_rejected_and_never_crashes_the_report() -> None:
    norms = _norms()
    raw = norms.model_dump(mode="json")
    raw["by_age"]["rows"] = []
    with pytest.raises(ValidationError):
        NormScoring.model_validate(raw)
    # A table that slipped past validation: no IndexError, just a note per composite.
    empty = norms.model_copy(
        update={"by_age": norms.by_age.model_copy(update={"rows": []})}
    )
    s1, s2, s3 = (f"iq-demo-s{n}" for n in (1, 2, 3))  # the ids `_norms()` uses
    full = compute_iq_report({s1: 3, s2: 2, s3: 2}, empty, 16)
    assert full.age_group == "teen"  # the age group exists - only the rows are missing
    assert {"sum_not_in_table:I", "sum_not_in_table:II"} <= set(full.notes)
    assert full.overall is None and all(c.score is None for c in full.composites)


def test_resolve_raises_value_error_for_a_begun_section_without_deadline() -> None:
    t0 = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)
    plan = SectionPlan(section_id="s1", position=0, time_limit_seconds=60, study_seconds=0)
    broken = AttemptSection(
        attempt_id="a1",
        section_id="s1",
        position=0,
        intro_started_at=t0,
        started_at=t0,
        started_by="student",
        answering_started_at=t0,
        deadline=None,
    )
    with pytest.raises(ValueError, match="without a deadline"):
        resolve(
            [plan],
            {"s1": broken},
            attempt_id="a1",
            attempt_started_at=t0,
            attempt_deadline=None,
            now=t0 + timedelta(seconds=5),
            intro_cap=300,
        )
