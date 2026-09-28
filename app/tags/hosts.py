"""Hardware hosts: the computers Cremind drives gateways on, who may use them, and their work.

A **host** is a computer whose USB ports Cremind itself drives gateways on:

- ``server`` — the backend's own (its hardware runtime, :mod:`app.tags.hosting`);
  its id is the runtime's ``host.json``. The admin may always use it; other
  profiles once the admin grants them (``tag_host_access``);
- ``desktop`` — a Cremind desktop installation enrolled by a profile (a remote
  or container backend cannot see this computer's USB). Only that profile uses
  it, through a credential scoped to the host and the profile.

A grant lets a profile **search for and claim unclaimed hardware** on a host.
It never shows another profile's devices: every connection, binding and device
stays its owner's, and revoking a grant only stops new claims.

Profile side (``/api/tags/hosts``, ``/api/tags/connections``,
``/api/tags/operations``): list hosts, search one (``host_scan``), connect a
gateway found there (``host_connect``), follow either, grant access, prepare
the server's components (``host_prepare``). Host side (in process for the
server's own host, ``/api/tag-host/v1`` for a desktop host): report status,
take work, report progress, and — for a connection — create the worker's
records once the host has its controller key and credentials ready.

Every search result is an opaque, expiring **candidate** bound to the profile
that searched, the host and the device identity it saw; a connection names a
candidate, never a port. The host checks the device again under the gateway's
exclusive lock before anything is claimed, and the connection appears (its
companion leaves ``connecting``) only once the worker has claimed the gateway
and reported in.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, insert, select, update

from app.storage.models import ProfileModel, TagHostAccessModel, TagHostCredentialModel, TagHostModel
from app.tags import credentials as creds
from app.tags import protocol_v2 as v2
from app.tags.cards import iso
from app.tags.ownership import (
    BINDINGS, OPERATIONS, PRIVATE, REVOCATIONS, profile_uuid, queue_operation,
)
from app.tags.service import TagError
from app.tags.storage import (
    COMPANIONS, CREDENTIALS, DEVICES, begin_write, get_tag_storage, notify_commands, now_ms,
)

HOSTS = TagHostModel.__table__
ACCESS = TagHostAccessModel.__table__
HOST_CREDENTIALS = TagHostCredentialModel.__table__

SERVER = "server"
DESKTOP = "desktop"
ADMIN = "admin"
HOST_OPS = ("host_scan", "host_connect", "host_prepare")
OPEN = ("queued", "running", "pending_device")
FINAL = ("succeeded", "failed", "cancelled")
ONLINE_WINDOW_MS = 90_000.0
SCAN_TTL_S = 120.0
CONNECT_TTL_S = 15 * 60.0
PREPARE_TTL_S = 60 * 60.0
CANDIDATE_TTL_MS = 10 * 60 * 1000.0
MAX_WAIT_S = 30
HOST_SCHEME = "CremindHost"
_HEX = set("0123456789abcdef")

# What a found device means for the profile that searched (the UI's words live there).
USABLE = "usable"
CANDIDATE_STATES = (USABLE, "already_connected", "recovery_required", "owned_elsewhere", "unsupported_firmware",
                    "not_a_gateway", "busy", "access_denied", "no_answer", "device_rejected")


def _hex(value: Any, size: int) -> str | None:
    text = str(value or "").lower()
    return text if len(text) == size * 2 and set(text) <= _HEX else None


# ── host events (a host's long-poll for work) ──

_work_events: dict[str, tuple[Any, asyncio.Event]] = {}


def work_event(host_id: str) -> asyncio.Event:
    loop = asyncio.get_running_loop()
    hit = _work_events.get(host_id)
    if hit is None or hit[0] is not loop:
        hit = (loop, asyncio.Event())
        _work_events[host_id] = hit
    return hit[1]


def notify_host(host_id: str | None) -> None:
    """Wake the host's work long-poll (and the in-process host) after a commit."""
    if not host_id:
        return
    hit = _work_events.get(host_id)
    if hit is not None:
        loop, event = hit
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            _work_events.pop(host_id, None)


# ── serialisation ──


def _fresh(row: Any, now: float) -> bool:
    """The host reported in recently (its agent says hello every ~20 s)."""
    return row.last_seen_at is not None and now - float(row.last_seen_at) <= ONLINE_WINDOW_MS


def _online(row: Any, now: float) -> bool:
    """A desktop host is online while it reports in; the server's own host is the server answering."""
    return row.kind == SERVER or _fresh(row, now)


def _runtime_state(row: Any, now: float) -> tuple[str, str | None]:
    status = row.status or {}
    state = status.get("state") or "unknown"
    if state == "running" and not _fresh(row, now):
        return "stalled", "Gateway support stopped reporting on this computer."
    return state, status.get("reason")


def host_json(row: Any, *, now: float, may_use: bool, why_not: str | None, can_manage: bool,
              grants: list[dict[str, Any]] | None, connections: int) -> dict[str, Any]:
    status = row.status or {}
    caps = row.capabilities or {}
    state, reason = _runtime_state(row, now)
    return {
        "id": row.id, "kind": row.kind, "name": row.name or ("This server" if row.kind == SERVER else "Computer"),
        "platform": row.platform or None, "version": row.version or None,
        "online": _online(row, now), "last_seen_at": iso(row.last_seen_at) if row.last_seen_at else None,
        "state": state, "reason": reason,
        "readiness": caps.get("readiness"), "usb": caps.get("usb"),
        "gateways": [g for g in status.get("gateways") or [] if isinstance(g, dict)],
        # Gateways this computer took over from the older Cremind Connect (moved / not yet).
        "migration": status.get("migration") or {},
        "access": {"can_use": may_use, "reason": why_not, "can_manage": can_manage,
                   **({"profiles": grants} if grants is not None else {})},
        "connections": connections,
    }


def _public_candidate(c: dict[str, Any]) -> dict[str, Any]:
    return {k: c.get(k) for k in ("id", "device_id", "short_id", "fw", "proto", "board", "state", "message",
                                  "companion_id", "expires_at")}


def operation_json(row: Any) -> dict[str, Any]:
    r = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    result = r.get("result") or {}
    out: dict[str, Any] = {
        "id": r["id"], "kind": r["kind"], "state": r["state"], "stage": r["stage"],
        "stage_detail": r.get("stage_detail"), "host_id": r.get("host_id"), "error": r.get("error"),
        "created_at": iso(r["created_at"]), "updated_at": iso(r["updated_at"]),
        "expires_at": iso(r["expires_at"]),
    }
    if r["kind"] == "host_scan":
        out["candidates"] = [_public_candidate(c) for c in result.get("candidates") or []]
        out["ports"] = result.get("ports") or []
    elif r["kind"] == "host_connect":
        out["candidate_id"] = (r.get("args") or {}).get("candidate_id")
        out["companion_id"] = result.get("companion_id")
        out["recover"] = (r.get("args") or {}).get("mode") == "recover"
        out["recovery_id"] = result.get("recovery_operation_id")
    elif r["kind"] == "host_prepare":
        out["log"] = list(result.get("log") or [])[-20:]
        out["readiness"] = result.get("readiness")
    return out


