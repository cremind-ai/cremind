"""`cremind tags devices ...` — set up and manage this profile's own Cremind Tag
hardware (Settings → Tags, "Your hardware").

Mirrors the Settings page: connect a USB gateway plugged into a gateway
computer (Cremind drives it from there — see ``cremind tags hosts``), add
tags and bridges from their setup codes, and pause, move, test, remove or
recover them onto another computer. A tag connects through the gateway itself
when the gateway reaches tags on its own radio (newer gateways), or through a
bridge, which carries updates farther. The CLI is optional — the Settings page
does all of it. It never acts on the USB ports of the computer it runs on
unless that is the gateway computer named (or the only one there is).

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
from typing import Any, Optional

import typer

from app.cli.commands._helpers import graceful_errors
from app.cli.commands.tags import _call, _cell, _fail, _match, _mode, _rows
from app.cli.commands.tags_hosts import (
    ERROR_PROBLEM, HINTS as HOST_HINTS, candidate_line, host_name, problem_text, resolve_host, run_scan,
    scan_problem, wait_operation,
)

devices_app = typer.Typer(
    name="devices",
    help="Set up your own gateways, bridges and tags: connect, add, pause, remove, move to another computer.",
    no_args_is_help=True,
)

_POLL_S = 1.5
_HINTS = {
    **HOST_HINTS,
    "simple_setup_disabled": "The admin has not turned on hardware setup from Settings → Tags on this server.",
    "no_gateway": "Connect a gateway first: cremind tags devices connect",
    "no_ready_bridge": "Connect a gateway first (or resume a paused one); if your gateway cannot reach tags itself, "
                       "add a bridge: cremind tags devices add bridge --code-file <label.txt>",
    "candidate_not_eligible": "Choose the tag's gateway (when it reaches tags itself) or a ready bridge of that "
                              "gateway: cremind tags devices list",
    "gateway_required": "Name the gateway: --gateway <connection>. List them: cremind tags devices list",
    "setup_code_invalid": "Check the code on the label (25 characters; 0/O and 1/I/L are the same).",
    "setup_code_wrong_role": "This label belongs to another kind of device.",
    "already_paired": "It is already set up: cremind tags devices list",
    "invalid_tag_id": "Give the tag id the hardware tools printed: 8 hex digits (cremind tags tools tag list).",
    "import_not_allowed": "Import it from the profile that owns the computer the gateway is plugged into (the "
                          "admin on the server's own computer).",
    "device_owned": "That device is set up elsewhere. Reset it (see its documentation) to set it up again.",
    "session_expired": "The setup expired. Start again.",
    "bridge_full": "Pick another gateway or bridge with room, or move a tag off this one: "
                   "cremind tags devices move <tag> --to <gateway or bridge>",
    "authority_unavailable": "This server lost the keys its devices trust; restore them from an encrypted backup "
                             "made with --include-tag-keys.",
}


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


def _places(view: dict[str, Any]) -> dict[str, str]:
    """Device id -> how the gateway or bridge a tag connects through is named: a
    gateway by its name (its connection's), a bridge by its name or short id."""
    names: dict[str, str] = {}
    for conn in _rows(view.get("connections")):
        gateway = conn.get("gateway") if isinstance(conn.get("gateway"), dict) else {}
        if gateway.get("id"):
            names[gateway["id"]] = str(gateway.get("name") or conn.get("name") or "the gateway")
        for b in _rows(conn.get("bridges")):
            if b.get("id"):
                names[b["id"]] = str(b.get("name") or b.get("short_id") or "a bridge")
    return names


def _find(items: list[dict[str, Any]], ref: str, *, what: str = "device") -> dict[str, Any]:
    """One of ``items`` by id, name, short id or device id prefix (8+ characters)."""
    low = ref.strip().lower()
    for d in items:
        if str(d.get("device_id") or "").startswith(low) and len(low) >= 8:
            return d
    return _match(items, ref, what=what, list_cmd="cremind tags devices list", fields=("name", "short_id"))


async def _device(client: Any, ref: str, *, kind: Optional[str] = None) -> dict[str, Any]:
    from app.cli.client import tags_setup as api

    items = [d for d in _devices(await api.connections(client)) if kind is None or d.get("kind") == kind]
    return _find(items, ref, what=kind or "device")


def _candidate_name(c: dict[str, Any], kind: str) -> str:
    """A search result as people know it: the gateway (a bridge's search, or a tag
    the gateway hears on its own radio) or the bridge that heard the device."""
    if kind == "bridge" or c.get("bridge_kind") == "gateway":
        return f"gateway '{c.get('bridge_name') or c.get('gateway_id')}'"
    return f"bridge '{c.get('bridge_name') or c.get('bridge_id')}'"


def _serves_tags(ctx: typer.Context, companion_id: Any) -> bool:
    """Whether that connection's gateway reaches tags on its own radio (False when unknown)."""
    from app.cli.client import tags_setup as api

    async def fetch(client: Any) -> dict[str, Any]:
        try:
            return await api.connections(client)
        except Exception:  # noqa: BLE001 - only the next-step line depends on it
            return {}

    conn = next((c for c in _rows(_call(ctx, fetch).get("connections")) if c.get("id") == companion_id), None)
    return bool(((conn or {}).get("gateway") or {}).get("serves_tags"))


async def _connection(client: Any, ref: Optional[str]) -> dict[str, Any]:
    from app.cli.client import tags_setup as api

    conns = _rows((await api.connections(client)).get("connections"))
    if ref is None:
        if len(conns) == 1:
            return conns[0]
        _fail("name the connection: --gateway <id or name> (cremind tags devices list)")
    return _match(conns, ref, what="connection", list_cmd="cremind tags devices list", fields=("name",))


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
        sys.stdout.write("No hardware yet. Plug a gateway into a gateway computer (cremind tags hosts list) and "
                         "run: cremind tags devices connect\n")
        return
    places = _places(out)
    table = Table(mode, "ID", "KIND", "NAME", "SHORT ID", "STATE", "CONNECTION", "DETAIL")
    for d in devices:
        kind, cap = d.get("kind"), d.get("capacity") or {}
        detail = ""
        if kind == "bridge" and cap:
            detail = f"{cap.get('assigned')}/{cap.get('max_tags')} tags"
        elif kind == "gateway" and d.get("serves_tags"):
            # Newer gateways reach the tags near them on their own radio.
            detail = f"{cap.get('assigned')}/{cap.get('max_tags')} tags directly" if cap else "reaches tags itself"
        elif kind == "gateway" and d.get("serves_tags") is False:
            detail = "tags need a bridge"
        elif kind == "tag":
            parts = [f"{d['delivery'].get('pending_count')} pending" if d.get("delivery") else "",
                     f"via {places[d['bridge_id']]}" if d.get("bridge_id") in places else ""]
            detail = ", ".join(p for p in parts if p)
        if d.get("paused"):
            detail = (detail + ", paused").lstrip(", ")
        table.add_row(str(d.get("id") or ""), str(kind or ""), _cell(mode, d.get("name")),
                      str(d.get("short_id") or ""), str(d.get("state") or ""),
                      _cell(mode, d.get("connection_name")), _cell(mode, detail))
    table.render()


@devices_app.command("status")
@graceful_errors
def devices_status(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="A connection, search, pairing, recovery or setup session id."),
) -> None:
    """Show one gateway connection or computer search, device search, pairing, recovery or setup session."""
    from app.cli.client import tags_setup as api
    from app.cli.client._base import APIError
    from app.cli.output import print_json

    async def go(client: Any) -> dict[str, Any]:
        for fetch in (api.get_operation, api.get_pairing, api.get_discovery, api.get_recovery, api.get_session):
            try:
                return await fetch(client, ref)
            except APIError as e:
                if getattr(e, "status", None) != 404:
                    raise
        _fail(f"nothing with id '{ref}'")

    print_json(_call(ctx, go, hints=_HINTS))


