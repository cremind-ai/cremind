"""`cremind tags devices ...` — set up and manage this profile's own Cremind Tag
hardware through Cremind Connect (Settings → Tags, "Hardware").

Mirrors the Settings page: connect a USB gateway (a Cremind Connect window on
the computer the gateway is plugged into asks for approval), add bridges and
tags from their setup codes, and pause, move, test, remove or recover them.
The CLI is optional — the Settings page does all of it.

Setup codes are secrets printed on labels: they are read from a file
(``--code-file``, ``-`` for stdin) or a hidden prompt (``--code-prompt``),
never taken as a command-line argument (shell history, process lists) and
never printed back or logged.

A device is named by its id, its name, its short id (``1A2B3C4D``) or a device
id prefix; a connection (gateway) by its id or name.
"""

from __future__ import annotations

import sys
import time
import webbrowser
from typing import Any, Optional
from urllib.parse import urlsplit

import typer

from app.cli.commands._helpers import graceful_errors
from app.cli.commands.tags import _call, _cell, _fail, _match, _mode, _rows

devices_app = typer.Typer(
    name="devices",
    help="Set up your own gateways, bridges and tags with Cremind Connect: connect, add, pause, remove, recover.",
    no_args_is_help=True,
)

_POLL_S = 1.5
_HINTS = {
    "simple_setup_disabled": "The admin has not turned on hardware setup from Settings → Tags on this server.",
    "no_gateway": "Connect a gateway first: cremind tags devices connect",
    "no_ready_bridge": "Add a bridge first: cremind tags devices add bridge --code-file <label.txt>",
    "gateway_required": "Name the gateway: --gateway <connection>. List them: cremind tags devices list",
    "setup_code_invalid": "Check the code on the label (25 characters; 0/O and 1/I/L are the same).",
    "setup_code_wrong_role": "This label belongs to another kind of device.",
    "already_paired": "It is already set up: cremind tags devices list",
    "device_owned": "That device is set up elsewhere. Reset it (see its documentation) to set it up again.",
    "not_approved": "Approve in the Cremind Connect window first.",
    "session_expired": "The setup expired (5 minutes). Start again.",
    "bridge_full": "Pick a bridge with room, or move a tag off this one: cremind tags devices move",
    "authority_unavailable": "This server lost the keys its devices trust; restore them from an encrypted backup "
                             "made with --include-tag-keys.",
}


def _server_origin(ctx: typer.Context, server: Optional[str]) -> str:
    raw = server or ctx.obj["cfg"].server
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        _fail(f"--server must be an http(s) address, got '{raw}'")
    return f"{parts.scheme}://{parts.netloc}"