# ── access ──


async def _host(conn, host_id: Any) -> Any:
    if not isinstance(host_id, str) or not host_id:
        raise TagError(422, "invalid_host", "'host_id' is required.")
    row = (await conn.execute(select(HOSTS).where(HOSTS.c.id == host_id))).first()
    if row is None or row.state != "active":
        raise TagError(404, "host_not_found", "No gateway computer with that id.")
    return row


async def _active_grants(conn, host_id: str) -> list[Any]:
    return (await conn.execute(select(ACCESS).where(ACCESS.c.host_id == host_id, ACCESS.c.revoked_at.is_(None))
                               .order_by(ACCESS.c.created_at))).all()


def may_use(row: Any, profile: str, profile_id: str, grants: list[Any]) -> tuple[bool, str | None]:
    """Whether ``profile`` may search and claim unclaimed hardware on the host (and why not)."""
    if row.kind == DESKTOP:
        if row.owner_profile_id == profile_id and row.owner_profile == profile:
            return True, None
        return False, "This computer was set up by another profile."
    if profile == ADMIN:
        return True, None
    if any(g.profile_id == profile_id and g.profile == profile for g in grants):
        return True, None
    return False, "The admin has not allowed this profile to use this computer's USB ports."


async def _usable_host(conn, host_id: Any, profile: str, profile_id: str) -> Any:
    row = await _host(conn, host_id)
    ok, why = may_use(row, profile, profile_id, await _active_grants(conn, row.id))
    if not ok:
        if row.kind == DESKTOP:
            raise TagError(404, "host_not_found", "No gateway computer with that id.")
        raise TagError(403, "host_access_denied", why or "This profile may not use that computer.")
    return row


def _host_ready(row: Any, now: float) -> None:
    """A search or a connection needs the host online and its runtime running."""
    status = row.status or {}
    if not _online(row, now):
        raise TagError(409, "host_offline", f"{row.name or 'That computer'} is not reachable right now.")
    state, reason = _runtime_state(row, now)
    if state == "running":
        return
    if state == "stalled":
        raise TagError(409, "host_not_running", reason or "Gateway support stopped reporting on that computer.")
    if state == "unavailable":
        raise TagError(409, "components_unavailable",
                       status.get("reason") or "The gateway components are not ready on that computer.")
    if state == "busy_elsewhere":
        raise TagError(409, "host_busy", status.get("reason") or "Another Cremind on that computer runs the gateways.")
    raise TagError(409, "host_not_running", status.get("reason") or "Gateway support is not running there.")


# ── profile side ──


async def list_hosts(profile: str) -> dict[str, Any]:
    now = now_ms()
    async with get_tag_storage().engine.connect() as conn:
        profile_id = await profile_uuid(conn, profile)
        rows = (await conn.execute(select(HOSTS).where(HOSTS.c.state == "active")
                                   .order_by(HOSTS.c.kind.desc(), HOSTS.c.created_at))).all()
        out = []
        for row in rows:
            if row.kind == DESKTOP and row.owner_profile_id != profile_id:
                continue  # another profile's computer: not even its name
            grants = await _active_grants(conn, row.id)
            ok, why = may_use(row, profile, profile_id, grants)
            can_manage = row.kind == SERVER and profile == ADMIN
            count = int((await conn.execute(select(func.count()).select_from(COMPANIONS).where(
                COMPANIONS.c.host_id == row.id, COMPANIONS.c.owner_profile_id == profile_id,
                COMPANIONS.c.state.notin_(("removed", "connecting"))))).scalar_one() or 0)
            grant_view = await _grant_view(conn, grants) if can_manage else None
            out.append(host_json(row, now=now, may_use=ok, why_not=why, can_manage=can_manage, grants=grant_view,
                                 connections=count))
        ops = (await conn.execute(select(OPERATIONS).where(
            OPERATIONS.c.owner_profile == profile, OPERATIONS.c.kind.in_(HOST_OPS),
            OPERATIONS.c.state.in_(OPEN)).order_by(OPERATIONS.c.created_at.desc()).limit(20))).all()
    return {"hosts": out, "active": [operation_json(o) for o in ops]}


async def _grant_view(conn, grants: list[Any]) -> list[dict[str, Any]]:
    """Every other profile, and whether it may use the server's USB ports (what the admin manages)."""
    granted = {g.profile_id: g for g in grants}
    rows = (await conn.execute(select(ProfileModel.id, ProfileModel.name).order_by(ProfileModel.name))).all()
    return [{"profile": r.name, "profile_id": r.id, "granted": r.id in granted,
             "granted_at": iso(granted[r.id].created_at) if r.id in granted else None}
            for r in rows if r.name != ADMIN and not r.name.startswith("__")]


async def start_scan(profile: str, host_id: Any) -> dict[str, Any]:
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        profile_id = await profile_uuid(conn, profile)
        host = await _usable_host(conn, host_id, profile, profile_id)
        _host_ready(host, now)
        # One search at a time per profile and host: a second click follows the first.
        running = (await conn.execute(select(OPERATIONS).where(
            OPERATIONS.c.owner_profile_id == profile_id, OPERATIONS.c.host_id == host.id,
            OPERATIONS.c.kind == "host_scan", OPERATIONS.c.state.in_(OPEN)))).first()
        if running is not None:
            return operation_json(running)
        row = await queue_operation(conn, kind="host_scan", profile=profile, profile_id=profile_id, companion_id=None,
                                    args={}, now=now, ttl_s=SCAN_TTL_S, run=False)
        await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == row["id"]).values(host_id=host.id))
        row = (await conn.execute(select(OPERATIONS).where(OPERATIONS.c.id == row["id"]))).first()
    notify_host(host.id)
    return operation_json(row)


async def _find_candidate(conn, profile_id: str, host_id: str, candidate_id: str, now: float) -> tuple[Any, dict]:
    scans = (await conn.execute(select(OPERATIONS).where(
        OPERATIONS.c.owner_profile_id == profile_id, OPERATIONS.c.host_id == host_id,
        OPERATIONS.c.kind == "host_scan").order_by(OPERATIONS.c.created_at.desc()).limit(10))).all()
    for scan in scans:
        for cand in (scan.result or {}).get("candidates") or []:
            if cand.get("id") == candidate_id:
                if float(cand.get("expires_ms") or 0) <= now:
                    raise TagError(410, "candidate_expired", "That search result is too old; search again.")
                return scan, cand
    raise TagError(404, "candidate_not_found", "No such gateway in this computer's recent searches; search again.")


