"""Hardware operations of private workers (cremind-tag ``docs/connect-setup.md``
§8-§10, ``docs/tags/setup-api.md``).

Profile side: the Connection view, discovery, pairing, unpair (Remove),
pause/resume, moving a tag to its gateway or another bridge, test cards,
recovery. Worker side (connector v2): the authorization lease, reconciliation
state, operation details and progress, grant signing, the recovery vault.

A tag's **parent** (``tag_devices.bridge_device_id``) is a bridge, or its own
gateway when that gateway serves tags on its own radio (its inventory reports
``tag_links`` > 0, protocol.md §11): the worker then runs the tag's session
itself and the gateway's radio is one more place a tag can live. A gateway
that does not (older firmware) needs bridges for every tag.

An operation reaches its worker as a ``run_operation`` command on the
existing long-poll queue. The worker reports stages with
``operations/{id}/progress``; those reports drive the binding states:

    pairing -> paired (device committed ownership) -> ready (configured; a tag
    after its authenticated clear) ; recovery_pending -> ready (rekeyed) ;
    removal_pending -> (gone, tombstone kept)

Cremind signs a grant only for an open operation of the requesting worker,
for the binding's current (or pending) generation, naming that worker's
controller key, the profile's UUID and this server's authority.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import delete, func, select, update

from app.tags import protocol_v2 as v2
from app.tags.cards import iso
from app.tags.ownership import (
    BINDINGS, INSTALLATIONS, OPERATIONS, PRIVATE, REVOCATIONS, RUN_OPERATION, companion_status, owns,
    profile_uuid, queue_operation, tombstone,
)
from app.tags.service import TagError, bridge_capacity, parent_capacity, serves_tags
from app.tags.storage import (
    ACTIVE_STAGES, COMMANDS, COMPANIONS, CREDENTIALS, DELIVERIES, DEVICES, begin_write,
    cancel_device_deliveries, device_json, get_tag_storage, insert_command, notify_commands, now_ms,
)

LEASE_TTL_S = 60
LEASE_RENEW_S = 20
DISCOVERY_DEFAULT_S = {"bridge": 45, "tag": 75}
DISCOVERY_MAX_S = 120
DISCOVERY_SLACK_MS = 20_000
OPEN_STATES = ("queued", "running", "pending_device")
FINAL_STATES = ("succeeded", "failed", "cancelled")
WAITING = "waiting_for_connect"
# A gateway binding the worker can act through (claimed; ready once it has reported in).
GATEWAY_READY = ("paired", "ready")
_PAIR_KINDS = {"bridge": "pair_bridge", "tag": "pair_tag"}
# A tag enrolled over SWD with the hardware tools (protocol v1) joins without a label: an import, followed
# through the pairing endpoints like a pairing.
_IMPORT = "import_tag"
_PAIRING_KINDS = (*_PAIR_KINDS.values(), _IMPORT)
_V1_DEVICE_LABEL = b"cremind-tag/v1-tag"
_HEX32 = set("0123456789abcdef")
VAULT_KEEP = 3


def _hex(value: Any, size: int) -> str | None:
    text = str(value or "").lower()
    return text if len(text) == size * 2 and set(text) <= _HEX32 else None


def _u32(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFF:
        return None
    return value


# ── serialisation ──


def operation_json(row: Any) -> dict[str, Any]:
    r = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    return {
        "id": r["id"], "kind": r["kind"], "state": r["state"], "stage": r["stage"],
        "stage_detail": r.get("stage_detail"), "device": None, "error": r.get("error"),
        "created_at": iso(r["created_at"]), "updated_at": iso(r["updated_at"]),
    }


def _delivery_status(dev: dict[str, Any], pending: int) -> str:
    if dev.get("status") in ("clear_failed", "assign_failed"):
        return "failed"
    if dev.get("clear_required"):
        return "clear_pending"
    return "pending" if pending else "ok"


def device_view(binding: Any, dev: dict[str, Any] | None, *, companion: Any, now: float,
                pending: int = 0, assigned: int = 0, affected: list[str] | None = None) -> dict[str, Any]:
    """The contract's Device object for one binding (+ its device row).
    ``assigned``: the tags whose parent it is (a bridge, or a gateway serving
    tags on its own radio); ``affected``: a bridge's tags, by device id."""
    d = dev or {}
    state = binding.state
    if state in ("paired", "ready"):
        seen = companion.last_seen_at if binding.role == "gateway" else d.get("last_contact_at")
        if binding.role == "gateway" and seen and now - float(seen) > 120_000:
            state = "offline"
        elif binding.role == "gateway" and not seen:
            state = "offline" if binding.state == "ready" else state
        elif binding.role != "gateway" and companion.last_seen_at and now - float(companion.last_seen_at) > 120_000:
            state = "offline"
    info = d.get("info") or {}
    out: dict[str, Any] = {
        "id": d.get("id"), "binding_id": binding.id, "kind": binding.role, "name": d.get("name") or "",
        "device_id": binding.device_id, "short_id": f"{int(binding.short_id or 0):08X}", "state": state,
        "paused": bool(binding.paused) or (binding.role == "gateway" and bool(companion.paused)),
        "generation": int(binding.generation or 0), "fw": binding.fw or d.get("fw"),
        "board": binding.board if binding.board is not None else d.get("board"),
        "last_contact_at": iso(d["last_contact_at"]) if d.get("last_contact_at") else None,
        "battery_mv": d.get("battery_mv"), "rssi": d.get("rssi"),
        "capacity": None, "serves_tags": None, "fontpack_ok": None, "bridge_id": None, "delivery": None,
        "affected_tag_ids": None,
    }
    if binding.role == "bridge":
        max_tags = bridge_capacity(info)
        out["capacity"] = {"max_tags": max_tags, "assigned": assigned} if max_tags else None
        out["fontpack_ok"] = info.get("fontpack_ok") if isinstance(info.get("fontpack_ok"), bool) else None
        out["affected_tag_ids"] = affected or []
    elif binding.role == "gateway":
        # Tags on its own radio: newer gateways reach the tags near them themselves.
        out["serves_tags"] = serves_tags(info)
        max_tags = parent_capacity("gateway", info)
        out["capacity"] = {"max_tags": max_tags, "assigned": assigned} if max_tags else None
    elif binding.role == "tag":
        out["bridge_id"] = d.get("bridge_device_id")
        out["delivery"] = {
            "pending_count": pending, "displayed_revision": int(d.get("displayed_revision") or 0),
            "desired_revision": int(d.get("desired_revision") or 0),
            "clear_required": bool(d.get("clear_required")), "status": _delivery_status(d, pending),
        }
    return out


async def _devices_by_id(conn, ids: list[str]) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    rows = (await conn.execute(select(DEVICES).where(DEVICES.c.id.in_(ids)))).all()
    return {r.id: device_json(r) for r in rows}


async def _pending_counts(conn, ids: list[str]) -> dict[str, int]:
    if not ids:
        return {}
    rows = (await conn.execute(select(DELIVERIES.c.tag_device_id, func.count()).where(
        DELIVERIES.c.tag_device_id.in_(ids), DELIVERIES.c.stage.in_(ACTIVE_STAGES),
    ).group_by(DELIVERIES.c.tag_device_id))).all()
    return {r[0]: int(r[1]) for r in rows}


async def _connection(conn, comp: Any, now: float) -> dict[str, Any]:
    bindings = (await conn.execute(select(BINDINGS).where(BINDINGS.c.companion_id == comp.id)
                                   .order_by(BINDINGS.c.created_at))).all()
    devs = await _devices_by_id(conn, [b.tag_device_id for b in bindings if b.tag_device_id])
    pending = await _pending_counts(conn, [b.tag_device_id for b in bindings if b.role == "tag" and b.tag_device_id])
    # The tags each parent serves: a bridge's, or the gateway's own (its radio).
    on_parent: dict[str, list[str]] = {}
    for b in bindings:
        if b.role == "tag" and b.tag_device_id and devs.get(b.tag_device_id, {}).get("bridge_device_id"):
            on_parent.setdefault(devs[b.tag_device_id]["bridge_device_id"], []).append(b.tag_device_id)
    views = {"gateway": None, "bridge": [], "tag": []}
    for b in bindings:
        dev = devs.get(b.tag_device_id or "")
        affected = on_parent.get(b.tag_device_id or "", [])
        view = device_view(b, dev, companion=comp, now=now, pending=pending.get(b.tag_device_id or "", 0),
                           assigned=len(affected), affected=affected)
        if b.role == "gateway":
            views["gateway"] = view
        else:
            views[b.role].append(view)
    inst = None
    if comp.host_id:
        from app.tags.hosts import HOSTS

        row = (await conn.execute(select(HOSTS).where(HOSTS.c.id == comp.host_id))).first()
        if row is not None:
            inst = {"host_id": row.id, "installation_id": row.installation_id, "name": row.name,
                    "kind": row.kind, "platform": row.platform, "version": row.version,
                    "last_seen_at": iso(row.last_seen_at) if row.last_seen_at else None}
    elif comp.installation_id:
        row = (await conn.execute(select(INSTALLATIONS).where(INSTALLATIONS.c.id == comp.installation_id))).first()
        if row is not None:
            inst = {"installation_id": row.id, "name": row.computer, "platform": row.platform,
                    "version": row.version, "last_seen_at": iso(row.last_seen_at), "kind": "connect"}
    return {
        "id": comp.id, "name": comp.name, "status": companion_status(comp, now), "paused": bool(comp.paused),
        "execution_kind": comp.execution_kind, "host_id": comp.host_id,
        "computer": inst, "gateway": views["gateway"], "bridges": views["bridge"], "tags": views["tag"],
        "last_seen_at": iso(comp.last_seen_at) if comp.last_seen_at else None, "created_at": iso(comp.created_at),
    }


def simple_setup_enabled() -> bool:
    """The release gate (connect-setup.md §13): on for development and
    pre-release builds, off for a final release until the admin turns it on
    (``server_config`` ``tags_simple_setup`` = on/off; env
    ``CREMIND_TAGS_SIMPLE_SETUP`` = 1/0 wins)."""
    import os

    env = os.environ.get("CREMIND_TAGS_SIMPLE_SETUP")
    if env is not None and env.strip():
        return env.strip().lower() in ("1", "true", "on", "yes")
    try:
        from app.config.settings import get_dynamic

        value = get_dynamic("server_config", "tags_simple_setup")
    except Exception:  # noqa: BLE001 - no config storage yet (tests, early boot)
        value = None
    if isinstance(value, str) and value.strip().lower() in ("on", "off"):
        return value.strip().lower() == "on"
    from packaging.version import InvalidVersion, Version

    from app.__version__ import __version__

    try:
        if Version(__version__).is_prerelease:
            return True
    except InvalidVersion:
        return True
    from pathlib import Path

    # A source checkout (development) — not an installed final release.
    return (Path(__file__).resolve().parents[2] / ".git").exists()


