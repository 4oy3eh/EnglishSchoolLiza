"""Admin HTTP surface of the content editor: update / reorder / add / remove,
duplicate, JSON export / import (multipart), asset upload and the keyless preview.

Every route requires the teacher token; refusals are 400 / 404 / 409 with the
content engine's Russian message; imports and copies land in the review queue
(draft), and approval stays the only publish path (golden rule #5). The preview is
the student projection — it never carries an answer key (golden rule #1).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.admin import AdminService, build_admin_service
from app.analysis import MockAnalysisLLM
from app.content import ContentService, FilesystemStorage
from app.content.seed import SEED_TEST_ID, build_sample_test, sample_assets
from app.core.db import get_session
from app.persistence.repository import AttemptRepository, ContentRepository
from apps.api import admin as admin_api
from apps.api import assets as assets_api
from apps.api.main import app
from contracts import Attempt, RosterEntry

PASSWORD = "editor-teacher-pw"
LOOPBACK = ("127.0.0.1", 50000)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
MP3 = b"ID3\x04\x00" + b"\x00" * 64
# (Matching-pool letters on the STIMULUS are student-visible; option `key`s are not.)
KEY_FIELDS = {"correct", "accepted", "accepted_variants", "rejected", "rubric", "grade_mode"}


@pytest.fixture
def storage(tmp_path: Path) -> FilesystemStorage:
    return FilesystemStorage(tmp_path / "assets")


@pytest.fixture
def client(
    session: Session, storage: FilesystemStorage, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setattr(admin_api.settings, "teacher_password", PASSWORD)
    monkeypatch.setattr(admin_api.settings, "environment", "test")
    admin_api.login_limiter.reset()
    for asset_id, (data, ct) in sample_assets().items():
        storage.put(asset_id, data, content_type=ct)
    ContentService(ContentRepository(session), storage).create_test(build_sample_test())
    session.commit()
    app.dependency_overrides[get_session] = lambda: session

    def _dep() -> Iterator[AdminService]:
        yield build_admin_service(session, storage, analysis_llm=MockAnalysisLLM())

    app.dependency_overrides[admin_api.get_admin_service] = _dep
    app.dependency_overrides[assets_api.get_storage] = lambda: storage
    try:
        yield TestClient(app, client=LOOPBACK)
    finally:
        app.dependency_overrides.clear()
        admin_api.login_limiter.reset()


@pytest.fixture
def auth(client: TestClient) -> dict[str, str]:
    token = client.post("/admin/login", json={"password": PASSWORD}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _item(client: TestClient, auth: dict, item_id: str, test_id: str = SEED_TEST_ID) -> dict:
    test = client.get(f"/admin/tests/{test_id}", headers=auth).json()
    for s in test["sections"]:
        for i in s["items"]:
            if i["id"] == item_id:
                return i
    raise AssertionError(item_id)


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(v) for v in value)) if value else set()
    return set()


def _attempt(session: Session) -> None:
    repo = AttemptRepository(session)
    repo.add_roster_entry(RosterEntry(id="r1", test_id=SEED_TEST_ID, display_name="S"))
    repo.add_attempt(
        Attempt(id="a1", test_id=SEED_TEST_ID, roster_entry_id="r1", status="submitted", seed=1)
    )
    session.commit()


# --------------------------------------------------------------------------- #
# Auth.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("patch", f"/admin/tests/{SEED_TEST_ID}"),
        ("patch", f"/admin/tests/{SEED_TEST_ID}/sections/seed-sec-reading"),
        ("post", f"/admin/tests/{SEED_TEST_ID}/sections/reorder"),
        ("post", f"/admin/tests/{SEED_TEST_ID}/sections"),
        ("delete", f"/admin/tests/{SEED_TEST_ID}/sections/seed-sec-vocab"),
        ("post", f"/admin/tests/{SEED_TEST_ID}/sections/seed-sec-reading/items"),
        ("post", f"/admin/tests/{SEED_TEST_ID}/sections/seed-sec-reading/items/reorder"),
        ("put", f"/admin/tests/{SEED_TEST_ID}/items/seed-q-read"),
        ("delete", f"/admin/tests/{SEED_TEST_ID}/items/seed-q-gap"),
        ("post", f"/admin/tests/{SEED_TEST_ID}/duplicate"),
        ("get", f"/admin/tests/{SEED_TEST_ID}/export.json"),
        ("get", f"/admin/tests/{SEED_TEST_ID}/preview"),
        ("post", "/admin/tests/import"),
        ("post", "/admin/assets"),
    ],
)
def test_editor_routes_require_teacher(client: TestClient, method: str, path: str) -> None:
    resp = getattr(client, method)(path)
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Updates.
# --------------------------------------------------------------------------- #
def test_patch_meta_and_russian_400(client: TestClient, auth: dict) -> None:
    r = client.patch(f"/admin/tests/{SEED_TEST_ID}", json={"title": "Новое"}, headers=auth)
    assert r.status_code == 200 and r.json()["title"] == "Новое"
    r = client.patch(
        f"/admin/tests/{SEED_TEST_ID}", json={"duration_minutes": "много"}, headers=auth
    )
    assert r.status_code == 400
    assert "Длительность (мин): должно быть целым числом" in r.json()["detail"]
    r = client.patch("/admin/tests/nope", json={"title": "x"}, headers=auth)
    assert r.status_code == 404 and "не найден" in r.json()["detail"]


def test_put_item_gap_fill_and_option(
    client: TestClient, auth: dict, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    gap = _item(client, auth, "seed-q-gap")
    gap["accepted"] = ["since", "from"]
    gap["rejected"] = ["sins"]
    r = client.put(f"/admin/tests/{SEED_TEST_ID}/items/seed-q-gap", json=gap, headers=auth)
    assert r.status_code == 200
    assert _item(client, auth, "seed-q-gap")["accepted"] == ["since", "from"]
    assert _item(client, auth, "seed-q-gap")["rejected"] == ["sins"]
    exported = client.get(f"/admin/tests/{SEED_TEST_ID}/export.json", headers=auth).json()
    assert exported["sections"][3]["items"][0]["rejected"] == ["sins"]
    preview = client.get(f"/admin/tests/{SEED_TEST_ID}/preview", headers=auth).text
    assert "rejected" not in preview and "sins" not in preview
    choice = _item(client, auth, "seed-q-read")
    choice["options"][0]["text"] = "Only on Mondays"
    r = client.put(f"/admin/tests/{SEED_TEST_ID}/items/seed-q-read", json=choice, headers=auth)
    assert r.status_code == 200
    assert "admin update_item target=seed-a2-demo/seed-q-read ok" in caplog.text
    choice["correct"] = "Q"
    r = client.put(f"/admin/tests/{SEED_TEST_ID}/items/seed-q-read", json=choice, headers=auth)
    assert r.status_code == 400 and "не совпадает ни с одним вариантом" in r.json()["detail"]


def test_sections_and_items_add_remove_reorder(client: TestClient, auth: dict) -> None:
    base = f"/admin/tests/{SEED_TEST_ID}"
    r = client.patch(
        f"{base}/sections/seed-sec-reading",
        json={"stimulus": {"kind": "passage_text", "text": ""}},
        headers=auth,
    )
    assert r.status_code == 200
    r = client.post(
        f"{base}/sections/seed-sec-reading/items",
        json={"item": {"item_type": "gap_fill", "prompt": "new", "accepted": ["x"]}},
        headers=auth,
    )
    assert r.status_code == 200
    ids = [i["id"] for i in r.json()["sections"][0]["items"]]
    r = client.post(
        f"{base}/sections/seed-sec-reading/items/reorder",
        json={"order": list(reversed(ids))},
        headers=auth,
    )
    assert r.status_code == 200
    # A shared-stimulus section refuses (rule #7).
    r = client.post(
        f"{base}/sections/seed-sec-writing/items/reorder",
        json={"order": ["seed-q-write", "seed-q-gap"]},
        headers=auth,
    )
    assert r.status_code == 409 and "менять нельзя" in r.json()["detail"]
    r = client.delete(f"{base}/items/{ids[-1]}", headers=auth)
    assert r.status_code == 200
    r = client.post(
        f"{base}/sections",
        json={
            "section": {
                "title": "New",
                "stimulus": {"kind": "passage_text", "text": "T"},
                "items": [{"item_type": "gap_fill", "prompt": "q", "accepted": ["a"]}],
            }
        },
        headers=auth,
    )
    assert r.status_code == 200 and len(r.json()["sections"]) == 5
    order = [s["id"] for s in r.json()["sections"]][::-1]
    r = client.post(f"{base}/sections/reorder", json={"order": order}, headers=auth)
    assert [s["id"] for s in r.json()["sections"]] == order
    r = client.delete(f"{base}/sections/{order[0]}", headers=auth)
    assert r.status_code == 200 and len(r.json()["sections"]) == 4


def test_published_guard_and_force(client: TestClient, auth: dict, session: Session) -> None:
    client.post(f"/admin/review/{SEED_TEST_ID}/approve", headers=auth)
    _attempt(session)
    item = _item(client, auth, "seed-q-read")
    item["prompt"] = "typo fixed"
    url = f"/admin/tests/{SEED_TEST_ID}/items/seed-q-read"
    r = client.put(url, json=item, headers=auth)
    assert r.status_code == 409 and "уже есть работы учеников (1)" in r.json()["detail"]
    r = client.put(f"{url}?force=true", json=item, headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "published"
    item["correct"] = "B"
    r = client.put(f"{url}?force=true", json=item, headers=auth)
    assert r.status_code == 409 and "черновик-копию" in r.json()["detail"]


def test_draft_with_attempts_is_locked_over_http(
    client: TestClient, auth: dict, session: Session
) -> None:
    _attempt(session)  # the seed test is a draft; an attempt locks it anyway
    item = _item(client, auth, "seed-q-read")
    item["correct"] = "B"
    url = f"/admin/tests/{SEED_TEST_ID}/items/seed-q-read"
    r = client.put(f"{url}?force=true", json=item, headers=auth)
    assert r.status_code == 409 and "Создать черновик-копию" in r.json()["detail"]
    item = _item(client, auth, "seed-q-read")
    item["prompt"] = "typo fixed"
    r = client.put(url, json=item, headers=auth)
    assert r.status_code == 409 and "подтвердить" in r.json()["detail"]
    assert client.put(f"{url}?force=true", json=item, headers=auth).status_code == 200


def test_option_text_swap_refused_on_published(client: TestClient, auth: dict) -> None:
    client.post(f"/admin/review/{SEED_TEST_ID}/approve", headers=auth)
    item = _item(client, auth, "seed-q-read")
    opts = item["options"]
    opts[0]["text"], opts[1]["text"] = opts[1]["text"], opts[0]["text"]
    r = client.put(
        f"/admin/tests/{SEED_TEST_ID}/items/seed-q-read?force=true", json=item, headers=auth
    )
    assert r.status_code == 409 and "варианты ответа" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# Duplicate / review gate.
# --------------------------------------------------------------------------- #
def test_duplicate_lands_in_review_queue(client: TestClient, auth: dict) -> None:
    client.post(f"/admin/review/{SEED_TEST_ID}/approve", headers=auth)
    r = client.post(f"/admin/tests/{SEED_TEST_ID}/duplicate", headers=auth)
    assert r.status_code == 200
    copy_ = r.json()
    assert copy_["id"] == f"{SEED_TEST_ID}-v2" and copy_["status"] == "draft"
    queue = [d["test_id"] for d in client.get("/admin/review", headers=auth).json()]
    assert queue == [copy_["id"]]
    r = client.post(f"/admin/review/{copy_['id']}/approve", headers=auth)
    assert r.json()["status"] == "published"


# --------------------------------------------------------------------------- #
# Export / import.
# --------------------------------------------------------------------------- #
def test_export_json_includes_key(client: TestClient, auth: dict) -> None:
    r = client.get(f"/admin/tests/{SEED_TEST_ID}/export.json", headers=auth)
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert r.json() == build_sample_test().model_dump(mode="json")


def test_import_round_trip_via_http(client: TestClient, auth: dict) -> None:
    exported = client.get(f"/admin/tests/{SEED_TEST_ID}/export.json", headers=auth).content
    r = client.post(
        "/admin/tests/import?new_id=http-copy",
        files={"test": ("t.json", exported, "application/json")},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["test_id"] == "http-copy" and body["status"] == "draft"
    assert "http-copy" in [d["test_id"] for d in client.get("/admin/review", headers=auth).json()]
    again = client.get("/admin/tests/http-copy/export.json", headers=auth).json()
    orig = json.loads(exported)
    assert again["title"] == orig["title"]
    assert [len(s["items"]) for s in again["sections"]] == [
        len(s["items"]) for s in orig["sections"]
    ]
    # Same id again -> 409; replace -> 200 (no attempts).
    r = client.post(
        "/admin/tests/import?new_id=http-copy",
        files={"test": ("t.json", exported, "application/json")},
        headers=auth,
    )
    assert r.status_code == 409 and "уже есть в банке" in r.json()["detail"]
    r = client.post(
        "/admin/tests/import?new_id=http-copy&replace=true",
        files={"test": ("t.json", exported, "application/json")},
        headers=auth,
    )
    assert r.status_code == 200 and r.json()["replaced"] is True
    assert r.json()["unpublished"] is False
    # Replacing a PUBLISHED test (no attempts) drops it back to draft: flagged.
    client.post("/admin/review/http-copy/approve", headers=auth)
    r = client.post(
        "/admin/tests/import?new_id=http-copy&replace=true",
        files={"test": ("t.json", exported, "application/json")},
        headers=auth,
    )
    assert r.status_code == 200 and r.json()["unpublished"] is True
    assert r.json()["status"] == "draft"


def test_import_with_assets_and_missing_report(
    client: TestClient, auth: dict, storage: FilesystemStorage
) -> None:
    data = build_sample_test().model_dump(mode="json")
    data["sections"][1]["stimulus"]["asset_id"] = "imp-audio.mp3"
    data["sections"][1]["items"][0]["options"][1]["asset_id"] = "imp-b.png"
    raw = json.dumps(data).encode()
    r = client.post(
        "/admin/tests/import?new_id=imp",
        files=[("test", ("t.json", raw, "application/json"))],
        headers=auth,
    )
    assert r.status_code == 400
    assert "imp-audio.mp3" in r.json()["detail"] and "imp-b.png" in r.json()["detail"]
    r = client.post(
        "/admin/tests/import?new_id=imp",
        files=[
            ("test", ("t.json", raw, "application/json")),
            ("assets", ("imp-audio.mp3", MP3, "audio/mpeg")),
            ("assets", ("imp-b.png", PNG, "image/png")),
            ("assets", ("../../escape.png", PNG, "image/png")),
        ],
        headers=auth,
    )
    assert r.status_code == 200, r.text
    assert r.json()["stored_assets"] == ["imp-audio.mp3", "imp-b.png"]
    assert r.json()["ignored_assets"] == ["../../escape.png"]
    assert storage.get("imp-b.png") == PNG
    assert not (storage.root.parent / "escape.png").exists()
    assert client.get("/assets/imp-b.png").content == PNG


def test_import_bad_json(client: TestClient, auth: dict) -> None:
    r = client.post(
        "/admin/tests/import",
        files={"test": ("t.json", b"{oops", "application/json")},
        headers=auth,
    )
    assert r.status_code == 400 and "корректным JSON" in r.json()["detail"]
    deep = b"[" * 200_000 + b"]" * 200_000
    r = client.post(
        "/admin/tests/import", files={"test": ("t.json", deep, "application/json")}, headers=auth
    )
    assert r.status_code == 400 and "слишком глубоко вложен" in r.json()["detail"]
    r = client.post("/admin/tests/import", files={"other": ("t.json", b"{}")}, headers=auth)
    assert r.status_code == 400 and "Приложите файл теста" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# Asset upload.
# --------------------------------------------------------------------------- #
def test_asset_upload(
    client: TestClient, auth: dict, monkeypatch: pytest.MonkeyPatch, storage: FilesystemStorage
) -> None:
    r = client.post("/admin/assets", files={"file": ("../x/Sign A.png", PNG)}, headers=auth)
    assert r.status_code == 200
    assert r.json() == {"asset_id": "Sign-A.png", "bytes": len(PNG), "content_type": "image/png"}
    assert client.get("/assets/Sign-A.png").content == PNG
    r = client.post("/admin/assets", files={"file": ("a.svg", b"<svg/>")}, headers=auth)
    assert r.status_code == 400 and "только mp3, png, jpg, webp" in r.json()["detail"]
    r = client.post("/admin/assets", files={"file": ("a.mp3", PNG)}, headers=auth)
    assert r.status_code == 400 and "не похож на mp3" in r.json()["detail"]
    monkeypatch.setattr(admin_api, "ASSET_MAX_BYTES", 16)
    r = client.post("/admin/assets", files={"file": ("big.png", PNG)}, headers=auth)
    assert r.status_code == 400 and "слишком большой" in r.json()["detail"]
    assert not storage.exists("big.png")


# --------------------------------------------------------------------------- #
# Preview (keyless).
# --------------------------------------------------------------------------- #
def test_preview_is_keyless(client: TestClient, auth: dict) -> None:
    r = client.get(f"/admin/tests/{SEED_TEST_ID}/preview", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert not (_keys(body) & KEY_FIELDS), _keys(body) & KEY_FIELDS
    assert "since" not in r.text  # the gap_fill key never appears
    for section in body["sections"]:
        for item in section["items"]:
            assert all("key" not in o for o in item.get("options", []))
    reading = body["sections"][0]["items"][0]
    assert sorted(o["text"] for o in reading["options"]) == [
        "Every day",
        "On Mondays",
        "On weekends",
    ]


# --------------------------------------------------------------------------- #
# Upload limits: request-body cap (413) before buffering, file count, streaming.
# --------------------------------------------------------------------------- #
def _run_asgi(
    middleware_app: Any, path: str, chunks: list[bytes], headers: list[tuple[bytes, bytes]]
) -> tuple[list[dict], int]:
    """Drive the ASGI app by hand; return (sent messages, number of receive calls)."""
    sent: list[dict] = []
    calls = 0
    queue = list(chunks)

    async def receive() -> dict:
        nonlocal calls
        calls += 1
        if not queue:
            return {"type": "http.disconnect"}
        body = queue.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(queue)}

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "root_path": "",
        "headers": headers,
        "query_string": b"",
    }
    asyncio.run(middleware_app(scope, receive, send))
    return sent, calls


async def _reading_app(scope: dict, receive: Any, send: Any) -> None:
    """A stand-in route: reads the whole body, then answers 200."""
    while True:
        msg = await receive()
        if not msg.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def test_oversized_content_length_is_413_before_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin_api, "IMPORT_REQUEST_MAX_BYTES", 1000)
    mw = admin_api.UploadLimitMiddleware(_reading_app)
    sent, calls = _run_asgi(mw, "/admin/tests/import", [b"x"], [(b"content-length", b"5000")])
    assert calls == 0  # the body was never read
    assert sent[0]["status"] == 413
    assert "Импорт слишком большой" in json.loads(sent[1]["body"])["detail"]
    # Other routes are not capped.
    sent, _ = _run_asgi(mw, "/admin/tests/x/items", [b"x"], [(b"content-length", b"5000")])
    assert sent[0]["status"] == 200


def test_lying_content_length_is_caught_by_the_stream_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin_api, "ASSET_REQUEST_MAX_BYTES", 1000)
    mw = admin_api.UploadLimitMiddleware(_reading_app)
    chunks = [b"a" * 600, b"b" * 600, b"c" * 600]
    sent, calls = _run_asgi(mw, "/admin/assets", chunks, [(b"content-length", b"10")])
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]
    assert calls == 2  # stopped at the chunk that crossed the cap
    assert "Файл слишком большой" in json.loads(sent[1]["body"])["detail"]
    # Under the cap: the app's own response goes through.
    sent, _ = _run_asgi(mw, "/admin/assets", [b"a" * 400, b"b" * 400], [])
    assert sent[0]["status"] == 200


def test_import_over_request_cap_is_413_over_http(
    client: TestClient, auth: dict, monkeypatch: pytest.MonkeyPatch, storage: FilesystemStorage
) -> None:
    monkeypatch.setattr(admin_api, "IMPORT_REQUEST_MAX_BYTES", 4096)
    exported = client.get(f"/admin/tests/{SEED_TEST_ID}/export.json", headers=auth).content
    r = client.post(
        "/admin/tests/import?new_id=too-big",
        files=[
            ("test", ("t.json", exported, "application/json")),
            ("assets", ("huge.png", PNG + b"\x00" * 8192, "image/png")),
        ],
        headers=auth,
    )
    assert r.status_code == 413 and "Импорт слишком большой" in r.json()["detail"]
    assert not storage.exists("huge.png")
    assert client.get("/admin/tests/too-big", headers=auth).status_code == 404

    # Chunked body (no Content-Length at all): the streamed count still stops it.
    def chunks() -> Iterator[bytes]:
        for _ in range(8):
            yield b"x" * 1024

    r = client.post(
        "/admin/tests/import",
        content=chunks(),
        headers={**auth, "Content-Type": "multipart/form-data; boundary=zz"},
    )
    assert r.status_code == 413


def test_asset_upload_over_request_cap_is_413(
    client: TestClient, auth: dict, monkeypatch: pytest.MonkeyPatch, storage: FilesystemStorage
) -> None:
    monkeypatch.setattr(admin_api, "ASSET_REQUEST_MAX_BYTES", 1024)
    r = client.post(
        "/admin/assets", files={"file": ("big.png", PNG + b"\x00" * 4096)}, headers=auth
    )
    assert r.status_code == 413 and "Файл слишком большой" in r.json()["detail"]
    assert not storage.exists("big.png")
    r = client.post(
        "/admin/assets", files=[("file", ("a.png", PNG)), ("file", ("b.png", PNG))], headers=auth
    )
    assert r.status_code == 400 and "по одному файлу" in r.json()["detail"]


def test_import_too_many_files_is_400(
    client: TestClient, auth: dict, monkeypatch: pytest.MonkeyPatch, storage: FilesystemStorage
) -> None:
    monkeypatch.setattr(admin_api, "IMPORT_MAX_FILES", 2)
    exported = client.get(f"/admin/tests/{SEED_TEST_ID}/export.json", headers=auth).content
    files = [("test", ("t.json", exported, "application/json"))] + [
        ("assets", (f"f{n}.png", PNG, "image/png")) for n in range(3)
    ]
    r = client.post("/admin/tests/import?new_id=many", files=files, headers=auth)
    assert r.status_code == 400 and "максимум 2" in r.json()["detail"]
    assert client.get("/admin/tests/many", headers=auth).status_code == 404
    # Exactly the limit is fine (unreferenced files are reported, not stored).
    r = client.post("/admin/tests/import?new_id=many", files=files[:3], headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["ignored_assets"] == ["f0.png", "f1.png"]
    assert not storage.exists("f0.png")


def test_upload_is_streamed_not_read_whole(
    client: TestClient, auth: dict, monkeypatch: pytest.MonkeyPatch, storage: FilesystemStorage
) -> None:
    """The route hands the spooled file object to the service, never `bytes`."""
    seen: list[type] = []
    real = admin_api.AdminService.upload_asset

    def spy(self: AdminService, filename: str, data: Any, **kw: Any) -> Any:
        seen.append(type(data))
        return real(self, filename, data, **kw)

    monkeypatch.setattr(admin_api.AdminService, "upload_asset", spy)
    blob = PNG + b"\x07" * (2 * 1024 * 1024)  # > Starlette's 1 MB spool -> on disk
    r = client.post("/admin/assets", files={"file": ("stream.png", blob)}, headers=auth)
    assert r.status_code == 200 and r.json()["bytes"] == len(blob)
    assert seen and seen[0] is not bytes
    assert storage.get("stream.png") == blob


def test_anonymous_upload_is_refused_before_the_body_is_parsed(client: TestClient) -> None:
    r = client.post("/admin/tests/import", files={"test": ("t.json", b"{}")})
    assert r.status_code == 401