async def start_connect(profile: str, body: dict[str, Any]) -> dict[str, Any]:
    from app.tags.service import device_name

    candidate_id = body.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise TagError(422, "invalid_connection", "'host_id' and 'candidate_id' are required.")
    name = device_name(body["name"]) if body.get("name") is not None else None
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        profile_id = await profile_uuid(conn, profile)
        host = await _usable_host(conn, body.get("host_id"), profile, profile_id)
        _host_ready(host, now)
        _, cand = await _find_candidate(conn, profile_id, host.id, candidate_id, now)
        recover = body.get("recover") is True
        device = cand["device_id"]
        if recover:
            if cand.get("state") != "recovery_required":
                raise TagError(409, "nothing_to_recover", "That gateway is not waiting to be recovered here.")
            comp = (await conn.execute(select(COMPANIONS).where(COMPANIONS.c.id == cand.get("companion_id")))).first()
            if comp is None or comp.mode != PRIVATE or comp.owner_profile_id != profile_id \
                    or comp.owner_profile != profile or comp.gateway_device_id != device:
                raise TagError(404, "connection_not_found", "No connection of yours drives that gateway.")
            if comp.state in ("removing", "removed"):
                raise TagError(409, "removal_pending", "That connection is being removed.")
        elif cand.get("state") != USABLE:
            raise TagError(409, str(cand.get("state") or "not_usable"), str(cand.get("message") or
                                                                              "That gateway cannot be connected."))
        elif (await conn.execute(select(BINDINGS.c.id).where(BINDINGS.c.device_id == device))).first() is not None:
            raise TagError(409, "already_connected", "That gateway was connected meanwhile.")
        pending = (await conn.execute(select(OPERATIONS).where(
            OPERATIONS.c.kind == "host_connect", OPERATIONS.c.state.in_(OPEN)))).all()
        for other in pending:
            if (other.args or {}).get("device_id") == device:
                if other.owner_profile_id == profile_id and other.host_id == host.id:
                    return operation_json(other)
                raise TagError(409, "gateway_busy", "Someone is connecting that gateway right now.")
        args = {"candidate_id": candidate_id, "device_id": device, "ik": cand["ik"], "gen": int(cand.get("gen") or 0),
                "owner_state": cand.get("owner_state"), "mode": "recover" if recover else cand.get("mode") or "claim",
                "companion_id": cand.get("companion_id") if recover else None,
                "name": name or "", "board": cand.get("board"), "fw": cand.get("fw")}
        row = await queue_operation(conn, kind="host_connect", profile=profile, profile_id=profile_id,
                                    companion_id=None, args=args, now=now, ttl_s=CONNECT_TTL_S, run=False)
        await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == row["id"]).values(host_id=host.id))
        row = (await conn.execute(select(OPERATIONS).where(OPERATIONS.c.id == row["id"]))).first()
    notify_host(host.id)
    return operation_json(row)


async def get_operation(profile: str, op_id: str) -> dict[str, Any]:
    """A host operation of this profile (search, connection, preparation)."""
    async with get_tag_storage().engine.connect() as conn:
        profile_id = await profile_uuid(conn, profile)
        row = (await conn.execute(select(OPERATIONS).where(OPERATIONS.c.id == op_id))).first()
        if row is None or row.owner_profile_id != profile_id or row.owner_profile != profile \
                or row.kind not in HOST_OPS:
            raise TagError(404, "not_found", "No such operation.")
    return operation_json(row)


async def cancel_operation(profile: str, op_id: str) -> dict[str, Any]:
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        profile_id = await profile_uuid(conn, profile)
        row = (await conn.execute(select(OPERATIONS).where(OPERATIONS.c.id == op_id))).first()
        if row is None or row.owner_profile_id != profile_id or row.kind not in HOST_OPS:
            raise TagError(404, "not_found", "No such operation.")
        if row.state in OPEN and not (row.result or {}).get("companion_id"):
            await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == op_id).values(
                state="cancelled", stage="cancelled", error={"code": "cancelled", "message": "Cancelled."},
                finished_at=now, updated_at=now))
        elif row.state in OPEN:
            raise TagError(409, "already_claiming", "The gateway is being claimed; remove it afterwards to undo.")
        row = (await conn.execute(select(OPERATIONS).where(OPERATIONS.c.id == op_id))).first()
    return operation_json(row)


async def set_access(caller: str, host_id: str, profile_id: str, granted: Any) -> dict[str, Any]:
    """Grant (or revoke) a profile's use of the server's own USB ports (admin only)."""
    if caller != ADMIN:
        raise TagError(403, "admin_required", "Only the admin profile manages who may use this computer.")
    if not isinstance(granted, bool):
        raise TagError(422, "invalid_access", "'granted' must be true or false.")
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        host = await _host(conn, host_id)
        if host.kind != SERVER:
            raise TagError(409, "host_not_shareable",
                           "A computer set up from a profile is that profile's; set it up again from the other "
                           "profile instead.")
        target = (await conn.execute(select(ProfileModel.id, ProfileModel.name).where(
            ProfileModel.id == profile_id))).first()
        if target is None:
            raise TagError(404, "profile_not_found", "No profile with that id.")
        existing = (await conn.execute(select(ACCESS).where(ACCESS.c.host_id == host.id,
                                                           ACCESS.c.profile_id == profile_id))).first()
        if granted:
            if existing is None:
                await conn.execute(insert(ACCESS), [{
                    "id": str(uuid.uuid4()), "host_id": host.id, "profile": target.name, "profile_id": profile_id,
                    "granted_by": caller, "created_at": now, "revoked_at": None}])
            elif existing.revoked_at is not None or existing.profile != target.name:
                await conn.execute(update(ACCESS).where(ACCESS.c.id == existing.id).values(
                    revoked_at=None, profile=target.name, granted_by=caller, created_at=now))
        elif existing is not None and existing.revoked_at is None:
            await conn.execute(update(ACCESS).where(ACCESS.c.id == existing.id).values(revoked_at=now))
    return {"host_id": host.id, "profile_id": profile_id, "profile": target.name, "granted": granted}