def require_simple_setup() -> None:
    if not simple_setup_enabled():
        raise TagError(403, "simple_setup_disabled",
                       "Hardware setup from this page is not enabled on this server yet.")


async def connections(profile: str) -> dict[str, Any]:
    from app.tags import setup

    now = now_ms()
    store = get_tag_storage()
    async with store.engine.connect() as conn:
        profile_id = await profile_uuid(conn, profile)
        comps = (await conn.execute(select(COMPANIONS).where(
            COMPANIONS.c.mode == PRIVATE, COMPANIONS.c.owner_profile == profile,
            COMPANIONS.c.owner_profile_id == profile_id, COMPANIONS.c.state.notin_(("removed", "connecting")),
        ).order_by(COMPANIONS.c.created_at))).all()
        out = [await _connection(conn, c, now) for c in comps]
        ops = (await conn.execute(select(OPERATIONS).where(
            OPERATIONS.c.owner_profile == profile, OPERATIONS.c.state.in_(OPEN_STATES + (WAITING,)),
        ).order_by(OPERATIONS.c.created_at.desc()).limit(50))).all()
    computers: dict[str, Any] = {}
    for c in out:
        if c["computer"]:
            computers[c["computer"].get("host_id") or c["computer"]["installation_id"]] = c["computer"]
    return {
        "simple_setup": simple_setup_enabled(),
        "connections": out,
        "computers": list(computers.values()),
        "active": {"sessions": await setup.active_sessions(profile),
                   "operations": [operation_json(o) for o in ops]},
    }


# ── lookups ──


async def _profile_ids(conn, profile: str) -> str:
    return await profile_uuid(conn, profile)


async def _owned_binding_by_device_row(conn, profile: str, profile_id: str, device_row_id: str) -> tuple[Any, Any]:
    binding = (await conn.execute(select(BINDINGS).where(BINDINGS.c.tag_device_id == device_row_id))).first()
    if binding is None or not owns(binding, profile, profile_id):
        raise TagError(404, "device_not_found", "No device with that id.")
    comp = (await conn.execute(select(COMPANIONS).where(COMPANIONS.c.id == binding.companion_id))).first()
    if comp is None or not owns(comp, profile, profile_id):
        raise TagError(404, "device_not_found", "No device with that id.")
    return binding, comp


async def _usable_companions(conn, profile: str, profile_id: str) -> list[Any]:
    return (await conn.execute(select(COMPANIONS).where(
        COMPANIONS.c.mode == PRIVATE, COMPANIONS.c.owner_profile == profile,
        COMPANIONS.c.owner_profile_id == profile_id, COMPANIONS.c.state == "active",
    ).order_by(COMPANIONS.c.created_at))).all()


async def _ready_gateway(conn, comp: Any) -> bool:
    row = (await conn.execute(select(BINDINGS.c.state).where(
        BINDINGS.c.companion_id == comp.id, BINDINGS.c.role == "gateway"))).first()
    return row is not None and row.state in GATEWAY_READY


async def _serving_gateway(conn, comp: Any) -> Any:
    """The worker's gateway device row when the gateway itself can take a tag:
    its binding is ready and it serves tags on its own radio. ``None``
    otherwise (whether a paused connection counts is the caller's call)."""
    binding = (await conn.execute(select(BINDINGS.c.state, BINDINGS.c.tag_device_id).where(
        BINDINGS.c.companion_id == comp.id, BINDINGS.c.role == "gateway"))).first()
    if binding is None or binding.state not in GATEWAY_READY or not binding.tag_device_id:
        return None
    row = (await conn.execute(select(DEVICES).where(DEVICES.c.id == binding.tag_device_id))).first()
    return row if row is not None and serves_tags(row.info) else None


async def _can_parent(conn, comp: Any, parent: Any) -> bool:
    """``parent`` (a device row) can take a tag of this worker now: one of its
    bridges that is ready, or its gateway while that serves tags itself."""
    if parent is None or parent.companion_id != comp.id:
        return False
    if parent.kind == "bridge":
        state = (await conn.execute(select(BINDINGS.c.state).where(
            BINDINGS.c.tag_device_id == parent.id))).scalar_one_or_none()
        return state == "ready"
    if parent.kind == "gateway":
        gateway = await _serving_gateway(conn, comp)
        return gateway is not None and gateway.id == parent.id
    return False


async def _tags_on(conn, parent_id: str) -> list[str]:
    """The tags whose parent is ``parent_id`` (a bridge or a gateway)."""
    return list((await conn.execute(select(DEVICES.c.id).where(
        DEVICES.c.bridge_device_id == parent_id, DEVICES.c.kind == "tag"))).scalars().all())


async def _taken(conn, parent_id: str, *, besides: str | None = None) -> int:
    """How many tags ``parent_id`` serves (other than ``besides``)."""
    conds = [DEVICES.c.bridge_device_id == parent_id, DEVICES.c.kind == "tag"]
    if besides is not None:
        conds.append(DEVICES.c.id != besides)
    return int((await conn.execute(select(func.count()).select_from(DEVICES).where(*conds))).scalar_one() or 0)


async def _reserved(conn, companion_id: str, parent_id: str) -> int:
    """Tag pairings onto ``parent_id`` still running whose tag has no device row
    yet (once the device took its grant, its row counts in :func:`_taken`)."""
    rows = (await conn.execute(select(OPERATIONS.c.args).where(
        OPERATIONS.c.kind == "pair_tag", OPERATIONS.c.state.in_(OPEN_STATES),
        OPERATIONS.c.companion_id == companion_id, OPERATIONS.c.binding_id.is_(None)))).scalars().all()
    return sum(1 for args in rows if isinstance(args, dict) and args.get("bridge_id") == parent_id)


def _full_message(parent: Any) -> str:
    return "That gateway has no room for more tags." if parent.kind == "gateway" else "That bridge is full."


# ── discovery ──


def discovery_json(row: Any, now: float | None = None) -> dict[str, Any]:
    r = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    now = now or now_ms()
    args = r.get("args") or {}
    result = r.get("result") or {}
    candidates = list(r.get("result", {}) and result.get("candidates") or [])
    eligible = [c for c in candidates if c.get("eligible")]
    recommended = max(eligible, key=lambda c: c.get("rssi") if isinstance(c.get("rssi"), int) else -999,
                      default=None)
    finished = r["state"] in FINAL_STATES or float(r["expires_at"]) <= now or bool(result.get("finished"))
    error_code = (r.get("error") or {}).get("code")
    # What heard the device: the gateway (a bridge's search always) or a bridge.
    # A search stored before the gateway could take tags only had bridges.
    default_kind = "gateway" if args.get("role") == "bridge" else "bridge"
    if r["state"] == "cancelled":
        state = "cancelled"
    elif r["state"] == "failed" and not candidates and error_code not in (None, "not_found"):
        state = "failed"
    elif eligible or (candidates and finished):
        state = "found"
    elif finished:
        state = "not_found"
    else:
        state = "scanning"
    return {
        "id": r["id"], "role": args.get("role"), "short_id": f"{int(args.get('short_id') or 0):08X}",
        "state": state, "started_at": iso(r["created_at"]), "expires_at": iso(r["expires_at"]),
        "candidates": [{**{k: c.get(k) for k in ("id", "gateway_id", "bridge_id", "bridge_name", "rssi", "seen_at",
                                                  "capacity", "eligible", "reason")},
                        "bridge_kind": c.get("bridge_kind") or default_kind} for c in candidates],
        "recommended": recommended["id"] if recommended else None,
        "error": r.get("error"),
    }


async def start_discovery(profile: str, body: dict[str, Any]) -> dict[str, Any]:
    require_simple_setup()
    from app.tags.authority import AuthorityUnavailable, get_authority
    from app.tags.vault import seal_secret

    role = body.get("role")
    if role not in ("bridge", "tag"):
        raise TagError(422, "invalid_role", "'role' must be 'bridge' or 'tag'.")
    try:
        payload = v2.parse_setup_code(body.get("setup_code"), role=role)
    except v2.SetupCodeError as exc:
        raise TagError(422, exc.code, exc.message) from None
    duration = body.get("duration_s", DISCOVERY_DEFAULT_S[role])
    if isinstance(duration, bool) or not isinstance(duration, int) or not 10 <= duration <= DISCOVERY_MAX_S:
        raise TagError(422, "invalid_duration", f"'duration_s' must be 10 to {DISCOVERY_MAX_S} seconds.")
    try:
        authority = await get_authority()
    except AuthorityUnavailable as exc:
        raise TagError(503, exc.code, exc.message) from None
    now = now_ms()
    store = get_tag_storage()
    async with store.engine.begin() as conn:
        await begin_write(conn)
        profile_id = await _profile_ids(conn, profile)
        comps = [c for c in await _usable_companions(conn, profile, profile_id) if await _ready_gateway(conn, c)]
        if not comps:
            raise TagError(409, "no_gateway", "Connect a gateway first.")
        already = (await conn.execute(select(BINDINGS.c.id).where(
            BINDINGS.c.owner_profile_id == profile_id, BINDINGS.c.role == role,
            BINDINGS.c.short_id == payload.short_id))).first()
        if already is not None:
            raise TagError(409, "already_paired", f"This {role} is already set up.")
        targets: list[dict[str, Any]] = []
        if role == "bridge":
            gateway_id = body.get("gateway_id")
            if gateway_id is None and len(comps) > 1:
                raise TagError(409, "gateway_required", "Choose the gateway the bridge should join.")
            chosen = [c for c in comps if gateway_id is None or c.id == gateway_id]
            if not chosen:
                raise TagError(404, "connection_not_found", "No connected gateway with that id.")
            if chosen[0].paused:
                raise TagError(409, "gateway_paused", "The gateway is paused; resume it first.")
            targets.append({"companion_id": chosen[0].id, "bridges": []})
        else:
            # Every place a tag can live listens: the gateway's own radio when it
            # serves tags (its hw id, first), and every ready bridge.
            running = [c for c in comps if not c.paused]
            for comp in running:
                bridges = (await conn.execute(select(BINDINGS.c.tag_device_id).where(
                    BINDINGS.c.companion_id == comp.id, BINDINGS.c.role == "bridge", BINDINGS.c.state == "ready",
                ))).scalars().all()
                hw = sorted((await conn.execute(select(DEVICES.c.hw_id).where(
                    DEVICES.c.id.in_(list(bridges))))).scalars().all()) if bridges else []
                gateway = await _serving_gateway(conn, comp)
                places = ([gateway.hw_id] if gateway is not None else []) + hw
                if places:
                    targets.append({"companion_id": comp.id, "bridges": places})
            if not targets:
                # The code stays no_ready_bridge (clients know it); the words say what is missing.
                raise TagError(409, "no_ready_bridge", "Your gateway is paused: resume it first." if not running else
                               "Your gateway cannot reach tags itself: add a bridge first, and wait until it "
                               "shows Ready.")
        op_id = str(uuid.uuid4())
        args = {"role": role, "short_id": payload.short_id, "duration_s": duration,
                "companions": [t["companion_id"] for t in targets], "targets": targets}
        sealed = seal_secret(authority, payload.secret, op_id, profile_id)
        row = await queue_operation(conn, kind="discovery", profile=profile, profile_id=profile_id,
                                    companion_id=targets[0]["companion_id"] if len(targets) == 1 else None,
                                    args=args, now=now, secret_sealed=sealed, state="running", stage="scanning",
                                    ttl_s=duration + DISCOVERY_SLACK_MS / 1000.0, op_id=op_id, run=False)
        command_ids = []
        for t in targets:
            cmd = await insert_command(conn, companion_id=t["companion_id"], kind=RUN_OPERATION,
                                       args={"operation_id": op_id, "kind": "discovery"}, requested_by=profile,
                                       ttl_s=duration + 30, now=now)
            command_ids.append(cmd["id"])
        await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == op_id).values(
            command_ids=command_ids, result={"candidates": [], "done": []}))
        row = (await conn.execute(select(OPERATIONS).where(OPERATIONS.c.id == op_id))).first()
    notify_commands([t["companion_id"] for t in targets])
    return discovery_json(row)


