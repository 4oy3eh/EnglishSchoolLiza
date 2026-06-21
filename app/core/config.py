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


settings = Settings()
