# Development plan

Each phase is independently buildable and testable. Do them in order. Every phase ends
with a **test gate**: `make test` green + a manual cmd check where noted. Add logging in
every phase (see CLAUDE.md). One phase ≈ one Claude Code session (`docs/PROMPTS.md`).

---

## Phase 0 — Scaffold & tooling
- 0.1 Repo layout per CLAUDE.md; Python venv; `pyproject.toml`/`requirements.txt`.
- 0.2 `app/core/logging.py` (canonical console logger) + `app/core/config.py` (env via
  pydantic-settings) + `.env.example`.
- 0.3 FastAPI app in `apps/api` with `GET /health` that logs the request and returns ok.
- 0.4 `Makefile` (install/run/test/lint/migrate/seed/ingest); `ruff` + `mypy`; `pytest`.
- 0.5 Pre-commit hooks.
- **Gate:** `make run` boots, `GET /health` returns ok and the request is logged to cmd;
  `make lint` and `make test` (one trivial test) pass.

## Phase 1 — Contracts (schemas)
- 1.1 Pydantic models: `Test`, `Section` (+ stimulus union), `Item` (+ type union:
  single_choice / gap_fill / matching / open_writing), `Option` (text|image).
- 1.2 Runtime models: `Attempt`, `Answer`, `IntegrityEvent`, `GradingResult`,
  `IntegrityProfile`, `AnalysisVerdict`, `RosterEntry`.
- 1.3 JSON Schema generation (`contracts/jsonschema/*.json`) + a `make` step to regenerate.
- **Gate:** valid/invalid fixtures parse/raise as expected; JSON Schema regenerates;
  no `correct` field is reachable from any student-facing model.

## Phase 2 — Persistence & migrations
- 2.1 SQLAlchemy 2.0 models mirroring contracts; `app/core/db.py` session.
- 2.2 Alembic init + first migration.
- 2.3 Thin repository layer per aggregate (content, attempts, events).
- **Gate:** create/read round-trip tests against a test DB; `make migrate` applies clean;
  CRUD operations log at INFO.

## Phase 3 — Content engine
- 3.1 CRUD for tests/sections/items/assets (assets behind a storage interface;
  filesystem impl now, MinIO later).
- 3.2 Pooling: draw sections/passages per attempt from a bank (seeded RNG for
  reproducibility) + option-shuffle with stored permutation.
- 3.3 Roster + assignment (one link per test; roster entries).
- **Gate:** two seeded students get different section sets but valid tests; permutation
  round-trips (displayed index -> canonical); pooling decisions logged.

## Phase 4 — Delivery engine (exam runtime)
- 4.1 `start`: validate window + grace, roster pick, create-or-resume attempt
  (refresh-safe), deadline = min(start+duration, closes_at).
- 4.2 Serve items one-at-a-time, **stripped of `correct`**; save answer (map permutation).
- 4.3 Server-authoritative timer/state endpoint; hard deadline; `submit`.
- **Gate:** window/grace enforced; payload has no `correct` (assert in test); resume on
  refresh; late submit rejected; lifecycle logged (start/answer/submit/expire).

## Phase 5 — Grading engine
- 5.1 Deterministic: single_choice, matching (via pool), gap_fill (normalize + accepted
  + variants + rapidfuzz threshold).
- 5.2 `open_writing`: LLM grading by rubric behind an interface (mock in tests; real call
  logs model id + latency). Manual override path + review flag.
- 5.3 Score assembly; needs-review queue.
- **Gate:** golden grading cases incl. acceptable-misspellings; grading never reads
  integrity data; LLM grader mocked deterministically in tests.

## Phase 6 — Telemetry engine (recorder + ingest)
- 6.1 Client recorder (vanilla JS or rrweb): `visibility_hidden/visible` with duration,
  `window_blur`, `pagehide`, per-question interaction timeline, answer-change
  timestamps, audio play/seek events. Batch + `sendBeacon`.
- 6.2 Ingest endpoint (append-only) storing `client_ts` + `server_ts`.
- **Gate:** events persisted with both timestamps; Playwright E2E: backgrounding the tab
  produces a `visibility_hidden` event; ingest logs a WARNING per integrity event.

## Phase 7 — Integrity engine (deterministic features)
- 7.1 Feature extractor over the event stream: per-question latency, hidden intervals +
  durations, post-return pattern (`visible -> answer` latency + interaction count),
  pacing variance / coefficient of variation, systematicity (rate across questions).
- 7.2 Emit `IntegrityProfile` (pure function: same events -> same profile).
- **Gate:** synthetic event streams -> expected features (golden); extractor is
  deterministic and calls no LLM.

## Phase 8 — Analysis engine (LLM verdict)
- 8.1 Build `IntegrityProfile` (+ flagged raw segments) -> Instructor-structured LLM call
  -> `AnalysisVerdict { suspicion_score, confidence, flags[], summary }`.
- 8.2 Log model id + inputs + latency; store verdict with attempt. Advisory only.
- **Gate:** verdict validates against schema; deterministic feature layer carries weight;
  LLM mocked in tests; never mutates score.

## Phase 9 — Ingestion engine (PDF -> items)
- 9.1 PDF extract (PyMuPDF/Docling): text + layout; crop A/B/C images as assets.
- 9.2 Multimodal LLM (Instructor) -> draft sections/items validated against contracts.
- 9.3 Answer-key PDF parse + merge by question number -> authoritative `correct`
  (+ accepted variants for gap_fill).
- 9.4 ASR adapter (WhisperX) for mp3 -> transcript + word timestamps + speakers;
  align each listening item to an `audio_span`.
- 9.5 Validation + review queue (`draft -> published`); run as an `arq` job.
- **Gate:** golden sample PDF -> expected section/item counts & types; key merge fills
  `correct`; malformed extraction is rejected; **never auto-publishes**; each pipeline
  step logs progress to cmd (`make ingest path=...` shows the steps).

## Phase 10 — Admin (teacher) API + dashboard
- 10.1 Auth (fastapi-users or signed token); roster management; bank management.
- 10.2 Review queue UI (approve/edit ingested drafts before publish).
- 10.3 Results view: score + integrity verdict + raw replay, ranked suspicious-first;
  live roster status.
- **Gate:** auth enforced; approve flow flips draft->published; results endpoint returns
  score + verdict + event count; actions logged.

## Phase 11 — Student runner frontend
- 11.1 Roster-pick entry; one-at-a-time runner; resume on refresh.
- 11.2 Integrity layer: fullscreen where supported (skip iOS), visibility/blur/pagehide
  logging, copy/paste/contextmenu block, countdown with auto-submit on deadline.
- 11.3 Audio player for listening with replay limit (`plays: 2`) per section.
- **Gate:** Playwright E2E full attempt; integrity events fire; timer auto-submits;
  iOS path degrades gracefully (no fullscreen, still logs visibility).

## Phase 12 — Wire-up, seed, E2E, dockerize
- 12.1 `docker-compose`: api, postgres, redis, minio, arq worker.
- 12.2 Seed a demo test by ingesting a sample PDF (human-approved fixture).
- 12.3 Full E2E: honest happy path + a scripted "cheat" path that yields a verdict.
- **Gate:** `docker compose up` -> full flow works; logs stream to cmd from every service.

---

### Suggested cut for a first usable MVP
Phases 0–5 + 11 (minimal runner) + 10 (minimal results) = a working, auto-graded,
per-student randomized test with a teacher view. Telemetry/integrity/analysis (6–8) and
full ingestion (9) layer on after the core is solid.
