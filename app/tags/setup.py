"""Connect setup sessions (cremind-tag ``docs/connect-setup.md`` §8.1, §8.4,
``docs/setup-api.md`` §1-2).

A session is created by a signed-in profile (the Settings page or the CLI)
and completed by Cremind Connect on the computer the gateway is plugged into:

    waiting_for_connect --bind--> waiting_for_approval --approve (native, gateway)-->
    waiting_for_confirmation --confirm (browser, same sign-in)--> redeeming
    --redeem--> connecting --claim done + first heartbeat--> completed

``probe`` sessions stop at bind (they only tell the page which Connect
answers on this computer); ``recover`` sessions redeem into a recovery of an
existing private worker. Sessions live five minutes until redeemed, are
single use, and carry only the SHA-256 of the launch-link token.

Connect authenticates with ``CremindSetup <session>.<token>`` and, from bind
on, an Ed25519 proof by its installation key over the action, the session,
the server nonce and the body (:func:`proof_message`). None of this
authorises anything but these five calls.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import uuid
from typing import Any
from urllib.parse import quote, urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import insert, select, update

from app.storage.models import ProfileModel, TagSetupSessionModel
from app.tags import protocol_v2 as v2
from app.tags.service import TagError
from app.tags.storage import get_tag_storage, now_ms

SESSIONS = TagSetupSessionModel.__table__

SESSION_TTL_MS = 5 * 60 * 1000.0
SCHEME = "CremindSetup"
LINK_SCHEME = "cremind-connect"
OPERATIONS = ("connect_gateway", "recover", "probe")
PLATFORMS = ("windows", "macos", "linux", "other")
PROOF_PREFIX = b"cremind-connect/v1/"
PRE_REDEEM = ("waiting_for_connect", "waiting_for_approval", "waiting_for_confirmation", "redeeming")
FINAL = ("completed", "cancelled", "expired", "failed")

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")

# Four words (32 bits) people compare between the page and the native window.
PHRASE_WORDS = (
    "acorn", "amber", "anchor", "apple", "arrow", "aspen", "atlas", "autumn", "badge", "bamboo", "banjo",
    "barley", "basil", "beacon", "birch", "bison", "blossom", "bonsai", "breeze", "brick", "bridge", "brook",
    "cabin", "cactus", "camel", "candle", "canyon", "carbon", "cedar", "cello", "chalk", "cherry", "chess",
    "cider", "cinder", "citrus", "clover", "cobalt", "comet", "coral", "cotton", "cove", "crane", "crater",
    "cricket", "crystal", "cypress", "daisy", "delta", "denim", "desert", "dolphin", "dune", "eagle", "echo",
    "ember", "emerald", "falcon", "fern", "fiddle", "fjord", "flint", "forest", "fossil", "fox", "garnet",
    "geyser", "ginger", "glacier", "globe", "granite", "grape", "harbor", "hazel", "heron", "hickory",
    "honey", "horizon", "iris", "island", "ivory", "jasmine", "jungle", "juniper", "kayak", "kelp", "kettle",
    "kiwi", "lagoon", "lantern", "larch", "lava", "lemon", "lilac", "linen", "lotus", "lynx", "magnet",
    "mango", "maple", "marble", "meadow", "melon", "mesa", "meteor", "mint", "mirror", "mocha", "monsoon",
    "moss", "nectar", "nickel", "nomad", "nutmeg", "oasis", "ocean", "olive", "onyx", "orbit", "orchid",
    "otter", "owl", "oyster", "paddle", "panda", "papaya", "parrot", "pebble", "pepper", "piano", "pine",
    "planet", "plum", "pollen", "poppy", "prairie", "prism", "pumpkin", "quartz", "quill", "radish",
    "rain", "raven", "reef", "ribbon", "river", "robin", "rocket", "rose", "ruby", "saddle", "saffron",
    "sage", "salmon", "sand", "sapphire", "satin", "savanna", "scarf", "sequoia", "shadow", "shell",
    "sierra", "silk", "silver", "sky", "slate", "snow", "sparrow", "spice", "spruce", "star", "stone",
    "storm", "summit", "sun", "swan", "tango", "teal", "thistle", "thunder", "tiger", "timber", "topaz",
    "tulip", "tundra", "turtle", "umber", "valley", "velvet", "violet", "walnut", "willow", "wind",
    "winter", "wren", "yarrow", "zebra", "zephyr", "zinc", "harp", "garden", "lake", "cloud", "field",
    "hill", "coast", "cliff", "trail", "pond", "wave", "tide", "leaf", "root", "seed", "bloom", "thorn",
    "berry", "bean", "wheat", "oat", "rye", "corn", "kale", "leek", "okra", "pear", "fig", "date", "lime",
    "peach", "melody", "rhythm", "violin", "flute", "drum", "bell", "chime", "lark", "finch", "gull",
    "stork", "ibis", "koala", "llama", "moose", "yak", "gecko", "newt", "toad", "moth", "bee", "ant",
    "canoe", "meadowlark", "puffin",
)
assert len(PHRASE_WORDS) == 256 and len(set(PHRASE_WORDS)) == 256, "phrase word list must be 256 distinct words"


# ── helpers ──


def fingerprint_token(authorization: str | None) -> str:
    """SHA-256 of the caller's bearer token (binds a session to one sign-in)."""
    raw = (authorization or "").strip()
    token = raw.split(" ", 1)[1].strip() if raw.lower().startswith("bearer ") else raw
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def server_origin(value: Any) -> str:
    """A bare ``http(s)://host[:port]`` origin, else 422 ``invalid_server_url``."""
    if not isinstance(value, str) or len(value) > 255:
        raise TagError(422, "invalid_server_url", "'server_url' must be the address of this Cremind server.")
    parts = urlsplit(value.strip())
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise TagError(422, "invalid_server_url",
                       "'server_url' must be an http(s) origin such as https://cremind.example.org:1180.")
    return f"{parts.scheme}://{parts.netloc}"


