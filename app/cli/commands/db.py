"""`cremind db ...` — schema and database operations against the local DB.

Unlike most ``cremind`` subcommands these don't go through the HTTP API; they
operate on the database directly via :mod:`app.storage.migrations` and
:mod:`app.storage.backup`. They typically run with the Cremind service
stopped — Alembic and SQLite's write-ahead log don't appreciate
concurrent writers.

Subcommands:

  db current     Show the current schema revision.
  db heads       Show the latest revision(s) shipped by this build.
  db upgrade     Apply pending migrations (defaults to ``head``).
  db downgrade   Roll back to a previous revision (target is required).
  db stamp       Mark the database at a revision without running migrations.
  db backup      Snapshot the database to a file (auto-named by default).
  db restore     Restore a snapshot taken by ``db backup``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer


db_app = typer.Typer(
    name="db",
    help="Local database schema and snapshot operations.",
    no_args_is_help=True,
)


@db_app.command("current")
def db_current() -> None:
    """Show the current Alembic revision (or 'none' if the DB is unstamped)."""
    from app.storage import migrations
    rev = migrations.current_revision()
    typer.echo(rev or "none")


@db_app.command("heads")
def db_heads() -> None:
    """Show the head revision(s) shipped by this build."""
    from app.storage import migrations
    for rev in migrations.heads():
        typer.echo(rev)


@db_app.command("upgrade")
def db_upgrade(
    target: str = typer.Argument(
        "head",
        help="Target revision (default: head).",
    ),
) -> None:
    """Apply migrations forward to ``target``."""
    # ``cremind serve`` runs in deferred-storage mode when ``bootstrap.toml``
    # is missing — it won't materialise a DB until the wizard's first-setup
    # request. The CLI path bypasses the wizard, so committing to a backend
    # at the moment we're about to materialise one keeps the two modes
    # self-consistent: a later ``cremind serve`` boots normally instead of
    # re-entering deferred mode and creating a parallel DB via the wizard.
    from app.config.bootstrap import bootstrap_exists, write_bootstrap
    if not bootstrap_exists():
        # Seam D — Kubernetes enforcement. The default-to-SQLite shortcut below
        # is correct for desktop/native installs but would silently commit a
        # pod-local SQLite backend on Kubernetes, where horizontal scaling
        # requires a shared PostgreSQL. The chart's entrypoint never reaches
        # here without a bootstrap (it skips ``db upgrade`` until the wizard
        # writes one), but a manual ``cremind db upgrade`` inside a pod would.
        from app.config.install_catalog import is_kubernetes_mode
        if is_kubernetes_mode():
            typer.echo(
                "SQLite is not supported in Kubernetes mode "
                "(INSTALL_MODE=kubernetes); pod-local storage breaks horizontal "
                "scaling. Complete the Setup Wizard to configure PostgreSQL "
                "instead of running 'cremind db upgrade' manually.",
                err=True,
            )
            raise typer.Exit(code=1)
        write_bootstrap({"db_provider": "sqlite"})

    from app.storage import migrations
    migrations.upgrade(target)
    typer.echo(f"Upgraded to {migrations.current_revision() or '<none>'}")


@db_app.command("downgrade")
def db_downgrade(
    target: str = typer.Argument(..., help="Target revision (e.g., '-1' or a revision id)."),
) -> None:
    """Roll back migrations to ``target``."""
    from app.storage import migrations
    migrations.downgrade(target)
    typer.echo(f"Downgraded to {migrations.current_revision() or '<none>'}")


@db_app.command("stamp")
def db_stamp(
    target: str = typer.Argument(
        "head",
        help="Revision to stamp at (default: head).",
    ),
) -> None:
    """Mark the database at ``target`` without running any migrations.

    Use case: an externally-restored DB that you know matches a particular
    revision. Mis-stamping leads to silent corruption on the next migration,
    so this is generally only run by the upgrader during recovery.
    """
    from app.storage import migrations
    migrations.stamp(target)
    typer.echo(f"Stamped at {migrations.current_revision() or '<none>'}")


@db_app.command("backup")
def db_backup(
    to: Optional[Path] = typer.Option(
        None,
        "--to",
        help="Output path. Defaults to ~/.cremind/backups/<timestamp>.<ext>.",
    ),
) -> None:
    """Snapshot the database to a file.

    SQLite snapshots are point-in-time file copies (consistent because the
    sync engine flushes WAL on each write). Postgres snapshots are
    gzipped JSON Lines containing the ORM-derived schema plus row data,
    produced in pure Python (no ``pg_dump`` binary required). Either way
    the output path is printed on success so the upgrader can capture it.
    """
    from app.storage.backup import backup as _backup

    out = _backup(to)
    typer.echo(str(out))


@db_app.command("restore")
def db_restore(
    src: Path = typer.Argument(..., help="Path to a backup file produced by `cremind db backup`."),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt.",
    ),
) -> None:
    """Restore the database from a snapshot.

    Destructive: the live database is replaced with the contents of ``src``.
    Run only with the Cremind service stopped.
    """
    from app.storage.backup import restore as _restore

    if not yes:
        confirm = typer.confirm(
            f"Restore from {src}? This will overwrite the current database.",
            default=False,
        )
        if not confirm:
            typer.echo("Aborted.")
            raise typer.Exit(code=1)

    _restore(src)
    typer.echo(f"Restored from {src}")
