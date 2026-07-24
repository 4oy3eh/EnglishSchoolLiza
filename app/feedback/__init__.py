"""Feedback engine — per-skill pedagogical strengths & weaknesses for an attempt.

Public surface (no SDK import here; the real analyst lives in `llm_gemini.py` and is
constructed only when a key is configured). This is the academic-performance axis:
it reads grading + content, never the integrity stream (golden rules #2/#6).
"""

from __future__ import annotations

from app.feedback.llm import (
    FeedbackLLM,
    ItemContext,
    MockFeedbackAnalyst,
    ReportDraft,
    SkillBreakdown,
    SkillVerdict,
)
from app.feedback.service import FeedbackService

__all__ = [
    "FeedbackService",
    "FeedbackLLM",
    "MockFeedbackAnalyst",
    "ReportDraft",
    "SkillBreakdown",
    "SkillVerdict",
    "ItemContext",
]
