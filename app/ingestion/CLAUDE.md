# CLAUDE.md — ingestion engine

## Responsibility
Turn a Cambridge-style **question-paper PDF + answer-key PDF (+ listening mp3)** into a
**draft** `contracts.Test` queued for human review. This is the authoring *intake*
side of content: it proposes structured items; it never decides if an answer is right
(grading) and never publishes (admin).

## Pipeline (each step logs to cmd)
1. **extract** (`extract.py` / real `extract_pymupdf.py`) — PDF -> page text + image
   crops (A/B/C options, listening signs) as assets tagged by `asset_id`.
2. **structure** (`structure.py` / real `llm_anthropic.py`) — multimodal LLM -> a
   `DraftTest` (sections/items/options) with **no answer key**.
3. **key-merge** (`answer_key.py`) — parse the answer-key PDF, merge by question number
   -> authoritative `correct` / `accepted` (+ gap_fill variants), producing a validated
   authoring `Test`.
4. **asr** (`asr.py` / real `asr_whisperx.py`) — mp3 -> transcript + word timestamps +
   speakers; align each listening item to an `audio_span`.
5. **queue** (`service.py`) — store crops as assets + persist the `Test` as **draft**.

## Inputs / Outputs
- **In:** `IngestionRequest` (test_id, level, question PDF bytes, answer-key PDF bytes,
  optional mp3).
- **Out:** `IngestionResult` (validated draft `Test` with `status="draft"`, image
  `crops`, `audio_spans`, optional `transcript`). `IngestionService.ingest` also stores
  the crops and persists the draft via `ContentService`.

## Must NOT
- **Auto-publish (golden rule #5).** Always `status="draft"`; `draft -> published` is a
  human-approved Admin/Phase-10 action. `merge_key` and the pipeline/service assert draft.
- **Let the LLM invent the answer key (golden rule #5).** Draft models carry NO
  `correct`/`accepted`; only `merge_key` sets them, from the answer-key PDF.
- **Reorder items within a section/passage/recording (golden rule #7).** The structurer
  preserves Cambridge order; key-merge keeps item order.
- **Hard-depend on heavy backends.** PyMuPDF / `anthropic` / WhisperX / `arq` are lazy
  imports in their own modules and are NOT imported from `__init__.py`. Tests inject
  `MockPdfExtractor` / `MockLLMStructurer` / `MockAsr`.

## Key invariants
- **Answer key is authoritative.** A keyed item (single_choice/matching/gap_fill) with
  no key entry, or a `correct` not among the item's options, raises `ValueError` — a
  malformed extraction is **rejected**, not published with a bad key.
- **Pure, deterministic core.** `parse_answer_key`, `merge_key`, `align_items_to_spans`
  and the `IngestionPipeline` (with mock seams) are pure: same inputs -> same draft.
- **`audio_span` is ingestion-internal**, not a `contracts/` field. Persisting it on an
  item would be a schema change (regen JSON Schema + Alembic migration, golden rule #4)
  the Phase-9 gate doesn't need; it rides in `IngestionResult` and lands with
  Admin/Phase-10 (same deferred-persistence pattern as Phases 5/7/8).
- **Every LLM call logs** model id + token usage + latency (golden rule #8).

## Layout
- `models.py` — `DraftTest`/`DraftSection`/`Draft*Item` (mirror authoring items, no key).
- `extract.py` (+ `extract_pymupdf.py`) — `PdfExtractor` seam + `MockPdfExtractor`.
- `structure.py` (+ `llm_anthropic.py`) — `LLMStructurer` seam + `MockLLMStructurer`.
- `answer_key.py` — `parse_answer_key` + `merge_key` (+ `KeyEntry`).
- `asr.py` (+ `asr_whisperx.py`) — `Asr` seam, `MockAsr`, `AudioSpan`, alignment.
- `pipeline.py` — `IngestionPipeline.run(request)` (pure orchestrator).
- `service.py` — `IngestionService.ingest` (store crops + queue draft via content).
- `jobs.py` — `ingest_pdf` arq task (arq-free at import time).
- `cli.py` — `python -m app.ingestion.cli` for `make ingest` (dry run, prints steps).

## Tests
`tests/test_ingestion.py` — golden extract->structure->merge yields expected section/
item counts & types; `parse_answer_key`/`merge_key` fill `correct`/`accepted`/variants;
missing or out-of-range key is rejected; the result is always `draft` (never published);
ASR alignment is deterministic & in-range; crops are stored as assets; an AST import
guard asserts `__init__.py` pulls no heavy backend.