def _read_code(code_file: Optional[str], code_prompt: bool) -> str:
    if bool(code_file) == bool(code_prompt):
        _fail("give the setup code with --code-file <file> (- for stdin) or --code-prompt")
    if code_prompt:
        return typer.prompt("Setup code from the label", hide_input=True).strip()
    if code_file == "-":
        return sys.stdin.readline().strip()
    try:
        with open(str(code_file), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError as e:
        _fail(f"--code-file: {e}")


def _devices(view: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for conn in _rows(view.get("connections")):
        for d in [conn.get("gateway")] + _rows(conn.get("bridges")) + _rows(conn.get("tags")):
            if isinstance(d, dict):
                out.append({**d, "connection": conn.get("id"), "connection_name": conn.get("name")})
    return out


async def _device(client: Any, ref: str, *, kind: Optional[str] = None) -> dict[str, Any]:
    from app.cli.client import tags_setup as api

    items = [d for d in _devices(await api.connections(client)) if kind is None or d.get("kind") == kind]
    low = ref.strip().lower()
    for d in items:
        if str(d.get("device_id") or "").startswith(low) and len(low) >= 8:
            return d
    return _match(items, ref, what=kind or "device", list_cmd="cremind tags devices list",
                  fields=("name", "short_id"))


async def _connection(client: Any, ref: Optional[str]) -> dict[str, Any]:
    from app.cli.client import tags_setup as api

    conns = _rows((await api.connections(client)).get("connections"))
    if ref is None:
        if len(conns) == 1:
            return conns[0]
        _fail("name the connection: --gateway <id or name> (cremind tags devices list)")
    return _match(conns, ref, what="connection", list_cmd="cremind tags devices list", fields=("name",))


def _open_link(url: str, *, open_it: bool) -> None:
    sys.stdout.write("Opening Cremind Connect on this computer…\n" if open_it else
                     "Open this link on the computer the gateway is plugged into:\n")
    if not open_it:
        sys.stdout.write(f"  {url}\n")
        return
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        sys.stdout.write(f"  (could not open it automatically — open: {url})\n")


def _wait(ctx: typer.Context, fetch, done, timeout: float, show) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = None
    while True:
        out = _call(ctx, fetch, hints=_HINTS)
        if show is not None:
            line = show(out)
            if line and line != last:
                sys.stdout.write(line + "\n")
                last = line
        if done(out):
            return out
        if time.monotonic() > deadline:
            _fail("timed out waiting; check again with: cremind tags devices status <id>")
        time.sleep(_POLL_S)


def _connect_flow(ctx: typer.Context, operation: str, server: Optional[str], open_link: bool, yes: bool,
                  timeout: float, companion_id: Optional[str] = None) -> dict[str, Any]:
    from app.cli.client import tags_setup as api

    origin = _server_origin(ctx, server)
    if operation == "recover":
        out = _call(ctx, lambda c: api.start_recovery(c, companion_id or "", origin), hints=_HINTS)
    else:
        out = _call(ctx, lambda c: api.create_session(c, operation, origin), hints=_HINTS)
    session = out.get("session") or {}
    sid = session.get("id")
    _open_link(str(out.get("launch_url") or ""), open_it=open_link)
    sys.stdout.write("Waiting for Cremind Connect (approve in its window)…\n")
    state = _wait(ctx, lambda c: api.get_session(c, sid),
                  lambda o: (o.get("session") or {}).get("state") in (
                      "waiting_for_confirmation", "completed", "cancelled", "expired", "failed"),
                  timeout, None).get("session") or {}
    if state.get("state") != "waiting_for_confirmation":
        _fail(f"setup ended: {state.get('state')} {((state.get('error') or {}).get('message') or '')}".rstrip())
    computer = (state.get("computer") or {}).get("name") or "this computer"
    gateway = (state.get("gateway") or {}).get("short_id") or ""
    sys.stdout.write(f"\nComputer: {computer}\nGateway:  …{gateway}\n"
                     f"Words:    {state.get('verification_phrase')}\n\n")
    if not yes and not typer.confirm("Does the Cremind Connect window show the same words?", default=False):
        _call(ctx, lambda c: api.cancel_session(c, sid))
        _fail("cancelled.")
    _call(ctx, lambda c: api.confirm_session(c, sid), hints=_HINTS)
    final = _wait(ctx, lambda c: api.get_session(c, sid),
                  lambda o: (o.get("session") or {}).get("state") in ("completed", "cancelled", "expired", "failed"),
                  timeout, lambda o: {"redeeming": "Setting up…", "connecting": "Connecting to your gateway…"}.get(
                      (o.get("session") or {}).get("state"))).get("session") or {}
    if final.get("state") != "completed":
        _fail(f"setup ended: {final.get('state')} {((final.get('error') or {}).get('message') or '')}".rstrip())
    return {"session": final, **({"recovery": out.get("recovery")} if operation == "recover" else {})}


# ── readers ────────────────────────────────────────────────────────────────


@devices_app.command("list")
@graceful_errors
def devices_list(ctx: typer.Context) -> None:
    """List this profile's gateways (connections) with their bridges and tags."""
    from app.cli.client import tags_setup as api
    from app.cli.output import Table, print_json

    mode = _mode(ctx)
    out = _call(ctx, api.connections, hints=_HINTS)
    if mode.json:
        print_json(out)
        return
    if not out.get("simple_setup"):
        sys.stdout.write("Hardware setup is not enabled on this server.\n")
    devices = _devices(out)
    if not devices:
        sys.stdout.write("No hardware yet. Plug in a gateway and run: cremind tags devices connect\n")
        return
    table = Table(mode, "ID", "KIND", "NAME", "SHORT ID", "STATE", "CONNECTION", "DETAIL")
    for d in devices:
        detail = ""
        if d.get("kind") == "bridge" and d.get("capacity"):
            detail = f"{d['capacity'].get('assigned')}/{d['capacity'].get('max_tags')} tags"
        elif d.get("kind") == "tag" and d.get("delivery"):
            detail = f"{d['delivery'].get('pending_count')} pending"
        if d.get("paused"):
            detail = (detail + ", paused").lstrip(", ")
        table.add_row(str(d.get("id") or ""), str(d.get("kind") or ""), _cell(mode, d.get("name")),
                      str(d.get("short_id") or ""), str(d.get("state") or ""),
                      _cell(mode, d.get("connection_name")), detail)
    table.render()


@devices_app.command("status")
@graceful_errors
def devices_status(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="A setup session, search, pairing or recovery id."),
) -> None:
    """Show one setup session, device search, pairing or recovery."""
    from app.cli.client import tags_setup as api
    from app.cli.client._base import APIError
    from app.cli.output import print_json

    async def go(client: Any) -> dict[str, Any]:
        for fetch in (api.get_session, api.get_pairing, api.get_discovery, api.get_recovery):
            try:
                return await fetch(client, ref)
            except APIError as e:
                if getattr(e, "status", None) != 404:
                    raise
        _fail(f"nothing with id '{ref}'")

    print_json(_call(ctx, go, hints=_HINTS))


@devices_app.command("installers")
@graceful_errors
def devices_installers(ctx: typer.Context) -> None:
    """Where to download Cremind Connect for each operating system."""
    from app.cli.client import tags_setup as api
    from app.cli.output import print_json

    out = _call(ctx, api.installers)
    if _mode(ctx).json:
        print_json(out)
        return
    for platform, item in sorted((out.get("downloads") or {}).items()):
        sys.stdout.write(f"{platform:16} {item.get('url')}\n")


# ── writers ────────────────────────────────────────────────────────────────


@devices_app.command("connect")
@graceful_errors
def devices_connect(
    ctx: typer.Context,
    server: Optional[str] = typer.Option(None, "--server", help="The address Cremind Connect should use "
                                                                "(default: the CLI's server)."),
    open_link: bool = typer.Option(True, "--open/--no-open", help="Open the Connect link on this computer."),
    yes: bool = typer.Option(False, "--yes", help="Do not ask whether the words match (you checked them)."),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait (the setup itself expires after 300)."),
) -> None:
    """Connect a USB gateway plugged into this computer (Cremind Connect asks for approval)."""
    from app.cli.output import print_json

    out = _connect_flow(ctx, "connect_gateway", server, open_link, yes, timeout)
    if _mode(ctx).json:
        print_json(out)
        return
    sys.stdout.write("Gateway connected. Next: cremind tags devices add bridge --code-file <label.txt>\n")


@devices_app.command("add")
@graceful_errors
def devices_add(
    ctx: typer.Context,
    kind: str = typer.Argument(..., help="bridge or tag"),
    code_file: Optional[str] = typer.Option(None, "--code-file", help="File holding the label's setup code "
                                                                      "(- reads one line from stdin)."),
    code_prompt: bool = typer.Option(False, "--code-prompt", help="Type the setup code at a hidden prompt."),
    gateway: Optional[str] = typer.Option(None, "--gateway", help="Bridges: the connection (gateway) to join."),
    candidate: Optional[str] = typer.Option(None, "--candidate", help="Tags: the search result to pair with "
                                                                      "(when several bridges hear the tag)."),
    name: Optional[str] = typer.Option(None, "--name", help="A name for the new device."),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait for the device."),
) -> None:
    """Add a bridge or a tag from the setup code on its label."""
    from app.cli.client import tags_setup as api
    from app.cli.output import print_json

    if kind not in ("bridge", "tag"):
        _fail("add what? bridge or tag")
    code = _read_code(code_file, code_prompt)
    gateway_id = None
    if kind == "bridge" and gateway:
        gateway_id = _call(ctx, lambda c: _connection(c, gateway)).get("id")
    out = _call(ctx, lambda c: api.start_discovery(c, kind, code, gateway_id=gateway_id), hints=_HINTS)
    code = ""
    disc = out.get("discovery") or {}
    sys.stdout.write("Searching" + (" — waiting for the tag to wake (about 30 s)…\n" if kind == "tag" else "…\n"))
    disc = _wait(ctx, lambda c: api.get_discovery(c, disc["id"]),
                 lambda o: (o.get("discovery") or {}).get("state") != "scanning", timeout, None).get("discovery") or {}
    if disc.get("state") != "found":
        _fail(f"nothing found ({disc.get('state')}). Check the device is powered and near a "
              f"{'gateway' if kind == 'bridge' else 'bridge'}.")
    eligible = [c for c in _rows(disc.get("candidates")) if c.get("eligible")]
    pick = candidate or (eligible[0]["id"] if len(eligible) == 1 else None)
    if pick is None:
        lines = "\n".join(f"  {c.get('id')}  {c.get('bridge_name') or c.get('gateway_id')}  rssi {c.get('rssi')}"
                          for c in eligible)
        _fail(f"several bridges hear it (recommended: {disc.get('recommended')}); choose with --candidate:\n{lines}")
    out = _call(ctx, lambda c: api.start_pairing(c, disc["id"], pick, name), hints=_HINTS)
    pairing = out.get("pairing") or {}
    final = _wait(ctx, lambda c: api.get_pairing(c, pairing["id"]),
                  lambda o: (o.get("pairing") or {}).get("state") in ("succeeded", "failed", "cancelled"), timeout,
                  lambda o: (o.get("pairing") or {}).get("stage_detail") or (o.get("pairing") or {}).get("stage"))
    if _mode(ctx).json:
        print_json(final)
        return
    p = final.get("pairing") or {}
    if p.get("state") != "succeeded":
        _fail(f"{kind} not added: {((p.get('error') or {}).get('message') or p.get('state'))}")
    sys.stdout.write(f"{kind.capitalize()} ready.\n")


@devices_app.command("cancel")
@graceful_errors
def devices_cancel(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="A setup session or pairing id."),
) -> None:
    """Cancel an unfinished setup session or pairing."""
    from app.cli.client import tags_setup as api
    from app.cli.client._base import APIError
    from app.cli.output import print_json

    async def go(client: Any) -> dict[str, Any]:
        try:
            return await api.cancel_pairing(client, ref)
        except APIError as e:
            if getattr(e, "status", None) != 404:
                raise
        return await api.cancel_session(client, ref)

    print_json(_call(ctx, go, hints=_HINTS))


