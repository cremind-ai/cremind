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
from app.cli.commands.calendar import calendar_app
from app.cli.commands.channels import channels_app
from app.cli.commands.config import config_app
from app.cli.commands.conv import conv_app
from app.cli.commands.db import db_app
from app.cli.commands.event_runs import event_runs_app
from app.cli.commands.file_watchers import file_watchers_app
from app.cli.commands.files import files_app
from app.cli.commands.llm import llm_app
from app.cli.commands.processes import proc_app
from app.cli.commands.profile import profile_app
from app.cli.commands.setup import setup_app
from app.cli.commands.skill_events import skill_events_app
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
) -> None:
    """cremind — lightweight client for the Cremind server."""
    from app.cli.config import ConfigError, load_from_env
    from app.cli.output import OutputMode

    try:
        cfg = load_from_env(server=server, token=token, json_flag=json_output)
    except ConfigError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e

    ctx.ensure_object(dict)
    ctx.obj["cfg"] = cfg
    ctx.obj["mode"] = OutputMode.from_config(cfg)


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
