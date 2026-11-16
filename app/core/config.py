"""Application settings, loaded from the environment via pydantic-settings.

Values come from process env or a local `.env` file (see `.env.example`).
Import the shared `settings` singleton; do not read `os.environ` directly.
"""

from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Dev-only admin auth defaults (see `Settings.teacher_password`). Kept as named
# constants so the admin API can detect a deployment that never overrode them.
DEV_TEACHER_PASSWORD = "change-me"
DEV_ADMIN_TOKEN_SECRET = "dev-insecure-admin-secret"
# Environments where the dev defaults above are tolerated — and then only for a
# request made on this machine (loopback, not relayed; see `app/admin/auth.py`).
DEV_ENVIRONMENTS = frozenset({"dev", "test"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- App ---
    app_name: str = "english-exam"
    # Fail-closed: NOT a dev environment unless ENVIRONMENT=dev|test is set
    # explicitly (process env or `.env`; `.env.example` ships `ENVIRONMENT=dev`).
    # A deployment that configures nothing must not inherit dev leniency — the
    # admin API refuses the default password / token secret outside dev/test.
    environment: str = "production"
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
    # While either still equals its dev default below, the admin API refuses to
    # log in (and refuses every token) — a token signed with a public default
    # secret could be forged by anyone. The one exception: `environment` is
    # explicitly "dev"/"test" AND the request comes from this machine (loopback
    # peer, no proxy/tunnel forwarding header).
    teacher_password: str = DEV_TEACHER_PASSWORD
    admin_token_secret: str = DEV_ADMIN_TOKEN_SECRET
    admin_token_ttl_seconds: int = 60 * 60 * 8  # an 8-hour teaching session
    # Brute-force guard on POST /admin/login (in-memory sliding window):
    # `login_max_failures` wrong passwords from ONE client IP, or
    # `login_global_max_failures` from ALL clients together (0 = off), inside
    # `login_window_seconds` -> 429. The global cap is the real brute-force bound
    # (rotating IPs buy nothing); the per-IP one is kept lenient because behind a
    # proxy/tunnel that hides client IPs the whole class shares one key.
    login_max_failures: int = 10
    login_global_max_failures: int = 30
    login_window_seconds: int = 300

    # --- Writing auto-grading (Gemini) ---
    # When set, open_writing items are graded by the Gemini API (rubric-based,
    # 0-5 like a Cambridge examiner). Empty/unattempted writing is skipped, so the
    # B1 Part 2 "choose one of two" leaves the unchosen task uncounted. Without a
    # key, writing falls back to the manual review queue.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    # --- Integrity verdict: optional LLM second opinion (ANALYSIS_LLM) ---
    # The cheating verdict of record is the DETERMINISTIC rule engine
    # (`app/integrity/rules.py`): suspicion, ranking and every rule card come from
    # it and never from a model. This switch only controls an extra Gemini call:
    #   "writing" — ask Gemini for a second opinion on the WRITING answers only
    #               (AI-assist likelihood, level mismatch). Needs GEMINI_API_KEY;
    #               without a key it behaves exactly like "off".
    #   "off"     — never make a Gemini analysis call.
    # Writing GRADING and the per-skill FEEDBACK are separate Gemini calls and are
    # not affected by this setting.
    analysis_llm: Literal["off", "writing"] = "writing"

    @field_validator("analysis_llm", mode="before")
    @classmethod
    def _normalize_analysis_llm(cls, value: object) -> object:
        """`ANALYSIS_LLM=OFF` / ` Writing ` are accepted; an empty value = default."""
        if isinstance(value, str):
            value = value.strip().lower()
            return value or "writing"
        return value


settings = Settings()
