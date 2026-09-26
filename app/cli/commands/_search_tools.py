"""The body shared by ``cremind conv search-tools`` and ``cremind group search-tools``.

Both show or replace a selection of search sources — a conversation's, or a
group room's shared one — through the same state body, so they share one
implementation here and differ only in the path they talk to.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

import typer

# The CLI cannot tell whether a response is running right now, so it says what
# is true either way (the web composer, which knows, shows the fuller notice
# "Saved for the next response. The current response keeps its existing search
# tools." only while one is).
PENDING_NOTICE = (
    "Saved for the next response (a response already running keeps its existing search tools)."
)


def resolve_selection(
    enable: Optional[list[str]], all_: bool, none_: bool,
) -> tuple[bool, Optional[list[str]]]:
    """``(is_write, enabled)`` from the flags. ``--enable`` (repeatable)
    replaces the selection, ``--all`` restores the defaults (every source),
    ``--none`` turns every source off; no flag just shows the state. The flags
    are mutually exclusive, checked here before the server checks again."""
    from app.cli.client.search_tools import SEARCH_TOOL_IDS

    chosen = sum(1 for flag in (bool(enable), all_, none_) if flag)
    if chosen > 1:
        raise typer.BadParameter("use only one of --enable, --all or --none")
    if all_:
        return True, None
    if none_:
        return True, []
    if enable:
        ids: list[str] = []
        for raw in enable:
            for part in str(raw).split(","):
                tool_id = part.strip()
                if not tool_id:
                    continue
                if tool_id not in SEARCH_TOOL_IDS:
                    raise typer.BadParameter(
                        f"unknown search tool {tool_id!r} (expected one of: {', '.join(SEARCH_TOOL_IDS)})"
                    )
                if tool_id in ids:
                    raise typer.BadParameter(f"search tool {tool_id!r} is listed twice")
                ids.append(tool_id)
        return True, ids
    return False, None


def render_state(state: dict[str, Any], mode: Any, *, subject: str) -> None:
    """Print a state body: the full JSON with ``--json``, else a table plus the
    pending notice and cache warning."""
    from app.cli.output import Table, print_json

    if mode.json:
        print_json(state)
        return
    enabled = set(state.get("enabled") or [])
    typer.echo(f"Search tools for {subject} (version {state.get('version', 0)})")
    table = Table(mode, "#", "TOOL", "ENABLED", "AVAILABLE", "NOTE")
    for n, tool in enumerate(state.get("tools") or [], start=1):
        tool_id = str(tool.get("id") or "")
        available = bool(tool.get("available"))
        table.add_row(
            str(n),
            f"{tool.get('label') or tool_id} ({tool_id})",
            "yes" if tool_id in enabled else "no",
            "yes" if available else "no",
            str(tool.get("unavailable_reason") or ""),
        )
    table.render()
    effective = state.get("effective") or []
    typer.echo(
        "Next response uses: " + (", ".join(effective) if effective else "no search tools")
    )
    if state.get("pending_next_response"):
        typer.echo(PENDING_NOTICE)
    if state.get("cache_warning"):
        typer.echo(f"warning: {state['cache_warning']}", err=True)


def run(
    ctx: typer.Context,
    path: Optional[str] = None,
    *,
    subject: str,
    enable: Optional[list[str]],
    all_: bool,
    none_: bool,
    resolve_path: Optional[Callable[[Any], Awaitable[str]]] = None,
) -> None:
    """Show, or replace, the selection at ``path`` (or at the path
    ``resolve_path(client)`` returns — a group named rather than by id).

    A write reads the current version first and sends it back, so a change made
    meanwhile (another tab, another member of the room) is never silently
    overwritten. The flags ask for a full replacement, not a delta, so one 409
    is retried against the fresh version; a second one is reported.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.search_tools import VersionConflict, get_state, put_state
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    mode = ctx.obj["mode"]
    cfg.require_token()
    is_write, enabled = resolve_selection(enable, all_, none_)

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            target = path if path is not None else await resolve_path(client)  # type: ignore[misc]
            state = await get_state(client, target)
            if not is_write:
                return state
            for attempt in range(2):
                try:
                    return await put_state(
                        client, target, version=int(state.get("version") or 0), enabled=enabled,
                    )
                except VersionConflict as conflict:
                    state = conflict.state
                    if attempt:
                        raise
            return state

    try:
        state = asyncio.run(_run())
    except VersionConflict as conflict:
        typer.echo("The search tools were changed elsewhere; the current choice is:", err=True)
        render_state(conflict.state, mode, subject=subject)
        raise typer.Exit(code=1) from conflict
    except RuntimeError as exc:
        # A group named rather than by id: an unknown or ambiguous name comes
        # back from the resolver as advice, not a crash (as every other
        # ``cremind group`` command reports it).
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    render_state(state, mode, subject=subject)
