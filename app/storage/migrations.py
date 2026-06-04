"""Database migration runner — Alembic, programmatically configured.

This module is the only place the rest of the codebase touches Alembic.
Two reasons for going programmatic instead of carrying an ``alembic.ini``:

1. The migration tree lives inside the wheel at ``app/alembic/``, so the
   path resolution depends on the installed package location (which an
   ``alembic.ini`` in the source tree can't see at runtime).
2. The DB URL comes from ``bootstrap.toml`` + ``CREMIND_*`` env vars via
   :mod:`app.databases`. Restating it in an ini file would be a second
   source of truth.

Boot-time contract (called by ``ConversationStorage.initialize``):

- Fresh DB           : run ``upgrade head`` — baseline migration creates
                       every table, future migrations chain off that.
- Existing DB stamped: run ``upgrade head`` — fast no-op if already at head.
- Existing DB not stamped (pre-Alembic install): apply ``compat_preflight``
  to bring the schema up to baseline, ``stamp baseline``, then
  ``upgrade head``.

The preflight encodes the additive ``ALTER TABLE`` statements that the
old ``initialize()`` used to apply on every boot. They were idempotent
there and remain idempotent here — safe to run on a partially-migrated
database, no-op on a fully-migrated one.
"""

from __future__ import annotations

import asyncio
from importlib.resources import files
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.databases import get_database_provider
from app.utils.logger import logger


_BASELINE_REVISION = "20260509_baseline"


def _alembic_dir() -> Path:
    """Resolve the on-disk path to the bundled ``app/alembic`` directory.

    Uses ``importlib.resources`` so we work the same whether the package
    is installed from a wheel, run from a source checkout, or zipped.
    """
    return Path(str(files("app").joinpath("alembic")))


def build_alembic_config() -> Config:
    """Configure Alembic in-process — no ini file involved.

    The URL is *not* set on this config: ``env.py`` always pulls the live
    engine from :func:`app.databases.get_database_provider` so that pragma
    application (WAL on SQLite, etc.) matches the runtime exactly. Setting
    ``sqlalchemy.url`` here would only matter for offline mode, where it
    would re-read the bootstrap anyway.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(_alembic_dir()))
    cfg.set_main_option("version_locations", str(_alembic_dir() / "versions"))
    cfg.set_main_option("file_template", "%%(year)d%%(month).2d%%(day).2d_%%(slug)s")
    return cfg


def current_revision() -> str | None:
    """Return the live database's current Alembic revision id, or ``None``."""
    engine = get_database_provider().sync_engine()
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        return ctx.get_current_revision()


def heads() -> tuple[str, ...]:
    """Return the script directory's head revisions (usually one)."""
    return ScriptDirectory.from_config(build_alembic_config()).get_heads()


def upgrade(target: str = "head") -> None:
    command.upgrade(build_alembic_config(), target)


def downgrade(target: str) -> None:
    command.downgrade(build_alembic_config(), target)


def stamp(target: str = "head") -> None:
    command.stamp(build_alembic_config(), target)


# ── boot orchestration ─────────────────────────────────────────────────────


async def ensure_at_head() -> None:
    """Bring the live database to the latest schema, idempotently.

    Async wrapper around the sync Alembic API: the engine is sync (Alembic's
    own constraint), and we don't want to block the event loop while
    migrations execute, so the work runs in a thread.
    """
    await asyncio.to_thread(_ensure_at_head_sync)


def _ensure_at_head_sync() -> None:
    provider = get_database_provider()
    engine = provider.sync_engine()

    import time as _mig_time
    _mig_start = _mig_time.monotonic()
    _before_rev = current_revision()

    # SQLite's sync engine applies WAL + foreign_keys via a connect-event
    # listener, and Postgres has no equivalent pragmas — so opening a
    # connection here is enough to get a properly-configured session for
    # the inspection/preflight pass.
    with engine.connect() as conn:
        inspector = inspect(conn)
        has_alembic_version = inspector.has_table("alembic_version")
        has_profiles = inspector.has_table("profiles")

        if not has_alembic_version and has_profiles:
            # Pre-Alembic install: apply the legacy additive ALTERs so the
            # schema reaches the baseline state, then stamp without rerunning
            # the baseline migration's ``create_all``.
            logger.info(
                "[migrations] pre-Alembic database detected: applying compat "
                "preflight + stamping baseline"
            )
            _compat_preflight(conn, provider)
            conn.commit()
            stamp(_BASELINE_REVISION)

    upgrade("head")
    rev = current_revision()
    if rev == _before_rev:
        logger.info(f"[migrations] schema already at head rev={rev or '<none>'}")
    else:
        logger.info(
            f"[migrations] applied from={_before_rev or '<none>'} to={rev or '<none>'} "
            f"elapsed={_mig_time.monotonic() - _mig_start:.1f}s"
        )


# ── compat preflight ───────────────────────────────────────────────────────


def _compat_preflight(conn, provider) -> None:
    """Apply the inline ALTERs that pre-Alembic installs ran every boot.

    These were idempotent in the old code path and remain idempotent here.
    Order matters only for the column rename + the channel_id add (it must
    exist before the conversation backfill runs in ``ConversationStorage``).
    """
    _add_column_if_absent(
        conn, provider, "profiles",
        "skill_mode VARCHAR(16) NOT NULL DEFAULT 'manual'",
    )
    _add_column_if_absent(
        conn, provider, "conversations",
        "channel_id VARCHAR(36) REFERENCES channels(id) ON DELETE CASCADE",
    )
    _add_column_if_absent(
        conn, provider, "conversations",
        "working_directory VARCHAR(1024)",
    )

    # WhatsApp mode rename. Idempotent — second run matches no rows.
    try:
        conn.execute(text(
            "UPDATE channels SET mode='userbot' "
            "WHERE channel_type='whatsapp' AND mode='normal'"
        ))
    except Exception:  # noqa: BLE001
        pass

    # Drop the old single-subscription unique index from dev DBs that
    # predate multi-subscription support.
    try:
        conn.execute(text("DROP INDEX IF EXISTS uq_skill_event_subs"))
    except Exception:  # noqa: BLE001
        pass

    # Postgres-only: widen task_id from VARCHAR(36) to VARCHAR(128). On
    # SQLite the length is advisory and the ALTER is unnecessary.
    alter_sql = provider.alter_column_type("conversations", "task_id", "VARCHAR(128)")
    if alter_sql is not None:
        try:
            conn.execute(text(alter_sql))
        except Exception:  # noqa: BLE001
            pass


def _add_column_if_absent(conn, provider, table: str, column_def: str) -> None:
    sql = provider.add_column_if_not_exists(table, column_def)
    if provider.supports_add_column_if_not_exists():
        conn.execute(text(sql))
    else:
        try:
            conn.execute(text(sql))
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "build_alembic_config",
    "current_revision",
    "downgrade",
    "ensure_at_head",
    "heads",
    "stamp",
    "upgrade",
]
