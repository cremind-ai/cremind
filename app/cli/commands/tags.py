"""`cremind tags ...` — Cremind Tag e-paper displays.

Mirrors the **Tags** sidebar page (the tags this profile owns, what they show,
pinned notes, delivery history) and **Settings → Tags** (which cards go to which
tag, content credentials). The `hardware` sub-app mirrors the admin-only part of
Settings → Tags: companions, the gateway/bridge/tag inventory, hardware
operations, who owns which tag, and the defaults every profile inherits.

A tag is addressed by its id, a unique id prefix (4+ characters), its hardware
id, or its name; a full id is sent as-is, anything else is looked up first.
Timestamps are epoch milliseconds on the wire and local time in tables.
Secrets (connector credentials) are printed once, by the command that creates
them, and never again.

Every read verb here is a plain reader (`list`, `show`, `settings`,
`status`), because the plan-mode planner may run readers on its own; every
write uses a verb that is not on that list.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from typing import Any, Awaitable, Callable, NoReturn, Optional

import typer

from app.cli.commands._helpers import graceful_errors

tags_app = typer.Typer(
    name="tags",
    help="Cremind Tag e-paper displays: your tags, pinned notes, routing settings, delivery history.",
    no_args_is_help=True,
)
deliveries_app = typer.Typer(
    name="deliveries",
    help="What was sent to your tags: history, one delivery in detail, cancel one.",
    no_args_is_help=True,
)
credentials_app = typer.Typer(
    name="credentials",
    help="Content credentials that let a companion fetch this profile's cards.",
    no_args_is_help=True,
)
hardware_app = typer.Typer(
    name="hardware",
    help="Companions, gateways, bridges and tag ownership (admin profile).",
    no_args_is_help=True,
)
tags_app.add_typer(deliveries_app, name="deliveries")
tags_app.add_typer(credentials_app, name="credentials")
tags_app.add_typer(hardware_app, name="hardware")


# ── helpers ────────────────────────────────────────────────────────────────

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_MIN_PREFIX = 4

# Hints for the Tags API's error codes, printed under the server's own message.
_HINTS = {
    "otp_refused": "Nothing was sent: one-time codes are never shown on a tag. Remove the code and try again.",
    "clear_pending": "The tag is still being blanked after a change of owner. Try again in a minute "
                     "(`cremind tags show <tag>` shows clear_required until it is done).",
    "device_not_found": "List your tags: cremind tags list",
    "no_preview": "The companion uploads a preview after it renders the tag's screen.",
    "delivery_not_found": "List deliveries: cremind tags deliveries list",
    "already_terminal": "Nothing to cancel.",
    "credential_not_found": "List credentials: cremind tags credentials list",
    "companion_not_found": "List companions: cremind tags companions",
    "invalid_icon": "`cremind tags settings` lists the icons.",
}
_ADMIN_HINTS = {
    **_HINTS,
    "device_not_found": "List devices: cremind tags hardware list",
    "companion_not_found": "List companions: cremind tags hardware list",
    "command_not_found": "Recent commands: cremind tags hardware list",
    "bridge_required": "Pick one of the companion's bridges (KIND bridge in `cremind tags hardware list`) "
                       "and pass --bridge <bridge>.",
    "bridge_not_found": "The bridge must be on the same companion as the tag: cremind tags hardware list",
    "unknown_profile": "List profiles: cremind profile list",
    "use_tag_endpoint": "Use `cremind tags hardware claim`, `assign` or `release` instead.",
    "tag_owned": "Release it first: cremind tags hardware release <tag>",
    "bridge_full": "Every slot of that bridge's table is taken (TAGS in `cremind tags hardware list`). Pick a "
                   "bridge with room (--bridge <bridge>), or move a tag off it "
                   "(cremind tags hardware assign <tag> --bridge <other>) or forget one it no longer serves.",
    "unknown_command": "Kinds: scan_unprovisioned, provision_bridge, configure_bridge, remove_bridge, "
                       "identify, refresh_tag, install_fontpack, collect_diagnostics.",
}

CLEAR_FAILED = "clear_failed"
_CLEAR_FAILED_HINT = (
    "clear_failed: the tag could not be blanked after it changed owner (3 attempts). "
    "Ask the admin to claim or release it again (cremind tags hardware claim|release <tag>)."
)
_CLEAR_FAILED_ADMIN_HINT = (
    "clear_failed: the tag could not be blanked after a change of owner (3 attempts). Retry by claiming "
    "it again (cremind tags hardware claim <tag> --owner <profile>) or releasing it "
    "(cremind tags hardware release <tag>)."
)
ASSIGN_FAILED = "assign_failed"
_ASSIGN_FAILED_HINT = (
    "assign_failed: the tag's bridge could not take it (bridge_full: every slot of its table is taken). "
    "Ask the admin to assign it to another bridge (cremind tags hardware assign <tag> --bridge <bridge>)."
)
_ASSIGN_FAILED_ADMIN_HINT = (
    "assign_failed: the bridge could not take the tag — its assign_tag command's ERROR says why "
    "(bridge_full: every slot of its table is taken; see TAGS). Assign it to a bridge with room "
    "(cremind tags hardware assign <tag> --bridge <bridge>) or release it (cremind tags hardware release <tag>)."
)

_ADMIN_REQUIRED = (
    "This needs the admin profile: `cremind tags hardware` manages hardware shared by every profile.\n"
    "Run it as admin, e.g. `cremind -p admin tags hardware list`."
)


def _fail(message: str, code: int = 1) -> NoReturn:
    sys.stderr.write(message.rstrip("\n") + "\n")
    raise typer.Exit(code=code)


def _api_detail(e: Any) -> Optional[dict[str, Any]]:
    import json

    try:
        detail = json.loads(getattr(e, "raw", b"") or b"")
    except (ValueError, TypeError):
        return None
    return detail if isinstance(detail, dict) else None


def _explain(e: Any, *, admin: bool, hints: Optional[dict[str, str]] = None) -> None:
    """Print a Tags API error (code, message, field details, hint) and exit 1.

    `hints` overrides the table for this one call (a hint that names the
    user's own argument). Returns normally for a body it does not recognise,
    so the caller re-raises and `graceful_errors` prints the generic line.
    """
    if admin and getattr(e, "status", None) == 403:
        _fail(_ADMIN_REQUIRED)
    detail = _api_detail(e)
    if not detail or not isinstance(detail.get("error"), str):
        return
    code = detail["error"]
    message = detail.get("message") or detail.get("detail") or ""
    head = f"server returned {e.status}: {code}"
    if isinstance(message, str) and message and message != code:
        head += f": {message}"
    lines = [head]
    details = detail.get("details")
    if isinstance(details, dict):
        lines += [f"  {field}: {why}" for field, why in details.items()]
    hint = (hints or {}).get(code) or (_ADMIN_HINTS if admin else _HINTS).get(code)
    if hint:
        lines.append(hint)
    _fail("\n".join(lines))


def _call(ctx: typer.Context, fn: Callable[[Any], Awaitable[Any]], *, admin: bool = False,
          hints: Optional[dict[str, str]] = None) -> Any:
    """Run `fn(client)` against the server; explain a Tags API error and exit 1."""
    import asyncio

    from app.cli.client._base import APIError, Client
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _go() -> Any:
        async with Client(cfg) as client:
            return await fn(client)

    try:
        return asyncio.run(_go())
    except APIError as e:
        _explain(e, admin=admin, hints=hints)
        raise


def _mode(ctx: typer.Context):
    return ctx.obj["mode"]


def _fmt_ts(value: Any) -> str:
    """Epoch **milliseconds** -> local `YYYY-MM-DD HH:MM:SS` (blank if unset)."""
    if value in (None, ""):
        return ""
    try:
        return datetime.fromtimestamp(float(value) / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _cell(mode: Any, value: Any) -> str:
    """A user-controlled string for a table cell. The rich table reads markup,
    so a tag named `[bold]x` would lose its brackets; TSV output stays raw."""
    from app.cli.output.console import is_tty

    if isinstance(value, bool):
        return "true" if value else "false"
    text = "" if value is None else str(value)
    if is_tty() and not mode.no_color:
        from rich.markup import escape

        return escape(text)
    return text


def _battery(mv: Any) -> str:
    try:
        return f"{float(mv) / 1000.0:.2f} V" if mv else ""
    except (TypeError, ValueError):
        return ""


def _screen(device: dict[str, Any]) -> str:
    if device.get("clear_required"):
        return "clearing"
    shown = int(device.get("displayed_revision") or 0)
    wanted = int(device.get("desired_revision") or 0)
    if wanted > shown:
        return f"rev {shown} ({wanted} pending)"
    return f"rev {shown}"


def _label(device: dict[str, Any]) -> str:
    """How a device is named in a sentence: its name, else its hardware id, else its id."""
    return str(device.get("name") or device.get("hw_id") or device.get("id") or "")


def _shell_arg(value: str) -> str:
    """`value` as one shell word in a suggested command (quoted when it has to be)."""
    return value if re.fullmatch(r"[\w.:@/+-]+", value) else '"' + value.replace('"', '\\"') + '"'


def _rows(value: Any) -> list[dict[str, Any]]:
    return [r for r in (value or []) if isinstance(r, dict)]


def _match(items: list[dict[str, Any]], ref: str, *, what: str, list_cmd: str,
           fields: tuple[str, ...] = ("hw_id", "name")) -> dict[str, Any]:
    """One item named by `ref`: its id, a unique id prefix, or an exact field value."""
    wanted = ref.strip()
    low = wanted.lower()
    if not low:
        _fail(f"name a {what} (id, name{', hardware id' if 'hw_id' in fields else ''}). List them: {list_cmd}")
    for item in items:
        if str(item.get("id") or "") == wanted:
            return item

    def pick(hits: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            listed = ", ".join(f"{h.get('id')} ({_label(h)})" for h in hits[:6])
            _fail(f"'{ref}' matches several {what}s: {listed}. Pass the full id.")
        return None

    for field in fields:
        found = pick([i for i in items if str(i.get(field) or "").lower() == low])
        if found is not None:
            return found
    if len(low) >= _MIN_PREFIX:
        found = pick([i for i in items if str(i.get("id") or "").lower().startswith(low)])
        if found is not None:
            return found
    _fail(f"no {what} matches '{ref}'. List them: {list_cmd}")


async def _owned_tag(client: Any, ref: str, *, overview: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """One of this profile's tags, by id / prefix / hardware id / name."""
    if _UUID_RE.match(ref.strip()):
        return {"id": ref.strip()}
    from app.cli.client import tags as api

    data = overview if overview is not None else await api.overview(client)
    return _match(_rows(data.get("devices")), ref, what="tag", list_cmd="cremind tags list")


async def _hardware_device(client: Any, ref: str, *, kind: Optional[str] = None,
                           inventory: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Any device (admin), optionally of one kind, by id / prefix / hardware id / name."""
    if _UUID_RE.match(ref.strip()):
        return {"id": ref.strip()}
    from app.cli.client import tags as api

    inv = inventory if inventory is not None else await api.hardware_inventory(client)
    devices = [d for d in _rows(inv.get("devices")) if kind is None or d.get("kind") == kind]
    return _match(devices, ref, what=kind or "device", list_cmd="cremind tags hardware list")


async def _companion(client: Any, ref: Optional[str], *, admin: bool,
                     inventory: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """A companion by id / prefix / name; with no `ref`, the only one there is."""
    from app.cli.client import tags as api

    if ref is not None and _UUID_RE.match(ref.strip()):
        return {"id": ref.strip()}
    if admin:
        inv = inventory if inventory is not None else await api.hardware_inventory(client)
        companions = _rows(inv.get("companions"))
        list_cmd = "cremind tags hardware list"
    else:
        companions = _rows((await api.list_companions(client)).get("companions"))
        list_cmd = "cremind tags companions"
    if ref is None:
        if len(companions) == 1:
            return companions[0]
        if not companions:
            _fail("no companion is registered yet — the admin registers one: "
                  "cremind tags hardware register <name>")
        listed = "\n".join(f"  {c.get('id')}  {c.get('name')}" for c in companions)
        _fail(f"several companions are registered; pick one with --companion <id or name>:\n{listed}")
    return _match(companions, ref, what="companion", list_cmd=list_cmd, fields=("name",))


def _parse_duration(text: str, flag: str) -> int:
    """`90`, `90s`, `30m`, `2h`, `7d` -> seconds."""
    m = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", (text or "").lower())
    if not m:
        _fail(f"{flag} takes seconds or a number with s/m/h/d, e.g. 90, 30m, 2h, 7d")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def _read_text_file(path: str, flag: str) -> str:
    if path == "-":
        return sys.stdin.read()
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError as e:
        _fail(f"{flag}: {e}")


def _print_secret(out: dict[str, Any], *, who: str) -> None:
    """The create/rotate secret — printed once, with the warning that goes with it."""
    cred = out.get("credential") if isinstance(out.get("credential"), dict) else {}
    authorization = str(out.get("authorization") or "")
    sys.stdout.write(f"credential: {cred.get('id', '')} ({cred.get('kind', '')}, label '{cred.get('label', '')}')\n")
    sys.stdout.write("\nAuthorization value for the companion:\n\n")
    sys.stdout.write(f"  {authorization}\n\n")
    sys.stderr.write(
        f"This secret is shown ONCE — Cremind keeps only a hash of it. Give it to {who} now\n"
        "(`cremind-tag connect`). Lost it? Revoke the credential and create a new one.\n"
    )


# ── option flags shared by `set` and `hardware set-defaults` ───────────────

_INHERIT = object()

_OPTION_KEYS = {
    "layout": "layout",
    "excerpts": "show_excerpts",
    "show_excerpts": "show_excerpts",
    "qr_links": "qr_links",
    "progress_cadence": "progress_cadence_s",
    "progress_cadence_s": "progress_cadence_s",
    "language": "language",
    "timezone": "timezone",
    "routes": "routes",
}


def _option_changes(*, layout: Optional[str], excerpts: Optional[bool], qr_links: Optional[bool],
                    progress_cadence: Optional[int], language: Optional[str],
                    timezone: Optional[str]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if layout is not None:
        changes["layout"] = layout
    if excerpts is not None:
        changes["show_excerpts"] = excerpts
    if qr_links is not None:
        changes["qr_links"] = qr_links
    if progress_cadence is not None:
        changes["progress_cadence_s"] = progress_cadence
    if language is not None:
        changes["language"] = language
    if timezone is not None:
        tz = timezone.strip()
        changes["timezone"] = "" if tz.lower() == "own" else tz
    return changes


def _inherit_keys(values: Optional[list[str]]) -> list[str]:
    keys = []
    for raw in values or []:
        key = _OPTION_KEYS.get(raw.strip().lower().replace("-", "_"))
        if key is None:
            _fail(f"--inherit {raw}: unknown setting. Settings: layout, excerpts, qr-links, "
                  "progress-cadence, language, timezone, routes")
        keys.append(key)
    return keys


def _route_specs(values: Optional[list[str]]) -> dict[str, Any]:
    """`KIND=all|none|inherit|TAG[,TAG…]` -> {kind: "all"|"none"|_INHERIT|[refs]}."""
    specs: dict[str, Any] = {}
    for raw in values or []:
        kind, sep, value = raw.partition("=")
        kind = kind.strip().lower().replace("-", "_")
        value = value.strip()
        if not sep or not kind or not value:
            _fail(f"--route {raw}: write KIND=all, KIND=none, KIND=inherit or KIND=<tag>[,<tag>...]")
        low = value.lower()
        if low in ("all", "none"):
            specs[kind] = low
        elif low == "inherit":
            specs[kind] = _INHERIT
        else:
            specs[kind] = [v.strip() for v in value.split(",") if v.strip()]
    return specs


def _check_no_clash(changes: dict[str, Any], specs: dict[str, Any], inherit: list[str]) -> None:
    """One PATCH cannot both set a setting and inherit it."""
    clash = sorted({k for k in inherit if k in changes or (k == "routes" and specs)})
    if clash:
        _fail(f"--inherit {', '.join(clash)} contradicts a value given for it in the same command; "
              "pass one or the other.")


def _needs_lookup(specs: dict[str, Any]) -> bool:
    """Whether a `--route` names a tag by anything but its full id."""
    return any(isinstance(v, list) and not all(_UUID_RE.match(r) for r in v) for v in specs.values())


async def _options_patch(changes: dict[str, Any], specs: dict[str, Any], inherit: list[str],
                         resolve: Callable[[str], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """The partial options object the PATCH routes merge: values given
    replace, `None` drops an override (inherit again), `routes` is per kind.
    Card kinds and values are validated by the server (422 invalid_settings)."""
    patch: dict[str, Any] = dict(changes)
    for key in inherit:
        patch[key] = None
    if specs:
        routes: dict[str, Any] = {}
        for kind, value in specs.items():
            if value is _INHERIT:
                routes[kind] = None
            elif isinstance(value, list):
                routes[kind] = [(await resolve(ref))["id"] for ref in value]
            else:
                routes[kind] = value
        patch["routes"] = routes
    return patch


def _route_text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "(no tags)"
    return "" if value is None else str(value)


def _render_settings(mode: Any, out: dict[str, Any]) -> None:
    from app.cli.output import Table, print_kv

    own = out.get("options") if isinstance(out.get("options"), dict) else {}
    defaults = out.get("defaults") if isinstance(out.get("defaults"), dict) else {}
    effective = out.get("effective") if isinstance(out.get("effective"), dict) else {}

    def source(key: str, layer_own: dict[str, Any], layer_admin: dict[str, Any]) -> str:
        if key in layer_own:
            return "profile"
        if key in layer_admin:
            return "admin default"
        return "built-in"

    print_kv([
        ("profile", str(out.get("profile") or "")),
        ("enabled", "yes" if out.get("enabled") else "no"),
        ("timezone", str(out.get("timezone") or "")),
        ("updated", _fmt_ts(out.get("updated_at"))),
    ])
    sys.stdout.write("\n")
    table = Table(mode, "SETTING", "VALUE", "SOURCE")
    for key, value in effective.items():
        if key == "routes":
            continue
        shown = value
        if key == "timezone" and not value:
            shown = "(the profile's own)"
        table.add_row(key, _cell(mode, shown), source(key, own, defaults))
    table.render()
    sys.stdout.write("\n")
    routes = effective.get("routes") if isinstance(effective.get("routes"), dict) else {}
    own_routes = own.get("routes") if isinstance(own.get("routes"), dict) else {}
    admin_routes = defaults.get("routes") if isinstance(defaults.get("routes"), dict) else {}
    table = Table(mode, "CARD KIND", "ROUTE", "SOURCE")
    for kind, value in routes.items():
        table.add_row(kind, _cell(mode, _route_text(value)), source(kind, own_routes, admin_routes))
    table.render()
    icons = out.get("icons") or []
    if icons:
        sys.stdout.write(f"\nicons (for `display --icon`): {', '.join(str(i) for i in icons)}\n")


def _pending(device: dict[str, Any]) -> str:
    """Active deliveries on their way to the tag (blank when the server did not say)."""
    value = device.get("pending_count")
    return "" if value is None else str(value)


def _stuck_hints(devices: list[dict[str, Any]], *, admin: bool) -> None:
    """A hint under a table for each stuck status (clear_failed, assign_failed) in it."""
    statuses = {d.get("status") for d in devices}
    if CLEAR_FAILED in statuses:
        sys.stdout.write("\n" + (_CLEAR_FAILED_ADMIN_HINT if admin else _CLEAR_FAILED_HINT) + "\n")
    if ASSIGN_FAILED in statuses:
        sys.stdout.write("\n" + (_ASSIGN_FAILED_ADMIN_HINT if admin else _ASSIGN_FAILED_HINT) + "\n")


def _bridge_tags(device: dict[str, Any]) -> str:
    """A bridge's slots in use: ``3/10``, ``3`` when its capacity is unknown, blank for other kinds."""
    if device.get("kind") != "bridge" or device.get("assigned_count") is None:
        return ""
    used = str(device.get("assigned_count"))
    return f"{used}/{device['max_tags']}" if device.get("max_tags") else used


def _device_table(mode: Any, devices: list[dict[str, Any]]) -> None:
    from app.cli.output import Table

    table = Table(mode, "ID", "NAME", "STATUS", "PENDING", "BATTERY", "LAST CONTACT", "SCREEN", "COMPANION")
    for d in devices:
        companion = d.get("companion_name") or d.get("companion_id") or ""
        if d.get("companion_name") and d.get("companion_online") is False:
            companion = f"{companion} (offline)"
        table.add_row(
            str(d.get("id") or ""), _cell(mode, d.get("name") or d.get("hw_id")),
            str(d.get("status") or ""), _pending(d), _battery(d.get("battery_mv")),
            _fmt_ts(d.get("last_contact_at")), _screen(d), _cell(mode, companion),
        )
    table.render()
    _stuck_hints(devices, admin=False)


def _card_title(delivery: dict[str, Any]) -> str:
    card = delivery.get("card") if isinstance(delivery.get("card"), dict) else {}
    return str(card.get("title") or "")


def _delivery_table(mode: Any, rows: list[dict[str, Any]], names: dict[str, str]) -> None:
    from app.cli.output import Table

    table = Table(mode, "ID", "CREATED", "TAG", "KIND", "TITLE", "STAGE", "DETAIL")
    for r in rows:
        device_id = str(r.get("device_id") or "")
        table.add_row(
            str(r.get("id") or ""), _fmt_ts(r.get("created_at")),
            _cell(mode, names.get(device_id) or device_id), str(r.get("kind") or ""),
            _cell(mode, _card_title(r)), str(r.get("stage") or ""), _cell(mode, r.get("detail")),
        )
    table.render()


def _names(devices: Any) -> dict[str, str]:
    return {str(d.get("id")): _label(d) for d in _rows(devices)}


# ── profile: tags ──────────────────────────────────────────────────────────


@tags_app.command("list")
@graceful_errors
def tags_list(ctx: typer.Context) -> None:
    """List the tags this profile owns: status, battery, last contact, screen revision."""
    from app.cli.client import tags as api
    from app.cli.output import print_json, print_kv

    mode = _mode(ctx)
    out = _call(ctx, api.overview)
    if mode.json:
        print_json(out)
        return
    counts = out.get("counts") if isinstance(out.get("counts"), dict) else {}
    print_kv([
        ("profile", str(out.get("profile") or "")),
        ("enabled", "yes" if out.get("enabled") else "no"),
        ("tags", str(counts.get("devices", 0))),
        ("active deliveries", str(counts.get("active_deliveries", 0))),
        ("needs input", str(counts.get("needs_input", 0))),
        ("failed (24 h)", str(counts.get("failed_24h", 0))),
    ])
    if not out.get("enabled"):
        sys.stdout.write("\nTags is off for this profile, so no cards are sent. Turn it on: cremind tags set --enable\n")
    devices = _rows(out.get("devices"))
    if not devices:
        sys.stdout.write("\nno tags belong to this profile yet — the admin assigns them "
                         "(cremind tags hardware claim).\n")
        return
    sys.stdout.write("\n")
    _device_table(mode, devices)


@tags_app.command("show")
@graceful_errors
def tags_show(
    ctx: typer.Context,
    tag: str = typer.Argument(..., help="The tag: id, id prefix, hardware id or name."),
) -> None:
    """Show one tag in detail, with its 20 latest deliveries."""
    from app.cli.client import tags as api
    from app.cli.output import print_json, print_kv

    mode = _mode(ctx)

    async def go(client: Any) -> dict[str, Any]:
        device = await _owned_tag(client, tag)
        return await api.get_device(client, device["id"])

    out = _call(ctx, go)
    if mode.json:
        print_json(out)
        return
    d = out.get("device") if isinstance(out.get("device"), dict) else {}
    previews = d.get("previews") if isinstance(d.get("previews"), dict) else {}
    size = f"{d.get('width') or '?'}x{d.get('height') or '?'}" if d.get("width") else ""
    print_kv([
        ("id", str(d.get("id") or "")),
        ("name", str(d.get("name") or "")),
        ("hw_id", str(d.get("hw_id") or "")),
        ("status", str(d.get("status") or "")),
        ("pending", _pending(d)),
        ("battery", _battery(d.get("battery_mv"))),
        ("rssi", "" if d.get("rssi") is None else f"{d.get('rssi')} dBm"),
        ("last contact", _fmt_ts(d.get("last_contact_at"))),
        ("screen", _screen(d)),
        ("clear_required", "yes" if d.get("clear_required") else "no"),
        ("panel", " ".join(p for p in (size, f"{d.get('planes')} plane(s)" if d.get("planes") else "") if p)),
        ("rotation", str(d.get("rotation") or 0)),
        ("firmware", str(d.get("fw") or "")),
        ("companion", str(d.get("companion_id") or "")),
        ("bridge", str(d.get("bridge_device_id") or "")),
        ("claimed", _fmt_ts(d.get("claimed_at"))),
        ("previews", f"desired rev {previews.get('desired') or '-'}, displayed rev {previews.get('displayed') or '-'}"),
    ])
    _stuck_hints([d], admin=False)
    rows = _rows(out.get("deliveries"))
    if rows:
        sys.stdout.write("\nrecent deliveries\n")
        _delivery_table(mode, rows, {str(d.get("id")): _label(d)})
    else:
        sys.stdout.write("\nno deliveries yet.\n")


@tags_app.command("settings")
@graceful_errors
def tags_settings(ctx: typer.Context) -> None:
    """Show this profile's Tags settings: on/off, each option and where it comes from, card routes."""
    from app.cli.client import tags as api
    from app.cli.output import print_json

    mode = _mode(ctx)
    out = _call(ctx, api.get_settings)
    if mode.json:
        print_json(out)
        return
    _render_settings(mode, out)


@tags_app.command("set")
@graceful_errors
def tags_set(
    ctx: typer.Context,
    enable: Optional[bool] = typer.Option(None, "--enable/--disable", help="Turn Tags on or off for this profile."),
    layout: Optional[str] = typer.Option(None, "--layout", help="Screen layout (see `cremind tags settings`)."),
    excerpts: Optional[bool] = typer.Option(None, "--excerpts/--no-excerpts",
                                            help="Show a short excerpt of the assistant's answer."),
    qr_links: Optional[bool] = typer.Option(None, "--qr-links/--no-qr-links", help="Draw a QR code linking to the source."),
    progress_cadence: Optional[int] = typer.Option(None, "--progress-cadence",
                                                   help="Seconds between progress screens (60-3600)."),
    language: Optional[str] = typer.Option(None, "--language", help="Language tag for tag text, e.g. en or vi."),
    timezone: Optional[str] = typer.Option(None, "--timezone",
                                           help="Timezone name or UTC offset; `own` = the profile's Cremind timezone."),
    route: Optional[list[str]] = typer.Option(None, "--route",
                                              help="KIND=all|none|inherit|<tag>[,<tag>...]. Repeatable."),
    inherit: Optional[list[str]] = typer.Option(None, "--inherit",
                                                help="Drop this profile's own value for a setting. Repeatable."),
) -> None:
    """Change this profile's Tags settings — only what you pass changes."""
    from app.cli.client import tags as api
    from app.cli.output import print_json

    mode = _mode(ctx)
    changes = _option_changes(layout=layout, excerpts=excerpts, qr_links=qr_links,
                              progress_cadence=progress_cadence, language=language, timezone=timezone)
    specs = _route_specs(route)
    inherit_keys = _inherit_keys(inherit)
    if enable is None and not changes and not specs and not inherit_keys:
        _fail("nothing to change — pass at least one option (cremind tags set --help).")
    _check_no_clash(changes, specs, inherit_keys)

    async def go(client: Any) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if enable is not None:
            body["enabled"] = enable
        if changes or specs or inherit_keys:
            owned = await api.overview(client) if _needs_lookup(specs) else None
            body["options"] = await _options_patch(
                changes, specs, inherit_keys, lambda ref: _owned_tag(client, ref, overview=owned),
            )
        return await api.patch_settings(client, body)

    out = _call(ctx, go)
    if mode.json:
        print_json(out)
        return
    _render_settings(mode, out)


@tags_app.command("rename")
@graceful_errors
def tags_rename(
    ctx: typer.Context,
    tag: str = typer.Argument(..., help="The tag: id, id prefix, hardware id or name."),
    name: str = typer.Argument(..., help="The new name (1-128 characters)."),
) -> None:
    """Rename one of your tags."""
    from app.cli.client import tags as api
    from app.cli.output import print_json

    mode = _mode(ctx)

    async def go(client: Any) -> dict[str, Any]:
        device = await _owned_tag(client, tag)
        return await api.rename_device(client, device["id"], name)

    out = _call(ctx, go)
    if mode.json:
        print_json(out)
        return
    d = out.get("device") if isinstance(out.get("device"), dict) else {}
    sys.stdout.write(f"renamed {d.get('id', '')} to '{d.get('name', '')}'\n")


@tags_app.command("display")
@graceful_errors
def tags_display(
    ctx: typer.Context,
    tag: str = typer.Argument(..., help="The tag: id, id prefix, hardware id or name."),
    title: str = typer.Argument(..., help="The note's title (at most 120 characters)."),
    body: Optional[str] = typer.Option(None, "--body", help="Body text (at most 400 characters)."),
    body_file: Optional[str] = typer.Option(None, "--body-file", "-f",
                                            help="Read the body from this file ('-' = stdin)."),
    icon: Optional[str] = typer.Option(None, "--icon", help="Icon name (default push_pin; `cremind tags settings` lists them)."),
    ttl: Optional[str] = typer.Option(None, "--ttl", help="How long the note stays: 90, 30m, 2h, 7d (1 min - 7 days; default 1 day)."),
    replace: bool = typer.Option(False, "--replace",
                                 help="Replace the tag's previous --replace note instead of adding a card."),
) -> None:
    """Pin a note on one of your tags. Text that looks like a one-time code is refused.

    Every note is its own card; with --replace it takes the tag's one
    replaceable slot, replacing the previous note that was also sent with
    --replace."""
    from app.cli.client import tags as api
    from app.cli.output import print_json

    mode = _mode(ctx)
    if body is not None and body_file is not None:
        _fail("pass either --body or --body-file, not both")
    text = _read_text_file(body_file, "--body-file") if body_file is not None else body
    payload: dict[str, Any] = {"title": title}
    if text is not None and text.strip():
        payload["body"] = text.strip()
    if icon:
        payload["icon"] = icon
    if ttl is not None:
        payload["ttl_s"] = _parse_duration(ttl, "--ttl")
    if replace:
        payload["replace"] = True
    found: dict[str, Any] = {}

    async def go(client: Any) -> dict[str, Any]:
        found.update(await _owned_tag(client, tag))
        return await api.display(client, found["id"], payload)

    out = _call(ctx, go)
    if mode.json:
        print_json(out)
        return
    delivery = out.get("delivery") if isinstance(out.get("delivery"), dict) else {}
    sys.stdout.write(
        f"pinned '{title}' on {_label(found)}: delivery {delivery.get('id', '')} "
        f"({delivery.get('stage', '')}), until {_fmt_ts(delivery.get('expires_at'))}"
        f"{' — replaces the previous --replace note' if replace else ''}\n"
        f"follow it: cremind tags deliveries show {delivery.get('id', '')}\n"
    )


@tags_app.command("clear")
@graceful_errors
def tags_clear(
    ctx: typer.Context,
    tag: str = typer.Argument(..., help="The tag: id, id prefix, hardware id or name."),
) -> None:
    """Blank one of your tags: its pending cards are cancelled and a clear is queued."""
    from app.cli.client import tags as api
    from app.cli.output import print_json

    mode = _mode(ctx)
    found: dict[str, Any] = {}

    async def go(client: Any) -> dict[str, Any]:
        found.update(await _owned_tag(client, tag))
        return await api.clear(client, found["id"])

    out = _call(ctx, go)
    if mode.json:
        print_json(out)
        return
    delivery = out.get("delivery") if isinstance(out.get("delivery"), dict) else {}
    sys.stdout.write(f"clearing {_label(found)}: delivery {delivery.get('id', '')} queued; "
                     "its other pending cards were cancelled.\n")


def _device_command(ctx: typer.Context, tag: str, kind: str) -> None:
    from app.cli.client import tags as api
    from app.cli.output import print_json

    mode = _mode(ctx)
    found: dict[str, Any] = {}

    async def go(client: Any) -> dict[str, Any]:
        found.update(await _owned_tag(client, tag))
        call = api.refresh if kind == "refresh" else api.identify
        return await call(client, found["id"])

    out = _call(ctx, go)
    if mode.json:
        print_json(out)
        return
    command = out.get("command") if isinstance(out.get("command"), dict) else {}
    sys.stdout.write(f"{kind} queued for {_label(found)}: command {command.get('id', '')} "
                     f"({command.get('status', '')}); the companion runs it at the tag's next wake-up.\n")


@tags_app.command("refresh")
@graceful_errors
def tags_refresh(
    ctx: typer.Context,
    tag: str = typer.Argument(..., help="The tag: id, id prefix, hardware id or name."),
) -> None:
    """Ask the companion to redraw one of your tags."""
    _device_command(ctx, tag, "refresh")


@tags_app.command("identify")
@graceful_errors
def tags_identify(
    ctx: typer.Context,
    tag: str = typer.Argument(..., help="The tag: id, id prefix, hardware id or name."),
) -> None:
    """Ask one of your tags to identify itself, to find which physical tag it is."""
    _device_command(ctx, tag, "identify")


@tags_app.command("preview")
@graceful_errors
def tags_preview(
    ctx: typer.Context,
    tag: str = typer.Argument(..., help="The tag: id, id prefix, hardware id or name."),
    kind: str = typer.Option("displayed", "--kind", help="displayed (on the tag now) or desired (being sent)."),
    out_path: Optional[str] = typer.Option(None, "--out", "-o", help="PNG file to write (default tag-<id>-<kind>.png)."),
) -> None:
    """Save a tag's screen preview as a PNG file."""
    from app.cli.client import tags as api
    from app.cli.output import print_json

    mode = _mode(ctx)
    kind = kind.strip().lower()
    if kind not in ("displayed", "desired"):
        _fail("--kind is displayed or desired")

    async def go(client: Any) -> dict[str, Any]:
        device = await _owned_tag(client, tag)
        path = out_path or f"tag-{str(device['id'])[:8]}-{kind}.png"
        return await api.download_preview(client, device["id"], path, kind=kind)

    out = _call(ctx, go)
    if mode.json:
        print_json(out)
        return
    sys.stdout.write(f"saved {out.get('path')} ({out.get('kind')} screen, revision "
                     f"{out.get('revision')}, {out.get('bytes')} bytes)\n")


@tags_app.command("companions")
@graceful_errors
def tags_companions(ctx: typer.Context) -> None:
    """List the companions (the PCs that drive the tags), for `credentials create`."""
    from app.cli.client import tags as api
    from app.cli.output import Table, print_json

    mode = _mode(ctx)
    out = _call(ctx, api.list_companions)
    if mode.json:
        print_json(out)
        return
    rows = _rows(out.get("companions"))
    if not rows:
        sys.stdout.write("no companion is registered yet.\n")
        return
    table = Table(mode, "ID", "NAME", "ONLINE", "LAST SEEN", "VERSION")
    for c in rows:
        table.add_row(str(c.get("id") or ""), _cell(mode, c.get("name")), "yes" if c.get("online") else "no",
                      _fmt_ts(c.get("last_seen_at")), str(c.get("version") or ""))
    table.render()


# ── profile: deliveries ────────────────────────────────────────────────────


@deliveries_app.command("list")
@graceful_errors
def deliveries_list(
    ctx: typer.Context,
    device: Optional[str] = typer.Option(None, "--device", help="Only this tag (id, prefix, hardware id or name)."),
    state: Optional[str] = typer.Option(None, "--state", help="active, terminal, or one stage (queued, displayed, failed, ...)."),
    limit: Optional[int] = typer.Option(None, "--limit", help="How many (default 50, at most 200)."),
    before: Optional[int] = typer.Option(None, "--before", help="Only deliveries older than this delivery id (paging)."),
) -> None:
    """List deliveries to your tags, newest first."""
    from app.cli.client import tags as api
    from app.cli.output import print_json

    mode = _mode(ctx)
    names: dict[str, str] = {}

    async def go(client: Any) -> dict[str, Any]:
        device_id = None
        if device:
            device_id = (await _owned_tag(client, device))["id"]
        out = await api.list_deliveries(client, device=device_id, state=state, limit=limit, before=before)
        if not mode.json:
            names.update(_names((await api.overview(client)).get("devices")))
        return out

    out = _call(ctx, go)
    if mode.json:
        print_json(out)
        return
    rows = _rows(out.get("deliveries"))
    if not rows:
        sys.stdout.write("no deliveries match.\n")
        return
    _delivery_table(mode, rows, names)
    if out.get("next_before"):
        more = f"cremind tags deliveries list --before {out['next_before']}"
        if device:
            more += f" --device {device}"
        if state:
            more += f" --state {state}"
        sys.stdout.write(f"\nolder: {more}\n")


@deliveries_app.command("show")
@graceful_errors
def deliveries_show(
    ctx: typer.Context,
    delivery_id: int = typer.Argument(..., help="Delivery id (from `deliveries list`)."),
) -> None:
    """Show one delivery: the card, each stage it reached and when, the outcome."""
    from app.cli.client import tags as api
    from app.cli.output import Table, print_json, print_kv

    mode = _mode(ctx)
    out = _call(ctx, lambda client: api.get_delivery(client, delivery_id))
    if mode.json:
        print_json(out)
        return
    d = out.get("delivery") if isinstance(out.get("delivery"), dict) else {}
    card = d.get("card") if isinstance(d.get("card"), dict) else {}
    print_kv([
        ("id", str(d.get("id") or "")),
        ("tag", str(d.get("device_id") or "")),
        ("kind", str(d.get("kind") or "")),
        ("title", str(card.get("title") or "")),
        ("body", str(card.get("body") or "")),
        ("stage", str(d.get("stage") or "")),
        ("outcome", str(d.get("outcome") or "")),
        ("detail", str(d.get("detail") or "")),
        ("status_code", "" if d.get("status_code") is None else str(d.get("status_code"))),
        ("revision", "" if d.get("revision") is None else str(d.get("revision"))),
        ("priority", str(d.get("priority") or "")),
        ("created", _fmt_ts(d.get("created_at"))),
        ("expires", _fmt_ts(d.get("expires_at"))),
        ("finished", _fmt_ts(d.get("finished_at"))),
    ])
    times = d.get("stage_times") if isinstance(d.get("stage_times"), dict) else {}
    if times:
        sys.stdout.write("\n")
        table = Table(mode, "STAGE", "AT")
        for stage, at in sorted(times.items(), key=lambda kv: float(kv[1] or 0)):
            table.add_row(str(stage), _fmt_ts(at))
        table.render()


@deliveries_app.command("cancel")
@graceful_errors
def deliveries_cancel(
    ctx: typer.Context,
    delivery_id: int = typer.Argument(..., help="Delivery id (from `deliveries list`)."),
) -> None:
    """Cancel a delivery that is not finished yet — also one the companion already fetched."""
    from app.cli.client import tags as api
    from app.cli.output import print_json

    mode = _mode(ctx)
    out = _call(ctx, lambda client: api.cancel_delivery(client, delivery_id))
    if mode.json:
        print_json(out)
        return
    d = out.get("delivery") if isinstance(out.get("delivery"), dict) else {}
    resolved = out.get("resolved") if isinstance(out.get("resolved"), dict) else None
    line = f"cancelled delivery {d.get('id', delivery_id)}"
    if resolved:
        line += (f"; the companion was sent resolved job {resolved.get('id', '')} "
                 "so the tag drops the card if it already has it")
    else:
        line += " (the tag has changed hands, so nothing was sent to it)"
    sys.stdout.write(line + "\n")


# ── profile: content credentials ───────────────────────────────────────────


@credentials_app.command("list")
@graceful_errors
def credentials_list(ctx: typer.Context) -> None:
    """List this profile's content credentials (never their secrets)."""
    from app.cli.client import tags as api
    from app.cli.output import Table, print_json

    mode = _mode(ctx)
    out = _call(ctx, api.list_credentials)
    if mode.json:
        print_json(out)
        return
    rows = _rows(out.get("credentials"))
    if not rows:
        sys.stdout.write("no content credentials — create one: cremind tags credentials create\n")
        return
    table = Table(mode, "ID", "COMPANION", "LABEL", "CREATED", "LAST USED", "STATE")
    for c in rows:
        table.add_row(str(c.get("id") or ""), str(c.get("companion_id") or ""), _cell(mode, c.get("label")),
                      _fmt_ts(c.get("created_at")), _fmt_ts(c.get("last_used_at")),
                      f"revoked {_fmt_ts(c.get('revoked_at'))}" if c.get("revoked") else "active")
    table.render()


@credentials_app.command("create")
@graceful_errors
def credentials_create(
    ctx: typer.Context,
    companion: Optional[str] = typer.Option(None, "--companion",
                                            help="Companion id or name (default: the only one registered)."),
    label: Optional[str] = typer.Option(None, "--label", help="A note to recognise the credential by."),
) -> None:
    """Create a content credential: lets one companion fetch this profile's cards. The secret is shown once."""
    from app.cli.client import tags as api
    from app.cli.output import print_json

    mode = _mode(ctx)
    found: dict[str, Any] = {}

    async def go(client: Any) -> dict[str, Any]:
        found.update(await _companion(client, companion, admin=False))
        return await api.create_credential(client, found["id"], label)

    out = _call(ctx, go)
    if mode.json:
        print_json(out)
        return
    sys.stdout.write(f"content credential for companion {found.get('name') or found.get('id')}\n")
    _print_secret(out, who="the companion")


@credentials_app.command("revoke")
@graceful_errors
def credentials_revoke(
    ctx: typer.Context,
    credential_id: str = typer.Argument(..., help="Credential id (tagc_…, from `credentials list`)."),
) -> None:
    """Revoke a content credential; the companion using it stops getting this profile's cards at once."""
    from app.cli.client import tags as api
    from app.cli.output import print_json

    mode = _mode(ctx)
    out = _call(ctx, lambda client: api.revoke_credential(client, credential_id))
    if mode.json:
        print_json(out)
        return
    cred = out.get("credential") if isinstance(out.get("credential"), dict) else {}
    sys.stdout.write(f"revoked {cred.get('id', credential_id)}\n")


# ── hardware (admin profile) ────────────────────────────────────────────────


@hardware_app.command("list")
@graceful_errors
def hardware_list(ctx: typer.Context) -> None:
    """List every companion, device (gateways, bridges, tags) and recent hardware command."""
    from app.cli.client import tags as api
    from app.cli.output import Table, print_json

    mode = _mode(ctx)
    out = _call(ctx, api.hardware_inventory, admin=True)
    if mode.json:
        print_json(out)
        return
    companions = _rows(out.get("companions"))
    if not companions:
        sys.stdout.write("no companions registered — register one: cremind tags hardware register <name>\n")
        return
    names = {str(c.get("id")): str(c.get("name") or "") for c in companions}
    sys.stdout.write("companions\n")
    table = Table(mode, "ID", "NAME", "ONLINE", "LAST SEEN", "VERSION", "HOST", "CREDENTIALS")
    for c in companions:
        active = [h for h in _rows(c.get("credentials")) if not h.get("revoked")]
        table.add_row(str(c.get("id") or ""), _cell(mode, c.get("name")), "yes" if c.get("online") else "no",
                      _fmt_ts(c.get("last_seen_at")), str(c.get("version") or ""), _cell(mode, c.get("host")),
                      f"{len(active)} active")
    table.render()
    devices = _rows(out.get("devices"))
    sys.stdout.write("\ndevices\n")
    if devices:
        table = Table(mode, "ID", "KIND", "HW ID", "NAME", "OWNER", "STATUS", "PENDING", "TAGS", "BATTERY",
                      "LAST CONTACT", "COMPANION")
        for d in devices:
            table.add_row(str(d.get("id") or ""), str(d.get("kind") or ""), _cell(mode, d.get("hw_id")),
                          _cell(mode, d.get("name")), str(d.get("owner_profile") or ""), str(d.get("status") or ""),
                          _pending(d), _bridge_tags(d), _battery(d.get("battery_mv")),
                          _fmt_ts(d.get("last_contact_at")),
                          _cell(mode, names.get(str(d.get("companion_id"))) or d.get("companion_id")))
        table.render()
        _stuck_hints(devices, admin=True)
    else:
        sys.stdout.write("none reported yet — the companion sends its inventory once it is connected.\n")
    commands = _rows(out.get("commands"))
    if commands:
        sys.stdout.write("\ncommands\n")
        table = Table(mode, "ID", "KIND", "STATUS", "CREATED", "COMPANION", "ERROR")
        for c in commands:
            table.add_row(str(c.get("id") or ""), str(c.get("kind") or ""), str(c.get("status") or ""),
                          _fmt_ts(c.get("created_at")),
                          _cell(mode, names.get(str(c.get("companion_id"))) or c.get("companion_id")),
                          _cell(mode, c.get("error")))
        table.render()


@hardware_app.command("status")
@graceful_errors
def hardware_status(
    ctx: typer.Context,
    command_id: str = typer.Argument(..., help="Command id (printed by `run`, `claim`, `assign`, `release`)."),
) -> None:
    """Show one hardware command: its status, arguments, result or error."""
    import json

    from app.cli.client import tags as api
    from app.cli.output import print_json, print_kv

    mode = _mode(ctx)
    out = _call(ctx, lambda client: api.get_command(client, command_id), admin=True)
    if mode.json:
        print_json(out)
        return
    c = out.get("command") if isinstance(out.get("command"), dict) else {}
    print_kv([
        ("id", str(c.get("id") or "")),
        ("kind", str(c.get("kind") or "")),
        ("status", str(c.get("status") or "")),
        ("companion", str(c.get("companion_id") or "")),
        ("args", json.dumps(c.get("args") or {}, ensure_ascii=False)),
        ("requested_by", str(c.get("requested_by") or "")),
        ("created", _fmt_ts(c.get("created_at"))),
        ("claimed", _fmt_ts(c.get("claimed_at"))),
        ("completed", _fmt_ts(c.get("completed_at"))),
        ("expires", _fmt_ts(c.get("expires_at"))),
        ("error", str(c.get("error") or "")),
    ])
    if c.get("result") is not None:
        sys.stdout.write("\nresult\n" + json.dumps(c.get("result"), indent=2, ensure_ascii=False) + "\n")


@hardware_app.command("register")
@graceful_errors
def hardware_register(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="A name for the companion, e.g. the PC it runs on."),
) -> None:
    """Register a companion and create its hardware credential. The secret is shown once."""
    from app.cli.client import tags as api
    from app.cli.output import print_json

    mode = _mode(ctx)
    out = _call(ctx, lambda client: api.register_companion(client, name), admin=True)
    if mode.json:
        print_json(out)
        return
    comp = out.get("companion") if isinstance(out.get("companion"), dict) else {}
    sys.stdout.write(f"registered companion {comp.get('name', name)}: {comp.get('id', '')}\n")
    _print_secret(out, who="the companion")


@hardware_app.command("rotate")
@graceful_errors
def hardware_rotate(
    ctx: typer.Context,
    companion: str = typer.Argument(..., help="Companion id or name."),
) -> None:
    """Replace a companion's hardware credential; the old one stops working at once."""
    from app.cli.client import tags as api
    from app.cli.output import print_json

    mode = _mode(ctx)

    async def go(client: Any) -> dict[str, Any]:
        found = await _companion(client, companion, admin=True)
        return await api.rotate_companion(client, found["id"])

    out = _call(ctx, go, admin=True)
    if mode.json:
        print_json(out)
        return
    revoked = [str(r) for r in out.get("revoked") or []]
    sys.stdout.write(f"revoked: {', '.join(revoked) or '(none)'}\n")
    _print_secret(out, who="the companion (it is locked out until it has it)")


@hardware_app.command("remove")
@graceful_errors
def hardware_remove(
    ctx: typer.Context,
    companion: str = typer.Argument(..., help="Companion id or name."),
    yes: bool = typer.Option(False, "--yes", help="Really remove it (without this, nothing changes)."),
) -> None:
    """Remove a companion: revokes its credentials and forgets its devices and their history."""
    from app.cli.client import tags as api
    from app.cli.output import print_json

    mode = _mode(ctx)

    async def go(client: Any) -> dict[str, Any]:
        inv = await api.hardware_inventory(client)
        found = await _companion(client, companion, admin=True, inventory=inv)
        if not yes:
            devices = [d for d in _rows(inv.get("devices")) if d.get("companion_id") == found["id"]]
            owned = [d for d in devices if d.get("owner_profile")]
            _fail(
                f"This would remove companion {found.get('name') or found['id']}: revoke its credentials and "
                f"forget its {len(devices)} device(s) ({len(owned)} tag(s) owned by a profile), "
                "with their delivery history.\nNothing was changed. Re-run with --yes to apply.",
                code=2,
            )
        return await api.delete_companion(client, found["id"])

    out = _call(ctx, go, admin=True)
    if mode.json:
        print_json(out)
        return
    sys.stdout.write(f"removed companion {companion}\n")


_COMMAND_KINDS = (
    "scan_unprovisioned", "provision_bridge", "configure_bridge", "remove_bridge",
    "identify", "refresh_tag", "install_fontpack", "collect_diagnostics",
)


@hardware_app.command("run")
@graceful_errors
def hardware_run(
    ctx: typer.Context,
    kind: str = typer.Argument(..., help="scan_unprovisioned, provision_bridge, configure_bridge, remove_bridge, "
                                         "identify, refresh_tag, install_fontpack or collect_diagnostics."),
    companion: Optional[str] = typer.Option(None, "--companion", help="Companion id or name (default: the only one)."),
    duration: Optional[int] = typer.Option(None, "--duration", help="scan_unprovisioned: seconds to scan (5-600, default 60)."),
    uuid: Optional[str] = typer.Option(None, "--uuid", help="provision_bridge: the unprovisioned bridge's UUID (from a scan)."),
    name: Optional[str] = typer.Option(None, "--name", help="provision_bridge: a name for the new bridge."),
    hw_id: Optional[str] = typer.Option(None, "--hw-id", help="configure_bridge, remove_bridge, identify: the device's hardware id."),
    tag_id: Optional[str] = typer.Option(None, "--tag-id", help="refresh_tag: the tag's hardware id."),
    bridge_hw_id: Optional[str] = typer.Option(None, "--bridge-hw-id", help="install_fontpack: the bridge's hardware id."),
) -> None:
    """Queue a hardware operation on a companion; it runs asynchronously."""
    from app.cli.client import tags as api
    from app.cli.output import print_json

    mode = _mode(ctx)
    kind = kind.strip().lower().replace("-", "_")
    args: dict[str, Any] = {}
    for key, value in (("duration_s", duration), ("uuid", uuid), ("name", name), ("hw_id", hw_id),
                       ("tag_id", tag_id), ("bridge_hw_id", bridge_hw_id)):
        if value is not None:
            args[key] = value
    found: dict[str, Any] = {}

    async def go(client: Any) -> dict[str, Any]:
        found.update(await _companion(client, companion, admin=True))
        return await api.create_command(client, found["id"], kind, args)

    out = _call(ctx, go, admin=True)
    if mode.json:
        print_json(out)
        return
    c = out.get("command") if isinstance(out.get("command"), dict) else {}
    sys.stdout.write(f"queued {kind} on companion {found.get('name') or found.get('id')}: command "
                     f"{c.get('id', '')} ({c.get('status', '')})\n"
                     f"check it: cremind tags hardware status {c.get('id', '')}\n")


def _queued(commands: Any) -> str:
    return ", ".join(f"{c.get('kind')} {c.get('id')}" for c in _rows(commands)) or "(none)"


@hardware_app.command("claim")
@graceful_errors
def hardware_claim(
    ctx: typer.Context,
    tag: str = typer.Argument(..., help="The tag: id, id prefix, hardware id or name."),
    owner: str = typer.Option(..., "--owner", help="The profile that will own the tag."),
    bridge: Optional[str] = typer.Option(None, "--bridge", help="Bridge id, hardware id or name (needed when the companion has several)."),
    name: Optional[str] = typer.Option(None, "--name", help="Rename the tag at the same time."),
) -> None:
    """Give a tag to a profile. Its screen is blanked before the new owner's cards appear."""
    from app.cli.client import tags as api
    from app.cli.output import print_json

    mode = _mode(ctx)

    async def go(client: Any) -> dict[str, Any]:
        refs = [tag] + ([bridge] if bridge is not None else [])
        inv = None if all(_UUID_RE.match(r.strip()) for r in refs) else await api.hardware_inventory(client)
        device = await _hardware_device(client, tag, kind="tag", inventory=inv)
        bridge_id = None
        if bridge is not None:
            bridge_id = (await _hardware_device(client, bridge, kind="bridge", inventory=inv))["id"]
        return await api.claim_tag(client, device["id"], owner, bridge_id=bridge_id, name=name)

    out = _call(ctx, go, admin=True)
    if mode.json:
        print_json(out)
        return
    d = out.get("device") if isinstance(out.get("device"), dict) else {}
    sys.stdout.write(f"tag {_label(d)} now belongs to {d.get('owner_profile', owner)} (epoch {d.get('epoch', '')}); "
                     f"queued: {_queued(out.get('commands'))}\n")


@hardware_app.command("assign")
@graceful_errors
def hardware_assign(
    ctx: typer.Context,
    tag: str = typer.Argument(..., help="The tag: id, id prefix, hardware id or name."),
    bridge: str = typer.Option(..., "--bridge", help="Bridge id, hardware id or name, on the tag's companion."),
) -> None:
    """Move a tag to another bridge; its owner and pending cards stay."""
    from app.cli.client import tags as api
    from app.cli.output import print_json

    mode = _mode(ctx)

    async def go(client: Any) -> dict[str, Any]:
        refs = [tag, bridge]
        inv = None if all(_UUID_RE.match(r.strip()) for r in refs) else await api.hardware_inventory(client)
        device = await _hardware_device(client, tag, kind="tag", inventory=inv)
        bridge_id = (await _hardware_device(client, bridge, kind="bridge", inventory=inv))["id"]
        return await api.assign_tag(client, device["id"], bridge_id)

    out = _call(ctx, go, admin=True)
    if mode.json:
        print_json(out)
        return
    d = out.get("device") if isinstance(out.get("device"), dict) else {}
    c = out.get("command") if isinstance(out.get("command"), dict) else {}
    sys.stdout.write(f"tag {_label(d)} moves to bridge {d.get('bridge_device_id', '')} (epoch {d.get('epoch', '')}); "
                     f"queued: {c.get('kind', '')} {c.get('id', '')}\n")


@hardware_app.command("release")
@graceful_errors
def hardware_release(
    ctx: typer.Context,
    tag: str = typer.Argument(..., help="The tag: id, id prefix, hardware id or name."),
) -> None:
    """Take a tag away from its profile: it belongs to nobody and its screen is blanked."""
    from app.cli.client import tags as api
    from app.cli.output import print_json

    mode = _mode(ctx)

    async def go(client: Any) -> dict[str, Any]:
        device = await _hardware_device(client, tag, kind="tag")
        return await api.release_tag(client, device["id"])

    out = _call(ctx, go, admin=True)
    if mode.json:
        print_json(out)
        return
    d = out.get("device") if isinstance(out.get("device"), dict) else {}
    sys.stdout.write(f"tag {_label(d)} released (epoch {d.get('epoch', '')}); queued: {_queued(out.get('commands'))}\n")


@hardware_app.command("rename")
@graceful_errors
def hardware_rename(
    ctx: typer.Context,
    device: str = typer.Argument(..., help="Any device: id, id prefix, hardware id or name."),
    name: str = typer.Argument(..., help="The new name (1-128 characters)."),
) -> None:
    """Rename any gateway, bridge or tag."""
    from app.cli.client import tags as api
    from app.cli.output import print_json

    mode = _mode(ctx)

    async def go(client: Any) -> dict[str, Any]:
        found = await _hardware_device(client, device)
        return await api.rename_hardware_device(client, found["id"], name)

    out = _call(ctx, go, admin=True)
    if mode.json:
        print_json(out)
        return
    d = out.get("device") if isinstance(out.get("device"), dict) else {}
    sys.stdout.write(f"renamed {d.get('kind', '')} {d.get('id', '')} to '{d.get('name', '')}'\n")


@hardware_app.command("forget")
@graceful_errors
def hardware_forget(
    ctx: typer.Context,
    device: str = typer.Argument(..., help="Any device: id, id prefix, hardware id or name."),
    yes: bool = typer.Option(False, "--yes", help="Really forget it (without this, nothing changes)."),
) -> None:
    """Delete a device record and its delivery history. A tag a profile owns must be released first."""
    from app.cli.client import tags as api
    from app.cli.output import print_json

    mode = _mode(ctx)
    release = f"cremind tags hardware release {_shell_arg(device)}"
    owned_message = "Release it first, so its screen is blanked and its epoch moves on: " + release

    async def go(client: Any) -> dict[str, Any]:
        if yes:
            found = await _hardware_device(client, device)
            return await api.delete_hardware_device(client, found["id"])
        inv = await api.hardware_inventory(client)
        found = await _hardware_device(client, device, inventory=inv)
        full = next((d for d in _rows(inv.get("devices")) if d.get("id") == found["id"]), found)
        if full.get("kind") == "tag" and full.get("owner_profile"):
            _fail(f"tag {_label(full)} belongs to {full['owner_profile']}, so it cannot be forgotten.\n"
                  + owned_message)
        _fail(f"This would forget {full.get('kind') or 'device'} {_label(full)} and its delivery history; "
              "if the companion still reports it, it comes back as a new, unclaimed device.\n"
              "Nothing was changed. Re-run with --yes to apply.", code=2)

    out = _call(ctx, go, admin=True, hints={"tag_owned": owned_message})
    if mode.json:
        print_json(out)
        return
    d = out.get("device") if isinstance(out.get("device"), dict) else {}
    epoch = ""
    if d.get("kind") == "tag" and out.get("last_epoch") is not None:
        epoch = f" (last epoch {out['last_epoch']})"
    sys.stdout.write(f"forgot {d.get('kind', '')} {_label(d)}{epoch}\n")


def _render_defaults(mode: Any, out: dict[str, Any]) -> None:
    from app.cli.output import Table

    defaults = out.get("defaults") if isinstance(out.get("defaults"), dict) else {}
    builtin = out.get("builtin") if isinstance(out.get("builtin"), dict) else {}
    table = Table(mode, "SETTING", "ADMIN DEFAULT", "BUILT-IN")
    for key, value in builtin.items():
        if key == "routes":
            continue
        table.add_row(key, _cell(mode, defaults.get(key, "")), _cell(mode, value))
    table.render()
    sys.stdout.write("\n")
    routes = builtin.get("routes") if isinstance(builtin.get("routes"), dict) else {}
    admin_routes = defaults.get("routes") if isinstance(defaults.get("routes"), dict) else {}
    table = Table(mode, "CARD KIND", "ADMIN DEFAULT", "BUILT-IN")
    for kind, value in routes.items():
        table.add_row(kind, _cell(mode, _route_text(admin_routes.get(kind))), _route_text(value))
    table.render()


@hardware_app.command("defaults")
@graceful_errors
def hardware_defaults(ctx: typer.Context) -> None:
    """Show the Tags defaults every profile inherits, next to the built-in values."""
    from app.cli.client import tags as api
    from app.cli.output import print_json

    mode = _mode(ctx)
    out = _call(ctx, api.get_defaults, admin=True)
    if mode.json:
        print_json(out)
        return
    _render_defaults(mode, out)


@hardware_app.command("set-defaults")
@graceful_errors
def hardware_set_defaults(
    ctx: typer.Context,
    layout: Optional[str] = typer.Option(None, "--layout", help="Screen layout."),
    excerpts: Optional[bool] = typer.Option(None, "--excerpts/--no-excerpts", help="Show answer excerpts."),
    qr_links: Optional[bool] = typer.Option(None, "--qr-links/--no-qr-links", help="Draw QR links."),
    progress_cadence: Optional[int] = typer.Option(None, "--progress-cadence", help="Seconds between progress screens (60-3600)."),
    language: Optional[str] = typer.Option(None, "--language", help="Language tag, e.g. en or vi."),
    timezone: Optional[str] = typer.Option(None, "--timezone", help="Timezone; `own` = each profile's Cremind timezone."),
    route: Optional[list[str]] = typer.Option(None, "--route", help="KIND=all|none|inherit|<tag>[,<tag>...]. Repeatable."),
    inherit: Optional[list[str]] = typer.Option(None, "--inherit", help="Drop the admin default (back to built-in). Repeatable."),
) -> None:
    """Change the defaults every profile inherits — only what you pass changes."""
    from app.cli.client import tags as api
    from app.cli.output import print_json

    mode = _mode(ctx)
    changes = _option_changes(layout=layout, excerpts=excerpts, qr_links=qr_links,
                              progress_cadence=progress_cadence, language=language, timezone=timezone)
    specs = _route_specs(route)
    inherit_keys = _inherit_keys(inherit)
    if not changes and not specs and not inherit_keys:
        _fail("nothing to change — pass at least one option (cremind tags hardware set-defaults --help).")
    _check_no_clash(changes, specs, inherit_keys)

    async def go(client: Any) -> dict[str, Any]:
        inv = await api.hardware_inventory(client) if _needs_lookup(specs) else None
        patch = await _options_patch(
            changes, specs, inherit_keys,
            lambda ref: _hardware_device(client, ref, kind="tag", inventory=inv),
        )
        return await api.patch_defaults(client, patch)

    out = _call(ctx, go, admin=True)
    if mode.json:
        print_json(out)
        return
    _render_defaults(mode, out)


# Simple device setup: `cremind tags devices …` and the gateway computers it
# runs on, `cremind tags hosts …` (app/cli/commands/tags_devices.py,
# tags_hosts.py; imported last — they reuse the helpers above).
from app.cli.commands.tags_devices import devices_app  # noqa: E402
from app.cli.commands.tags_hosts import hosts_app  # noqa: E402

tags_app.add_typer(devices_app, name="devices")
tags_app.add_typer(hosts_app, name="hosts")

# Host-side hardware tools (firmware, enrollment, bridge fonts, simulator):
# `cremind tags tools …` hands everything after `tools` to app.tags.runtime.cli.
from app.cli.commands import tags_tools  # noqa: E402

tags_tools.register(tags_app)
