"""Phase 1 gate: valid fixtures parse, malformed fixtures raise."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

# Reference Test via the module so pytest doesn't try to collect `Test*`.
from contracts import (
    AnalysisVerdict,
    Attempt,
    GapFillItem,
    SingleChoiceItem,
    content,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text("utf-8"))


def test_valid_test_fixture_parses() -> None:
    test = content.Test.model_validate(_load("valid_test.json"))
    assert test.level == "B1_PRELIMINARY"
    assert len(test.sections) == 3
    # discriminated unions resolve to the right concrete types
    first_item = test.sections[0].items[0]
    assert isinstance(first_item, SingleChoiceItem)
    assert first_item.correct == "B"


def test_too_few_options_raises() -> None:
    with pytest.raises(ValidationError):
        content.Test.model_validate(_load("invalid_test_too_few_options.json"))


def test_unknown_field_raises() -> None:
    with pytest.raises(ValidationError):
        SingleChoiceItem.model_validate(
            {
                "item_type": "single_choice",
                "id": "q",
                "prompt": "?",
                "options": [
                    {"kind": "text", "key": "A", "text": "a"},
                    {"kind": "text", "key": "B", "text": "b"},
                ],
                "correct": "A",
                "surprise": "not allowed",
            }
        )


def test_gap_fill_requires_accepted() -> None:
    with pytest.raises(ValidationError):
        GapFillItem.model_validate(
            {"item_type": "gap_fill", "id": "q", "prompt": "?", "accepted": []}
        )


def test_suspicion_score_out_of_range_raises() -> None:
    with pytest.raises(ValidationError):
        AnalysisVerdict.model_validate(
            {"attempt_id": "a", "suspicion_score": 1.5, "confidence": 0.5, "summary": "x"}
        )


def test_attempt_roundtrips() -> None:
    now = datetime.now(UTC)
    attempt = Attempt(
        id="att-1", test_id="t-1", roster_entry_id="r-1", seed=42, started_at=now
    )
    again = Attempt.model_validate(attempt.model_dump())
    assert again == attempt
    assert again.status == "not_started"
