"""TagStorage — async Core statements over the ``tag_*`` tables.

Every public method opens and commits its own transaction (the repo's store
convention). The allocation and supersede helpers at module level take a
connection instead, because the projection worker and the direct device
actions must allocate inside the transaction that inserts the deliveries.

Allocation (no database sequences — a restore would not reset them):

- ``delivery_id``: ``UPDATE tag_counters SET value = value + n … RETURNING``;
- a profile's delivery ``seq``: ``UPDATE tag_streams SET next_delivery_seq =
  next_delivery_seq + n … RETURNING``. Lock order is always the stream row,
  then the counter row. The stream row lock also orders commits, so a cursor
  reader that reads ``next_delivery_seq`` first and then rows ``<=`` it never
  skips an uncommitted lower seq.

Serialised shapes (``*_json``) are what the REST API returns; timestamps are
epoch milliseconds. The connector's job shape (ISO timestamps) is
:func:`job_json`.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from sqlalchemy import and_, case, delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.databases import DatabaseProvider, get_database_provider
from app.storage.models import (
    TagCommandModel, TagCompanionModel, TagCounterModel, TagCredentialModel,
    TagDeliveryModel, TagDeviceModel, TagPreviewModel, TagSettingsModel,
    TagStreamModel,
)
from app.tags import journal
from app.tags.cards import CardSpec, iso, make_card

COMPANIONS = TagCompanionModel.__table__
CREDENTIALS = TagCredentialModel.__table__
DEVICES = TagDeviceModel.__table__
STREAMS = TagStreamModel.__table__
DELIVERIES = TagDeliveryModel.__table__
COUNTERS = TagCounterModel.__table__
COMMANDS = TagCommandModel.__table__
PREVIEWS = TagPreviewModel.__table__
SETTINGS = TagSettingsModel.__table__

DELIVERY_COUNTER = "delivery_id"

# Delivery stages, in order; the last one is also a terminal outcome.
STAGES = (
    "queued", "companion_accepted", "gateway_received", "bridge_received",
    "transferring", "refreshing", "displayed",
)
STAGE_RANK = {s: i for i, s in enumerate(STAGES)}
TERMINAL_STAGES = ("displayed", "superseded", "expired", "cancelled", "failed", "uncertain")
ACTIVE_STAGES = STAGES[:-1]
# A card is no longer on (or headed to) the tag.
GONE_STAGES = ("superseded", "expired", "cancelled", "failed")

COMMAND_ACTIVE = ("queued", "claimed")
COMMAND_TERMINAL = ("succeeded", "failed", "expired", "cancelled")
# A claimed command is the companion's work in progress: expire it only this
# long after its deadline (a late result is still accepted after that).
CLAIMED_GRACE_MS = 3_600_000.0

ONLINE_WINDOW_MS = 120_000

# Device statuses only an admin action (claim, assign, release) or a later
# success of the failed command clears — a heartbeat never overwrites them.
# The v2 ones (private workers) belong to an operation or a user choice.
STUCK_STATUSES = ("clear_failed", "assign_failed", "pairing", "paused", "recovering", "removing",
                  "needs_bridge")


def now_ms() -> float:
    return time.time() * 1000


def parse_ts(value: Any) -> float | None:
    """ISO 8601 (``Z`` allowed) or epoch ms -> epoch ms; ``None`` if unusable."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp() * 1000
    return None


# ── serialisation ───────────────────────────────────────────────────────────


def companion_json(row: Any, *, now: float | None = None) -> dict[str, Any]:
    r = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    seen = r.get("last_seen_at")
    return {
        "id": r["id"],
        "name": r["name"],
        "created_by": r.get("created_by") or "",
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "last_seen_at": seen,
        "online": bool(seen and (now or now_ms()) - seen <= ONLINE_WINDOW_MS),
        "version": r.get("version"),
        "host": r.get("host"),
        "heartbeat": r.get("heartbeat"),
        "mode": r.get("mode") or "legacy_shared",
        "owner_profile": r.get("owner_profile"),
        "state": r.get("state") or "active",
        "paused": bool(r.get("paused")),
        "generation": int(r.get("generation") or 0),
        "gateway_device_id": r.get("gateway_device_id"),
    }


def credential_json(row: Any) -> dict[str, Any]:
    r = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    return {
        "id": r["id"],
        "companion_id": r["companion_id"],
        "kind": r["kind"],
        "profile": r.get("profile"),
        "label": r.get("label") or "",
        "created_by": r.get("created_by") or "",
        "created_at": r["created_at"],
        "last_used_at": r.get("last_used_at"),
        "revoked_at": r.get("revoked_at"),
        "revoked": r.get("revoked_at") is not None,
    }


