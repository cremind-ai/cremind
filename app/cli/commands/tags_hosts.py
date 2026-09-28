"""`cremind tags hosts ...` — the gateway computers: the computers whose USB
ports Cremind drives this profile's gateways from (Settings → Tags, "Gateway
computers").

- ``list`` / ``show <computer>``: each computer's readiness (its components and
  USB access, as the computer itself reports them), state, and the gateways it
  sees;
- ``scan <computer>``: search its USB ports (what ``cremind tags devices
  connect`` does first);
- ``prepare <computer>`` (admin): install the server's gateway components;
- ``access <computer> <profile> --allow|--deny`` (admin): let another profile
  connect gateways plugged into the server.

A computer is named by its id or its name. A search always runs on the
computer named — never silently on the one the CLI runs on — and its results
are opaque ids bound to this profile and that computer, never port names.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Optional

import typer

from app.cli.commands._helpers import graceful_errors
from app.cli.commands.tags import _call, _cell, _fail, _match, _mode, _rows

hosts_app = typer.Typer(
    name="hosts",
    help="The computers Cremind drives your gateways from: list them, search one's USB ports, prepare, share.",
    no_args_is_help=True,
)

POLL_S = 1.5
LIST_CMD = "cremind tags hosts list"
HINTS = {
    "simple_setup_disabled": "The admin has not turned on hardware setup from Settings → Tags on this server.",
    "host_not_found": f"List the computers: {LIST_CMD}",
    "host_access_denied": "The admin has not allowed this profile on that computer's USB ports: "
                          "cremind -p admin tags hosts access <computer> <profile> --allow",
    "host_offline": "That computer is not reachable: make sure it is on and Cremind is running there.",
    "host_not_running": "Gateway support is not running on that computer; restart Cremind there.",
    "host_busy": "Another Cremind on that computer drives its gateways.",
    "components_unavailable": "Its gateway components are missing: cremind -p admin tags hosts prepare <computer>",
    "candidate_expired": "That search result is too old; search again: cremind tags hosts scan <computer>",
    "candidate_not_found": "Search again: cremind tags hosts scan <computer>",
    "owned_elsewhere": "That gateway belongs to another Cremind or profile: remove it there, or reset it.",
    "recovery_required": "That gateway is yours, driven from another computer: "
                         "cremind tags devices recover <connection> --host <computer>",
    "already_claiming": "The gateway is being claimed; remove it afterwards to undo: cremind tags devices remove",
    "admin_required": "Run it as the admin: cremind -p admin tags hosts …",
    "host_not_shareable": "A computer set up from a profile stays that profile's.",
    "profile_not_found": "List profiles: cremind profile list",
}

# The things that can stand between a person and a connected gateway (the Settings page uses the same words).
PROBLEMS = {
    "no_gateway": ("No gateway detected",
                   "Cremind did not find a gateway on {name}'s USB ports. Check that it is plugged in with a data "
                   "cable (some cables only charge), then search again."),
    "usb_access_denied": ("USB access denied", ""),
    "gateway_busy": ("Gateway busy in another application",
                     "Another program on {name} is using the gateway: a serial monitor, a firmware tool, or an older "
                     "Cremind Connect. Close it, then search again."),
    "unsupported_firmware": ("Unsupported firmware",
                             "This gateway runs older firmware that Cremind cannot set up. Update its firmware, "
                             "then search again."),
    "components_unavailable": ("Required components unavailable",
                               "{name} is missing the components Cremind needs to drive gateways. The admin "
                               "prepares them: cremind -p admin tags hosts prepare {ref}"),
    "host_offline": ("Computer offline",
                     "{name} is not reachable right now. Make sure it is on and that Cremind is running there."),
    "owned_elsewhere": ("Gateway owned elsewhere",
                        "This gateway already belongs to another Cremind or profile. Remove it there, or reset it "
                        "(hold its button while plugging it in) to set it up here."),
    "recovery_required": ("Recovery required",
                          "This gateway is yours, connected through another computer. Move it to {name}: "
                          "cremind tags devices recover <connection> --host {ref}"),
}
ERROR_PROBLEM = {
    "gateway_not_found": "no_gateway", "usb_access_denied": "usb_access_denied", "access_denied": "usb_access_denied",
    "gateway_busy": "gateway_busy", "busy": "gateway_busy", "unsupported_firmware": "unsupported_firmware",
    "v1_firmware": "unsupported_firmware", "components_unavailable": "components_unavailable",
    "host_offline": "host_offline", "host_not_running": "host_offline", "owned_elsewhere": "owned_elsewhere",
    "recovery_required": "recovery_required",
}
TERMINAL = ("succeeded", "failed", "cancelled")


def host_name(host: dict[str, Any]) -> str:
    return str(host.get("name") or ("the Cremind server" if host.get("kind") == "server" else "that computer"))


def _usb_text(host: dict[str, Any]) -> str:
    name = host_name(host)
    if (host.get("usb") or {}).get("container"):
        return (f"Cremind runs in a container on {name}, so it cannot open USB ports there. Map the gateway's USB "
                "device into the container (see \"Gateways and containers\" in the Cremind guide), then search again.")
    return {
        "linux": f"{name} does not let Cremind open the gateway's USB port. Add the account Cremind runs as to the "
                 "\"dialout\" group, sign out and back in, then search again.",
        "windows": "Windows refused access to the gateway's USB port. Close other programs that may use it, unplug "
                   "the gateway and plug it back in, then search again.",
        "macos": "macOS refused access to the gateway's USB port. Unplug the gateway, plug it back in, then search "
                 "again.",
    }.get(str(host.get("platform") or ""), f"{name} refused access to the gateway's USB port. Unplug the gateway, "
                                           "plug it back in, then search again.")


def problem_text(problem: str, host: dict[str, Any]) -> str:
    """``Title: what to do`` for one of the eight problems, naming the computer."""
    title, text = PROBLEMS[problem]
    if problem == "usb_access_denied":
        text = _usb_text(host)
    ref = str(host.get("name") or host.get("id") or "<computer>")
    return f"{title}: " + text.format(name=host_name(host), ref=ref if " " not in ref else f'"{ref}"')


def scan_problem(op: dict[str, Any]) -> Optional[str]:
    """What a finished search that found nothing means (``None``: it found something)."""
    if op.get("state") == "failed":
        return ERROR_PROBLEM.get(str((op.get("error") or {}).get("code") or ""))
    if _rows(op.get("candidates")):
        return None
    reasons = {str(p.get("reason") or "") for p in _rows(op.get("ports"))}
    for reason, problem in (("no_access", "usb_access_denied"), ("busy", "gateway_busy"),
                            ("v1_firmware", "unsupported_firmware")):
        if reason in reasons:
            return problem
    return "no_gateway"


def host_block(host: dict[str, Any]) -> Optional[str]:
    """Why a computer cannot search right now, in words (``None``: it can)."""
    if not (host.get("access") or {}).get("can_use"):
        return str((host.get("access") or {}).get("reason") or "This profile may not use that computer.")
    if not host.get("online"):
        return problem_text("host_offline", host)
    state = host.get("state")
    if state == "unavailable":
        return problem_text("components_unavailable", host)
    if state == "running" and (host.get("usb") or {}).get("available") is False:
        return problem_text("usb_access_denied", host)
    if state in ("running", "paused"):
        return None
    return f"Gateway support is not running on {host_name(host)} ({host.get('reason') or state})."


async def resolve_host(client: Any, ref: Optional[str]) -> dict[str, Any]:
    """The computer named by ``ref`` (id or name); with none, the only one this profile may use."""
    from app.cli.client import tags_setup as api

    items = _rows((await api.hosts(client)).get("hosts"))
    if ref is not None:
        return _match(items, ref, what="gateway computer", list_cmd=LIST_CMD, fields=("name",))
    usable = [h for h in items if (h.get("access") or {}).get("can_use")]
    if len(usable) == 1:
        return usable[0]
    if not usable:
        reasons = {str((h.get("access") or {}).get("reason") or "") for h in items} - {""}
        _fail("no computer can drive a gateway for this profile yet"
              + (f": {reasons.pop()}" if len(reasons) == 1 else f". List them: {LIST_CMD}"))
    listed = "\n".join(f"  {h.get('id')}  {h.get('name')}" for h in usable)
    _fail(f"several gateway computers: pick one with --host <id or name>:\n{listed}")


def wait_operation(ctx: typer.Context, op_id: str, timeout: float, *, show: bool = True,
                   hints: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """Follow a search, connection or preparation to its end (printing each new step)."""
    from app.cli.client import tags_setup as api

    deadline = time.monotonic() + timeout
    last = None
    while True:
        op = _call(ctx, lambda c: api.get_operation(c, op_id), hints=hints or HINTS).get("operation") or {}
        line = op.get("stage_detail")
        if show and line and line != last:
            sys.stdout.write(f"{str(line).rstrip('…').rstrip()}…\n")
            last = line
        if op.get("state") in TERMINAL:
            return op
        if time.monotonic() > deadline:
            _fail(f"timed out waiting; it goes on on the server: cremind tags devices status {op_id}")
        time.sleep(POLL_S)


def run_scan(ctx: typer.Context, host: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Search ``host``'s USB ports and wait for what it found."""
    from app.cli.client import tags_setup as api

    blocked = host_block(host)
    if blocked:
        _fail(blocked)
    sys.stdout.write(f"Searching {host_name(host)}'s USB ports…\n")
    started = _call(ctx, lambda c: api.scan_host(c, host["id"]), hints=HINTS).get("operation") or {}
    return wait_operation(ctx, str(started.get("id")), timeout, show=False)