# ── writers ────────────────────────────────────────────────────────────────


@devices_app.command("connect")
@graceful_errors
def devices_connect(
    ctx: typer.Context,
    host: Optional[str] = typer.Option(None, "--host", help="The gateway computer it is plugged into (id or name; "
                                                            "optional when there is only one)."),
    candidate: Optional[str] = typer.Option(None, "--candidate", help="A gateway `cremind tags hosts scan` found "
                                                                      "(when it found several)."),
    name: Optional[str] = typer.Option(None, "--name", help="A name for the gateway."),
    yes: bool = typer.Option(False, "--yes", help="Connect the gateway found without asking."),
    timeout: int = typer.Option(120, "--timeout", help="Seconds to wait for the search and for the connection."),
) -> None:
    """Connect a USB gateway plugged into a gateway computer (Cremind drives it from there)."""
    from app.cli.client import tags_setup as api
    from app.cli.output import print_json

    computer = _call(ctx, lambda c: resolve_host(c, host), hints=_HINTS)
    candidate_id = candidate
    if candidate_id is None:
        found = run_scan(ctx, computer, timeout)
        problem = scan_problem(found)
        if found.get("state") != "succeeded" or problem:
            _fail(problem_text(problem, computer) if problem else
                  f"the search did not finish: {(found.get('error') or {}).get('message') or found.get('state')}")
        usable = [c for c in _rows(found.get("candidates")) if c.get("state") == "usable"]
        if not usable:
            first = _rows(found.get("candidates"))[0]
            known = ERROR_PROBLEM.get(str(first.get("state") or ""))
            _fail(problem_text(known, computer) if known else candidate_line(first))
        if len(usable) > 1:
            listed = "\n".join(f"  {c.get('id')}  {candidate_line(c)}" for c in usable)
            _fail(f"several gateways on {host_name(computer)}; pick one with --candidate:\n{listed}")
        chosen = usable[0]
        if not yes and not typer.confirm(f"Connect {candidate_line(chosen)} on {host_name(computer)}?",
                                         default=True):
            _fail("cancelled.")
        candidate_id = str(chosen["id"])
    started = _call(ctx, lambda c: api.connect_gateway(c, computer["id"], candidate_id, name=name),
                    hints=_HINTS).get("operation") or {}
    op = wait_operation(ctx, str(started.get("id")), timeout, show=not _mode(ctx).json, hints=_HINTS)
    if _mode(ctx).json:
        print_json({"operation": op})
        return
    if op.get("state") != "succeeded":
        code = (op.get("error") or {}).get("code")
        known = ERROR_PROBLEM.get(str(code or ""))
        _fail("the gateway was not connected: " + (problem_text(known, computer) if known else
                                                    str((op.get("error") or {}).get("message") or op.get("state"))))
    if _serves_tags(ctx, op.get("companion_id")):
        sys.stdout.write(f"Gateway connected through {host_name(computer)}. It reaches the tags near it itself. "
                         "Next: cremind tags devices add tag --code-file <label.txt> (add a bridge only to reach "
                         "tags farther away)\n")
    else:
        sys.stdout.write(f"Gateway connected through {host_name(computer)}. It does not report that it reaches tags "
                         "itself, so tags need a bridge. Next: cremind tags devices add bridge --code-file "
                         "<label.txt>\n")


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
                                                                      "(when several devices — your gateway, "
                                                                      "bridges — hear the tag)."),
    name: Optional[str] = typer.Option(None, "--name", help="A name for the new device."),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait for the device."),
) -> None:
    """Add a tag or a bridge from the setup code on its label (a tag connects through your gateway itself, when it
    reaches tags, or through a bridge)."""
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
        _fail(f"nothing found ({disc.get('state')}). Check the device is powered and near "
              f"{'the gateway' if kind == 'bridge' else 'your gateway or a bridge'}.")
    candidates = _rows(disc.get("candidates"))
    eligible = [c for c in candidates if c.get("eligible")]
    if candidate is None and not eligible:
        reason = next((str(c["reason"]) for c in candidates if c.get("reason")), "not eligible")
        _fail(f"found it, but nothing that hears it can take it ({reason})."
              + (f" {_HINTS[reason]}" if reason in _HINTS else ""))
    pick = candidate or (eligible[0]["id"] if len(eligible) == 1 else None)
    if pick is None:
        lines = "\n".join(f"  {c.get('id')}  {_candidate_name(c, kind)}  rssi {c.get('rssi')}" for c in eligible)
        _fail(f"several devices hear it (recommended: {disc.get('recommended')}); choose with --candidate:\n{lines}")
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
    chosen = next((c for c in candidates if c.get("id") == pick), None)
    via = f" It connects through {_candidate_name(chosen, kind)}." if kind == "tag" and chosen else ""
    sys.stdout.write(f"{kind.capitalize()} ready.{via}\n")