async def start_prepare(caller: str, host_id: Any) -> dict[str, Any]:
    """Prepare the server's gateway components (packages, fonts) in the background (admin)."""
    if caller != ADMIN:
        raise TagError(403, "admin_required", "Only the admin profile installs components on the server.")
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        profile_id = await profile_uuid(conn, caller)
        host = await _host(conn, host_id)
        if host.kind != SERVER:
            raise TagError(409, "prepared_on_that_computer",
                           "Prepare a desktop computer's components from the Cremind app on it.")
        running = (await conn.execute(select(OPERATIONS).where(
            OPERATIONS.c.host_id == host.id, OPERATIONS.c.kind == "host_prepare",
            OPERATIONS.c.state.in_(OPEN)))).first()
        if running is not None:
            return operation_json(running)
        row = await queue_operation(conn, kind="host_prepare", profile=caller, profile_id=profile_id,
                                    companion_id=None, args={}, now=now, ttl_s=PREPARE_TTL_S, run=False,
                                    state="running", stage="starting")
        await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == row["id"]).values(host_id=host.id,
                                                                                         result={"log": []}))
        row = (await conn.execute(select(OPERATIONS).where(OPERATIONS.c.id == row["id"]))).first()
    from app.tags.hosting.prepare import run_in_background

    run_in_background(row.id)
    return operation_json(row)


# ── host side ──


@dataclass(frozen=True)
class HostPrincipal:
    """Who is asking on the host side: the server's own host (in process) or a desktop host's credential."""

    host_id: str
    kind: str
    profile: str | None = None
    profile_id: str | None = None
    credential_id: str | None = None


def new_host_credential_id() -> str:
    """A desktop host credential's id (``tagh_…``)."""
    import base64

    return "tagh_" + base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=").lower()


def new_host_credential() -> tuple[str, str]:
    """``(id, secret)`` of a desktop host credential (only the secret's SHA-256 is stored)."""
    return new_host_credential_id(), creds.new_secret()


async def enroll_redeem(conn, session: Any, credential_sha256: str, now: float) -> dict[str, Any]:
    """An ``enroll_host`` setup session is redeemed: the computer that answered it becomes (or stays) one of the
    profile's desktop gateway computers, and gets a credential scoped to that host and that profile — the one
    whose SHA-256 it sent (the secret never reaches Cremind). Enrolling the same computer again for the same
    profile keeps its host record and revokes its older credentials."""
    computer = session.computer or {}
    name = str(computer.get("name") or "")[:255] or "This computer"
    values = {"name": name, "platform": str(computer.get("platform") or "")[:16],
              "version": str(computer.get("version") or "")[:64], "public_key": session.installation_pub,
              "state": "active", "updated_at": now}
    host = (await conn.execute(select(HOSTS).where(
        HOSTS.c.kind == DESKTOP, HOSTS.c.installation_id == session.installation_id,
        HOSTS.c.owner_profile_id == session.owner_profile_id))).first()
    if host is None:
        host_id = str(uuid.uuid4())
        await conn.execute(insert(HOSTS), [{
            "id": host_id, "kind": DESKTOP, "owner_profile": session.owner_profile,
            "owner_profile_id": session.owner_profile_id, "installation_id": session.installation_id,
            "capabilities": None, "status": None, "created_at": now, "last_seen_at": None, **values}])
    else:
        host_id = host.id
        await conn.execute(update(HOSTS).where(HOSTS.c.id == host_id).values(**values))
        await conn.execute(update(HOST_CREDENTIALS).where(
            HOST_CREDENTIALS.c.host_id == host_id, HOST_CREDENTIALS.c.revoked_at.is_(None)).values(revoked_at=now))
    cred_id = new_host_credential_id()
    await conn.execute(insert(HOST_CREDENTIALS), [{
        "id": cred_id, "host_id": host_id, "profile": session.owner_profile, "profile_id": session.owner_profile_id,
        "secret_sha256": credential_sha256, "label": f"Cremind app on {name}"[:128], "created_at": now,
        "last_used_at": None, "revoked_at": None}])
    return {"host_id": host_id, "credential_id": cred_id, "host": {"id": host_id, "name": name}}


async def _retire_host(conn, host: Any, now: float, why: str) -> None:
    """Revoke a desktop host's credentials and take it off the list; its connections stay the profile's (they are
    offline until they move to another computer), its open searches and connections end."""
    await conn.execute(update(HOST_CREDENTIALS).where(
        HOST_CREDENTIALS.c.host_id == host.id, HOST_CREDENTIALS.c.revoked_at.is_(None)).values(revoked_at=now))
    await conn.execute(update(HOSTS).where(HOSTS.c.id == host.id).values(state="revoked", updated_at=now))
    for op in (await conn.execute(select(OPERATIONS).where(OPERATIONS.c.host_id == host.id,
                                                           OPERATIONS.c.state.in_(OPEN)))).all():
        await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == op.id).values(
            state="failed", stage="done", finished_at=now, updated_at=now,
            error={"code": "host_removed", "message": why}))
        if op.kind == "host_connect" and (op.result or {}).get("companion_id"):
            await _abandon_connection(conn, op.result["companion_id"], now)


async def remove_host(profile: str, host_id: Any) -> dict[str, Any]:
    """A profile removes one of its desktop gateway computers (the server's own cannot be removed)."""
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        profile_id = await profile_uuid(conn, profile)
        host = await _host(conn, host_id)
        if host.kind == SERVER:
            raise TagError(409, "host_not_removable", "The computer the Cremind server runs on cannot be removed; "
                                                      "the admin can stop other profiles from using it.")
        if host.owner_profile_id != profile_id or host.owner_profile != profile:
            raise TagError(404, "host_not_found", "No gateway computer with that id.")
        await _retire_host(conn, host, now, "The computer was removed.")
        count = int((await conn.execute(select(func.count()).select_from(COMPANIONS).where(
            COMPANIONS.c.host_id == host.id, COMPANIONS.c.state.notin_(("removed",))))).scalar_one() or 0)
    return {"host": {"id": host.id, "name": host.name, "state": "revoked"}, "connections": count}


LEGACY_EXTERNAL = "legacy_external"


async def adopt_legacy_worker(principal: HostPrincipal, companion_id: Any) -> dict[str, Any]:
    """A host takes over a worker the older Cremind Connect ran on the same computer (its directory moved in as
    it was, :mod:`app.tags.hosting.migration`): Cremind records the worker on this host. Only a connection of
    the profile the host serves (a desktop host) that still runs outside any host; a repeat answers the same."""
    if not isinstance(companion_id, str) or not companion_id:
        raise TagError(422, "invalid_worker", "Name the connection to take over.")
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        comp = (await conn.execute(select(COMPANIONS).where(COMPANIONS.c.id == companion_id))).first()
        if comp is None or comp.mode != PRIVATE or comp.state in ("removed", "removing") \
                or (principal.profile_id is not None and comp.owner_profile_id != principal.profile_id):
            raise TagError(404, "connection_not_found", "No connection with that id.")
        if comp.host_id == principal.host_id and comp.execution_kind == principal.kind:
            pass  # taken over already (a retried request)
        elif comp.execution_kind != LEGACY_EXTERNAL:
            raise TagError(409, "not_legacy", "That connection already runs on a gateway computer.")
        else:
            await conn.execute(update(COMPANIONS).where(COMPANIONS.c.id == comp.id).values(
                execution_kind=principal.kind, host_id=principal.host_id, updated_at=now))
    return {"companion_id": comp.id, "generation": int(comp.generation or 0), "host_id": principal.host_id,
            "profile": {"name": comp.owner_profile, "id": comp.owner_profile_id}}