async def _own_operation(conn, profile: str, op_id: str, kinds: tuple[str, ...]) -> Any:
    row = (await conn.execute(select(OPERATIONS).where(
        OPERATIONS.c.id == op_id, OPERATIONS.c.owner_profile == profile))).first()
    if row is None or row.kind not in kinds:
        raise TagError(404, "not_found", "No such operation.")
    return row


async def get_discovery(profile: str, op_id: str) -> dict[str, Any]:
    async with get_tag_storage().engine.connect() as conn:
        row = await _own_operation(conn, profile, op_id, ("discovery",))
    return discovery_json(row)


# ── pairing ──


async def _first_tag(conn, profile_id: str) -> bool:
    count = (await conn.execute(select(func.count()).select_from(BINDINGS).where(
        BINDINGS.c.owner_profile_id == profile_id, BINDINGS.c.role == "tag", BINDINGS.c.state == "ready",
    ))).scalar_one()
    return int(count or 0) == 0


async def pairing_json(conn, row: Any, now: float | None = None) -> dict[str, Any]:
    out = operation_json(row)
    args = row.args or {}
    out["role"] = args.get("role")
    out["first_tag"] = bool(args.get("first_tag"))
    if row.binding_id:
        binding = (await conn.execute(select(BINDINGS).where(BINDINGS.c.id == row.binding_id))).first()
        comp = (await conn.execute(select(COMPANIONS).where(COMPANIONS.c.id == row.companion_id))).first() \
            if row.companion_id else None
        if binding is not None and comp is not None:
            dev = (await _devices_by_id(conn, [binding.tag_device_id] if binding.tag_device_id else [])).get(
                binding.tag_device_id or "")
            out["device"] = device_view(binding, dev, companion=comp, now=now or now_ms())
    return out


async def start_pairing(profile: str, body: dict[str, Any]) -> dict[str, Any]:
    require_simple_setup()
    from app.tags.authority import AuthorityUnavailable, get_authority
    from app.tags.vault import open_secret, seal_secret

    discovery_id, candidate_id = body.get("discovery_id"), body.get("candidate_id")
    if not isinstance(discovery_id, str) or not isinstance(candidate_id, str):
        raise TagError(422, "invalid_pairing", "'discovery_id' and 'candidate_id' are required.")
    name = body.get("name")
    if name is not None:
        from app.tags.service import device_name

        name = device_name(name)
    try:
        authority = await get_authority()
    except AuthorityUnavailable as exc:
        raise TagError(503, exc.code, exc.message) from None
    now = now_ms()
    store = get_tag_storage()
    async with store.engine.begin() as conn:
        await begin_write(conn)
        profile_id = await _profile_ids(conn, profile)
        disc = await _own_operation(conn, profile, discovery_id, ("discovery",))
        view = discovery_json(disc, now)
        cand = next((c for c in (disc.result or {}).get("candidates") or [] if c.get("id") == candidate_id), None)
        if cand is None:
            raise TagError(404, "not_found", "No such candidate in that search.")
        if not cand.get("eligible"):
            full = "That gateway cannot take another tag." if cand.get("bridge_kind") == "gateway" else \
                "That bridge cannot take another tag."
            raise TagError(409, "candidate_not_eligible",
                           full if cand.get("reason") == "bridge_full" else "That candidate cannot be used.")
        if view["state"] == "cancelled" or disc.secret_sealed is None:
            raise TagError(409, "discovery_closed", "Search again: this search is closed.")
        args = disc.args or {}
        role = args.get("role")
        comp = (await conn.execute(select(COMPANIONS).where(COMPANIONS.c.id == cand.get("gateway_id")))).first()
        if comp is None or not owns(comp, profile, profile_id) or comp.state != "active":
            raise TagError(409, "gateway_offline", "The gateway for that candidate is not available.")
        secret = open_secret(authority, disc.secret_sealed, disc.id, profile_id)
        op_id = str(uuid.uuid4())
        op_args: dict[str, Any] = {"role": role, "short_id": int(args.get("short_id") or 0), "name": name or "",
                                   "first_tag": role == "tag" and await _first_tag(conn, profile_id)}
        if role == "bridge":
            op_args.update({"uuid": cand.get("uuid"), "gateway_device_id": comp.gateway_device_id})
        else:
            # The tag's parent: the bridge that heard it, or the gateway's own radio. Locked, so two
            # pairings cannot both take its last slot.
            parent = (await conn.execute(select(DEVICES).where(
                DEVICES.c.id == cand.get("bridge_id")).with_for_update())).first()
            if not await _can_parent(conn, comp, parent):
                raise TagError(409, "candidate_not_eligible",
                               "That gateway can no longer reach tags itself. Search again."
                               if cand.get("bridge_kind") == "gateway" else "That bridge is no longer available.")
            max_tags = parent_capacity(parent.kind, parent.info)
            if max_tags is not None:
                taken = await _taken(conn, parent.id)
                if taken + await _reserved(conn, comp.id, parent.id) >= max_tags:
                    raise TagError(409, "bridge_full", _full_message(parent), bridge={"id": parent.id})
            op_args.update({"tag_id": int(args.get("short_id") or 0), "bridge_hw_id": parent.hw_id,
                            "bridge_id": parent.id})
        row = await queue_operation(conn, kind=_PAIR_KINDS[role], profile=profile, profile_id=profile_id,
                                    companion_id=comp.id, args=op_args, now=now, parent_id=disc.id,
                                    secret_sealed=seal_secret(authority, secret, op_id, profile_id), op_id=op_id,
                                    stage="waiting_for_device")
        # One pairing per search: the search's copy of the secret is no longer needed.
        await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == disc.id).values(
            secret_sealed=None, state="succeeded" if disc.state not in FINAL_STATES else disc.state,
            finished_at=now, updated_at=now))
        out = await pairing_json(conn, (await conn.execute(select(OPERATIONS).where(OPERATIONS.c.id == row["id"]))).first(), now)
    notify_commands([comp.id])
    return out


async def get_pairing(profile: str, op_id: str) -> dict[str, Any]:
    async with get_tag_storage().engine.connect() as conn:
        row = await _own_operation(conn, profile, op_id, _PAIRING_KINDS)
        return await pairing_json(conn, row)


async def cancel_operation_row(conn, row: Any, now: float, *, reason: str = "cancelled") -> None:
    if row.state in FINAL_STATES:
        return
    await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == row.id).values(
        state="cancelled", stage="cancelled", secret_sealed=None, error={"code": "cancelled", "message": reason},
        finished_at=now, updated_at=now))
    if row.command_ids:
        await conn.execute(update(COMMANDS).where(
            COMMANDS.c.id.in_(list(row.command_ids)), COMMANDS.c.status == "queued").values(
            status="cancelled", completed_at=now, error=reason))


async def cancel_pairing(profile: str, op_id: str) -> dict[str, Any]:
    """Cancel a pairing. A worker already running it sees ``cancelled`` on its
    next look at the operation and reconciles (removes a provisioned but
    unbound bridge; probes a tag that may have committed)."""
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        row = await _own_operation(conn, profile, op_id, _PAIRING_KINDS)
        await cancel_operation_row(conn, row, now, reason="Cancelled.")
        if row.binding_id and row.state not in FINAL_STATES:
            binding = (await conn.execute(select(BINDINGS).where(BINDINGS.c.id == row.binding_id))).first()
            if binding is not None and binding.state == "pairing":
                await _forget_binding(conn, binding, now, reason="pairing cancelled", cleanup="pending")
        row = (await conn.execute(select(OPERATIONS).where(OPERATIONS.c.id == op_id))).first()
        out = await pairing_json(conn, row, now)
    if row.companion_id:
        notify_commands([row.companion_id])
    return out


def _protocol(binding: Any) -> int:
    """1 for a tag imported from the hardware tools (no v2 ownership to release or rekey), else 2."""
    return 1 if (binding.info or {}).get("proto") == 1 else 2


async def _forget_binding(conn, binding: Any, now: float, *, reason: str, cleanup: str) -> None:
    await tombstone(conn, binding, reason=reason, cleanup=cleanup, now=now)
    await conn.execute(delete(BINDINGS).where(BINDINGS.c.id == binding.id))
    if binding.tag_device_id:
        await conn.execute(delete(DEVICES).where(DEVICES.c.id == binding.tag_device_id))


