"""Alembic migration environment for LeakSentinel.

The target metadata is the shared declarative ``Base`` after every model module
has been imported, and the database URL comes from application settings
(``DATABASE_URL``) rather than alembic.ini, so migrations always target the same
database the app uses.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from leaksentinel.config import get_settings
from leaksentinel.db import Base, _load_models

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import all model modules so they register on Base.metadata, then expose it.
_load_models()
target_metadata = Base.metadata

# Inject the application's database URL (overrides any value in alembic.ini).
config.set_main_option("sqlalchemy.url", get_settings().database_url)


def run_migrations_offline() -> None:
    """Run migrations without a DB connection (emit SQL)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations with a live DB connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