async def host_leave(principal: HostPrincipal) -> dict[str, Any]:
    """A desktop host forgets its enrollment (``cremind tags host forget``): the same as its owner removing it."""
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        host = (await conn.execute(select(HOSTS).where(HOSTS.c.id == principal.host_id))).first()
        if host is None or host.kind != DESKTOP:
            raise TagError(409, "host_not_removable", "Only a desktop gateway computer can leave.")
        await _retire_host(conn, host, now, "The computer left.")
    return {"host": {"id": host.id, "state": "revoked"}}


def parse_host_authorization(header: str | None) -> tuple[str, str] | None:
    raw = (header or "").strip()
    scheme, _, rest = raw.partition(" ")
    if scheme.lower() != HOST_SCHEME.lower():
        return None
    cred_id, sep, secret = rest.strip().partition(".")
    if not sep or not cred_id.startswith("tagh_") or len(secret) < 32:
        return None
    return cred_id, secret


async def authenticate_host(credential_id: str, secret: str) -> HostPrincipal:
    async with get_tag_storage().engine.connect() as conn:
        row = (await conn.execute(select(HOST_CREDENTIALS).where(HOST_CREDENTIALS.c.id == credential_id))).first()
        expected = row.secret_sha256 if row is not None else hashlib.sha256(b"cremind-host-unknown").hexdigest()
        if not hmac.compare_digest(creds.hash_secret(secret), expected) or row is None:
            raise TagError(401, "invalid_credential", "The host credential is not valid.")
        if row.revoked_at is not None:
            raise TagError(401, "credential_revoked", "The host credential has been revoked.")
        host = (await conn.execute(select(HOSTS).where(HOSTS.c.id == row.host_id))).first()
        owner = (await conn.execute(select(ProfileModel.id).where(ProfileModel.name == row.profile))).scalar_one_or_none()
    if host is None or host.state != "active" or str(owner or "") != row.profile_id:
        raise TagError(401, "credential_revoked", "This computer is no longer set up for that profile.")
    return HostPrincipal(host.id, host.kind, row.profile, row.profile_id, row.id)


def _clean_status(body: dict[str, Any]) -> dict[str, Any]:
    """What a host may report about itself (bounded; never a port path or a key)."""
    status = body.get("status") if isinstance(body.get("status"), dict) else {}
    gateways = []
    for g in (status.get("gateways") or [])[:20]:
        if not isinstance(g, dict):
            continue
        device = _hex(g.get("device_id"), 16)
        gateways.append({"device_id": device, "short_id": f"{v2.short_id(bytes.fromhex(device)):08X}" if device else None,
                         "state": str(g.get("state") or "")[:24], "in_use": bool(g.get("in_use")),
                         "companion_id": str(g.get("companion_id") or "")[:36] or None})
    workers = []
    for w in (status.get("workers") or [])[:50]:
        if isinstance(w, dict):
            workers.append({"companion_id": str(w.get("companion_id") or "")[:36], "state": str(w.get("state") or "")[:24],
                            "gateway_connected": bool(w.get("gateway_connected")),
                            "gate_open": bool((w.get("gate") or {}).get("open")) if isinstance(w.get("gate"), dict)
                            else None, "last_error": str(w.get("last_error") or "")[:300] or None})
    moved = status.get("migration") if isinstance(status.get("migration"), dict) else {}
    migration = {key: int(moved.get(key) or 0) for key in ("moved", "failed", "rolled_back")
                 if isinstance(moved.get(key), int) and not isinstance(moved.get(key), bool)}
    return {"state": str(status.get("state") or "unknown")[:24], "reason": str(status.get("reason") or "")[:400] or None,
            "fonts_pack": _hex(status.get("fonts_pack"), 8), "paused": str(status.get("paused") or "")[:120] or None,
            "gateways": gateways, "workers": workers, "migration": migration}


async def host_hello(principal: HostPrincipal, body: dict[str, Any]) -> dict[str, Any]:
    """A host's status report (every ~20 s): who it is, its components and USB access, its gateways."""
    now = now_ms()
    caps = body.get("capabilities") if isinstance(body.get("capabilities"), dict) else {}
    values = {"name": str(body.get("name") or "")[:255], "platform": str(body.get("platform") or "")[:16],
              "version": str(body.get("version") or "")[:64],
              "capabilities": {"readiness": caps.get("readiness"), "usb": caps.get("usb"),
                               "arch": str(caps.get("arch") or "")[:16] or None},
              "status": _clean_status(body), "last_seen_at": now, "updated_at": now}
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        row = (await conn.execute(select(HOSTS).where(HOSTS.c.id == principal.host_id))).first()
        if row is None:
            if principal.kind != SERVER:
                raise TagError(401, "credential_revoked", "This computer is no longer set up.")
            await conn.execute(insert(HOSTS), [{"id": principal.host_id, "kind": SERVER, "owner_profile": None,
                                                "owner_profile_id": None, "installation_id": None, "public_key": None,
                                                "state": "active", "created_at": now, **values}])
        else:
            await conn.execute(update(HOSTS).where(HOSTS.c.id == row.id).values(**values))
        pending = (await conn.execute(select(OPERATIONS.c.id).where(
            OPERATIONS.c.host_id == principal.host_id, OPERATIONS.c.state == "queued",
            OPERATIONS.c.kind.in_(("host_scan", "host_connect"))))).all()
    return {"host_id": principal.host_id, "server_time": iso(now), "work_pending": len(pending),
            "hello_every_s": 20}


async def _scoped(conn, principal: HostPrincipal, op: Any) -> bool:
    """The operation is this host's and (a desktop credential) this profile's, and the profile may still use it."""
    if op is None or op.host_id != principal.host_id or op.kind not in HOST_OPS:
        return False
    if principal.profile_id is not None and op.owner_profile_id != principal.profile_id:
        return False
    return True


async def host_work(principal: HostPrincipal, wait: Any = 0) -> dict[str, Any]:
    """Queued searches and connections for this host (long-poll up to 30 s); each is taken (``running``)."""
    from app.tags.connector_service import as_int

    seconds = as_int(wait if wait is not None else 0)
    if seconds is None or seconds < 0:
        raise TagError(400, "invalid_wait", "'wait' must be a whole number of seconds.")
    deadline = time.monotonic() + min(seconds, MAX_WAIT_S)
    event = work_event(principal.host_id)
    while True:
        event.clear()
        taken = await _take_work(principal)
        if taken or time.monotonic() >= deadline:
            return {"work": taken}
        try:
            await asyncio.wait_for(event.wait(), timeout=min(deadline - time.monotonic(), 5.0))
        except asyncio.TimeoutError:
            pass


