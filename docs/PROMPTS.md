# Claude Code prompts (run in order)

How to use: open the repo in VS Code with Claude Code. Paste one prompt per session.
Every prompt assumes `CLAUDE.md`, `docs/ARCHITECTURE.md`, and `docs/DEVELOPMENT_PLAN.md`
are present and authoritative. Do not move to the next prompt until the test gate passes.

General rule baked into each step: **implement -> add console logging -> write tests ->
`make test` until green -> stop. Do not scaffold future engines.**

---

### Prompt S — Skills setup (run once, before Prompt 0)
```
Before we start building, set up Agent Skills for this project. Use only Anthropic's
official skills (do NOT install community skills — they execute code in this environment).

1. The official skills `pdf`, `claude-api`, `skill-creator`, and the example
   web-app testing skill are typically already available on paid Claude Code plans.
   Check which are available to you in this session. If any are missing, register
   Anthropic's marketplace and install them:
     /plugin marketplace add anthropics/skills
     /plugin install document-skills@anthropic-agent-skills
     /plugin install example-skills@anthropic-agent-skills
2. Confirm back to me which of these are now available: pdf, claude-api,
   skill-creator, web-app testing.
3. From now on, when a phase is relevant, USE the matching skill automatically:
   - PDF reading/extraction (Phase 9 ingestion)        -> `pdf`
   - any Claude API / structured-output / tool-use work
     (Phases 5 grading-LLM, 8 analysis, 9 ingestion)   -> `claude-api`
   - browser E2E tests (Phases 6, 11)                   -> web-app testing skill
   - authoring a new project skill                      -> `skill-creator`
4. Do not write any application code in this step. Only set up and report skills.
```
After the core is stable, author project-specific skills with `skill-creator` (highest
value first): an `ingestion` skill (the PDF->items procedure + invariants), a
`schema-change` skill (regenerate JSON Schema + Alembic migration together), and an
`item-authoring` skill. These live in `.claude/skills/` and load on demand.

---

### Prompt 0 — Scaffold & tooling
```
Read CLAUDE.md and docs/DEVELOPMENT_PLAN.md (Phase 0). Implement Phase 0 only.
Create the repo layout, a Python venv config (pyproject + requirements), the canonical
app/core/logging.py and app/core/config.py exactly as specified in CLAUDE.md, and a
FastAPI app in apps/api with GET /health that logs the incoming request via the shared
logger and returns {"status":"ok"}. Add a Makefile with install/run/test/lint/migrate/
seed/ingest targets, wire ruff + mypy + pytest + pre-commit. Write one trivial test for
/health.
Gate: `make run` boots and a request to /health prints a log line to stdout; `make lint`
and `make test` pass. Show me the log output. Stop after Phase 0.
```

### Prompt 1 — Contracts
```
Implement Phase 1 only (contracts/). Create the Pydantic models from docs/ARCHITECTURE.md:
Test, Section (stimulus union: passage_text|audio_asset|image_set|gapped_text|
matching_pool), Item (type union: single_choice|gap_fill|matching|open_writing), Option
(text|image), and runtime models Attempt, Answer, IntegrityEvent, GradingResult,
IntegrityProfile, AnalysisVerdict, RosterEntry. Add JSON Schema generation into
contracts/jsonschema/ and a Makefile target to regenerate.
Invariant: there must be NO `correct` field on any student-facing model — put correct
answers only on internal/authoring models. Add a test asserting this.
Gate: valid/invalid fixtures parse/raise correctly; JSON Schema regenerates; `make test`
green. Stop after Phase 1.
```

### Prompt 2 — Persistence & migrations
```
Implement Phase 2 only. Add SQLAlchemy 2.0 models mirroring contracts/, app/core/db.py
session, Alembic init + first migration, and a thin repository layer for content,
attempts, and events. CRUD must log at INFO via the shared logger.
Invariant (CLAUDE.md #4): models must match contracts; if anything diverges, fix the
contract or the model so they agree.
Gate: create/read round-trip tests pass against a test DB; `make migrate` applies clean;
`make test` green. Stop after Phase 2.
```

### Prompt 3 — Content engine
```
Implement Phase 3 only (app/content). CRUD for tests/sections/items/assets (assets behind
a StorageBackend interface with a filesystem implementation). Pooling: draw sections/
passages per attempt from a bank using a seeded RNG, plus option-shuffle storing the
per-attempt permutation. Roster + assignment (one link per test).
Log every pooling/assignment decision. Respect invariant #7 (pool at section level, never
shuffle items within a passage).
Gate: tests show two seeded students get different valid section sets; permutation
round-trips displayed-index -> canonical; `make test` green. Stop after Phase 3.
```

### Prompt 4 — Delivery engine
```
Implement Phase 4 only (app/delivery). Endpoints: start (validate window+grace, roster
pick, create-or-resume attempt that is refresh-safe, deadline=min(start+duration,
closes_at)); serve items one-at-a-time STRIPPED of `correct`; save answer (map the stored
permutation back to canonical); server-authoritative state/timer; submit.
Invariants: #1 (no correct to client), #3 (timer never pauses). Log the full attempt
lifecycle.
Gate: tests assert the served payload has no `correct`; window/grace enforced; refresh
resumes the same attempt; late submit rejected. `make test` green. Stop after Phase 4.
```

