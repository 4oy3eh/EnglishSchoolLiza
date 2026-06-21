# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
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
