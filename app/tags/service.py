"""Operations shared by the Tags REST handlers (and profile deletion).

Each function runs in ONE transaction and raises :class:`TagError` (status,
code, message) for anything the caller should answer with a 4xx. Ownership
changes follow one pattern — bump ``epoch`` (the companion re-keys the tag
from it), cancel the tag's active deliveries and queued per-tag commands, then
queue the hardware commands:

- **claim**   owner := X, ``clear_required``, ``assign_tag`` + ``clear_tag``;
- **assign**  bridge := B, ``assign_tag`` (active deliveries move to the new epoch);
- **release** owner := none, ``clear_required``, ``clear_tag``;
- **profile deleted** — every tag it owned is released the same way, inside
  the profile delete's own transaction.

Content (``display``/``clear``) is written straight into ``tag_deliveries``;
it is not a journal event, because it targets one tag the caller named.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update

from app.storage.models import ProfileModel
from app.tags import credentials as creds
from app.tags.cards import ICONS, NON_CONTENT_KINDS, CardSpec, make_card
from app.tags.sanitize import clean_multiline, clean_text, contains_otp
from app.tags.storage import (
    ACTIVE_STAGES, DELIVERIES, DEVICES, STREAMS, cancel_device_deliveries, cancel_tag_commands,
    command_json, credential_json, delivery_json, device_json, get_tag_storage,
    insert_command, lock_stream, notify_commands, now_ms, write_deliveries,
)

HOUR_S = 3600.0
DAY_S = 24 * HOUR_S

COMMAND_TTL_S = {
    "assign_tag": 7 * DAY_S,
    "clear_tag": 7 * DAY_S,
    "scan_unprovisioned": 15 * 60.0,
}
DEFAULT_COMMAND_TTL_S = HOUR_S

PINNED_TTL_DEFAULT_S = DAY_S
PINNED_TTL_MIN_S = 60
PINNED_TTL_MAX_S = 7 * DAY_S
CLEAR_TTL_S = 7 * DAY_S


class TagError(Exception):
    """A refusal the API turns into ``{"error": code, "message": message}``."""

    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


def _ttl(kind: str) -> float:
    return COMMAND_TTL_S.get(kind, DEFAULT_COMMAND_TTL_S)


NAME_MAX = 128


def device_name(raw: Any) -> str:
    """A device name: one line, 1..128 characters once stripped (422
    ``invalid_name`` otherwise), with anything secret-looking redacted."""
    text = raw.strip() if isinstance(raw, str) else ""
    if not text or len(text) > NAME_MAX:
        raise TagError(422, "invalid_name", f"'name' must be 1 to {NAME_MAX} characters.")
    name = clean_text(text, NAME_MAX)
    if not name:
        raise TagError(422, "invalid_name", f"'name' must be 1 to {NAME_MAX} characters.")
    return name


async def rename_owned_tag(profile: str, device_id: str, raw_name: Any) -> dict[str, Any]:
    """Rename one of ``profile``'s tags. Ownership (404 for anyone else's id,
    whatever the body) and the name are checked before anything is written;
    the UPDATE itself is also scoped to the owner and to tags."""
    await owned_tag(profile, device_id)
    name = device_name(raw_name)
    device = await get_tag_storage().rename_device(device_id, name, owner=profile)
    if device is None:
        raise TagError(404, "device_not_found", "No tag with that id.")
    return device


async def rename_device(device_id: str, raw_name: Any) -> dict[str, Any]:
    """Admin rename of any device."""
    name = device_name(raw_name)
    device = await get_tag_storage().rename_device(device_id, name)
    if device is None:
        raise TagError(404, "device_not_found", "No device with that id.")
    return device


async def forget_device(device_id: str) -> dict[str, Any]:
    """Admin forget. A tag a profile owns must be released first (409
    ``tag_owned``) so its screen is cleared and its epoch moves on."""
    device, problem = await get_tag_storage().delete_device(device_id)
    if problem == "not_found":
        raise TagError(404, "device_not_found", "No device with that id.")
    if problem == "tag_owned":
        raise TagError(409, "tag_owned",
                       f"The tag is owned by '{device['owner_profile']}'; release it first "
                       "(POST /api/tags/hardware/tags/{id}/release).", device=device)
    return device


def _str_arg(args: dict[str, Any], key: str, *, required: bool = True, limit: int = 128) -> str | None:
    value = args.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise TagError(422, "invalid_args", f"'{key}' must be a non-empty string of at most {limit} characters.")
    return value.strip()


def _validate_admin_args(kind: str, raw: Any) -> dict[str, Any]:
    args = raw if isinstance(raw, dict) else {}
    if raw is not None and not isinstance(raw, dict):
        raise TagError(422, "invalid_args", "'args' must be an object.")
    if kind == "scan_unprovisioned":
        duration = args.get("duration_s", 60)
        if isinstance(duration, bool) or not isinstance(duration, int) or not 5 <= duration <= 600:
            raise TagError(422, "invalid_args", "'duration_s' must be a whole number of seconds from 5 to 600.")
        return {"duration_s": duration}
    if kind == "provision_bridge":
        out = {"uuid": _str_arg(args, "uuid", limit=64)}
        name = _str_arg(args, "name", required=False)
        if name:
            out["name"] = name
        return out
    if kind in ("configure_bridge", "remove_bridge", "identify"):
        return {"hw_id": _str_arg(args, "hw_id", limit=64)}
    if kind == "refresh_tag":
        return {"tag_id": _str_arg(args, "tag_id", limit=64)}
    if kind == "install_fontpack":
        return {"bridge_hw_id": _str_arg(args, "bridge_hw_id", limit=64)}
    if kind == "collect_diagnostics":
        return {}
    raise TagError(422, "unknown_command", f"Unknown command kind '{kind}'.")


# Kinds an admin may queue directly. ``assign_tag`` / ``clear_tag`` only come
# from claim / assign / release, which own the epoch.
ADMIN_COMMAND_KINDS = (
    "scan_unprovisioned", "provision_bridge", "configure_bridge", "remove_bridge",
    "identify", "refresh_tag", "install_fontpack", "collect_diagnostics",
)


async def admin_command(companion_id: str, kind: str, args: Any, *, requested_by: str) -> dict[str, Any]:
    if kind in ("assign_tag", "clear_tag"):
        raise TagError(422, "use_tag_endpoint",
                       f"'{kind}' is queued by the claim / assign / release endpoints, which manage the tag's epoch.")
    if kind not in ADMIN_COMMAND_KINDS:
        raise TagError(422, "unknown_command", f"Unknown command kind '{kind}'.")
    clean = _validate_admin_args(kind, args)
    store = get_tag_storage()
    if await store.get_companion(companion_id) is None:
        raise TagError(404, "companion_not_found", "No companion with that id.")
    return await store.create_command(companion_id=companion_id, kind=kind, args=clean,
                                      requested_by=requested_by, ttl_s=_ttl(kind))


# ── companions and credentials ──────────────────────────────────────────────


def _new_credential(kind: str, *, label: str, created_by: str, profile: str | None = None
                    ) -> tuple[dict[str, Any], str]:
    cred_id = creds.new_credential_id()
    secret = creds.new_secret()
    row = {
        "id": cred_id,
        "kind": kind,
        "profile": profile,
        "secret_sha256": creds.hash_secret(secret),
        "label": label,
        "created_by": created_by or "",
    }
    return row, secret


def secret_payload(cred: dict[str, Any], secret: str) -> dict[str, Any]:
    """What a create/rotate response carries — the only time the secret is shown."""
    return {
        "credential": cred,
        "secret": secret,
        "authorization": creds.authorization_value(cred["id"], secret),
    }


async def register_companion(name: Any, *, created_by: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    label = clean_text(name, 128) if isinstance(name, str) else ""
    if not label:
        raise TagError(422, "invalid_name", "'name' must be a non-empty string.")
    row, secret = _new_credential(creds.KIND_HARDWARE, label=label, created_by=created_by)
    store = get_tag_storage()
    companion = await store.create_companion(name=label, created_by=created_by, credential=row)
    cred = await store.get_credential(row["id"])
    return companion, credential_json(cred), secret


async def rotate_companion(companion_id: str, *, created_by: str) -> tuple[dict[str, Any], str, list[str]]:
    store = get_tag_storage()
    companion = await store.get_companion(companion_id)
    if companion is None:
        raise TagError(404, "companion_not_found", "No companion with that id.")
    row, secret = _new_credential(creds.KIND_HARDWARE, label=companion["name"], created_by=created_by)
    revoked = await store.rotate_hardware_credential(companion_id, row)
    return credential_json(await store.get_credential(row["id"])), secret, revoked


async def create_content_credential(profile: str, *, companion_id: Any, label: Any
                                    ) -> tuple[dict[str, Any], str]:
    if not isinstance(companion_id, str) or not companion_id:
        raise TagError(422, "invalid_companion", "'companion_id' is required.")
    text = label if isinstance(label, str) else ""
    text = clean_text(text, 128) or "Content credential"
    store = get_tag_storage()
    if await store.get_companion(companion_id) is None:
        raise TagError(404, "companion_not_found", "No companion with that id.")
    row, secret = _new_credential(creds.KIND_CONTENT, label=text, created_by=profile, profile=profile)
    row["companion_id"] = companion_id
    cred = await store.insert_credential(row)
    return cred, secret


# ── ownership ───────────────────────────────────────────────────────────────


async def _tag_row(conn, device_id: str):
    row = (await conn.execute(select(DEVICES).where(DEVICES.c.id == device_id))).first()
    if row is None or row.kind != "tag":
        raise TagError(404, "device_not_found", "No tag with that id.")
    return row


async def _bridge_row(conn, tag_row, bridge_id: str | None):
    if bridge_id:
        row = (await conn.execute(select(DEVICES).where(DEVICES.c.id == bridge_id))).first()
        if row is None or row.kind != "bridge" or row.companion_id != tag_row.companion_id:
            raise TagError(422, "bridge_not_found", "No bridge with that id on the tag's companion.")
        return row
    if tag_row.bridge_device_id:
        row = (await conn.execute(select(DEVICES).where(DEVICES.c.id == tag_row.bridge_device_id))).first()
        if row is not None:
            return row
    bridges = (await conn.execute(select(DEVICES).where(
        DEVICES.c.companion_id == tag_row.companion_id, DEVICES.c.kind == "bridge",
    ))).all()
    if len(bridges) == 1:
        return bridges[0]
    raise TagError(409, "bridge_required",
                   "Name the bridge to assign the tag to ('bridge_id'): the companion has "
                   f"{len(bridges)} bridges.")


async def _queue(conn, companion_id: str, kind: str, args: dict[str, Any], requested_by: str, now: float):
    return await insert_command(conn, companion_id=companion_id, kind=kind, args=args,
                                requested_by=requested_by, ttl_s=_ttl(kind), now=now)


async def claim_tag(device_id: str, *, owner: Any, bridge_id: Any = None, name: Any = None,
                    requested_by: str) -> dict[str, Any]:
    if not isinstance(owner, str) or not owner.strip() or owner.startswith("__"):
        raise TagError(422, "invalid_owner", "'owner' must name a profile.")
    owner = owner.strip()
    if bridge_id is not None and not isinstance(bridge_id, str):
        raise TagError(422, "invalid_bridge", "'bridge_id' must be a device id.")
    new_name = device_name(name) if name is not None else None
    store = get_tag_storage()
    now = now_ms()
    async with store.engine.begin() as conn:
        tag = await _tag_row(conn, device_id)
        exists = (await conn.execute(select(ProfileModel.name).where(ProfileModel.name == owner))).first()
        if exists is None:
            raise TagError(422, "unknown_profile", f"No profile named '{owner}'.")
        bridge = await _bridge_row(conn, tag, bridge_id)
        epoch = int(tag.epoch or 0) + 1
        values: dict[str, Any] = {
            "owner_profile": owner, "epoch": epoch, "clear_required": True,
            "bridge_device_id": bridge.id, "status": "assigning", "claimed_at": now, "updated_at": now,
        }
        if new_name is not None:
            values["name"] = new_name
        await conn.execute(update(DEVICES).where(DEVICES.c.id == tag.id).values(**values))
        await cancel_device_deliveries(conn, tag.id, now, "reassigned")
        await cancel_tag_commands(conn, tag.companion_id, tag.hw_id, now)
        assign = await _queue(conn, tag.companion_id, "assign_tag",
                              {"tag_id": tag.hw_id, "bridge_hw_id": bridge.hw_id, "epoch": epoch},
                              requested_by, now)
        clear = await _queue(conn, tag.companion_id, "clear_tag", {"tag_id": tag.hw_id, "epoch": epoch},
                             requested_by, now)
        row = (await conn.execute(select(DEVICES).where(DEVICES.c.id == tag.id))).first()
    notify_commands([tag.companion_id])
    return {"device": device_json(row), "commands": [command_json(assign), command_json(clear)]}


async def assign_tag(device_id: str, *, bridge_id: Any, requested_by: str) -> dict[str, Any]:
    if not isinstance(bridge_id, str) or not bridge_id:
        raise TagError(422, "invalid_bridge", "'bridge_id' is required.")
    store = get_tag_storage()
    now = now_ms()
    async with store.engine.begin() as conn:
        tag = await _tag_row(conn, device_id)
        bridge = await _bridge_row(conn, tag, bridge_id)
        epoch = int(tag.epoch or 0) + 1
        await conn.execute(update(DEVICES).where(DEVICES.c.id == tag.id).values(
            epoch=epoch, bridge_device_id=bridge.id, updated_at=now,
            status="assigning" if tag.owner_profile else tag.status,
        ))
        # Same owner, new key: the queued cards stay valid under the new epoch.
        await conn.execute(update(DELIVERIES).where(
            DELIVERIES.c.tag_device_id == tag.id, DELIVERIES.c.stage.in_(ACTIVE_STAGES),
        ).values(epoch=epoch, updated_at=now))
        await cancel_tag_commands(conn, tag.companion_id, tag.hw_id, now)
        assign = await _queue(conn, tag.companion_id, "assign_tag",
                              {"tag_id": tag.hw_id, "bridge_hw_id": bridge.hw_id, "epoch": epoch},
                              requested_by, now)
        row = (await conn.execute(select(DEVICES).where(DEVICES.c.id == tag.id))).first()
    notify_commands([tag.companion_id])
    return {"device": device_json(row), "command": command_json(assign)}


async def _release_rows(conn, rows, *, detail: str, requested_by: str, now: float) -> list[dict[str, Any]]:
    commands = []
    for tag in rows:
        epoch = int(tag.epoch or 0) + 1
        await conn.execute(update(DEVICES).where(DEVICES.c.id == tag.id).values(
            owner_profile=None, epoch=epoch, clear_required=True, status="unclaimed",
            claimed_at=None, updated_at=now,
        ))
        await cancel_device_deliveries(conn, tag.id, now, detail)
        await cancel_tag_commands(conn, tag.companion_id, tag.hw_id, now)
        commands.append(await _queue(conn, tag.companion_id, "clear_tag",
                                     {"tag_id": tag.hw_id, "epoch": epoch}, requested_by, now))
    return commands


async def release_tag(device_id: str, *, requested_by: str) -> dict[str, Any]:
    store = get_tag_storage()
    now = now_ms()
    async with store.engine.begin() as conn:
        tag = await _tag_row(conn, device_id)
        commands = await _release_rows(conn, [tag], detail="released", requested_by=requested_by, now=now)
        row = (await conn.execute(select(DEVICES).where(DEVICES.c.id == tag.id))).first()
    notify_commands([tag.companion_id])
    return {"device": device_json(row), "commands": [command_json(c) for c in commands]}


async def release_profile_tags(session, profile: str) -> list[str]:
    """Release every tag ``profile`` owns, inside the caller's transaction
    (``ConversationStorage.delete_profile``). Returns the companions to wake
    once that transaction commits.

    The first statement is a write (the stream-row lock, then the device
    UPDATE ``RETURNING`` the new epochs), so SQLite takes its write lock
    before any read, and PostgreSQL orders this after an in-flight projection
    of the same profile instead of deadlocking with it."""
    now = now_ms()
    await session.execute(
        update(STREAMS).where(STREAMS.c.profile == profile).values(updated_at=now)
    )
    rows = (await session.execute(
        update(DEVICES)
        .where(DEVICES.c.owner_profile == profile, DEVICES.c.kind == "tag")
        .values(owner_profile=None, epoch=DEVICES.c.epoch + 1, clear_required=True,
                status="unclaimed", claimed_at=None, updated_at=now)
        .returning(DEVICES.c.id, DEVICES.c.companion_id, DEVICES.c.hw_id, DEVICES.c.epoch)
    )).all()
    for tag in rows:
        await cancel_device_deliveries(session, tag.id, now, "profile deleted")
        await cancel_tag_commands(session, tag.companion_id, tag.hw_id, now)
        await _queue(session, tag.companion_id, "clear_tag",
                     {"tag_id": tag.hw_id, "epoch": int(tag.epoch)}, "system", now)
    return sorted({r.companion_id for r in rows})


# ── a profile's own tags ────────────────────────────────────────────────────


async def owned_tag(profile: str, device_id: str) -> dict[str, Any]:
    """The tag, when ``profile`` owns it; otherwise 404 (never 403, so another
    profile's device ids are not confirmed to exist)."""
    device = await get_tag_storage().get_device(device_id)
    if device is None or device["kind"] != "tag" or device["owner_profile"] != profile:
        raise TagError(404, "device_not_found", "No tag with that id.")
    return device


def _pinned_fields(body: dict[str, Any]) -> tuple[str, str | None, str, float]:
    title = body.get("title")
    text = body.get("body")
    icon = body.get("icon") or "push_pin"
    ttl = body.get("ttl_s", PINNED_TTL_DEFAULT_S)
    if not isinstance(title, str) or not title.strip():
        raise TagError(422, "invalid_title", "'title' must be a non-empty string.")
    if len(title) > 120:
        raise TagError(422, "invalid_title", "'title' must be at most 120 characters.")
    if text is not None and (not isinstance(text, str) or len(text) > 400):
        raise TagError(422, "invalid_body", "'body' must be a string of at most 400 characters.")
    if icon not in ICONS:
        raise TagError(422, "invalid_icon", f"'icon' must be one of: {', '.join(ICONS)}.")
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not PINNED_TTL_MIN_S <= ttl <= PINNED_TTL_MAX_S:
        raise TagError(422, "invalid_ttl", f"'ttl_s' must be between {PINNED_TTL_MIN_S} and {int(PINNED_TTL_MAX_S)} seconds.")
    if contains_otp(title) or contains_otp(text):
        raise TagError(422, "otp_refused",
                       "The text looks like it contains a one-time code; codes are never shown on a tag.")
    body_text = clean_multiline(text, 400) if text else ""
    return clean_text(title, 120), (body_text or None), icon, float(ttl)


async def display(profile: str, device_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Pin a note on one of the profile's tags (sanitised, OTP-checked). The
    title is one line; the body keeps its paragraphs. A tag still waiting for
    its screen to be cleared answers 409 ``clear_pending`` — decided inside
    the transaction that would write the delivery."""
    title, text, icon, ttl = _pinned_fields(body)
    await owned_tag(profile, device_id)
    from app.tags.routing import effective_options

    settings = await get_tag_storage().get_settings(profile)
    lang = effective_options((settings or {}).get("options")).get("language") or "en"
    now = now_ms()
    spec = CardSpec(
        kind="pinned_note",
        card=make_card("pinned_note", title=title, body=text, icon=icon, lang=lang, ts_ms=now,
                       source_type="user", source_id=device_id),
        priority=55,
        expires_at=now + ttl * 1000.0,
        replace_key=f"pinned:{device_id}",
    )
    return await _write_direct(profile, device_id, spec, now)


async def clear(profile: str, device_id: str) -> dict[str, Any]:
    """Blank one of the profile's tags: its active cards are cancelled and a
    ``clear`` job is queued."""
    await owned_tag(profile, device_id)
    now = now_ms()
    spec = CardSpec(
        kind="clear",
        card=make_card("clear", title="Cleared", icon="info", ts_ms=now, source_type="user",
                       source_id=device_id),
        priority=100,
        expires_at=now + CLEAR_TTL_S * 1000.0,
    )
    return await _write_direct(profile, device_id, spec, now, cancel_first=True)


async def _write_direct(profile: str, device_id: str, spec: CardSpec, now: float, *,
                        cancel_first: bool = False) -> dict[str, Any]:
    store = get_tag_storage()
    async with store.engine.begin() as conn:
        await lock_stream(conn, profile, now)
        # Re-read inside the transaction: ownership may have moved since.
        row = (await conn.execute(select(DEVICES).where(
            DEVICES.c.id == device_id, DEVICES.c.owner_profile == profile, DEVICES.c.kind == "tag",
        ))).first()
        if row is None:
            raise TagError(404, "device_not_found", "No tag with that id.")
        # Under the stream-row lock and in the transaction that writes: a
        # claim that landed after the caller's first read is seen here.
        if spec.kind not in NON_CONTENT_KINDS and row.clear_required:
            raise TagError(409, "clear_pending",
                           "The tag's screen is still being cleared after a change of owner; "
                           "try again shortly.")
        if cancel_first:
            await cancel_device_deliveries(conn, device_id, now, "cleared")
        written = await write_deliveries(conn, profile, [(device_json(row), spec, None)], now)
    return delivery_json(written[0])


async def device_command(profile: str, device_id: str, kind: str, *, requested_by: str) -> dict[str, Any]:
    """``refresh_tag`` / ``identify`` for one of the profile's own tags."""
    device = await owned_tag(profile, device_id)
    args = {"tag_id": device["hw_id"]} if kind == "refresh_tag" else {"hw_id": device["hw_id"]}
    return await get_tag_storage().create_command(
        companion_id=device["companion_id"], kind=kind, args=args,
        requested_by=requested_by, ttl_s=_ttl(kind),
    )
