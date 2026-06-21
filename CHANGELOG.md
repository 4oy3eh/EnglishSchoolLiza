# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
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
