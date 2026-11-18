"""Phase 11: student delivery HTTP surface.

Gate (docs/PROMPTS.md Prompt 11): the runner serves items without a key (golden
rule #1); the timer counts down server-side and a crossed deadline expires the
attempt (golden rule #3); answers persist (displayed -> canonical); submit
finalizes; late actions are rejected. Telemetry ingest stays the Phase-6 sink.

These exercise the router with FastAPI's TestClient over the same in-memory DB
the repositories use, so the wiring (router -> DeliveryService -> repos) is real.

Phase 14: every start goes through the classroom PIN and every attempt call carries
the session token the start returned (`_start` sets it as the client's default
`X-Attempt-Token` header); the PIN / token / lockout rules are tested at the end.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.persistence.repository import AttemptRepository, ContentRepository
from apps.api.main import app
from contracts import (
    Attempt,
    GapFillItem,
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

_FORBIDDEN = {"correct", "accepted", "accepted_variants", "rubric", "grade_mode"}
TOKEN = "X-Attempt-Token"
PINS = {"entry-1": "AB3D", "entry-2": "XY7K"}


def _test() -> ExamTest:
    return ExamTest(
        id="t1",
        title="A2 Key — Mock",
        level="A2_KEY",
        status="published",
        duration_minutes=30,
        sections=[
            Section(
                id="sec-read",
                skill="reading",
                stimulus=PassageTextStimulus(text="Read this."),
                items=[
                    SingleChoiceItem(
                        id="q-sc",
                        prompt="Pick one",
                        options=[
                            TextOption(key="A", text="alpha"),
                            TextOption(key="B", text="bravo"),
                            TextOption(key="C", text="charlie"),
                        ],
                        correct="C",
                    ),
                    GapFillItem(id="q-gap", prompt="Fill", accepted=["house"]),
                ],
            ),
            Section(
                id="sec-match",
                skill="reading",
                stimulus=MatchingPoolStimulus(
                    options=[PoolOption(key="A", text="a"), PoolOption(key="B", text="b")]
                ),
                items=[MatchingItem(id="q-match", prompt="Match", correct="B")],
            ),
        ],
    )


def _assert_no_key(obj: object) -> None:
    if isinstance(obj, dict):
        leaked = _FORBIDDEN & obj.keys()
        assert not leaked, f"answer-key field(s) leaked: {leaked}"
        for v in obj.values():
            _assert_no_key(v)
    elif isinstance(obj, list):
        for v in obj:
            _assert_no_key(v)


@pytest.fixture(autouse=True)
def finalize_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Spy on the background finalize the delivery API schedules.

    The router looks `app.results.finalize_attempt` up at call time, so patching
    the package attribute catches every scheduled run (submit + lazy expiry) and
    keeps the real grade+report job (its own session on the app DB) out of these
    HTTP tests. TestClient runs background tasks right after the response.
    """
    import app.results as results

    calls: list[str] = []
    monkeypatch.setattr(results, "finalize_attempt", calls.append)
    return calls


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    content = ContentRepository(session)
    attempts = AttemptRepository(session)
    content.add_test(_test())
    attempts.add_roster_entry(
        RosterEntry(id="entry-1", test_id="t1", display_name="Alice", pin=PINS["entry-1"])
    )
    attempts.add_roster_entry(
        RosterEntry(id="entry-2", test_id="t1", display_name="Bob", pin=PINS["entry-2"])
    )
    session.commit()

    app.dependency_overrides[get_session] = lambda: session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _start(client: TestClient, entry: str = "entry-1", pin: str | None = None) -> dict:
    """PIN start; the returned token becomes the client's default session header."""
    resp = client.post(f"/exam/roster/{entry}/start", json={"pin": pin or PINS[entry]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    client.headers[TOKEN] = body["token"]
    return body


# --------------------------------------------------------------------------- #
# Pick-your-name landing.
# --------------------------------------------------------------------------- #
def test_roster_for_link_lists_names_without_keys(client: TestClient) -> None:
    resp = client.get("/exam/tests/t1/roster")
    assert resp.status_code == 200
    names = {r["display_name"] for r in resp.json()}
    assert names == {"Alice", "Bob"}
    _assert_no_key(resp.json())


# --------------------------------------------------------------------------- #
# Start -> serve keyless -> save -> submit (the happy path).
# --------------------------------------------------------------------------- #
def test_full_attempt_flow(client: TestClient, session: Session) -> None:
    started = _start(client)
    attempt_id = started["attempt_id"]
    assert started["test_id"] == "t1"
    assert started["state"]["status"] == "in_progress"
    assert 0 < started["state"]["remaining_seconds"] <= 30 * 60

    # Full client test carries no answer key.
    test = client.get(f"/exam/attempts/{attempt_id}/test").json()
    _assert_no_key(test)
    assert "charlie" in str(test)  # the correct option's text is still served

    # One-at-a-time serve is keyless too.
    item = client.get(f"/exam/attempts/{attempt_id}/items/q-sc").json()
    _assert_no_key(item)
    assert item["item_type"] == "single_choice"

    # single_choice saves a *displayed* index; the server maps it to canonical.
    sc = next(i for s in test["sections"] for i in s["items"] if i["id"] == "q-sc")
    displayed_for_c = next(
        idx for idx, o in enumerate(sc["options"]) if o["text"] == "charlie"
    )
    assert (
        client.put(
            f"/exam/attempts/{attempt_id}/answers/q-sc",
            json={"response": displayed_for_c},
        ).status_code
        == 204
    )
    client.put(f"/exam/attempts/{attempt_id}/answers/q-gap", json={"response": "house"})
    client.put(f"/exam/attempts/{attempt_id}/answers/q-match", json={"response": "B"})

    stored = {
        a.item_id: a.response
        for a in AttemptRepository(session).get_answers(attempt_id)
    }
    assert stored == {"q-sc": "C", "q-gap": "house", "q-match": "B"}

    # Submit finalizes; a second submit is rejected (409).
    submitted = client.post(f"/exam/attempts/{attempt_id}/submit")
    assert submitted.status_code == 200 and submitted.json()["status"] == "submitted"
    assert client.post(f"/exam/attempts/{attempt_id}/submit").status_code == 409
    # No saves after submit.
    assert (
        client.put(
            f"/exam/attempts/{attempt_id}/answers/q-gap", json={"response": "barn"}
        ).status_code
        == 409
    )


def test_refresh_resumes_same_attempt(client: TestClient) -> None:
    first = _start(client)
    second = _start(client)
    assert first["attempt_id"] == second["attempt_id"]
    assert first["token"] != second["token"]  # each PIN start is its own session


# --------------------------------------------------------------------------- #
# Server-authoritative timer (golden rule #3): a crossed deadline expires it.
# --------------------------------------------------------------------------- #
def test_state_expires_crossed_deadline(client: TestClient, session: Session) -> None:
    # Seed an attempt whose deadline already passed; /state must flip it expired.
    past = datetime.now(UTC) - timedelta(minutes=1)
    attempts = AttemptRepository(session)
    attempts.add_attempt(
        Attempt(
            id="att-expired",
            test_id="t1",
            roster_entry_id="entry-2",
            status="in_progress",
            seed=1,
            started_at=past - timedelta(minutes=30),
            deadline=past,
        )
    )
    attempts.add_attempt_session("att-expired", "tok-expired")
    session.commit()
    client.headers[TOKEN] = "tok-expired"

    state = client.get("/exam/attempts/att-expired/state").json()
    assert state["expired"] is True
    assert state["status"] == "expired"
    assert state["remaining_seconds"] == 0

    # A late submit on the expired attempt is rejected (409).
    assert client.post("/exam/attempts/att-expired/submit").status_code == 409


def test_unknown_attempt_is_404(client: TestClient) -> None:
    client.headers[TOKEN] = "whatever"
    assert client.get("/exam/attempts/nope/state").status_code == 404
    assert client.get("/exam/attempts/nope/test").status_code == 404


# --------------------------------------------------------------------------- #
# Lazy expiry schedules the finalize — once per transition, never per poll.
# --------------------------------------------------------------------------- #
def _seed_overdue(
    session: Session,
    client: TestClient,
    attempt_id: str = "att-late",
    entry: str = "entry-2",
) -> str:
    """An `in_progress` attempt past its deadline + a session the client holds."""
    past = datetime.now(UTC) - timedelta(minutes=1)
    repo = AttemptRepository(session)
    repo.add_attempt(
        Attempt(
            id=attempt_id,
            test_id="t1",
            roster_entry_id=entry,
            status="in_progress",
            seed=1,
            started_at=past - timedelta(minutes=30),
            deadline=past,
        )
    )
    repo.update_roster_entry(
        repo.get_roster_entry(entry).model_copy(
            update={"status": "in_progress", "attempt_id": attempt_id}
        )
    )
    repo.add_attempt_session(attempt_id, f"tok-{attempt_id}")
    session.commit()
    client.headers[TOKEN] = f"tok-{attempt_id}"
    return attempt_id


def test_state_expiry_schedules_finalize_once(
    client: TestClient, session: Session, finalize_calls: list[str]
) -> None:
    attempt_id = _seed_overdue(session, client)
    first = client.get(f"/exam/attempts/{attempt_id}/state").json()
    assert first["status"] == "expired" and first["expired"] is True
    assert finalize_calls == [attempt_id]
    # Every later poll (and a resume via start) sees `expired`: no second finalize.
    for _ in range(3):
        assert client.get(f"/exam/attempts/{attempt_id}/state").json()["status"] == "expired"
    resumed = client.post("/exam/roster/entry-2/start", json={"pin": PINS["entry-2"]}).json()
    assert resumed["state"]["status"] == "expired"
    assert client.post(f"/exam/attempts/{attempt_id}/submit").status_code == 409
    assert (
        client.put(f"/exam/attempts/{attempt_id}/answers/q-gap", json={"response": "x"})
        .status_code
        == 409
    )
    assert finalize_calls == [attempt_id]


def test_in_time_state_poll_schedules_nothing(
    client: TestClient, finalize_calls: list[str]
) -> None:
    attempt_id = _start(client)["attempt_id"]
    for _ in range(2):
        assert client.get(f"/exam/attempts/{attempt_id}/state").json()["status"] == "in_progress"
    assert finalize_calls == []


def test_late_save_expires_and_schedules_finalize(
    client: TestClient, session: Session, finalize_calls: list[str]
) -> None:
    attempt_id = _seed_overdue(session, client)
    resp = client.put(f"/exam/attempts/{attempt_id}/answers/q-gap", json={"response": "house"})
    assert resp.status_code == 409
    assert "past deadline" in resp.json()["detail"]  # same body shape as before
    # The expiry survived the 409 (committed, not rolled back with the request).
    assert AttemptRepository(session).get_attempt(attempt_id).status == "expired"
    assert AttemptRepository(session).get_answers(attempt_id) == []  # late save dropped
    assert finalize_calls == [attempt_id]
    # Further late actions / polls schedule nothing more.
    client.put(f"/exam/attempts/{attempt_id}/answers/q-gap", json={"response": "house"})
    client.get(f"/exam/attempts/{attempt_id}/state")
    assert finalize_calls == [attempt_id]


def test_late_submit_expires_and_schedules_finalize(
    client: TestClient, session: Session, finalize_calls: list[str]
) -> None:
    attempt_id = _seed_overdue(session, client)
    resp = client.post(f"/exam/attempts/{attempt_id}/submit")
    assert resp.status_code == 409
    assert AttemptRepository(session).get_attempt(attempt_id).status == "expired"
    assert finalize_calls == [attempt_id]
    assert client.post(f"/exam/attempts/{attempt_id}/submit").status_code == 409
    assert finalize_calls == [attempt_id]


def test_resume_past_deadline_schedules_finalize_once(
    client: TestClient, session: Session, finalize_calls: list[str]
) -> None:
    attempt_id = _seed_overdue(session, client)
    first = _start(client, "entry-2")
    assert first["attempt_id"] == attempt_id and first["state"]["status"] == "expired"
    _start(client, "entry-2")
    assert finalize_calls == [attempt_id]


def test_submit_in_time_schedules_finalize_once(
    client: TestClient, finalize_calls: list[str]
) -> None:
    attempt_id = _start(client)["attempt_id"]
    assert client.post(f"/exam/attempts/{attempt_id}/submit").status_code == 200
    assert client.post(f"/exam/attempts/{attempt_id}/submit").status_code == 409
    client.get(f"/exam/attempts/{attempt_id}/state")
    assert finalize_calls == [attempt_id]


# --------------------------------------------------------------------------- #
# Roster after auto-expiry: the pick-your-name list shows the student finished.
# --------------------------------------------------------------------------- #
def test_roster_link_shows_expired_after_lazy_expiry(
    client: TestClient, session: Session, finalize_calls: list[str]
) -> None:
    attempt_id = _seed_overdue(session, client)
    before = {r["display_name"]: r["status"] for r in client.get("/exam/tests/t1/roster").json()}
    assert before == {"Alice": "not_started", "Bob": "in_progress"}
    assert client.get(f"/exam/attempts/{attempt_id}/state").json()["status"] == "expired"
    after = {r["display_name"]: r["status"] for r in client.get("/exam/tests/t1/roster").json()}
    assert after == {"Alice": "not_started", "Bob": "expired"}
    assert finalize_calls == [attempt_id]


def test_roster_link_shows_expired_after_late_submit(
    client: TestClient, session: Session
) -> None:
    """The late-action path commits the expiry (409 body) — roster included."""
    attempt_id = _seed_overdue(session, client)
    assert client.post(f"/exam/attempts/{attempt_id}/submit").status_code == 409
    statuses = {r["display_name"]: r["status"] for r in client.get("/exam/tests/t1/roster").json()}
    assert statuses["Bob"] == "expired"


# --------------------------------------------------------------------------- #
# Phase 14: classroom PIN, session token, wrong-PIN lockout.
# --------------------------------------------------------------------------- #
def test_roster_list_never_carries_pins(client: TestClient) -> None:
    resp = client.get("/exam/tests/t1/roster")
    assert resp.status_code == 200
    for row in resp.json():
        assert set(row) == {"roster_entry_id", "display_name", "status"}
    for pin in PINS.values():
        assert pin not in resp.text


def test_test_info_for_the_start_screen(client: TestClient) -> None:
    info = client.get("/exam/tests/t1/info")
    assert info.status_code == 200
    body = info.json()
    server_now = body.pop("server_now")
    assert datetime.fromisoformat(server_now).tzinfo is not None
    assert body == {
        "test_id": "t1",
        "title": "A2 Key — Mock",
        "level": "A2_KEY",
        # No schedule on the test -> always open (tests/test_exam_window.py).
        "window_state": "open",
        "opens_at": None,
        "closes_at": None,
        "starts_until": None,
    }
    _assert_no_key(info.json())
    assert client.get("/exam/tests/nope/info").status_code == 404


@pytest.mark.parametrize("body", [None, {}, {"pin": ""}, {"pin": "   "}])
def test_start_without_pin_is_400(
    client: TestClient, session: Session, body: dict | None
) -> None:
    resp = client.post("/exam/roster/entry-1/start", json=body)
    assert resp.status_code == 400
    assert "PIN" in resp.json()["detail"]
    assert AttemptRepository(session).get_roster_entry("entry-1").attempt_id is None


@pytest.mark.parametrize("pin", ["AB3", "AB3DE", "AB-3", "0O1I"])
def test_malformed_pin_is_400_and_not_counted(client: TestClient, pin: str) -> None:
    for _ in range(6):  # more than the lock threshold: malformed input never locks
        assert client.post("/exam/roster/entry-1/start", json={"pin": pin}).status_code == 400
    assert _start(client)["state"]["status"] == "in_progress"


def test_wrong_pin_is_403_and_creates_nothing(
    client: TestClient, session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING"):
        resp = client.post("/exam/roster/entry-1/start", json={"pin": PINS["entry-2"]})
    assert resp.status_code == 403
    assert resp.json()["detail"].startswith("Wrong PIN")
    assert resp.headers["X-Pin-Tries-Left"] == "4"
    assert "token" not in resp.json()
    assert AttemptRepository(session).get_roster_entry("entry-1").attempt_id is None
    assert any("pin WRONG entry=entry-1" in r.getMessage() for r in caplog.records)
    assert all(PINS["entry-2"] not in r.getMessage() for r in caplog.records)  # never logged


def test_pin_is_case_insensitive_and_ignores_spaces(client: TestClient) -> None:
    resp = client.post("/exam/roster/entry-1/start", json={"pin": " ab 3d "})
    assert resp.status_code == 200
    assert resp.json()["token"]


def test_unknown_roster_entry_is_404(client: TestClient) -> None:
    assert client.post("/exam/roster/nope/start", json={"pin": "AB3D"}).status_code == 404


def test_five_wrong_pins_lock_the_entry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import app.delivery.access as access

    clock = [1000.0]
    monkeypatch.setattr(access.time, "monotonic", lambda: clock[0])
    codes = []
    with caplog.at_level("WARNING"):
        for _ in range(5):
            codes.append(client.post("/exam/roster/entry-1/start", json={"pin": "ZZZZ"}))
    assert [r.status_code for r in codes] == [403, 403, 403, 403, 429]
    assert [r.headers.get("X-Pin-Tries-Left") for r in codes[:4]] == ["4", "3", "2", "1"]
    locked = codes[-1]
    assert int(locked.headers["Retry-After"]) == 300
    assert "Too many wrong PINs" in locked.json()["detail"]
    assert any("LOCKED entry" in r.getMessage() for r in caplog.records)
    # While locked even the right PIN is refused (else the lock bounds nothing).
    right = client.post("/exam/roster/entry-1/start", json={"pin": PINS["entry-1"]})
    assert right.status_code == 429
    # Another student is not affected.
    assert _start(client, "entry-2")["token"]
    # Five minutes later the lock is gone.
    clock[0] += 301
    assert _start(client)["state"]["status"] == "in_progress"


def test_right_pin_clears_the_entry_failures(client: TestClient) -> None:
    for _ in range(4):
        assert client.post("/exam/roster/entry-1/start", json={"pin": "ZZZZ"}).status_code == 403
    _start(client)
    resp = client.post("/exam/roster/entry-1/start", json={"pin": "ZZZZ"})
    assert resp.status_code == 403 and resp.headers["X-Pin-Tries-Left"] == "4"


def _add_students(session: Session, n: int) -> dict[str, str]:
    """`n` more students on t1 with known PINs (all start from the one TestClient IP)."""
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ"
    pins = {}
    repo = AttemptRepository(session)
    for i in range(n):
        pin = f"Q{alphabet[i // 20]}{alphabet[i % 20]}9"
        pins[f"kid-{i}"] = pin
        repo.add_roster_entry(
            RosterEntry(id=f"kid-{i}", test_id="t1", display_name=f"Kid {i}", pin=pin)
        )
    session.commit()
    return pins


def test_class_of_30_behind_one_ip_with_typos_all_get_in(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every child of a class sharing ONE address mistypes once or twice, in waves of
    six typing at the same moment: nobody's right PIN is ever refused (rule #3)."""
    import app.delivery.access as access

    clock = [1000.0]
    monkeypatch.setattr(access.time, "monotonic", lambda: clock[0])
    pins = _add_students(session, 30)
    kids = list(pins)
    for wave in range(5):
        row = kids[wave * 6 : wave * 6 + 6]
        for typo in range(2):
            for i, kid in enumerate(row):
                if typo == 1 and i % 2:  # half the row mistypes twice
                    continue
                resp = client.post(f"/exam/roster/{kid}/start", json={"pin": "ZZZZ"})
                assert resp.status_code == 403, resp.text
            clock[0] += 8
        for kid in row:
            resp = client.post(f"/exam/roster/{kid}/start", json={"pin": pins[kid]})
            assert resp.status_code == 200, (kid, resp.text)
        clock[0] += 10


def test_ip_spraying_many_students_is_throttled(
    client: TestClient,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import app.delivery.access as access

    clock = [1000.0]
    monkeypatch.setattr(access.time, "monotonic", lambda: clock[0])
    pins = _add_students(session, 8)
    for kid in list(pins)[:6]:  # one guess on each of six names
        assert client.post(f"/exam/roster/{kid}/start", json={"pin": "ZZZZ"}).status_code == 403
    clock[0] += 125  # nobody resolved them: this is a spray, not typos
    with caplog.at_level("WARNING"):
        strike = client.post("/exam/roster/kid-6/start", json={"pin": "ZZZZ"})
    assert strike.status_code == 429 and strike.headers["Retry-After"] == "30"
    assert "from this network" in strike.json()["detail"]
    assert any("PIN THROTTLE" in r.getMessage() for r in caplog.records)
    # Throttled: even a right PIN from this IP waits (briefly), then works.
    right = client.post("/exam/roster/kid-7/start", json={"pin": pins["kid-7"]})
    assert right.status_code == 429
    clock[0] += 31
    assert _start(client, "entry-1")["token"]


def test_share_link_serves_published_tests_only(client: TestClient, session: Session) -> None:
    """A draft (e.g. unpublished again) looks exactly like an unknown test (404)."""
    content = ContentRepository(session)
    content.set_status("t1", "draft")
    session.commit()
    assert client.get("/exam/tests/t1/info").status_code == 404
    assert client.get("/exam/tests/t1/roster").status_code == 404
    assert client.get("/exam/tests/nope/roster").status_code == 404
    content.set_status("t1", "published")
    session.commit()
    assert client.get("/exam/tests/t1/info").status_code == 200
    assert client.get("/exam/tests/t1/roster").status_code == 200


def test_a_new_attempt_needs_a_published_test_but_resume_survives_unpublish(
    client: TestClient, session: Session
) -> None:
    content = ContentRepository(session)
    content.set_status("t1", "draft")
    session.commit()
    # No attempt yet: a draft looks like an unknown test.
    resp = client.post("/exam/roster/entry-1/start", json={"pin": PINS["entry-1"]})
    assert resp.status_code == 404
    content.set_status("t1", "published")
    session.commit()
    first = _start(client, "entry-1")
    # Forced unpublish mid-exam: the student who already started can still resume.
    content.set_status("t1", "draft")
    session.commit()
    again = _start(client, "entry-1")
    assert again["attempt_id"] == first["attempt_id"]


def test_resume_requires_the_pin_and_issues_a_second_session(client: TestClient) -> None:
    first = _start(client)
    assert client.post("/exam/roster/entry-1/start").status_code == 400
    assert client.post("/exam/roster/entry-1/start", json={"pin": "ZZZZ"}).status_code == 403
    again = client.post("/exam/roster/entry-1/start", json={"pin": PINS["entry-1"]}).json()
    assert again["attempt_id"] == first["attempt_id"]
    # Both devices' sessions work (another device does not kick the first out).
    for tok in (first["token"], again["token"]):
        assert (
            client.get(
                f"/exam/attempts/{first['attempt_id']}/state", headers={TOKEN: tok}
            ).status_code
            == 200
        )


_ATTEMPT_CALLS = [
    ("GET", "/test", None),
    ("GET", "/items/q-sc", None),
    ("GET", "/state", None),
    ("POST", "/audio-progress", {"seconds": 5}),
    ("GET", "/answers", None),
    ("PUT", "/answers/q-gap", {"response": "house"}),
    ("POST", "/submit", None),
]


@pytest.mark.parametrize(("method", "suffix", "body"), _ATTEMPT_CALLS)
@pytest.mark.parametrize("token", [None, "", "not-a-token"])
def test_every_attempt_endpoint_rejects_missing_or_wrong_token(
    client: TestClient,
    session: Session,
    method: str,
    suffix: str,
    body: dict | None,
    token: str | None,
) -> None:
    attempt_id = _start(client)["attempt_id"]
    headers = {} if token is None else {TOKEN: token}
    del client.headers[TOKEN]
    resp = client.request(
        method, f"/exam/attempts/{attempt_id}{suffix}", json=body, headers=headers
    )
    assert resp.status_code == 401
    assert "PIN" in resp.json()["detail"]
    # Nothing happened: no answer saved, still in progress.
    assert AttemptRepository(session).get_answers(attempt_id) == []
    assert AttemptRepository(session).get_attempt(attempt_id).status == "in_progress"


@pytest.mark.parametrize(("method", "suffix", "body"), _ATTEMPT_CALLS)
def test_a_token_only_opens_its_own_attempt(
    client: TestClient, method: str, suffix: str, body: dict | None
) -> None:
    alice = _start(client, "entry-1")
    bob = _start(client, "entry-2")
    resp = client.request(
        method,
        f"/exam/attempts/{alice['attempt_id']}{suffix}",
        json=body,
        headers={TOKEN: bob["token"]},
    )
    assert resp.status_code == 401


def test_rejected_token_is_logged_as_warning(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    attempt_id = _start(client)["attempt_id"]
    with caplog.at_level("WARNING"):
        client.get(f"/exam/attempts/{attempt_id}/state", headers={TOKEN: "bad"})
    assert any("session REJECTED" in r.getMessage() for r in caplog.records)


def test_session_token_is_stored_hashed(client: TestClient, session: Session) -> None:
    from sqlalchemy import select

    from app.persistence import models as m

    started = _start(client)
    hashes = session.scalars(select(m.AttemptSessionRow.token_hash)).all()
    assert started["token"] not in hashes
    import hashlib

    assert hashlib.sha256(started["token"].encode()).hexdigest() in hashes
