"""Real `LLMStructurer` backed by the Anthropic Messages API (multimodal).

Kept out of `app/ingestion/__init__.py` and importing the SDK lazily, so the engine
has no hard runtime dependency on `anthropic` — tests use `MockLLMStructurer` and only
this module pulls the SDK, only when actually constructed. Mirrors
`app/analysis/llm_anthropic.py`.

Produces a **draft only** (golden rule #5): the model lays out sections/items with
their options from the question paper, and is asked NOT to guess the answer key — the
answer-key PDF is authoritative for `correct` and is merged in afterwards. Every call
logs model id + token usage + latency (golden rule #8). Uses structured outputs
(`messages.parse`) so the draft comes back as a validated `DraftTest`.
"""

from __future__ import annotations

import base64
import time
from typing import TYPE_CHECKING, Any

from app.core.logging import get_logger
from app.ingestion.extract import ExtractedDocument
from app.ingestion.models import DraftTest
from contracts.content import Level

if TYPE_CHECKING:
    from anthropic import Anthropic

log = get_logger(__name__)

DEFAULT_MODEL = "claude-opus-4-8"

_SYSTEM = (
    "You convert a Cambridge English exam question paper into a STRUCTURED DRAFT. "
    "Lay out the sections (each with its shared stimulus: passage text, audio asset, "
    "image set, gapped text, or matching pool) and the items in order, preserving the "
    "Cambridge question numbers. For single_choice items include every option with its "
    "A/B/C key. DO NOT guess or invent the answer key: never mark which option is "
    "correct and never fill gap-fill answers — the answer-key PDF is authoritative and "
    "is merged in separately. Never reorder items within a section."
)


class AnthropicStructurer:
    """Multimodal draft structurer using Claude."""

    def __init__(
        self,
        *,
        client: Anthropic | None = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 8192,
    ) -> None:
        if client is None:
            from anthropic import Anthropic  # lazy: only needed for the real path

            client = Anthropic()
        self._client = client
        self.model = model
        self.max_tokens = max_tokens

    def structure(
        self, document: ExtractedDocument, *, level: Level, test_id: str
    ) -> DraftTest:
        content = self._build_content(document, level=level, test_id=test_id)

        start = time.perf_counter()
        message = self._client.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            system=_SYSTEM,
            # Our hand-built multimodal blocks are looser than the SDK's TypedDicts.
            messages=[{"role": "user", "content": content}],  # type: ignore[typeddict-item]
            output_format=DraftTest,
        )
        latency_ms = (time.perf_counter() - start) * 1000.0

        usage = message.usage
        log.info(
            "llm structure test=%s model=%s in_tokens=%d out_tokens=%d latency=%.0fms",
            test_id,
            message.model,
            usage.input_tokens,
            usage.output_tokens,
            latency_ms,
        )
        return self._extract(message)

    @staticmethod
    def _build_content(
        document: ExtractedDocument, *, level: Level, test_id: str
    ) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"TEST ID: {test_id}\nLEVEL: {level}\n\n"
                    f"QUESTION PAPER TEXT:\n{document.text}"
                ),
            }
        ]
        for crop in document.crops:
            blocks.append(
                {
                    "type": "text",
                    "text": f"(image asset_id={crop.asset_id}, page {crop.page})",
                }
            )
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": crop.content_type,
                        "data": base64.b64encode(crop.data).decode("ascii"),
                    },
                }
            )
        return blocks

    @staticmethod
    def _extract(message: object) -> DraftTest:
        from anthropic.types import ParsedTextBlock

        for block in getattr(message, "content", []):
            if isinstance(block, ParsedTextBlock) and block.parsed_output is not None:
                parsed = block.parsed_output
                if isinstance(parsed, DraftTest):
                    return parsed
        # Surface the boundary loudly (log-before-raise style used across the codebase).
        log.error("LLM structurer returned no parseable draft test")
        raise RuntimeError("LLM structurer returned no parseable draft test")
