"""Golden rule #1: correct answers NEVER reach the client.

No student-facing model may carry an answer-key field anywhere in its schema.
This walks the full JSON Schema (including `$defs`) of every client-facing
contract and asserts those fields are structurally absent.
"""

from typing import Any

import pytest

from contracts import (
    CLIENT_FACING,
    REGISTRY,
    ClientDrawingItem,
    ClientGapFillItem,
    ClientMarkToggleItem,
    ClientSection,
    ClientSingleChoiceItem,
    ClientStudyInfo,
    Item,
)
from contracts import (
    Test as ExamTest,
)
from contracts.export_jsonschema import schema_for

# Fields that encode the answer key. None may appear on a client model.
FORBIDDEN_ON_CLIENT = {
    "correct",
    "accepted",
    "accepted_variants",
    "rejected",
    "rubric",
    "answers",  # key word transformation patterns
    "part_a",
    "part_b",
    # IQ family (Phase 17): drawing targets, mark_toggle `correct` (above), norm tables
    "target",
    "zones",
    "segments",
    "tolerance",
    "min_inside_ratio",
    "norm_scoring",
    "scaled_tables",
    "by_age",
    "composites",
}


def _all_property_names(schema: dict[str, Any]) -> set[str]:
    """Every property name appearing anywhere in a JSON Schema, including $defs."""
    names: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                names.update(props.keys())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return names


@pytest.mark.parametrize("name", CLIENT_FACING)
def test_client_schema_has_no_answer_key(name: str) -> None:
    schema = schema_for(REGISTRY[name])
    leaked = _all_property_names(schema) & FORBIDDEN_ON_CLIENT
    assert not leaked, f"{name} leaks answer-key field(s): {leaked}"


def test_client_models_drop_correct_field() -> None:
    assert "correct" not in ClientSingleChoiceItem.model_fields
    assert "accepted" not in ClientGapFillItem.model_fields
    assert "correct" not in ClientMarkToggleItem.model_fields
    assert "target" not in ClientDrawingItem.model_fields


def test_bulk_client_section_carries_no_study_content() -> None:
    # The study stimulus is served on its own, only inside the study window: the
    # bulk section knows only that a study phase exists and how long it lasts.
    assert set(ClientStudyInfo.model_fields) == {"study_seconds"}
    assert ClientSection.model_fields["study"].annotation == ClientStudyInfo | None


def test_norm_scoring_is_authoring_only() -> None:
    assert "norm_scoring" in ExamTest.model_fields
    assert "norm_scoring" not in _all_property_names(schema_for(REGISTRY["ClientTest"]))


def test_invariant_test_is_meaningful() -> None:
    # Sanity: the authoring Item DOES carry `correct`, so the check above is real.
    authoring_props = _all_property_names(schema_for(Item))
    assert "correct" in authoring_props
    assert {"target", "zones", "segments", "tolerance"} <= authoring_props
