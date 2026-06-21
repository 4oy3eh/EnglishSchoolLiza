"""Phase 3: ContentService CRUD + RosterService/assignment."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.content import (
    ContentService,
    FilesystemStorage,
    RosterService,
    build_attempt_layout,
    derive_seed,
)
from app.content.pooling import bank_from_sections
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import (
    PassageTextStimulus,
    Section,
    SingleChoiceItem,
    TextOption,
)
from contracts import Test as ExamTest


def _test(test_id: str = "t1", status: str = "draft") -> ExamTest:
    return ExamTest(
        id=test_id,
        title="A2 Key — Mock",
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
                        options=[TextOption(key="A", text="a"), TextOption(key="B", text="b")],
                        correct="A",
                    )
                ],
            )
        ],
    )


def _service(session: Session, tmp_path: Path) -> ContentService:
    return ContentService(ContentRepository(session), FilesystemStorage(tmp_path))


def test_create_read_list_tests(session: Session, tmp_path: Path) -> None:
    svc = _service(session, tmp_path)
    svc.create_test(_test("t1"))
    svc.create_test(_test("t2"))
    session.commit()

    assert svc.get_test("t1") == _test("t1")
    assert {t.id for t in svc.list_tests()} == {"t1", "t2"}


def test_publish_and_unpublish(session: Session, tmp_path: Path) -> None:
    svc = _service(session, tmp_path)
    svc.create_test(_test("t1"))
    session.commit()

    assert svc.publish("t1") is True
    loaded = svc.get_test("t1")
    assert loaded is not None and loaded.status == "published"

    assert svc.unpublish("t1") is True
    loaded = svc.get_test("t1")
    assert loaded is not None and loaded.status == "draft"

    assert svc.publish("missing") is False


def test_delete_test(session: Session, tmp_path: Path) -> None:
    svc = _service(session, tmp_path)
    svc.create_test(_test("t1"))
    session.commit()

    assert svc.delete_test("t1") is True
    session.commit()
    assert svc.get_test("t1") is None
    assert svc.delete_test("t1") is False


def test_asset_crud_through_service(session: Session, tmp_path: Path) -> None:
    svc = _service(session, tmp_path)
    svc.add_asset("logo.png", b"bytes", content_type="image/png")
    assert svc.has_asset("logo.png")
    assert svc.get_asset("logo.png") == b"bytes"
    svc.delete_asset("logo.png")
    assert not svc.has_asset("logo.png")


def test_roster_add_list_and_seed_assignment(session: Session, tmp_path: Path) -> None:
    ContentService(ContentRepository(session), FilesystemStorage(tmp_path)).create_test(
        _test("t1")
    )
    roster = RosterService(AttemptRepository(session))

    alice = roster.add_student("t1", "Alice")
    bob = roster.add_student("t1", "Bob")
    session.commit()

    names = {e.display_name for e in roster.list_students("t1")}
    assert names == {"Alice", "Bob"}

    # Seeds are stable per (test, entry) and differ between students.
    assert roster.assign_seed(alice) == derive_seed("t1", alice.id)
    assert roster.assign_seed(alice) == roster.assign_seed(alice)
    assert roster.assign_seed(alice) != roster.assign_seed(bob)
    # Seed fits a signed 32-bit INTEGER column (Postgres-portable).
    assert 0 <= roster.assign_seed(alice) < 2**31


def test_assigned_seeds_drive_divergent_layouts(session: Session, tmp_path: Path) -> None:
    # End-to-end: roster seeds -> reproducible per-student layout over a bank.
    sections = [
        Section(
            id=f"sec-{i}",
            skill="reading",
            stimulus=PassageTextStimulus(text=f"p{i}"),
            items=[
                SingleChoiceItem(
                    id=f"sec-{i}-q",
                    prompt="?",
                    options=[TextOption(key="A", text="a"), TextOption(key="B", text="b")],
                    correct="A",
                )
            ],
        )
        for i in range(6)
    ]
    bank = bank_from_sections("t1", sections, pick=3)
    roster = RosterService(AttemptRepository(session))
    alice = roster.add_student("t1", "Alice")

    seed = roster.assign_seed(alice)
    layout = build_attempt_layout(bank, seed)

    # The roster seed produces a valid draw (3 of 6 sections, each with one
    # shuffled single_choice item).
    assert len(layout.section_ids) == 3
    assert set(layout.section_ids) <= {f"sec-{i}" for i in range(6)}
    assert len(layout.option_shuffles) == 3
    # Reproducible: recomputing from the same roster seed gives the same layout
    # (so a resumed attempt is identical — divergence between students is proven
    # deterministically in test_content_pooling).
    assert build_attempt_layout(bank, seed).to_dict() == layout.to_dict()