@devices_app.command("remove")
@graceful_errors
def devices_remove(
    ctx: typer.Context,
    device: str = typer.Argument(..., help="The device: id, name, short id or device id prefix."),
    force: bool = typer.Option(False, "--force", help="Forget it now, even though it cannot be cleaned up."),
    yes: bool = typer.Option(False, "--yes", help="Do not ask for confirmation."),
) -> None:
    """Remove a tag, a bridge or a whole gateway (its access is revoked at once)."""
    from app.cli.client import tags_setup as api
    from app.cli.output import print_json

    target = _call(ctx, lambda c: _device(c, device))
    if not yes and not typer.confirm(f"Remove {target.get('kind')} '{target.get('name') or target.get('short_id')}'?",
                                     default=False):
        _fail("cancelled.")
    out = _call(ctx, lambda c: api.unpair(c, target["id"], force=force), hints=_HINTS)
    if _mode(ctx).json:
        print_json(out)
        return
    affected = ((out.get("device") or {}).get("affected_tag_ids") or [])
    if affected:
        sys.stdout.write(f"{len(affected)} tag(s) used this bridge; move them: cremind tags devices move <tag> "
                         "--bridge <bridge>\n")
    sys.stdout.write("Removed from Cremind." + ("" if force else " Cleanup finishes when the device is reachable.")
                     + "\n")


