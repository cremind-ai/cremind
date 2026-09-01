"""`cremind boot ...` — start Cremind at login and keep it running.

Like ``cremind tls`` and ``cremind db``, this never goes through the HTTP API:
it writes a unit file and talks to the operating system's service manager. That
is deliberate — the command exists for the case where there is no running
server to ask, and the whole point is to arrange for one. So there is no client
wrapper in ``app/cli/client/``; there is nothing to call.

Subcommands:

  boot enable    Register the service (systemd / launchd / Scheduled Task).
  boot disable   Remove it, stopping whatever it was running.
  boot status    Report registration, supervisor and server state.

The registration is per OS user, not per Cremind profile: one ``cremind serve``
process serves every profile, so there is exactly one service.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer


boot_app = typer.Typer(
    name="boot",
    help="Start Cremind automatically at login/boot, and restart it if it stops.",
    no_args_is_help=True,
)


def _default_exec_path() -> Path:
    """``<system dir>/bin/cremind`` — the installer-generated shim.

    Always the shim, never ``venv/bin/cremind``: ``app/config/settings.py``
    resolves its dotenv path relative to the working directory, so only the
    shim loads the install's ``~/.cremind/.env``. A service that skipped it
    would boot with default HOST/PORT and no ``CREMIND_SSL`` — the exact
    failure the shim was written to prevent. It also keeps dev installs
    working, where the shim points into the repo's ``.venv``.
    """
    from app.cli import session

    name = "cremind.cmd" if sys.platform == "win32" else "cremind"
    return session.system_dir() / "bin" / name


def _system_dir() -> Path:
    from app.cli import session

    return session.system_dir()


def _fail(message: str, *extra: str) -> None:
    """Print an error (plus any follow-up lines) and exit non-zero."""
    typer.secho(message, fg="red", err=True)
    for line in extra:
        typer.secho(line, err=True)
    raise typer.Exit(1)


def _require_supported(plan) -> None:
    if not plan.supported:
        _fail(plan.reason or "A boot service is not supported here.")


def _print_plan(plan, mode) -> None:
    """``--print-only``: show every file and command, and run nothing."""
    from app.cli.output import print_json
    from app.system.boot_service import render_command

    if mode.json:
        print_json({
            "kind": plan.kind,
            "unit_path": plan.unit_path,
            "artifacts": [
                {"path": a.path, "content": a.content} for a in plan.artifacts
            ],
            "commands": plan.commands,
            "best_effort": plan.best_effort,
            "remove_paths": plan.remove_paths,
        })
        return
    for artifact in plan.artifacts:
        typer.echo(f"# {artifact.path}")
        typer.echo(artifact.content.rstrip("\n"))
        typer.echo("")
    for argv in list(plan.best_effort) + list(plan.commands) + list(plan.cleanup):
        typer.echo(render_command(argv))
    for target in plan.remove_paths:
        typer.echo(f"# remove {target}")


def _warn_all(warnings: list[str]) -> None:
    for warning in warnings:
        typer.secho(f"warning: {warning}", fg="yellow", err=True)


@boot_app.command("enable")
def boot_enable(
    ctx: typer.Context,
    exec_path: Optional[str] = typer.Option(
        None,
        "--exec",
        help="Command the service runs (default: <CREMIND_SYSTEM_DIR>/bin/cremind).",
    ),
    start: bool = typer.Option(
        True,
        "--start/--no-start",
        help="Also start the service now. --no-start registers it for the "
             "next login, which is what you want when a server is already "
             "running on the port.",
    ),
    print_only: bool = typer.Option(
        False,
        "--print-only",
        help="Print the unit file and commands for this OS without running anything.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt.",
    ),
) -> None:
    """Register a service that starts Cremind at login and restarts it if it stops."""
    from app.cli.output import print_json
    from app.system import boot_service

    mode = ctx.obj["mode"]
    target = Path(exec_path) if exec_path else _default_exec_path()
    if not exec_path and not target.is_file():
        _fail(
            f"No Cremind launcher at {target}",
            "That file is written by the installer. If Cremind was installed "
            "somewhere else, point --exec at its `cremind` shim; if this is a "
            "broken install, re-run the installer.",
        )

    refusal = boot_service.boot_environment_error()
    if refusal:
        _fail(refusal)

    user_id = boot_service.current_user_id() if sys.platform == "win32" else None
    plan = boot_service.enable_plan(
        str(target),
        system_dir=_system_dir(),
        start_now=start,
        user_id=user_id,
    )
    _require_supported(plan)

    if print_only:
        _print_plan(plan, mode)
        return

    if mode.json and not yes:
        _fail(
            "--json needs --yes (or --print-only): registering a service asks "
            "for confirmation, which has no meaning in JSON mode."
        )

    if not yes:
        typer.echo(f"About to register a boot service ({plan.kind}):")
        typer.echo(f"  Unit     {plan.unit_path}")
        typer.echo(f"  Runs     {target} serve")
        typer.echo(f"  Starts   {'now and at every login' if start else 'at next login'}")
        if not typer.confirm("Continue?", default=False):
            raise typer.Exit(1)

    ok, error, warnings = boot_service.run_boot_plan(plan)
    _warn_all(warnings)
    if not ok:
        _fail(f"Could not register the boot service: {error}")

    if mode.json:
        print_json({
            "enabled": True,
            "kind": plan.kind,
            "unit_path": plan.unit_path,
            "started": start,
            "warnings": warnings,
        })
        return
    typer.secho(f"Boot service registered ({plan.unit_path}).", fg="green")
    if start:
        typer.echo("Cremind is starting now and will start at every login.")
    else:
        typer.echo("Cremind will start at your next login.")
    typer.echo(_logs_hint(plan.kind))
    typer.echo("Undo with: cremind boot disable")


def _logs_hint(kind: str | None) -> str:
    if kind == "systemd":
        return "Logs: journalctl --user -u cremind -f (and ~/.cremind/server.log)"
    return "Logs: ~/.cremind/server.log"


@boot_app.command("disable")
def boot_disable(
    ctx: typer.Context,
    print_only: bool = typer.Option(
        False,
        "--print-only",
        help="Print what would be removed without running anything.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt.",
    ),
) -> None:
    """Remove the boot service and stop the server it was running."""
    from app.cli.output import print_json
    from app.system import boot_service

    mode = ctx.obj["mode"]
    system_dir = _system_dir()
    plan = boot_service.disable_plan(system_dir=system_dir)
    _require_supported(plan)

    if print_only:
        _print_plan(plan, mode)
        return

    if mode.json and not yes:
        _fail(
            "--json needs --yes (or --print-only): removing a service asks for "
            "confirmation, which has no meaning in JSON mode."
        )

    # Asked before the teardown for two reasons: it decides the wording below,
    # and it decides whether the warnings are worth showing. With nothing
    # registered, every teardown command fails with "not found" — noise that
    # reads like a problem. The plan still runs, to mop up stray artifacts.
    before = boot_service.boot_status(system_dir=system_dir)
    was_registered = bool(before.registered or before.active)

    if not yes:
        typer.echo(f"About to remove the boot service ({plan.kind}):")
        typer.echo(f"  Unit     {plan.unit_path}")
        typer.echo("Cremind will stop, and will not start at the next login.")
        if not typer.confirm("Continue?", default=False):
            raise typer.Exit(1)

    ok, error, warnings = boot_service.run_boot_plan(plan)
    if was_registered:
        _warn_all(warnings)
    if not ok:
        _fail(f"Could not remove the boot service: {error}")

    # Exit codes lie here on purpose — every teardown command is best-effort,
    # because stopping a service that is not running is an error to the tool
    # and a success to us. So confirm against what the OS says now.
    if boot_service.boot_status(system_dir=system_dir).registered:
        _fail(
            f"The boot service is still registered at {plan.unit_path}.",
            *(f"Last error: {detail}" for detail in warnings[:1]),
        )

    if mode.json:
        print_json({
            "enabled": False,
            "kind": plan.kind,
            "unit_path": plan.unit_path,
            "was_registered": was_registered,
            "warnings": warnings if was_registered else [],
        })
        return
    if not was_registered:
        typer.secho("No boot service was registered.", fg="green")
        return
    typer.secho("Boot service removed.", fg="green")
    typer.echo("Start Cremind by hand with: cremind serve")


@boot_app.command("status")
def boot_status(ctx: typer.Context) -> None:
    """Show whether the boot service is registered, and what it is running."""
    from app.cli.output import print_json, print_kv
    from app.system import boot_service

    mode = ctx.obj["mode"]
    status = boot_service.boot_status(system_dir=_system_dir())

    if mode.json:
        print_json({
            "supported": status.supported,
            "kind": status.kind,
            "registered": status.registered,
            "active": status.active,
            "unit_path": status.unit_path,
            "linger": status.linger,
            "server_pid": status.server_pid,
            "detail": status.detail,
            "reason": status.reason,
        })
        return

    if not status.supported:
        typer.echo(status.reason or "A boot service is not supported here.")
        return

    rows = [
        ("Registered", _tri(status.registered)),
        ("Running", _tri(status.active)),
        ("Unit", status.unit_path or ""),
    ]
    if status.linger is not None:
        # Only meaningful on systemd, and only worth surfacing because the
        # failure it predicts (server dies at logout) looks like a bug.
        rows.append(
            ("Survives logout", "yes" if status.linger else "no (enable-linger is off)")
        )
    rows.append(
        ("Server", f"pid {status.server_pid}" if status.server_pid else "not running")
    )
    if status.detail:
        rows.append(("Detail", status.detail))
    print_kv(rows)

    if status.registered and status.linger is False:
        typer.secho(
            "Cremind will stop when you log out. Run: sudo loginctl "
            "enable-linger $USER",
            fg="yellow",
            err=True,
        )


def _tri(value: bool | None) -> str:
    """Render a tri-state honestly — unknown is not the same as no."""
    if value is None:
        return "unknown"
    return "yes" if value else "no"
