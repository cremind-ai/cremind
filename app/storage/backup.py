"""Database backup and restore — thin dispatcher over the active provider.

The upgrader calls :func:`backup` before running migrations and keeps the
returned path so a post-upgrade health-check failure can trigger
:func:`restore`. ``opa db backup`` and ``opa db restore`` are thin
wrappers around these.

This module no longer knows anything provider-specific: SQLite uses the
``sqlite3`` backup API for a file-level snapshot, Postgres dumps schema +
rows to a gzipped JSON Lines file (see
:meth:`SqliteDatabaseProvider.backup` and
:meth:`PostgresDatabaseProvider.backup`). The previous implementation
shelled to ``pg_dump`` / ``pg_restore`` which broke every Docker-installed
Postgres upgrade because the cremind container doesn't ship the postgres
CLI — moving the logic onto the provider is what fixed that.
"""

from __future__ import annotations

import time
from pathlib import Path

from app.databases import get_database_provider
from app.utils import logger


def _default_backup_root() -> Path:
    """Resolve the default backup directory.

    Imported lazily so this module stays importable in environments where
    the settings stack hasn't been initialized yet (e.g., bare ``opa db``
    invocations against a non-existent install).
    """
    from app.config.settings import BaseConfig
    return Path(BaseConfig.CREMIND_SYSTEM_DIR) / "backups"


def _timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.localtime())


def backup(to: Path | None = None) -> Path:
    """Take a snapshot. Returns the path the snapshot was written to.

    Format is decided by the active database provider. The default
    destination is ``~/.cremind/backups/<timestamp>.<provider.backup_suffix>``;
    callers can pass a path to override.
    """
    provider = get_database_provider()
    if to is None:
        root = _default_backup_root()
        root.mkdir(parents=True, exist_ok=True)
        to = root / f"{_timestamp()}.{provider.backup_suffix}"
    else:
        to = Path(to)
        to.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"[db:backup] starting provider={provider.name} dest={to}")
    started = time.monotonic()
    out = provider.backup(to)
    try:
        size = out.stat().st_size
    except OSError:
        size = -1
    logger.info(
        f"[db:backup] complete provider={provider.name} bytes={size} "
        f"elapsed={time.monotonic() - started:.1f}s path={out}"
    )
    return out


def restore(src: Path) -> None:
    """Replace the live database with the contents of ``src``.

    Destructive — caller is responsible for ensuring no writers are
    connected. The CLI prompts for confirmation; the upgrader takes the
    backup path it captured at upgrade-start time.
    """
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(src)

    provider = get_database_provider()
    logger.info(f"[db:restore] starting provider={provider.name} src={src}")
    started = time.monotonic()
    provider.restore(src)
    logger.info(
        f"[db:restore] complete provider={provider.name} "
        f"elapsed={time.monotonic() - started:.1f}s"
    )


__all__ = ["backup", "restore"]