# ── import (a tag enrolled with the hardware tools) ──


def v1_device_id(tag_id: int) -> str:
    """A v1 tag's stand-in device id (hex): its tag id, little-endian, so that its short id and hw id stay
    the tag id, then a fixed hash of it. A v1 tag has no identity key; this only keys its binding, so the
    same tag cannot be bound twice on this server."""
    raw = tag_id.to_bytes(4, "little")
    return (raw + hashlib.sha256(_V1_DEVICE_LABEL + raw).digest()[:12]).hex()


def _import_tag_id(value: Any) -> int:
    text = str(value if value is not None else "").strip()
    text = text[2:] if text.lower().startswith("0x") else text
    if len(text) == 8 and set(text.lower()) <= _HEX32 and 1 <= int(text, 16) <= 0xFFFFFFFE:
        return int(text, 16)
    raise TagError(422, "invalid_tag_id", "'tag_id' is the tag id the hardware tools printed: 8 hex digits, "
                                          "e.g. 1A2B3C4D.")


async def _may_import_on(conn, comp: Any, profile: str, profile_id: str) -> bool:
    """The gateway's worker reads the tag's secret from the hardware tools on its own computer, so only the
    owner of that computer may import: the admin on the server's computer, the enrolling profile on a
    desktop it enrolled."""
    from app.tags.hosts import HOSTS

    host = (await conn.execute(select(HOSTS).where(HOSTS.c.id == comp.host_id))).first() if comp.host_id else None
    if host is None or host.state != "active":
        return False
    if host.kind == "server":
        return profile == "admin"
    return host.kind == "desktop" and host.owner_profile_id == profile_id


async def _import_parent(conn, comp: Any) -> tuple[Any, str | None]:
    """Where an imported tag connects: the gateway's own radio while it reaches tags, else the first ready
    bridge with room (locked, so two adds cannot both take its last slot). ``(None, reason)`` when none."""
    gateway = await _serving_gateway(conn, comp)
    bridges = (await conn.execute(select(BINDINGS.c.tag_device_id).where(
        BINDINGS.c.companion_id == comp.id, BINDINGS.c.role == "bridge", BINDINGS.c.state == "ready",
    ).order_by(BINDINGS.c.created_at))).scalars().all()
    places = ([gateway.id] if gateway is not None else []) + [b for b in bridges if b]
    for place in places:
        parent = (await conn.execute(select(DEVICES).where(DEVICES.c.id == place).with_for_update())).first()
        if not await _can_parent(conn, comp, parent):
            continue
        max_tags = parent_capacity(parent.kind, parent.info)
        if max_tags is not None and await _taken(conn, parent.id) + await _reserved(conn, comp.id, parent.id) >= max_tags:
            continue
        return parent, None
    return None, "bridge_full" if places else "no_ready_bridge"


async def start_import(profile: str, body: dict[str, Any]) -> dict[str, Any]:
    """Add a tag enrolled over SWD with ``cremind tags tools tag enroll`` (protocol v1: no setup label, no
    pairing). The gateway's worker takes the tag's secret and panel from the hardware tools on its computer,
    keeps the secret (and a sealed copy in the recovery vault), assigns the tag's key and clears its screen:
    the clear proves the secret is the tag's. The device row and binding exist from the start; a failed or
    cancelled import removes them. Followed like a pairing (``GET /api/tags/pairings/{id}``)."""
    require_simple_setup()
    tag_id = _import_tag_id(body.get("tag_id"))
    name = body.get("name")
    if name is not None:
        from app.tags.service import device_name

        name = device_name(name)
    gateway_id = body.get("gateway_id")
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        profile_id = await _profile_ids(conn, profile)
        comps = [c for c in await _usable_companions(conn, profile, profile_id) if await _ready_gateway(conn, c)]
        if not comps:
            raise TagError(409, "no_gateway", "Connect a gateway first.")
        if gateway_id is None and len(comps) > 1:
            raise TagError(409, "gateway_required", "Choose the gateway the tag should use.")
        comp = next((c for c in comps if gateway_id is None or c.id == gateway_id), None)
        if comp is None:
            raise TagError(404, "connection_not_found", "No connected gateway with that id.")
        if comp.paused:
            raise TagError(409, "gateway_paused", "The gateway is paused; resume it first.")
        if not await _may_import_on(conn, comp, profile, profile_id):
            raise TagError(403, "import_not_allowed", "Only the owner of the computer this gateway is plugged into "
                                                      "can add tags enrolled there with the hardware tools.")
        device = v1_device_id(tag_id)
        hw = f"{tag_id:08X}"
        bound = (await conn.execute(select(BINDINGS.c.id).where(BINDINGS.c.device_id == device))).first()
        known = (await conn.execute(select(DEVICES.c.id).where(
            DEVICES.c.companion_id == comp.id, DEVICES.c.kind == "tag", DEVICES.c.hw_id == hw))).first()
        if bound is not None or known is not None:
            raise TagError(409, "already_paired", "This tag is already set up.")
        parent, reason = await _import_parent(conn, comp)
        if parent is None:
            if reason == "bridge_full":
                raise TagError(409, "bridge_full", "Your gateway and its bridges have no room for another tag.")
            raise TagError(409, "no_ready_bridge", "Your gateway cannot reach tags itself: add a bridge first, and "
                                                   "wait until it shows Ready.")
        device_row_id, binding_id = str(uuid.uuid4()), str(uuid.uuid4())
        await conn.execute(DEVICES.insert(), [{
            "id": device_row_id, "companion_id": comp.id, "kind": "tag", "hw_id": hw, "name": name or "New tag",
            "owner_profile": profile, "bridge_device_id": parent.id, "epoch": 0, "rotation": 0,
            "info": {"device_id": device, "protocol": 1}, "status": "pairing", "desired_revision": 0,
            "displayed_revision": 0, "clear_required": True, "claimed_at": now, "created_at": now,
            "updated_at": now,
        }])
        await conn.execute(BINDINGS.insert(), [{
            "id": binding_id, "device_id": device, "role": "tag", "identity_pub": "00" * 32, "short_id": tag_id,
            "companion_id": comp.id, "tag_device_id": device_row_id, "owner_profile": profile,
            "owner_profile_id": profile_id, "state": "pairing", "generation": 0, "pending_generation": None,
            "paused": False, "fw": None, "board": None, "info": {"proto": 1}, "created_at": now, "updated_at": now,
            "paired_at": None, "ready_at": None,
        }])
        op_args = {"role": "tag", "tag_id": tag_id, "device_id": device, "name": name or "",
                   "bridge_hw_id": parent.hw_id, "bridge_id": parent.id,
                   "first_tag": await _first_tag(conn, profile_id)}
        row = await queue_operation(conn, kind=_IMPORT, profile=profile, profile_id=profile_id,
                                    companion_id=comp.id, binding_id=binding_id, args=op_args, now=now,
                                    stage="waiting_for_device")
        out = await pairing_json(conn, (await conn.execute(
            select(OPERATIONS).where(OPERATIONS.c.id == row["id"]))).first(), now)
    notify_commands([comp.id])
    return out


# ── unpair (Remove), pause, move, test ──


async def unpair(profile: str, device_row_id: str, body: dict[str, Any]) -> dict[str, Any]:
    require_simple_setup()
    force = body.get("force", False)
    if not isinstance(force, bool):
        raise TagError(422, "invalid_force", "'force' must be true or false.")
    now = now_ms()
    wake: list[str] = []
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        profile_id = await _profile_ids(conn, profile)
        binding, comp = await _owned_binding_by_device_row(conn, profile, profile_id, device_row_id)
        if force:
            # Nothing physical will finish: revoke now, keep the tombstones.
            if binding.role == "gateway":
                for b in (await conn.execute(select(BINDINGS).where(BINDINGS.c.companion_id == comp.id))).all():
                    await tombstone(conn, b, reason="removed (forced)", cleanup="abandoned", now=now)
                await conn.execute(update(CREDENTIALS).where(
                    CREDENTIALS.c.companion_id == comp.id, CREDENTIALS.c.revoked_at.is_(None)).values(revoked_at=now))
                await conn.execute(delete(COMPANIONS).where(COMPANIONS.c.id == comp.id))
            else:
                await _forget_binding(conn, binding, now, reason="removed (forced)", cleanup="abandoned")
            return {"operation": None, "device": None}
        dev = (await conn.execute(select(DEVICES).where(DEVICES.c.id == device_row_id))).first()
        affected: list[str] = []
        if binding.role == "gateway":
            kind = "release_gateway"
            await conn.execute(update(COMPANIONS).where(COMPANIONS.c.id == comp.id).values(
                state="removing", updated_at=now))
            # Content is revoked at once; the hardware credential stays for the cleanup.
            await conn.execute(update(CREDENTIALS).where(
                CREDENTIALS.c.companion_id == comp.id, CREDENTIALS.c.kind == "content",
                CREDENTIALS.c.revoked_at.is_(None)).values(revoked_at=now))
            targets = (await conn.execute(select(BINDINGS).where(BINDINGS.c.companion_id == comp.id))).all()
        else:
            kind = "unpair"
            targets = [binding]
        for b in targets:
            await conn.execute(update(BINDINGS).where(BINDINGS.c.id == b.id).values(
                state="removal_pending", updated_at=now))
            if b.role == "tag" and b.tag_device_id:
                await cancel_device_deliveries(conn, b.tag_device_id, now, "removed")
                await conn.execute(update(DEVICES).where(DEVICES.c.id == b.tag_device_id).values(
                    clear_required=True, status="removing", updated_at=now))
        if binding.role == "bridge" and dev is not None:
            affected = list((await conn.execute(select(DEVICES.c.id).where(
                DEVICES.c.bridge_device_id == dev.id, DEVICES.c.kind == "tag"))).scalars().all())
            if affected:
                await conn.execute(update(DEVICES).where(DEVICES.c.id.in_(affected)).values(
                    status="needs_bridge", updated_at=now))
        args = {"device_id": binding.device_id, "role": binding.role, "hw_id": dev.hw_id if dev is not None else None,
                "generation": int(binding.generation or 0), "epoch": int(dev.epoch or 0) if dev is not None else 0,
                "affected": affected, "protocol": _protocol(binding),
                "devices": [{"device_id": b.device_id, "role": b.role, "generation": int(b.generation or 0),
                             "protocol": _protocol(b)} for b in targets]}
        op = await queue_operation(conn, kind=kind, profile=profile, profile_id=profile_id, companion_id=comp.id,
                                   binding_id=binding.id, args=args, now=now, stage="queued")
        wake.append(comp.id)
        binding = (await conn.execute(select(BINDINGS).where(BINDINGS.c.id == binding.id))).first()
        comp = (await conn.execute(select(COMPANIONS).where(COMPANIONS.c.id == comp.id))).first()
        dev_json = device_json(dev) if dev is not None else None
        assigned = await _taken(conn, dev.id) if dev is not None and binding.role != "tag" else 0
        out = {"operation": operation_json(op), "device": device_view(binding, dev_json, companion=comp, now=now,
                                                                       assigned=assigned, affected=affected)}
    notify_commands(wake)
    return out