def _pause(ctx: typer.Context, device: str, paused: bool) -> None:
    from app.cli.client import tags_setup as api
    from app.cli.output import print_json

    target = _call(ctx, lambda c: _device(c, device))
    print_json(_call(ctx, lambda c: api.pause(c, target["id"], paused), hints=_HINTS))


@devices_app.command("pause")
@graceful_errors
def devices_pause(
    ctx: typer.Context,
    device: str = typer.Argument(..., help="A tag, or a gateway (pauses its whole connection)."),
) -> None:
    """Stop sending new updates to a tag or a whole gateway (pairing is kept)."""
    _pause(ctx, device, True)


@devices_app.command("resume")
@graceful_errors
def devices_resume(
    ctx: typer.Context,
    device: str = typer.Argument(..., help="A paused tag or gateway."),
) -> None:
    """Resume a paused tag or gateway."""
    _pause(ctx, device, False)


@devices_app.command("move")
@graceful_errors
def devices_move(
    ctx: typer.Context,
    tag: str = typer.Argument(..., help="The tag."),
    bridge: str = typer.Option(..., "--bridge", help="The ready bridge (same gateway) to use."),
) -> None:
    """Serve a tag through another bridge."""
    from app.cli.client import tags_setup as api
    from app.cli.output import print_json

    async def go(client: Any) -> dict[str, Any]:
        t = await _device(client, tag, kind="tag")
        b = await _device(client, bridge, kind="bridge")
        return await api.move(client, t["id"], b["id"])

    print_json(_call(ctx, go, hints=_HINTS))