def device_json(row: Any) -> dict[str, Any]:
    r = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    return {
        "id": r["id"],
        "companion_id": r["companion_id"],
        "kind": r["kind"],
        "hw_id": r["hw_id"],
        "name": r.get("name") or "",
        "owner_profile": r.get("owner_profile"),
        "bridge_device_id": r.get("bridge_device_id"),
        "epoch": int(r.get("epoch") or 0),
        "rotation": int(r.get("rotation") or 0),
        "board": r.get("board"),
        "panel": r.get("panel"),
        "width": r.get("width"),
        "height": r.get("height"),
        "planes": r.get("planes"),
        "fw": r.get("fw"),
        "info": r.get("info") or {},
        "status": r.get("status") or "unclaimed",
        "battery_mv": r.get("battery_mv"),
        "rssi": r.get("rssi"),
        "last_contact_at": r.get("last_contact_at"),
        "desired_revision": int(r.get("desired_revision") or 0),
        "displayed_revision": int(r.get("displayed_revision") or 0),
        "displayed_digest": r.get("displayed_digest"),
        "clear_required": bool(r.get("clear_required")),
        "claimed_at": r.get("claimed_at"),
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def delivery_json(row: Any) -> dict[str, Any]:
    r = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    return {
        "id": int(r["id"]),
        "seq": int(r["seq"]),
        "profile": r["profile"],
        "device_id": r["tag_device_id"],
        "companion_id": r.get("companion_id"),
        "epoch": int(r.get("epoch") or 0),
        "event_id": r.get("event_id"),
        "kind": r["kind"],
        "priority": int(r.get("priority") or 0),
        "replace_key": r.get("replace_key"),
        "resolves": r.get("resolves"),
        "card": r.get("card"),
        "stage": r["stage"],
        "terminal": r["stage"] in TERMINAL_STAGES,
        "outcome": r.get("outcome"),
        "status_code": r.get("status_code"),
        "revision": r.get("revision"),
        "digest": r.get("digest"),
        "detail": r.get("detail"),
        "timing": r.get("timing"),
        "stage_times": r.get("stage_times") or {},
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "expires_at": r["expires_at"],
        "finished_at": r.get("finished_at"),
    }


def command_json(row: Any) -> dict[str, Any]:
    r = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    return {
        "id": r["id"],
        "companion_id": r["companion_id"],
        "kind": r["kind"],
        "args": r.get("args") or {},
        "requested_by": r.get("requested_by") or "",
        "status": r["status"],
        "result": r.get("result"),
        "error": r.get("error"),
        "created_at": r["created_at"],
        "claimed_at": r.get("claimed_at"),
        "completed_at": r.get("completed_at"),
        "expires_at": r["expires_at"],
    }


def connector_command_json(row: Any) -> dict[str, Any]:
    """A command as the companion sees it (ISO timestamps)."""
    c = command_json(row)
    return {
        "id": c["id"],
        "kind": c["kind"],
        "args": c["args"],
        "status": c["status"],
        "created_at": iso(c["created_at"]),
        "expires_at": iso(c["expires_at"]),
    }


def job_json(row: Any, hw_id: str) -> dict[str, Any]:
    """A delivery as a connector job (connector-api.md "Job shape")."""
    d = delivery_json(row)
    return {
        "delivery_id": d["id"],
        "seq": d["seq"],
        "tag_id": hw_id,
        "epoch": d["epoch"],
        "kind": d["kind"],
        "priority": d["priority"],
        "replace_key": d["replace_key"],
        "resolves": d["resolves"],
        "created_at": iso(d["created_at"]),
        "expires_at": iso(d["expires_at"]),
        "stage": d["stage"],
        "card": d["card"],
    }


async def cas_stage(conn: AsyncConnection, delivery_id: int, expected: str | Sequence[str],
                    values: dict[str, Any]) -> bool:
    """Update a delivery only if its stage is still ``expected`` (one stage,
    or any of several). The one write path for receipts and user cancels, so a
    terminal state another transaction committed first is never reopened."""
    stage_cond = (DELIVERIES.c.stage == expected if isinstance(expected, str)
                  else DELIVERIES.c.stage.in_(list(expected)))
    result = await conn.execute(update(DELIVERIES).where(
        DELIVERIES.c.id == delivery_id, stage_cond,
    ).values(**values))
    return bool(result.rowcount)


def _servable(row: Any, profile: str) -> bool:
    """Whether a delivery row (joined with its device's owner and epoch) may
    be served to ``profile``'s companion: only on a tag the profile still
    owns, and a live card only at the tag's current epoch."""
    if row.device_owner != profile:
        return False
    return row.stage in TERMINAL_STAGES or int(row.epoch or 0) == int(row.device_epoch or 0)


# ── allocation / supersede helpers (inside a caller's transaction) ──────────


def _dialect(conn: AsyncConnection) -> str:
    return conn.dialect.name


async def ensure_stream(conn: AsyncConnection, profile: str, now: float | None = None) -> None:
    await conn.execute(journal.insert_ignore(
        _dialect(conn), STREAMS, journal.new_stream_values(profile, now or now_ms()), ["profile"],
    ))


async def lock_stream(conn: AsyncConnection, profile: str, now: float) -> None:
    """Take the profile's stream-row lock FIRST in a transaction that will
    also touch its deliveries. Every writer orders its locks stream row ->
    device row -> delivery rows -> counter row, so none of them can deadlock
    another on PostgreSQL (SQLite has one writer anyway)."""
    stmt = update(STREAMS).where(STREAMS.c.profile == profile).values(updated_at=now)
    if not (await conn.execute(stmt)).rowcount:
        await ensure_stream(conn, profile, now)
        await conn.execute(stmt)


async def begin_write(conn: AsyncConnection) -> None:
    """On SQLite, take the database write lock before the transaction's first
    read: a deferred transaction that reads and THEN writes fails outright
    (``SQLITE_BUSY_SNAPSHOT``) when another writer committed in between. A
    no-op on PostgreSQL, whose transactions lock rows, not the database."""
    if _dialect(conn) == "sqlite":
        await conn.execute(update(COUNTERS).where(COUNTERS.c.name == DELIVERY_COUNTER)
                           .values(value=COUNTERS.c.value))


async def lock_device(conn: AsyncConnection, device_id: str, now: float) -> Any:
    """Lock a device for an ownership or epoch change: its CURRENT owner's
    stream row first — the lock every content writer of that owner (the
    projection batch, ``display``/``clear``, a clear lift) takes first — then
    the device row itself (``FOR UPDATE``). A content writer that read the
    owner under its stream lock therefore never writes onto a tag that
    changed hands meanwhile. Retries if the owner changed between the read
    and the lock. Returns the locked row, or ``None``."""
    for _ in range(5):
        row = (await conn.execute(select(DEVICES).where(DEVICES.c.id == device_id))).first()
        if row is None:
            return None
        if row.owner_profile:
            await lock_stream(conn, row.owner_profile, now)
        locked = (await conn.execute(
            select(DEVICES).where(DEVICES.c.id == device_id).with_for_update()
        )).first()
        if locked is None or locked.owner_profile == row.owner_profile:
            return locked
    raise RuntimeError(f"tag {device_id} kept changing owner; try again")


async def merge_stream_state(conn: AsyncConnection, profile: str, mutate: Callable[[dict], Any]) -> None:
    row = (await conn.execute(select(STREAMS.c.state).where(STREAMS.c.profile == profile))).first()
    if row is None:
        return
    state = dict(row.state or {})
    mutate(state)
    await conn.execute(update(STREAMS).where(STREAMS.c.profile == profile).values(state=state))


def reset_content_state(state: dict[str, Any], device_id: str | None = None) -> None:
    """Forget what periodic content and diagnostics were already sent, so the
    next run sends them again (a tag that just became able to show them)."""
    state.pop("periodic", None)
    if device_id is None:
        state.pop("diag", None)
    elif isinstance(state.get("diag"), dict):
        state["diag"].pop(device_id, None)


# Owner-specific state a device carries, reset on every change of owner.
OWNER_RESET = {
    "name": "", "desired_revision": 0, "displayed_revision": 0, "displayed_digest": None,
}


async def drop_previews(conn: AsyncConnection, device_ids: Sequence[str]) -> None:
    if device_ids:
        await conn.execute(delete(PREVIEWS).where(PREVIEWS.c.tag_device_id.in_(list(device_ids))))


CLEAR_RETRIES = 3
_CLEAR_TTL_S = 7 * 24 * 3600.0


async def requeue_clear(conn: AsyncConnection, companion_id: str, hw_id: str, epoch: int,
                        now: float) -> str:
    """A ``clear_tag`` for ``(tag, epoch)`` ended failed or expired while the
    tag still waits for it: queue another, up to :data:`CLEAR_RETRIES` in
    all; after that mark the device ``clear_failed`` (the UI/CLI show it; a new
    claim or release starts over). Returns ``requeued`` / ``clear_failed`` /
    ``not_needed``."""
    device = (await conn.execute(select(DEVICES).where(
        DEVICES.c.companion_id == companion_id, DEVICES.c.kind == "tag", DEVICES.c.hw_id == hw_id,
    ))).first()
    if device is None or not device.clear_required or int(device.epoch or 0) != int(epoch):
        return "not_needed"
    rows = (await conn.execute(select(COMMANDS.c.status, COMMANDS.c.args).where(
        COMMANDS.c.companion_id == companion_id, COMMANDS.c.kind == "clear_tag",
    ))).all()
    same = [r for r in rows if isinstance(r.args, dict) and r.args.get("tag_id") == hw_id
            and int(r.args.get("epoch") or 0) == int(epoch)]
    if any(r.status in COMMAND_ACTIVE for r in same):
        return "not_needed"
    if len(same) >= CLEAR_RETRIES:
        await conn.execute(update(DEVICES).where(DEVICES.c.id == device.id)
                           .values(status="clear_failed", updated_at=now))
        return "clear_failed"
    await insert_command(conn, companion_id=companion_id, kind="clear_tag",
                         args={"tag_id": hw_id, "epoch": int(epoch)}, requested_by="system",
                         ttl_s=_CLEAR_TTL_S, now=now)
    return "requeued"


async def _settle_assign(conn: AsyncConnection, device: Any, args: dict[str, Any], epoch: int, *,
                         status: str, error: str | None, result: dict[str, Any] | None,
                         now: float) -> None:
    """An ``assign_tag`` result at the tag's current epoch. A failure marks
    the tag ``assign_failed`` until an admin assigns it elsewhere or releases
    it; ``bridge_full`` also drops the tag from that bridge (it holds no slot
    there) and records the bridge's ``max_tags`` when the result names it —
    the same for a gateway serving tags on its own radio (``bridge_hw_id``
    names the gateway then). A success clears an earlier ``assign_failed``.
    Locks: the device (held), then the bridge (or gateway)."""
    if int(device.epoch or 0) != int(epoch):
        return
    if status == "succeeded":
        if device.status == "assign_failed":
            await conn.execute(update(DEVICES).where(DEVICES.c.id == device.id).values(
                status="assigning" if device.owner_profile else "unclaimed", updated_at=now))
        return
    values: dict[str, Any] = {"status": "assign_failed", "updated_at": now}
    if error == "bridge_full":
        values["bridge_device_id"] = None
        max_tags = _bounded((result or {}).get("max_tags"), 1, 255)
        bridge_hw = args.get("bridge_hw_id")
        if max_tags is not None and isinstance(bridge_hw, str):
            bridge = (await conn.execute(select(DEVICES).where(
                DEVICES.c.companion_id == device.companion_id, DEVICES.c.kind.in_(("bridge", "gateway")),
                DEVICES.c.hw_id == bridge_hw,
            ).with_for_update())).first()
            if bridge is not None:
                await conn.execute(update(DEVICES).where(DEVICES.c.id == bridge.id).values(
                    info={**(bridge.info or {}), "max_tags": max_tags}, updated_at=now))
    await conn.execute(update(DEVICES).where(DEVICES.c.id == device.id).values(**values))


async def allocate_delivery_seqs(conn: AsyncConnection, profile: str, n: int, now: float) -> list[int]:
    stmt = (
        update(STREAMS).where(STREAMS.c.profile == profile)
        .values(next_delivery_seq=STREAMS.c.next_delivery_seq + n, updated_at=now)
        .returning(STREAMS.c.next_delivery_seq)
    )
    head = (await conn.execute(stmt)).scalar_one_or_none()
    if head is None:
        await ensure_stream(conn, profile, now)
        head = (await conn.execute(stmt)).scalar_one()
    return list(range(int(head) - n + 1, int(head) + 1))


async def allocate_delivery_ids(conn: AsyncConnection, n: int) -> list[int]:
    stmt = (
        update(COUNTERS).where(COUNTERS.c.name == DELIVERY_COUNTER)
        .values(value=COUNTERS.c.value + n).returning(COUNTERS.c.value)
    )
    head = (await conn.execute(stmt)).scalar_one_or_none()
    if head is None:
        await conn.execute(journal.insert_ignore(
            _dialect(conn), COUNTERS, {"name": DELIVERY_COUNTER, "value": 0}, ["name"],
        ))
        head = (await conn.execute(stmt)).scalar_one()
    return list(range(int(head) - n + 1, int(head) + 1))


async def _retire_key(conn: AsyncConnection, device_id: str, key: str, now: float, detail: str) -> None:
    await conn.execute(
        update(DELIVERIES)
        .where(
            DELIVERIES.c.tag_device_id == device_id,
            DELIVERIES.c.replace_key == key,
            DELIVERIES.c.stage.in_(ACTIVE_STAGES),
        )
        .values(stage="superseded", outcome="superseded", detail=detail,
                finished_at=now, updated_at=now)
    )


async def write_deliveries(
    conn: AsyncConnection, profile: str,
    items: Sequence[tuple[dict[str, Any], CardSpec, str | None]], now: float,
) -> list[dict[str, Any]]:
    """Insert one delivery per ``(device, spec, event_id)`` inside ``conn``'s
    transaction: allocate the profile's seqs, retire older active deliveries
    with the same ``(tag, replace_key)`` (or the key a ``resolved`` card
    names), allocate ids, insert. Returns the inserted rows (as dicts).

    Lock order: stream row, delivery rows, then the counter row — the one row
    every profile shares — taken last so it is held only for the insert."""
    if not items:
        return []
    n = len(items)
    seqs = await allocate_delivery_seqs(conn, profile, n, now)

    rows: list[dict[str, Any]] = []
    live: dict[tuple[str, str], int] = {}
    retired: set[tuple[str, str]] = set()
    for i, (device, spec, event_id) in enumerate(items):
        row = {
            "id": None,
            "profile": profile,
            "seq": seqs[i],
            "companion_id": device.get("companion_id"),
            "tag_device_id": device["id"],
            "epoch": int(device.get("epoch") or 0),
            "event_id": event_id,
            "kind": spec.kind,
            "priority": int(spec.priority),
            "replace_key": spec.replace_key,
            "resolves": spec.resolves,
            "card": spec.card,
            "stage": "queued",
            "outcome": None,
            "status_code": None,
            "revision": None,
            "digest": None,
            "detail": None,
            "timing": None,
            "stage_times": {"queued": now},
            "created_at": now,
            "updated_at": now,
            "expires_at": float(spec.expires_at),
            "finished_at": None,
        }
        for key, reason in ((spec.replace_key, "replaced"), (spec.resolves, "resolved")):
            if not key:
                continue
            slot = (device["id"], key)
            if slot not in retired:
                await _retire_key(conn, device["id"], key, now, reason)
                retired.add(slot)
            prev = live.pop(slot, None)
            if prev is not None:
                rows[prev].update(stage="superseded", outcome="superseded", detail=reason,
                                  finished_at=now)
        if spec.replace_key and spec.kind != "resolved":
            live[(device["id"], spec.replace_key)] = i
        rows.append(row)
    for row, delivery_id in zip(rows, await allocate_delivery_ids(conn, n)):
        row["id"] = delivery_id
        # Every card carries a key the companion can remove it by: a cancel is
        # sent as a ``resolved`` job naming it (see service.cancel_delivery).
        if row["replace_key"] is None and row["kind"] not in ("resolved", "clear"):
            row["replace_key"] = f"delivery:{delivery_id}"
    await conn.execute(insert(DELIVERIES), rows)
    return rows


async def open_key_devices(conn: AsyncConnection, profile: str, key: str, now: float) -> set[str]:
    """Tags on which the card keyed ``key`` is still live (not retired,
    resolved, or expired) — where a ``resolved`` card has something to do."""
    rows = (await conn.execute(
        select(DELIVERIES.c.tag_device_id, DELIVERIES.c.id, DELIVERIES.c.kind,
               DELIVERIES.c.stage, DELIVERIES.c.expires_at)
        .where(
            DELIVERIES.c.profile == profile,
            or_(DELIVERIES.c.replace_key == key, DELIVERIES.c.resolves == key),
        )
        .order_by(DELIVERIES.c.id.asc())
    )).all()
    last: dict[str, Any] = {}
    for r in rows:
        last[r.tag_device_id] = r
    return {
        dev for dev, r in last.items()
        if r.kind != "resolved" and r.stage not in GONE_STAGES and float(r.expires_at) > now
    }


async def insert_command(
    conn: AsyncConnection, *, companion_id: str, kind: str, args: dict[str, Any],
    requested_by: str, ttl_s: float, now: float,
) -> dict[str, Any]:
    row = {
        "id": str(uuid.uuid4()),
        "companion_id": companion_id,
        "kind": kind,
        "args": args,
        "requested_by": requested_by or "",
        "status": "queued",
        "result": None,
        "error": None,
        "created_at": now,
        "claimed_at": None,
        "completed_at": None,
        "expires_at": now + ttl_s * 1000.0,
    }
    await conn.execute(insert(COMMANDS), [row])
    return row


async def cancel_tag_commands(conn: AsyncConnection, companion_id: str, hw_id: str, now: float) -> int:
    """Cancel queued per-tag commands (assign/clear/refresh) for one tag."""
    rows = (await conn.execute(
        select(COMMANDS.c.id, COMMANDS.c.kind, COMMANDS.c.args).where(
            COMMANDS.c.companion_id == companion_id, COMMANDS.c.status == "queued",
        )
    )).all()
    doomed = [
        r.id for r in rows
        if r.kind in ("assign_tag", "clear_tag", "refresh_tag")
        and isinstance(r.args, dict) and r.args.get("tag_id") == hw_id
    ]
    if doomed:
        await conn.execute(
            update(COMMANDS).where(COMMANDS.c.id.in_(doomed))
            .values(status="cancelled", completed_at=now, error="superseded by a newer assignment")
        )
    return len(doomed)


async def cancel_device_deliveries(conn: AsyncConnection, device_id: str, now: float, detail: str) -> int:
    result = await conn.execute(
        update(DELIVERIES)
        .where(DELIVERIES.c.tag_device_id == device_id, DELIVERIES.c.stage.in_(ACTIVE_STAGES))
        .values(stage="cancelled", outcome="cancelled", detail=detail,
                finished_at=now, updated_at=now)
    )
    return int(result.rowcount or 0)


MAX_EPOCH = 2 ** 32 - 1  # the protocol's uint32
_TAG_COMMAND_TTL_S = 7 * 24 * 3600.0


def _reported_epoch(item: dict[str, Any]) -> int | None:
    value = item.get("epoch")
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_EPOCH:
        return None
    return value


async def _raise_epoch(conn: AsyncConnection, row: Any, reported: int, now: float) -> int:
    """The new epoch of a tag the companion reports at ``reported`` (above the
    stored one). Work still owed under the old epoch — the owner's assignment,
    a pending clear — is re-queued at ``reported + 1`` and the tag's active
    deliveries move with it; otherwise the stored epoch simply becomes the
    reported one. The assignment names the tag's parent by its hardware id:
    a bridge's, or the gateway's (``gw-…``) for a tag it serves on its own
    radio."""
    needs_assign = bool(row.owner_profile) and row.bridge_device_id is not None
    needs_clear = bool(row.clear_required)
    if not (needs_assign or needs_clear) or reported >= MAX_EPOCH:
        return reported
    epoch = reported + 1
    await cancel_tag_commands(conn, row.companion_id, row.hw_id, now)
    if needs_assign:
        bridge_hw = (await conn.execute(
            select(DEVICES.c.hw_id).where(DEVICES.c.id == row.bridge_device_id)
        )).scalar_one_or_none()
        if bridge_hw:
            await insert_command(conn, companion_id=row.companion_id, kind="assign_tag",
                                 args={"tag_id": row.hw_id, "bridge_hw_id": bridge_hw, "epoch": epoch},
                                 requested_by="system", ttl_s=_TAG_COMMAND_TTL_S, now=now)
    if needs_clear:
        await insert_command(conn, companion_id=row.companion_id, kind="clear_tag",
                             args={"tag_id": row.hw_id, "epoch": epoch},
                             requested_by="system", ttl_s=_TAG_COMMAND_TTL_S, now=now)
    await conn.execute(update(DELIVERIES).where(
        DELIVERIES.c.tag_device_id == row.id, DELIVERIES.c.stage.in_(ACTIVE_STAGES),
    ).values(epoch=epoch, updated_at=now))
    return epoch


# Long-poll wake-ups for ``GET commands?wait=``: one event per companion,
# rebuilt when the running loop changes (tests run one loop per case).
_command_events: dict[str, tuple[Any, asyncio.Event]] = {}


def command_event(companion_id: str) -> asyncio.Event:
    loop = asyncio.get_running_loop()
    hit = _command_events.get(companion_id)
    if hit is None or hit[0] is not loop:
        hit = (loop, asyncio.Event())
        _command_events[companion_id] = hit
    return hit[1]


def notify_commands(companion_ids: Iterable[str | None]) -> None:
    """Wake long-polls of these companions (call after the commit)."""
    for cid in set(c for c in companion_ids if c):
        hit = _command_events.get(cid)
        if hit is None:
            continue
        loop, event = hit
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            _command_events.pop(cid, None)


# ── the store ───────────────────────────────────────────────────────────────


class TagStorage:
    """Async CRUD over the ``tag_*`` tables. Methods scope by the arguments
    they are given — callers (the API layer) decide what a caller may see."""

    def __init__(self, provider: DatabaseProvider | None = None):
        self._provider_override = provider

    @property
    def provider(self) -> DatabaseProvider:
        return self._provider_override or get_database_provider()

    @property
    def engine(self) -> AsyncEngine:
        return self.provider.async_engine()

    # ── settings ──

    async def get_settings(self, profile: str) -> dict[str, Any] | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(
                select(SETTINGS).where(SETTINGS.c.profile == profile)
            )).first()
        if row is None:
            return None
        return {"enabled": bool(row.enabled), "options": row.options or {}, "updated_at": row.updated_at}

    async def save_settings(self, profile: str, *, enabled: bool | None = None,
                            options: dict[str, Any] | None = None, replace_options: bool = False,
                            merge: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
                            ) -> dict[str, Any]:
        """Upsert a profile's settings. ``options`` replaces the stored
        overrides when ``replace_options``; ``merge`` maps the stored
        overrides to new ones (a PATCH; it may raise to refuse, and nothing is
        written); neither leaves them. The row is locked for the read, so two
        concurrent merges cannot lose each other's keys. Enabling makes sure
        the profile's stream row exists."""
        now = now_ms()
        async with self.engine.begin() as conn:
            await conn.execute(journal.insert_ignore(_dialect(conn), SETTINGS, {
                "profile": profile, "enabled": False, "options": {}, "updated_at": now,
            }, ["profile"]))
            row = (await conn.execute(
                select(SETTINGS).where(SETTINGS.c.profile == profile).with_for_update()
            )).first()
            new_enabled = bool(row.enabled)
            if enabled is not None:
                new_enabled = bool(enabled)
            new_options = dict(row.options or {})
            if replace_options:
                new_options = dict(options or {})
            if merge is not None:
                new_options = merge(new_options)
            await conn.execute(
                update(SETTINGS).where(SETTINGS.c.profile == profile)
                .values(enabled=new_enabled, options=new_options, updated_at=now)
            )
            if new_enabled:
                await ensure_stream(conn, profile, now)
        journal.invalidate_enabled_cache()
        return {"enabled": new_enabled, "options": new_options, "updated_at": now}

    async def enabled_profiles(self) -> list[str]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                select(SETTINGS.c.profile).where(SETTINGS.c.enabled.is_(True))
            )).scalars().all()
        return sorted(str(r) for r in rows)

    async def profiles_with_backlog(self) -> list[str]:
        """Profiles whose journal has entries not projected yet (one query)."""
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                select(STREAMS.c.profile).where(STREAMS.c.projected_seq < STREAMS.c.next_seq)
            )).scalars().all()
        return sorted(str(r) for r in rows)

    async def get_stream(self, profile: str) -> dict[str, Any] | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(STREAMS).where(STREAMS.c.profile == profile))).first()
        return dict(row._mapping) if row is not None else None

    # ── companions ──

    async def list_companions(self, *, include_private: bool = True,
                              private_owner: str | None = None) -> list[dict[str, Any]]:
        """Companions. ``include_private=False`` (the admin's shared-hardware
        views) leaves every private worker out; ``private_owner`` keeps only
        that profile's private workers next to the shared ones."""
        conds = []
        if not include_private:
            conds.append(COMPANIONS.c.mode != "private")
        elif private_owner is not None:
            conds.append(or_(COMPANIONS.c.mode != "private", COMPANIONS.c.owner_profile == private_owner))
        async with self.engine.connect() as conn:
            rows = (await conn.execute(select(COMPANIONS).where(*conds)
                                       .order_by(COMPANIONS.c.created_at.asc()))).all()
        now = now_ms()
        return [companion_json(r, now=now) for r in rows]

    async def is_private(self, companion_id: str) -> bool:
        async with self.engine.connect() as conn:
            mode = (await conn.execute(select(COMPANIONS.c.mode).where(COMPANIONS.c.id == companion_id))).scalar()
        return mode == "private"

    async def get_companion(self, companion_id: str) -> dict[str, Any] | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(COMPANIONS).where(COMPANIONS.c.id == companion_id))).first()
        return companion_json(row) if row is not None else None

    async def create_companion(self, *, name: str, created_by: str,
                               credential: dict[str, Any]) -> dict[str, Any]:
        """Insert a companion and its first hardware credential together."""
        now = now_ms()
        cid = str(uuid.uuid4())
        async with self.engine.begin() as conn:
            await conn.execute(insert(COMPANIONS), [{
                "id": cid, "name": name, "created_by": created_by or "",
                "created_at": now, "updated_at": now, "last_seen_at": None,
                "version": None, "host": None, "heartbeat": None,
            }])
            await conn.execute(insert(CREDENTIALS), [{**credential, "companion_id": cid, "created_at": now}])
            row = (await conn.execute(select(COMPANIONS).where(COMPANIONS.c.id == cid))).first()
        return companion_json(row)

    async def delete_companion(self, companion_id: str) -> bool:
        async with self.engine.begin() as conn:
            await conn.execute(update(CREDENTIALS).where(
                CREDENTIALS.c.companion_id == companion_id, CREDENTIALS.c.revoked_at.is_(None),
            ).values(revoked_at=now_ms()))
            result = await conn.execute(delete(COMPANIONS).where(COMPANIONS.c.id == companion_id))
            return int(result.rowcount or 0) > 0

    async def record_heartbeat(self, companion_id: str, body: dict[str, Any]) -> int:
        """Store a heartbeat; return the number of commands still queued."""
        now = now_ms()
        comp = body.get("companion") if isinstance(body.get("companion"), dict) else {}
        queue = body.get("queue") if isinstance(body.get("queue"), dict) else {}
        devices = body.get("devices") if isinstance(body.get("devices"), list) else []
        async with self.engine.begin() as conn:
            await conn.execute(
                update(COMPANIONS).where(COMPANIONS.c.id == companion_id).values(
                    last_seen_at=now, updated_at=now,
                    version=str(comp.get("version") or "")[:64] or None,
                    host=str(comp.get("host") or "")[:255] or None,
                    heartbeat={"companion": _small(comp), "queue": _small(queue), "at": now},
                )
            )
            existing = {
                (r.kind, r.hw_id): r for r in (await conn.execute(
                    select(DEVICES).where(DEVICES.c.companion_id == companion_id)
                )).all()
            }
            found = []
            for item in devices[:500]:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("kind") or "tag").strip()
                row = existing.get((kind, str(item.get("hw_id") or "").strip()))
                if row is not None:
                    found.append((kind, row, item))
            # Device rows in one order — tags, then bridges, each by id — as
            # claim / assign lock a tag, then its bridge.
            found.sort(key=lambda f: (f[0] != "tag", f[0], f[1].id))
            for kind, row, item in found:
                values: dict[str, Any] = {"updated_at": now}
                if _bounded(item.get("battery_mv"), 0, 100_000) is not None:
                    values["battery_mv"] = item["battery_mv"]
                if _bounded(item.get("rssi"), -1000, 1000) is not None:
                    values["rssi"] = item["rssi"]
                contact = parse_ts(item.get("last_contact_at"))
                if contact is not None:
                    values["last_contact_at"] = contact
                rev = _bounded(item.get("displayed_revision"), 0, MAX_EPOCH)
                if rev is not None and rev >= int(row.displayed_revision or 0):
                    values["displayed_revision"] = rev
                    if isinstance(item.get("displayed_digest"), str):
                        values["displayed_digest"] = item["displayed_digest"][:64]
                status = item.get("status")
                if status in ("ok", "pending", "offline", "error"):
                    # Judged on the row as the UPDATE finds it, not as first read.
                    values["status"] = case(
                        (DEVICES.c.status.in_(STUCK_STATUSES), DEVICES.c.status),
                        (and_(DEVICES.c.kind == "tag", DEVICES.c.owner_profile.is_(None)), DEVICES.c.status),
                        else_=status,
                    )
                await conn.execute(update(DEVICES).where(DEVICES.c.id == row.id).values(**values))
            pending = (await conn.execute(
                select(func.count()).select_from(COMMANDS).where(
                    COMMANDS.c.companion_id == companion_id, COMMANDS.c.status == "queued",
                    COMMANDS.c.expires_at > now,
                )
            )).scalar_one()
        return int(pending or 0)

    # ── credentials ──

    async def insert_credential(self, row: dict[str, Any]) -> dict[str, Any]:
        now = now_ms()
        full = {"created_at": now, "last_used_at": None, "revoked_at": None, **row}
        async with self.engine.begin() as conn:
            await conn.execute(insert(CREDENTIALS), [full])
        return credential_json(full)

    async def get_credential(self, credential_id: str) -> dict[str, Any] | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(CREDENTIALS).where(CREDENTIALS.c.id == credential_id))).first()
        return dict(row._mapping) if row is not None else None

    async def list_credentials(self, *, profile: str | None = None, kind: str | None = None,
                               companion_id: str | None = None) -> list[dict[str, Any]]:
        conds = []
        if profile is not None:
            conds.append(CREDENTIALS.c.profile == profile)
        if kind is not None:
            conds.append(CREDENTIALS.c.kind == kind)
        if companion_id is not None:
            conds.append(CREDENTIALS.c.companion_id == companion_id)
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                select(CREDENTIALS).where(*conds).order_by(CREDENTIALS.c.created_at.desc())
            )).all()
        return [credential_json(r) for r in rows]

    async def revoke_credential(self, credential_id: str, *, profile: str | None = None,
                                kind: str | None = None) -> dict[str, Any] | None:
        """Revoke one credential, scoped to ``profile``/``kind`` when given.
        Returns the row, or ``None`` when it is not in scope."""
        conds = [CREDENTIALS.c.id == credential_id]
        if profile is not None:
            conds.append(CREDENTIALS.c.profile == profile)
        if kind is not None:
            conds.append(CREDENTIALS.c.kind == kind)
        async with self.engine.begin() as conn:
            row = (await conn.execute(select(CREDENTIALS).where(*conds))).first()
            if row is None:
                return None
            if row.revoked_at is None:
                await conn.execute(update(CREDENTIALS).where(CREDENTIALS.c.id == credential_id)
                                   .values(revoked_at=now_ms()))
            row = (await conn.execute(select(CREDENTIALS).where(CREDENTIALS.c.id == credential_id))).first()
        return credential_json(row)

    async def rotate_hardware_credential(self, companion_id: str, row: dict[str, Any]) -> list[str]:
        """Revoke the companion's live hardware credentials and insert ``row``."""
        now = now_ms()
        async with self.engine.begin() as conn:
            old = (await conn.execute(select(CREDENTIALS.c.id).where(
                CREDENTIALS.c.companion_id == companion_id,
                CREDENTIALS.c.kind == "hardware",
                CREDENTIALS.c.revoked_at.is_(None),
            ))).scalars().all()
            if old:
                await conn.execute(update(CREDENTIALS).where(CREDENTIALS.c.id.in_(old)).values(revoked_at=now))
            await conn.execute(insert(CREDENTIALS), [{
                "created_at": now, "last_used_at": None, "revoked_at": None,
                **row, "companion_id": companion_id,
            }])
        return list(old)

    async def touch_credential(self, credential_id: str, when: float) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(update(CREDENTIALS).where(CREDENTIALS.c.id == credential_id)
                               .values(last_used_at=when))

    # ── devices ──

    async def list_devices(self, *, owner: str | None = None, companion_id: str | None = None,
                           kind: str | None = None, include_private: bool = True) -> list[dict[str, Any]]:
        conds = []
        if owner is not None:
            conds.append(DEVICES.c.owner_profile == owner)
        if companion_id is not None:
            conds.append(DEVICES.c.companion_id == companion_id)
        if kind is not None:
            conds.append(DEVICES.c.kind == kind)
        if not include_private:
            conds.append(DEVICES.c.companion_id.in_(
                select(COMPANIONS.c.id).where(COMPANIONS.c.mode != "private")))
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                select(DEVICES).where(*conds).order_by(DEVICES.c.kind.asc(), DEVICES.c.created_at.asc())
            )).all()
        return [device_json(r) for r in rows]

    async def get_device(self, device_id: str) -> dict[str, Any] | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(DEVICES).where(DEVICES.c.id == device_id))).first()
        return device_json(row) if row is not None else None

    async def rename_device(self, device_id: str, name: str, *, owner: str | None = None) -> dict[str, Any] | None:
        """Rename one device. With ``owner``, only a TAG that profile owns —
        the condition is part of the UPDATE, so nothing else is ever written."""
        conds = [DEVICES.c.id == device_id]
        if owner is not None:
            conds += [DEVICES.c.owner_profile == owner, DEVICES.c.kind == "tag"]
        async with self.engine.begin() as conn:
            result = await conn.execute(update(DEVICES).where(*conds).values(name=name, updated_at=now_ms()))
            if not result.rowcount:
                return None
            row = (await conn.execute(select(DEVICES).where(DEVICES.c.id == device_id))).first()
        return device_json(row)

    async def delete_device(self, device_id: str) -> tuple[dict[str, Any] | None, str | None]:
        """Forget a device. A tag a profile still owns is refused
        (``tag_owned``): forgetting it would skip the release — no epoch bump,
        no ``clear_tag`` — and leave the owner's last screen up. Returns
        ``(device, None)``, ``(None, "not_found")`` or ``(device, "tag_owned")``."""
        async with self.engine.begin() as conn:
            # A bridge's tags lose their bridge (FK SET NULL): lock them before
            # the bridge row, the order claim / assign use.
            await begin_write(conn)
            await conn.execute(select(DEVICES.c.id).where(
                DEVICES.c.bridge_device_id == device_id,
            ).order_by(DEVICES.c.id).with_for_update())
            result = await conn.execute(delete(DEVICES).where(
                DEVICES.c.id == device_id,
                or_(DEVICES.c.kind != "tag", DEVICES.c.owner_profile.is_(None)),
            ).returning(*DEVICES.c))
            gone = result.first()
            if gone is not None:
                return device_json(gone), None
            row = (await conn.execute(select(DEVICES).where(DEVICES.c.id == device_id))).first()
        if row is None:
            return None, "not_found"
        return device_json(row), "tag_owned"

    async def upsert_inventory(self, companion_id: str, body: dict[str, Any]) -> list[dict[str, Any]]:
        """Upsert the gateways, bridges and tags a companion reports. Devices it
        no longer reports are kept (a sleeping tag is not a removed tag).

        A tag may carry ``epoch``: the highest assignment epoch the companion
        has used for it or learned from the tag (its handshake reports
        ``stored_epoch``). The stored epoch never goes below it — a tag that was
        forgotten and re-reported, or whose epoch a restore rewound, would
        otherwise be assigned an epoch the tag refuses (``STALE_EPOCH``). When
        the report is ahead of an epoch Cremind still has work queued under
        (an owned tag's assignment, a pending clear), that work is re-queued
        one epoch above the report, exactly as ``assign`` / ``release`` would.

        A gateway reports ``tag_links`` (> 0: it serves tags on its own radio;
        0: it does not; absent: unknown, the last known value stays) with
        ``max_tags`` / ``assigned`` for that radio; see :func:`_inventory_values`."""
        now = now_ms()
        wanted: list[tuple[str, str, dict[str, Any]]] = []
        for kind, key, id_field in (("tag", "tags", "tag_id"), ("bridge", "bridges", "hw_id"),
                                    ("gateway", "gateways", "hw_id")):
            items = body.get(key) if isinstance(body.get(key), list) else []
            for item in items[:500]:
                if not isinstance(item, dict):
                    continue
                hw_id = str(item.get(id_field) or "").strip()
                if not hw_id or len(hw_id) > 64:
                    continue
                wanted.append((kind, hw_id, item))
        async with self.engine.begin() as conn:
            await begin_write(conn)
            # The companion row first, as a heartbeat takes it.
            await conn.execute(update(COMPANIONS).where(COMPANIONS.c.id == companion_id)
                               .values(last_seen_at=now, updated_at=now))
            private = (await conn.execute(select(COMPANIONS.c.mode).where(
                COMPANIONS.c.id == companion_id))).scalar() == "private"
            existing = {
                (r.kind, r.hw_id): r for r in (await conn.execute(
                    select(DEVICES).where(DEVICES.c.companion_id == companion_id)
                )).all()
            }
            # An epoch raise on an owned tag moves that owner's queued cards:
            # take those owners' stream locks first (sorted), before any
            # device row, in the order every content writer uses.
            ahead_owners = sorted({
                row.owner_profile for kind, hw_id, item in wanted
                if kind == "tag" and (row := existing.get((kind, hw_id))) is not None
                and row.owner_profile and row.status != "pairing"
                and (_reported_epoch(item) or 0) > int(row.epoch or 0)
            })
            for owner in ahead_owners:
                await lock_stream(conn, owner, now)
            # Then device rows in one order — tags, then bridges, each by id —
            # as claim / assign lock a tag, then its bridge.
            rank = {"tag": 0, "bridge": 1, "gateway": 2}
            wanted.sort(key=lambda w: (rank[w[0]], getattr(existing.get((w[0], w[1])), "id", "")))
            requeued = False
            for kind, hw_id, item in wanted:
                values = _inventory_values(kind, item)
                reported = _reported_epoch(item) if kind == "tag" else None
                row = existing.get((kind, hw_id))
                if row is None and private:
                    # A private worker's devices come from pairing only: an
                    # inventory report or an open USB port never adds one.
                    continue
                if row is None:
                    await conn.execute(insert(DEVICES), [{
                        "id": str(uuid.uuid4()), "companion_id": companion_id, "kind": kind,
                        "hw_id": hw_id, "name": "", "owner_profile": None, "bridge_device_id": None,
                        "epoch": reported or 0, "rotation": 0,
                        "status": "unclaimed" if kind == "tag" else "ok",
                        "desired_revision": 0, "displayed_revision": 0, "clear_required": False,
                        "created_at": now, "updated_at": now, **values,
                    }])
                    continue
                info = dict(row.info or {})
                info.update(values.pop("info", {}) or {})
                # A tag still pairing (a pair or import operation runs) is at the
                # epoch its operation assigned, which the operation records when
                # it finishes: raising it here would queue an assignment above it
                # that cancels the operation's own CLEAR.
                if reported is not None and reported > int(row.epoch or 0) and row.status != "pairing":
                    locked = (await conn.execute(
                        select(DEVICES).where(DEVICES.c.id == row.id).with_for_update()
                    )).first()
                    # An owner that changed since the first read has no stream
                    # lock here: leave the raise to the companion's next report.
                    owner_locked = locked is not None and (
                        not locked.owner_profile or locked.owner_profile in ahead_owners)
                    if owner_locked and reported > int(locked.epoch or 0):
                        values["epoch"] = await _raise_epoch(conn, locked, reported, now)
                        requeued = requeued or values["epoch"] != reported
                await conn.execute(update(DEVICES).where(DEVICES.c.id == row.id)
                                   .values(**values, info=info, updated_at=now))
            rows = (await conn.execute(
                select(DEVICES).where(DEVICES.c.companion_id == companion_id)
                .order_by(DEVICES.c.kind.asc(), DEVICES.c.created_at.asc())
            )).all()
        if requeued:
            notify_commands([companion_id])
        return [device_json(r) for r in rows]

    # ── deliveries ──

    async def list_deliveries(self, profile: str, *, device_id: str | None = None,
                              state: str | None = None, limit: int = 50,
                              before: int | None = None) -> list[dict[str, Any]]:
        conds = [DELIVERIES.c.profile == profile]
        if device_id:
            conds.append(DELIVERIES.c.tag_device_id == device_id)
        if state == "active":
            conds.append(DELIVERIES.c.stage.in_(ACTIVE_STAGES))
        elif state == "terminal":
            conds.append(DELIVERIES.c.stage.in_(TERMINAL_STAGES))
        elif state:
            conds.append(DELIVERIES.c.stage == state)
        if before is not None:
            conds.append(DELIVERIES.c.id < before)
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                select(DELIVERIES).where(*conds).order_by(DELIVERIES.c.id.desc()).limit(limit)
            )).all()
        return [delivery_json(r) for r in rows]

    async def get_delivery(self, delivery_id: int, *, profile: str | None = None) -> dict[str, Any] | None:
        conds = [DELIVERIES.c.id == delivery_id]
        if profile is not None:
            conds.append(DELIVERIES.c.profile == profile)
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(DELIVERIES).where(*conds))).first()
        return delivery_json(row) if row is not None else None

    async def cancel_delivery(self, profile: str, delivery_id: int
                              ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
        """Cancel an active delivery and tell the companion.

        The row moves to ``cancelled`` with a compare-and-set on its stage, so
        a card that was displayed (or expired) a moment earlier stays so. The
        companion may already hold the card, so the cancel is sent on as a
        ``resolved`` job naming the card's ``replace_key`` (every card has one;
        see :func:`write_deliveries`) — the job kind a companion already
        handles. Returns ``(cancelled, resolved_job_row | None, problem)``
        with problem ``not_found`` / ``already_terminal``."""
        now = now_ms()
        async with self.engine.begin() as conn:
            await lock_stream(conn, profile, now)
            row = (await conn.execute(select(DELIVERIES).where(
                DELIVERIES.c.id == delivery_id, DELIVERIES.c.profile == profile,
            ))).first()
            if row is None:
                return None, None, "not_found"
            if row.stage in TERMINAL_STAGES:
                return delivery_json(row), None, "already_terminal"
            times = dict(row.stage_times or {})
            times["cancelled"] = now
            changed = await cas_stage(conn, delivery_id, ACTIVE_STAGES, {
                "stage": "cancelled", "outcome": "cancelled", "detail": "cancelled by the user",
                "stage_times": times, "finished_at": now, "updated_at": now,
            })
            row = (await conn.execute(select(DELIVERIES).where(DELIVERIES.c.id == delivery_id))).first()
            if not changed:
                return delivery_json(row), None, "already_terminal"
            resolved = None
            device = (await conn.execute(select(DEVICES).where(DEVICES.c.id == row.tag_device_id))).first()
            if (device is not None and device.owner_profile == profile and row.replace_key
                    and int(device.epoch or 0) == int(row.epoch or 0) and row.kind != "clear"):
                spec = CardSpec(
                    kind="resolved",
                    card=make_card("resolved", title="Cancelled", icon="info", ts_ms=now,
                                   source_type="delivery", source_id=str(delivery_id)),
                    priority=90,
                    expires_at=max(float(row.expires_at), now + 3_600_000.0),
                    resolves=row.replace_key,
                )
                resolved = (await write_deliveries(conn, profile, [(device_json(device), spec, None)], now))[0]
        return delivery_json(row), (delivery_json(resolved) if resolved else None), None

    async def pending_counts(self, device_ids: Sequence[str]) -> dict[str, int]:
        """Active deliveries per device (the overview's ``pending_count``)."""
        if not device_ids:
            return {}
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                select(DELIVERIES.c.tag_device_id, func.count())
                .where(DELIVERIES.c.tag_device_id.in_(list(device_ids)),
                       DELIVERIES.c.stage.in_(ACTIVE_STAGES))
                .group_by(DELIVERIES.c.tag_device_id)
            )).all()
        return {r[0]: int(r[1]) for r in rows}

    async def delivery_counts(self, profile: str) -> dict[str, int]:
        now = now_ms()
        async with self.engine.connect() as conn:
            active = (await conn.execute(select(func.count()).select_from(DELIVERIES).where(
                DELIVERIES.c.profile == profile, DELIVERIES.c.stage.in_(ACTIVE_STAGES),
            ))).scalar_one()
            needs = (await conn.execute(select(func.count()).select_from(DELIVERIES).where(
                DELIVERIES.c.profile == profile, DELIVERIES.c.kind == "needs_input",
                DELIVERIES.c.stage.notin_(GONE_STAGES), DELIVERIES.c.expires_at > now,
            ))).scalar_one()
            failed = (await conn.execute(select(func.count()).select_from(DELIVERIES).where(
                DELIVERIES.c.profile == profile, DELIVERIES.c.stage.in_(("failed", "uncertain")),
                DELIVERIES.c.updated_at > now - 86_400_000,
            ))).scalar_one()
        return {"active_deliveries": int(active or 0), "needs_input": int(needs or 0),
                "failed_24h": int(failed or 0)}

    # ── connector: content ──

    async def jobs_after(self, profile: str, companion_id: str, after: int,
                         limit: int) -> dict[str, Any]:
        """One page of jobs for ``(profile, companion)`` with ``seq > after``.

        Reads the committed head first and pages only up to it, so a page
        never skips a lower seq still being committed. Returns ``{"stream_id",
        "head_seq", "pruned_through", "jobs", "next_after"}``."""
        async with self.engine.connect() as conn:
            stream = (await conn.execute(select(STREAMS).where(STREAMS.c.profile == profile))).first()
            head = int(stream.next_delivery_seq) if stream is not None else 0
            pruned = int(((stream.state or {}) if stream is not None else {}).get("pruned_through_seq") or 0)
            rows = (await conn.execute(
                select(DELIVERIES, DEVICES.c.hw_id, DEVICES.c.owner_profile.label("device_owner"),
                       DEVICES.c.epoch.label("device_epoch"))
                .join(DEVICES, DEVICES.c.id == DELIVERIES.c.tag_device_id)
                .where(
                    DELIVERIES.c.profile == profile,
                    DELIVERIES.c.companion_id == companion_id,
                    DELIVERIES.c.seq > after,
                    DELIVERIES.c.seq <= head,
                )
                .order_by(DELIVERIES.c.seq.asc())
                .limit(limit)
            )).all()
        # The cursor advances over every row; a job is SERVED only for a tag
        # this profile still owns, and a live one only at the tag's epoch.
        jobs = [job_json(r, r.hw_id) for r in rows if _servable(r, profile)]
        next_after = int(rows[-1].seq) if len(rows) >= limit else max(head, after)
        return {
            "stream_id": stream.stream_id if stream is not None else None,
            "head_seq": head,
            "pruned_through": pruned,
            "jobs": jobs,
            "next_after": next_after,
        }

    async def outstanding_jobs(self, profile: str, companion_id: str) -> list[dict[str, Any]]:
        now = now_ms()
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                select(DELIVERIES, DEVICES.c.hw_id)
                .join(DEVICES, DEVICES.c.id == DELIVERIES.c.tag_device_id)
                .where(
                    DELIVERIES.c.profile == profile,
                    DELIVERIES.c.companion_id == companion_id,
                    DELIVERIES.c.stage.in_(ACTIVE_STAGES),
                    DELIVERIES.c.expires_at > now,
                    DEVICES.c.owner_profile == profile,
                    DELIVERIES.c.epoch == DEVICES.c.epoch,
                )
                .order_by(DELIVERIES.c.seq.asc())
            )).all()
        return [job_json(r, r.hw_id) for r in rows]

    async def accept(self, profile: str, companion_id: str, delivery_ids: Sequence[int]) -> int:
        """Move queued deliveries to ``companion_accepted``. Returns how many of
        ``delivery_ids`` belong to this profile and companion, on a tag the
        profile still owns at the delivery's epoch (idempotent)."""
        ids = sorted({int(i) for i in delivery_ids})
        if not ids:
            return 0
        now = now_ms()
        async with self.engine.begin() as conn:
            await begin_write(conn)
            rows = (await conn.execute(select(
                DELIVERIES.c.id, DELIVERIES.c.stage, DELIVERIES.c.stage_times,
            ).join(DEVICES, DEVICES.c.id == DELIVERIES.c.tag_device_id).where(
                DELIVERIES.c.id.in_(ids), DELIVERIES.c.profile == profile,
                DELIVERIES.c.companion_id == companion_id,
                DEVICES.c.owner_profile == profile, DELIVERIES.c.epoch == DEVICES.c.epoch,
            ))).all()
            for r in rows:
                if r.stage != "queued":
                    continue
                times = dict(r.stage_times or {})
                times.setdefault("companion_accepted", now)
                await conn.execute(update(DELIVERIES).where(
                    DELIVERIES.c.id == r.id, DELIVERIES.c.stage == "queued",
                ).values(stage="companion_accepted", stage_times=times, updated_at=now))
        return len(rows)

    async def apply_receipts(self, profile: str, companion_id: str,
                             receipts: Sequence[dict[str, Any]]) -> tuple[int, list[dict[str, Any]]]:
        """Apply delivery receipts. Idempotent: a stage never moves backwards
        and a terminal outcome is final — every write is a compare-and-set on
        the stage it read, so a concurrent cancel or expiry is never reopened.

        Returns ``(applied, rejected)``; each rejection is ``{"delivery_id",
        "reason"}`` with reason ``invalid`` (no delivery id), ``unknown`` (not
        this profile's delivery on this companion), ``not_owned`` (the tag has
        changed hands), ``epoch_mismatch`` (the receipt's or the delivery's
        epoch is not the tag's current one), ``terminal`` (already final with
        another outcome; a repeat of the same final receipt is a silent no-op)."""
        rejected: list[dict[str, Any]] = []
        ids = []
        for rec in receipts:
            did = _bounded(rec.get("delivery_id"), 1, 2 ** 63 - 1)
            if did is not None:
                ids.append(did)
        if not ids:
            return 0, [{"delivery_id": rec.get("delivery_id"), "reason": "invalid"} for rec in receipts]
        now = now_ms()
        applied = 0
        async with self.engine.begin() as conn:
            await begin_write(conn)
            rows = {
                int(r.id): dict(r._mapping) for r in (await conn.execute(
                    select(DELIVERIES, DEVICES.c.owner_profile.label("device_owner"),
                           DEVICES.c.epoch.label("device_epoch"))
                    .join(DEVICES, DEVICES.c.id == DELIVERIES.c.tag_device_id)
                    .where(
                        DELIVERIES.c.id.in_(sorted(set(ids))), DELIVERIES.c.profile == profile,
                        DELIVERIES.c.companion_id == companion_id,
                    )
                )).all()
            }
            touched_devices: dict[str, dict[str, Any]] = {}
            for rec in receipts:
                did = _bounded(rec.get("delivery_id"), 1, 2 ** 63 - 1)
                if did is None:
                    rejected.append({"delivery_id": rec.get("delivery_id"), "reason": "invalid"})
                    continue
                row = rows.get(did)
                if row is None:
                    rejected.append({"delivery_id": did, "reason": "unknown"})
                    continue
                if row["stage"] in TERMINAL_STAGES:
                    repeat = rec.get("outcome") == row["stage"] or rec.get("stage") == row["stage"]
                    if not repeat:
                        rejected.append({"delivery_id": did, "reason": "terminal"})
                    continue
                if row["device_owner"] != profile:
                    rejected.append({"delivery_id": did, "reason": "not_owned"})
                    continue
                epoch = rec.get("epoch")
                row_epoch = int(row["epoch"] or 0)
                if (row_epoch != int(row["device_epoch"] or 0) or (
                        isinstance(epoch, int) and not isinstance(epoch, bool) and epoch != row_epoch)):
                    rejected.append({"delivery_id": did, "reason": "epoch_mismatch"})
                    continue
                outcome = rec.get("outcome")
                stage = rec.get("stage")
                target = row["stage"]
                if outcome in TERMINAL_STAGES:
                    target = outcome
                elif stage in STAGE_RANK and STAGE_RANK[stage] > STAGE_RANK.get(row["stage"], -1):
                    target = stage
                values: dict[str, Any] = {}
                if target != row["stage"]:
                    values["stage"] = target
                    times = dict(row.get("stage_times") or {})
                    times.setdefault(target, parse_ts(rec.get("at")) or now)
                    values["stage_times"] = times
                    if target in TERMINAL_STAGES:
                        values["outcome"] = target
                        values["finished_at"] = now
                for field, lo, hi in (("status_code", *_INT32), ("revision", 0, MAX_EPOCH)):
                    v = _bounded(rec.get(field), lo, hi)
                    if v is not None and v != row.get(field):
                        values[field] = v
                if isinstance(rec.get("digest"), str) and rec["digest"][:64] != row.get("digest"):
                    values["digest"] = rec["digest"][:64]
                if isinstance(rec.get("detail"), str) and rec["detail"].strip():
                    from app.tags.sanitize import clean_text

                    values["detail"] = clean_text(rec["detail"], 500)
                if isinstance(rec.get("timing"), dict):
                    values["timing"] = _small(rec["timing"])
                if not values:
                    continue
                values["updated_at"] = now
                if not await cas_stage(conn, did, row["stage"], values):
                    # Someone else (a cancel, a claim, the expiry sweep) moved
                    # it first; their terminal state stands.
                    rejected.append({"delivery_id": did, "reason": "terminal"})
                    rows.pop(did, None)
                    continue
                row.update(values)
                applied += 1
                rev = values.get("revision", row.get("revision"))
                if isinstance(rev, int):
                    dev = touched_devices.setdefault(row["tag_device_id"], {"desired": 0, "displayed": None})
                    dev["desired"] = max(dev["desired"], rev)
                    if row["stage"] == "displayed":
                        if dev["displayed"] is None or rev >= dev["displayed"][0]:
                            dev["displayed"] = (rev, row.get("digest"))
            for device_id, dev in touched_devices.items():
                cur = (await conn.execute(select(
                    DEVICES.c.desired_revision, DEVICES.c.displayed_revision,
                ).where(DEVICES.c.id == device_id))).first()
                if cur is None:
                    continue
                values = {"updated_at": now}
                if dev["desired"] > int(cur.desired_revision or 0):
                    values["desired_revision"] = dev["desired"]
                if dev["displayed"] is not None and dev["displayed"][0] >= int(cur.displayed_revision or 0):
                    values["displayed_revision"] = dev["displayed"][0]
                    values["displayed_digest"] = dev["displayed"][1]
                await conn.execute(update(DEVICES).where(DEVICES.c.id == device_id).values(**values))
        return applied, rejected

    async def store_preview(self, companion_id: str, profile: str, tag_hw_id: Any, *, kind: str,
                            revision: int, epoch: int | None, png_base64: str,
                            delivery_ids: list[int]) -> tuple[bool, str | None]:
        """Keep the newest preview per (tag, kind), for the tag's CURRENT owner
        and epoch — checked in the transaction that writes, under a share lock
        on the device row, so a change of owner (which deletes the previews)
        cannot interleave. Revisions compare within one epoch only: a preview
        of an older epoch is replaced whatever its revision. Returns
        ``(stored, problem)``, problem ``tag_not_found`` / ``epoch_mismatch``."""
        now = now_ms()
        async with self.engine.begin() as conn:
            await begin_write(conn)
            device = (await conn.execute(select(DEVICES).where(
                DEVICES.c.companion_id == companion_id, DEVICES.c.kind == "tag",
                DEVICES.c.hw_id == str(tag_hw_id or ""),
            ).with_for_update(read=True))).first()
            if device is None or device.owner_profile != profile:
                return False, "tag_not_found"
            current = int(device.epoch or 0)
            if epoch is not None and int(epoch) != current:
                return False, "epoch_mismatch"
            row = (await conn.execute(select(PREVIEWS).where(
                PREVIEWS.c.tag_device_id == device.id, PREVIEWS.c.kind == kind,
            ))).first()
            if row is not None and int(row.epoch or 0) == current and int(row.revision or 0) > revision:
                return False, None
            if row is None:
                await conn.execute(insert(PREVIEWS), [{
                    "id": str(uuid.uuid4()), "tag_device_id": device.id, "kind": kind,
                    "revision": revision, "epoch": current, "png_base64": png_base64,
                    "delivery_ids": delivery_ids, "created_at": now,
                }])
            else:
                await conn.execute(update(PREVIEWS).where(PREVIEWS.c.id == row.id).values(
                    revision=revision, epoch=current, png_base64=png_base64,
                    delivery_ids=delivery_ids, created_at=now,
                ))
            if kind == "desired":
                await conn.execute(update(DEVICES).where(
                    DEVICES.c.id == device.id, DEVICES.c.desired_revision < revision,
                ).values(desired_revision=revision, updated_at=now))
        return True, None

    async def get_preview(self, device_id: str, kind: str) -> dict[str, Any] | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(PREVIEWS).where(
                PREVIEWS.c.tag_device_id == device_id, PREVIEWS.c.kind == kind,
            ))).first()
        return dict(row._mapping) if row is not None else None

    async def preview_revisions(self, device_ids: Sequence[str]) -> dict[str, dict[str, int]]:
        if not device_ids:
            return {}
        async with self.engine.connect() as conn:
            rows = (await conn.execute(select(
                PREVIEWS.c.tag_device_id, PREVIEWS.c.kind, PREVIEWS.c.revision,
            ).where(PREVIEWS.c.tag_device_id.in_(list(device_ids))))).all()
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r.tag_device_id, {})[r.kind] = int(r.revision or 0)
        return out

    # ── commands ──

    async def create_command(self, *, companion_id: str, kind: str, args: dict[str, Any],
                             requested_by: str, ttl_s: float, dedupe: bool = False) -> dict[str, Any]:
        """Queue a command. With ``dedupe`` (the profile-issued identify /
        refresh), an identical command still queued is returned instead of a
        second one, so repeated requests cannot flood the companion's queue."""
        now = now_ms()
        async with self.engine.begin() as conn:
            await begin_write(conn)
            if dedupe:
                for existing in (await conn.execute(select(COMMANDS).where(
                    COMMANDS.c.companion_id == companion_id, COMMANDS.c.kind == kind,
                    COMMANDS.c.status == "queued", COMMANDS.c.expires_at > now,
                ))).all():
                    if (existing.args or {}) == args:
                        return command_json(existing)
            row = await insert_command(conn, companion_id=companion_id, kind=kind, args=args,
                                       requested_by=requested_by, ttl_s=ttl_s, now=now)
        notify_commands([companion_id])
        return command_json(row)

    async def get_command(self, command_id: str, *, companion_id: str | None = None,
                          include_private: bool = True) -> dict[str, Any] | None:
        conds = [COMMANDS.c.id == command_id]
        if companion_id is not None:
            conds.append(COMMANDS.c.companion_id == companion_id)
        if not include_private:
            conds.append(COMMANDS.c.companion_id.in_(
                select(COMPANIONS.c.id).where(COMPANIONS.c.mode != "private")))
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(COMMANDS).where(*conds))).first()
        return command_json(row) if row is not None else None

    async def list_commands(self, *, companion_id: str | None = None, statuses: Sequence[str] | None = None,
                            limit: int = 100, include_private: bool = True) -> list[dict[str, Any]]:
        conds = []
        if companion_id is not None:
            conds.append(COMMANDS.c.companion_id == companion_id)
        if statuses:
            conds.append(COMMANDS.c.status.in_(list(statuses)))
        if not include_private:
            conds.append(COMMANDS.c.companion_id.in_(
                select(COMPANIONS.c.id).where(COMPANIONS.c.mode != "private")))
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                select(COMMANDS).where(*conds).order_by(COMMANDS.c.created_at.desc()).limit(limit)
            )).all()
        return [command_json(r) for r in rows]

    async def expire_commands(self, now: float | None = None) -> int:
        """Expire queued commands past ``expires_at`` and claimed ones past it
        plus :data:`CLAIMED_GRACE_MS` (the companion is working on them). A
        ``clear_tag`` that expires while its tag still waits is re-queued
        (bounded, see :func:`requeue_clear`)."""
        now = now or now_ms()
        async with self.engine.begin() as conn:
            await begin_write(conn)
            expired = (await conn.execute(update(COMMANDS).where(or_(
                (COMMANDS.c.status == "queued") & (COMMANDS.c.expires_at <= now),
                (COMMANDS.c.status == "claimed") & (COMMANDS.c.expires_at + CLAIMED_GRACE_MS <= now),
            )).values(status="expired", completed_at=now).returning(
                COMMANDS.c.companion_id, COMMANDS.c.kind, COMMANDS.c.args,
            ))).all()
            for cmd in expired:
                args = cmd.args if isinstance(cmd.args, dict) else {}
                if cmd.kind == "clear_tag" and args.get("tag_id"):
                    await requeue_clear(conn, cmd.companion_id, str(args["tag_id"]),
                                        int(args.get("epoch") or 0), now)
        if any(c.kind == "clear_tag" for c in expired):
            notify_commands([c.companion_id for c in expired])
        return len(expired)

    async def queued_commands(self, companion_id: str) -> list[dict[str, Any]]:
        """The companion's queue: ownership commands (``assign_tag`` /
        ``clear_tag``) first, then the rest, each oldest first."""
        now = now_ms()
        async with self.engine.connect() as conn:
            rows = (await conn.execute(select(COMMANDS).where(
                COMMANDS.c.companion_id == companion_id, COMMANDS.c.status == "queued",
                COMMANDS.c.expires_at > now,
            ).order_by(
                case((COMMANDS.c.kind.in_(("assign_tag", "clear_tag")), 0), else_=1),
                COMMANDS.c.created_at.asc(),
            ).limit(50))).all()
        return [dict(r._mapping) for r in rows]

    async def claim_command(self, companion_id: str, command_id: str) -> tuple[dict[str, Any] | None, str | None]:
        now = now_ms()
        async with self.engine.begin() as conn:
            result = await conn.execute(update(COMMANDS).where(
                COMMANDS.c.id == command_id, COMMANDS.c.companion_id == companion_id,
                COMMANDS.c.status == "queued", COMMANDS.c.expires_at > now,
            ).values(status="claimed", claimed_at=now))
            row = (await conn.execute(select(COMMANDS).where(
                COMMANDS.c.id == command_id, COMMANDS.c.companion_id == companion_id,
            ))).first()
        if row is None:
            return None, "not_found"
        if not result.rowcount:
            return dict(row._mapping), "already_claimed"
        return dict(row._mapping), None

    async def complete_command(self, companion_id: str, command_id: str, *, status: str,
                               result: dict[str, Any] | None, error: str | None
                               ) -> tuple[dict[str, Any] | None, str | None]:
        """Record a command's result. Same status twice is a no-op; a
        different terminal status is ``conflict`` — except that a command
        Cremind merely stopped waiting for (``expired``) still takes a late
        result: the companion did the work.

        ``clear_tag``: a success at the tag's current epoch lifts
        ``clear_required``, and in the same transaction the owner's content
        that was held meanwhile — the open cards (needs-input, health,
        periodic, diagnostics) and whatever arrived since the claim — is
        delivered to the tag, and the owner's periodic/diagnostics state is
        reset so it is sent afresh. A failure while the tag still waits
        re-queues the clear (bounded; then the device reads ``clear_failed``).

        ``assign_tag``: see :func:`_settle_assign`. A failed command's
        ``error`` defaults to ``result.error`` (``{"error": "bridge_full",
        "max_tags": n}``).
        Locks: the owner's stream row, then the device, then the command."""
        now = now_ms()
        lifted: list[str] = []
        async with self.engine.begin() as conn:
            await begin_write(conn)
            peek = (await conn.execute(select(COMMANDS).where(
                COMMANDS.c.id == command_id, COMMANDS.c.companion_id == companion_id,
            ))).first()
            if peek is None:
                return None, "not_found"
            args = peek.args if isinstance(peek.args, dict) else {}
            tag_id = str(args.get("tag_id") or "")
            epoch = int(args.get("epoch") or 0)
            device = None
            if peek.kind in ("clear_tag", "assign_tag") and tag_id:
                found = (await conn.execute(select(DEVICES.c.id).where(
                    DEVICES.c.companion_id == companion_id, DEVICES.c.kind == "tag",
                    DEVICES.c.hw_id == tag_id,
                ))).scalar_one_or_none()
                # The same order as claim / assign / release: owner's stream,
                # device, then command rows.
                device = await lock_device(conn, found, now) if found else None
            row = (await conn.execute(select(COMMANDS).where(
                COMMANDS.c.id == command_id).with_for_update())).first()
            late = row.status == "expired"
            if row.status in COMMAND_TERMINAL and not late:
                return dict(row._mapping), (None if row.status == status else "conflict")
            if not error and status == "failed" and isinstance(result, dict) and isinstance(result.get("error"), str):
                error = result["error"]
            await conn.execute(update(COMMANDS).where(COMMANDS.c.id == command_id).values(
                status=status, result=_small(result) if result else None,
                error=(error or None) and str(error)[:1000], completed_at=now,
            ))
            if peek.kind == "assign_tag":
                if device is not None:
                    await _settle_assign(conn, device, args, epoch, status=status, error=error,
                                         result=result, now=now)
            elif device is not None and status == "succeeded":
                cleared = (await conn.execute(update(DEVICES).where(
                    DEVICES.c.id == device.id, DEVICES.c.epoch == epoch,
                    DEVICES.c.clear_required.is_(True),
                ).values(clear_required=False, updated_at=now,
                         status=case((DEVICES.c.status == "clear_failed", "assigning"),
                                     else_=DEVICES.c.status))
                .returning(*DEVICES.c))).first()
                if cleared is not None and cleared.owner_profile and cleared.owner_profile == device.owner_profile:
                    from app.tags.backfill import backfill_tag

                    await merge_stream_state(conn, cleared.owner_profile,
                                             lambda st: reset_content_state(st, cleared.id))
                    await backfill_tag(conn, cleared.owner_profile, device_json(cleared), now,
                                       since_ms=float(cleared.claimed_at or 0))
                    lifted.append(cleared.owner_profile)
            elif device is not None and status == "failed":
                await requeue_clear(conn, companion_id, tag_id, epoch, now)
            row = (await conn.execute(select(COMMANDS).where(COMMANDS.c.id == command_id))).first()
        if device is not None and peek.kind == "clear_tag":
            notify_commands([companion_id])
        if lifted:
            journal.wake()
        return dict(row._mapping), None