async def set_paused(profile: str, device_row_id: str, paused: bool) -> dict[str, Any]:
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        profile_id = await _profile_ids(conn, profile)
        binding, comp = await _owned_binding_by_device_row(conn, profile, profile_id, device_row_id)
        if binding.role == "gateway":
            await conn.execute(update(COMPANIONS).where(COMPANIONS.c.id == comp.id).values(
                paused=paused, updated_at=now))
        else:
            await conn.execute(update(BINDINGS).where(BINDINGS.c.id == binding.id).values(
                paused=paused, updated_at=now))
            if binding.role == "tag" and binding.tag_device_id:
                dev = (await conn.execute(select(DEVICES).where(DEVICES.c.id == binding.tag_device_id))).first()
                if dev is not None and (dev.status == "paused") != paused:
                    await conn.execute(update(DEVICES).where(DEVICES.c.id == dev.id).values(
                        status="paused" if paused else "ok", updated_at=now))
        binding = (await conn.execute(select(BINDINGS).where(BINDINGS.c.id == binding.id))).first()
        comp = (await conn.execute(select(COMPANIONS).where(COMPANIONS.c.id == comp.id))).first()
        dev = await _devices_by_id(conn, [device_row_id])
        served = await _tags_on(conn, device_row_id) if binding.role != "tag" else []
        return {"device": device_view(binding, dev.get(device_row_id), companion=comp, now=now,
                                      assigned=len(served), affected=served)}


