"""Private workers and device bindings (cremind-tag ``docs/connect-setup.md``
§1, §8, §9, §12).

A **private** companion is one Connect worker of ONE profile. Its owner is
the profile's name AND its immutable UUID (``profiles.id``): every check here
requires both, so a profile deleted and recreated under the same name owns
nothing of the old one. Its gateway, bridges and tags have **bindings**
(``tag_bindings``) keyed by the canonical ``device_id``, unique across every
worker, so one physical device can never be authorised twice.

This module creates a worker when a setup session is redeemed, hands a worker
over to a replacement computer (recovery), answers "is this gateway free",
and revokes a profile's workers when the profile is deleted.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import delete, insert, select, update

from app.storage.models import (
    ProfileModel, TagBindingModel, TagConnectInstallationModel, TagOperationModel, TagRevocationModel,
)
from app.tags import credentials as creds
from app.tags import protocol_v2 as v2
from app.tags.service import TagError
from app.tags.storage import (
    ACTIVE_STAGES, COMMANDS, COMPANIONS, CREDENTIALS, DELIVERIES, DEVICES, insert_command,
)

BINDINGS = TagBindingModel.__table__
INSTALLATIONS = TagConnectInstallationModel.__table__
REVOCATIONS = TagRevocationModel.__table__
OPERATIONS = TagOperationModel.__table__

PRIVATE = "private"
LEGACY = "legacy_shared"
ONLINE_WINDOW_MS = 120_000
RUN_OPERATION = "run_operation"
_OPERATION_TTL_S = 7 * 24 * 3600.0


def _now() -> float:
    return time.time() * 1000


async def profile_uuid(conn, profile: str) -> str:
    row = (await conn.execute(select(ProfileModel.id).where(ProfileModel.name == profile))).first()
    if row is None:
        raise TagError(404, "profile_not_found", "The profile no longer exists.")
    return str(row[0])


def owns(row: Any, profile: str, profile_id: str) -> bool:
    """A private companion (or binding) row belongs to exactly this profile."""
    return (getattr(row, "owner_profile", None) == profile
            and getattr(row, "owner_profile_id", None) == profile_id)


async def owned_companion_row(conn, profile: str, profile_id: str, companion_id: Any) -> Any:
    """The profile's private companion, else 404 (never another profile's)."""
    if not isinstance(companion_id, str) or not companion_id:
        raise TagError(422, "invalid_companion", "'companion_id' is required.")
    row = (await conn.execute(select(COMPANIONS).where(COMPANIONS.c.id == companion_id))).first()
    if row is None or row.mode != PRIVATE or not owns(row, profile, profile_id) or row.state == "removed":
        raise TagError(404, "connection_not_found", "No connection with that id.")
    return row


async def touch_installation(conn, inst: dict[str, str], now: float) -> None:
    row = (await conn.execute(select(INSTALLATIONS.c.id).where(INSTALLATIONS.c.id == inst["id"]))).first()
    values = {"public_key": inst["public_key"], "computer": inst["computer"], "platform": inst["platform"],
              "version": inst["version"], "last_seen_at": now}
    if row is None:
        await conn.execute(insert(INSTALLATIONS), [{"id": inst["id"], "first_seen_at": now, **values}])
    else:
        await conn.execute(update(INSTALLATIONS).where(INSTALLATIONS.c.id == inst["id"]).values(**values))


async def companion_summary(conn, companion_id: str) -> dict[str, Any] | None:
    row = (await conn.execute(select(COMPANIONS).where(COMPANIONS.c.id == companion_id))).first()
    if row is None:
        return None
    return {"companion_id": row.id, "gateway_device_id": row.gateway_device_id, "gateway_name": row.name}


async def check_gateway_available(conn, session: Any, gateway: dict[str, Any], authority: Any) -> str:
    """How the worker will take this gateway: ``claim`` (unowned), ``readopt``
    (already owned by THIS server's authority but not bound — e.g. a restore
    lost the binding; the worker sends RECOVER, which the gateway refuses
    unless its pinned owner is this profile) or ``recover`` (a recovery).
    Raises 409 ``device_owned`` / ``wrong_gateway`` otherwise."""
    device = gateway["device_id"]
    ours = gateway.get("authority_id") == authority.authority_id.hex()
    owned = gateway.get("owner_state") == 1
    binding = (await conn.execute(select(BINDINGS).where(BINDINGS.c.device_id == device))).first()
    tomb = (await conn.execute(select(REVOCATIONS).where(REVOCATIONS.c.device_id == device))).first()
    if tomb is not None and int(gateway.get("gen") or 0) < int(tomb.highest_generation or 0):
        raise TagError(409, "device_rejected",
                       "This gateway reports an ownership history older than Cremind's record. "
                       "Unplug it and try again; if this repeats, reset it.")
    if session.operation == "recover":
        target = (await conn.execute(select(COMPANIONS).where(COMPANIONS.c.id == session.companion_id))).first()
        if target is None or target.gateway_device_id != device:
            raise TagError(409, "wrong_gateway", "This is not the gateway of the connection being recovered.")
        if not (owned and ours):
            raise TagError(409, "device_owned",
                           "This gateway is no longer set up for this Cremind server. Reset it and connect it "
                           "as a new gateway.")
        return "recover"
    if binding is not None:
        if binding.owner_profile_id == session.owner_profile_id:
            raise TagError(409, "device_owned",
                           "This gateway is already connected to your Cremind. Use Recover on this computer "
                           "for its connection instead.", use="recover", companion_id=binding.companion_id)
        raise TagError(409, "device_owned", "This gateway is already set up for someone else.")
    if owned and not ours:
        raise TagError(409, "device_owned",
                       "This gateway belongs to another Cremind server. Reset it (hold its button while "
                       "plugging it in) to use it here.")
    if owned and ours:
        if tomb is not None and tomb.last_owner_profile_id and tomb.last_owner_profile_id != session.owner_profile_id:
            raise TagError(409, "device_owned", "This gateway is already set up for someone else.")
        return "readopt"
    return "claim"


def _credential_row(kind: str, secret_sha256: str, companion_id: str, *, profile: str | None,
                    label: str, now: float) -> dict[str, Any]:
    return {
        "id": creds.new_credential_id(), "companion_id": companion_id, "kind": kind, "profile": profile,
        "secret_sha256": secret_sha256, "label": label, "created_by": "cremind-connect",
        "created_at": now, "last_used_at": None, "revoked_at": None,
    }


async def queue_operation(conn, *, kind: str, profile: str, profile_id: str, companion_id: str | None,
                          args: dict[str, Any], now: float, binding_id: str | None = None,
                          parent_id: str | None = None, secret_sealed: dict[str, Any] | None = None,
                          state: str = "queued", stage: str = "queued", ttl_s: float = _OPERATION_TTL_S,
                          op_id: str | None = None, run: bool = True) -> dict[str, Any]:
    """Insert an operation and (``run``) its ``run_operation`` command."""
    op_id = op_id or str(uuid.uuid4())
    command_ids = []
    if run and companion_id:
        command = await insert_command(conn, companion_id=companion_id, kind=RUN_OPERATION,
                                       args={"operation_id": op_id, "kind": kind}, requested_by=profile,
                                       ttl_s=ttl_s, now=now)
        command_ids.append(command["id"])
    row = {
        "id": op_id, "kind": kind, "state": state, "stage": stage, "stage_detail": None,
        "owner_profile": profile, "owner_profile_id": profile_id, "companion_id": companion_id,
        "binding_id": binding_id, "parent_id": parent_id, "args": args, "result": None,
        "secret_sealed": secret_sealed, "command_ids": command_ids, "error": None, "created_at": now,
        "updated_at": now, "expires_at": now + ttl_s * 1000.0, "finished_at": None,
    }
    await conn.execute(insert(OPERATIONS), [row])
    return row


async def redeem_connect(conn, session: Any, controller: str, hw_hash: str, ct_hash: str, authority: Any,
                         now: float) -> dict[str, Any]:
    """A new private worker for this profile and its gateway (binding
    ``pairing``) plus the ``claim_gateway`` operation it runs first."""
    gateway = dict(session.gateway or {})
    device = gateway.get("device_id")
    if not device:
        raise TagError(409, "not_approved", "No gateway was approved.")
    if (await conn.execute(select(BINDINGS.c.id).where(BINDINGS.c.device_id == device))).first() is not None:
        raise TagError(409, "device_owned", "This gateway was connected by another setup meanwhile.")
    profile, profile_id = session.owner_profile, session.owner_profile_id
    if (await conn.execute(select(ProfileModel.id).where(ProfileModel.name == profile))).scalar_one_or_none() \
            != profile_id:
        raise TagError(404, "profile_not_found", "The profile no longer exists.")
    computer = (session.computer or {}).get("name") or "This computer"
    short = v2.short_id(bytes.fromhex(device))
    name = f"{computer} gateway"[:128]
    cid = str(uuid.uuid4())
    await conn.execute(insert(COMPANIONS), [{
        "id": cid, "name": name, "created_by": profile, "created_at": now, "updated_at": now,
        "last_seen_at": None, "version": None, "host": computer[:255], "heartbeat": None,
        "mode": PRIVATE, "owner_profile": profile, "owner_profile_id": profile_id,
        "installation_id": session.installation_id, "controller_pub": controller, "generation": 0,
        "state": "active", "paused": False, "lease_expires_at": None, "lease_credential_id": None,
        "gateway_device_id": device,
    }])
    hardware = _credential_row(creds.KIND_HARDWARE, hw_hash, cid, profile=None,
                               label=f"Cremind Connect on {computer}"[:128], now=now)
    content = _credential_row(creds.KIND_CONTENT, ct_hash, cid, profile=profile,
                              label=f"Cremind Connect on {computer}"[:128], now=now)
    await conn.execute(insert(CREDENTIALS), [hardware, content])
    device_row_id = str(uuid.uuid4())
    await conn.execute(insert(DEVICES), [{
        "id": device_row_id, "companion_id": cid, "kind": "gateway", "hw_id": v2.hw_id("gateway", bytes.fromhex(device)),
        "name": name, "owner_profile": profile, "bridge_device_id": None, "epoch": 0, "rotation": 0,
        "board": gateway.get("board"), "fw": gateway.get("fw") or None,
        "info": {"device_id": device, "proto": gateway.get("proto")}, "status": "pairing",
        "desired_revision": 0, "displayed_revision": 0, "clear_required": False,
        "created_at": now, "updated_at": now,
    }])
    binding_id = str(uuid.uuid4())
    await conn.execute(insert(BINDINGS), [{
        "id": binding_id, "device_id": device, "role": "gateway", "identity_pub": gateway["ik"], "short_id": short,
        "companion_id": cid, "tag_device_id": device_row_id, "owner_profile": profile,
        "owner_profile_id": profile_id, "state": "pairing", "generation": int(gateway.get("gen") or 0),
        "pending_generation": None, "paused": False, "fw": gateway.get("fw") or None, "board": gateway.get("board"),
        "info": {"proto": gateway.get("proto")}, "created_at": now, "updated_at": now,
        "paired_at": None, "ready_at": None,
    }])
    op = await queue_operation(conn, kind="claim_gateway", profile=profile, profile_id=profile_id,
                               companion_id=cid, binding_id=binding_id, now=now,
                               args={"device_id": device, "ik": gateway["ik"], "gen": int(gateway.get("gen") or 0),
                                     "mode": gateway.get("mode") or "claim"})
    return {"companion_id": cid, "credentials": {"hardware_id": hardware["id"], "content_id": content["id"]},
            "operation_id": op["id"]}


async def redeem_recover(conn, session: Any, controller: str, hw_hash: str, ct_hash: str, authority: Any,
                         now: float) -> dict[str, Any]:
    """Hand an existing private worker to the Connect that bound this session:
    atomically revoke the old worker's credentials and lease, move the
    worker generation, hold every device (bindings ``recovery_pending``; tags
    keep no content until rekeyed and cleared), then queue the recovery."""
    cid = session.companion_id
    comp = (await conn.execute(select(COMPANIONS).where(COMPANIONS.c.id == cid).with_for_update())).first()
    if comp is None or comp.mode != PRIVATE or comp.owner_profile_id != session.owner_profile_id:
        raise TagError(404, "connection_not_found", "The connection being recovered no longer exists.")
    profile, profile_id = session.owner_profile, session.owner_profile_id
    computer = (session.computer or {}).get("name") or "This computer"
    await conn.execute(update(CREDENTIALS).where(
        CREDENTIALS.c.companion_id == cid, CREDENTIALS.c.revoked_at.is_(None)).values(revoked_at=now))
    hardware = _credential_row(creds.KIND_HARDWARE, hw_hash, cid, profile=None,
                               label=f"Cremind Connect on {computer}"[:128], now=now)
    content = _credential_row(creds.KIND_CONTENT, ct_hash, cid, profile=profile,
                              label=f"Cremind Connect on {computer}"[:128], now=now)
    await conn.execute(insert(CREDENTIALS), [hardware, content])
    await conn.execute(update(COMPANIONS).where(COMPANIONS.c.id == cid).values(
        installation_id=session.installation_id, controller_pub=controller, generation=COMPANIONS.c.generation + 1,
        state="recovering", lease_expires_at=None, lease_credential_id=None, host=computer[:255],
        updated_at=now))
    # Every old command is the old worker's; the new one starts from the recovery.
    await conn.execute(update(COMMANDS).where(
        COMMANDS.c.companion_id == cid, COMMANDS.c.status.in_(("queued", "claimed"))).values(
        status="cancelled", completed_at=now, error="the connection moved to another computer"))
    bindings = (await conn.execute(select(BINDINGS).where(BINDINGS.c.companion_id == cid)
                                   .order_by(BINDINGS.c.role, BINDINGS.c.id))).all()
    await conn.execute(update(BINDINGS).where(BINDINGS.c.companion_id == cid).values(
        state="recovery_pending", pending_generation=None, updated_at=now))
    tag_ids = [b.tag_device_id for b in bindings if b.role == "tag" and b.tag_device_id]
    if tag_ids:
        await conn.execute(update(DELIVERIES).where(
            DELIVERIES.c.tag_device_id.in_(tag_ids), DELIVERIES.c.stage.in_(ACTIVE_STAGES)).values(
            stage="cancelled", outcome="cancelled", detail="recovering on another computer",
            finished_at=now, updated_at=now))
        await conn.execute(update(DEVICES).where(DEVICES.c.id.in_(tag_ids)).values(
            clear_required=True, status="recovering", updated_at=now))
    devices = [{"device_id": b.device_id, "role": b.role, "generation": int(b.generation or 0),
                "binding_id": b.id} for b in bindings]
    args = {"gateway_device_id": comp.gateway_device_id, "devices": devices}
    op_id = session.operation_id
    existing = None
    if op_id:
        existing = (await conn.execute(select(OPERATIONS).where(OPERATIONS.c.id == op_id))).first()
    if existing is not None:
        command = await insert_command(conn, companion_id=cid, kind=RUN_OPERATION,
                                       args={"operation_id": op_id, "kind": "recover_gateway"},
                                       requested_by=profile, ttl_s=_OPERATION_TTL_S, now=now)
        await conn.execute(update(OPERATIONS).where(OPERATIONS.c.id == op_id).values(
            state="queued", stage="queued", args=args, command_ids=[command["id"]], updated_at=now,
            expires_at=now + _OPERATION_TTL_S * 1000.0))
    else:
        op_id = (await queue_operation(conn, kind="recover_gateway", profile=profile, profile_id=profile_id,
                                       companion_id=cid, args=args, now=now))["id"]
    return {"companion_id": cid, "credentials": {"hardware_id": hardware["id"], "content_id": content["id"]},
            "operation_id": op_id}


async def tombstone(conn, binding: Any, *, reason: str, cleanup: str, now: float,
                    generation: int | None = None) -> None:
    """Record (or raise) a device's revocation: identity and the highest
    generation it was ever authorised for."""
    gen = max(int(binding.generation or 0), int(binding.pending_generation or 0), int(generation or 0))
    row = (await conn.execute(select(REVOCATIONS).where(REVOCATIONS.c.device_id == binding.device_id))).first()
    values = {"role": binding.role, "identity_pub": binding.identity_pub, "last_owner_profile": binding.owner_profile,
              "last_owner_profile_id": binding.owner_profile_id, "reason": reason[:64], "cleanup": cleanup,
              "updated_at": now}
    if row is None:
        await conn.execute(insert(REVOCATIONS), [{"device_id": binding.device_id, "highest_generation": gen,
                                                  "revoked_at": now, **values}])
    else:
        await conn.execute(update(REVOCATIONS).where(REVOCATIONS.c.device_id == binding.device_id).values(
            highest_generation=max(gen, int(row.highest_generation or 0)), **values))


async def revoke_profile_workers(conn, profile: str) -> list[str]:
    """Deleting a profile: every private worker it owns is revoked at once —
    credentials revoked, tombstones written (cleanup ``abandoned``: nobody is
    left to finish it), the worker and its rows deleted. Physical devices keep
    their owner until reset; the tombstones keep their generations. Runs in
    the caller's transaction BEFORE the profile row goes."""
    now = _now()
    rows = (await conn.execute(select(COMPANIONS).where(
        COMPANIONS.c.mode == PRIVATE, COMPANIONS.c.owner_profile == profile))).all()
    gone = []
    for comp in rows:
        await conn.execute(update(CREDENTIALS).where(
            CREDENTIALS.c.companion_id == comp.id, CREDENTIALS.c.revoked_at.is_(None)).values(revoked_at=now))
        for binding in (await conn.execute(select(BINDINGS).where(BINDINGS.c.companion_id == comp.id))).all():
            await tombstone(conn, binding, reason="profile deleted", cleanup="abandoned", now=now)
        await conn.execute(delete(COMPANIONS).where(COMPANIONS.c.id == comp.id))
        gone.append(comp.id)
    return gone


def companion_status(row: Any, now: float | None = None) -> str:
    now = now or _now()
    if row.state == "recovering":
        return "recovery_pending"
    if row.state == "removing":
        return "removal_pending"
    if row.paused:
        return "paused"
    if not row.last_seen_at:
        return "setting_up"
    if now - float(row.last_seen_at) > ONLINE_WINDOW_MS:
        return "offline"
    return "connected"


__all__ = [
    "BINDINGS", "INSTALLATIONS", "LEGACY", "OPERATIONS", "PRIVATE", "REVOCATIONS", "RUN_OPERATION",
    "check_gateway_available", "companion_status", "companion_summary", "owned_companion_row", "owns",
    "profile_uuid", "queue_operation", "redeem_connect", "redeem_recover", "revoke_profile_workers",
    "tombstone", "touch_installation",
]
