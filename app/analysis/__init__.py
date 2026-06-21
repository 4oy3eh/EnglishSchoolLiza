"""Analysis engine: an advisory LLM cheating-likelihood verdict.

Public surface:

* `AnalysisService` — read an attempt's integrity profile + raw segments and build
  an advisory `AnalysisVerdict`.
* `AnalysisLLM` protocol + `MockAnalysisLLM` + `VerdictDraft` for the verdict path.
* `flag_segments` / `FlaggedSegment` — the deterministic raw-segment selection.

Layer 3 of the three integrity layers (golden rule #6); **advisory only and never
touches a score** (golden rule #2). The real Anthropic-backed analyst lives in
`app.analysis.llm_anthropic` and is NOT imported here, so importing this package
never requires the `anthropic` SDK.
"""

from __future__ import annotations

from app.analysis.llm import AnalysisLLM, MockAnalysisLLM, VerdictDraft
from app.analysis.segments import FlaggedSegment, flag_segments
from app.analysis.service import AnalysisService

__all__ = [
    "AnalysisService",
    "AnalysisLLM",
    "MockAnalysisLLM",
    "VerdictDraft",
    "flag_segments",
    "FlaggedSegment",
]
