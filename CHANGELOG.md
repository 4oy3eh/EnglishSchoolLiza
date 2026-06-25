# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **Manual grading (teacher marks, persisted)** — the attempt detail now has a
  **Writing — read & grade** section (each `open_writing` as its own card: task +
  content points + the student's full text + a 0–5 mark selector) and a **✓/✗ override**
  on typed `gap_fill` rows (auto fuzzy-match can miss an acceptable spelling). Marks
  persist in a new `manual_grades` table (Alembic `a1b2c3d4e5f6`) and are overlaid onto
  the auto score via the pure `GradingService.apply_override` — grading stays auto-only
  per its boundary, and a cheating signal still never moves a score (golden rule #2). New
  `ManualGrade` contract + `ManualGradeRow`; `PUT /admin/results/{attempt}/grades/{item}`;
  reset/remove wipe an attempt's marks. Writing shows its 0–5 score (not pass/✗); objective
  items show ✓/✗ from points. New `ItemResponse` fields `bullet_points` / `word_min` / `manual`.
- **Human-readable activity timeline** (`app/admin`, `apps/web/teacher.*`): the attempt
  detail now shows a decoded, chronological "what they did" list next to the raw event
  JSON — each event as a plain sentence with the question it touched ('Q1: answered
  "C — …"', 'Left the exam tab', 'Returned (away 9s)', 'blocked paste'), `single_choice`
  indices de-shuffled to the option the student saw. Tab-leave/focus/device events are
  highlighted. The raw `events` stay as the machine/script view (now collapsed under a
  `<details>`). New `TimelineEntry` DTO; review lookups refactored into a shared
  `_ReviewContext`. Capture only — it judges nothing (golden rule #6).
- **Device / session forensics** (telemetry → integrity → dashboard, no migration).
  The recorder captures a `device_info` fingerprint at each (re)start — user-agent,
  platform, screen, viewport, time zone, language, CPU/memory, touch — and the
  telemetry ingest **server-stamps the client IP** onto it (server-observed, never
  trusted from the body, like `server_ts`). The integrity extractor adds deterministic
  `device_count` / `device_changed` to `IntegrityProfile` (a resume on a different
  machine flags a switch — capture→feature, judges nothing, golden rule #6). The
  dashboard shows the device/IP captures and a "device changed" warning. New
  `EventType` `device_info` + `DeviceCapture` DTO; the student start screen now carries
  a monitoring disclosure. Schema regenerated; no Alembic migration (event `type` is a
  free string column, the profile/DTOs aren't persisted).
- **Student controls** (`app/admin`, `apps/web/teacher.*`): **reset attempt** (wipe an
  attempt + its answers/events and return the entry to `not_started` for a genuine
  retake), **remove student** (delete a roster entry + its attempt), and **live
  monitoring** (auto-refresh the roster/results every 5 s). New repository deletes
  (`delete_attempt` / `delete_roster_entry`, explicit dependents — SQLite has no FK
  cascade) + admin endpoints `POST /admin/roster/{id}/reset`, `DELETE /admin/roster/{id}`.
- **Teacher dashboard — per-question review & test vetting** (`app/admin`, `apps/web/teacher.*`).
  - **Attempt breakdown**: the attempt detail now lists every question with the student's
    answer vs the answer key, ✓/✗, points, the **full writing text** for `open_writing`, and
    the **change history** ("tried: B → C") decoded from the `answer_change` replay
    (`single_choice` displayed indices are de-shuffled back to canonical via the attempt
    seed, mirroring `delivery._default_bank`). New read-side DTOs `ItemResponse` /
    `ResponseChange` on `AttemptResult` (composed from contracts; nothing persisted, no
    migration — golden rule #4). Teacher surface, so it carries `correct` (rule #1 forbids
    that only on the student runner); no integrity signal touches a score (rule #2).
  - **View test**: an Item-bank "View test" action renders the full authored test
    (stimuli, audio players, options, answer key) so a teacher can vet composition before
    approving — the human gate (rule #5) is no longer blind to content.

### Changed
- **Listening UX rework (student runner)** — the runner now renders one **section
  (= part)** at a time with all its questions, grouped into **Reading / Writing /
  Listening** blocks (skill pill + part label) so students always know which paper
  they're in. A **question navigator** shows answered (filled) vs unanswered (empty)
  squares per part and jumps on click. Listening is now **one continuous recording**:
  the `<audio>` lives at the block level (`#audioBar`, built once), so moving between
  parts never stops or restarts it; the main track is **locked** (single play, no
  pause/seek/restart) and an optional **sound-check** (`preview_asset_id`) is freely
  replayable before the test track starts. (`apps/web/exam.html`, `apps/web/exam.js`.)
- **Contract — `AudioAssetStimulus`** (`contracts/content.py`): default `plays` is now
  **1** (Cambridge recordings bake in the double play); added optional
  `preview_asset_id` (replayable opening explanation / sound-check) and `locked`
  (main track can't be paused/seeked/restarted once started). Additive, inside the
  `stimulus` JSON column → JSON Schema regenerated, **no Alembic migration** needed.
  Stimulus is shared by the authoring and client families, so the keyless projection
  is unchanged (golden rule #1).

### Added
- **Phase 12 — Wire-up, seed, E2E, dockerize** (the final phase).
  - **Asset-serving route** (`apps/api/assets.py`) — `GET /assets/{asset_id}` streams
    stimulus blobs (listening mp3 / option & sign images) through the shared
    `StorageBackend` (`FilesystemStorage` now, MinIO later), closing the Phase-11 known
    gap so the runner's `<img>`/`<audio>` resolve. Unauthenticated like the rest of
    delivery — assets are stimulus content, never the answer key (golden rule #1) — with
    extension-then-magic-byte content typing and 404 for missing/traversal-unsafe ids.
    Tests (`tests/test_assets_api.py`, 4).
  - **Demo seed** (`app/content/seed.py`, `make seed`) — loads one **human-approved**
    A2 Key demo test (the shape ingestion emits: all four item types + a listening
    section) with self-contained asset bytes (generated WAV + PNG) written through the
    storage backend, then **explicitly publishes** it (the golden-rule-#5 approval step)
    and builds a roster. Idempotent. Tests (`tests/test_seed.py`, 4).
  - **Dockerization** (`Dockerfile`, `docker-compose.yml`, `.dockerignore`) — full local
    stack: **api + postgres + redis + minio + arq worker**, all unbuffered so every
    service streams clean logs to `docker compose` (CLAUDE.md logging rules). The api
    boots with `alembic upgrade head` → seed → uvicorn, so the runner has content
    immediately. `app/ingestion/worker.py` is the arq `WorkerSettings` entry point (the
    only module importing `arq`; the job stays arq-free) and starts healthy even without
    `ANTHROPIC_API_KEY`/ASR backends, logging that ingestion jobs need them.
  - **Full honest-vs-cheat E2E** (`tests/test_e2e_full.py`) — boots the seeded API and
    drives two real attempts through real browser telemetry (recorder.js → ingest →
    integrity → analysis): an honest path (human pauses, never leaves the tab) and a
    cheat path (background the tab, return, answer instantly per question). The gate
    holds: the cheat attempt's `suspicion_score` is higher than the honest one's and
    non-trivial (systematicity + `fast_post_return` flags), honest stays near zero, and
    the advisory verdict never carries a score (golden rule #2). Skips cleanly without
    Chromium.
  - **Wiring/config**: `apps/api/main.py` includes the assets router; `Settings` gains
    `redis_url` + `minio_*`; `requirements.txt` adds `redis`/`minio`; `arq`/`minio` are
    mypy-ignored (they live only in the worker image).
- **Phase 11 — Student runner frontend + delivery HTTP surface** (`apps/api/delivery.py`,
  `apps/web/exam.*`).
  - **Delivery router** (`apps/api/delivery.py`) — the exam runtime's browser-facing API,
    wiring the existing `DeliveryService` (no schema/engine change). Unauthenticated
    behind the per-test share link, students are identified by `roster_entry_id`:
    `GET /exam/tests/{id}/roster` (pick-your-name), `POST /exam/roster/{id}/start`
    (create-or-resume), `GET /exam/attempts/{id}/test` + `/items/{item_id}` (keyless
    `Client*` projections — golden rule #1), `GET /exam/attempts/{id}/state` (the
    server-authoritative timer — golden rule #3), `PUT .../answers/{item_id}`
    (displayed → canonical), and `POST .../submit`. `DeliveryError` subclasses map to
    distinguishable HTTP codes (404 / 403 window / 409 expired-or-finalized).
  - **Exam window is permissive this phase** (`_ALWAYS_OPEN`): there is no scheduled
    window on the authoring `Test`, so the per-attempt deadline is driven purely by
    `duration_minutes`. Scheduled windows would live on the `Test` contract later.
  - **Student runner** (`apps/web/exam.html` + `exam.js`): same-origin SPA —
    pick-your-name landing, **refresh-safe resume** (active attempt kept in
    `localStorage`, re-hydrated from `/state`), **one-at-a-time** rendering of all four
    item types, a **display-only countdown** reconciled against `/state` every 15 s that
    **auto-submits at zero** (the server has the last word on expiry — golden rule #3),
    and a listening **audio player with a per-stimulus replay limit** (`plays`, default 2).
  - **Client-side integrity** (capture, never judge — golden rule #6): requests element
    fullscreen on the first gesture where supported (**skipped on iOS**, degrades
    gracefully), blocks copy/cut/paste/contextmenu, and wires the Phase-6 `recorder.js`
    so visibility/blur/answer-change/audio events reach the append-only ingest sink.
    The runner only ever consumes keyless payloads (golden rule #1).
  - **Wiring**: `apps/api/main.py` now includes the delivery router alongside telemetry
    (Phase 6) and admin (Phase 10).
  - Tests (`tests/test_delivery_api.py`, 5): roster lists names with no answer key; the
    full start → serve-keyless → save (displayed→canonical) → submit flow; refresh
    resumes the same attempt; a crossed deadline expires the attempt server-side and a
    late submit is rejected (409); unknown attempt → 404.
- **Phase 10 — Admin (teacher) API + dashboard** (`app/admin/`, `apps/api/admin.py`,
  `apps/web/teacher.*`).
  - `AdminService` — the teacher-facing **composition root**: bank management
    (list/get/delete tests), the **review-queue publish gate**, roster management +
    live status, and a results view. The one engine allowed to hold grading *and*
    integrity/analysis at once, because the dashboard shows them side by side — it
    only *bundles* them and never feeds one into the other (golden rule #2).
  - **Review queue → approve** is the single `draft → published` path (golden
    rule #5). `approve(test_id)` refuses anything not currently a draft, so the
    human-approval gate can't be a silent no-op; `unpublish` reverts published→draft
    with the same status guard. This is where Phase 9's ingested drafts get consumed.
  - **Results** — `attempt_result(attempt_id)` returns the deterministic score, the
    advisory `AnalysisVerdict`, the integrity `IntegrityProfile`, the event count, and
    the **raw event replay** so the verdict is auditable next to the evidence (rule #6).
    `results_for_test(test_id)` ranks attempts **most-suspicious-first** — reordering
    rows only, never touching a score (rule #2).
  - **Auth** (`auth.py`): a single shared teacher password mints a short-lived
    **HMAC-signed bearer token** (`TokenSigner`, constant-time verify + expiry); no user
    table / migration. Every admin route requires it via `require_teacher`;
    `fastapi-users` is the documented multi-teacher upgrade path.
  - **Read-side DTOs** (`models.py`: `ReviewDraft`/`RosterStatus`/`AttemptOverview`/
    `AttemptResult`) compose existing contracts and persist nothing — no schema change
    (mirrors `app/ingestion/models.py`; golden rule #4). Engine contract in
    `app/admin/CLAUDE.md`.
  - **Teacher dashboard** (`apps/web/teacher.html` + `teacher.js`): same-origin SPA —
    sign in, approve drafts, manage roster, and browse results ranked suspicious-first
    with a raw-replay detail view next to the advisory verdict.
  - Every admin action logs at INFO (approve/delete/add_student/results reads, login);
    auth failures at WARNING.
  - New settings: `teacher_password`, `admin_token_secret`, `admin_token_ttl_seconds`,
    `assets_dir` (wires `FilesystemStorage` into the admin service).
  - Tests (`tests/test_admin.py`): auth enforced (401 without/with bad/expired token;
    login→token works); approve flips draft→published and is the only publish path;
    `unpublish` guarded; results endpoint returns score + verdict + event count;
    suspicious-first ranking with score independent of suspicion (rule #2); admin
    actions logged.
- **Phase 9 — Ingestion engine** (`app/ingestion/`).
  - `IngestionPipeline.run(request)` — a **pure orchestrator** over injected seams that
    turns a question-paper PDF + answer-key PDF (+ optional listening mp3) into a
    validated **draft** `contracts.Test`: (a) extract text + image crops, (b) structure
    into a `DraftTest` via the LLM, (c) parse the answer key and merge by question
    number to set authoritative `correct`/`accepted`, (d) ASR + align listening items to
    audio spans, (e) validate. Each step logs progress to cmd.
  - `IngestionService.ingest(request)` — runs the pipeline, stores the image crops as
    assets via the content `StorageBackend`, and persists the `Test` as a **draft**
    through `ContentService` (the review queue). It **never publishes** —
    `draft → published` is a human-approved Admin/Phase-10 action (golden rule #5).
  - **Draft models** (`models.py`): `DraftTest`/`DraftSection`/`Draft*Item` mirror the
    authoring items but **carry no answer key** — the LLM is structurally incapable of
    inventing `correct`. `answer_key.parse_answer_key` + `merge_key` are the only path
    that sets it, from the answer-key PDF (authoritative, golden rule #5). Both are pure;
    a keyed item with no key entry or a `correct` not among its options raises
    `ValueError` — a malformed extraction is rejected rather than published.
  - **Seams + mocks** (Protocol + Mock + lazy real impl, mirroring grading/analysis):
    `PdfExtractor`/`MockPdfExtractor` (real `PyMuPdfExtractor`, lazy `fitz`),
    `LLMStructurer`/`MockLLMStructurer` (real multimodal `AnthropicStructurer`, lazy
    `anthropic`, `messages.parse` structured output, logs id+tokens+latency, golden
    rule #8), `Asr`/`MockAsr` (real `WhisperXAsr`, lazy `whisperx`). None are re-exported
    from `__init__.py`, so importing the package pulls no optional/heavy backend (an AST
    import guard asserts this).
  - `align_items_to_spans` — pure, deterministic mapping of each listening item to an
    `AudioSpan`. The span is **ingestion-internal metadata** (carried in `IngestionResult`,
    not a `contracts/` field) so no schema change/migration is needed this phase
    (deferred to Admin/Phase-10, same pattern as Phases 5/7/8).
  - `jobs.ingest_pdf` — an `arq` task entry point (arq-free at import time); `cli.py`
    (`python -m app.ingestion.cli`) wires the real seams for `make ingest path=… key=…
    audio=…` and prints each pipeline step. Engine contract in `app/ingestion/CLAUDE.md`.
  - Tests (`tests/test_ingestion.py`): a golden extract→structure→merge yields the
    expected section/item counts & types; the key merge fills `correct`/`accepted` +
    misspelling variants; a missing or out-of-range key is rejected; the result is always
    `draft` (never published); ASR alignment is deterministic and in range; crops are
    stored as assets; an AST import guard over the public surface.
  - Deps: `arq`, `pymupdf` added to `requirements.txt` (whisperx noted, install
    per-platform); `fitz`/`whisperx` `ignore_missing_imports` mypy overrides.
- **Phase 8 — Analysis engine** (`app/analysis/`).
  - `AnalysisService.analyze(attempt_id)` — layer 3 of the integrity pipeline
    (`telemetry` → `integrity` → **`analysis`**, golden rule #6). It consumes the
    Phase 7 `IntegrityProfile` via `IntegrityService` (it does **not** re-derive
    features) plus the raw event segments behind it, and asks an injected
    `AnalysisLLM` for an advisory `AnalysisVerdict` (`suspicion_score`, `confidence`,
    `flags[]`, `summary`, `model_id`). With no analyst injected it returns a neutral
    zero-suspicion verdict and logs a WARNING (mirrors grading's no-grader fallback).
  - `flag_segments(profile, events)` — a **pure, deterministic** selector of the raw
    segments worth surfacing: long bounded hides (≥ `LONG_HIDDEN_MS`) and fast
    post-return answers (≤ `POST_RETURN_FAST_MS`, reused from integrity), each
    carrying the raw `IntegrityEvent`s in its window so the teacher's replay stays
    auditable next to the verdict (rule #6).
  - `AnalysisLLM` protocol + `MockAnalysisLLM` (deterministic blend of systematicity
    + total hidden time, monotone so a cheatier profile scores higher) for the
    verdict path; tests inject the mock.
  - Real `AnthropicAnalyst` (`llm_anthropic.py`) — lazy `anthropic` import, structured
    output via `messages.parse` with adaptive thinking, clamps the model's
    `suspicion_score`/`confidence` into `[0, 1]`, and logs model id + token usage +
    latency (golden rule #8). Not re-exported from `__init__.py`, so importing the
    package never needs the SDK.
  - **Advisory only — never touches a score (golden rule #2):** the engine imports no
    `app.grading` (asserted via an AST import scan); `AnalysisVerdict` carries no
    score, and a behavioural test confirms a re-grade is byte-for-byte identical
    after analysis runs. Being the LLM layer, it MAY import `anthropic` (unlike
    telemetry/integrity). Nothing is persisted (mirrors Phase 5/7; storing the verdict
    with the attempt is an Admin/Phase-10 concern). Engine contract in
    `app/analysis/CLAUDE.md`.
  - Tests (`tests/test_analysis.py`): segment-flagging goldens (incl. short-hide and
    honest-stream negatives); the mock's determinism, range, and monotonicity; the
    no-analyst neutral fallback; the service reading through the repo + integrity and
    validating against the schema; the no-score-field + grade-untouched invariants;
    and the import-graph guard.
- **Phase 7 — Integrity engine** (`app/integrity/`).
  - `extract_profile(attempt_id, events)` — a **pure, deterministic** reducer from an
    attempt's `IntegrityEvent` stream to an `IntegrityProfile`: per-question latency +
    interaction count, hidden intervals + durations (`total_hidden_ms`), the
    post-return pattern (`visible -> answer` latency), per-question pacing coefficient
    of variation, and a systematicity rate across questions. Events are ordered by
    trusted `server_ts` with the ingest index as a stable tie-breaker, so the same
    events always yield the same profile regardless of input order.
  - `IntegrityService.profile(attempt_id)` reads the append-only stream via
    `EventRepository.list_events` (telemetry owns the raw stream; integrity only reads
    it — it does not import `app/telemetry`) and logs the profile summary at INFO.
  - Feature rules: a "question" item is any `item_id` carrying an `interaction`/
    `answer_change` event (audio events reference a *section*, never a question);
    latency = first-answer − first-sighting (unanswered → 0, excluded from pacing);
    `post_return_latency_ms` is set only when the last visibility transition before the
    answer was `visibility_visible`; `pacing_cv` is population std/mean of answered
    latencies; `systematicity_rate` is the fraction of questions answered within
    `POST_RETURN_FAST_MS` (2000 ms) of returning to the tab.
  - **Deterministic, no LLM, judges nothing about guilt (golden rules #2, #6):** the
    engine imports no `app.grading` / `app.analysis` / LLM SDK (asserted via an AST
    import scan). The advisory verdict is Phase 8 (`app/analysis`). Nothing is
    persisted (mirrors Phase 5). Engine contract in `app/integrity/CLAUDE.md`.
  - Tests (`tests/test_integrity.py`): synthetic event-stream goldens for every
    feature; threshold boundary; audio/unanswered/unclosed-hide/empty edge cases;
    determinism under reordering; the service reading through the repository; and the
    import-graph guard.
- **Phase 6 — Telemetry engine** (`app/telemetry/` + recorder in `apps/web/`).
  - `TelemetryService.record_batch(attempt_id, batch)` appends a batch of recorder
    events to the append-only store via `EventRepository`, which stamps the trusted
    `server_ts` and logs each event at **WARNING** (golden rule: integrity events
    surfaced loudly). `list_events` reads the stream back in ingest order.
  - Ingest transport (`schema.py`): `EventBatch` of `ClientEvent`. `ClientEvent`
    has **no `server_ts` field** (`extra="forbid"`), so a client structurally cannot
    forge the trusted server timestamp — the same way delivery's `Client*`
    projections make "no answer key to the client" structural.
  - HTTP: `POST /attempts/{attempt_id}/events` (`apps/api/telemetry.py`), append-only,
    returns `{ingested: n}`. `apps/web` is served as static assets so the recorder
    posts same-origin.
  - Browser recorder (`apps/web/recorder.js`, vanilla JS, no build step): captures
    `visibility_hidden`/`visibility_visible` (+hidden duration), `window_blur`/
    `window_focus`, `pagehide`, per-question `interaction`, `answer_change`, and
    `audio_play`/`audio_seek`. Batches on a timer and flushes synchronously via
    `navigator.sendBeacon` on hide/pagehide; telemetry failures never break the UI.
  - **Capture only — no judgment (golden rule #6):** the engine imports no
    `app.grading` / `app.integrity` / `app.analysis` (asserted via an AST import
    scan). Feature extraction (Phase 7) and the LLM verdict (Phase 8) are separate
    layers. Engine contract in `app/telemetry/CLAUDE.md`.
  - Tests: batch persists with both `client_ts` and a server-stamped `server_ts`;
    append-only accumulation; one WARNING per event; a posted `server_ts` is
    rejected (422); import-graph cleanliness; **Playwright E2E** drives the real
    recorder in headless Chromium, backgrounds the tab, and asserts a
    `visibility_hidden` event reaches the store (skips if Playwright is absent).
    Added dev deps: `playwright`, `pytest-playwright`.
- **Phase 5 — Grading engine** (`app/grading/`).
  - `GradingService.grade(attempt_id)` reads the `Attempt`, its canonical `Answer`
    rows, and the authoring `Test`, grades every item, and assembles a
    `GradingResult` (one `ItemGrade` each + `score` / `max_score` / `needs_review`).
  - Deterministic graders (`deterministic.py`, pure & reproducible): `single_choice`
    and `matching` compare the canonical response to the authored `correct`;
    `gap_fill` normalizes (`normalize.py`: casefold + whitespace) then matches
    `accepted` -> `accepted_variants` -> a rapidfuzz ratio (default 85, configurable)
    for unlisted acceptable misspellings, skipping the fuzzy step for short answers.
    One point per objective item.
  - `open_writing` graded behind an injected `LLMGrader` protocol (`llm.py`):
    `MockLLMGrader` is deterministic for tests; the real `AnthropicWritingGrader`
    (`llm_anthropic.py`, lazy SDK import, kept out of the package `__init__`) uses
    structured outputs on `claude-opus-4-8` and logs model id + token usage +
    latency (golden rule #8). `grade_mode="manual"` (or an llm item with no grader
    injected) routes to needs-review instead of guessing.
  - Manual override (`apply_override`) regrades one item as `open_writing_manual`,
    clears its `needs_review`, and recomputes totals (returns a new result, no
    mutation); `review_queue` lists items a human must finish.
  - Golden rule #2 held: the engine imports no `app/telemetry` or `app/integrity`
    (asserted in tests); a cheating signal can never move a score. Engine contract
    in `app/grading/CLAUDE.md`. Added deps: `rapidfuzz`, `anthropic`.
  - Tests: objective goldens; gap_fill normalization + acceptable-misspellings +
    threshold; writing via a mocked grader (llm / manual / no-grader fallback);
    score assembly, review queue, manual override; import-graph cleanliness.
- **Phase 4 — Delivery engine** (`app/delivery/`).
  - `DeliveryService` drives the attempt lifecycle: `start` (validate exam
    window + grace, then **create-or-resume** — a roster entry never gets a
    second attempt; reopening the link resumes), serve (`client_test` /
    `serve_item`), `save_answer`, `get_state` (server timer), `submit`.
  - `projection.py`: pure authoring -> `Client*` projection that strips the answer
    key and applies the per-attempt `OptionShuffle` to `single_choice` options —
    the enforcement point for golden rule #1 (no `correct` to the client).
  - The per-attempt `AttemptLayout` is recomputed from `Attempt.seed`
    (`derive_seed(test_id, roster_entry_id)`) on every call, so a resume serves
    the identical draw; `bank_builder` is injectable (default: one singleton pool
    per section, delivering the whole authored test plus option shuffle).
  - `ExamWindow` (opens/closes + `grace_seconds`, which extends the close);
    deadline = `min(started_at + duration, window close)`, fixed at start. The
    timer is server-authoritative and never pauses (golden rule #3): `get_state`
    flips a crossed-deadline attempt to `expired`; late `save_answer`/`submit`
    raise `AttemptExpiredError`. `save_answer` maps the displayed option index
    back to the canonical key before persisting.
  - `DeliveryError` hierarchy (window / expiry / state / not-found) so callers can
    distinguish rejection modes. Full lifecycle logs at INFO.
  - `AttemptRepository` extended with `update_attempt` / `update_roster_entry`
    (write-back of existing columns). Engine contract in
    `app/delivery/CLAUDE.md`.
  - Schema change (golden rule #4): a **unique constraint** on
    `attempts.roster_entry_id` enforces one attempt per roster entry at the DB,
    so a concurrent double-start fails loudly instead of forking two attempts;
    `start()` catches the violation and resumes the winner. Migration
    `25c1f9debd77_attempt_unique_per_roster_entry`.
  - Tests: served payload has no `correct`; window/grace enforced; refresh
    resumes the same attempt; displayed->canonical round-trip; timer expiry; late
    submit/save rejected.
- **Phase 3 — Content engine** (`app/content/`).
  - `ContentService` facade for item-bank CRUD (create/read/list/publish/
    unpublish/delete) plus asset storage, built on the existing
    `ContentRepository`/`AttemptRepository` (extended with `list_tests`,
    `set_status`, `delete_test`, `list_roster_entries`).
  - `StorageBackend` interface + `FilesystemStorage` for asset blobs keyed by
    `asset_id` (MinIO slots in later); asset ids are validated against path
    traversal.
  - Pooling (`pooling.py`): `TestBank`/`SectionPool` bank shape and a pure,
    seed-driven `build_attempt_layout` producing the per-attempt `AttemptLayout`
    (drawn `section_ids` + per-item `OptionShuffle`). Randomization is at the
    section-pool level plus a `single_choice` option shuffle; item order within a
    section is never touched (golden rule #7). The permutation is recomputed from
    `Attempt.seed` (not stored as its own column); a SHA-256 keyed RNG gives
    independent per-pool/per-item streams.
  - Roster + assignment (`roster.py`): `RosterService` manages a test's named
    roster; `derive_seed(test_id, roster_entry_id)` gives each student a stable
    seed (reproducible across resumes), constrained to signed 32-bit for
    Postgres portability.
  - Every pooling/assignment decision logs at INFO via the shared logger.
  - Engine contract in `app/content/CLAUDE.md`.
  - Tests: two seeded students get different valid section sets; option
    permutation round-trips displayed-index -> canonical; layout reproducible
    from a seed; storage round-trip + traversal guard; roster/seed assignment.
- **Phase 2 — Persistence & migrations.**
  - `app/core/db.py`: sync SQLAlchemy 2.0 engine + `sessionmaker`, declarative
    `Base`, and `session_scope` / `get_session` helpers. DB URL comes from
    `Settings.database_url` (sqlite by default, Postgres via env) — added
    `database_url` / `db_echo` to `app/core/config.py` and `.env.example`.
  - ORM models in `app/persistence/models.py` mirroring the contracts
    (golden rule #4): normalized `tests`/`sections`/`items` (polymorphic answer-key
    and stimulus parts stored as contract-governed JSON), and flat
    `roster_entries`/`attempts`/`answers`/`integrity_events`. A `UtcDateTime`
    column type normalizes all datetimes to UTC-aware on read/write so the
    server-authoritative timer (rule #3) and event ordering (rule #6) stay
    comparable on sqlite, which otherwise drops `tzinfo`.
  - Thin repository layer in `app/persistence/repository.py`
    (`ContentRepository`, `AttemptRepository`, `EventRepository`) translating
    between contract models and rows, logging at the persistence boundary
    (events logged at WARNING per CLAUDE.md); `EventRepository` is append-only and
    stamps `server_ts` on ingest.
  - Alembic initialized (`alembic.ini`, `migrations/env.py` driven by app settings
    + `Base.metadata`) with the initial schema migration; `make migrate` applies
    clean and `alembic check` reports no drift.
  - Added `sqlalchemy`, `alembic`, `psycopg[binary]` to `requirements.txt`.
  - Tests: create/read round-trips per aggregate against an in-memory sqlite DB,
    section/item order preservation, answer upsert, and append-only event ingest
    with `server_ts` stamping.
- **Phase 1 — Contracts (schemas).**
  - Authoring item-bank contracts in `contracts/content.py`: `Test`, `Section`
    (stimulus union: `passage_text` | `audio_asset` | `image_set` | `gapped_text` |
    `matching_pool`), `Item` (type union: `single_choice` | `gap_fill` | `matching` |
    `open_writing`), and `Option` (text | image) — these carry the answer key
    (`correct`, `accepted`, `accepted_variants`, `rubric`).
  - Parallel student-facing client family (`ClientTest`/`ClientSection`/`ClientItem`/
    `ClientOption`) that is structurally incapable of carrying an answer key, plus
    client options that drop the canonical `key` (golden rule #1).
  - Runtime contracts in `contracts/runtime.py`: `Attempt`, `Answer`,
    `IntegrityEvent`, `GradingResult` (+ `ItemGrade`), `IntegrityProfile`
    (+ `QuestionTiming`, `HiddenInterval`), `AnalysisVerdict`, `RosterEntry`.
  - JSON Schema generation (`contracts/export_jsonschema.py` -> `contracts/jsonschema/`)
    driven by a `REGISTRY`, wired to `make schema`.
  - Tests: valid/invalid fixtures parse/raise; the no-`correct` invariant walks every
    client-facing schema; committed JSON Schema is asserted in sync with the models.
- **Phase 0 — Scaffold & tooling.**
  - Repo layout per `CLAUDE.md` (engine package skeleton under `app/`, `contracts/`,
    `apps/`, `tests/`, `migrations/`).
  - `pyproject.toml` (ruff + mypy + pytest config) and `requirements.txt`.
  - Canonical `app/core/logging.py` (console logger to stdout) and
    `app/core/config.py` (pydantic-settings) + `.env.example`.
  - FastAPI app `apps/api/main.py` with `GET /health` that logs the request via the
    shared logger and returns `{"status": "ok"}`.
  - Pre-commit hooks (ruff, ruff-format, mypy) and one health-endpoint test.
