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
    DETERMINISTIC_POINTS,
    grade_gap_fill,
    grade_matching,
    grade_single_choice,
    objective_points,
)
from app.grading.llm import (
    DEFAULT_WRITING_POINTS,
    LLMGrader,
    MockLLMGrader,
    WritingGrade,
    scale_writing_score,
    writing_points,
)
from app.grading.normalize import normalize
from app.grading.service import GradingError, GradingService, item_max_points

__all__ = [
    "GradingService",
    "GradingError",
    "grade_single_choice",
    "grade_matching",
    "grade_gap_fill",
    "normalize",
    "DETERMINISTIC_POINTS",
    "LLMGrader",
    "MockLLMGrader",
    "WritingGrade",
    "DEFAULT_WRITING_POINTS",
    "objective_points",
    "writing_points",
    "scale_writing_score",
    "item_max_points",
]