async def move_tag(profile: str, device_row_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Serve one of the profile's tags through another place of the same
    gateway: the gateway itself, while it serves tags on its own radio, or a
    ready bridge (after its bridge was removed, or by choice)."""
    require_simple_setup()
    bridge_id = body.get("bridge_id")
    if not isinstance(bridge_id, str):
        raise TagError(422, "invalid_bridge", "'bridge_id' is required: the gateway or bridge the tag should use.")
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        profile_id = await _profile_ids(conn, profile)
        binding, comp = await _owned_binding_by_device_row(conn, profile, profile_id, device_row_id)
        if binding.role != "tag" or binding.state not in ("ready", "paired"):
            raise TagError(409, "not_movable", "Only a ready tag can be moved.")
        # Locked, so two moves cannot both take the target's last slot.
        target = (await conn.execute(select(DEVICES).where(DEVICES.c.id == bridge_id).with_for_update())).first()
        if not await _can_parent(conn, comp, target):
            raise TagError(409, "candidate_not_eligible",
                           "Choose the tag's gateway (if it can reach tags itself) or a ready bridge of that gateway.")
        max_tags = parent_capacity(target.kind, target.info)
        if max_tags is not None and await _taken(conn, target.id, besides=device_row_id) >= max_tags:
            raise TagError(409, "bridge_full", _full_message(target), bridge={"id": target.id})
        dev = (await conn.execute(select(DEVICES).where(DEVICES.c.id == device_row_id))).first()
        op = await queue_operation(conn, kind="move_tag", profile=profile, profile_id=profile_id,
                                   companion_id=comp.id, binding_id=binding.id, now=now,
                                   args={"device_id": binding.device_id, "tag_id": int(binding.short_id or 0),
                                         "hw_id": dev.hw_id, "bridge_hw_id": target.hw_id, "bridge_id": target.id,
                                         "epoch": int(dev.epoch or 0)})
    notify_commands([comp.id])
    return {"operation": operation_json(op)}


async def send_test(profile: str, device_row_id: str) -> dict[str, Any]:
    from app.tags import service

    await service.owned_tag(profile, device_row_id)
    return await service.display(profile, device_row_id, {
        "title": "Hello from Cremind", "body": "This tag is set up and receiving updates.",
        "icon": "check_circle", "ttl_s": 3600, "replace": True,
    })


# ── recovery ──


def _recovery_json(row: Any, bindings: list[Any]) -> dict[str, Any]:
    out = operation_json(row)
    out["companion_id"] = row.companion_id
    done = {d.get("device_id"): d.get("state") for d in ((row.result or {}).get("devices") or [])}
    devices = []
    for b in bindings:
        state = done.get(b.device_id) or ("rekeyed" if b.state == "ready" else
                                          "recovery_pending" if b.state == "recovery_pending" else "pending")
        devices.append({"id": b.tag_device_id, "kind": b.role, "name": (row.args or {}).get("names", {}).get(b.device_id, ""),
                        "state": state})
    out["devices"] = devices
    return out


async def start_recovery(profile: str, body: dict[str, Any], *, authorization: str | None) -> dict[str, Any]:
    require_simple_setup()
    from app.tags import setup

    companion_id = body.get("companion_id")
    now = now_ms()
    op_id = str(uuid.uuid4())
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        profile_id = await _profile_ids(conn, profile)
        from app.tags.ownership import owned_companion_row

        comp = await owned_companion_row(conn, profile, profile_id, companion_id)
        if comp.state == "removing":
            raise TagError(409, "removal_pending", "This connection is being removed.")
        await queue_operation(conn, kind="recover_gateway", profile=profile, profile_id=profile_id,
                              companion_id=comp.id, args={}, now=now, state=WAITING, stage=WAITING,
                              op_id=op_id, run=False)
    try:
        session, url = await setup.create_session(profile, operation="recover", server_url=body.get("server_url"),
                                                  companion_id=companion_id, authorization=authorization,
                                                  recovery_operation_id=op_id)
    except TagError:
        async with get_tag_storage().engine.begin() as conn:
            await conn.execute(delete(OPERATIONS).where(OPERATIONS.c.id == op_id))
        raise
    return {"recovery": await get_recovery(profile, op_id), "session": session, "launch_url": url}


async def get_recovery(profile: str, op_id: str) -> dict[str, Any]:
    async with get_tag_storage().engine.connect() as conn:
        row = await _own_operation(conn, profile, op_id, ("recover_gateway",))
        bindings = (await conn.execute(select(BINDINGS).where(BINDINGS.c.companion_id == row.companion_id)
                                       .order_by(BINDINGS.c.role, BINDINGS.c.created_at))).all() \
            if row.companion_id else []
    return _recovery_json(row, bindings)


async def cancel_waiting_recovery(conn, op_id: str, now: float) -> None:
    await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == op_id, OPERATIONS.c.state == WAITING).values(
        state="cancelled", stage="cancelled", finished_at=now, updated_at=now))


# ── worker side (connector v2) ──


async def worker_companion(conn, grant: dict[str, Any]) -> Any:
    comp = (await conn.execute(select(COMPANIONS).where(COMPANIONS.c.id == grant["companion_id"]))).first()
    if comp is None:
        raise TagError(404, "companion_not_found", "The worker no longer exists.")
    if comp.mode != PRIVATE:
        raise TagError(409, "legacy_companion", "This companion uses the manual (legacy) setup.")
    return comp


def _worker_state(comp: Any) -> str:
    if comp.state in ("recovering", "removing", "removed"):
        return comp.state
    return "paused" if comp.paused else "active"


async def lease(grant: dict[str, Any]) -> dict[str, Any]:
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        comp = await worker_companion(conn, grant)
        expires = now + LEASE_TTL_S * 1000.0
        await conn.execute(update(COMPANIONS).where(COMPANIONS.c.id == comp.id).values(
            lease_expires_at=expires, lease_credential_id=grant["credential_id"], last_seen_at=now))
    return {"lease_id": f"{comp.id}:{int(comp.generation or 0)}", "expires_at": iso(expires), "ttl_s": LEASE_TTL_S,
            "renew_s": LEASE_RENEW_S, "state": _worker_state(comp), "paused": bool(comp.paused),
            "generation": int(comp.generation or 0)}


async def worker_state(grant: dict[str, Any]) -> dict[str, Any]:
    async with get_tag_storage().engine.connect() as conn:
        comp = await worker_companion(conn, grant)
        bindings = (await conn.execute(select(BINDINGS).where(BINDINGS.c.companion_id == comp.id))).all()
        devs = await _devices_by_id(conn, [b.tag_device_id for b in bindings if b.tag_device_id])
        revoked = (await conn.execute(select(REVOCATIONS.c.device_id).where(
            REVOCATIONS.c.last_owner_profile_id == comp.owner_profile_id))).scalars().all()
        ops = (await conn.execute(select(OPERATIONS.c.id, OPERATIONS.c.kind, OPERATIONS.c.state).where(
            OPERATIONS.c.companion_id == comp.id, OPERATIONS.c.state.in_(OPEN_STATES)))).all()
    return {
        "generation": int(comp.generation or 0), "state": _worker_state(comp), "paused": bool(comp.paused),
        "bindings": [{"device_id": b.device_id, "role": b.role,
                      "hw_id": (devs.get(b.tag_device_id or "") or {}).get("hw_id"),
                      "generation": int(b.generation or 0), "state": b.state, "paused": bool(b.paused)}
                     for b in bindings],
        "revoked": sorted(revoked),
        "operations": [{"id": o.id, "kind": o.kind, "state": o.state} for o in ops],
    }


def _worker_may_see(op: Any, comp: Any) -> bool:
    if op.companion_id == comp.id:
        return True
    return op.kind == "discovery" and comp.id in ((op.args or {}).get("companions") or [])


async def worker_operation(grant: dict[str, Any], op_id: str) -> dict[str, Any]:
    from app.tags.authority import AuthorityUnavailable, get_authority
    from app.tags.vault import open_secret

    async with get_tag_storage().engine.connect() as conn:
        comp = await worker_companion(conn, grant)
        op = (await conn.execute(select(OPERATIONS).where(OPERATIONS.c.id == op_id))).first()
        if op is None or not _worker_may_see(op, comp):
            raise TagError(404, "operation_not_found", "No such operation for this worker.")
    secret = None
    if op.secret_sealed and op.state in OPEN_STATES and op.kind in ("pair_bridge", "pair_tag"):
        try:
            secret = open_secret(await get_authority(), op.secret_sealed, op.id, op.owner_profile_id).hex()
        except AuthorityUnavailable as exc:
            raise TagError(503, exc.code, exc.message) from None
    args = dict(op.args or {})
    if op.kind == "discovery":
        # ``bridges``: the hw ids to listen on — ready bridges, and the gateway's own ``gw-…`` id
        # when it serves tags on its own radio.
        target = next((t for t in args.get("targets") or [] if t.get("companion_id") == comp.id), {})
        args = {"role": args.get("role"), "short_id": args.get("short_id"), "duration_s": args.get("duration_s"),
                "bridges": target.get("bridges") or []}
    return {"operation": {"id": op.id, "kind": op.kind, "state": op.state, "stage": op.stage, "args": args,
                          "setup_secret": secret, "owner": v2.owner_bytes(op.owner_profile_id).hex(),
                          "expires_at": iso(op.expires_at)}}


async def _discovery_progress(conn, op: Any, comp: Any, body: dict[str, Any], now: float) -> None:
    args = op.args or {}
    result = dict(op.result or {"candidates": [], "done": []})
    candidates = list(result.get("candidates") or [])
    for item in (body.get("candidates") or [])[:40]:
        if not isinstance(item, dict):
            continue
        rssi = item.get("rssi") if isinstance(item.get("rssi"), int) and not isinstance(item.get("rssi"), bool) else None
        if args.get("role") == "bridge":
            uuid_hex = _hex(item.get("uuid"), 16)
            if uuid_hex is None or v2.short_id(bytes.fromhex(uuid_hex)) != int(args.get("short_id") or -1):
                continue
            key = ("bridge", comp.id, uuid_hex)
            cand = {"gateway_id": comp.id, "bridge_id": None, "bridge_kind": "gateway", "bridge_name": None,
                    "uuid": uuid_hex, "capacity": None, "eligible": True, "reason": None}
        else:
            if _u32(item.get("tag_id")) != int(args.get("short_id") or -1):
                continue
            # What heard it: a ready bridge, or the gateway's own radio (``bridge_hw_id`` = the
            # gateway's ``gw-…`` id) while the gateway serves tags.
            parent = (await conn.execute(select(DEVICES).where(
                DEVICES.c.companion_id == comp.id, DEVICES.c.kind.in_(("bridge", "gateway")),
                DEVICES.c.hw_id == str(item.get("bridge_hw_id") or "")))).first()
            if not await _can_parent(conn, comp, parent):
                continue
            max_tags = parent_capacity(parent.kind, parent.info)
            taken = await _taken(conn, parent.id)
            full = max_tags is not None and taken >= max_tags
            key = ("tag", comp.id, parent.id)
            # A gateway is named as the page names its connection.
            name = (parent.name or (comp.name if parent.kind == "gateway" else "")) or None
            cand = {"gateway_id": comp.id, "bridge_id": parent.id, "bridge_kind": parent.kind, "bridge_name": name,
                    "capacity": {"max_tags": max_tags, "assigned": taken} if max_tags else None,
                    "eligible": not full, "reason": "bridge_full" if full else None}
        existing = next((c for c in candidates if tuple(c.get("_key") or ()) == key), None)
        if existing is None:
            candidates.append({"id": f"c{len(candidates) + 1}", "_key": list(key), "rssi": rssi,
                               "seen_at": iso(now), **cand})
        else:
            if rssi is not None and (existing.get("rssi") is None or rssi > existing["rssi"]):
                existing["rssi"] = rssi
            existing["seen_at"] = iso(now)
            existing.update({k: v for k, v in cand.items() if k in ("capacity", "eligible", "reason")})
    result["candidates"] = candidates
    if body.get("state") in ("succeeded", "failed"):
        done = set(result.get("done") or [])
        done.add(comp.id)
        result["done"] = sorted(done)
        if set((args.get("companions") or [])) <= done:
            result["finished"] = True
    values: dict[str, Any] = {"result": result, "updated_at": now}
    if result.get("finished"):
        values.update(state="succeeded" if candidates else "failed",
                      stage="done", finished_at=now)
        if not candidates:
            values["error"] = {"code": "not_found", "message": "Nothing answered with that setup code."}
    await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == op.id).values(**values))


async def _device_report(conn, op: Any, comp: Any, report: dict[str, Any], now: float) -> None:
    """A worker's report about the device an operation works on: telemetry
    and configuration (never identity — that comes from the grant)."""
    if not op.binding_id:
        return
    binding = (await conn.execute(select(BINDINGS).where(BINDINGS.c.id == op.binding_id))).first()
    if binding is None or binding.companion_id != comp.id:
        return
    gen = _u32(report.get("gen"))
    if gen is not None and gen in (int(binding.generation or 0), int(binding.pending_generation or -1)):
        await conn.execute(update(BINDINGS).where(BINDINGS.c.id == binding.id).values(
            generation=gen, pending_generation=None if gen == binding.pending_generation else binding.pending_generation,
            updated_at=now))
    if not binding.tag_device_id:
        return
    dev = (await conn.execute(select(DEVICES).where(DEVICES.c.id == binding.tag_device_id))).first()
    if dev is None:
        return
    values: dict[str, Any] = {"updated_at": now}
    info = dict(dev.info or {})
    if isinstance(report.get("fw"), str):
        values["fw"] = report["fw"][:32]
    for key in ("board", "panel", "width", "height", "planes"):
        value = report.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 65535:
            values[key] = value
    if binding.role == "bridge":
        for key, lo, hi in (("max_tags", 1, 255), ("assigned", 0, 255), ("addr", 1, 0x7FFF)):
            value = report.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi:
                info[key] = value
        if isinstance(report.get("fontpack_id"), str):
            info["fontpack_id"] = report["fontpack_id"][:32]
        if isinstance(report.get("fontpack_ok"), bool):
            info["fontpack_ok"] = report["fontpack_ok"]
    if binding.role == "tag" and op.kind in ("pair_tag", _IMPORT) and not dev.rotation and "panel" in values:
        # A new tag reads the way its panel sits in it (the 2.13-inch Hema: landscape, rotation 3).
        values["rotation"] = _default_rotation(values["panel"])
    epoch = _u32(report.get("epoch"))
    if binding.role == "tag" and epoch is not None and epoch > int(dev.epoch or 0):
        values["epoch"] = epoch
        await conn.execute(update(DELIVERIES).where(
            DELIVERIES.c.tag_device_id == dev.id, DELIVERIES.c.stage.in_(ACTIVE_STAGES)).values(epoch=epoch))
    values["info"] = info
    await conn.execute(update(DEVICES).where(DEVICES.c.id == dev.id).values(**values))


def _default_rotation(panel: int) -> int:
    """The quarter turns a new tag with ``panel`` gets (its panel profile's)."""
    try:
        from app.tags.runtime.enroll.hardware import PANEL_PROFILES
        from app.tags.runtime.protocol.ids import Panel

        profile = PANEL_PROFILES.get(Panel(panel))
    except (ImportError, ValueError):
        return 0
    return profile.rotation if profile is not None else 0


async def _mark_ready(conn, binding: Any, now: float, *, lift_clear: bool) -> list[str]:
    """Binding ready; a tag's held content flows (clear confirmed)."""
    await conn.execute(update(BINDINGS).where(BINDINGS.c.id == binding.id).values(
        state="ready", ready_at=now, paired_at=binding.paired_at or now, updated_at=now))
    lifted: list[str] = []
    if binding.tag_device_id:
        dev = (await conn.execute(select(DEVICES).where(DEVICES.c.id == binding.tag_device_id))).first()
        if dev is None:
            return lifted
        status = "paused" if binding.paused else "ok"
        values: dict[str, Any] = {"status": status, "updated_at": now}
        if binding.role == "tag" and lift_clear and dev.clear_required:
            values.update(clear_required=False)
            from app.tags.backfill import backfill_tag
            from app.tags.storage import lock_stream, merge_stream_state, reset_content_state

            if dev.owner_profile:
                await lock_stream(conn, dev.owner_profile, now)
            await conn.execute(update(DEVICES).where(DEVICES.c.id == dev.id).values(**values))
            fresh = (await conn.execute(select(DEVICES).where(DEVICES.c.id == dev.id))).first()
            if fresh.owner_profile:
                await merge_stream_state(conn, fresh.owner_profile, lambda st: reset_content_state(st, fresh.id))
                await backfill_tag(conn, fresh.owner_profile, device_json(fresh), now,
                                   since_ms=float(fresh.claimed_at or 0))
                lifted.append(fresh.owner_profile)
            return lifted
        await conn.execute(update(DEVICES).where(DEVICES.c.id == dev.id).values(**values))
    return lifted


async def progress(grant: dict[str, Any], op_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Apply a worker's progress report (stages, candidates, device facts, the
    outcome). Reports for a finished or cancelled operation change nothing
    but are answered, so a worker learns it should stop."""
    now = now_ms()
    lifted: list[str] = []
    wake: list[str] = []
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        comp = await worker_companion(conn, grant)
        op = (await conn.execute(select(OPERATIONS).where(OPERATIONS.c.id == op_id))).first()
        if op is None or not _worker_may_see(op, comp):
            raise TagError(404, "operation_not_found", "No such operation for this worker.")
        if op.state in FINAL_STATES:
            return {"operation": operation_json(op)}
        if op.kind == "discovery":
            await _discovery_progress(conn, op, comp, body, now)
            return {"operation": operation_json((await conn.execute(
                select(OPERATIONS).where(OPERATIONS.c.id == op_id))).first())}
        values: dict[str, Any] = {"updated_at": now}
        if isinstance(body.get("stage"), str):
            values["stage"] = body["stage"][:32]
            if op.state == "queued":
                values["state"] = "running"
        if isinstance(body.get("detail"), str):
            from app.tags.sanitize import clean_text

            values["stage_detail"] = clean_text(body["detail"], 255) or None
        device = body.get("device") if isinstance(body.get("device"), dict) else None
        if device is not None:
            await _device_report(conn, op, comp, device, now)
        state = body.get("state")
        if op.kind == "recover_gateway" and isinstance(body.get("devices"), list):
            lifted += await _recovery_devices(conn, op, comp, body["devices"], now)
        if state in ("succeeded", "failed", "pending_device"):
            error = body.get("error") if isinstance(body.get("error"), dict) else None
            if error is not None:
                values["error"] = {"code": str(error.get("code") or "failed")[:64],
                                   "message": str(error.get("message") or "")[:400]}
            values["state"] = state
            if state != "pending_device":
                values["finished_at"] = now
                values["secret_sealed"] = None
            result = body.get("result") if isinstance(body.get("result"), dict) else None
            if result is not None:
                from app.tags.storage import _small

                values["result"] = _small(result)
            lifted += await _finish(conn, op, comp, state, now, error=values.get("error"))
            wake.append(comp.id)
        await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == op_id).values(**values))
        op = (await conn.execute(select(OPERATIONS).where(OPERATIONS.c.id == op_id))).first()
    if lifted:
        from app.tags import journal

        journal.wake()
    return {"operation": operation_json(op)}


async def _finish(conn, op: Any, comp: Any, state: str, now: float,
                  error: dict[str, Any] | None = None) -> list[str]:
    """Operation outcome -> bindings, devices, the worker (``error``: what the worker reported)."""
    lifted: list[str] = []
    binding = (await conn.execute(select(BINDINGS).where(BINDINGS.c.id == op.binding_id))).first() \
        if op.binding_id else None
    if op.kind == "claim_gateway" and state == "failed":
        from app.tags.hosts import on_claim_outcome

        await on_claim_outcome(conn, op, state, now, error=error)
    if op.kind == "claim_gateway" and binding is not None:
        if state == "succeeded":
            await conn.execute(update(BINDINGS).where(BINDINGS.c.id == binding.id).values(
                state="paired", paired_at=now, updated_at=now))
            if binding.tag_device_id:
                await conn.execute(update(DEVICES).where(DEVICES.c.id == binding.tag_device_id).values(
                    status="ok", updated_at=now))
    elif op.kind in ("pair_bridge", "pair_tag", _IMPORT) and binding is not None:
        if state == "succeeded":
            lifted += await _mark_ready(conn, binding, now, lift_clear=True)
        elif state == "failed" and binding.state == "pairing":
            await _forget_binding(conn, binding, now, reason="pairing failed", cleanup="done")
    elif op.kind == "move_tag" and binding is not None and state == "succeeded":
        args = op.args or {}
        await conn.execute(update(DEVICES).where(DEVICES.c.id == binding.tag_device_id).values(
            bridge_device_id=args.get("bridge_id"), status="ok", updated_at=now))
    elif op.kind == "unpair" and binding is not None and state == "succeeded":
        await _forget_binding(conn, binding, now, reason="removed", cleanup="done")
    elif op.kind == "release_gateway" and state == "succeeded":
        for b in (await conn.execute(select(BINDINGS).where(BINDINGS.c.companion_id == comp.id))).all():
            await tombstone(conn, b, reason="removed", cleanup="done", now=now)
        await conn.execute(update(CREDENTIALS).where(
            CREDENTIALS.c.companion_id == comp.id, CREDENTIALS.c.revoked_at.is_(None)).values(revoked_at=now))
        await conn.execute(delete(COMPANIONS).where(COMPANIONS.c.id == comp.id))
    elif op.kind == "recover_gateway" and state == "succeeded":
        await conn.execute(update(COMPANIONS).where(COMPANIONS.c.id == comp.id).values(
            state="active", updated_at=now))
    return lifted


async def _recovery_devices(conn, op: Any, comp: Any, devices: list[Any], now: float) -> list[str]:
    """Per-device recovery reports: ``{device_id, state: rekeyed|pending|failed,
    gen?, cleared?}``. A device is ready again only once rekeyed (a tag also
    cleared); the gateway's recovery makes the worker active again."""
    lifted: list[str] = []
    result = dict(op.result or {})
    known = {d["device_id"]: d for d in (result.get("devices") or []) if isinstance(d, dict)}
    for item in devices[:200]:
        if not isinstance(item, dict):
            continue
        dev_id = _hex(item.get("device_id"), 16)
        state = item.get("state")
        if dev_id is None or state not in ("rekeyed", "pending", "failed"):
            continue
        binding = (await conn.execute(select(BINDINGS).where(
            BINDINGS.c.device_id == dev_id, BINDINGS.c.companion_id == comp.id))).first()
        if binding is None:
            continue
        gen = _u32(item.get("gen"))
        if gen is not None and gen in (int(binding.generation or 0), int(binding.pending_generation or -1)):
            await conn.execute(update(BINDINGS).where(BINDINGS.c.id == binding.id).values(
                generation=gen, pending_generation=None, updated_at=now))
            binding = (await conn.execute(select(BINDINGS).where(BINDINGS.c.id == binding.id))).first()
        final = state
        if state == "rekeyed" and binding.role == "tag" and not item.get("cleared"):
            final = "pending"
        if final == "rekeyed":
            lifted += await _mark_ready(conn, binding, now, lift_clear=True)
            if binding.role == "gateway":
                await conn.execute(update(COMPANIONS).where(
                    COMPANIONS.c.id == comp.id, COMPANIONS.c.state == "recovering").values(state="active", updated_at=now))
        known[dev_id] = {"device_id": dev_id, "state": "rekeyed" if final == "rekeyed" else
                         ("failed" if state == "failed" else "recovery_pending")}
    result["devices"] = list(known.values())
    await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == op.id).values(result=result))
    return lifted


