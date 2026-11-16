"""Integrity engine: deterministic features over the telemetry event stream.

Public surface:

* `IntegrityService` — read an attempt's events and build its `IntegrityProfile`.
* `extract_profile` — the pure `events -> IntegrityProfile` extractor.
* `POST_RETURN_FAST_MS` — the post-return "answered immediately" threshold.
* `extract_writing_features` — the pure per-writing-item features + flags
  (thresholds live in `app.integrity.writing`).
* `evaluate_rules` / `RuleReport` / `RULE_TITLES` / `RULES_VERSION` — the
  deterministic rule engine (`app.integrity.rules`): explainable hits + a bounded
  suspicion, the advisory verdict of record.

Deterministic and LLM-free (golden rule #6): `app/analysis` only assembles the
verdict and may add an LLM second opinion on writing. `tests/test_integrity.py`
asserts the import graph carries no grading/analysis/LLM dependency.
"""

from __future__ import annotations

from app.integrity.features import POST_RETURN_FAST_MS, extract_profile
from app.integrity.rules import RULE_TITLES, RULES_VERSION, RuleReport, evaluate_rules
from app.integrity.service import IntegrityService
from app.integrity.writing import extract_writing_features

__all__ = [
    "IntegrityService",
    "extract_profile",
    "POST_RETURN_FAST_MS",
    "extract_writing_features",
    "evaluate_rules",
    "RuleReport",
    "RULE_TITLES",
    "RULES_VERSION",
]