def _ca_pin(origin: str) -> str | None:
    """SHA-256 of Cremind's own CA certificate (DER) when ``origin`` is HTTPS
    and this server made its own CA — Connect then trusts exactly that CA."""
    if not origin.startswith("https://"):
        return None
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives.serialization import Encoding

        from app.config.settings import BaseConfig
        from app.config.tls_auto import tls_dir

        path = os.path.join(tls_dir(BaseConfig.CREMIND_SYSTEM_DIR), "ca.pem")
        with open(path, "rb") as handle:
            cert = x509.load_pem_x509_certificate(handle.read())
        return hashlib.sha256(cert.public_bytes(Encoding.DER)).hexdigest()
    except Exception:  # noqa: BLE001 - no own CA (public certificate, plain HTTP, or none yet)
        return None


def launch_url(session_id: str, token: str, origin: str) -> str:
    url = (f"{LINK_SCHEME}://setup?v=1&server={quote(origin, safe='')}"
           f"&session={session_id}&token={token}")
    pin = _ca_pin(origin)
    return url + (f"&pin={pin}" if pin else "")


def verification_phrase(server_nonce: str, installation_pub: str, session_id: str) -> str:
    digest = hmac.new(bytes.fromhex(server_nonce),
                      b"cremind-connect/v1/phrase" + bytes.fromhex(installation_pub) + session_id.encode(),
                      hashlib.sha256).digest()
    return " ".join(PHRASE_WORDS[b] for b in digest[:4])


def canonical_body(body: dict[str, Any]) -> bytes:
    clean = {k: v for k, v in body.items() if k != "proof"}
    return json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def proof_message(action: str, session_id: str, server_nonce: str, body: dict[str, Any]) -> bytes:
    return (PROOF_PREFIX + action.encode() + b"\x00" + session_id.encode() + b"\x00"
            + (server_nonce or "").encode() + b"\x00" + hashlib.sha256(canonical_body(body)).digest())


def check_proof(public_key_hex: str, proof: Any, message: bytes) -> bool:
    if not isinstance(proof, str) or not _HEX64.match(public_key_hex or ""):
        return False
    try:
        signature = bytes.fromhex(proof)
    except ValueError:
        try:
            signature = base64.b64decode(proof, validate=True)
        except (binascii.Error, ValueError):
            return False
    if len(signature) != 64:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex)).verify(signature, message)
        return True
    except (InvalidSignature, ValueError):
        return False