async def _take_work(principal: HostPrincipal) -> list[dict[str, Any]]:
    now = now_ms()
    out = []
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        rows = (await conn.execute(select(OPERATIONS).where(
            OPERATIONS.c.host_id == principal.host_id, OPERATIONS.c.state == "queued",
            OPERATIONS.c.kind.in_(("host_scan", "host_connect"))).order_by(OPERATIONS.c.created_at))).all()
        host = (await conn.execute(select(HOSTS).where(HOSTS.c.id == principal.host_id))).first()
        grants = await _active_grants(conn, principal.host_id)
        for op in rows:
            if not await _scoped(conn, principal, op):
                continue
            if float(op.expires_at) <= now:
                continue  # the expiry sweep ends it
            ok = host is not None and may_use(host, op.owner_profile, op.owner_profile_id, grants)[0]
            if not ok:
                await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == op.id).values(
                    state="failed", stage="done", finished_at=now, updated_at=now,
                    error={"code": "host_access_denied", "message": "This profile may no longer use that computer."}))
                continue
            await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == op.id, OPERATIONS.c.state == "queued")
                               .values(state="running", stage="started", updated_at=now))
            out.append({"id": op.id, "kind": op.kind, "args": dict(op.args or {}), "profile": op.owner_profile,
                        "profile_id": op.owner_profile_id})
    return out


async def _open_op(conn, principal: HostPrincipal, op_id: str) -> Any:
    op = (await conn.execute(select(OPERATIONS).where(OPERATIONS.c.id == op_id))).first()
    if not await _scoped(conn, principal, op):
        raise TagError(404, "operation_not_found", "No such operation for this computer.")
    return op


async def host_progress(principal: HostPrincipal, op_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Stages, a search's findings, the outcome. A finished or cancelled operation changes no more."""
    from app.tags.authority import AuthorityUnavailable, get_authority

    try:
        authority = await get_authority()
    except AuthorityUnavailable:
        authority = None
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        op = await _open_op(conn, principal, op_id)
        if op.state in FINAL:
            return {"operation": operation_json(op)}
        values: dict[str, Any] = {"updated_at": now}
        if isinstance(body.get("stage"), str):
            values["stage"] = body["stage"][:32]
        if isinstance(body.get("detail"), str):
            from app.tags.sanitize import clean_text

            values["stage_detail"] = clean_text(body["detail"], 255) or None
        result = dict(op.result or {})
        if op.kind == "host_scan" and isinstance(body.get("found"), list):
            result["candidates"] = await _candidates(conn, op, body["found"], authority, now)
            result["ports"] = [{"reason": str(p.get("reason") or "")[:24], "detail": str(p.get("detail") or "")[:200]}
                               for p in body.get("unidentified") or [] if isinstance(p, dict)][:20]
            values["result"] = result
        state = body.get("state")
        if state in ("succeeded", "failed"):
            values["state"] = state
            values["finished_at"] = now
            values["stage"] = "done"
            error = body.get("error") if isinstance(body.get("error"), dict) else None
            if error is not None:
                values["error"] = {"code": str(error.get("code") or "failed")[:64],
                                   "message": str(error.get("message") or "")[:400]}
            if op.kind == "host_connect" and state == "failed" and result.get("companion_id"):
                await _abandon_connection(conn, result["companion_id"], now)
        await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == op.id).values(**values))
        op = (await conn.execute(select(OPERATIONS).where(OPERATIONS.c.id == op.id))).first()
    return {"operation": operation_json(op)}


async def _candidates(conn, op: Any, found: list[Any], authority: Any, now: float) -> list[dict[str, Any]]:
    """What each device the host found means for the profile that searched."""
    ours = authority.authority_id.hex() if authority is not None else None
    out = []
    for item in found[:20]:
        if not isinstance(item, dict):
            continue
        device = _hex(item.get("device_id"), 16)
        ik = _hex(item.get("ik"), 32)
        if device is None or ik is None:
            continue
        role = str(item.get("role") or "")
        proto = item.get("proto") if isinstance(item.get("proto"), int) else 0
        gen = item.get("gen") if isinstance(item.get("gen"), int) and not isinstance(item.get("gen"), bool) else 0
        owner_state = str(item.get("owner_state") or "")
        authority_id = _hex(item.get("authority_id"), 16)
        state, message, mode, companion_id = USABLE, "Ready to connect.", "claim", None
        binding = (await conn.execute(select(BINDINGS).where(BINDINGS.c.device_id == device))).first()
        tomb = (await conn.execute(select(REVOCATIONS).where(REVOCATIONS.c.device_id == device))).first()
        if role != "gateway":
            state, message = "not_a_gateway", "This is not a gateway (a bridge plugged in for maintenance?)."
        elif proto < v2.SECURE_PROTO_VERSION:
            state, message = "unsupported_firmware", "This gateway needs a firmware update before it can be set up."
        elif v2.device_id("gateway", bytes.fromhex(ik)).hex() != device:
            state, message = "device_rejected", "The gateway's identity does not check out."
        elif binding is not None:
            if binding.owner_profile_id == op.owner_profile_id:
                companion_id = binding.companion_id
                comp = (await conn.execute(select(COMPANIONS).where(COMPANIONS.c.id == binding.companion_id))).first()
                if comp is not None and comp.host_id == op.host_id:
                    state, message = "already_connected", "This gateway is already connected here."
                else:
                    state, message = "recovery_required", ("This gateway is connected through another computer. "
                                                           "Recover it on this computer to move it here.")
            else:
                state, message = "owned_elsewhere", "This gateway is already set up for someone else."
        elif tomb is not None and gen < int(tomb.highest_generation or 0):
            state, message = "device_rejected", ("This gateway reports an ownership history older than Cremind's "
                                                 "record. Unplug it and try again; if this repeats, reset it.")
        elif owner_state == "owned":
            if ours is None or authority_id != ours:
                state, message = "owned_elsewhere", ("This gateway belongs to another Cremind server. Reset it (hold "
                                                     "its button while plugging it in) to use it here.")
            elif tomb is not None and tomb.last_owner_profile_id and tomb.last_owner_profile_id != op.owner_profile_id:
                state, message = "owned_elsewhere", "This gateway is already set up for someone else."
            else:
                mode, message = "readopt", "Connected to this Cremind before: ready to connect again."
        if item.get("in_use") and state == USABLE:
            state, message = "busy", "This gateway is in use by a worker on that computer."
        out.append({"id": "hc_" + secrets.token_urlsafe(12), "device_id": device, "ik": ik,
                    "short_id": f"{v2.short_id(bytes.fromhex(device)):08X}", "fw": str(item.get("fw") or "")[:32],
                    "proto": proto, "board": item.get("board") if isinstance(item.get("board"), int) else None,
                    "gen": gen, "owner_state": owner_state, "state": state, "message": message, "mode": mode,
                    "companion_id": companion_id, "expires_ms": now + CANDIDATE_TTL_MS,
                    "expires_at": iso(now + CANDIDATE_TTL_MS)})
    return out


