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
    ClientGapFillItem,
    ClientSingleChoiceItem,
    Item,
)
from contracts.export_jsonschema import schema_for

# Fields that encode the answer key. None may appear on a client model.
FORBIDDEN_ON_CLIENT = {"correct", "accepted", "accepted_variants", "rubric"}


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


def test_invariant_test_is_meaningful() -> None:
    # Sanity: the authoring Item DOES carry `correct`, so the check above is real.
    authoring_props = _all_property_names(schema_for(Item))
    assert "correct" in authoring_props