def _iso(ms: float | None) -> str | None:
    from app.tags.cards import iso

    return iso(ms) if ms is not None else None


def _short(device_id_hex: str | None) -> str | None:
    if not device_id_hex:
        return None
    try:
        return f"{v2.short_id(bytes.fromhex(device_id_hex)):08X}"
    except ValueError:
        return None


def session_json(row: Any) -> dict[str, Any]:
    r = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    gateway = r.get("gateway") or None
    computer = r.get("computer") or None
    return {
        "id": r["id"],
        "operation": r["operation"],
        "state": r["state"],
        "expires_at": _iso(r["expires_at"]),
        "created_at": _iso(r["created_at"]),
        "computer": ({"installation_id": r.get("installation_id"), **computer} if computer else None),
        "verification_phrase": r.get("verification_phrase"),
        "gateway": ({"device_id": gateway.get("device_id"), "short_id": _short(gateway.get("device_id")),
                     "fw": gateway.get("fw"), "usable": True} if gateway else None),
        "native_approved": r.get("native_approved_at") is not None,
        "browser_confirmed": r.get("browser_confirmed_at") is not None,
        "companion_id": r.get("companion_id"),
        "operation_id": r.get("operation_id"),
        "error": r.get("error"),
    }


async def _profile_uuid(conn, profile: str) -> str:
    row = (await conn.execute(select(ProfileModel.id).where(ProfileModel.name == profile))).first()
    if row is None:
        raise TagError(404, "profile_not_found", "The profile no longer exists.")
    return str(row[0])


def _expire_values(row: Any, now: float) -> dict[str, Any] | None:
    if row.state in PRE_REDEEM and float(row.expires_at) <= now:
        return {"state": "expired", "updated_at": now}
    return None


async def _refresh(conn, row: Any, now: float) -> Any:
    values = _expire_values(row, now)
    if values:
        await conn.execute(update(SESSIONS).where(SESSIONS.c.id == row.id, SESSIONS.c.state == row.state)
                           .values(**values))
        row = (await conn.execute(select(SESSIONS).where(SESSIONS.c.id == row.id))).first()
    return row


# ── profile side ──


async def create_session(profile: str, *, operation: Any, server_url: Any, companion_id: Any = None,
                         authorization: str | None, recovery_operation_id: str | None = None,
                         ) -> tuple[dict[str, Any], str]:
    """Create a session; returns ``(session, launch_url)``."""
    if operation not in OPERATIONS:
        raise TagError(422, "invalid_operation", f"'operation' must be one of: {', '.join(OPERATIONS)}.")
    origin = server_origin(server_url)
    token = secrets.token_urlsafe(32)
    now = now_ms()
    sid = str(uuid.uuid4())
    store = get_tag_storage()
    async with store.engine.begin() as conn:
        profile_id = await _profile_uuid(conn, profile)
        target = None
        if operation == "recover":
            from app.tags.ownership import owned_companion_row

            target = await owned_companion_row(conn, profile, profile_id, companion_id)
            if not target.gateway_device_id:
                raise TagError(409, "nothing_to_recover", "This connection has no gateway to recover yet.")
        await conn.execute(insert(SESSIONS), [{
            "id": sid, "operation": operation, "state": "waiting_for_connect", "owner_profile": profile,
            "owner_profile_id": profile_id, "session_fingerprint": fingerprint_token(authorization),
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(), "server_origin": origin,
            "server_nonce": None, "installation_id": None, "installation_pub": None, "computer": None,
            "verification_phrase": None, "gateway": None,
            "companion_id": target.id if target is not None else None,
            "operation_id": recovery_operation_id, "native_approved_at": None, "browser_confirmed_at": None,
            "redeemed_at": None, "redeem_key": None, "result": None, "error": None,
            "created_at": now, "updated_at": now, "expires_at": now + SESSION_TTL_MS,
        }])
        row = (await conn.execute(select(SESSIONS).where(SESSIONS.c.id == sid))).first()
    return session_json(row), launch_url(sid, token, origin)


