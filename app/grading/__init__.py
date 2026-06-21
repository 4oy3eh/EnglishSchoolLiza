"""Grading engine: deterministic objective grading + writing (LLM/manual).

Public surface:

* `GradingService` — grade an attempt, manual override, needs-review queue.
* deterministic graders (`grade_single_choice` / `grade_matching` / `grade_gap_fill`)
  and `normalize` for gap-fill comparison.
* `LLMGrader` protocol + `MockLLMGrader` + `WritingGrade` for the writing path.
* `GradingError`.

The real Anthropic-backed grader lives in `app.grading.llm_anthropic` and is NOT
imported here, so importing this package never requires the `anthropic` SDK.
"""

from __future__ import annotations

from app.grading.deterministic import (
    DEFAULT_FUZZY_THRESHOLD,
    DETERMINISTIC_POINTS,
    grade_gap_fill,
    grade_matching,
    grade_single_choice,
)
from app.grading.llm import (
    DEFAULT_WRITING_POINTS,
    LLMGrader,
    MockLLMGrader,
    WritingGrade,
)
from app.grading.normalize import normalize
from app.grading.service import GradingError, GradingService

__all__ = [
    "GradingService",
    "GradingError",
    "grade_single_choice",
    "grade_matching",
    "grade_gap_fill",
    "normalize",
    "DETERMINISTIC_POINTS",
    "DEFAULT_FUZZY_THRESHOLD",
    "LLMGrader",
    "MockLLMGrader",
    "WritingGrade",
    "DEFAULT_WRITING_POINTS",
]
