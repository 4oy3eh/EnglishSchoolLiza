"""Phase 10: admin (teacher) API + dashboard backend.

Gate (docs/PROMPTS.md Prompt 10): auth is enforced; the approve flow flips
draft->published (the only publish path, golden rule #5); the results endpoint
returns score + advisory verdict + event count; results rank suspicious-first;
admin actions are logged. Grading and integrity stay independent (golden rule #2).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.admin import (
    AdminError,
    AdminService,
    AuthError,
    TokenSigner,
    build_admin_service,
)
from app.analysis import MockAnalysisLLM
from app.content import FilesystemStorage
from app.core.db import get_session
from app.persistence.repository import AttemptRepository, EventRepository
from apps.api import admin as admin_api
from apps.api.main import app
from contracts import (
    Answer,
    Attempt,
    IntegrityEvent,
    PassageTextStimulus,
    RosterEntry,
    Section,
    SingleChoiceItem,
    TextOption,
)
from contracts import Test as ExamTest

PASSWORD = "test-teacher-pw"
T0 = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Builders.
# --------------------------------------------------------------------------- #
def _test(test_id: str, *, status: str = "draft") -> ExamTest:
    return ExamTest(
        id=test_id,
        title=f"A2 Key — {test_id}",
        level="A2_KEY",
        status=status,  # type: ignore[arg-type]
        duration_minutes=60,
        sections=[
            Section(
                id=f"{test_id}-sec",
                skill="reading",
                stimulus=PassageTextStimulus(text="Read."),
                items=[
                    SingleChoiceItem(
                        id=f"{test_id}-q1",
                        prompt="?",
                        options=[
                            TextOption(key="A", text="a"),
                            TextOption(key="B", text="b"),
                        ],
                        correct="A",
                    )
                ],
            )
        ],
    )


def _seed_attempt(
    session: Session,
    *,
    test_id: str,
    name: str,
    answer: str,
    hidden_secs: float = 0.0,
) -> str:
    """Create a roster entry + submitted attempt with one answer and some events.

    `hidden_secs` injects a bounded hidden interval so the mock analyst produces a
    non-zero suspicion (used to prove suspicious-first ranking).
    """
    repo = AttemptRepository(session)
    entry = RosterEntry(id=f"{test_id}-{name}", test_id=test_id, display_name=name)
    repo.add_roster_entry(entry)
    attempt_id = f"att-{test_id}-{name}"
    repo.add_attempt(
        Attempt(
            id=attempt_id,
            test_id=test_id,
            roster_entry_id=entry.id,
            status="submitted",
            seed=1,
            started_at=T0,
            submitted_at=T0 + timedelta(minutes=5),
            deadline=T0 + timedelta(hours=1),
        )
    )
    repo.update_roster_entry(
        entry.model_copy(update={"status": "submitted", "attempt_id": attempt_id})
    )
    repo.save_answer(
        Answer(
            attempt_id=attempt_id,
            item_id=f"{test_id}-q1",
            response=answer,
            answered_at=T0 + timedelta(seconds=1),
        )
    )

    events = EventRepository(session)
    if hidden_secs:
        ts = T0 + timedelta(seconds=10)
        events.add_event(
            IntegrityEvent(
                attempt_id=attempt_id, type="visibility_hidden", client_ts=ts, server_ts=ts
            )
        )
        back = ts + timedelta(seconds=hidden_secs)
        events.add_event(
            IntegrityEvent(
                attempt_id=attempt_id, type="visibility_visible", client_ts=back, server_ts=back
            )
        )
    else:
        events.add_event(
            IntegrityEvent(
                attempt_id=attempt_id, item_id=f"{test_id}-q1", type="interaction",
                client_ts=T0, server_ts=T0,
            )
        )
    session.commit()
    return attempt_id


# --------------------------------------------------------------------------- #
# Service-layer: approve is the publish gate (rule #5).
# --------------------------------------------------------------------------- #
def test_approve_flips_draft_to_published(session: Session, tmp_path) -> None:
    svc = build_admin_service(session, FilesystemStorage(tmp_path), analysis_llm=MockAnalysisLLM())
    svc.content.create_test(_test("t1", status="draft"))
    session.commit()

    assert [d.test_id for d in svc.review_queue()] == ["t1"]
    published = svc.approve("t1")
    assert published.status == "published"
    # No longer a draft -> drops out of the review queue, and re-approve is refused.
    assert svc.review_queue() == []
    with pytest.raises(AdminError):
        svc.approve("t1")


def test_unpublish_reverts_only_published(session: Session, tmp_path) -> None:
    svc = build_admin_service(session, FilesystemStorage(tmp_path), analysis_llm=MockAnalysisLLM())
    svc.content.create_test(_test("t1", status="draft"))
    session.commit()

    # A draft cannot be unpublished (guarded like approve refuses non-drafts).
    with pytest.raises(AdminError):
        svc.unpublish("t1")

    svc.approve("t1")
    reverted = svc.unpublish("t1")
    assert reverted.status == "draft"
    # Back in the review queue after the revert.
    assert [d.test_id for d in svc.review_queue()] == ["t1"]


def test_results_independent_and_ranked(session: Session, tmp_path) -> None:
    svc = build_admin_service(session, FilesystemStorage(tmp_path), analysis_llm=MockAnalysisLLM())
    svc.content.create_test(_test("t1", status="published"))
    session.commit()

    # Honest: correct answer, no hidden time. Cheaty: wrong answer, long hide.
    honest = _seed_attempt(session, test_id="t1", name="Honest", answer="A")
    cheaty = _seed_attempt(session, test_id="t1", name="Cheaty", answer="B", hidden_secs=120)

    ranked = svc.results_for_test("t1")
    assert [o.attempt_id for o in ranked] == [cheaty, honest]  # suspicious-first
    # Score is independent of suspicion (rule #2): the more-suspicious attempt
    # actually scored *lower* here, and ranking never touched either score.
    assert ranked[0].suspicion_score >= ranked[1].suspicion_score
    assert ranked[0].score == 0.0 and ranked[1].score == 1.0

    detail = svc.attempt_result(honest)
    assert detail.grading.score == 1.0
    assert detail.event_count == len(detail.events) >= 1


# --------------------------------------------------------------------------- #
# Token signer.
# --------------------------------------------------------------------------- #
def test_token_roundtrip_and_expiry() -> None:
    signer = TokenSigner("secret", ttl_seconds=100)
    now = time.time()
    token = signer.mint(now=now)
    assert signer.verify(token, now=now + 10) == "teacher"
    with pytest.raises(AuthError):
        signer.verify(token, now=now + 101)  # expired
    with pytest.raises(AuthError):
        signer.verify(token + "x", now=now)  # tampered signature
    # A different secret cannot validate the token.
    with pytest.raises(AuthError):
        TokenSigner("other", ttl_seconds=100).verify(token, now=now)


# --------------------------------------------------------------------------- #
# HTTP surface: auth enforced + the full gate flow.
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(admin_api.settings, "teacher_password", PASSWORD)
    # Point asset storage + the analyst at deterministic test doubles by overriding
    # the service factory dependency to inject the mock analyst.
    app.dependency_overrides[get_session] = lambda: session

    def _svc() -> Iterator[AdminService]:
        yield build_admin_service(
            session, FilesystemStorage(tmp_path), analysis_llm=MockAnalysisLLM()
        )

    app.dependency_overrides[admin_api.get_admin_service] = _svc
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_auth_is_enforced(client: TestClient) -> None:
    # No token -> 401.
    assert client.get("/admin/tests").status_code == 401
    # Bad token -> 401.
    assert client.get("/admin/tests", headers=_auth("garbage")).status_code == 401
    # Wrong password -> 401, no token issued.
    assert client.post("/admin/login", json={"password": "nope"}).status_code == 401


def test_login_then_full_flow(client: TestClient, session: Session, tmp_path) -> None:
    # Login mints a usable token.
    resp = client.post("/admin/login", json={"password": PASSWORD})
    assert resp.status_code == 200
    token = resp.json()["token"]
    headers = _auth(token)

    # Seed a draft + a graded/observed attempt directly in the DB.
    svc = build_admin_service(session, FilesystemStorage(tmp_path))
    svc.content.create_test(_test("t1", status="draft"))
    session.commit()
    attempt_id = _seed_attempt(session, test_id="t1", name="Ann", answer="A", hidden_secs=90)

    # Review queue lists the draft; approve publishes it.
    assert [d["test_id"] for d in client.get("/admin/review", headers=headers).json()] == ["t1"]
    approved = client.post("/admin/review/t1/approve", headers=headers)
    assert approved.status_code == 200 and approved.json()["status"] == "published"
    assert client.get("/admin/review", headers=headers).json() == []

    # Results endpoint returns score + verdict + event count.
    detail = client.get(f"/admin/results/{attempt_id}", headers=headers).json()
    assert detail["grading"]["score"] == 1.0
    assert detail["grading"]["max_score"] == 1.0
    assert detail["verdict"]["suspicion_score"] > 0.0  # mock analyst saw the hide
    assert detail["event_count"] == 2

    # Ranked list is reachable and carries the same score (independent of verdict).
    ranked = client.get("/admin/tests/t1/results", headers=headers).json()
    assert ranked[0]["attempt_id"] == attempt_id
    assert ranked[0]["score"] == 1.0


def test_admin_actions_are_logged(
    session: Session, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    svc = build_admin_service(session, FilesystemStorage(tmp_path), analysis_llm=MockAnalysisLLM())
    svc.content.create_test(_test("t1", status="draft"))
    session.commit()
    with caplog.at_level(logging.INFO):
        svc.approve("t1")
    assert any("APPROVE" in r.message and "t1" in r.message for r in caplog.records)
