"""`cremind group ...` — multi-profile group chats (one room several agents share).

The terminal counterpart of the web UI's **Group chat** rail entry. A group is
system-wide with per-profile membership: a human posts once, every member agent
gets the message in its own hidden seat and decides for itself whether it was
addressed. Silence is a real answer (`[silent]`), so a quiet room is normal.

Group arguments take an id or a unique case-insensitive name — resolution lives
in `app.cli.client.groups.resolve_group_id`, which refuses an ambiguous name
rather than guessing which room to post into.
"""

from __future__ import annotations

import sys
from typing import Any, Optional

import typer

from app.cli.commands._helpers import graceful_errors


group_app = typer.Typer(
    name="group",
    help="Manage multi-profile group chats and post into them.",
    no_args_is_help=True,
)
members_app = typer.Typer(
    name="members",
    help="Add or remove the member profiles seated in a group.",
    no_args_is_help=True,
)
group_app.add_typer(members_app, name="members")


# ── helpers ───────────────────────────────────────────────────────────────


def _run_async(coro: Any) -> Any:
    """`asyncio.run` plus a clean exit for the group resolver's `RuntimeError`.

    `graceful_errors` only covers config/API/network failures, so without this a
    mistyped group name would end in a traceback instead of one line of advice.
    """
    import asyncio

    try:
        return asyncio.run(coro)
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e


