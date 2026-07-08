"""`cremind backup ...` — full-system backup & restore.

A ``.cremind-backup`` archive captures the whole system (database, skills,
OAuth tokens, personas, channels, browser login state) in an
environment-independent form and restores it into this or a fresh install,
across OSes and database backends. Distinct from ``cremind db backup``, which
snapshots only the database.

Online subcommands go through the server's REST API (``--server`` /
``CREMIND_TOKEN``). The ``--offline`` flag on ``create`` / ``restore`` talks to
the engine directly and is the path to use with the service **stopped** (moving
to a new machine, disaster recovery). Import discipline: every ``app.*`` import
lives inside a function body (see ``app/cli/main.py``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import typer

from app.cli.commands._helpers import graceful_errors

backup_app = typer.Typer(
    name="backup",
    help="Create, download, and restore full-system backups.",
    no_args_is_help=True,
)

_ENV_PASSPHRASE = "CREMIND_BACKUP_PASSPHRASE"
_TERMINAL_PHASES = ("done", "failed")


def _resolve_passphrase(passphrase: Optional[str], prompt: bool, *, confirm: bool) -> Optional[str]:
    """Resolve a passphrase from --passphrase / --passphrase-prompt / env."""
    if prompt:
        value = typer.prompt("Passphrase", hide_input=True)
        if confirm:
            again = typer.prompt("Confirm passphrase", hide_input=True)
            if value != again:
                typer.echo("Passphrases do not match.", err=True)
                raise typer.Exit(code=1)
        return value or None
    if passphrase:
        return passphrase
    env = os.environ.get(_ENV_PASSPHRASE)
    return env or None


def _poll_status(cfg, mode, *, restore: bool) -> int:
    """Poll a status file endpoint to a terminal phase, printing each phase.

    Tolerates connection errors while the server restarts (the restore path
    drops the connection at the ``restart`` phase and reconnects afterwards).
    Returns process exit code (0 ok / 1 failed).
    """
    import asyncio
    import time

    import httpx

    from app.cli.client._base import Client
    from app.cli.client import backup as api

    getter = api.restore_status if restore else api.status
    deadline = time.monotonic() + (900 if restore else 300)
    seen: tuple | None = None

    async def _once() -> dict | None:
        async with Client(cfg) as client:
            return await getter(client)

    while time.monotonic() < deadline:
        try:
            st = asyncio.run(_once())
        except httpx.RequestError:
            # Server likely restarting mid-restore — keep polling.
            time.sleep(1.5)
            continue
        if st:
            fp = (st.get("phase"), len(st.get("log_tail") or []))
            if fp != seen:
                seen = fp
                phase = st.get("phase")
                typer.echo(f"  [{phase}]")
                if phase in _TERMINAL_PHASES:
                    if phase == "failed":
                        typer.echo(f"Failed: {st.get('error') or 'unknown error'}", err=True)
                        return 1
                    detail = st.get("detail") or {}
                    if detail:
                        typer.echo(f"  {detail}")
                    return 0
        time.sleep(1.5)
    typer.echo("Timed out waiting for completion; check `cremind backup status`.", err=True)
    return 1


# ── create ─────────────────────────────────────────────────────────────────


@backup_app.command("create")
@graceful_errors
def backup_create(
    ctx: typer.Context,
    offline: bool = typer.Option(False, "--offline", help="Run against the local system directly (service stopped)."),
    to: Optional[Path] = typer.Option(None, "--to", help="Output path (offline only). Defaults to backups/<name>."),
    passphrase: Optional[str] = typer.Option(None, "--passphrase", help="Encrypt the archive with this passphrase."),
    passphrase_prompt: bool = typer.Option(False, "--passphrase-prompt", help="Prompt for an encryption passphrase."),
) -> None:
    """Create a full-system backup archive."""
    pw = _resolve_passphrase(passphrase, passphrase_prompt, confirm=True)

    if offline:
        from app.backup.engine import BackupOptions, create_backup
        from app.backup.manifest import BackupError

        try:
            result = create_backup(BackupOptions(dest=to, passphrase=pw))
        except BackupError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(code=1) from e
        typer.echo(f"Created {result.path}")
        typer.echo(f"  files={result.file_count} bytes={result.bytes_written} encrypted={bool(pw)}")
        if result.skipped:
            typer.echo(f"  skipped {len(result.skipped)} entr(ies) (symlinks/unreadable)")
        return

    import asyncio

    from app.cli.client._base import Client
    from app.cli.client import backup as api
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    mode = ctx.obj["mode"]
    cfg.require_token()

    async def _kick() -> None:
        async with Client(cfg) as client:
            await api.create(client, pw)

    asyncio.run(_kick())
    typer.echo("Backup started:")
    code = _poll_status(cfg, mode, restore=False)
    raise typer.Exit(code=code)


# ── list / download / upload / delete ─────────────────────────────────────


@backup_app.command("list")
@graceful_errors
def backup_list(ctx: typer.Context) -> None:
    """List available backup archives."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client import backup as api
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run():
        async with Client(cfg) as client:
            return await api.list_backups(client)

    out = asyncio.run(_run())
    if mode.json:
        print_json(out)
        return
    rows = (out or {}).get("backups") or []
    if not rows:
        typer.echo("No backups found.")
        return
    table = Table(mode, "NAME", "CREATED", "SIZE", "PROVIDER", "ENCRYPTED")
    for r in rows:
        man = r.get("manifest") or {}
        table.add_row(
            r.get("name"),
            man.get("created_at") or "-",
            f"{r.get('size_bytes', 0):,}",
            man.get("db_provider") or "-",
            "yes" if man.get("encrypted") else "no",
        )
    table.render()


