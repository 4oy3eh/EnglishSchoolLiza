"""Phase 17b: the step-by-step subtest intro's data (contracts + projection).

* `IntroPractice` shapes per kind (text / choice / mark / drawing) are validated;
* the intro (incl. its public practice problems) is served with the section, and
  `ClientSection.item_count` survives the timed-section item withholding (the
  Ready screen's "N questions");
* practice pictures are checked / referenced like every other intro picture;
* `GapFillItem.numeric` reaches the client (numeric keyboard), default off;
* the intro cap default covers the three intro screens (420 s).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.content.editor import referenced_asset_ids, validate_test_data
from app.content.pooling import AttemptLayout
from app.core.config import Settings
from app.delivery.projection import project_section, project_test
from contracts import IntroPractice
from contracts import Test as ExamTest
from tests import iq_fixture as fx

TID = "iq-demo"


def _layout(test: ExamTest) -> AttemptLayout:
    return AttemptLayout(
        test_id=TID, seed=1, section_ids=tuple(s.id for s in test.sections), option_shuffles={}
    )


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "text", "solution": "8"},
        {"kind": "choice", "options": [{"key": "a"}, {"key": "b"}], "solution": "b"},
        {"kind": "mark", "prompt": "12 .. 21", "solution": "1"},
        {"kind": "mark", "prompt": "12 .. 12", "solution": "0"},
        {"kind": "drawing", "images": ["p.png"], "solution_images": ["a.png"]},
        {"kind": "drawing", "images": ["p.png"], "explanation": "No printed answer."},
    ],
)
def test_practice_shapes_accepted(data: dict[str, Any]) -> None:
    assert IntroPractice.model_validate(data).kind == data["kind"]


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "text", "solution": "  "},  # a text practice needs its answer
        {"kind": "choice", "options": [{"key": "a"}], "solution": "a"},  # < 2 options
        {"kind": "choice", "options": [{"key": "a"}, {"key": "a"}], "solution": "a"},
        {"kind": "choice", "options": [{"key": "a"}, {"key": "b"}], "solution": "z"},
        {"kind": "mark", "solution": "yes"},
        {"kind": "text", "solution": "x", "options": [{"key": "a"}, {"key": "b"}]},
        {"kind": "drawing", "images": ["p.png"]},  # nothing to show as the answer
        {"kind": "drawing", "solution_images": ["a.png"]},  # nothing to draw on
        {"kind": "puzzle", "solution": "x"},
    ],
)
def test_practice_shapes_refused(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        IntroPractice.model_validate(data)


def test_intro_practice_is_served_and_item_count_survives_withholding() -> None:
    test = ExamTest.model_validate(fx.iq_test_json(TID))
    client = project_test(test, _layout(test))
    pairs, drawing, memory = client.sections
    assert [p.kind for p in pairs.intro.practice] == ["mark", "mark"]  # type: ignore[union-attr]
    assert drawing.intro.practice[0].solution_images == ["iq-practice-answer.png"]  # type: ignore[union-attr]
    assert [p.kind for p in memory.intro.practice] == ["text", "choice"]  # type: ignore[union-attr]
    assert (pairs.item_count, drawing.item_count, memory.item_count) == (6, 2, 2)
    # The delivery service withholds a timed section's items with model_copy: the
    # count stays (the Ready screen needs it before Begin).
    held = pairs.model_copy(update={"items": [], "items_withheld": True})
    assert held.item_count == 6 and held.items == []


def test_practice_pictures_are_checked_and_referenced() -> None:
    test = ExamTest.model_validate(fx.iq_test_json(TID))
    refs = referenced_asset_ids(test)
    assert {"iq-practice.png", "iq-practice-answer.png"} <= refs
    data = fx.iq_test_json(TID)
    data["sections"][1]["intro"]["practice"][0]["images"] = ["../evil.png"]
    with pytest.raises(Exception, match="тренировочное"):
        validate_test_data(data)


def test_gap_fill_numeric_reaches_the_client() -> None:
    data = fx.iq_test_json(TID)
    data["sections"][2]["items"].append(
        {
            "item_type": "gap_fill",
            "id": f"{TID}-n1",
            "prompt": "Number for this symbol",
            "accepted": ["7"],
            "numeric": True,
        }
    )
    test = ExamTest.model_validate(data)
    cs = project_section(test.sections[2], _layout(test))
    gap = cs.items[-1]
    assert gap.item_type == "gap_fill" and gap.numeric is True  # type: ignore[union-attr]
    assert "accepted" not in gap.model_dump()
    plain = ExamTest.model_validate(fx.iq_test_json(TID))
    assert all(getattr(i, "numeric", False) is False for s in plain.sections for i in s.items)


def test_intro_cap_default_covers_three_screens() -> None:
    assert Settings.model_fields["section_intro_max_seconds"].default == 420
