"""`python -m app.ingestion.cli` — run ingestion from the command line (`make ingest`).

Wires the real seams (PyMuPDF extractor, Anthropic structurer, WhisperX ASR — each a
lazy import inside its module) and runs the `IngestionPipeline`, printing every step
to cmd via the shared logger (golden rule logging). This is a dry run of the pipeline:
it produces and reports the validated **draft** but does not persist it (queuing into
the bank needs a DB session and is the `IngestionService`/worker path). It never
publishes (golden rule #5).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.core.logging import configure_logging, get_logger
from app.ingestion.pipeline import IngestionPipeline, IngestionRequest

log = get_logger(__name__)


def _build_pipeline(*, with_audio: bool) -> IngestionPipeline:
    # Lazy real seams: only constructed when the CLI actually runs.
    from app.ingestion.extract_pymupdf import PyMuPdfExtractor
    from app.ingestion.llm_anthropic import AnthropicStructurer

    asr = None
    if with_audio:
        from app.ingestion.asr_whisperx import WhisperXAsr

        asr = WhisperXAsr()
    return IngestionPipeline(PyMuPdfExtractor(), AnthropicStructurer(), asr=asr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest a Cambridge paper as a draft.")
    parser.add_argument("--path", required=True, help="Question-paper PDF.")
    parser.add_argument("--key", required=True, help="Answer-key PDF.")
    parser.add_argument("--audio", default="", help="Listening mp3 (optional).")
    parser.add_argument("--test-id", default="ingest-draft")
    parser.add_argument(
        "--level", default="B1_PRELIMINARY", choices=["A2_KEY", "B1_PRELIMINARY"]
    )
    args = parser.parse_args(argv)

    configure_logging()
    audio_bytes = Path(args.audio).read_bytes() if args.audio else None

    pipeline = _build_pipeline(with_audio=audio_bytes is not None)
    request = IngestionRequest(
        test_id=args.test_id,
        level=args.level,  # argparse `choices` restricts this to a valid Level
        questions_pdf=Path(args.path).read_bytes(),
        answer_key_pdf=Path(args.key).read_bytes(),
        audio=audio_bytes,
    )
    result = pipeline.run(request)
    log.info(
        "ingest cli complete test=%s sections=%d items=%d status=%s (NOT persisted)",
        result.test.id,
        len(result.test.sections),
        sum(len(s.items) for s in result.test.sections),
        result.test.status,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
