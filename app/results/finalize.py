"""Background finalize entrypoint — wire a `ResultsService` from settings + run it.

Called as a FastAPI background task right after an attempt is submitted (see
`apps/api/delivery.py`). It opens its OWN session (the request session is already
closed by the time the task runs) and commits via `session_scope`, then grades +
reports the attempt once and persists the frozen `AttemptReport`.

The Gemini grader/analyst are built only when `GEMINI_API_KEY` is configured;
without it, writing falls back to manual review and the report is scores-only.
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import session_scope
from app.core.logging import get_logger
from app.feedback import FeedbackService
from app.feedback.llm import FeedbackLLM
from app.grading.llm import LLMGrader
from app.grading.service import GradingService
from app.persistence.repository import AttemptRepository, ContentRepository
from app.results.service import ResultsService

log = get_logger(__name__)


@lru_cache(maxsize=1)
def _writing_grader() -> LLMGrader | None:
    if settings.gemini_api_key:
        from app.grading.llm_gemini import GeminiWritingGrader

        return GeminiWritingGrader(api_key=settings.gemini_api_key, model=settings.gemini_model)
    return None


@lru_cache(maxsize=1)
def _feedback_analyst() -> FeedbackLLM | None:
    if settings.gemini_api_key:
        from app.feedback.llm_gemini import GeminiFeedbackAnalyst

        return GeminiFeedbackAnalyst(api_key=settings.gemini_api_key, model=settings.gemini_model)
    return None


def build_results_service(
    session: Session,
    *,
    llm_grader: LLMGrader | None = None,
    feedback_llm: FeedbackLLM | None = None,
) -> ResultsService:
    """Construct a `ResultsService` and the engines it composes from a session."""
    content_repo = ContentRepository(session)
    attempt_repo = AttemptRepository(session)
    return ResultsService(
        content=content_repo,
        attempts=attempt_repo,
        grading=GradingService(content_repo, attempt_repo, llm_grader=llm_grader),
        feedback=FeedbackService(llm=feedback_llm),
    )


def finalize_attempt(attempt_id: str) -> None:
    """Finalize one attempt in its own transaction (FastAPI background task).

    Swallows errors after logging: a background task must never crash the worker,
    and the teacher can always re-grade from the dashboard if the report is missing.
    """
    try:
        with session_scope() as session:
            svc = build_results_service(
                session,
                llm_grader=_writing_grader(),
                feedback_llm=_feedback_analyst(),
            )
            svc.finalize(attempt_id)
        log.info("background finalize attempt=%s done", attempt_id)
    except Exception:  # never let a background task take down the process
        log.exception("background finalize attempt=%s failed", attempt_id)