@devices_app.command("test")
@graceful_errors
def devices_test(
    ctx: typer.Context,
    tag: str = typer.Argument(..., help="The tag."),
) -> None:
    """Show a test card on a tag."""
    from app.cli.client import tags_setup as api
    from app.cli.output import print_json

    async def go(client: Any) -> dict[str, Any]:
        t = await _device(client, tag, kind="tag")
        return await api.test_card(client, t["id"])

    print_json(_call(ctx, go, hints=_HINTS))


@devices_app.command("recover")
@graceful_errors
def devices_recover(
    ctx: typer.Context,
    connection: str = typer.Argument(..., help="The connection (gateway) to move to this computer."),
    server: Optional[str] = typer.Option(None, "--server", help="The address Cremind Connect should use."),
    open_link: bool = typer.Option(True, "--open/--no-open", help="Open the Connect link on this computer."),
    yes: bool = typer.Option(False, "--yes", help="Do not ask whether the words match."),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait."),
) -> None:
    """Recover a gateway and its devices on this (replacement) computer."""
    from app.cli.output import print_json

    conn = _call(ctx, lambda c: _connection(c, connection))
    out = _connect_flow(ctx, "recover", server, open_link, yes, timeout, companion_id=conn.get("id"))
    if _mode(ctx).json:
        print_json(out)
        return
    sys.stdout.write("Recovery started; devices move over as they wake: cremind tags devices list\n")


__all__ = ["devices_app"]
