# English School Liza

Online English testing platform for a language school (Cambridge **A2 Key** / **B1
Preliminary** format). Students take tests in the browser via one shareable link per
test. The platform serves randomized per-student tests, auto-grades, records
behavioral telemetry, and produces an advisory cheating-likelihood verdict for the
teacher. Content is created by ingesting Cambridge-style PDFs (+ mp3) into a
structured item bank.

See [CLAUDE.md](CLAUDE.md) for the full project brief, golden invariants, and layout,
and [docs/](docs) for architecture and the development plan.

## Stack

FastAPI, SQLAlchemy 2.0, Alembic, Postgres, Redis, arq, MinIO, PyMuPDF/Docling,
WhisperX, Instructor, rapidfuzz, fastapi-users, Playwright.

## Commands

```
make install   # install dependencies
make run       # run the API (uvicorn, reload)
make test      # run the test suite
make lint      # ruff check
make migrate   # alembic upgrade head
make seed      # seed the item bank
make ingest path=... key=... audio=...   # ingest a Cambridge-style PDF
```

## Layout

```
contracts/   # Pydantic models + generated JSON Schema (source of truth)
app/         # engines: core, content, ingestion, delivery, grading, telemetry, integrity, analysis, admin
apps/        # api (FastAPI), web (student runner, teacher dashboard)
tests/       # golden samples, grading goldens, synthetic event streams
migrations/  # Alembic
docs/        # ARCHITECTURE.md, DEVELOPMENT_PLAN.md, PROMPTS.md
```
