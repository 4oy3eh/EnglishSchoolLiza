"""Phase 4: delivery engine — attempt lifecycle, projection, timer.

Gate (docs/PROMPTS.md Prompt 4): the served payload has **no `correct`**;
window/grace enforced; refresh resumes the same attempt; late submit rejected.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.delivery import (
    AttemptExpiredError,
    AttemptStateError,
    DeliveryError,
    DeliveryService,
    ExamWindow,
    NotFoundError,
)
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import (
    Attempt,
    GapFillItem,
    MatchingItem,
    MatchingPoolStimulus,
    OpenWritingItem,
    PassageTextStimulus,
    PoolOption,
    RosterEntry,
    Section,
    SingleChoiceItem,
    TextOption,
)
from contracts import Test as ExamTest

BASE = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)


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
                            TextOption(key="D", text="delta"),
                        ],
                        correct="C",
                    ),
                    GapFillItem(
                        id="q-gap",
                        prompt="Fill the gap",
                        accepted=["house"],
                        accepted_variants=["hous"],
                    ),
                ],
            ),
            Section(
                id="sec-match",
                skill="reading",
                stimulus=MatchingPoolStimulus(
                    options=[
                        PoolOption(key="A", text="opt a"),
                        PoolOption(key="B", text="opt b"),
                    ]
                ),
                items=[MatchingItem(id="q-match", prompt="Match", correct="B")],
            ),
            Section(
                id="sec-write",
                skill="writing",
                stimulus=PassageTextStimulus(text="Write."),
                items=[
                    OpenWritingItem(
                        id="q-write",
                        prompt="Write 25 words",
                        word_min=25,
                        bullet_points=["where", "when"],
                        rubric="secret rubric — never serve this",
                    )
                ],
            ),
        ],
    )


def _seed(session: Session) -> tuple[DeliveryService, str]:
    """Persist a test + one roster entry; return the service and the entry id."""
    content = ContentRepository(session)
    attempts = AttemptRepository(session)
    content.add_test(_test())
    entry = RosterEntry(id="entry-1", test_id="t1", display_name="Alice")
    attempts.add_roster_entry(entry)
    session.commit()
    return DeliveryService(content, attempts), entry.id


def _open_window() -> ExamWindow:
    return ExamWindow(opens_at=BASE - timedelta(hours=1), closes_at=BASE + timedelta(hours=2))


# --------------------------------------------------------------------------- #
# Golden rule #1: no `correct` (or any answer-key field) ever reaches the client.
# --------------------------------------------------------------------------- #
# `key` is intentionally NOT here: a matching-pool `PoolOption.key` (the A-H label
# the student selects) is shared, answer-free stimulus — single_choice client
# options are structurally keyless anyway (`ClientTextOption` has no `key` field).
_FORBIDDEN_KEYS = {"correct", "accepted", "accepted_variants", "rubric", "grade_mode"}


def _assert_no_answer_key(obj: object) -> None:
    if isinstance(obj, dict):
        leaked = _FORBIDDEN_KEYS & obj.keys()
        assert not leaked, f"answer-key field(s) leaked to client: {leaked}"
        for value in obj.values():
            _assert_no_answer_key(value)
    elif isinstance(obj, list):
        for value in obj:
            _assert_no_answer_key(value)


def test_served_payload_has_no_correct(session: Session) -> None:
    svc, entry_id = _seed(session)
    attempt = svc.start(entry_id, _open_window(), now=BASE)

    client = svc.client_test(attempt.id)
    dumped = client.model_dump(mode="json")

    _assert_no_answer_key(dumped)
    # Sanity: the rubric string exists in authoring but not in the served blob.
    assert "secret rubric" not in str(dumped)
    # All four single_choice options are still served (shuffled, keyless).
    sc = next(i for s in client.sections for i in s.items if i.id == "q-sc")
    assert {o.text for o in sc.options} == {"alpha", "bravo", "charlie", "delta"}  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# Create-or-resume (refresh-safe).
# --------------------------------------------------------------------------- #
def test_start_creates_attempt_and_links_roster(session: Session) -> None:
    svc, entry_id = _seed(session)
    attempt = svc.start(entry_id, _open_window(), now=BASE)

    assert attempt.status == "in_progress"
    assert attempt.started_at == BASE
    assert attempt.deadline == BASE + timedelta(minutes=30)

    entry = AttemptRepository(session).get_roster_entry(entry_id)
    assert entry is not None
    assert entry.attempt_id == attempt.id
    assert entry.status == "in_progress"


def test_refresh_resumes_same_attempt(session: Session) -> None:
    svc, entry_id = _seed(session)
    first = svc.start(entry_id, _open_window(), now=BASE)
    # A later reopen (even with a different window) returns the SAME attempt.
    second = svc.start(entry_id, _open_window(), now=BASE + timedelta(minutes=5))

    assert second.id == first.id
    assert second.deadline == first.deadline  # deadline fixed at first start


def test_unknown_roster_entry_rejected(session: Session) -> None:
    svc, _ = _seed(session)
    with pytest.raises(NotFoundError):
        svc.start("nope", _open_window(), now=BASE)


# --------------------------------------------------------------------------- #
# Window + grace enforcement.
# --------------------------------------------------------------------------- #
def test_start_before_window_opens_rejected(session: Session) -> None:
    svc, entry_id = _seed(session)
    window = ExamWindow(opens_at=BASE + timedelta(hours=1), closes_at=BASE + timedelta(hours=2))
    with pytest.raises(DeliveryError):
        svc.start(entry_id, window, now=BASE)


def test_start_after_close_rejected_but_grace_allows(session: Session) -> None:
    svc, entry_id = _seed(session)
    closed = ExamWindow(opens_at=BASE - timedelta(hours=2), closes_at=BASE - timedelta(minutes=1))
    with pytest.raises(DeliveryError):
        svc.start(entry_id, closed, now=BASE)

    # Same close, but 5 minutes of grace now covers `now`.
    graced = ExamWindow(
        opens_at=BASE - timedelta(hours=2),
        closes_at=BASE - timedelta(minutes=1),
        grace_seconds=600,
    )
    attempt = svc.start(entry_id, graced, now=BASE)
    # Deadline is capped by the grace-extended close, not start + duration.
    assert attempt.deadline == graced.hard_close


# --------------------------------------------------------------------------- #
# Displayed -> canonical mapping on save.
# --------------------------------------------------------------------------- #
def test_single_choice_displayed_index_maps_to_canonical(session: Session) -> None:
    svc, entry_id = _seed(session)
    attempt = svc.start(entry_id, _open_window(), now=BASE)

    client = svc.client_test(attempt.id)
    sc = next(i for s in client.sections for i in s.items if i.id == "q-sc")
    text_to_key = {"alpha": "A", "bravo": "B", "charlie": "C", "delta": "D"}
    # Find the displayed index whose option is canonical "C".
    displayed_for_c = next(
        idx for idx, opt in enumerate(sc.options) if text_to_key[opt.text] == "C"  # type: ignore[union-attr]
    )

    svc.save_answer(attempt.id, "q-sc", displayed_for_c, now=BASE)
    stored = {a.item_id: a.response for a in AttemptRepository(session).get_answers(attempt.id)}
    assert stored["q-sc"] == "C"


def test_text_items_stored_verbatim(session: Session) -> None:
    svc, entry_id = _seed(session)
    attempt = svc.start(entry_id, _open_window(), now=BASE)

    svc.save_answer(attempt.id, "q-gap", "house", now=BASE)
    svc.save_answer(attempt.id, "q-match", "B", now=BASE)
    svc.save_answer(attempt.id, "q-write", "I went to London last year.", now=BASE)

    stored = {a.item_id: a.response for a in AttemptRepository(session).get_answers(attempt.id)}
    assert stored["q-gap"] == "house"
    assert stored["q-match"] == "B"
    assert stored["q-write"].startswith("I went to London")


def test_get_saved_answers_round_trips_for_resume(session: Session) -> None:
    # Refresh/resume: saved answers come back as their *display* values — the
    # single_choice displayed index survives the canonical round-trip.
    svc, entry_id = _seed(session)
    attempt = svc.start(entry_id, _open_window(), now=BASE)

    client = svc.client_test(attempt.id)
    sc = next(i for s in client.sections for i in s.items if i.id == "q-sc")
    text_to_key = {"alpha": "A", "bravo": "B", "charlie": "C", "delta": "D"}
    displayed_for_c = next(
        idx for idx, opt in enumerate(sc.options) if text_to_key[opt.text] == "C"  # type: ignore[union-attr]
    )
    svc.save_answer(attempt.id, "q-sc", displayed_for_c, now=BASE)
    svc.save_answer(attempt.id, "q-gap", "house", now=BASE)
    svc.save_answer(attempt.id, "q-match", "B", now=BASE)

    saved = svc.get_saved_answers(attempt.id)
    assert saved["q-sc"] == displayed_for_c  # displayed index, not the canonical "C"
    assert saved["q-gap"] == "house"
    assert saved["q-match"] == "B"


def test_audio_progress_is_monotonic_and_surfaced_in_state(session: Session) -> None:
    # Server-side anti-replay: the furthest listening point only moves forward and
    # is read back via /state, so a refresh / new device resumes from there.
    svc, entry_id = _seed(session)
    attempt = svc.start(entry_id, _open_window(), now=BASE)
    assert svc.get_state(attempt.id, now=BASE).audio_progress_seconds == 0

    svc.report_audio_progress(attempt.id, 30, now=BASE)
    svc.report_audio_progress(attempt.id, 10, now=BASE)  # backwards -> ignored
    assert svc.get_state(attempt.id, now=BASE).audio_progress_seconds == 30


def test_single_choice_requires_index_not_text(session: Session) -> None:
    svc, entry_id = _seed(session)
    attempt = svc.start(entry_id, _open_window(), now=BASE)
    with pytest.raises(DeliveryError):
        svc.save_answer(attempt.id, "q-sc", "C", now=BASE)


def test_negative_displayed_index_rejected(session: Session) -> None:
    # A tuple accepts -1, which would silently map to the last option; reject it.
    svc, entry_id = _seed(session)
    attempt = svc.start(entry_id, _open_window(), now=BASE)
    with pytest.raises(DeliveryError):
        svc.save_answer(attempt.id, "q-sc", -1, now=BASE)
    with pytest.raises(DeliveryError):
        svc.save_answer(attempt.id, "q-sc", 99, now=BASE)


def test_one_attempt_per_roster_entry_enforced_by_db(session: Session) -> None:
    # The unique constraint makes a forked second attempt fail loudly rather than
    # silently violate "a roster entry never gets a second attempt".
    _seed(session)
    attempts = AttemptRepository(session)
    attempts.add_attempt(
        Attempt(id="a1", test_id="t1", roster_entry_id="entry-1", seed=1)
    )
    with pytest.raises(IntegrityError):
        attempts.add_attempt(
            Attempt(id="a2", test_id="t1", roster_entry_id="entry-1", seed=2)
        )


# --------------------------------------------------------------------------- #
# Serve one-at-a-time.
# --------------------------------------------------------------------------- #
def test_serve_item_one_at_a_time(session: Session) -> None:
    svc, entry_id = _seed(session)
    attempt = svc.start(entry_id, _open_window(), now=BASE)

    item = svc.serve_item(attempt.id, "q-gap")
    assert item.id == "q-gap"
    with pytest.raises(NotFoundError):
        svc.serve_item(attempt.id, "does-not-exist")


# --------------------------------------------------------------------------- #
# Server-authoritative timer + late actions.
# --------------------------------------------------------------------------- #
def test_state_counts_down_and_expires(session: Session) -> None:
    svc, entry_id = _seed(session)
    attempt = svc.start(entry_id, _open_window(), now=BASE)

    mid = svc.get_state(attempt.id, now=BASE + timedelta(minutes=10))
    assert mid.status == "in_progress"
    assert mid.remaining_seconds == 20 * 60
    assert mid.expired is False

    late = svc.get_state(attempt.id, now=BASE + timedelta(minutes=31))
    assert late.status == "expired"
    assert late.remaining_seconds == 0
    assert late.expired is True


def test_late_submit_rejected(session: Session) -> None:
    svc, entry_id = _seed(session)
    attempt = svc.start(entry_id, _open_window(), now=BASE)
    with pytest.raises(AttemptExpiredError):
        svc.submit(attempt.id, now=BASE + timedelta(minutes=31))


def test_save_after_deadline_rejected(session: Session) -> None:
    svc, entry_id = _seed(session)
    attempt = svc.start(entry_id, _open_window(), now=BASE)
    with pytest.raises(AttemptExpiredError):
        svc.save_answer(attempt.id, "q-gap", "house", now=BASE + timedelta(minutes=31))


def test_submit_finalizes_and_blocks_resave(session: Session) -> None:
    svc, entry_id = _seed(session)
    attempt = svc.start(entry_id, _open_window(), now=BASE)

    submitted = svc.submit(attempt.id, now=BASE + timedelta(minutes=10))
    assert submitted.status == "submitted"
    assert submitted.submitted_at == BASE + timedelta(minutes=10)

    entry = AttemptRepository(session).get_roster_entry(entry_id)
    assert entry is not None and entry.status == "submitted"

    # No saves after submit; double submit rejected.
    with pytest.raises(AttemptStateError):
        svc.save_answer(attempt.id, "q-gap", "house", now=BASE + timedelta(minutes=11))
    with pytest.raises(AttemptStateError):
        svc.submit(attempt.id, now=BASE + timedelta(minutes=11))
