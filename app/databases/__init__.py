"""Database backend abstraction.

Application logic talks to a :class:`DatabaseProvider` and never branches on
the underlying engine. Wire it up at startup with::

    from app.databases import create_database_provider, set_database_provider
    provider = create_database_provider()
    set_database_provider(provider)

then storage modules read it through :func:`get_database_provider` and obtain
their engines from the provider.
"""

from __future__ import annotations

from .base import DatabaseProvider
from .factory import create_database_provider


_provider: DatabaseProvider | None = None


def get_database_provider() -> DatabaseProvider:
    """Return the active provider, building one from bootstrap config on demand.

    Lazy so that modules importing this at import-time (storage singletons)
    don't force the engine to exist before ``main()`` has a chance to swap
    in a different one.
    """
    global _provider
    if _provider is None:
        _provider = create_database_provider()
    return _provider


def set_database_provider(provider: DatabaseProvider | None) -> None:
    """Install a provider as the active one.

    Passing ``None`` clears the singleton — used by the wizard right before
    re-resolving from a freshly written ``bootstrap.toml``.
    """
    global _provider
    _provider = provider


__all__ = [
    "DatabaseProvider",
    "create_database_provider",
    "get_database_provider",
    "set_database_provider",
]