async def _own_session(conn, profile: str, session_id: str) -> Any:
    from app.tags.storage import begin_write

    await begin_write(conn)
    row = (await conn.execute(select(SESSIONS).where(
        SESSIONS.c.id == session_id, SESSIONS.c.owner_profile == profile))).first()
    if row is None:
        raise TagError(404, "session_not_found", "No setup session with that id.")
    return row


async def get_session(profile: str, session_id: str) -> dict[str, Any]:
    async with get_tag_storage().engine.begin() as conn:
        row = await _refresh(conn, await _own_session(conn, profile, session_id), now_ms())
    return session_json(row)


async def confirm_session(profile: str, session_id: str, *, authorization: str | None) -> dict[str, Any]:
    """The browser confirms the computer, gateway and phrase the native
    window shows. Same profile AND the same sign-in that created the session."""
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        row = await _refresh(conn, await _own_session(conn, profile, session_id), now)
        if not hmac.compare_digest(row.session_fingerprint, fingerprint_token(authorization)):
            raise TagError(403, "session_mismatch", "Confirm from the window that started this setup.")
        if row.state == "expired":
            raise TagError(410, "session_expired", "This setup expired; start again.")
        if row.state in ("redeeming", "connecting", "completed") and row.browser_confirmed_at is not None:
            return session_json(row)
        if row.state != "waiting_for_confirmation":
            raise TagError(409, "not_approved", "Approve in the Cremind Connect window first.")
        await conn.execute(update(SESSIONS).where(SESSIONS.c.id == row.id, SESSIONS.c.state == row.state)
                           .values(state="redeeming", browser_confirmed_at=now, updated_at=now))
        row = (await conn.execute(select(SESSIONS).where(SESSIONS.c.id == row.id))).first()
    return session_json(row)


async def cancel_session(profile: str, session_id: str) -> dict[str, Any]:
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        row = await _refresh(conn, await _own_session(conn, profile, session_id), now)
        if row.state in PRE_REDEEM:
            await conn.execute(update(SESSIONS).where(SESSIONS.c.id == row.id, SESSIONS.c.state == row.state)
                               .values(state="cancelled", updated_at=now))
            if row.operation == "recover" and row.operation_id:
                from app.tags.operations import cancel_waiting_recovery

                await cancel_waiting_recovery(conn, row.operation_id, now)
        elif row.redeemed_at is not None:
            raise TagError(409, "already_redeemed",
                           "Connect already finished this step; remove the device to undo it.")
        row = (await conn.execute(select(SESSIONS).where(SESSIONS.c.id == row.id))).first()
    return session_json(row)


async def active_sessions(profile: str) -> list[dict[str, Any]]:
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        rows = (await conn.execute(select(SESSIONS).where(
            SESSIONS.c.owner_profile == profile, SESSIONS.c.state.notin_(FINAL),
        ).order_by(SESSIONS.c.created_at.desc()).limit(20))).all()
        out = []
        for row in rows:
            row = await _refresh(conn, row, now)
            if row.state not in FINAL:
                out.append(session_json(row))
    return out


# ── bootstrap (Connect) ──


def parse_authorization(header: str | None) -> tuple[str, str] | None:
    raw = (header or "").strip()
    scheme, _, rest = raw.partition(" ")
    if scheme.lower() != SCHEME.lower():
        return None
    session_id, sep, token = rest.strip().partition(".")
    if not sep:
        return None
    try:
        uuid.UUID(session_id)
    except ValueError:
        return None
    if not _TOKEN.match(token):
        return None
    return session_id, token


async def _bootstrap_row(conn, header: str | None, session_id: str) -> Any:
    from app.tags.storage import begin_write

    parsed = parse_authorization(header)
    if parsed is None or parsed[0] != session_id:
        raise TagError(401, "invalid_setup_credential", "Send 'Authorization: CremindSetup <session>.<token>'.")
    await begin_write(conn)
    row = (await conn.execute(select(SESSIONS).where(SESSIONS.c.id == session_id).with_for_update())).first()
    expected = row.token_sha256 if row is not None else hashlib.sha256(b"cremind-setup-unknown").hexdigest()
    if not hmac.compare_digest(hashlib.sha256(parsed[1].encode()).hexdigest(), expected) or row is None:
        raise TagError(401, "invalid_setup_credential", "This setup link is not valid.")
    return row


