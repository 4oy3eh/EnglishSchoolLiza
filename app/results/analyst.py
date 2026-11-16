"""The writing second-opinion analyst the finalize path uses, built from settings.

The integrity verdict of record is the deterministic rule engine; this module only
decides whether an LLM is ALSO asked for a second opinion on the writing answers
(`AnalysisService` then calls it for writing only):

* `ANALYSIS_LLM=writing` (default) + `GEMINI_API_KEY` set -> a `GeminiAnalyst`;
* `ANALYSIS_LLM=off`, or no key -> None: no Gemini analysis call is ever made
  (writing grading / feedback are separate calls and unaffected).

Mirrors `finalize._writing_grader` / `_feedback_analyst`. Lives in its own module so
both `ResultsService` (constructed by the admin layer for a teacher regrade, without
an analyst) and the background `finalize` factory resolve the SAME analyst, and so
tests can stub it in one place (`tests/conftest.py` patches `default_analysis_llm`
to None, so no test ever calls the real API even when a developer's `.env` carries
a key).
"""

from __future__ import annotations

from functools import lru_cache

from app.analysis.llm import AnalysisLLM
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)


@lru_cache(maxsize=1)
def _build() -> AnalysisLLM | None:
    if settings.analysis_llm == "off":
        log.info(
            "analysis analyst: ANALYSIS_LLM=off -> rule-engine verdicts only "
            "(no Gemini analysis call)"
        )
        return None
    if settings.gemini_api_key:
        from app.analysis.llm_gemini import GeminiAnalyst

        log.info(
            "analysis analyst: ANALYSIS_LLM=%s -> gemini model=%s (second opinion on "
            "writing only)",
            settings.analysis_llm,
            settings.gemini_model,
        )
        return GeminiAnalyst(api_key=settings.gemini_api_key, model=settings.gemini_model)
    log.info(
        "analysis analyst: ANALYSIS_LLM=%s but no GEMINI_API_KEY -> rule-engine "
        "verdicts only",
        settings.analysis_llm,
    )
    return None


def default_analysis_llm() -> AnalysisLLM | None:
    """The configured writing analyst (Gemini) or None. Call through the module attribute."""
    return _build()
