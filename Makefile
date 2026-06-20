.PHONY: install run test lint typecheck migrate seed ingest schema fmt

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

# usage: make ingest path=samples/B1_listening.pdf key=samples/B1_listening_key.pdf audio=samples/b1/
ingest:
	python -m app.ingestion.cli --path "$(path)" --key "$(key)" --audio "$(audio)"
