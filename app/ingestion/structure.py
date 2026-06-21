"""Structuring seam: extracted document -> `DraftTest` (sections/items, no key).

The engine depends only on the `LLMStructurer` *protocol*. The real multimodal
Anthropic-backed implementation lives in `llm_anthropic.py` (lazy SDK import) and
tests inject `MockLLMStructurer`, so the pipeline never hard-depends on a network
call. Mirrors grading's `LLMGrader` / analysis's `AnalysisLLM` seam.

The structurer produces a **draft only** (golden rule #5): it lays out sections,
stimuli, and items with their options, but never the answer key — `correct` /
`accepted` come from the answer-key PDF via `answer_key.merge_key`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.ingestion.extract import ExtractedDocument
from app.ingestion.models import DraftTest
from contracts.content import Level


@runtime_checkable
class LLMStructurer(Protocol):
    """Lay out an extracted paper into a `DraftTest` (no answer key)."""

    def structure(
        self, document: ExtractedDocument, *, level: Level, test_id: str
    ) -> DraftTest: ...


class MockLLMStructurer:
    """Deterministic stand-in for tests / offline runs.

    Returns a pre-canned `DraftTest` regardless of the document, so a golden pipeline
    run is hermetic (no LLM call). Construct it with the draft the test expects the
    structurer to "produce".
    """

    model_id = "mock-structurer"

    def __init__(self, draft: DraftTest) -> None:
        self._draft = draft

    def structure(
        self, document: ExtractedDocument, *, level: Level, test_id: str
    ) -> DraftTest:
        # Honour the caller's id/level so the draft is consistent with the request.
        return self._draft.model_copy(update={"id": test_id, "level": level})