async def host_register_worker(principal: HostPrincipal, op_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """A connection's host has its controller key and two credential secrets ready and re-checked the gateway
    under its lock: create the private companion (``connecting``), its credentials (by hash), the gateway's
    binding and the ``claim_gateway`` operation the worker runs first. A repeat returns the same records."""
    from app.tags.authority import AuthorityUnavailable, get_authority

    controller = _hex(body.get("controller_pub"), 32)
    hashes = body.get("credentials") if isinstance(body.get("credentials"), dict) else {}
    hw_hash, ct_hash = _hex(hashes.get("hardware_sha256"), 32), _hex(hashes.get("content_sha256"), 32)
    if controller is None or hw_hash is None or ct_hash is None or hw_hash == ct_hash:
        raise TagError(422, "invalid_worker", "'controller_pub' and two different credential hashes are required.")
    gateway = body.get("gateway") if isinstance(body.get("gateway"), dict) else {}
    try:
        authority = await get_authority()
    except AuthorityUnavailable as exc:
        raise TagError(503, exc.code, exc.message) from None
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        op = await _open_op(conn, principal, op_id)
        if op.kind != "host_connect":
            raise TagError(409, "wrong_operation", "Only a connection registers a worker.")
        result = dict(op.result or {})
        if result.get("companion_id"):
            if result.get("controller_pub") != controller:
                raise TagError(409, "already_registered", "This connection registered another worker already.")
            return _registered(result, authority)
        if op.state not in ("running",):
            raise TagError(409, "operation_closed", f"The connection is {op.state}.")
        args = dict(op.args or {})
        device = args.get("device_id")
        if _hex(gateway.get("device_id"), 16) != device or _hex(gateway.get("ik"), 32) != args.get("ik"):
            raise TagError(409, "gateway_changed", "Another gateway answered on that port; search again.")
        if args.get("mode") == "recover":
            result.update(await _register_recovery(conn, principal, op, controller, hw_hash, ct_hash, authority, now))
            await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == op.id).values(
                result=result, state="succeeded", stage="done", stage_detail=None, finished_at=now, updated_at=now))
            notify_commands([result["companion_id"]])
            return _registered(result, authority)
        if (await conn.execute(select(BINDINGS.c.id).where(BINDINGS.c.device_id == device))).first() is not None:
            raise TagError(409, "already_connected", "That gateway was connected meanwhile.")
        profile, profile_id = op.owner_profile, op.owner_profile_id
        if (await conn.execute(select(ProfileModel.id).where(ProfileModel.name == profile))).scalar_one_or_none() \
                != profile_id:
            raise TagError(404, "profile_not_found", "The profile no longer exists.")
        host = (await conn.execute(select(HOSTS).where(HOSTS.c.id == principal.host_id))).first()
        host_name = (host.name if host is not None else "") or "This computer"
        name = (args.get("name") or f"{host_name} gateway")[:128]
        gen = gateway.get("gen") if isinstance(gateway.get("gen"), int) and not isinstance(gateway.get("gen"), bool) \
            else int(args.get("gen") or 0)
        cid = str(uuid.uuid4())
        await conn.execute(insert(COMPANIONS), [{
            "id": cid, "name": name, "created_by": profile, "created_at": now, "updated_at": now,
            "last_seen_at": None, "version": None, "host": host_name[:255], "heartbeat": None,
            "mode": PRIVATE, "owner_profile": profile, "owner_profile_id": profile_id, "installation_id": None,
            "controller_pub": controller, "generation": 0, "state": "connecting", "paused": False,
            "lease_expires_at": None, "lease_credential_id": None, "gateway_device_id": device,
            "execution_kind": principal.kind, "host_id": principal.host_id,
        }])
        label = f"Cremind on {host_name}"[:128]
        hardware = {"id": creds.new_credential_id(), "companion_id": cid, "kind": creds.KIND_HARDWARE,
                    "profile": None, "secret_sha256": hw_hash, "label": label, "created_by": "cremind-host",
                    "created_at": now, "last_used_at": None, "revoked_at": None}
        content = {**hardware, "id": creds.new_credential_id(), "kind": creds.KIND_CONTENT, "profile": profile,
                   "secret_sha256": ct_hash}
        await conn.execute(insert(CREDENTIALS), [hardware, content])
        device_row_id = str(uuid.uuid4())
        board = gateway.get("board") if isinstance(gateway.get("board"), int) else args.get("board")
        fw = str(gateway.get("fw") or args.get("fw") or "")[:32] or None
        await conn.execute(insert(DEVICES), [{
            "id": device_row_id, "companion_id": cid, "kind": "gateway",
            "hw_id": v2.hw_id("gateway", bytes.fromhex(device)), "name": name, "owner_profile": profile,
            "bridge_device_id": None, "epoch": 0, "rotation": 0, "board": board, "fw": fw,
            "info": {"device_id": device, "proto": gateway.get("proto")}, "status": "pairing",
            "desired_revision": 0, "displayed_revision": 0, "clear_required": False,
            "created_at": now, "updated_at": now,
        }])
        binding_id = str(uuid.uuid4())
        await conn.execute(insert(BINDINGS), [{
            "id": binding_id, "device_id": device, "role": "gateway", "identity_pub": args["ik"],
            "short_id": v2.short_id(bytes.fromhex(device)), "companion_id": cid, "tag_device_id": device_row_id,
            "owner_profile": profile, "owner_profile_id": profile_id, "state": "pairing", "generation": gen,
            "pending_generation": None, "paused": False, "fw": fw, "board": board,
            "info": {"proto": gateway.get("proto")}, "created_at": now, "updated_at": now,
            "paired_at": None, "ready_at": None,
        }])
        claim = await queue_operation(conn, kind="claim_gateway", profile=profile, profile_id=profile_id,
                                      companion_id=cid, binding_id=binding_id, now=now, parent_id=op.id,
                                      args={"device_id": device, "ik": args["ik"], "gen": gen,
                                            "mode": args.get("mode") or "claim"})
        result.update({"companion_id": cid, "controller_pub": controller, "claim_operation_id": claim["id"],
                       "credentials": {"hardware_id": hardware["id"], "content_id": content["id"]},
                       "profile": {"name": profile, "id": profile_id}})
        await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == op.id).values(
            result=result, stage="claiming", stage_detail="Connecting to the gateway…", updated_at=now))
    notify_commands([cid])
    return _registered(result, authority)


