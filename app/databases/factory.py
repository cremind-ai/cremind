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

    # Seam B — Kubernetes backstop. resolve_bootstrap() defaults to "sqlite"
    # when neither bootstrap.toml nor CREMIND_DB_PROVIDER is set, so a stray
    # sqlite bootstrap (baked into an image layer, or a leftover on the PVC)
    # would otherwise build a pod-local SQLite engine that breaks horizontal
    # scaling. The wizard write path (Seam A) and the CLI (Seam D) reject the
    # choice up front; this is the last line of defence at the point every
    # storage build funnels through.
    if provider == "sqlite":
        from app.config.install_catalog import is_kubernetes_mode
        if is_kubernetes_mode():
            raise ValueError(
                "SQLite is not supported in Kubernetes mode "
                "(INSTALL_MODE=kubernetes); pod-local storage breaks "
                "horizontal scaling. Configure PostgreSQL."
            )

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
