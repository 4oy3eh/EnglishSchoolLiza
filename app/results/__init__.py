"""Results engine — finalize an attempt once (grade + report) and persist it.

Public surface. The composition that runs on submit: grade the attempt and build its
per-skill feedback exactly once, persisting a frozen `AttemptReport` the dashboard
reads instead of recomputing. `finalize_attempt` is the background-task entrypoint.
"""

from __future__ import annotations

from app.results.finalize import build_results_service, finalize_attempt
from app.results.service import ResultsError, ResultsService

__all__ = [
    "ResultsService",
    "ResultsError",
    "build_results_service",
    "finalize_attempt",
]