### Prompt 5 — Grading engine
```
Implement Phase 5 only (app/grading). Deterministic grading for single_choice, matching
(via section pool), gap_fill (normalize + accepted[] + accepted_variants[] + rapidfuzz
threshold). open_writing graded by an LLMGrader interface (rubric-based); mock it
deterministically in tests, and in the real impl log model id + latency. Add manual
override + needs-review queue.
Invariant #2: grading must not read or touch integrity data.
Gate: golden grading cases including acceptable-misspellings pass; LLM grader is mocked
in tests; `make test` green. Stop after Phase 5.
Skills: use `claude-api` when implementing the real LLMGrader.
```

### Prompt 6 — Telemetry recorder + ingest
```
Implement Phase 6 only (app/telemetry + recorder in apps/web). Client recorder capturing:
visibility_hidden/visible (with duration), window_blur, pagehide, per-question
interaction timeline, answer-change timestamps, audio play/seek. Batch and send via
fetch/sendBeacon. Append-only ingest endpoint storing client_ts + server_ts.
Log a WARNING per integrity event received.
Gate: events persist with both timestamps; a Playwright E2E test that backgrounds the tab
produces a visibility_hidden event; `make test` green. Stop after Phase 6.
Skills: use the web-app testing skill for the Playwright E2E.
```

### Prompt 7 — Integrity feature extractor
```
Implement Phase 7 only (app/integrity). Pure deterministic extractor over an attempt's
event stream producing IntegrityProfile: per-question latency, hidden intervals +
durations, post-return pattern (visible->answer latency + interaction count), pacing
variance / coefficient of variation, systematicity rate across questions.
Invariant: deterministic (same events -> same profile), no LLM calls.
Gate: synthetic event-stream goldens map to expected feature values; `make test` green.
Stop after Phase 7.
```

### Prompt 8 — Analysis (LLM verdict)
```
Implement Phase 8 only (app/analysis). Take an IntegrityProfile (+ flagged raw segments)
and produce AnalysisVerdict {suspicion_score, confidence, flags[], summary} via an
Instructor-structured LLM call behind an interface. Log model id + inputs + latency; store
the verdict with the attempt.
Invariants: advisory only; must never mutate the score (#2). Mock the LLM in tests.
Gate: verdict validates against schema; tests confirm the score is untouched; `make test`
green. Stop after Phase 8.
Skills: use `claude-api` for the Instructor-structured verdict call.
```

### Prompt 9 — Ingestion (PDF -> items)
```
Implement Phase 9 only (app/ingestion). Pipeline: (a) extract text+layout from the
question-paper PDF with PyMuPDF/Docling and crop A/B/C images as assets; (b) multimodal
LLM via Instructor -> draft sections/items validated against contracts; (c) parse the
answer-key PDF and merge by question number to set authoritative `correct` (+ gap_fill
variants); (d) WhisperX ASR adapter for mp3 -> transcript + word timestamps + speakers,
align each listening item to an audio_span; (e) validate + push to a review queue
(draft). Run as an arq job. Each step logs progress to cmd.
Invariant #5: NEVER auto-publish; answer key is authoritative for `correct`.
Gate: a golden sample PDF yields expected section/item counts and types; key merge fills
correct; malformed extraction is rejected; `make ingest path=...` prints each step to
cmd; `make test` green. Stop after Phase 9.
Skills: use `pdf` for PDF extraction and `claude-api` for the LLM extraction/structuring.
```

### Prompt 10 — Admin API + dashboard
```
Implement Phase 10 only (app/admin + teacher pages in apps/web). Auth (fastapi-users or
signed token), roster management, bank management, a review queue UI to approve/edit
ingested drafts (flips draft->published), and a results view showing score + integrity
verdict + raw replay, ranked suspicious-first, plus live roster status. Log admin actions.
Invariant #5: publish only via explicit human approval.
Gate: auth enforced; approve flow publishes; results endpoint returns score + verdict +
event count; `make test` green. Stop after Phase 10.
```

### Prompt 11 — Student runner frontend
```
Implement Phase 11 only (apps/web student runner). Roster-pick entry; one-at-a-time
runner; resume on refresh (token in URL/localStorage). Integrity layer: request
fullscreen where supported and skip on iOS, log visibility/blur/pagehide, block copy/cut/
paste/contextmenu, countdown timer that auto-submits on deadline. Audio player for
listening sections with a replay limit (plays:2). Wire telemetry events to Phase 6 ingest.
Invariants #1/#3 on the client too (never request/expect correct; timer is display-only,
server decides).
Gate: Playwright E2E completes a full attempt; integrity events fire; timer auto-submits;
the iOS path degrades gracefully. `make test` green. Stop after Phase 11.
Skills: use the web-app testing skill for the Playwright E2E.
```

### Prompt 12 — Wire-up, seed, E2E, dockerize
```
Implement Phase 12 only. docker-compose with api, postgres, redis, minio, and an arq
worker. Seed a demo test from a human-approved ingested sample. Add a full E2E test: an
honest happy path and a scripted "cheat" path (backgrounding + instant answers) that
produces a non-trivial suspicion verdict. Ensure every service streams logs to cmd.
Gate: `docker compose up` runs the full flow; the cheat path yields a higher
suspicion_score than the honest path; `make test` green. Stop after Phase 12.
```
