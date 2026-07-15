"""Entry point for the `cremind` CLI.

Registered in pyproject.toml as `cremind = "app.cli.main:app"`.

Discipline: this module and any module under `app/cli/` MUST NOT import from
`app.server`, `app.api`, `app.tools`, `app.agent`, `app.skills`, `app.events`,
`app.documents`, `app.channels`, `app.databases`, or `app.storage` at module
top level. The only exception is `app/cli/commands/serve.py`, which imports
`app.server.main` inside the function body. This keeps the slim install
(`pip install cremind`) from needing server dependencies.
"""

from __future__ import annotations

import sys
from typing import Optional

import typer

# Force UTF-8 on stdout/stderr so half-block QR glyphs and other non-ASCII
# output work regardless of the user's console code page (Windows defaults
# to cp1252 on Python <3.15, which mangles ▀▄█ etc.). The Go CLI writes raw
# UTF-8 bytes, so this just keeps parity.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

from app.cli.commands import chat as chat_cmd
from app.cli.commands import me as me_cmd
from app.cli.commands import serve as serve_cmd
from app.cli.commands import system_vars as system_vars_cmd
from app.cli.commands import usage as usage_cmd
from app.cli.commands.agents import agents_app
from app.cli.commands.backup import backup_app
from app.cli.commands.blueprint import blueprint_app
from app.cli.commands.calendar import calendar_app
from app.cli.commands.channels import channels_app
from app.cli.commands.clean import clean_app
from app.cli.commands.config import config_app
from app.cli.commands.conv import conv_app
from app.cli.commands.db import db_app
from app.cli.commands.embedding import embedding_app
from app.cli.commands.event_runs import event_runs_app
from app.cli.commands.features import features_app
from app.cli.commands.file_watchers import file_watchers_app
from app.cli.commands.files import files_app
from app.cli.commands.llm import llm_app
from app.cli.commands.logs import logs_app
from app.cli.commands.processes import proc_app
from app.cli.commands.profile import profile_app
from app.cli.commands.server import server_app
from app.cli.commands.setup import setup_app
from app.cli.commands.skill_events import skill_events_app
from app.cli.commands.skills import skills_app
from app.cli.commands.tools import tools_app
from app.cli.commands.upgrade import upgrade_app


app = typer.Typer(
    name="cremind",
    help="Cremind command-line interface.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@app.callback()
def _root(
    ctx: typer.Context,
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Output JSON instead of human-readable tables.",
    ),
    server: Optional[str] = typer.Option(
        None,
        "--server",
        envvar="CREMIND_SERVER",
        help="Server base URL (default: http://localhost:1112).",
    ),
    token: Optional[str] = typer.Option(
        None,
        "--token",
        envvar="CREMIND_TOKEN",
        help="JWT bearer token for the Cremind server.",
    ),
    profile: Optional[str] = typer.Option(
        None,
        "--profile",
        "-p",
        envvar="CREMIND_PROFILE",
        help="Act as this profile (remembered for this terminal). "
        "Omit to pick interactively on first use.",
    ),
) -> None:
    """cremind — lightweight client for the Cremind server."""
    from app.cli.config import ConfigError, load_from_env
    from app.cli.output import OutputMode

    try:
        cfg = load_from_env(server=server, token=token, json_flag=json_output)
    except ConfigError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e

    # Auto-resolve a token from the on-disk per-profile token files when the
    # user hasn't supplied one via --token / CREMIND_TOKEN. This lets the CLI be
    # used without exporting a JWT: on first use in a terminal we prompt for a
    # profile (TUI picker) and remember it. `exec_shell` injects CREMIND_TOKEN,
    # so that path keeps winning here (cfg.token is set) and is untouched.
    if not cfg.token and _should_resolve_profile(ctx):
        cfg = _resolve_token(cfg, profile)

    ctx.ensure_object(dict)
    ctx.obj["cfg"] = cfg
    ctx.obj["mode"] = OutputMode.from_config(cfg)


