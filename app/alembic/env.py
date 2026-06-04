"""Alembic environment for Cremind.

This file is invoked by Alembic (online or offline) to apply migrations.
The DB URL is sourced from the same ``bootstrap.toml`` the runtime uses, so
we never have two sources of truth for which database to migrate.

Online mode (the path used at server startup and from ``cremind db upgrade``):
the existing :class:`DatabaseProvider` builds the sync engine, so SQLite
gets the same WAL/foreign-key pragmas it would get at runtime.

Offline mode (``alembic upgrade --sql ...``): only the URL is used; the
SQL is rendered to stdout so an operator can review what will run before
pointing it at production.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from app.databases import get_database_provider

# a2a.server.models is the SQLAlchemy declarative base shared with the
# runtime — importing app.storage.models registers all Cremind tables on
# this metadata. ``target_metadata`` drives autogenerate diff reports for
# future migrations.
from a2a.server.models import Base
import app.storage.models  # noqa: F401 — registers tables on Base


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Render migration SQL to stdout without opening a connection."""
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        # Fall back to the runtime provider's URL so ``alembic upgrade --sql``
        # works without an explicit ``-x url=...`` argument.
        provider = get_database_provider()
        url = str(provider.sync_engine().url)

    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite + Alembic ALTER TABLE works only via ``batch`` mode (which
        # rebuilds the table). Enable per-dialect so future migrations don't
        # silently break on SQLite.
        render_as_batch=url.startswith("sqlite"),
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against the live runtime database."""
    provider = get_database_provider()
    connectable = provider.sync_engine()

    with connectable.connect() as connection:
        is_sqlite = connection.engine.dialect.name == "sqlite"
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
