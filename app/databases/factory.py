"""Database factory — picks a provider from bootstrap config and instantiates it.

Mirrors :mod:`app.vectorstores.factory`: a single function that reads a
configuration key and lazily imports the matching backend. New providers are
added by appending one branch here and one file alongside.
"""

from __future__ import annotations

from app.config.bootstrap import resolve_bootstrap

from .base import DatabaseProvider


def create_database_provider() -> DatabaseProvider:
    """Build the configured database provider.

    Reads ``bootstrap.toml`` (with ``CREMIND_*`` env-var overrides). Imports
    are lazy so a misconfigured provider never pulls in unused SDK code.
    """
    cfg = resolve_bootstrap()
    provider = cfg["db_provider"]

    if provider == "sqlite":
        # Imported here so SQLITE_DB_PATH resolution stays lazy — the path
        # depends on CREMIND_SYSTEM_DIR which itself comes from settings.
        from app.config.settings import BaseConfig
        from .sqlite import SqliteDatabaseProvider
        return SqliteDatabaseProvider(BaseConfig.SQLITE_DB_PATH)

    if provider == "postgres":
        from .postgres import PostgresDatabaseProvider
        pg = cfg["postgres"]
        return PostgresDatabaseProvider(
            host=pg["host"],
            port=pg["port"],
            database=pg["database"],
            user=pg["user"],
            password=pg["password"],
            sslmode=pg["sslmode"],
        )

    raise ValueError(f"Unknown database provider: {provider!r}")
