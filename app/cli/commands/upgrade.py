"""`cremind upgrade` — full backup → install → migrate → health flow.

Wraps :mod:`app.upgrade.runner`. Two subcommands:

  upgrade check      Look up the latest release; report whether one is
                     available without making any changes.
  upgrade apply      Run the upgrade flow. Prompts for confirmation
                     unless ``--yes`` is passed.
"""

from __future__ import annotations

import typer


upgrade_app = typer.Typer(
    name="upgrade",
    help="Check for and apply Cremind upgrades.",
    no_args_is_help=False,
    invoke_without_command=True,
)


def _print_event(event) -> None:
    """Render an UpgradeEvent for the CLI in a single line."""
    prefix = "✓ " if event.ok else "✗ "
    typer.echo(f"{prefix}[{event.kind}] {event.message}")


def _print_channel_banner() -> None:
    """One line at the top of every upgrade subcommand.

    Surfaces the active channel so a user noticing they're on the
    wrong feed can abort before triggering a state-mutating apply.
    """
    from app.upgrade.channel import get_channel

    channel = get_channel()
    if channel == "test":
        typer.echo("Channel: test (Test PyPI)")
    else:
        typer.echo("Channel: production")


@upgrade_app.callback()
def _root(
    ctx: typer.Context,
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt (used by `cremind upgrade -y`).",
    ),
    target: str | None = typer.Option(
        None,
        "--target",
        help=(
            "Target version. On production/dev it must equal the latest "
            "release (safety). On the test channel it may be any published "
            "RC — including one older than the current install — so you can "
            "switch between PR release candidates."
        ),
    ),
) -> None:
    """Default action: ``cremind upgrade`` with no subcommand runs ``apply``.

    Mirrors what most package-manager-style CLIs do (``brew upgrade``,
    ``cargo install --upgrade``) so users don't have to remember the
    explicit subcommand for the common case. The ``--yes`` / ``-y`` and
    ``--target`` flags are accepted at this level too so the documented
    one-liner ``cremind upgrade -y`` works without users having to know
    they're really invoking the ``apply`` subcommand under the hood.
    """
    if ctx.invoked_subcommand is not None:
        return
    ctx.invoke(upgrade_apply, yes=yes, target=target)


@upgrade_app.command("check")
def upgrade_check() -> None:
    """Check whether a newer version is available without changing anything."""
    from app.upgrade import runner

    _print_channel_banner()
    release, status = runner.check(callback=_print_event)
    if status == "available" and release is not None:
        typer.echo(f"\nRun `cremind upgrade apply` to install {release.version}.")
    raise typer.Exit(code=0 if status in ("up_to_date", "available") else 1)


@upgrade_app.command("apply")
def upgrade_apply(
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt.",
    ),
    target: str | None = typer.Option(
        None,
        "--target",
        help=(
            "Target version. On production/dev it must equal the latest "
            "release (safety). On the test channel it may be any published "
            "RC — including one older than the current install — so you can "
            "switch between PR release candidates."
        ),
    ),
) -> None:
    """Apply the upgrade. Stops on rollback if any step fails."""
    from app.upgrade import runner

    _print_channel_banner()

    def _confirm(release) -> bool:
        if yes:
            return True
        return typer.confirm(
            f"\nUpgrade to {release.version}? "
            f"This will take a backup, install the new package, and run migrations.",
            default=True,
        )

    success = runner.apply(target_version=target, callback=_print_event, confirm=_confirm)
    raise typer.Exit(code=0 if success else 1)
