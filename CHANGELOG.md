# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
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
