"""Integrity engine: deterministic features over the telemetry event stream.

Public surface:

* `IntegrityService` — read an attempt's events and build its `IntegrityProfile`.
* `extract_profile` — the pure `events -> IntegrityProfile` extractor.
* `POST_RETURN_FAST_MS` — the post-return "answered immediately" threshold.

Deterministic and LLM-free (golden rule #6): the advisory verdict is Phase 8
(`app/analysis`). `tests/test_integrity.py` asserts the import graph carries no
grading/analysis/LLM dependency.
"""

from __future__ import annotations

from app.integrity.features import POST_RETURN_FAST_MS, extract_profile
from app.integrity.service import IntegrityService

__all__ = [
    "IntegrityService",
    "extract_profile",
    "POST_RETURN_FAST_MS",
]
