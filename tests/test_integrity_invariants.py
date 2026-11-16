"""Cross-engine invariants for the cheating detector (golden rules #2 and #6).

* The advisory verdict contract (`AnalysisVerdict`, `WritingAssessment`) and the
  integrity features (`IntegrityProfile`, `WritingFeatures`) carry NO score field.
* Grading never imports integrity / analysis / telemetry (AST-level, so docstrings
  may name the layers).
* Telemetry imports no integrity / analysis / grading; integrity imports no LLM,
  analysis or grading — the three layers stay separate.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from contracts import AnalysisVerdict, IntegrityProfile, WritingAssessment, WritingFeatures

APP = Path(__file__).resolve().parent.parent / "app"

# Names that would mean a grade leaked into the integrity axis.
_SCORE_KEYS = {"score", "awarded", "max_score", "max_points", "points", "grade", "grading"}


def _property_names(schema: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                names.update(value)
            names |= _property_names(value)
    elif isinstance(schema, list):
        for value in schema:
            names |= _property_names(value)
    return names


@pytest.mark.parametrize(
    "model", [AnalysisVerdict, WritingAssessment, IntegrityProfile, WritingFeatures]
)
def test_integrity_and_verdict_contracts_have_no_score_fields(model: type) -> None:
    names = _property_names(model.model_json_schema())
    assert names, model.__name__
    assert not names & _SCORE_KEYS, (model.__name__, names & _SCORE_KEYS)


def _imports(package: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for path in (APP / package).glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                out.append((path.name, node.module))
            elif isinstance(node, ast.Import):
                out.extend((path.name, alias.name) for alias in node.names)
    return out


def _assert_no_import(package: str, forbidden: tuple[str, ...]) -> None:
    for fname, module in _imports(package):
        assert not any(module == f or module.startswith(f + ".") for f in forbidden), (
            f"app/{package}/{fname} imports {module}"
        )


def test_grading_never_imports_integrity_analysis_or_telemetry() -> None:
    _assert_no_import("grading", ("app.integrity", "app.analysis", "app.telemetry"))


def test_telemetry_is_capture_only() -> None:
    _assert_no_import("telemetry", ("app.integrity", "app.analysis", "app.grading"))


def test_integrity_has_no_llm_analysis_grading_or_telemetry() -> None:
    _assert_no_import(
        "integrity",
        ("app.analysis", "app.grading", "app.telemetry", "anthropic", "httpx", "openai"),
    )


def test_analysis_never_imports_grading() -> None:
    _assert_no_import("analysis", ("app.grading", "app.feedback", "app.results"))
