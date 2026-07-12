"""`cremind clean ...` — wipe or reset ONE profile's data.

Scoped to the profile the presented ``CREMIND_TOKEN`` was minted for (the server
resolves it from the token — there is no ``--profile`` flag). Three modes:

- ``clean components`` — purge a custom subset (chat, memory, usage, automation, …).
- ``clean working``    — preset: clear all runtime data, keep every config/credential.
- ``clean factory``    — preset: also strip config/credentials/customization back to a
  fresh-provisioned baseline (guarded by a type-the-profile-name confirmation).

Irreversible. Distinct from ``cremind backup restore`` (whole-system) and
``cremind conv delete-all`` (only conversations). Import discipline: every ``app.*``
import lives inside a function body (see ``app/cli/main.py``).
"""

from __future__ import annotations

from typing import Optional

import typer

from app.cli.commands._helpers import graceful_errors

clean_app = typer.Typer(
    name="clean",
    help="Wipe or reset the current profile's data (by component or preset).",
    no_args_is_help=True,
)

# Canonical component vocabulary, mirrored from app/reset/components.py. Kept as a
# local copy so the slim `pip install cremind` CLI never imports server code.
# (api_key, flag) — order defines both the flags and the output-table rows.
_COMPONENTS: list[tuple[str, str]] = [
    ("conversations", "--conversations"),
    ("memory", "--memory"),
    ("uploads", "--uploads"),
    ("plans", "--plans"),
    ("usage", "--usage"),
    ("event_runs", "--event-runs"),
    ("processes", "--processes"),
    ("schedules", "--schedules"),
    ("file_watchers", "--file-watchers"),
    ("skill_events", "--skill-events"),
    ("channels", "--channels"),
    ("llm_config", "--llm-config"),
    ("oauth_tokens", "--oauth-tokens"),
    ("tool_configs", "--tool-configs"),
    ("skills", "--skills"),
    ("documents", "--documents"),
    ("browser_login", "--browser-login"),
    ("app_settings", "--app-settings"),
]


def _fmt(val) -> str:
    if isinstance(val, bool):
        return "yes" if val else "no"
    if isinstance(val, dict):
        return ", ".join(f"{k}={v}" for k, v in val.items()) or "-"
    return str(val)


def _render(resp: dict, mode) -> None:
    from app.cli.output import Table, print_json

    if mode.json:
        print_json(resp)
        return
    cleaned = resp.get("cleaned") or {}
    if cleaned:
        table = Table(mode, "COMPONENT", "REMOVED")
        for key, val in cleaned.items():
            table.add_row(key, _fmt(val))
        table.render()
    typer.echo(
        f"\ncleaned {resp.get('total', 0)} item(s) [{resp.get('scope')}] "
        f"for profile '{resp.get('profile')}'"
    )
    errors = resp.get("errors") or {}
    if errors:
        typer.echo(f"\n{len(errors)} component(s) had errors:", err=True)
        for key, msg in errors.items():
            typer.echo(f"  - {key}: {msg}", err=True)