# ── grants ──

_GRANTS_FOR = {
    "claim_gateway": {"claim", "recover"},
    "pair_bridge": {"pair"},
    "pair_tag": {"pair"},
    "unpair": {"release"},
    "release_gateway": {"release"},
    "recover_gateway": {"recover", "rekey"},
    "recommission_bridge": {"maint"},
}


async def issue_grant(grant: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    from app.tags.authority import AuthorityUnavailable, get_authority

    op_name = body.get("op")
    device = _hex(body.get("device_id"), 16)
    challenge = _hex(body.get("challenge"), 16)
    role = body.get("role")
    gen_from = _u32(body.get("gen_from"))
    if (op_name not in v2.GRANT_OPS or device is None or challenge is None or role not in v2.ROLES
            or gen_from is None or gen_from >= 0xFFFFFFFF):
        raise TagError(422, "invalid_grant_request",
                       "'op', 'device_id', 'role', 'gen_from' (u32) and 'challenge' are required.")
    try:
        authority = await get_authority()
    except AuthorityUnavailable as exc:
        raise TagError(503, exc.code, exc.message) from None
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        comp = await worker_companion(conn, grant)
        if comp.state == "removed" or not comp.controller_pub:
            raise TagError(403, "grant_refused", "This worker may not change device ownership.")
        op = (await conn.execute(select(OPERATIONS).where(OPERATIONS.c.id == body.get("operation_id")))).first()
        if op is None or op.companion_id != comp.id or op.state not in OPEN_STATES:
            raise TagError(403, "grant_refused", "No open operation of this worker needs that grant.")
        if op_name not in _GRANTS_FOR.get(op.kind, set()):
            raise TagError(403, "grant_refused", f"A {op.kind} operation does not use a {op_name} grant.")
        binding = (await conn.execute(select(BINDINGS).where(BINDINGS.c.device_id == device))).first()
        args = op.args or {}
        if op_name == "pair":
            ik = _hex(body.get("ik"), 32)
            if ik is None or v2.device_id(role, bytes.fromhex(ik)).hex() != device:
                raise TagError(422, "invalid_grant_request", "A pairing grant needs the device's identity key.")
            if role != args.get("role") or v2.short_id(bytes.fromhex(device)) != int(args.get("short_id") or -1):
                raise TagError(403, "grant_refused", "That device is not the one on the setup label.")
            if binding is not None and binding.id != op.binding_id:
                raise TagError(409, "device_owned", "That device is already set up.")
            tomb = (await conn.execute(select(REVOCATIONS).where(REVOCATIONS.c.device_id == device))).first()
            if tomb is not None and gen_from < int(tomb.highest_generation or 0):
                raise TagError(409, "stale_generation", "The device reports an ownership history older than Cremind's.")
            if binding is None:
                binding = await _create_pairing_binding(conn, op, comp, device, ik, role, gen_from, now)
        else:
            if binding is None or binding.companion_id != comp.id:
                raise TagError(403, "grant_refused", "That device does not belong to this worker.")
            if op.kind == "claim_gateway" and device != args.get("device_id"):
                raise TagError(403, "grant_refused", "That is not the gateway being connected.")
            if op.kind == "unpair" and device != args.get("device_id"):
                raise TagError(403, "grant_refused", "That is not the device being removed.")
            if op_name == "recover" and role != "gateway":
                raise TagError(422, "invalid_grant_request", "Only a gateway takes a recover grant.")
            if role != binding.role:
                raise TagError(403, "grant_refused", "The device's role does not match its binding.")
        allowed = {int(binding.generation or 0)}
        if binding.pending_generation is not None:
            allowed.add(int(binding.pending_generation))
        if gen_from not in allowed:
            raise TagError(409, "stale_generation", "The device's generation is not the one Cremind expects.",
                           expected=sorted(allowed))
        grant_bytes = v2.encode_grant(op=op_name, device=bytes.fromhex(device), role=role,
                                      authority_pub=authority.authority_pub,
                                      owner=v2.owner_bytes(op.owner_profile_id),
                                      controller=bytes.fromhex(comp.controller_pub), gen_from=gen_from,
                                      challenge=bytes.fromhex(challenge))
        await conn.execute(update(BINDINGS).where(BINDINGS.c.id == binding.id).values(
            pending_generation=gen_from + 1, updated_at=now))
        if op.binding_id is None:
            await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == op.id).values(
                binding_id=binding.id, updated_at=now))
    sig = authority.sign(v2.GRANT_LABEL + grant_bytes)
    return {"grant": grant_bytes.hex(), "sig": sig.hex(), "authority_pub": authority.authority_pub.hex()}