def _need_proof(row: Any, action: str, body: dict[str, Any]) -> None:
    if not check_proof(row.installation_pub or "", body.get("proof"),
                       proof_message(action, row.id, row.server_nonce or "", body)):
        raise TagError(401, "invalid_proof", "The request was not signed by the bound Cremind Connect.")


def _open_for(row: Any, now: float, *states: str) -> None:
    if row.state in PRE_REDEEM and float(row.expires_at) <= now:
        raise TagError(410, "session_expired", "This setup expired; start again from the Cremind page.")
    if row.state == "cancelled":
        raise TagError(410, "session_cancelled", "This setup was cancelled on the Cremind page.")
    if states and row.state not in states:
        raise TagError(409, "wrong_state", f"The setup is {row.state.replace('_', ' ')}.", state=row.state)


def _installation(body: dict[str, Any]) -> dict[str, str]:
    inst = body.get("installation")
    if not isinstance(inst, dict):
        raise TagError(422, "invalid_installation", "'installation' is required.")
    pub = str(inst.get("public_key") or "").lower()
    iid = str(inst.get("id") or "").lower()
    if not _HEX64.match(pub) or not _HEX32.match(iid):
        raise TagError(422, "invalid_installation", "'installation' needs an id and an Ed25519 public key.")
    if hashlib.sha256(bytes.fromhex(pub)).digest()[:16].hex() != iid:
        raise TagError(422, "invalid_installation", "The installation id does not match its key.")
    platform = str(inst.get("platform") or "other").lower()
    return {
        "id": iid, "public_key": pub, "computer": str(inst.get("computer") or "")[:255].strip() or "This computer",
        "platform": platform if platform in PLATFORMS else "other", "version": str(inst.get("version") or "")[:64],
    }


async def bind(session_id: str, header: str | None, body: dict[str, Any]) -> dict[str, Any]:
    from app.tags.authority import AuthorityUnavailable, get_authority
    from app.tags.ownership import touch_installation

    try:
        authority = await get_authority()
    except AuthorityUnavailable as exc:
        raise TagError(503, exc.code, exc.message) from None
    inst = _installation(body)
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        row = await _bootstrap_row(conn, header, session_id)
        if row.installation_pub and row.installation_pub != inst["public_key"]:
            raise TagError(409, "already_bound", "Another Cremind Connect already answered this setup.")
        if not check_proof(inst["public_key"], body.get("proof"), proof_message("bind", row.id, "", body)):
            raise TagError(401, "invalid_proof", "The bind request was not signed by that installation key.")
        if not row.installation_pub:
            _open_for(row, now, "waiting_for_connect")
            nonce = secrets.token_hex(16)
            phrase = verification_phrase(nonce, inst["public_key"], row.id)
            await conn.execute(update(SESSIONS).where(SESSIONS.c.id == row.id).values(
                installation_id=inst["id"], installation_pub=inst["public_key"], server_nonce=nonce,
                verification_phrase=phrase,
                computer={"name": inst["computer"], "platform": inst["platform"], "version": inst["version"]},
                state="completed" if row.operation == "probe" else "waiting_for_approval", updated_at=now))
            await touch_installation(conn, inst, now)
            row = (await conn.execute(select(SESSIONS).where(SESSIONS.c.id == row.id))).first()
        recover = None
        if row.operation == "recover" and row.companion_id:
            from app.tags.ownership import companion_summary

            recover = await companion_summary(conn, row.companion_id)
        profile_id = row.owner_profile_id
    return {
        "session": {"id": row.id, "operation": row.operation, "state": row.state,
                    "expires_at": _iso(row.expires_at)},
        "server": {"installation_id": authority.installation_id, "name": "Cremind", "origin": row.server_origin,
                   "authority_pub": authority.authority_pub.hex(), "authority_id": authority.authority_id.hex()},
        "profile": {"name": row.owner_profile, "id": profile_id},
        "verification_phrase": row.verification_phrase,
        "server_nonce": row.server_nonce,
        "recover": recover,
    }


