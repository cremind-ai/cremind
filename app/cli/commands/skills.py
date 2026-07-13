"""`cremind skills ...` — import and remove agent skills.

Mirrors the Tools & Skills settings page's skill lifecycle: import a skill from
a local archive, a public GitHub repo, or a Cremind Hub link/name, and delete
an external skill (or reset a built-in one to its shipped default).

Skills are *listed* and *configured* via `cremind tools` (a skill shows up as a
tool); there is deliberately no `skills list` here — this group is install /
uninstall only.
"""

from __future__ import annotations

import sys
from typing import Any

import typer

from app.cli.commands._helpers import graceful_errors


skills_app = typer.Typer(
    name="skills",
    help="Import and remove agent skills.",
    no_args_is_help=True,
)
import_app = typer.Typer(
    name="import",
    help="Import skills from an archive, a GitHub repo, or Cremind Hub.",
    no_args_is_help=True,
)
skills_app.add_typer(import_app, name="import")


def _render_import(out: dict[str, Any], mode: Any) -> None:
    """Render an import result: installed names on stdout, skips on stderr."""
    from app.cli.output import print_json

    if mode.json:
        print_json(out)
        return
    installed = out.get("installed") or []
    if installed:
        sys.stdout.write("installed: " + ", ".join(str(x) for x in installed) + "\n")
    else:
        sys.stdout.write("no skills installed\n")
    for skip in out.get("skipped") or []:
        if isinstance(skip, dict):
            sys.stderr.write(f"skipped {skip.get('name')}: {skip.get('reason')}\n")


@import_app.command("archive")
@graceful_errors
def skills_import_archive(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Path to a skill archive (.zip / .tar.gz)."),
) -> None:
    """Install skills from a local archive file."""
    import asyncio
    import os

    from app.cli.client._base import Client
    from app.cli.client.skills import import_archive
    from app.cli.config import Config
    from app.cli.output import OutputMode

    if not os.path.isfile(path):
        typer.echo(f"not a file: {path}", err=True)
        raise typer.Exit(code=1)
    with open(path, "rb") as fh:
        data = fh.read()
    filename = os.path.basename(path) or "skill-archive"

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await import_archive(client, filename, data)

    _render_import(asyncio.run(_run()), mode)


@import_app.command("github")
@graceful_errors
def skills_import_github(
    ctx: typer.Context,
    repo: str = typer.Argument(
        ..., help="GitHub repo URL or owner/repo (e.g. https://github.com/acme/skills).",
    ),
) -> None:
    """Install skills from a public GitHub repository."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.skills import import_github
    from app.cli.config import Config
    from app.cli.output import OutputMode

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await import_github(client, repo)

    _render_import(asyncio.run(_run()), mode)


@import_app.command("hub")
@graceful_errors
def skills_import_hub(
    ctx: typer.Context,
    ref: str = typer.Argument(
        ..., help="Cremind Hub skill link or bare name "
                  "(e.g. https://hub.cremind.io/skills/foo or foo).",
    ),
) -> None:
    """Install a skill from a Cremind Hub link or name."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.skills import import_hub
    from app.cli.config import Config
    from app.cli.output import OutputMode

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await import_hub(client, ref)

    _render_import(asyncio.run(_run()), mode)


@skills_app.command("delete")
@graceful_errors
def skills_delete(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Skill tool id (from `cremind tools list`)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Delete an external skill, or reset a built-in skill to its default.

    Built-in skills can't be removed — deleting one restores its shipped copy
    (`reset`). External (imported) skills are deleted permanently.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.skills import delete_skill
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    if not yes and not typer.confirm(
        f"Delete skill '{tool_id}'? (a built-in skill resets to its default)",
        default=False,
    ):
        raise typer.Exit(code=1)

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await delete_skill(client, tool_id)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
        return
    sys.stdout.write("reset to default\n" if out.get("reset") else "deleted\n")
