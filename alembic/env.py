"""Alembic, pointed at the same database the service uses.

Runs against the SYNC url. Alembic's async recipe exists, but migrations run in
a one-shot container where an event loop buys nothing and costs a failure mode.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.core.config import settings

config = context.config
config.set_main_option("sqlalchemy.url", settings.sync_database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Deliberately None: autogenerate is not used here (see alembic.ini), and
# handing it the full metadata would invite it to try.
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