async def poll(session_id: str, header: str | None, proof: str | None) -> dict[str, Any]:
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        row = await _bootstrap_row(conn, header, session_id)
        if not row.installation_pub:
            raise TagError(409, "not_bound", "Bind the session first.")
        _need_proof(row, "poll", {"proof": proof})
        row = await _refresh(conn, row, now)
    return {"state": row.state, "native_approved": row.native_approved_at is not None,
            "browser_confirmed": row.browser_confirmed_at is not None, "error": row.error}


def _gateway(body: dict[str, Any]) -> dict[str, Any]:
    gw = body.get("gateway")
    if not isinstance(gw, dict):
        raise TagError(422, "invalid_gateway", "'gateway' is required.")
    device = str(gw.get("device_id") or "").lower()
    ik = str(gw.get("ik") or "").lower()
    proto = gw.get("proto")
    if not isinstance(proto, int) or isinstance(proto, bool) or proto < v2.SECURE_PROTO_VERSION:
        raise TagError(422, "v1_firmware",
                       "This gateway runs older firmware that cannot be set up this way. Update its firmware.")
    if not _HEX32.match(device) or not _HEX64.match(ik):
        raise TagError(422, "invalid_gateway", "The gateway identity is malformed.")
    if gw.get("role", 1) != v2.ROLES["gateway"]:
        raise TagError(422, "not_a_gateway", "The device on this port is not a gateway.")
    if v2.device_id("gateway", bytes.fromhex(ik)).hex() != device:
        raise TagError(422, "invalid_gateway", "The gateway identity does not match its key.")
    gen = gw.get("gen", 0)
    owner_state = gw.get("owner_state", 0)
    authority = str(gw.get("authority_id") or "").lower() or None
    if not isinstance(gen, int) or isinstance(gen, bool) or not 0 <= gen < 0xFFFFFFFF:
        raise TagError(422, "invalid_gateway", "'gen' must be a u32.")
    if owner_state not in (0, 1, 2) or (authority is not None and not _HEX32.match(authority)):
        raise TagError(422, "invalid_gateway", "The gateway's ownership report is malformed.")
    return {"device_id": device, "ik": ik, "proto": proto, "gen": gen, "owner_state": owner_state,
            "authority_id": authority, "fw": str(gw.get("fw") or "")[:32], "board": gw.get("board")
            if isinstance(gw.get("board"), int) and not isinstance(gw.get("board"), bool) else None}


async def approve(session_id: str, header: str | None, body: dict[str, Any]) -> dict[str, Any]:
    from app.tags.authority import AuthorityUnavailable, get_authority
    from app.tags.ownership import check_gateway_available

    try:
        authority = await get_authority()
    except AuthorityUnavailable as exc:
        raise TagError(503, exc.code, exc.message) from None
    gateway = _gateway(body)
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        row = await _bootstrap_row(conn, header, session_id)
        _need_proof(row, "approve", body)
        if row.state == "waiting_for_confirmation" and (row.gateway or {}).get("device_id") == gateway["device_id"]:
            return {"state": row.state}
        _open_for(row, now, "waiting_for_approval")
        if row.operation == "probe":
            raise TagError(409, "wrong_state", "A probe has nothing to approve.")
        gateway["mode"] = await check_gateway_available(conn, row, gateway, authority)
        await conn.execute(update(SESSIONS).where(SESSIONS.c.id == row.id).values(
            gateway=gateway, native_approved_at=now, state="waiting_for_confirmation", updated_at=now))
    return {"state": "waiting_for_confirmation"}


