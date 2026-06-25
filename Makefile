.PHONY: install run test lint typecheck migrate seed ingest ingest-a2 ingest-b1 ingest-movers load-test schema fmt

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

# usage: make ingest path=samples/B1_listening.pdf key=samples/B1_listening_key.pdf audio=samples/b1/
ingest:
	python -m app.ingestion.cli --path "$(path)" --key "$(key)" --audio "$(audio)"