@devices_app.command("import")
@graceful_errors
def devices_import(
    ctx: typer.Context,
    tag_id: str = typer.Argument(..., help="The tag id `cremind tags tools tag enroll` printed (8 hex digits)."),
    gateway: Optional[str] = typer.Option(None, "--gateway", help="The connection (gateway) the tag should use "
                                                                  "(when you have several)."),
    name: Optional[str] = typer.Option(None, "--name", help="A name for the tag."),
    timeout: int = typer.Option(300, "--timeout", help="Seconds to wait for the tag."),
) -> None:
    """Add a tag enrolled over SWD with the hardware tools (it has no setup label). Run it with the gateway
    plugged into the computer where the tag was enrolled: Cremind takes the tag's key from the hardware tools
    there and clears the tag's screen to prove it."""
    from app.cli.client import tags_setup as api
    from app.cli.output import print_json

    gateway_id = _call(ctx, lambda c: _connection(c, gateway)).get("id") if gateway else None
    out = _call(ctx, lambda c: api.import_tag(c, tag_id, gateway_id=gateway_id, name=name), hints=_HINTS)
    pairing = out.get("pairing") or {}
    sys.stdout.write("Importing — waiting for the tag to wake (about 30 s)…\n")
    final = _wait(ctx, lambda c: api.get_pairing(c, pairing["id"]),
                  lambda o: (o.get("pairing") or {}).get("state") in ("succeeded", "failed", "cancelled"), timeout,
                  lambda o: (o.get("pairing") or {}).get("stage_detail") or (o.get("pairing") or {}).get("stage"))
    if _mode(ctx).json:
        print_json(final)
        return
    p = final.get("pairing") or {}
    if p.get("state") != "succeeded":
        _fail(f"tag not added: {((p.get('error') or {}).get('message') or p.get('state'))}")
    sys.stdout.write("Tag ready.\n")