async def redeem(session_id: str, header: str | None, body: dict[str, Any]) -> dict[str, Any]:
    from app.tags.authority import AuthorityUnavailable, get_authority
    from app.tags.ownership import redeem_connect, redeem_recover

    try:
        authority = await get_authority()
    except AuthorityUnavailable as exc:
        raise TagError(503, exc.code, exc.message) from None
    key = body.get("idempotency_key")
    controller = str(body.get("controller_pub") or "").lower()
    creds_in = body.get("credentials") if isinstance(body.get("credentials"), dict) else {}
    hw_hash = str(creds_in.get("hardware_sha256") or "").lower()
    ct_hash = str(creds_in.get("content_sha256") or "").lower()
    if not isinstance(key, str) or not 8 <= len(key) <= 64:
        raise TagError(422, "invalid_idempotency_key", "'idempotency_key' is required (8 to 64 characters).")
    if not _HEX64.match(controller) or not _HEX64.match(hw_hash) or not _HEX64.match(ct_hash) or hw_hash == ct_hash:
        raise TagError(422, "invalid_redeem", "'controller_pub' and two different credential hashes are required.")
    now = now_ms()
    async with get_tag_storage().engine.begin() as conn:
        row = await _bootstrap_row(conn, header, session_id)
        _need_proof(row, "redeem", body)
        if row.redeemed_at is not None:
            if row.redeem_key != key:
                raise TagError(409, "already_redeemed", "This setup was already completed by another request.")
            return dict(row.result or {})
        _open_for(row, now)
        if row.state != "redeeming":
            raise TagError(409, "not_confirmed", "Waiting for the Cremind page to confirm.")
        if row.operation == "connect_gateway":
            result = await redeem_connect(conn, row, controller, hw_hash, ct_hash, authority, now)
        elif row.operation == "recover":
            result = await redeem_recover(conn, row, controller, hw_hash, ct_hash, authority, now)
        else:
            raise TagError(409, "wrong_state", "A probe has nothing to redeem.")
        result = {
            **result,
            "profile": {"name": row.owner_profile, "id": row.owner_profile_id},
            "server": {"installation_id": authority.installation_id, "origin": row.server_origin,
                       "authority_pub": authority.authority_pub.hex(), "authority_id": authority.authority_id.hex()},
        }
        await conn.execute(update(SESSIONS).where(SESSIONS.c.id == row.id).values(
            state="connecting", redeemed_at=now, redeem_key=key, result=result,
            companion_id=result["companion_id"], operation_id=result["operation_id"], updated_at=now))
    from app.tags.storage import notify_commands

    notify_commands([result["companion_id"]])
    return result


async def fail(session_id: str, header: str | None, body: dict[str, Any]) -> dict[str, Any]:
    now = now_ms()
    code = str(body.get("code") or "connect_failed")[:64]
    message = str(body.get("message") or "Cremind Connect could not finish the setup.")[:400]
    async with get_tag_storage().engine.begin() as conn:
        row = await _bootstrap_row(conn, header, session_id)
        _need_proof(row, "fail", body)
        if row.state in PRE_REDEEM:
            await conn.execute(update(SESSIONS).where(SESSIONS.c.id == row.id).values(
                state="failed", error={"code": code, "message": message}, updated_at=now))
            if row.operation == "recover" and row.operation_id:
                from app.tags.operations import cancel_waiting_recovery

                await cancel_waiting_recovery(conn, row.operation_id, now)
            return {"state": "failed"}
    return {"state": row.state}


async def on_worker_heartbeat(conn, companion_id: str, now: float) -> None:
    """The first authenticated heartbeat after a claim completes the setup."""
    rows = (await conn.execute(select(SESSIONS).where(
        SESSIONS.c.companion_id == companion_id, SESSIONS.c.state == "connecting"))).all()
    for row in rows:
        if not row.operation_id:
            continue
        from app.tags.operations import OPERATIONS as OPS_TABLE

        op = (await conn.execute(select(OPS_TABLE.c.state, OPS_TABLE.c.kind).where(
            OPS_TABLE.c.id == row.operation_id))).first()
        if op is None:
            continue
        if op.state == "succeeded" or (op.kind == "recover_gateway" and op.state in ("running", "pending_device")):
            await conn.execute(update(SESSIONS).where(SESSIONS.c.id == row.id, SESSIONS.c.state == "connecting")
                               .values(state="completed", updated_at=now))
        elif op.state in ("failed", "cancelled"):
            await conn.execute(update(SESSIONS).where(SESSIONS.c.id == row.id, SESSIONS.c.state == "connecting")
                               .values(state="failed", error={"code": "claim_failed",
                                                              "message": "The gateway could not be connected."},
                                       updated_at=now))


__all__ = [
    "OPERATIONS", "SCHEME", "active_sessions", "approve", "bind", "cancel_session", "confirm_session",
    "create_session", "fail", "fingerprint_token", "get_session", "launch_url", "on_worker_heartbeat",
    "parse_authorization", "poll", "proof_message", "redeem", "server_origin", "session_json",
    "verification_phrase",
]