@backup_app.command("download")
@graceful_errors
def backup_download(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Backup archive name (see `cremind backup list`)."),
    to: Optional[Path] = typer.Option(None, "--to", help="Destination file (default: ./<name>)."),
) -> None:
    """Download a backup archive to a local file."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client import backup as api
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()
    dest = Path(to) if to else Path(os.getcwd()) / name

    async def _run():
        async with Client(cfg) as client, open(dest, "wb") as sink:
            await api.download(client, name, sink)

    asyncio.run(_run())
    typer.echo(f"Saved {dest}")


@backup_app.command("upload")
@graceful_errors
def backup_upload(
    ctx: typer.Context,
    path: Path = typer.Argument(..., help="Path to a .cremind-backup archive to upload."),
) -> None:
    """Upload a backup archive to the server's backups directory."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client import backup as api
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run():
        async with Client(cfg) as client:
            return await api.upload(client, str(path))

    out = asyncio.run(_run())
    typer.echo(f"Uploaded as {out.get('name')} ({out.get('size_bytes', 0):,} bytes)")


@backup_app.command("delete")
@graceful_errors
def backup_delete(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Backup archive name to delete."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Delete a backup archive from the server."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client import backup as api
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()
    if not yes and not typer.confirm(f"Delete backup {name}?", default=False):
        typer.echo("Aborted.")
        raise typer.Exit(code=1)

    async def _run():
        async with Client(cfg) as client:
            await api.delete(client, name)

    asyncio.run(_run())
    typer.echo(f"Deleted {name}")


# ── restore ────────────────────────────────────────────────────────────────


@backup_app.command("restore")
@graceful_errors
def backup_restore(
    ctx: typer.Context,
    src: str = typer.Argument(..., help="Local archive path (uploaded first) or a server-side backup name."),
    offline: bool = typer.Option(False, "--offline", help="Restore the local system directly (service stopped)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
    passphrase: Optional[str] = typer.Option(None, "--passphrase", help="Passphrase for an encrypted archive."),
    passphrase_prompt: bool = typer.Option(False, "--passphrase-prompt", help="Prompt for the archive passphrase."),
) -> None:
    """Restore the system from a backup archive.

    Destructive: replaces ALL current data. Online restore restarts the server;
    offline restore requires the service to be stopped. After a restore,
    previously-activated events re-arm and autostart processes relaunch on boot
    — any that can't run in the new environment surface as warnings
    (`cremind backup report`).
    """
    pw = _resolve_passphrase(passphrase, passphrase_prompt, confirm=False)

    if offline:
        from app.backup.engine import restore_backup
        from app.backup.manifest import BackupError
        from app.config.settings import BaseConfig

        if not yes and not typer.confirm(
            f"Restore from {src}? This OVERWRITES all current data.", default=False
        ):
            typer.echo("Aborted.")
            raise typer.Exit(code=1)
        try:
            report = restore_backup(Path(src), pw, target_system_dir=BaseConfig.CREMIND_SYSTEM_DIR)
        except BackupError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(code=1) from e
        typer.echo(f"Restored: db_rows={sum(report.db_row_counts.values())} files={report.file_count}")
        for w in report.warnings:
            typer.echo(f"  warning: {w}", err=True)
        typer.echo(
            "Start the service to finish: previously-activated events and "
            "autostart processes re-arm on boot. Run `cremind backup report` after."
        )
        return

    import asyncio

    from app.cli.client._base import Client
    from app.cli.client import backup as api
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    mode = ctx.obj["mode"]
    cfg.require_token()

    if not yes and not typer.confirm(
        f"Restore from {src}? This OVERWRITES all current data and restarts the server.",
        default=False,
    ):
        typer.echo("Aborted.")
        raise typer.Exit(code=1)

    # A local file path → upload it first; a bare name → assume it's server-side.
    name = src
    local = Path(src)
    if local.is_file():
        async def _upload():
            async with Client(cfg) as client:
                return await api.upload(client, str(local))

        up = asyncio.run(_upload())
        name = up.get("name")
        typer.echo(f"Uploaded as {name}")

    async def _kick():
        async with Client(cfg) as client:
            return await api.restore(client, name, pw)

    resp = asyncio.run(_kick())
    typer.echo(f"Restore started ({resp.get('mode')}):")
    code = _poll_status(cfg, mode, restore=True)
    raise typer.Exit(code=code)


# ── status / report ──────────────────────────────────────────────────────


@backup_app.command("status")
@graceful_errors
def backup_status_cmd(ctx: typer.Context) -> None:
    """Show current backup and restore status."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client import backup as api
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_map

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run():
        async with Client(cfg) as client:
            return await api.status(client), await api.restore_status(client)

    bstat, rstat = asyncio.run(_run())
    if mode.json:
        print_json({"backup": bstat, "restore": rstat})
        return
    typer.echo("Backup:")
    print_map({"phase": bstat.get("phase"), "error": bstat.get("error"), **(bstat.get("detail") or {})})
    typer.echo("Restore:")
    print_map({"phase": rstat.get("phase"), "error": rstat.get("error"), **(rstat.get("detail") or {})})


@backup_app.command("report")
@graceful_errors
def backup_report(
    ctx: typer.Context,
    ack: bool = typer.Option(False, "--ack", help="Mark the restore report as acknowledged."),
) -> None:
    """Show the last restore report and post-restore warnings."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client import backup as api
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run():
        async with Client(cfg) as client:
            if ack:
                await api.ack_report(client)
            return await api.restore_report(client)

    out = asyncio.run(_run())
    if mode.json:
        print_json(out)
        return
    report = (out or {}).get("report")
    warnings = (out or {}).get("warnings") or {}
    if not report:
        typer.echo("No restore report.")
    else:
        typer.echo(f"Last restore: {'OK' if report.get('ok') else 'FAILED'}")
        if report.get("source"):
            typer.echo(f"  source: {report['source']}")
        for w in report.get("warnings") or []:
            typer.echo(f"  warning: {w}")
    af = warnings.get("autostart_failures") or []
    dc = warnings.get("disabled_channels") or []
    if af:
        typer.echo(f"\nAutostart processes that failed to start ({len(af)}):")
        for r in af:
            typer.echo(f"  - {r.get('command')}  ({r.get('error')})")
    if dc:
        typer.echo(f"\nChannels disabled at boot ({len(dc)}):")
        for r in dc:
            typer.echo(f"  - {r.get('channel_type')}  ({r.get('error')})")
    if ack:
        typer.echo("\nAcknowledged.")
