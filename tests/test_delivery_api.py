"""Phase 11: student delivery HTTP surface.

Gate (docs/PROMPTS.md Prompt 11): the runner serves items without a key (golden
rule #1); the timer counts down server-side and a crossed deadline expires the
attempt (golden rule #3); answers persist (displayed -> canonical); submit
finalizes; late actions are rejected. Telemetry ingest stays the Phase-6 sink.

These exercise the router with FastAPI's TestClient over the same in-memory DB
the repositories use, so the wiring (router -> DeliveryService -> repos) is real.
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
        RosterEntry(id="entry-1", test_id="t1", display_name="Alice")
    )
    attempts.add_roster_entry(
        RosterEntry(id="entry-2", test_id="t1", display_name="Bob")
    )
    session.commit()

    app.dependency_overrides[get_session] = lambda: session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


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
    started = client.post("/exam/roster/entry-1/start").json()
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
    first = client.post("/exam/roster/entry-1/start").json()["attempt_id"]
    second = client.post("/exam/roster/entry-1/start").json()["attempt_id"]
    assert first == second


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
    session.commit()

    state = client.get("/exam/attempts/att-expired/state").json()
    assert state["expired"] is True
    assert state["status"] == "expired"
    assert state["remaining_seconds"] == 0

    # A late submit on the expired attempt is rejected (409).
    assert client.post("/exam/attempts/att-expired/submit").status_code == 409


def test_unknown_attempt_is_404(client: TestClient) -> None:
    assert client.get("/exam/attempts/nope/state").status_code == 404
    assert client.get("/exam/attempts/nope/test").status_code == 404


# --------------------------------------------------------------------------- #
# Lazy expiry schedules the finalize — once per transition, never per poll.
# --------------------------------------------------------------------------- #
def _seed_overdue(session: Session, attempt_id: str = "att-late", entry: str = "entry-2") -> str:
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
    session.commit()
    return attempt_id


def test_state_expiry_schedules_finalize_once(
    client: TestClient, session: Session, finalize_calls: list[str]
) -> None:
    attempt_id = _seed_overdue(session)
    first = client.get(f"/exam/attempts/{attempt_id}/state").json()
    assert first["status"] == "expired" and first["expired"] is True
    assert finalize_calls == [attempt_id]
    # Every later poll (and a resume via start) sees `expired`: no second finalize.
    for _ in range(3):
        assert client.get(f"/exam/attempts/{attempt_id}/state").json()["status"] == "expired"
    assert client.post("/exam/roster/entry-2/start").json()["state"]["status"] == "expired"
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
    attempt_id = client.post("/exam/roster/entry-1/start").json()["attempt_id"]
    for _ in range(2):
        assert client.get(f"/exam/attempts/{attempt_id}/state").json()["status"] == "in_progress"
    assert finalize_calls == []


def test_late_save_expires_and_schedules_finalize(
    client: TestClient, session: Session, finalize_calls: list[str]
) -> None:
    attempt_id = _seed_overdue(session)
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
    attempt_id = _seed_overdue(session)
    resp = client.post(f"/exam/attempts/{attempt_id}/submit")
    assert resp.status_code == 409
    assert AttemptRepository(session).get_attempt(attempt_id).status == "expired"
    assert finalize_calls == [attempt_id]
    assert client.post(f"/exam/attempts/{attempt_id}/submit").status_code == 409
    assert finalize_calls == [attempt_id]


def test_resume_past_deadline_schedules_finalize_once(
    client: TestClient, session: Session, finalize_calls: list[str]
) -> None:
    attempt_id = _seed_overdue(session)
    first = client.post("/exam/roster/entry-2/start").json()
    assert first["attempt_id"] == attempt_id and first["state"]["status"] == "expired"
    client.post("/exam/roster/entry-2/start")
    assert finalize_calls == [attempt_id]


def test_submit_in_time_schedules_finalize_once(
    client: TestClient, finalize_calls: list[str]
) -> None:
    attempt_id = client.post("/exam/roster/entry-1/start").json()["attempt_id"]
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
    attempt_id = _seed_overdue(session)
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
    attempt_id = _seed_overdue(session)
    assert client.post(f"/exam/attempts/{attempt_id}/submit").status_code == 409
    statuses = {r["display_name"]: r["status"] for r in client.get("/exam/tests/t1/roster").json()}
    assert statuses["Bob"] == "expired"
