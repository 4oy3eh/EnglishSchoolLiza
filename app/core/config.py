"""Application settings, loaded from the environment via pydantic-settings.

Values come from process env or a local `.env` file (see `.env.example`).
Import the shared `settings` singleton; do not read `os.environ` directly.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- App ---
    app_name: str = "english-exam"
    environment: str = "dev"
    log_level: str = "INFO"

    # --- API ---
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # --- Database ---
    # Dev/test default to a local sqlite file; prod points at Postgres via env,
    # e.g. postgresql+psycopg://user:pass@host:5432/english_exam
    database_url: str = "sqlite:///./english_exam.db"
    db_echo: bool = False

    # Where asset blobs (image crops, mp3s) live on the local filesystem. MinIO/S3
    # replaces this behind `StorageBackend` later (see `app/content/storage.py`).
    assets_dir: str = "./var/assets"

    # --- Jobs / object storage (Phase 12 wire-up) ---
    # arq's broker (api enqueues, the worker consumes). MinIO settings are carried
    # for the eventual object-storage `StorageBackend` swap (compose runs MinIO).
    redis_url: str = "redis://localhost:6379"
    minio_endpoint: str = ""
    minio_access_key: str = ""
    minio_secret_key: str = ""

    # --- Admin auth (Phase 10) ---
    # Single-teacher access: a password mints a short-lived HMAC-signed bearer
    # token (no user table / migration). Override BOTH in prod via the env —
    # the dev defaults are intentionally weak and must never ship.
    teacher_password: str = "change-me"
    admin_token_secret: str = "dev-insecure-admin-secret"
    admin_token_ttl_seconds: int = 60 * 60 * 8  # an 8-hour teaching session

    # --- Writing auto-grading (Gemini) ---
    # When set, open_writing items are graded by the Gemini API (rubric-based,
    # 0-5 like a Cambridge examiner). Empty/unattempted writing is skipped, so the
    # B1 Part 2 "choose one of two" leaves the unchosen task uncounted. Without a
    # key, writing falls back to the manual review queue.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"


settings = Settings()
