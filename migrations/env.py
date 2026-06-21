from logging.config import fileConfig
from typing import Any

from sqlalchemy import engine_from_config, pool

from alembic import context
from alembic.autogenerate.api import AutogenContext
from app.core.config import settings
from app.core.db import Base

# Import models so they register on Base.metadata for autogenerate.
import app.persistence.models as models  # noqa: F401


def render_item(type_: str, obj: Any, autogen_context: AutogenContext) -> str | bool:
    """Render custom types by their stable impl so migrations stay self-contained
    (no imports of live app model classes)."""
    if type_ == "type" and isinstance(obj, models.UtcDateTime):
        return "sa.DateTime(timezone=True)"
    return False

config = context.config

# Drive the connection from app settings (single source for the DB URL).
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DBAPI)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite"),
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