def candidate_line(c: dict[str, Any]) -> str:
    fw = f", firmware {c['fw']}" if c.get("fw") else ""
    return f"gateway …{str(c.get('short_id') or '')[-4:]}{fw}: {c.get('message') or c.get('state')}"


# ── readers ────────────────────────────────────────────────────────────────


@hosts_app.command("list")
@graceful_errors
def hosts_list(ctx: typer.Context) -> None:
    """The computers this profile may connect gateways on, and whether each is ready."""
    from app.cli.client import tags_setup as api
    from app.cli.output import Table, print_json

    mode = _mode(ctx)
    out = _call(ctx, api.hosts, hints=HINTS)
    if mode.json:
        print_json(out)
        return
    items = _rows(out.get("hosts"))
    if not items:
        sys.stdout.write("This server reports no gateway computer. Update Cremind to use gateways with it.\n")
        return
    table = Table(mode, "ID", "NAME", "KIND", "STATE", "READY", "USB", "ACCESS", "YOURS")
    for h in items:
        access = h.get("access") or {}
        usb = h.get("usb") or {}
        table.add_row(str(h.get("id") or ""), _cell(mode, h.get("name")), str(h.get("kind") or ""),
                      str(h.get("state") if h.get("online") else "offline"),
                      str((h.get("readiness") or {}).get("state") or ""),
                      "container" if usb.get("container") and not usb.get("available")
                      else ("yes" if usb.get("available") else "no" if usb else ""),
                      "manage" if access.get("can_manage") else "yes" if access.get("can_use") else "no",
                      str(h.get("connections") or 0))
    table.render()
    for h in items:
        blocked = host_block(h)
        if blocked and (h.get("access") or {}).get("can_use"):
            sys.stdout.write(f"{h.get('name')}: {blocked}\n")


