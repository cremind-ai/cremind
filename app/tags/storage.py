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
from typing import Any, Iterable, Sequence

from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.databases import DatabaseProvider, get_database_provider
from app.storage.models import (
    TagCommandModel, TagCompanionModel, TagCounterModel, TagCredentialModel,
    TagDeliveryModel, TagDeviceModel, TagPreviewModel, TagSettingsModel,
    TagStreamModel,
)
from app.tags import journal
from app.tags.cards import CardSpec, iso

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

ONLINE_WINDOW_MS = 120_000


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
    delivery rows -> counter row, so none of them can deadlock another on
    PostgreSQL (SQLite has one writer anyway)."""
    stmt = update(STREAMS).where(STREAMS.c.profile == profile).values(updated_at=now)
    if not (await conn.execute(stmt)).rowcount:
        await ensure_stream(conn, profile, now)
        await conn.execute(stmt)


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
                            options: dict[str, Any] | None = None, replace_options: bool = False) -> dict[str, Any]:
        """Upsert a profile's settings. ``options`` replaces the stored
        overrides when ``replace_options``; ``None`` leaves them. Enabling
        makes sure the profile's stream row exists."""
        now = now_ms()
        async with self.engine.begin() as conn:
            row = (await conn.execute(
                select(SETTINGS).where(SETTINGS.c.profile == profile)
            )).first()
            new_enabled = bool(row.enabled) if row is not None else False
            if enabled is not None:
                new_enabled = bool(enabled)
            new_options = (row.options or {}) if row is not None else {}
            if replace_options:
                new_options = dict(options or {})
            if row is None:
                await conn.execute(insert(SETTINGS), [{
                    "profile": profile, "enabled": new_enabled,
                    "options": new_options, "updated_at": now,
                }])
            else:
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

    async def list_companions(self) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(select(COMPANIONS).order_by(COMPANIONS.c.created_at.asc()))).all()
        now = now_ms()
        return [companion_json(r, now=now) for r in rows]

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
            for item in devices[:500]:
                if not isinstance(item, dict):
                    continue
                hw_id = str(item.get("hw_id") or "").strip()
                kind = str(item.get("kind") or "tag").strip()
                row = existing.get((kind, hw_id))
                if row is None:
                    continue
                values: dict[str, Any] = {"updated_at": now}
                if isinstance(item.get("battery_mv"), int) and not isinstance(item.get("battery_mv"), bool):
                    values["battery_mv"] = item["battery_mv"]
                if isinstance(item.get("rssi"), int) and not isinstance(item.get("rssi"), bool):
                    values["rssi"] = item["rssi"]
                contact = parse_ts(item.get("last_contact_at"))
                if contact is not None:
                    values["last_contact_at"] = contact
                rev = item.get("displayed_revision")
                if isinstance(rev, int) and not isinstance(rev, bool) and rev >= int(row.displayed_revision or 0):
                    values["displayed_revision"] = rev
                    if isinstance(item.get("displayed_digest"), str):
                        values["displayed_digest"] = item["displayed_digest"][:64]
                status = item.get("status")
                if status in ("ok", "pending", "offline", "error") and (kind != "tag" or row.owner_profile):
                    values["status"] = status
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
                           kind: str | None = None) -> list[dict[str, Any]]:
        conds = []
        if owner is not None:
            conds.append(DEVICES.c.owner_profile == owner)
        if companion_id is not None:
            conds.append(DEVICES.c.companion_id == companion_id)
        if kind is not None:
            conds.append(DEVICES.c.kind == kind)
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
        conds = [DEVICES.c.id == device_id]
        if owner is not None:
            conds.append(DEVICES.c.owner_profile == owner)
        async with self.engine.begin() as conn:
            result = await conn.execute(update(DEVICES).where(*conds).values(name=name, updated_at=now_ms()))
            if not result.rowcount:
                return None
            row = (await conn.execute(select(DEVICES).where(DEVICES.c.id == device_id))).first()
        return device_json(row)

    async def delete_device(self, device_id: str) -> dict[str, Any] | None:
        async with self.engine.begin() as conn:
            row = (await conn.execute(select(DEVICES).where(DEVICES.c.id == device_id))).first()
            if row is None:
                return None
            await conn.execute(delete(DEVICES).where(DEVICES.c.id == device_id))
        return device_json(row)

    async def upsert_inventory(self, companion_id: str, body: dict[str, Any]) -> list[dict[str, Any]]:
        """Upsert the gateways, bridges and tags a companion reports. Devices it
        no longer reports are kept (a sleeping tag is not a removed tag)."""
        now = now_ms()
        wanted: list[tuple[str, str, dict[str, Any]]] = []
        for kind, key, id_field in (("gateway", "gateways", "hw_id"), ("bridge", "bridges", "hw_id"),
                                    ("tag", "tags", "tag_id")):
            items = body.get(key) if isinstance(body.get(key), list) else []
            for item in items[:500]:
                if not isinstance(item, dict):
                    continue
                hw_id = str(item.get(id_field) or "").strip()
                if not hw_id or len(hw_id) > 64:
                    continue
                wanted.append((kind, hw_id, item))
        async with self.engine.begin() as conn:
            existing = {
                (r.kind, r.hw_id): r for r in (await conn.execute(
                    select(DEVICES).where(DEVICES.c.companion_id == companion_id)
                )).all()
            }
            for kind, hw_id, item in wanted:
                values = _inventory_values(kind, item)
                row = existing.get((kind, hw_id))
                if row is None:
                    await conn.execute(insert(DEVICES), [{
                        "id": str(uuid.uuid4()), "companion_id": companion_id, "kind": kind,
                        "hw_id": hw_id, "name": "", "owner_profile": None, "bridge_device_id": None,
                        "epoch": 0, "rotation": 0, "status": "unclaimed" if kind == "tag" else "ok",
                        "desired_revision": 0, "displayed_revision": 0, "clear_required": False,
                        "created_at": now, "updated_at": now, **values,
                    }])
                else:
                    info = dict(row.info or {})
                    info.update(values.pop("info", {}) or {})
                    await conn.execute(update(DEVICES).where(DEVICES.c.id == row.id)
                                       .values(**values, info=info, updated_at=now))
            await conn.execute(update(COMPANIONS).where(COMPANIONS.c.id == companion_id)
                               .values(last_seen_at=now, updated_at=now))
            rows = (await conn.execute(
                select(DEVICES).where(DEVICES.c.companion_id == companion_id)
                .order_by(DEVICES.c.kind.asc(), DEVICES.c.created_at.asc())
            )).all()
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

    async def cancel_delivery(self, profile: str, delivery_id: int) -> tuple[dict[str, Any] | None, str | None]:
        """Cancel an active delivery. Returns ``(row, None)``, ``(None,
        "not_found")`` or ``(row, "already_terminal")``."""
        now = now_ms()
        async with self.engine.begin() as conn:
            row = (await conn.execute(select(DELIVERIES).where(
                DELIVERIES.c.id == delivery_id, DELIVERIES.c.profile == profile,
            ))).first()
            if row is None:
                return None, "not_found"
            if row.stage in TERMINAL_STAGES:
                return delivery_json(row), "already_terminal"
            times = dict(row.stage_times or {})
            times["cancelled"] = now
            await conn.execute(update(DELIVERIES).where(DELIVERIES.c.id == delivery_id).values(
                stage="cancelled", outcome="cancelled", detail="cancelled by the user",
                stage_times=times, finished_at=now, updated_at=now,
            ))
            row = (await conn.execute(select(DELIVERIES).where(DELIVERIES.c.id == delivery_id))).first()
        return delivery_json(row), None

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
                select(DELIVERIES, DEVICES.c.hw_id)
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
        jobs = [job_json(r, r.hw_id) for r in rows]
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
                )
                .order_by(DELIVERIES.c.seq.asc())
            )).all()
        return [job_json(r, r.hw_id) for r in rows]

    async def accept(self, profile: str, companion_id: str, delivery_ids: Sequence[int]) -> int:
        """Move queued deliveries to ``companion_accepted``. Returns how many of
        ``delivery_ids`` belong to this profile and companion (idempotent)."""
        ids = sorted({int(i) for i in delivery_ids})
        if not ids:
            return 0
        now = now_ms()
        async with self.engine.begin() as conn:
            rows = (await conn.execute(select(
                DELIVERIES.c.id, DELIVERIES.c.stage, DELIVERIES.c.stage_times,
            ).where(
                DELIVERIES.c.id.in_(ids), DELIVERIES.c.profile == profile,
                DELIVERIES.c.companion_id == companion_id,
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
                             receipts: Sequence[dict[str, Any]]) -> int:
        """Apply delivery receipts. Idempotent: a stage never moves backwards
        and a terminal outcome is final. Returns how many changed something."""
        ids = []
        for rec in receipts:
            try:
                ids.append(int(rec["delivery_id"]))
            except (KeyError, TypeError, ValueError):
                continue
        if not ids:
            return 0
        now = now_ms()
        applied = 0
        async with self.engine.begin() as conn:
            rows = {
                int(r.id): dict(r._mapping) for r in (await conn.execute(select(DELIVERIES).where(
                    DELIVERIES.c.id.in_(sorted(set(ids))), DELIVERIES.c.profile == profile,
                    DELIVERIES.c.companion_id == companion_id,
                ))).all()
            }
            touched_devices: dict[str, dict[str, Any]] = {}
            for rec in receipts:
                try:
                    did = int(rec["delivery_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                row = rows.get(did)
                if row is None or row["stage"] in TERMINAL_STAGES:
                    continue
                epoch = rec.get("epoch")
                if isinstance(epoch, int) and not isinstance(epoch, bool) and epoch != int(row["epoch"] or 0):
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
                for field in ("status_code", "revision"):
                    v = rec.get(field)
                    if isinstance(v, int) and not isinstance(v, bool) and v != row.get(field):
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
                await conn.execute(update(DELIVERIES).where(DELIVERIES.c.id == did).values(**values))
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
        return applied

    async def store_preview(self, device_id: str, *, kind: str, revision: int, png_base64: str,
                            delivery_ids: list[int]) -> bool:
        """Keep the newest preview per (tag, kind); an older revision is ignored."""
        now = now_ms()
        async with self.engine.begin() as conn:
            row = (await conn.execute(select(PREVIEWS).where(
                PREVIEWS.c.tag_device_id == device_id, PREVIEWS.c.kind == kind,
            ))).first()
            if row is not None and int(row.revision or 0) > revision:
                return False
            if row is None:
                await conn.execute(insert(PREVIEWS), [{
                    "id": str(uuid.uuid4()), "tag_device_id": device_id, "kind": kind,
                    "revision": revision, "png_base64": png_base64,
                    "delivery_ids": delivery_ids, "created_at": now,
                }])
            else:
                await conn.execute(update(PREVIEWS).where(PREVIEWS.c.id == row.id).values(
                    revision=revision, png_base64=png_base64, delivery_ids=delivery_ids, created_at=now,
                ))
            if kind == "desired":
                await conn.execute(update(DEVICES).where(
                    DEVICES.c.id == device_id, DEVICES.c.desired_revision < revision,
                ).values(desired_revision=revision, updated_at=now))
        return True

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
                             requested_by: str, ttl_s: float) -> dict[str, Any]:
        async with self.engine.begin() as conn:
            row = await insert_command(conn, companion_id=companion_id, kind=kind, args=args,
                                       requested_by=requested_by, ttl_s=ttl_s, now=now_ms())
        notify_commands([companion_id])
        return command_json(row)

    async def get_command(self, command_id: str, *, companion_id: str | None = None) -> dict[str, Any] | None:
        conds = [COMMANDS.c.id == command_id]
        if companion_id is not None:
            conds.append(COMMANDS.c.companion_id == companion_id)
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(COMMANDS).where(*conds))).first()
        return command_json(row) if row is not None else None

    async def list_commands(self, *, companion_id: str | None = None, statuses: Sequence[str] | None = None,
                            limit: int = 100) -> list[dict[str, Any]]:
        conds = []
        if companion_id is not None:
            conds.append(COMMANDS.c.companion_id == companion_id)
        if statuses:
            conds.append(COMMANDS.c.status.in_(list(statuses)))
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                select(COMMANDS).where(*conds).order_by(COMMANDS.c.created_at.desc()).limit(limit)
            )).all()
        return [command_json(r) for r in rows]

    async def expire_commands(self, now: float | None = None) -> int:
        now = now or now_ms()
        async with self.engine.begin() as conn:
            result = await conn.execute(update(COMMANDS).where(
                COMMANDS.c.status.in_(COMMAND_ACTIVE), COMMANDS.c.expires_at <= now,
            ).values(status="expired", completed_at=now))
        return int(result.rowcount or 0)

    async def queued_commands(self, companion_id: str) -> list[dict[str, Any]]:
        now = now_ms()
        async with self.engine.connect() as conn:
            rows = (await conn.execute(select(COMMANDS).where(
                COMMANDS.c.companion_id == companion_id, COMMANDS.c.status == "queued",
                COMMANDS.c.expires_at > now,
            ).order_by(COMMANDS.c.created_at.asc()).limit(50))).all()
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
        different terminal status is ``conflict``. A ``clear_tag`` that
        succeeded at the tag's current epoch lifts ``clear_required``."""
        now = now_ms()
        async with self.engine.begin() as conn:
            row = (await conn.execute(select(COMMANDS).where(
                COMMANDS.c.id == command_id, COMMANDS.c.companion_id == companion_id,
            ))).first()
            if row is None:
                return None, "not_found"
            if row.status in COMMAND_TERMINAL:
                return dict(row._mapping), (None if row.status == status else "conflict")
            await conn.execute(update(COMMANDS).where(COMMANDS.c.id == command_id).values(
                status=status, result=_small(result) if result else None,
                error=(error or None) and str(error)[:1000], completed_at=now,
            ))
            args = row.args if isinstance(row.args, dict) else {}
            if status == "succeeded" and row.kind == "clear_tag" and args.get("tag_id"):
                await conn.execute(update(DEVICES).where(
                    DEVICES.c.companion_id == companion_id, DEVICES.c.kind == "tag",
                    DEVICES.c.hw_id == str(args["tag_id"]), DEVICES.c.epoch == int(args.get("epoch") or 0),
                ).values(clear_required=False, updated_at=now))
            row = (await conn.execute(select(COMMANDS).where(COMMANDS.c.id == command_id))).first()
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


def _inventory_values(kind: str, item: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if isinstance(item.get("fw"), str):
        values["fw"] = item["fw"][:32]
    if _int(item.get("board")) is not None:
        values["board"] = item["board"]
    info: dict[str, Any] = {}
    if kind == "gateway":
        for key in ("boot_id", "port"):
            if key in item:
                info[key] = _small(item[key])
    elif kind == "bridge":
        for key in ("addr", "fontpack_id", "flash_size"):
            if key in item:
                info[key] = _small(item[key])
    else:
        for key in ("panel", "width", "height", "planes"):
            if _int(item.get(key)) is not None:
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