def _small(value: Any, depth: int = 0) -> Any:
    """A JSON-safe, bounded copy of companion-supplied data."""
    if depth > 4:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, dict):
        return {str(k)[:64]: _small(v, depth + 1) for k, v in list(value.items())[:50]}
    if isinstance(value, (list, tuple)):
        return [_small(v, depth + 1) for v in list(value)[:50]]
    return str(value)[:500]


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


_INT32 = (-(2 ** 31), 2 ** 31 - 1)


def _bounded(value: Any, lo: int, hi: int) -> int | None:
    """An int within ``[lo, hi]`` or ``None`` — companion data never reaches a
    column it could overflow (one bad field must not fail a whole report)."""
    number = _int(value)
    if number is None or not lo <= number <= hi:
        return None
    return number


def _inventory_values(kind: str, item: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if isinstance(item.get("fw"), str):
        values["fw"] = item["fw"][:32]
    if _bounded(item.get("board"), 0, 65535) is not None:
        values["board"] = item["board"]
    info: dict[str, Any] = {}
    if kind == "gateway":
        for key in ("boot_id", "port"):
            if key in item:
                info[key] = _small(item[key])
        # Tags on the gateway's own radio (protocol.md §11): ``tag_links`` > 0
        # serves them, with ``max_tags`` / ``assigned`` like a bridge's table;
        # an explicit ``tag_links`` 0 serves none. Absent (the worker has not
        # talked to the gateway yet, e.g. right after a restart) or a bad value
        # keeps the last known one — a gateway that never reported serves none.
        for key, lo in (("tag_links", 0), ("max_tags", 1), ("assigned", 0)):
            if _bounded(item.get(key), lo, 255) is not None:
                info[key] = item[key]
    elif kind == "bridge":
        for key in ("addr", "fontpack_id", "flash_size"):
            if key in item:
                info[key] = _small(item[key])
        # Assignment-table capacity (nRF52832: 10, nRF52840: 20) and the
        # bridge's own count; a bad value is dropped, the last good one kept.
        for key, lo in (("max_tags", 1), ("assigned", 0)):
            if _bounded(item.get(key), lo, 255) is not None:
                info[key] = item[key]
    else:
        for key in ("panel", "width", "height", "planes"):
            if _bounded(item.get(key), 0, 65535) is not None:
                values[key] = item[key]
    values["info"] = info
    return values


_instance: TagStorage | None = None


def get_tag_storage(provider: DatabaseProvider | None = None) -> TagStorage:
    global _instance
    if _instance is None:
        _instance = TagStorage(provider)
    return _instance


__all__ = [
    "ACTIVE_STAGES", "COMMAND_ACTIVE", "COMMAND_TERMINAL", "GONE_STAGES", "STAGES",
    "TERMINAL_STAGES", "TagStorage", "allocate_delivery_ids", "allocate_delivery_seqs",
    "cancel_device_deliveries", "cancel_tag_commands", "command_event", "command_json",
    "companion_json", "connector_command_json", "credential_json", "delivery_json",
    "device_json", "get_tag_storage", "insert_command", "job_json", "notify_commands",
    "now_ms", "open_key_devices", "parse_ts", "write_deliveries",
]
