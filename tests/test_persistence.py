"""Phase 2 gate: create/read round-trips for each repository aggregate.

These assert the ORM models + repositories faithfully persist and reload the
`contracts/` models (CLAUDE.md golden rule #4 — models match contracts).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.persistence.repository import (
    AttemptRepository,
    ContentRepository,
    EventRepository,
)
from contracts import (
    Answer,
    Attempt,
    GapFillItem,
    IntegrityEvent,
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
from contracts import Test as ExamTest  # aliased: avoid pytest collecting `Test*`


def _sample_test() -> ExamTest:
    """A Test exercising every item type and a couple of stimulus kinds."""
    return ExamTest(
        id="test-1",
        title="B1 Preliminary — Mock",
        level="B1_PRELIMINARY",
        status="draft",
        duration_minutes=90,
        sections=[
            Section(
                id="sec-reading",
                title="Reading Part 1",
                skill="reading",
                stimulus=PassageTextStimulus(text="Read the passage and answer."),
                items=[
                    SingleChoiceItem(
                        id="q1",
                        prompt="What colour is the sky?",
                        options=[
                            TextOption(key="A", text="Blue"),
                            TextOption(key="B", text="Green"),
                        ],
                        correct="A",
                    ),
                    GapFillItem(
                        id="q2",
                        prompt="The grass is ____.",
                        accepted=["green"],
                        accepted_variants=["grean"],
                    ),
                ],
            ),
            Section(
                id="sec-matching",
                title="Reading Part 2",
                skill="reading",
                stimulus=MatchingPoolStimulus(
                    options=[
                        PoolOption(key="A", text="Museum"),
                        PoolOption(key="B", text="Park"),
                        PoolOption(key="C", text="Library"),
                    ]
                ),
                items=[
                    MatchingItem(id="q3", prompt="Where do you borrow books?", correct="C"),
                ],
            ),
            Section(
                id="sec-writing",
                title="Writing Part 1",
                skill="writing",
                stimulus=PassageTextStimulus(text="Write an email to a friend."),
                items=[
                    OpenWritingItem(
                        id="q4",
                        prompt="Invite your friend to a party.",
                        word_min=35,
                        bullet_points=["when", "where", "what to bring"],
                        rubric="Award marks for task achievement and range.",
                        grade_mode="llm",
                    ),
                ],
            ),
        ],
    )


def test_content_round_trip(session: Session) -> None:
    repo = ContentRepository(session)
    original = _sample_test()
    repo.add_test(original)
    session.commit()

    loaded = repo.get_test("test-1")
    assert loaded is not None
    # Full structural equality: the answer key and stimulus unions survive.
    assert loaded == original


def test_content_get_missing_returns_none(session: Session) -> None:
    assert ContentRepository(session).get_test("nope") is None


def test_content_preserves_section_and_item_order(session: Session) -> None:
    repo = ContentRepository(session)
    repo.add_test(_sample_test())
    session.commit()

    loaded = repo.get_test("test-1")
    assert loaded is not None
    assert [s.id for s in loaded.sections] == ["sec-reading", "sec-matching", "sec-writing"]
    assert [i.id for i in loaded.sections[0].items] == ["q1", "q2"]


def test_roster_and_attempt_round_trip(session: Session) -> None:
    ContentRepository(session).add_test(_sample_test())
    attempts = AttemptRepository(session)

    entry = RosterEntry(id="r1", test_id="test-1", display_name="Alice")
    attempts.add_roster_entry(entry)
    attempt = Attempt(
        id="a1",
        test_id="test-1",
        roster_entry_id="r1",
        status="in_progress",
        seed=4242,
        started_at=datetime(2026, 6, 21, 9, 0, 0, tzinfo=UTC),
        deadline=datetime(2026, 6, 21, 10, 30, 0, tzinfo=UTC),
    )
    attempts.add_attempt(attempt)
    session.commit()

    assert attempts.get_roster_entry("r1") == entry
    loaded = attempts.get_attempt("a1")
    assert loaded == attempt
    # Timer is server-authoritative (rule #3): datetimes must survive as UTC-aware,
    # not silently degrade to naive on sqlite.
    assert loaded is not None
    assert loaded.deadline is not None and loaded.deadline.tzinfo is not None


def test_answer_save_is_upsert(session: Session) -> None:
    ContentRepository(session).add_test(_sample_test())
    attempts = AttemptRepository(session)
    attempts.add_roster_entry(RosterEntry(id="r1", test_id="test-1", display_name="Bob"))
    attempts.add_attempt(
        Attempt(id="a1", test_id="test-1", roster_entry_id="r1", seed=1)
    )

    first = Answer(
        attempt_id="a1",
        item_id="q1",
        response="A",
        answered_at=datetime(2026, 6, 21, 9, 5, tzinfo=UTC),
    )
    attempts.save_answer(first)
    # Re-answering the same item overwrites rather than duplicating.
    second = Answer(
        attempt_id="a1",
        item_id="q1",
        response="B",
        answered_at=datetime(2026, 6, 21, 9, 6, tzinfo=UTC),
    )
    attempts.save_answer(second)
    attempts.save_answer(
        Answer(
            attempt_id="a1",
            item_id="q2",
            response="green",
            answered_at=datetime(2026, 6, 21, 9, 7, tzinfo=UTC),
        )
    )
    session.commit()

    answers = attempts.get_answers("a1")
    assert len(answers) == 2
    assert {a.item_id: a.response for a in answers} == {"q1": "B", "q2": "green"}


def test_event_round_trip_and_server_ts_stamp(session: Session) -> None:
    ContentRepository(session).add_test(_sample_test())
    attempts = AttemptRepository(session)
    attempts.add_roster_entry(RosterEntry(id="r1", test_id="test-1", display_name="Cara"))
    attempts.add_attempt(Attempt(id="a1", test_id="test-1", roster_entry_id="r1", seed=1))

    events = EventRepository(session)
    stored = events.add_event(
        IntegrityEvent(
            attempt_id="a1",
            item_id="q1",
            type="visibility_hidden",
            client_ts=datetime(2026, 6, 21, 9, 10, tzinfo=UTC),
            duration_ms=2500,
            payload={"reason": "tab_switch"},
        )
    )
    session.commit()

    # server_ts is stamped on ingest even though the client did not send one.
    assert stored.server_ts is not None

    listed = events.list_events("a1")
    assert len(listed) == 1
    assert listed[0].type == "visibility_hidden"
    assert listed[0].duration_ms == 2500
    assert listed[0].payload == {"reason": "tab_switch"}
    assert listed[0].server_ts is not None
    # Timestamps come back UTC-aware (rule #6: comparable event ordering).
    assert listed[0].client_ts.tzinfo is not None
    assert listed[0].server_ts.tzinfo is not None


def test_events_are_append_only(session: Session) -> None:
    ContentRepository(session).add_test(_sample_test())
    attempts = AttemptRepository(session)
    attempts.add_roster_entry(RosterEntry(id="r1", test_id="test-1", display_name="Dan"))
    attempts.add_attempt(Attempt(id="a1", test_id="test-1", roster_entry_id="r1", seed=1))

    events = EventRepository(session)
    for _ in range(3):
        events.add_event(
            IntegrityEvent(
                attempt_id="a1",
                type="window_blur",
                client_ts=datetime(2026, 6, 21, 9, 11, tzinfo=UTC),
            )
        )
    session.commit()

    assert len(events.list_events("a1")) == 3
