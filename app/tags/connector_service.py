"""The connector API's business logic, shared by both ways a worker reaches it.

A hardware runtime worker talks to Cremind through exactly one of two
adapters, and both end here:

- **HTTP** (:mod:`app.api.tag_connector`): a remote worker — a desktop
  hardware host, or a legacy Cremind Connect / manual runtime — sends
  ``Authorization: CremindTag <credential-id>.<secret>`` to
  ``/api/tag-connector/v1/*``;
- **in process** (:mod:`app.tags.hosting.local_connector`): a worker the
  backend runs itself calls these functions directly, with the same
  credential.

So there is one set of rules. :func:`authenticate` checks the credential
exactly as the HTTP header check always did (constant-time secret check,
revocation, kind, a content credential's profile), and a worker hosted in
process additionally names what it believes it is (:class:`WorkerExpectation`):
the companion, the owning profile's immutable UUID, the worker generation and
the hardware host. A profile deleted and recreated under the same name, a
recovery that moved the worker to another computer, or a gateway handed to
another host all end its access (401 ``worker_revoked``), just as the
revoked credentials end a remote worker's.

Every function raises :class:`app.tags.service.TagError` for a refusal (the
HTTP adapter turns it into the documented error body, the local one into the
same exception the HTTP client would raise) and returns plain JSON-able data.
The endpoint contract itself is documented in :mod:`app.api.tag_connector`.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.tags import credentials as creds
from app.tags import routing
from app.tags.cards import iso
from app.tags.service import TagError
from app.tags.storage import (
    COMPANIONS, command_event, connector_command_json, ensure_stream, get_tag_storage,
)
from app.utils.logger import logger

API_VERSION = 1
# v2: private workers (lease, reconciliation state, operations, grants, vault).
API_VERSION_V2 = 2
V2_CAPABILITIES = ("lease", "state", "operations", "grants", "vault")
MAX_WAIT_S = 30
MAX_PREVIEW_BYTES = 64 * 1024
MAX_U32 = 2 ** 32 - 1
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_TOUCH_EVERY_S = 60.0
_last_touch: dict[str, float] = {}


@dataclass(frozen=True)
class WorkerExpectation:
    """What a worker hosted in process believes it is (from its worker directory)."""

    companion_id: str
    profile_id: str
    generation: int
    host_id: str | None = None


def now_iso() -> str:
    return iso(time.time() * 1000)


def as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


# ── authentication ──


async def authenticate(credential_id: str, secret: str, kind: str | None, *,
                       expect: WorkerExpectation | None = None) -> dict[str, Any]:
    """The principal of a connector credential: ``{credential_id, kind,
    companion_id, profile}`` (``profile`` only for a content credential).

    Raises 401 ``invalid_credential`` / ``credential_revoked`` or 403
    ``wrong_credential_kind`` — and, with ``expect``, 401 ``worker_revoked``
    when the worker is no longer the one it believes it is."""
    store = get_tag_storage()
    row = await store.get_credential(credential_id)
    if not creds.verify(row, secret):
        raise TagError(401, "invalid_credential", "The credential is not valid.")
    if row.get("revoked_at") is not None:
        raise TagError(401, "credential_revoked", "The credential has been revoked.")
    if row["kind"] == creds.KIND_CONTENT and not row.get("profile"):
        raise TagError(401, "invalid_credential", "The credential is not valid.")
    if kind is not None and row["kind"] != kind:
        raise TagError(403, "wrong_credential_kind", f"This endpoint needs a {kind} credential.")
    if expect is not None:
        await _check_expectation(row, expect)
    mono = time.monotonic()
    if mono - _last_touch.get(row["id"], 0.0) >= _TOUCH_EVERY_S:
        _last_touch[row["id"]] = mono
        try:
            await store.touch_credential(row["id"], time.time() * 1000)
        except Exception:  # noqa: BLE001
            logger.debug("[tags] could not record credential use", exc_info=True)
    return {
        "credential_id": row["id"],
        "kind": row["kind"],
        "companion_id": row["companion_id"],
        "profile": row.get("profile") if row["kind"] == creds.KIND_CONTENT else None,
    }


async def _check_expectation(row: dict[str, Any], expect: WorkerExpectation) -> None:
    from app.storage.models import ProfileModel

    async with get_tag_storage().engine.connect() as conn:
        comp = (await conn.execute(select(COMPANIONS).where(COMPANIONS.c.id == row["companion_id"]))).first()
        owner = None
        if comp is not None and comp.owner_profile:
            owner = (await conn.execute(select(ProfileModel.id).where(
                ProfileModel.name == comp.owner_profile))).scalar_one_or_none()
    problems = []
    if comp is None or comp.id != expect.companion_id:
        problems.append("the credential belongs to another worker")
    elif comp.mode != "private":
        problems.append("the worker is not a private worker")
    else:
        if comp.owner_profile_id != expect.profile_id or (owner is not None and str(owner) != expect.profile_id):
            problems.append("its profile is not the one it was set up for")
        elif owner is None:
            problems.append("its profile no longer exists")
        if int(comp.generation or 0) != int(expect.generation):
            problems.append("it moved to another computer")
        if expect.host_id is not None and getattr(comp, "host_id", None) != expect.host_id:
            problems.append("another hardware host runs it now")
        if comp.state == "removed":
            problems.append("it was removed")
    if problems:
        raise TagError(401, "worker_revoked", "This worker may no longer act: " + "; ".join(problems) + ".")


# ── any kind ──


async def whoami(grant: dict[str, Any]) -> dict[str, Any]:
    comp = await get_tag_storage().get_companion(grant["companion_id"]) or {}
    private = comp.get("mode") == "private"
    out: dict[str, Any] = {**grant, "api_version": API_VERSION_V2 if private else API_VERSION,
                           "server_time": now_iso(), "mode": comp.get("mode") or "legacy_shared",
                           "capabilities": list(V2_CAPABILITIES) if private else []}
    if private:
        state = comp.get("state") or "active"
        out["worker"] = {"generation": int(comp.get("generation") or 0),
                         "state": state if state != "active" or not comp.get("paused") else "paused",
                         "paused": bool(comp.get("paused"))}
    return out


# ── hardware ──


async def inventory(grant: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    devices = await get_tag_storage().upsert_inventory(grant["companion_id"], body)
    hw_by_id = {d["id"]: d["hw_id"] for d in devices}
    assignments = [
        {
            "tag_id": d["hw_id"],
            "owner_profile": d["owner_profile"],
            "bridge_hw_id": hw_by_id.get(d["bridge_device_id"] or ""),
            "epoch": d["epoch"],
            "rotation": d["rotation"],
        }
        for d in devices if d["kind"] == "tag"
    ]
    return {"devices": devices, "assignments": assignments}


async def heartbeat(grant: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    store = get_tag_storage()
    pending = await store.record_heartbeat(grant["companion_id"], body)
    comp = await store.get_companion(grant["companion_id"]) or {}
    if comp.get("mode") == "private":
        from app.tags import operations

        devices = body.get("devices") if isinstance(body.get("devices"), list) else None
        async with store.engine.begin() as conn:
            await operations.on_worker_heartbeat(conn, grant["companion_id"], time.time() * 1000, devices=devices)
    return {"server_time": now_iso(), "commands_pending": pending}


async def commands(grant: dict[str, Any], wait: Any) -> dict[str, Any]:
    """Queued commands; waits up to ``wait`` (≤ 30) seconds for one to be queued."""
    seconds = as_int(wait if wait is not None else 0)
    if seconds is None or seconds < 0:
        raise TagError(400, "invalid_wait", "'wait' must be a whole number of seconds.")
    seconds = min(seconds, MAX_WAIT_S)
    store = get_tag_storage()
    companion_id = grant["companion_id"]
    event = command_event(companion_id)
    deadline = time.monotonic() + seconds
    while True:
        event.clear()
        rows = await store.queued_commands(companion_id)
        remaining = deadline - time.monotonic()
        if rows or remaining <= 0:
            break
        try:
            await asyncio.wait_for(event.wait(), timeout=min(remaining, 5.0))
        except asyncio.TimeoutError:
            pass
    return {"commands": [connector_command_json(r) for r in rows]}


async def claim(grant: dict[str, Any], command_id: str) -> dict[str, Any]:
    row, problem = await get_tag_storage().claim_command(grant["companion_id"], command_id)
    if problem == "not_found":
        raise TagError(404, "command_not_found", "No command with that id.")
    if problem == "already_claimed":
        raise TagError(409, "already_claimed", f"The command is already {row['status']}.",
                       command=connector_command_json(row))
    return connector_command_json(row)


async def result(grant: dict[str, Any], command_id: str, body: dict[str, Any]) -> dict[str, Any]:
    status = body.get("status")
    if status not in ("succeeded", "failed"):
        raise TagError(422, "invalid_status", "'status' must be 'succeeded' or 'failed'.")
    outcome = body.get("result")
    if outcome is not None and not isinstance(outcome, dict):
        raise TagError(422, "invalid_result", "'result' must be an object.")
    error = body.get("error")
    if error is not None and not isinstance(error, str):
        raise TagError(422, "invalid_error", "'error' must be a string.")
    row, problem = await get_tag_storage().complete_command(
        grant["companion_id"], command_id, status=status, result=outcome, error=error)
    if problem == "not_found":
        raise TagError(404, "command_not_found", "No command with that id.")
    if problem == "conflict":
        raise TagError(409, "already_completed", f"The command already finished as {row['status']}.",
                       command=connector_command_json(row))
    return {"ok": True, "command": connector_command_json(row)}


# ── content ──


async def _profile_tags(profile: str, companion_id: str) -> list[dict[str, Any]]:
    devices = await get_tag_storage().list_devices(companion_id=companion_id)
    # A tag's parent: a bridge, or the gateway itself for a tag it serves on its own radio.
    parents = {d["id"]: d["hw_id"] for d in devices if d["kind"] in ("bridge", "gateway")}
    out = []
    for d in devices:
        if d["kind"] != "tag" or d["owner_profile"] != profile:
            continue
        out.append({
            "tag_id": d["hw_id"],
            "name": d["name"],
            "epoch": d["epoch"],
            "bridge_hw_id": parents.get(d["bridge_device_id"] or ""),
            "width": d["width"],
            "height": d["height"],
            "planes": d["planes"],
            "rotation": d["rotation"],
            "desired_revision": d["desired_revision"],
            "displayed_revision": d["displayed_revision"],
            "clear_required": d["clear_required"],
        })
    return out


async def sync(grant: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    profile, companion_id = grant["profile"], grant["companion_id"]
    cursor = body.get("cursor")
    if cursor is not None and (as_int(cursor) is None or as_int(cursor) < 0):
        raise TagError(400, "invalid_cursor", "'cursor' must be a whole number or null.")
    store = get_tag_storage()
    async with store.engine.begin() as conn:
        await ensure_stream(conn, profile)
    stream = await store.get_stream(profile) or {}
    head = int(stream.get("next_delivery_seq") or 0)
    pruned = int((stream.get("state") or {}).get("pruned_through_seq") or 0)
    cursor_valid = cursor is not None and pruned <= as_int(cursor) <= head
    settings = await store.get_settings(profile)
    options = await routing.effective_options_async((settings or {}).get("options"))
    return {
        "profile": profile,
        "companion_id": companion_id,
        "stream_id": stream.get("stream_id"),
        "cursor_valid": bool(cursor_valid),
        "oldest_seq": pruned + 1,
        "head_seq": head,
        "outstanding": await store.outstanding_jobs(profile, companion_id),
        "tags": await _profile_tags(profile, companion_id),
        "settings": {
            "enabled": bool((settings or {}).get("enabled")),
            "layout": options["layout"],
            "show_excerpts": options["show_excerpts"],
            "qr_links": options["qr_links"],
            "progress_cadence_s": options["progress_cadence_s"],
            "timezone": routing.resolved_timezone(profile, options),
            "language": options["language"],
        },
    }


async def events(grant: dict[str, Any], after: Any, limit: Any) -> dict[str, Any]:
    after_seq = as_int(after if after is not None else 0)
    if after_seq is None or after_seq < 0:
        raise TagError(400, "invalid_cursor", "'after' must be a whole number.")
    page_limit = as_int(limit if limit is not None else 100)
    if page_limit is None or page_limit < 1:
        raise TagError(400, "invalid_limit", "'limit' must be a positive whole number.")
    page = await get_tag_storage().jobs_after(grant["profile"], grant["companion_id"], after_seq,
                                              min(page_limit, 200))
    head, pruned = page["head_seq"], page["pruned_through"]
    if after_seq > head or after_seq < pruned:
        raise TagError(410, "cursor_expired", "The cursor is outside the retained history; call sync.",
                       oldest_seq=pruned + 1, head_seq=head, stream_id=page["stream_id"])
    return {"stream_id": page["stream_id"], "jobs": page["jobs"], "next_after": page["next_after"],
            "head_seq": head}


async def accepted(grant: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    through = body.get("through_seq")
    if through is not None and (as_int(through) is None or as_int(through) < 0):
        raise TagError(400, "invalid_cursor", "'through_seq' must be a whole number.")
    ids = body.get("delivery_ids") or []
    if not isinstance(ids, list) or len(ids) > 1000 or any(as_int(i) is None for i in ids):
        raise TagError(422, "invalid_delivery_ids", "'delivery_ids' must be a list of delivery ids.")
    count = await get_tag_storage().accept(grant["profile"], grant["companion_id"], [as_int(i) for i in ids])
    return {"accepted": count}


async def receipts(grant: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    items = body.get("receipts")
    if not isinstance(items, list) or len(items) > 500 or not all(isinstance(r, dict) for r in items):
        raise TagError(422, "invalid_receipts", "'receipts' must be a list of receipt objects.")
    applied, rejected = await get_tag_storage().apply_receipts(grant["profile"], grant["companion_id"], items)
    return {"applied": applied, "rejected": rejected}


async def previews(grant: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    kind = body.get("kind")
    if kind not in ("desired", "displayed"):
        raise TagError(422, "invalid_kind", "'kind' must be 'desired' or 'displayed'.")
    revision = as_int(body.get("revision"))
    if revision is None or not 0 <= revision <= MAX_U32:
        raise TagError(422, "invalid_revision", "'revision' must be a whole number (uint32).")
    epoch = body.get("epoch")
    if epoch is not None and (as_int(epoch) is None or not 0 <= as_int(epoch) <= MAX_U32):
        raise TagError(422, "invalid_epoch", "'epoch' must be a whole number (uint32).")
    ids = body.get("delivery_ids") or []
    if not isinstance(ids, list) or len(ids) > 200 or any(as_int(i) is None for i in ids):
        raise TagError(422, "invalid_delivery_ids", "'delivery_ids' must be a list of delivery ids.")
    raw = body.get("png_base64")
    if not isinstance(raw, str) or not raw:
        raise TagError(422, "invalid_preview", "'png_base64' is required.")
    if len(raw) > (MAX_PREVIEW_BYTES * 4) // 3 + 8:
        raise TagError(422, "preview_too_large", "A preview is at most 64 KiB.")
    try:
        png = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise TagError(422, "invalid_preview", "'png_base64' is not valid base64.") from None
    if len(png) > MAX_PREVIEW_BYTES:
        raise TagError(422, "preview_too_large", "A preview is at most 64 KiB.")
    if not png.startswith(_PNG_MAGIC):
        raise TagError(422, "invalid_preview", "The preview is not a PNG image.")
    stored, problem = await get_tag_storage().store_preview(
        grant["companion_id"], grant["profile"], body.get("tag_id"), kind=kind, revision=revision,
        epoch=as_int(epoch) if epoch is not None else None,
        png_base64=base64.b64encode(png).decode("ascii"), delivery_ids=[as_int(i) for i in ids],
    )
    if problem == "tag_not_found":
        raise TagError(404, "tag_not_found", "No tag with that id belongs to this profile.")
    if problem == "epoch_mismatch":
        raise TagError(409, "epoch_mismatch", "The preview was rendered for another epoch of the tag.")
    return {"stored": stored}


# ── v2: private workers ──


async def lease(grant: dict[str, Any]) -> dict[str, Any]:
    from app.tags import operations

    return await operations.lease(grant)


async def worker_state(grant: dict[str, Any]) -> dict[str, Any]:
    from app.tags import operations

    return await operations.worker_state(grant)


async def operation(grant: dict[str, Any], operation_id: str) -> dict[str, Any]:
    from app.tags import operations

    return await operations.worker_operation(grant, operation_id)


async def progress(grant: dict[str, Any], operation_id: str, body: dict[str, Any]) -> dict[str, Any]:
    from app.tags import operations

    return await operations.progress(grant, operation_id, body)


async def issue_grant(grant: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    from app.tags import operations

    return await operations.issue_grant(grant, body)


async def vault_put(grant: dict[str, Any], subject: str, body: dict[str, Any]) -> dict[str, Any]:
    from app.tags import operations

    return await operations.vault_put(grant, subject, body)


async def vault_get(grant: dict[str, Any]) -> dict[str, Any]:
    from app.tags import operations

    return await operations.vault_get(grant)


# ── the endpoint table both adapters route through ──


@dataclass(frozen=True)
class Call:
    """One request, transport-neutral: path parameters, query parameters, the JSON body."""

    path: dict[str, str]
    query: Mapping[str, Any]
    body: dict[str, Any] | None


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    """Below ``/api/tag-connector/v1``, with ``{name}`` parameters."""
    kind: str | None
    """The credential kind it needs (``None``: any)."""
    run: Callable[[dict[str, Any], Call], Awaitable[Any]]
    body: bool = False
    """Takes a JSON object body (400 ``invalid_json`` otherwise)."""

    @property
    def pattern(self) -> re.Pattern[str]:
        return re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", self.path) + "$")


_HW, _CT = creds.KIND_HARDWARE, creds.KIND_CONTENT

ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("GET", "/whoami", None, lambda g, c: whoami(g)),
    # hardware credential
    Endpoint("POST", "/inventory", _HW, lambda g, c: inventory(g, c.body or {}), body=True),
    Endpoint("POST", "/heartbeat", _HW, lambda g, c: heartbeat(g, c.body or {}), body=True),
    Endpoint("GET", "/commands", _HW, lambda g, c: commands(g, c.query.get("wait") or 0)),
    Endpoint("POST", "/commands/{command_id}/claim", _HW, lambda g, c: claim(g, c.path["command_id"])),
    Endpoint("POST", "/commands/{command_id}/result", _HW,
             lambda g, c: result(g, c.path["command_id"], c.body or {}), body=True),
    # content credential
    Endpoint("POST", "/sync", _CT, lambda g, c: sync(g, c.body or {}), body=True),
    Endpoint("GET", "/events", _CT, lambda g, c: events(g, c.query.get("after") or 0, c.query.get("limit") or 100)),
    Endpoint("POST", "/accepted", _CT, lambda g, c: accepted(g, c.body or {}), body=True),
    Endpoint("POST", "/receipts", _CT, lambda g, c: receipts(g, c.body or {}), body=True),
    Endpoint("POST", "/previews", _CT, lambda g, c: previews(g, c.body or {}), body=True),
    # v2: private workers, hardware credential
    Endpoint("POST", "/lease", _HW, lambda g, c: lease(g)),
    Endpoint("GET", "/state", _HW, lambda g, c: worker_state(g)),
    Endpoint("GET", "/operations/{operation_id}", _HW, lambda g, c: operation(g, c.path["operation_id"])),
    Endpoint("POST", "/operations/{operation_id}/progress", _HW,
             lambda g, c: progress(g, c.path["operation_id"], c.body or {}), body=True),
    Endpoint("POST", "/grants", _HW, lambda g, c: issue_grant(g, c.body or {}), body=True),
    Endpoint("PUT", "/vault/{subject}", _HW, lambda g, c: vault_put(g, c.path["subject"], c.body or {}), body=True),
    Endpoint("GET", "/vault", _HW, lambda g, c: vault_get(g)),
)


def match(method: str, path: str) -> tuple[Endpoint, dict[str, str]] | None:
    """The endpoint serving ``method path`` and its path parameters."""
    for endpoint in ENDPOINTS:
        if endpoint.method == method:
            found = endpoint.pattern.match(path)
            if found is not None:
                return endpoint, found.groupdict()
    return None


__all__ = [
    "API_VERSION", "API_VERSION_V2", "ENDPOINTS", "MAX_WAIT_S", "V2_CAPABILITIES", "Call", "Endpoint",
    "WorkerExpectation", "accepted", "as_int", "authenticate", "claim", "commands", "events", "heartbeat",
    "inventory", "issue_grant", "lease", "match", "now_iso", "operation", "previews", "progress", "receipts",
    "result", "sync", "vault_get", "vault_put", "whoami", "worker_state",
]