@hosts_app.command("show")
@graceful_errors
def hosts_show(
    ctx: typer.Context,
    computer: str = typer.Argument(..., help="The computer: its id or name."),
) -> None:
    """One computer in detail: components, USB access, the gateways it sees, who may use it."""
    from app.cli.output import print_json

    host = _call(ctx, lambda c: resolve_host(c, computer), hints=HINTS)
    if _mode(ctx).json:
        print_json(host)
        return
    access = host.get("access") or {}
    lines = [f"{host.get('name')} ({host.get('kind')}, {host.get('platform') or 'unknown system'}, "
             f"Cremind {host.get('version') or '?'})",
             f"State:      {host.get('state') if host.get('online') else 'offline'}"
             + (f" — {host.get('reason')}" if host.get("reason") else "")]
    readiness = host.get("readiness") or {}
    if readiness:
        lines.append(f"Components: {readiness.get('state')}")
        lines += [f"  {c.get('key')}: {c.get('state')}" + (f" — {c.get('detail')}" if c.get("detail") else "")
                  for c in _rows(readiness.get("components"))]
    usb = host.get("usb") or {}
    if usb:
        lines.append("USB:        " + ("available" if usb.get("available") else "not available")
                     + (" (in a container)" if usb.get("container") else "")
                     + (f" — {usb.get('reason')}" if usb.get("reason") else ""))
    gateways = _rows(host.get("gateways"))
    lines.append(f"Gateways:   {len(gateways)} seen" + ("" if not gateways else ": " + ", ".join(
        f"…{str(g.get('short_id') or '')[-4:]}{' (in use)' if g.get('in_use') else ''}" for g in gateways)))
    lines.append("Access:     " + ("admin (manages who may use it)" if access.get("can_manage")
                                   else "yes" if access.get("can_use") else f"no — {access.get('reason')}"))
    if access.get("profiles") is not None:
        allowed = [p.get("profile") for p in _rows(access.get("profiles")) if p.get("granted")]
        lines.append("Allowed:    " + (", ".join(str(p) for p in allowed) if allowed else "no other profile"))
    blocked = host_block(host)
    if blocked and access.get("can_use"):
        lines.append(blocked)
    sys.stdout.write("\n".join(lines) + "\n")


# ── writers ────────────────────────────────────────────────────────────────