async def _create_pairing_binding(conn, op: Any, comp: Any, device: str, ik: str, role: str, gen: int,
                                  now: float) -> Any:
    args = op.args or {}
    dev_bytes = bytes.fromhex(device)
    hw = v2.hw_id(role, dev_bytes)
    existing = (await conn.execute(select(DEVICES).where(
        DEVICES.c.companion_id == comp.id, DEVICES.c.kind == role, DEVICES.c.hw_id == hw))).first()
    if existing is not None:
        raise TagError(409, "device_owned", "That device is already known to this worker.")
    device_row_id = str(uuid.uuid4())
    row: dict[str, Any] = {
        "id": device_row_id, "companion_id": comp.id, "kind": role, "hw_id": hw,
        "name": args.get("name") or ("New tag" if role == "tag" else "New bridge"),
        "owner_profile": op.owner_profile, "bridge_device_id": args.get("bridge_id") if role == "tag" else None,
        "epoch": 0, "rotation": 0, "info": {"device_id": device}, "status": "pairing",
        "desired_revision": 0, "displayed_revision": 0, "clear_required": role == "tag",
        "claimed_at": now if role == "tag" else None, "created_at": now, "updated_at": now,
    }
    await conn.execute(DEVICES.insert(), [row])
    binding_id = str(uuid.uuid4())
    await conn.execute(BINDINGS.insert(), [{
        "id": binding_id, "device_id": device, "role": role, "identity_pub": ik,
        "short_id": v2.short_id(dev_bytes), "companion_id": comp.id, "tag_device_id": device_row_id,
        "owner_profile": op.owner_profile, "owner_profile_id": op.owner_profile_id, "state": "pairing",
        "generation": gen, "pending_generation": None, "paused": False, "fw": None, "board": None, "info": {},
        "created_at": now, "updated_at": now, "paired_at": None, "ready_at": None,
    }])
    return (await conn.execute(select(BINDINGS).where(BINDINGS.c.id == binding_id))).first()


# ── vault ──


async def vault_put(grant: dict[str, Any], subject: str, body: dict[str, Any]) -> dict[str, Any]:
    from app.storage.models import TagVaultModel
    from app.tags.authority import AuthorityUnavailable, get_authority
    from app.tags.vault import seal, vault_context

    vault = TagVaultModel.__table__
    if subject != "worker" and _hex(subject, 16) is None:
        raise TagError(422, "invalid_subject", "The subject is a device id or 'worker'.")
    stage = body.get("stage")
    state = body.get("state")
    expected = body.get("expected_version")
    generation = _u32(body.get("generation", 0))
    if stage not in ("pending", "committed") or not isinstance(state, dict) or generation is None:
        raise TagError(422, "invalid_vault_entry", "'stage', 'generation' and an object 'state' are required.")
    if expected is not None and _u32(expected) is None:
        raise TagError(422, "invalid_vault_entry", "'expected_version' must be a whole number or null.")
    import json as _json

    if len(_json.dumps(state)) > 64 * 1024:
        raise TagError(413, "vault_entry_too_large", "A vault entry is at most 64 KiB.")
    try:
        authority = await get_authority()
    except AuthorityUnavailable as exc:
        raise TagError(503, exc.code, exc.message) from None
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        comp = await worker_companion(conn, grant)
        if comp.state in ("removing", "removed"):
            raise TagError(403, "not_current_worker", "This worker is being removed.")
        if subject != "worker":
            binding = (await conn.execute(select(BINDINGS).where(BINDINGS.c.device_id == subject))).first()
            if binding is None or binding.companion_id != comp.id:
                raise TagError(403, "not_current_worker", "That device does not belong to this worker.")
        latest = (await conn.execute(select(vault.c.version).where(
            vault.c.companion_id == comp.id, vault.c.subject == subject,
        ).order_by(vault.c.version.desc()).limit(1).with_for_update())).scalar_one_or_none()
        current = int(latest) if latest is not None else None
        if (expected if expected is not None else None) != current:
            raise TagError(409, "version_conflict", "Another version was saved meanwhile.", version=current)
        version = (current or 0) + 1
        sealed = seal(authority, state, vault_context(comp.owner_profile_id, subject, generation, version))
        await conn.execute(vault.insert(), [{
            "id": str(uuid.uuid4()), "companion_id": comp.id, "subject": subject,
            "owner_profile_id": comp.owner_profile_id, "version": version, "generation": generation,
            "stage": stage, "created_at": now, **sealed,
        }])
        old = (await conn.execute(select(vault.c.id).where(
            vault.c.companion_id == comp.id, vault.c.subject == subject,
        ).order_by(vault.c.version.desc()).offset(VAULT_KEEP))).scalars().all()
        if old:
            await conn.execute(delete(vault).where(vault.c.id.in_(list(old))))
    return {"version": version}


async def vault_get(grant: dict[str, Any]) -> dict[str, Any]:
    """The worker's recovery state — only while a recovery of this worker is
    open (its NEW credential: the old ones were revoked when it began)."""
    from app.storage.models import TagVaultModel
    from app.tags.authority import AuthorityUnavailable, get_authority
    from app.tags.vault import VaultError, open_record, vault_context

    vault = TagVaultModel.__table__
    async with get_tag_storage().engine.connect() as conn:
        comp = await worker_companion(conn, grant)
        open_recovery = (await conn.execute(select(OPERATIONS.c.id).where(
            OPERATIONS.c.companion_id == comp.id, OPERATIONS.c.kind == "recover_gateway",
            OPERATIONS.c.state.in_(OPEN_STATES)))).first()
        if open_recovery is None:
            raise TagError(403, "no_recovery", "Recovery data is delivered only during a recovery.")
        rows = (await conn.execute(select(vault).where(vault.c.companion_id == comp.id)
                                   .order_by(vault.c.subject, vault.c.version.desc()))).all()
    if not rows:
        return {"entries": []}
    try:
        authority = await get_authority()
    except AuthorityUnavailable as exc:
        raise TagError(503, "recovery_key_unavailable", exc.message) from None
    latest: dict[str, Any] = {}
    for r in rows:
        latest.setdefault(r.subject, []).append(r)
    entries = []
    for subject, versions in latest.items():
        for r in versions[:2]:  # the newest, and the one before it (a pending may not have committed)
            try:
                state = open_record(authority, dict(r._mapping),
                                    vault_context(r.owner_profile_id, r.subject, int(r.generation), int(r.version)))
            except VaultError:
                continue
            except AuthorityUnavailable as exc:
                raise TagError(503, exc.code, exc.message) from None
            entries.append({"device_id": subject, "version": int(r.version), "generation": int(r.generation),
                            "stage": r.stage, "state": state})
    return {"entries": entries}


# ── heartbeat hook and maintenance ──


async def on_worker_heartbeat(conn, companion_id: str, now: float,
                              devices: list[Any] | None = None) -> None:
    """A private worker's heartbeat: the gateway of a fresh claim becomes
    ready (and completes its setup session); a device the worker reports with
    its LIVE generation (``{hw_id, kind, gen}``, from contact with the device)
    leaves ``reconciling`` — a restore's hold — once that generation is not
    behind Cremind's record."""
    comp = (await conn.execute(select(COMPANIONS).where(COMPANIONS.c.id == companion_id))).first()
    if comp is None or comp.mode != PRIVATE:
        return
    gateway = (await conn.execute(select(BINDINGS).where(
        BINDINGS.c.companion_id == companion_id, BINDINGS.c.role == "gateway"))).first()
    if gateway is not None and gateway.state == "paired":
        await conn.execute(update(BINDINGS).where(BINDINGS.c.id == gateway.id).values(
            state="ready", ready_at=now, updated_at=now))
    if gateway is not None and comp.state == "connecting" and gateway.state in ("paired", "ready"):
        from app.tags.hosts import on_gateway_ready

        await on_gateway_ready(conn, companion_id, now)
    for item in (devices or [])[:500]:
        if not isinstance(item, dict) or _u32(item.get("gen")) is None:
            continue
        dev = (await conn.execute(select(DEVICES.c.id).where(
            DEVICES.c.companion_id == companion_id, DEVICES.c.kind == str(item.get("kind") or "tag"),
            DEVICES.c.hw_id == str(item.get("hw_id") or "")))).first()
        if dev is None:
            continue
        binding = (await conn.execute(select(BINDINGS).where(BINDINGS.c.tag_device_id == dev.id))).first()
        if binding is None or binding.state != "reconciling":
            continue
        gen = int(item["gen"])
        if gen >= int(binding.generation or 0):
            await conn.execute(update(BINDINGS).where(BINDINGS.c.id == binding.id).values(
                generation=gen, state="ready", updated_at=now))
    if comp.installation_id:
        await conn.execute(update(INSTALLATIONS).where(INSTALLATIONS.c.id == comp.installation_id).values(
            last_seen_at=now))
    from app.tags import setup

    await setup.on_worker_heartbeat(conn, companion_id, now)


async def expire_operations(now: float | None = None) -> int:
    now = now or now_ms()
    from app.tags import hosts

    expired = await hosts.expire(now)
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        rows = (await conn.execute(select(OPERATIONS).where(
            OPERATIONS.c.state.in_(OPEN_STATES + (WAITING,)), OPERATIONS.c.expires_at <= now,
            OPERATIONS.c.kind.notin_(hosts.HOST_OPS)))).all()
        for row in rows:
            if row.kind == "discovery":
                result = dict(row.result or {})
                found = bool(result.get("candidates"))
                await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == row.id).values(
                    state="succeeded" if found else "failed", stage="done", finished_at=now, updated_at=now,
                    secret_sealed=row.secret_sealed if found else None,
                    error=None if found else {"code": "not_found", "message": "Nothing answered with that setup code."},
                    expires_at=now + 10 * 60 * 1000.0 if found else row.expires_at))
                continue
            await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == row.id).values(
                state="failed", stage="timeout", secret_sealed=None, finished_at=now, updated_at=now,
                error={"code": "timeout", "message": "The operation did not finish in time."}))
            if row.kind in ("pair_bridge", "pair_tag") and row.binding_id:
                binding = (await conn.execute(select(BINDINGS).where(BINDINGS.c.id == row.binding_id))).first()
                if binding is not None and binding.state == "pairing":
                    await _forget_binding(conn, binding, now, reason="pairing timed out", cleanup="pending")
    return len(rows) + expired


__all__ = [
    "cancel_pairing", "cancel_waiting_recovery", "connections", "discovery_json", "expire_operations",
    "get_discovery", "get_pairing", "get_recovery", "issue_grant", "lease", "move_tag", "on_worker_heartbeat",
    "operation_json", "progress", "require_simple_setup", "send_test", "set_paused", "simple_setup_enabled",
    "start_discovery", "start_import", "start_pairing", "start_recovery", "unpair", "v1_device_id", "vault_get",
    "vault_put", "worker_operation", "worker_state",
]