def _clock(created_at: float) -> str:
    """Local `HH:MM:SS` for an epoch-MILLISECONDS timestamp (blank when absent).

    Timeline rows store milliseconds.
    """
    if not created_at:
        return ""
    try:
        from datetime import datetime

        return datetime.fromtimestamp(float(created_at) / 1000.0).strftime("%H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _render_message(msg: Any) -> str:
    """One timeline line: `[#<ordering> HH:MM:SS] <Name> (<kind>): <text>`."""
    stamp = _clock(msg.created_at)
    head = f"#{msg.ordering} {stamp}".strip()
    return f"[{head}] {msg.sender_name} ({msg.sender_kind}): {msg.content}"


def _read_message_text(message: Optional[str], message_file: Optional[str]) -> str:
    """Resolve the post body from an argument, a file, `-`, or piped stdin."""
    if message is not None and message_file is not None:
        typer.echo("pass either a message argument or --message-file, not both", err=True)
        raise typer.Exit(code=1)

    if message_file is not None:
        if message_file == "-":
            text = sys.stdin.read()
        else:
            try:
                with open(message_file, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError as e:
                typer.echo(f"--message-file: {e}", err=True)
                raise typer.Exit(code=1) from e
    elif message is not None:
        text = message
    else:
        # Nothing inline — fall back to stdin so `... | cremind group send` works.
        text = sys.stdin.read()

    text = text.strip()
    if not text:
        typer.echo("message is empty — nothing to post", err=True)
        raise typer.Exit(code=1)
    return text


def _confirm_or_exit(prompt: str, yes: bool) -> None:
    """Gate a destructive action; never guess when there is no terminal."""
    if yes:
        return
    if not sys.stdin.isatty():
        # Non-interactive (scripts, exec_shell): make the caller opt in.
        typer.echo(f"{prompt} Re-run with --yes to confirm.", err=True)
        raise typer.Exit(code=1)
    if not typer.confirm(prompt):
        raise typer.Exit(code=1)


def _setting(settings: dict[str, Any], key: str) -> str:
    """Render one settings value, telling "unset" apart from a falsy one.

    `--max-hops 0` is a real setting — it stops agent-to-agent replies outright —
    so a plain `or ""` would print a deliberately silenced room as an
    unconfigured one.
    """
    value = settings.get(key)
    return "" if value is None else str(value)


def _flag(settings: dict[str, Any], key: str, default: bool) -> str:
    """Render a boolean setting, falling back for a room that predates it.

    A group stored before the knob existed simply has no key, and printing that
    as `False` would describe the opposite of what the server actually does with
    it — the server fills the default in on its next write.
    """
    value = settings.get(key)
    return str(default if value is None else bool(value))


def _settings_patch(
    web_sender_name: Optional[str],
    max_hops: Optional[int],
    routing: Optional[bool] = None,
) -> dict[str, Any]:
    """The settings keys named by the flags, skipping the ones left alone."""
    patch: dict[str, Any] = {}
    if web_sender_name is not None:
        patch["web_sender_name"] = web_sender_name
    if max_hops is not None:
        patch["max_agent_hops"] = max_hops
    if routing is not None:
        patch["smart_routing"] = routing
    return patch


async def _follow(client: Any, mode: Any, group_id: str, since: Optional[int]) -> None:
    """Print the live timeline until the group is deleted (or Ctrl-C).

    `agent_status` is progress, not content: it goes to stderr and only on a
    terminal, so a piped `--follow` stays a clean transcript. `--json` forwards
    every frame verbatim as JSONL instead, mirroring `run_admin_stream`.
    """
    from app.cli.client.groups import GroupMessage, group_stream_path
    from app.cli.output.console import is_tty

    async for event in client.stream(group_stream_path(group_id, since=since)):
        if mode.json:
            sys.stdout.write(event.raw)
            if not event.raw.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()
            if event.type == "deleted":
                return
            continue

        payload = event.data.get("data") if isinstance(event.data, dict) else None
        payload = payload if isinstance(payload, dict) else {}
        if event.type == "message":
            sys.stdout.write(_render_message(GroupMessage.from_dict(payload)) + "\n")
            sys.stdout.flush()
        elif event.type == "agent_status":
            if is_tty():
                who = str(payload.get("agent_name") or payload.get("profile") or "agent")
                state = str(payload.get("state") or "")
                label = "is thinking..." if state == "thinking" else f"is {state}"
                sys.stderr.write(f"* {who} {label}\n")
                sys.stderr.flush()
        elif event.type == "deleted":
            sys.stderr.write("group deleted\n")
            return


# ── group lifecycle ───────────────────────────────────────────────────────


@group_app.command("list")
@graceful_errors
def group_list(ctx: typer.Context) -> None:
    """List the group chats you can see (every group for admin)."""
    from app.cli.client._base import Client
    from app.cli.client.groups import GroupChat, list_groups
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> list[GroupChat]:
        async with Client(cfg) as client:
            return await list_groups(client)

    groups = _run_async(_run())

    if mode.json:
        print_json([g.to_dict() for g in groups])
        return
    table = Table(mode, "ID", "NAME", "MEMBERS", "LAST_MESSAGE")
    for g in groups:
        last = g.last_message or {}
        preview = str(last.get("content") or "").replace("\n", " ")
        if len(preview) > 48:
            preview = preview[:45] + "..."
        sender = str(last.get("sender_name") or "")
        table.add_row(
            g.id,
            g.name,
            ", ".join(g.members),
            f"{sender}: {preview}" if sender else "",
        )
    table.render()


@group_app.command("create")
@graceful_errors
def group_create(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Group name (what people call the room)."),
    member: Optional[list[str]] = typer.Option(
        None, "--member", "-m",
        help="Seat this profile in the group. Repeat for several members.",
    ),
    web_sender_name: Optional[str] = typer.Option(
        None, "--web-sender-name",
        help="Name your posts from the web/CLI appear under (default: Operator).",
    ),
    max_hops: Optional[int] = typer.Option(
        None, "--max-hops",
        help="Loop guard: how far an agent-to-agent chain may run from the last "
             "human post before replies stop (default: 6).",
    ),
    routing: Optional[bool] = typer.Option(
        None, "--routing/--no-routing",
        help="Let a cheap model pick which agents start a turn on each post "
             "instead of waking all of them; every agent still gets the "
             "message and still decides for itself (default: yes).",
    ),
) -> None:
    """Create a group and seat its member profiles (admin only)."""
    from app.cli.client._base import Client
    from app.cli.client.groups import GroupChat, create_group
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    settings = _settings_patch(web_sender_name, max_hops, routing)

    async def _run() -> GroupChat:
        async with Client(cfg) as client:
            return await create_group(
                client,
                name=name,
                members=list(member or []),
                settings=settings or None,
            )

    group = _run_async(_run())

    if mode.json:
        print_json(group.to_dict())
        return
    print_kv([
        ("id", group.id),
        ("name", group.name),
        ("members", ", ".join(group.members)),
    ])


@group_app.command("show")
@graceful_errors
def group_show(
    ctx: typer.Context,
    group: str = typer.Argument(..., help="Group id or unique name."),
) -> None:
    """Show a group's members and settings."""
    from app.cli.client._base import Client
    from app.cli.client.groups import GroupChat, get_group, resolve_group_id
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json, print_kv

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> GroupChat:
        async with Client(cfg) as client:
            return await get_group(client, await resolve_group_id(client, group))

    found = _run_async(_run())

    if mode.json:
        print_json(found.to_dict())
        return

    settings = found.settings or {}
    print_kv([
        ("id", found.id),
        ("name", found.name),
        ("created_by", found.created_by),
        ("web_sender_name", _setting(settings, "web_sender_name")),
        ("max_agent_hops", _setting(settings, "max_agent_hops")),
        ("smart_routing", _flag(settings, "smart_routing", True)),
    ])

    sys.stdout.write("\n--- members ---\n")
    members = Table(mode, "PROFILE", "STATE")
    for profile in found.members:
        members.add_row(
            profile, "thinking" if profile in found.thinking else "idle",
        )
    members.render()


@group_app.command("set")
@graceful_errors
def group_set(
    ctx: typer.Context,
    group: str = typer.Argument(..., help="Group id or unique name."),
    name: Optional[str] = typer.Option(None, "--name", help="Rename the group."),
    web_sender_name: Optional[str] = typer.Option(
        None, "--web-sender-name",
        help="Name your posts from the web/CLI appear under.",
    ),
    max_hops: Optional[int] = typer.Option(
        None, "--max-hops",
        help="Loop guard: how far an agent-to-agent chain may run from the last "
             "human post before replies stop.",
    ),
    routing: Optional[bool] = typer.Option(
        None, "--routing/--no-routing",
        help="Let a cheap model pick which agents start a turn on each post "
             "instead of waking all of them (see the Routing section of the "
             "`cremind group` doc).",
    ),
) -> None:
    """Change a group's name or settings (admin only).

    Settings are stored as one blob and replaced whole, so this reads the group
    first and sends back the merged result — the knobs you don't name keep their
    current values, including the user accounts.
    """
    from app.cli.client._base import Client
    from app.cli.client.groups import GroupChat, get_group, resolve_group_id, update_group
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    patch = _settings_patch(web_sender_name, max_hops, routing)
    if name is None and not patch:
        typer.echo(
            "nothing to update — pass at least one of --name / --web-sender-name / "
            "--max-hops / --allow-unknown/--no-allow-unknown / --routing/--no-routing",
            err=True,
        )
        raise typer.Exit(code=1)

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> GroupChat:
        async with Client(cfg) as client:
            group_id = await resolve_group_id(client, group)
            fields: dict[str, Any] = {}
            if name is not None:
                fields["name"] = name
            if patch:
                current = await get_group(client, group_id)
                fields["settings"] = {**(current.settings or {}), **patch}
            return await update_group(client, group_id, fields)

    updated = _run_async(_run())

    if mode.json:
        print_json(updated.to_dict())
        return
    settings = updated.settings or {}
    print_kv([
        ("id", updated.id),
        ("name", updated.name),
        ("web_sender_name", _setting(settings, "web_sender_name")),
        ("max_agent_hops", _setting(settings, "max_agent_hops")),
        ("smart_routing", _flag(settings, "smart_routing", True)),
    ])


@group_app.command("delete")
@graceful_errors
def group_delete(
    ctx: typer.Context,
    group: str = typer.Argument(..., help="Group id or unique name."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Delete a group, its timeline, and every member's hidden seat.

    Irreversible: the room's history goes with it. The member profiles and their
    own conversations are untouched.
    """
    from app.cli.client._base import Client
    from app.cli.client.groups import delete_group, resolve_group_id
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()
    _confirm_or_exit(
        f"Delete group {group!r} with its whole timeline? This cannot be undone.",
        yes,
    )

    async def _run() -> None:
        async with Client(cfg) as client:
            await delete_group(client, await resolve_group_id(client, group))

    _run_async(_run())

    if mode.json:
        print_json({"deleted": True, "group": group})
        return
    sys.stdout.write(f"deleted group {group}\n")


# ── membership ────────────────────────────────────────────────────────────


def _set_members(ctx: typer.Context, group: str, profiles: list[str], add: bool) -> None:
    """Read-modify-write the member list (PATCH replaces it whole)."""
    from app.cli.client._base import Client
    from app.cli.client.groups import GroupChat, get_group, resolve_group_id, update_group
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> Optional[GroupChat]:
        async with Client(cfg) as client:
            group_id = await resolve_group_id(client, group)
            current = await get_group(client, group_id)
            if add:
                wanted = list(dict.fromkeys([*current.members, *profiles]))
            else:
                drop = set(profiles)
                wanted = [p for p in current.members if p not in drop]
            if wanted == current.members:
                return None
            return await update_group(client, group_id, {"members": wanted})

    updated = _run_async(_run())

    if updated is None:
        # Still valid output in --json mode — a script must not have to parse
        # "no change" out of a stream it expects to be JSON.
        if mode.json:
            print_json({"changed": False})
        else:
            sys.stdout.write("no change\n")
        return
    if mode.json:
        print_json(updated.to_dict())
        return
    sys.stdout.write(f"{updated.name}: {', '.join(updated.members) or '(no members)'}\n")


@members_app.command("add")
@graceful_errors
def members_add(
    ctx: typer.Context,
    group: str = typer.Argument(..., help="Group id or unique name."),
    profiles: list[str] = typer.Argument(..., help="Profiles to seat in the group."),
) -> None:
    """Seat one or more profiles in a group.

    Each new member gets a hidden seat conversation and starts receiving the
    room's posts from that moment; it does not see the history it missed.
    """
    _set_members(ctx, group, list(profiles), add=True)


@members_app.command("remove")
@graceful_errors
def members_remove(
    ctx: typer.Context,
    group: str = typer.Argument(..., help="Group id or unique name."),
    profiles: list[str] = typer.Argument(..., help="Profiles to remove."),
) -> None:
    """Remove profiles from a group (their seat conversation is deleted)."""
    _set_members(ctx, group, list(profiles), add=False)


# ── user accounts ─────────────────────────────────────────────────────────


@group_app.command("send")
@graceful_errors
def group_send(
    ctx: typer.Context,
    group: str = typer.Argument(..., help="Group id or unique name."),
    message: Optional[str] = typer.Argument(
        None, help="Message text. Omit to read from --message-file or stdin.",
    ),
    message_file: Optional[str] = typer.Option(
        None, "--message-file", "-f",
        help="Read the message from this file (use '-' for stdin). Preferred on "
             "PowerShell, where inline quoting mangles apostrophes/quotes.",
    ),
    as_profile: Optional[str] = typer.Option(
        None, "--as",
        help="Post as this member agent instead of as a human (counts as one "
             "agent hop).",
    ),
    follow: bool = typer.Option(
        False, "--follow", "-F",
        help="Keep streaming the room after posting, so replies print as they land.",
    ),
) -> None:
    """Post into a group. Every other member agent then takes its own turn.

    Only the agents that consider themselves addressed answer; the rest end
    their turn with the silent sentinel and nothing appears for them. A room
    that stays quiet is a normal outcome, not a failure.
    """
    from app.cli.client._base import Client
    from app.cli.client.groups import GroupMessage, post_message, resolve_group_id
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    text = _read_message_text(message, message_file)

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> GroupMessage:
        async with Client(cfg) as client:
            group_id = await resolve_group_id(client, group)
            posted = await post_message(
                client, group_id, text, as_profile=as_profile or "",
            )
            if follow:
                if not mode.json:
                    sys.stdout.write(_render_message(posted) + "\n")
                    sys.stdout.flush()
                # Subscribe AT our own post, not before it: `since` is
                # exclusive, so this drops the replay copy of the row we just
                # printed. Nothing is lost — anything that landed between the
                # POST and here has a strictly higher ordering.
                await _follow(client, mode, group_id, since=posted.ordering)
            return posted

    try:
        posted = _run_async(_run())
    except KeyboardInterrupt:
        raise typer.Exit(code=130)

    if follow:
        return
    if mode.json:
        print_json(posted.to_dict())
        return
    sys.stdout.write(f"posted #{posted.ordering} as {posted.sender_name}\n")


@group_app.command("history")
@graceful_errors
def group_history(
    ctx: typer.Context,
    group: str = typer.Argument(..., help="Group id or unique name."),
    limit: int = typer.Option(100, "--limit", help="Page size."),
    after: Optional[int] = typer.Option(
        None, "--after",
        help="Only posts after this #ordering (default: the newest --limit posts).",
    ),
    follow: bool = typer.Option(
        False, "--follow", "-f",
        help="After printing the history, keep streaming new posts.",
    ),
) -> None:
    """Print a group's timeline, optionally tailing it.

    Without `--after` the server answers with its newest `--limit` posts, which
    is what you want in a long-running room; `--after -1` reads from the very
    first post, since `ordering` counts from 0.

    Silent turns leave no row: a member that decided the message was not for it
    never appears here.
    """
    from app.cli.client._base import Client
    from app.cli.client.groups import GroupMessage, list_messages, resolve_group_id
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> list[GroupMessage]:
        async with Client(cfg) as client:
            group_id = await resolve_group_id(client, group)
            msgs = await list_messages(client, group_id, after=after, limit=limit)
            if not follow:
                return msgs
            if mode.json:
                from app.cli.output import print_jsonl

                # Wrapped in the live tail's frame shape so one jq filter reads
                # the replayed page and the tail that follows it alike.
                for msg in msgs:
                    print_jsonl({"type": "message", "data": msg.to_dict()})
            else:
                for msg in msgs:
                    sys.stdout.write(_render_message(msg) + "\n")
                sys.stdout.flush()
            cursor = msgs[-1].ordering if msgs else after
            await _follow(client, mode, group_id, since=cursor)
            return msgs

    try:
        msgs = _run_async(_run())
    except KeyboardInterrupt:
        raise typer.Exit(code=130)

    if follow:
        return
    if mode.json:
        print_json([m.to_dict() for m in msgs])
        return
    for msg in msgs:
        sys.stdout.write(_render_message(msg) + "\n")
