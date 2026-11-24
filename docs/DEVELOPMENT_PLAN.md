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

---

## Phase 17 — IQ test family (book-faithful, norm-referenced)
Source: "The Cambridge Self-Scoring I.Q. Test" (1993, © Dan Pape; NOT Cambridge
University/Assessment; non-clinical, ages 14–75). **Licensed content never enters git:**
the book, its transcription, item JSON, crops and norm tables live under `/pdf/` and
`/private/` (gitignored). Only the generic engine code ships. English only for now.

- 17.1 **Contracts** — level `IQ`; new item types `drawing` (grader `region` for
  "draw what is missing", `lines` for "divide the square", tolerance + allowed
  rotations/reflections; targets authoring-only) and `mark_toggle` (mark a row, e.g.
  "pair NOT identical"); `Section.time_limit_seconds` (own server timer per subtest),
  `Section.study` (show-then-hide stimulus, `study_seconds`), untimed `intro`
  (instructions + sample problems). Generic `NormScoring` block on `Test`
  (authoring-only): composites over sections, raw->scaled tables per section,
  sum->score table per age group, final = mean of composites, brackets. Client twins
  carry no keys/targets/tables. `make schema` + Alembic migration.
- 17.2 **Delivery** — subtests run strictly in order; per-section start/deadline rows,
  server-authoritative and not pausable; answers to a closed section are rejected;
  study stimulus served only during its study window and never alongside recall items.
  Overall attempt deadline = sum of section limits (+ intro allowance).
- 17.3 **Grading** — `mark_toggle` (correct marks count; each false mark cancels one,
  floor 0 — anti "mark everything"); `drawing` pixel/stroke grader -> `correct` /
  `incorrect` + confidence; low confidence -> `needs_review`; teacher override via the
  existing `ManualGrade`. New pure `app/grading/norm_scoring.py`: per-section raw ->
  scaled -> composite sums -> IQ by age -> bracket. Grading ⊥ integrity untouched.
- 17.4 **Roster** — optional `birth_date` per student (teacher-only, never served to the
  student runner); age taken at attempt start; < 14 or missing -> "no norms" (raw and
  scaled still shown).
- 17.5 **Runner** — intro/sample screen -> "Begin" starts the subtest clock; per-subtest
  countdown, auto-advance on expiry; study screen that disappears after N s; drawing
  canvas (pen + undo + clear, touch + mouse) over the item picture; toggle rows.
- 17.6 **Teacher report** — IQ block on result card / printable report / CSV:
  subtest raw + scaled, composite I (odd) & II (even) with their IQ, overall IQ,
  bracket, "non-clinical, per book" note; drawings shown with override buttons.
- 17.7 **Content (private)** — `scripts/build_iq_book.py` turns
  `private/iq/.../source.json` + page crops into an import JSON + assets in `private/`;
  zone/line targets authored with a small helper; imported as draft, human-approved.
- **Gate:** contracts/schema/migration green; invariant test proves no key, target or
  norm table reaches the client; golden: synthetic attempt -> expected raw/scaled/IQ;
  drawing grader goldens (hit, miss, scribble, rotated); per-section expiry E2E.

## Phase 18 — ICAR (open cognitive-ability items)
- 18.1 Request/obtain ICAR item sets (matrix reasoning, letter-number series, verbal
  reasoning, 3D rotation) under their non-commercial terms; keep the items in
  `/private/` unless the licence allows redistribution.
- 18.2 Reuse Phase 17 machinery (level `IQ`, per-section timers, `NormScoring`) with
  percentile/standard-score norms instead of book IQ tables.
- 18.3 Russian version: pick a separately normed test (not a translation of the book)
  — see Phases 19–21.

## Shared rules for Phases 18–21 (each test is its own Phase-17-style import)
- Source material (booklet + key + norm tables) is supplied by the owner into `/pdf/`;
  transcription, crops and import JSON live in `/private/`. Nothing from a test
  ships in git. Only the generic code ships.
- Each test is a separate `Test` with its own norms. We never translate one test into
  another language: the norms only hold for the language they were made in.
- The import is always a draft and is human-approved before publish.
- Shared machinery to add once, at the first test that needs it:
  - **Russian/Ukrainian runner UI** for the IQ runner (intro, ready screen, timers).
  - **Generalized norms.** Norm groups keyed by age *or* school grade, and output as
    IQ, percentile or "% of norm". A single-scale path (raw total → score) alongside
    the book's subtest → composite → IQ path.
  - **Partial-credit open answers** (an accepted-answer list with 2/1/0 points).
  - **Validity checks** reported as report notes, never changing the score (for
    example, Raven's expected series profile).

## Phase 19 — Raven Progressive Matrices (language-free)
- Standard (SPM, 60 items, A–E, about 12+) and/or Coloured (CPM, 36 items, ages
  5–11) for the YLE kids. single_choice with image options already exists.
- Norms: raw → percentile/IQ by age, from the edition the owner supplies.
- Validity: series-profile discrepancy check → a report note.

## Phase 20 — Amthauer Intelligence Structure Test (TSI / ТСИ, Russian adaptation)
- 9 timed subtests (about 176 items), including a memory subtest (study → recall,
  reusing the T1 study window).
- Verbal subtests in Russian. "Generalization" uses partial-credit open answers.
- Norms: subtest raw → standard score by age, then the total → IQ (book-like).

## Phase 21 — ШТУР (School Test of Mental Development, ages 12–16 / grades 7–9)
- 6 timed verbal subtests in Russian.
- Norms by school grade, output as "% of the social-psychological norm" (no IQ).
- Exact edition and grading rules come from the supplied manual.
