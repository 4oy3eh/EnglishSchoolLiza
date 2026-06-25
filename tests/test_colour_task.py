"""ColourTaskItem (YLE 'listen and colour') + stimulus context images.

Covers the contract additions used by the Movers ingest: the colour task projects
to a keyless client item, grading routes it to teacher review (never auto-scored),
and the loader validates both the colour asset and stimulus context images.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.content.load_test import referenced_asset_ids
from app.delivery.projection import project_item
from app.grading import GradingService
from app.persistence.repository import AttemptRepository, ContentRepository
from contracts import (
    AudioAssetStimulus,
    ColourTaskItem,
    PassageTextStimulus,
    Section,
    SingleChoiceItem,
    TextOption,
)
from contracts import Test as ExamTest


def test_colour_task_projects_without_key() -> None:
    item = ColourTaskItem(
        id="c1", prompt="Colour it", asset_id="scene.png", palette=["blue", "red"], key="clock=blue"
    )
    client = project_item(item, None)
    blob = client.model_dump_json()
    assert client.item_type == "colour_task"
    assert client.asset_id == "scene.png"
    assert client.palette == ["blue", "red"]
    # The colouring solution must never reach the student.
    assert '"key"' not in blob
    assert "clock=blue" not in blob


def test_referenced_asset_ids_covers_stimulus_images_and_colour() -> None:
    test = ExamTest(
        id="t",
        title="Movers",
        level="A1_MOVERS",
        status="draft",
        duration_minutes=30,
        sections=[
            Section(
                id="s1",
                skill="listening",
                stimulus=AudioAssetStimulus(asset_id="aud.mp3", images=["form.png"]),
                items=[ColourTaskItem(id="c1", prompt="Colour", asset_id="line.png")],
            ),
            Section(
                id="s2",
                skill="reading",
                stimulus=PassageTextStimulus(text="read", images=["bank.png"]),
                items=[
                    SingleChoiceItem(
                        id="q2",
                        prompt="pick",
                        options=[TextOption(key="A", text="a"), TextOption(key="B", text="b")],
                        correct="A",
                    )
                ],
            ),
        ],
    )
    assert referenced_asset_ids(test) == {"aud.mp3", "form.png", "line.png", "bank.png"}


def test_colour_task_routes_to_teacher_review(session: Session) -> None:
    svc = GradingService(ContentRepository(session), AttemptRepository(session))
    item = ColourTaskItem(id="q-col", prompt="Colour", asset_id="x.png")

    grade = svc._grade_item(item, "data:image/png;base64,AAAA")

    assert grade.needs_review is True
    assert grade.method == "colour_manual"
    assert grade.awarded == 0.0
