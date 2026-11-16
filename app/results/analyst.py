"""The advisory analyst the finalize path uses, built once from settings.

Mirrors `finalize._writing_grader` / `_feedback_analyst`: a `GeminiAnalyst` when
`GEMINI_API_KEY` is configured, else None (-> the analysis engine's
deterministic-only verdict). Lives in its own module so both `ResultsService`
(constructed by the admin layer for a teacher regrade, without an analyst) and
the background `finalize` factory resolve the SAME analyst, and so tests can stub
it in one place (`tests/conftest.py` patches `default_analysis_llm` to None, so no
test ever calls the real API even when a developer's `.env` carries a key).
"""

from __future__ import annotations

from functools import lru_cache

from app.analysis.llm import AnalysisLLM
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)


@lru_cache(maxsize=1)
def _build() -> AnalysisLLM | None:
    if settings.gemini_api_key:
        from app.analysis.llm_gemini import GeminiAnalyst

        log.info("analysis analyst: gemini model=%s", settings.gemini_model)
        return GeminiAnalyst(api_key=settings.gemini_api_key, model=settings.gemini_model)
    log.info("analysis analyst: none configured (deterministic-only verdicts)")
    return None


def default_analysis_llm() -> AnalysisLLM | None:
    """The configured analyst (Gemini) or None. Call through the module attribute."""
    return _build()