@hosts_app.command("scan")
@graceful_errors
def hosts_scan(
    ctx: typer.Context,
    computer: Optional[str] = typer.Argument(None, help="The computer (id or name); optional when there is one."),
    timeout: int = typer.Option(60, "--timeout", help="Seconds to wait for the search."),
) -> None:
    """Search a computer's USB ports for gateways (nothing is claimed)."""
    from app.cli.output import Table, print_json

    mode = _mode(ctx)
    host = _call(ctx, lambda c: resolve_host(c, computer), hints=HINTS)
    op = run_scan(ctx, host, timeout)
    if mode.json:
        print_json({"operation": op})
        return
    problem = scan_problem(op)
    if op.get("state") != "succeeded" or problem:
        _fail(problem_text(problem, host) if problem else
              f"the search did not finish: {(op.get('error') or {}).get('message') or op.get('state')}")
    table = Table(mode, "CANDIDATE", "GATEWAY", "STATE", "DETAIL")
    for c in _rows(op.get("candidates")):
        table.add_row(str(c.get("id") or ""), f"…{str(c.get('short_id') or '')[-4:]}", str(c.get("state") or ""),
                      _cell(mode, c.get("message")))
    table.render()
    sys.stdout.write(f"Connect one: cremind tags devices connect --host {host.get('id')} --candidate <CANDIDATE>\n")


@hosts_app.command("prepare")
@graceful_errors
def hosts_prepare(
    ctx: typer.Context,
    computer: Optional[str] = typer.Argument(None, help="The Cremind server's computer (id or name)."),
    timeout: int = typer.Option(1800, "--timeout", help="Seconds to wait (downloads can take a few minutes)."),
) -> None:
    """Install the gateway components on the Cremind server's computer (admin)."""
    from app.cli.client import tags_setup as api
    from app.cli.output import print_json

    host = _call(ctx, lambda c: resolve_host(c, computer), hints=HINTS)
    started = _call(ctx, lambda c: api.prepare_host(c, host["id"]), hints=HINTS).get("operation") or {}
    op_id = str(started.get("id"))
    deadline = time.monotonic() + timeout
    last: Optional[str] = None
    while True:
        op = _call(ctx, lambda c: api.get_operation(c, op_id), hints=HINTS).get("operation") or {}
        log = [str(line) for line in op.get("log") or []]
        # The server keeps the latest lines only: print what follows the last line printed.
        fresh = log[len(log) - log[::-1].index(last):] if last in log else log
        if not _mode(ctx).json:
            for line in fresh:
                sys.stdout.write(line + "\n")
        last = log[-1] if log else last
        if op.get("state") in TERMINAL:
            break
        if time.monotonic() > deadline:
            _fail(f"timed out waiting; it goes on on the server: cremind tags devices status {op_id}")
        time.sleep(POLL_S)
    if _mode(ctx).json:
        print_json({"operation": op})
        return
    if op.get("state") != "succeeded":
        _fail(f"the components were not prepared: {(op.get('error') or {}).get('message') or op.get('state')}")
    sys.stdout.write(f"{host_name(host)} is ready to drive gateways.\n")


@hosts_app.command("access")
@graceful_errors
def hosts_access(
    ctx: typer.Context,
    computer: str = typer.Argument(..., help="The Cremind server's computer (id or name)."),
    profile: str = typer.Argument(..., help="The profile to allow or stop."),
    allow: Optional[bool] = typer.Option(None, "--allow/--deny", help="Allow the profile, or stop it."),
) -> None:
    """Let another profile connect gateways plugged into the Cremind server, or stop it (admin)."""
    from app.cli.client import tags_setup as api
    from app.cli.output import print_json

    if allow is None:
        _fail("say which: --allow or --deny")
    host = _call(ctx, lambda c: resolve_host(c, computer), hints=HINTS)
    profiles = (host.get("access") or {}).get("profiles")
    if profiles is None:
        _fail("only the admin manages who may use this computer: cremind -p admin tags hosts access …")
    target = _match([{**p, "id": p.get("profile_id"), "name": p.get("profile")} for p in _rows(profiles)], profile,
                    what="profile", list_cmd=f"cremind tags hosts show {computer}", fields=("name",))
    out = _call(ctx, lambda c: api.set_host_access(c, host["id"], str(target.get("profile_id")), bool(allow)),
                hints=HINTS)
    if _mode(ctx).json:
        print_json(out)
        return
    sys.stdout.write(f"{out.get('profile')} {'may now' if allow else 'may no longer'} connect gateways plugged "
                     f"into {host_name(host)}.\n")


__all__ = ["hosts_app"]
