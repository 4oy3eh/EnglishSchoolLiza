# CLAUDE.md — English Test Platform

## What this is
Online English testing for a language school (Cambridge **A2 Key** / **B1 Preliminary**
format). Students take tests in the browser on their own devices via **one shareable
link per test**. The platform serves randomized per-student tests, auto-grades,
records behavioral telemetry, and produces an **advisory** cheating-likelihood verdict
for the teacher. Content is created by ingesting Cambridge-style PDFs (+ mp3) into a
structured item bank.

## Golden rules (invariants — NEVER violate)
1. **Correct answers never reach the client.** Grading is server-side only. The
   student-facing payload must never contain a `correct` field.
2. **Grading ⊥ Integrity.** "Is the answer right" and "did they cheat" are separate
   axes. A cheating signal must never automatically change a score.
3. **Timer is server-authoritative and does NOT pause** when the page is hidden.
   Leaving the tab burns the student's time.
4. **`contracts/` is the single source of truth.** Changing a schema => regenerate the
   JSON Schema AND write an Alembic migration in the same change. Never let a DB model
   and its contract drift.
5. **Ingested content is always human-approved before publish** (`draft -> published`).
   LLM extraction is a draft; the answer-key PDF is authoritative for `correct`.
6. **Three integrity layers stay separate:** `telemetry` (capture only, no judgment)
   -> `integrity` (deterministic, reproducible features) -> `analysis` (LLM judgment,
   advisory only). The teacher always sees the raw replay next to the verdict.
7. **Randomization is at the section/passage-pool level**, plus option-shuffle for
   single_choice. Never shuffle item order *within* a passage/recording — it breaks
   coherence.
8. **Every LLM call logs** model id + token usage + latency (auditability).

## Logging (must be visible in the terminal/cmd)
- Use the shared logger from `app/core/logging.py`. **Never use bare `print()`.**
- A console handler prints clean, structured lines to stdout so the developer sees
  them live in cmd while `make run` is active.
- Log at boundaries: HTTP request in/out, pooling/assignment decisions, attempt
  lifecycle (start / answer / submit / expire), grading results, telemetry batch
  received, every ingestion step, every LLM call.
- Levels: `INFO` lifecycle, `DEBUG` detail, `WARNING` integrity events & validation
  failures, `ERROR` failures.

Canonical setup (created in Phase 0 — keep identical everywhere):
```python
# app/core/logging.py
import logging, sys

def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-20s %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
```
Each module: `log = get_logger(__name__)`. (Optional nicer cmd output: swap the
formatter for `rich.logging.RichHandler` or `loguru` — same call sites.)

## Commands (use these, do not invent)
`make install` · `make run` · `make test` · `make lint` · `make migrate` · `make seed`
· `make ingest path=...`  — see `Makefile`.

## Layout
```
contracts/        # Pydantic models + generated JSON Schema (SOURCE OF TRUTH)
app/
  core/           # logging, config, db session, settings
  content/        # item bank: tests, sections, items, assets, pooling, assignment, roster
  ingestion/      # PDF -> items: extract, image crops, ASR adapter, key merge, review
  delivery/       # exam runtime: window, server timer, serve-one-at-a-time, submit
  grading/        # deterministic (mcq/matching/gap_fill) + writing (LLM) + variants
  telemetry/      # event recorder ingest (append-only)
  integrity/      # deterministic feature extractor over the event stream
  analysis/       # LLM verdict (advisory) over the integrity profile
  admin/          # teacher API: roster, bank, review queue, results
apps/
  api/            # FastAPI app wiring the engines together
  web/            # frontends: student runner, teacher dashboard
tests/
  golden/         # sample PDF -> expected items; grading goldens; synthetic event streams
migrations/       # Alembic
.claude/commands/ # slash commands (/ingest-pdf, /new-engine)
docs/             # ARCHITECTURE.md, DEVELOPMENT_PLAN.md, PROMPTS.md
```

## Per-engine contracts
Each engine dir has its own `CLAUDE.md` (inputs, outputs, what it must NOT do, where
its tests live). Read the engine's `CLAUDE.md` before editing it.

## Phase-end ritual (after each phase in DEVELOPMENT_PLAN)
1. `verifier` subagent green — the phase gate passes.
2. `reviewer` subagent — address every "must fix".
3. Update `CHANGELOG.md` (Keep a Changelog style).
4. `/commit-phase` — commit + push (conventional message).
5. `/handoff` then `/clear` before the next phase, to keep context lean.
A PostToolUse hook (`.claude/hooks/post_edit_lint.py`) runs ruff on each edited `.py`
and nudges about engine CLAUDE.md / CHANGELOG automatically.

## Commit format
Conventional commits tied to the phase: `type(phase-N): summary`
(`feat` | `fix` | `test` | `docs` | `chore`). Never commit with a red gate.

## How to work (for the agent)
- **One step at a time** (see `docs/DEVELOPMENT_PLAN.md` + `docs/PROMPTS.md`):
  implement -> add logging -> write tests -> `make test` -> stop when green. Do not
  jump ahead or scaffold future engines.
- **Respect engine boundaries.** Edit one engine + its tests per step. Cross-engine
  changes go through `contracts/`.
- Prefer the OSS stack already chosen: FastAPI, SQLAlchemy 2.0, Alembic, Postgres,
  Redis, arq (async jobs), MinIO (assets), PyMuPDF/Docling (PDF), WhisperX (ASR),
  Instructor (structured LLM output), rapidfuzz (fuzzy match), fastapi-users (auth),
  Playwright (E2E).