"""Analysis engine: an advisory LLM cheating-likelihood verdict.

Public surface:

* `AnalysisService` — read an attempt's integrity profile + raw segments and build
  an advisory `AnalysisVerdict`.
* `AnalysisLLM` protocol + `MockAnalysisLLM` + `VerdictDraft` for the verdict path.
* `flag_segments` / `FlaggedSegment` — the deterministic raw-segment selection.

Layer 3 of the three integrity layers (golden rule #6); **advisory only and never
touches a score** (golden rule #2). The real analysts (`app.analysis.llm_gemini`
— the wired one — and `app.analysis.llm_anthropic`) are NOT imported here, so
importing this package never requires an SDK or a key.
"""

from __future__ import annotations

from app.analysis.fallback import deterministic_verdict
from app.analysis.llm import (
    AnalysisContext,
    AnalysisLLM,
    MockAnalysisLLM,
    VerdictDraft,
    WritingTask,
)
from app.analysis.segments import FlaggedSegment, flag_segments
from app.analysis.service import AnalysisService

__all__ = [
    "AnalysisService",
    "AnalysisLLM",
    "MockAnalysisLLM",
    "VerdictDraft",
    "AnalysisContext",
    "WritingTask",
    "deterministic_verdict",
    "flag_segments",
    "FlaggedSegment",
]