async def _register_recovery(conn, principal: HostPrincipal, op: Any, controller: str, hw_hash: str, ct_hash: str,
                             authority: Any, now: float) -> dict[str, Any]:
    """Move an existing connection to this host (the gateway was plugged in here): the recovery of
    ``ownership.redeem_recover`` — old credentials and lease revoked, the worker generation moved, every
    device held until rekeyed — with the worker now on this host."""
    from types import SimpleNamespace

    from app.tags.ownership import redeem_recover

    args = dict(op.args or {})
    host = (await conn.execute(select(HOSTS).where(HOSTS.c.id == principal.host_id))).first()
    host_name = (host.name if host is not None else "") or "This computer"
    session = SimpleNamespace(companion_id=args.get("companion_id"), owner_profile=op.owner_profile,
                              owner_profile_id=op.owner_profile_id, computer={"name": host_name},
                              installation_id=None, operation_id=None)
    redeemed = await redeem_recover(conn, session, controller, hw_hash, ct_hash, authority, now,
                                    label=f"Cremind on {host_name}", created_by="cremind-host")
    await conn.execute(update(COMPANIONS).where(COMPANIONS.c.id == redeemed["companion_id"]).values(
        host_id=principal.host_id, execution_kind=principal.kind, updated_at=now))
    generation = (await conn.execute(select(COMPANIONS.c.generation).where(
        COMPANIONS.c.id == redeemed["companion_id"]))).scalar_one()
    return {"companion_id": redeemed["companion_id"], "controller_pub": controller,
            "claim_operation_id": redeemed["operation_id"], "recovery_operation_id": redeemed["operation_id"],
            "credentials": redeemed["credentials"], "generation": int(generation or 0),
            "profile": {"name": op.owner_profile, "id": op.owner_profile_id}}


def _registered(result: dict[str, Any], authority: Any) -> dict[str, Any]:
    return {"companion_id": result["companion_id"], "credentials": result["credentials"],
            "operation_id": result["claim_operation_id"], "profile": result["profile"],
            "generation": int(result.get("generation") or 0),
            "recovery_operation_id": result.get("recovery_operation_id"),
            "server": {"installation_id": authority.installation_id, "authority_pub": authority.authority_pub.hex(),
                       "authority_id": authority.authority_id.hex()}}


async def _abandon_connection(conn, companion_id: str, now: float) -> None:
    """A connection that did not finish: its companion (still ``connecting``) goes with its records; the
    worker's credentials stop working, so it retires itself."""
    comp = (await conn.execute(select(COMPANIONS).where(COMPANIONS.c.id == companion_id))).first()
    if comp is None or comp.state != "connecting":
        return
    await conn.execute(update(CREDENTIALS).where(CREDENTIALS.c.companion_id == companion_id,
                                                 CREDENTIALS.c.revoked_at.is_(None)).values(revoked_at=now))
    await conn.execute(delete(BINDINGS).where(BINDINGS.c.companion_id == companion_id))
    await conn.execute(delete(COMPANIONS).where(COMPANIONS.c.id == companion_id))


async def on_claim_outcome(conn, claim: Any, state: str, now: float, *,
                           error: dict[str, Any] | None = None) -> None:
    """The ``claim_gateway`` of a connection ended: a failure fails the connection (and abandons it) with
    the worker's reason (``error``)."""
    if state != "failed" or not claim.parent_id:
        return
    parent = (await conn.execute(select(OPERATIONS).where(OPERATIONS.c.id == claim.parent_id))).first()
    if parent is None or parent.kind != "host_connect" or parent.state in FINAL:
        return
    error = error or claim.error or {"code": "claim_failed", "message": "The gateway could not be claimed."}
    await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == parent.id).values(
        state="failed", stage="done", finished_at=now, updated_at=now, error=error))
    if claim.companion_id:
        await _abandon_connection(conn, claim.companion_id, now)


async def on_gateway_ready(conn, companion_id: str, now: float) -> None:
    """The first heartbeat after the claim: the connection is made, its companion appears."""
    comp = (await conn.execute(select(COMPANIONS).where(COMPANIONS.c.id == companion_id))).first()
    if comp is None or comp.state != "connecting":
        return
    await conn.execute(update(COMPANIONS).where(COMPANIONS.c.id == companion_id).values(state="active",
                                                                                     updated_at=now))
    ops = (await conn.execute(select(OPERATIONS).where(OPERATIONS.c.kind == "host_connect",
                                                       OPERATIONS.c.state.in_(OPEN)))).all()
    for op in ops:
        if (op.result or {}).get("companion_id") == companion_id:
            await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == op.id).values(
                state="succeeded", stage="done", stage_detail=None, finished_at=now, updated_at=now))


async def expire(now: float | None = None) -> int:
    """End host operations past their deadline (a connection's companion goes with it)."""
    now = now or now_ms()
    async with get_tag_storage().engine.begin() as conn:
        await begin_write(conn)
        rows = (await conn.execute(select(OPERATIONS).where(OPERATIONS.c.kind.in_(HOST_OPS),
                                                            OPERATIONS.c.state.in_(OPEN),
                                                            OPERATIONS.c.expires_at <= now))).all()
        for row in rows:
            if row.kind == "host_scan" and (row.result or {}).get("candidates") is not None:
                await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == row.id).values(
                    state="succeeded", stage="done", finished_at=now, updated_at=now))
                continue
            await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == row.id).values(
                state="failed", stage="timeout", finished_at=now, updated_at=now,
                error={"code": "host_offline" if row.state == "queued" else "timeout",
                       "message": "The computer did not pick this up in time." if row.state == "queued"
                       else "This did not finish in time."}))
            if row.kind == "host_connect" and (row.result or {}).get("companion_id"):
                await _abandon_connection(conn, row.result["companion_id"], now)
    return len(rows)


__all__ = [
    "ADMIN", "CANDIDATE_STATES", "DESKTOP", "HOST_OPS", "HOST_SCHEME", "HostPrincipal", "SERVER", "USABLE",
    "adopt_legacy_worker", "authenticate_host", "cancel_operation", "enroll_redeem", "expire", "get_operation", "host_hello",
    "host_leave", "host_progress", "host_register_worker", "host_work", "list_hosts", "may_use",
    "new_host_credential", "new_host_credential_id", "notify_host", "on_claim_outcome", "on_gateway_ready",
    "operation_json", "parse_host_authorization", "remove_host", "set_access", "start_connect", "start_prepare",
    "start_scan",
]
