# Shared image for the API and the arq worker (Phase 12).
# Python 3.12 (not the dev 3.14) so the ingestion wheels — pymupdf etc. — resolve
# in the container, which is where real ingestion is meant to run.
FROM python:3.12-slim

# Unbuffered stdout so every service streams clean log lines to `docker compose`
# (CLAUDE.md logging rules: the developer watches them live in cmd).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

EXPOSE 8000

# Default = API. The worker service overrides `command` in docker-compose.
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