# Top-level commands that never call the server — never prompt for a profile.
_TOKEN_FREE_COMMANDS = {"version", "setup", "serve"}
# `profile` subcommands that manage the local session and need no token.
_PROFILE_SESSION_SUBCOMMANDS = {"use", "which", "clear"}


def _should_resolve_profile(ctx: typer.Context) -> bool:
    """Whether the invoked command warrants resolving a profile/token.

    Skips shell-completion, bare `cremind`/`--help`, the token-free top-level
    commands, and the local `profile use/which/clear` subcommands (so listing or
    clearing the session never triggers the picker).

    The deep subcommand under a group isn't available via the Click API at the
    root-callback stage (its context isn't built yet), so the `profile` check
    reads `sys.argv` — authoritative for the `cremind` entry point.
    """
    if ctx.resilient_parsing:  # shell completion
        return False
    sub = ctx.invoked_subcommand
    if sub is None:  # bare `cremind` → help
        return False
    if sub in _TOKEN_FREE_COMMANDS:
        return False
    argv = sys.argv[1:]
    if "-h" in argv or "--help" in argv:
        return False
    if sub == "profile" and _profile_subcommand(argv) in _PROFILE_SESSION_SUBCOMMANDS:
        return False
    return True


def _profile_subcommand(argv: list[str]) -> Optional[str]:
    """The first positional token after `profile` in `argv` (its subcommand)."""
    try:
        idx = argv.index("profile")
    except ValueError:
        return None
    for tok in argv[idx + 1 :]:
        if not tok.startswith("-"):
            return tok
    return None


def _resolve_token(cfg, explicit_profile: Optional[str]):
    """Fill in `cfg.token` from a resolved profile's on-disk token file."""
    import dataclasses

    from app.cli import session

    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    try:
        chosen = session.resolve_profile(explicit_profile, interactive=interactive)
    except (KeyboardInterrupt, EOFError):
        raise typer.Exit(code=130)
    if not chosen:
        return cfg  # leave token empty; the command's require_token() explains
    tok = session.read_token(chosen)
    if not tok:
        if explicit_profile:
            typer.echo(
                f"profile '{chosen}' has no token file under "
                f"{session.tokens_dir()} — run `cremind setup` or check the name.",
                err=True,
            )
            raise typer.Exit(code=1)
        return cfg
    return dataclasses.replace(cfg, token=tok)


@app.command()
def version() -> None:
    """Print the installed Cremind version."""
    from app.__version__ import __version__

    typer.echo(f"cremind {__version__}")


app.command(
    "me",
    help="Show identity info for the current CREMIND_TOKEN.",
)(me_cmd.me)

app.command(
    "system-vars",
    help="List the env vars Cremind injects into exec_shell subprocesses.",
)(system_vars_cmd.system_vars)

app.command(
    "usage",
    help="Show the token-usage & estimated-cost summary.",
)(usage_cmd.usage)

app.add_typer(profile_app, name="profile")
app.add_typer(conv_app, name="conv")
app.add_typer(tools_app, name="tools")
app.add_typer(llm_app, name="llm")
app.add_typer(agents_app, name="agents")
app.add_typer(channels_app, name="channels")
app.add_typer(clean_app, name="clean")
app.add_typer(files_app, name="files")
app.add_typer(calendar_app, name="calendar")
app.add_typer(file_watchers_app, name="file-watchers")
app.add_typer(skill_events_app, name="skill-events")
app.add_typer(event_runs_app, name="event-runs")
app.add_typer(proc_app, name="proc")
app.add_typer(setup_app, name="setup")
app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")
app.add_typer(upgrade_app, name="upgrade")
app.add_typer(backup_app, name="backup")
app.add_typer(blueprint_app, name="blueprint")
app.add_typer(skills_app, name="skills")
app.add_typer(features_app, name="features")
app.add_typer(embedding_app, name="embedding")
app.add_typer(logs_app, name="logs")
app.add_typer(server_app, name="server")

app.command(
    "chat",
    help="Open an interactive chat REPL with streamed thinking.",
)(chat_cmd.chat)

app.command(
    "serve",
    help="Start the Cremind HTTP server in-process.",
)(serve_cmd.serve)


if __name__ == "__main__":
    app()
