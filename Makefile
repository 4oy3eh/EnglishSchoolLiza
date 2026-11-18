.PHONY: install run test lint typecheck migrate seed ingest ingest-a2 ingest-b1 ingest-movers load-test export-test import-test review-test schema fmt backup restore session

install:
	pip install -r requirements.txt
	pre-commit install || true

run:
	uvicorn apps.api.main:app --reload --port 8000 --log-level info

test:
	pytest -q

lint:
	ruff check .

fmt:
	ruff format .

typecheck:
	mypy app contracts apps

migrate:
	alembic upgrade head

# regenerate JSON Schema from the Pydantic contracts (source of truth)
schema:
	python -m contracts.export_jsonschema

seed:
	python -m app.content.seed

# (re)load the manual A2 Key 2022 sample ingest as a DRAFT (Option B, no paid API).
# Does not publish and adds no roster — those happen after human review.
ingest-a2:
	python -m app.content.ingest_a2_2022

# (re)load the manual B1 Preliminary 2022 sample ingest as a DRAFT.
ingest-b1:
	python -m app.content.ingest_b1_2022

# (re)load the manual YLE Movers 2022 sample ingest as a DRAFT (gradeable parts).
ingest-movers:
	python -m app.content.ingest_movers_2022

# generic loader: load an authored Test JSON as a draft. usage:
#   make load-test file=path/to/test.json            (assets read from assets_dir)
# add --publish / --roster "A,B" by calling the module directly after review.
load-test:
	python -m app.content.load_test --file "$(file)"

# JSON authoring path (preferred for new tests — no Python builder per test):
#   make export-test id=a2-2022 [out=a2-2022.json]     (authoring JSON incl. the key)
#   make import-test file=test.json [assets=dir] [replace=1] [new_id=x]
# Import always lands as a DRAFT in the teacher review queue (golden rule #5).
export-test:
	python -m app.content.transfer export --id "$(id)" $(if $(out),--out "$(out)")

import-test:
	python -m app.content.transfer import --file "$(file)" $(if $(assets),--assets-dir "$(assets)") $(if $(replace),--replace) $(if $(new_id),--new-id "$(new_id)")

# Offline teacher review page for a draft Test JSON (key highlighted, NOTES.md,
# per-question OK + comments). Writes REVIEW.html next to the JSON by default.
#   make review-test file=path/to/test.json [notes=NOTES.md] [out=REVIEW.html]
review-test:
	python -m scripts.review_html --file "$(file)" $(if $(notes),--notes "$(notes)") $(if $(out),--out "$(out)")

# usage: make ingest path=samples/B1_listening.pdf key=samples/B1_listening_key.pdf audio=samples/b1/
ingest:
	python -m app.ingestion.cli --path "$(path)" --key "$(key)" --audio "$(audio)"

# --- Exam-session operations (docs/OPERATIONS.md) ---------------------------------
# Online SQLite backup -> backups/english_exam-YYYYMMDD-HHMMSS.db (verified, newest
# BACKUP_KEEP=30 kept). usage: make backup [keep=N] [assets=1]
backup:
	python -m scripts.backup_db $(if $(keep),--keep $(keep)) $(if $(assets),--with-assets)

# Restore a backup (server must be STOPPED; the current DB is saved first as
# ...-prerestore.db). usage: make restore file=backups/english_exam-....db
restore:
	python -m scripts.restore_db --file "$(file)"

# Exam session: .env checks -> migrate -> pre-session backup -> uvicorn (1 worker)
# + a backup every 15 min -> final backup on Ctrl+C.
# usage: make session [every=15] [tunnel=cloudflared|ngrok] [port=8000]
session:
	python -m scripts.session $(if $(every),--every $(every)) $(if $(tunnel),--tunnel $(tunnel)) $(if $(port),--port $(port))