def _run_clean(ctx: typer.Context, scope: str, components: Optional[list[str]]) -> None:
    """Shared body: POST /api/clean, render, and exit non-zero on any error."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.clean import clean as clean_api
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    mode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict:
        async with Client(cfg) as client:
            return await clean_api(client, scope, components)

    resp = asyncio.run(_run())
    _render(resp, mode)
    if resp.get("errors"):
        raise typer.Exit(code=1)


@clean_app.command("components")
@graceful_errors
def clean_components(
    ctx: typer.Context,
    conversations: bool = typer.Option(False, "--conversations", help="Chat history + messages."),
    memory: bool = typer.Option(False, "--memory", help="Long-term memory facts."),
    uploads: bool = typer.Option(False, "--uploads", help="Uploaded chat files."),
    plans: bool = typer.Option(False, "--plans", help="Plan-mode files."),
    usage: bool = typer.Option(False, "--usage", help="Token/cost usage records."),
    event_runs: bool = typer.Option(False, "--event-runs", help="Event-run history."),
    processes: bool = typer.Option(False, "--processes", help="Kill running background processes."),
    schedules: bool = typer.Option(False, "--schedules", help="Schedule/calendar rules."),
    file_watchers: bool = typer.Option(False, "--file-watchers", help="File-watcher rules."),
    skill_events: bool = typer.Option(False, "--skill-events", help="Skill-event subscriptions."),
    channels: bool = typer.Option(False, "--channels", help="External channels (keeps 'main')."),
    llm_config: bool = typer.Option(False, "--llm-config", help="LLM providers, keys, model groups."),
    oauth_tokens: bool = typer.Option(False, "--oauth-tokens", help="OAuth tokens."),
    tool_configs: bool = typer.Option(False, "--tool-configs", help="Tools/MCP + their configs."),
    skills: bool = typer.Option(False, "--skills", help="Reset persona + skills to shipped defaults."),
    documents: bool = typer.Option(False, "--documents", help="Documents + their embeddings."),
    browser_login: bool = typer.Option(False, "--browser-login", help="Saved browser login state."),
    app_settings: bool = typer.Option(False, "--app-settings", help="Reset app settings to defaults."),
    all_: bool = typer.Option(False, "--all", help="Select every component (same as factory reset)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Clean a custom subset of the current profile's data. Select at least one
    component (or --all). Irreversible."""
    chosen = {
        "conversations": conversations, "memory": memory, "uploads": uploads,
        "plans": plans, "usage": usage, "event_runs": event_runs,
        "processes": processes, "schedules": schedules, "file_watchers": file_watchers,
        "skill_events": skill_events, "channels": channels,
        "llm_config": llm_config, "oauth_tokens": oauth_tokens,
        "tool_configs": tool_configs, "skills": skills, "documents": documents,
        "browser_login": browser_login, "app_settings": app_settings,
    }
    if all_:
        selected = [key for key, _ in _COMPONENTS]
    else:
        selected = [key for key, _ in _COMPONENTS if chosen[key]]
    if not selected:
        raise typer.BadParameter(
            "select at least one component (e.g. --usage) or --all"
        )
    if not yes and not typer.confirm(
        f"Clean {len(selected)} component(s) for this profile? This cannot be undone.",
        default=False,
    ):
        typer.echo("Aborted.")
        raise typer.Exit(code=1)
    _run_clean(ctx, "custom", selected)


@clean_app.command("working")
@graceful_errors
def clean_working(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Working-data reset: wipe all runtime data (conversations, memory, usage,
    event-runs, schedules, watchers, skill-events, uploads, plans) but keep every
    configuration and credential. Irreversible."""
    if not yes and not typer.confirm(
        "Wipe all runtime data (keeping config) for this profile? This cannot be undone.",
        default=False,
    ):
        typer.echo("Aborted.")
        raise typer.Exit(code=1)
    _run_clean(ctx, "working", None)


@clean_app.command("factory")
@graceful_errors
def clean_factory(
    ctx: typer.Context,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the y/n prompt (the typed name is still required)."),
    confirm_profile: Optional[str] = typer.Option(
        None, "--confirm-profile",
        help="Non-interactive guard: must equal this profile's name.",
    ),
) -> None:
    """Full factory reset: working-data reset PLUS strip all post-setup
    customization (LLM keys, OAuth, tools/MCP, skills, documents, browser login,
    app settings) back to a fresh-provisioned baseline. Keeps the profile itself
    and server-wide config (the Setup Wizard does NOT re-run). Irreversible."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.me import get_me
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _who() -> str:
        async with Client(cfg) as client:
            return (await get_me(client)).profile

    profile = asyncio.run(_who())
    if not profile:
        typer.echo("Could not resolve the current profile from the token.", err=True)
        raise typer.Exit(code=1)

    # Type-to-confirm guard (mirrors the UI). --confirm-profile is the scriptable
    # path; interactively we prompt for the name. --yes only skips the y/n, never
    # the typed name.
    if confirm_profile is not None:
        if confirm_profile != profile:
            raise typer.BadParameter(
                f"--confirm-profile must equal '{profile}' to factory-reset it"
            )
    else:
        typed = typer.prompt(f"Type the profile name '{profile}' to confirm factory reset")
        if typed != profile:
            typer.echo("Name did not match. Aborted.", err=True)
            raise typer.Exit(code=1)
        if not yes and not typer.confirm(
            f"Factory-reset profile '{profile}' to a fresh baseline?", default=False
        ):
            typer.echo("Aborted.")
            raise typer.Exit(code=1)

    _run_clean(ctx, "factory", None)