@devices_app.command("cancel")
@graceful_errors
def devices_cancel(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="A gateway connection, search, pairing or setup session id."),
) -> None:
    """Cancel an unfinished gateway connection, search, pairing or setup session."""
    from app.cli.client import tags_setup as api
    from app.cli.client._base import APIError
    from app.cli.output import print_json

    async def go(client: Any) -> dict[str, Any]:
        for cancel in (api.cancel_operation, api.cancel_pairing):
            try:
                return await cancel(client, ref)
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
        sys.stdout.write(f"{len(affected)} tag(s) used this bridge; move them to your gateway (if it reaches tags "
                         "itself) or another bridge: cremind tags devices move <tag> --to <gateway or bridge>\n")
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
    to: str = typer.Option(..., "--bridge", "--to", help="Where it connects from now on: `gateway` (the tag's own "
                                                         "gateway, when it reaches tags itself) or a ready bridge of "
                                                         "that gateway (id, name or short id)."),
) -> None:
    """Serve a tag through its gateway or another bridge of the same gateway."""
    from app.cli.client import tags_setup as api
    from app.cli.output import print_json

    async def go(client: Any) -> dict[str, Any]:
        items = _devices(await api.connections(client))
        t = _find([d for d in items if d.get("kind") == "tag"], tag, what="tag")
        # Only the tag's own connection: its gateway and that gateway's bridges.
        places = [d for d in items
                  if d.get("kind") in ("gateway", "bridge") and d.get("connection") == t.get("connection")]
        if to.strip().lower() == "gateway":
            target = next((d for d in places if d.get("kind") == "gateway"), None)
            if target is None:
                _fail("the tag's gateway is not listed: cremind tags devices list")
        else:
            target = _find(places, to, what="gateway or bridge of the tag's connection")
        return await api.move(client, t["id"], target["id"])

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
    connection: str = typer.Argument(..., help="The connection (gateway) to move."),
    host: Optional[str] = typer.Option(None, "--host", help="The gateway computer it is plugged into now (id or "
                                                            "name; optional when there is only one)."),
    yes: bool = typer.Option(False, "--yes", help="Move it without asking."),
    timeout: int = typer.Option(120, "--timeout", help="Seconds to wait for the search and for the move."),
) -> None:
    """Move a gateway, with its bridges and tags, to another gateway computer (plug it in there first)."""
    from app.cli.client import tags_setup as api
    from app.cli.output import print_json

    conn = _call(ctx, lambda c: _connection(c, connection))
    title = str(conn.get("name") or connection)
    computer = _call(ctx, lambda c: resolve_host(c, host), hints=_HINTS)
    device = (conn.get("gateway") or {}).get("device_id")
    found = run_scan(ctx, computer, timeout)
    ours = next((c for c in _rows(found.get("candidates")) if c.get("device_id") == device), None)
    if ours is None:
        problem = scan_problem(found)
        _fail(problem_text(problem, computer) if problem and problem != "no_gateway" else
              f"{title} is not plugged into {host_name(computer)}. Plug it in there, then run this again.")
    if ours.get("state") == "already_connected":
        sys.stdout.write(f"{title} already works through {host_name(computer)}; nothing to move.\n")
        return
    if ours.get("state") != "recovery_required":
        known = ERROR_PROBLEM.get(str(ours.get("state") or ""))
        _fail(problem_text(known, computer) if known else candidate_line(ours))
    if not yes and not typer.confirm(f"Move {title} and its bridges and tags to {host_name(computer)}? The computer it "
                                     "used until now loses access at once.", default=False):
        _fail("cancelled.")
    started = _call(ctx, lambda c: api.connect_gateway(c, computer["id"], str(ours["id"]), recover=True),
                    hints=_HINTS).get("operation") or {}
    op = wait_operation(ctx, str(started.get("id")), timeout, show=not _mode(ctx).json, hints=_HINTS)
    if _mode(ctx).json:
        print_json({"operation": op})
        return
    if op.get("state") != "succeeded":
        _fail(f"{title} was not moved: {(op.get('error') or {}).get('message') or op.get('state')}")
    sys.stdout.write(f"{title} now works through {host_name(computer)}. Bridges and tags move over as they wake: "
                     f"cremind tags devices status {op.get('recovery_id')}\n")


__all__ = ["devices_app"]
