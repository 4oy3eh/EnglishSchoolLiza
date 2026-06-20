"""The committed JSON Schema must stay in sync with the Pydantic contracts.

If this fails, run `make schema` and commit the regenerated files (CLAUDE.md #4).
"""

import json
from pathlib import Path

import pytest

from contracts import REGISTRY
from contracts.export_jsonschema import OUT_DIR, schema_for


@pytest.mark.parametrize("name", list(REGISTRY))
def test_committed_schema_matches_models(name: str) -> None:
    path: Path = OUT_DIR / f"{name}.json"
    assert path.exists(), f"missing generated schema {path.name}; run `make schema`"
    committed = json.loads(path.read_text("utf-8"))
    assert committed == schema_for(REGISTRY[name]), (
        f"{name}.json is stale; run `make schema` and commit"
    )
