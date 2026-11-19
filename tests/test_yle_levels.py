"""Pre A1 Starters + A2 Flyers: the two remaining Cambridge Young Learners levels.

A Starters and a Flyers test (authored JSON, like every new test) import as drafts,
publish through the approval gate, start + save + grade through the real services,
and `/exam/tests/{id}/info` reports their level so the runner offers the uk/ru UI
(the browser side is in tests/test_window_runner_e2e.py). Every level label table
(analysis prompt, teacher dashboard, student report, ingestion CLI, the runner's and
PIN cards' YLE sets) covers every `Level` value. YLE "Reading & Writing" parts are
authored as `skill="reading"` sections, like Movers.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.analysis.llm_gemini import _LEVELS as ANALYSIS_LEVELS
from app.content import ContentService, FilesystemStorage
from app.core.db import get_session
from app.delivery import DeliveryService
from app.feedback.llm_gemini import _SYSTEM as FEEDBACK_SYSTEM
from app.grading import GradingService
from app.ingestion import cli as ingestion_cli
from app.persistence.repository import AttemptRepository, ContentRepository
from apps.api.main import app
from contracts import RosterEntry
from contracts.content import Level

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB = REPO_ROOT / "apps" / "web"
YLE = {"PRE_A1_STARTERS", "A1_MOVERS", "A2_FLYERS"}
NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)


def yle_test(test_id: str, level: str, title: str) -> dict[str, Any]:
    """A small YLE-shaped test: Listening (picture-free choice) + Reading & Writing."""
    return {
        "id": test_id,
        "title": title,
        "level": level,
        "duration_minutes": 20,
        "sections": [
            {
                "id": f"{test_id}-l1",
                "title": "Listening Part 1",
                "skill": "listening",
                "stimulus": {"kind": "passage_text", "text": "Listen and choose."},
                "items": [
                    {
                        "item_type": "single_choice",
                        "id": f"{test_id}-l1-q1",
                        "prompt": "What is Sam's favourite animal?",
                        "options": [
                            {"kind": "text", "key": "A", "text": "a cat"},
                            {"kind": "text", "key": "B", "text": "a dog"},
                            {"kind": "text", "key": "C", "text": "a fish"},
                        ],
                        "correct": "B",
                    }
                ],
            },
            {
                "id": f"{test_id}-rw1",
                "title": "Reading & Writing Part 1",
                # YLE R&W is authored as `reading` (Movers convention).
                "skill": "reading",
                "stimulus": {"kind": "passage_text", "text": "Look and read. Write one word."},
                "items": [
                    {
                        "item_type": "gap_fill",
                        "id": f"{test_id}-rw1-q1",
                        "prompt": "You can read this. It has pages. a ____",
                        "accepted": ["book"],
                    },
                    {
                        "item_type": "gap_fill",
                        "id": f"{test_id}-rw1-q2",
                        "prompt": "This is an orange fruit. a ____",
                        "accepted": ["orange"],
                    },
                ],
            },
        ],
    }


CASES = [
    ("starters-demo", "PRE_A1_STARTERS", "Starters — Demo"),
    ("flyers-demo", "A2_FLYERS", "Flyers — Demo"),
]


def test_level_literal_has_every_cambridge_level() -> None:
    assert set(get_args(Level)) == YLE | {
        "A2_KEY",
        "B1_PRELIMINARY",
        "B2_FIRST",
        "C1_ADVANCED",
        "C2_PROFICIENCY",
    }


@pytest.mark.parametrize(("test_id", "level", "title"), CASES)
def test_yle_test_imports_publishes_starts_and_grades(
    session: Session, tmp_path: Path, test_id: str, level: str, title: str
) -> None:
    content = ContentService(ContentRepository(session), FilesystemStorage(tmp_path))
    outcome = content.import_test(yle_test(test_id, level, title))
    assert outcome.test.level == level and outcome.test.status == "draft"
    assert content.publish(test_id)
    AttemptRepository(session).add_roster_entry(
        RosterEntry(id=f"{test_id}-r1", test_id=test_id, display_name="Kid")
    )
    session.commit()

    delivery = DeliveryService(ContentRepository(session), AttemptRepository(session))
    attempt = delivery.start(f"{test_id}-r1", now=NOW)  # no schedule -> always open
    client_test = delivery.client_test(attempt.id)
    assert client_test.level == level
    shown = [o.text for o in client_test.sections[0].items[0].options]  # type: ignore[union-attr]
    delivery.save_answer(attempt.id, f"{test_id}-l1-q1", shown.index("a dog"), now=NOW)
    delivery.save_answer(attempt.id, f"{test_id}-rw1-q1", "Book", now=NOW)
    delivery.save_answer(attempt.id, f"{test_id}-rw1-q2", "apple", now=NOW)
    delivery.submit(attempt.id, now=NOW)
    session.commit()

    result = GradingService(ContentRepository(session), AttemptRepository(session)).grade(
        attempt.id
    )
    assert (result.score, result.max_score) == (2, 3)
    assert not result.needs_review


@pytest.fixture
def exam_client(session: Session, tmp_path: Path) -> Iterator[TestClient]:
    content = ContentService(ContentRepository(session), FilesystemStorage(tmp_path))
    for test_id, level, title in CASES:
        content.import_test(yle_test(test_id, level, title))
        content.publish(test_id)
    session.commit()
    app.dependency_overrides[get_session] = lambda: session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(("test_id", "level", "title"), CASES)
def test_info_reports_the_yle_level(
    exam_client: TestClient, test_id: str, level: str, title: str
) -> None:
    info = exam_client.get(f"/exam/tests/{test_id}/info").json()
    assert (info["level"], info["title"], info["window_state"]) == (level, title, "open")


# --------------------------------------------------------------------------- #
# Every label table covers every level.
# --------------------------------------------------------------------------- #
def _js_object_keys(text: str, name: str) -> set[str]:
    m = re.search(rf"const {name} = \{{(.*?)\}};", text, re.S)
    assert m, name
    return set(re.findall(r"\b([A-Z][A-Z0-9_]+):", m.group(1)))


def _js_set(text: str, name: str) -> set[str]:
    m = re.search(rf"const {name} = new Set\(\[(.*?)\]\)", text, re.S)
    assert m, name
    return set(re.findall(r'"([A-Z0-9_]+)"', m.group(1)))


def test_every_label_table_covers_every_level() -> None:
    levels = set(get_args(Level))
    assert set(ANALYSIS_LEVELS) == levels
    assert _js_object_keys((WEB / "teacher.js").read_text(encoding="utf-8"), "LEVELS") == levels
    report = (WEB / "teacher-report.html").read_text(encoding="utf-8")
    assert _js_object_keys(report, "LEVELS") == levels
    # The YLE sets (uk/ru UI in the runner, trilingual PIN cards) = exactly the YLE codes.
    assert _js_set((WEB / "exam.js").read_text(encoding="utf-8"), "YLE_LEVELS") == YLE
    assert _js_set((WEB / "teacher-pins.html").read_text(encoding="utf-8"), "YLE_LEVELS") == YLE
    # Gemini's feedback prompt names the YLE levels too.
    for name in ("Starters", "Movers", "Flyers", "A2 Key", "B1 Preliminary"):
        assert name in FEEDBACK_SYSTEM


def test_ingestion_cli_accepts_every_level(monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    seen: dict[str, Any] = {}
    real = argparse.ArgumentParser.parse_args

    def spy(self: argparse.ArgumentParser, argv: list[str] | None = None) -> Any:
        args = real(self, argv)
        seen["level"] = args.level
        raise SystemExit(0)  # stop before the real pipeline

    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", spy)
    for level in get_args(Level):
        with pytest.raises(SystemExit):
            ingestion_cli.main(["--path", "q.pdf", "--key", "k.pdf", "--level", level])
        assert seen["level"] == level
