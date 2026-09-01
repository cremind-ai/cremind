"""Channels API: register, list, update, delete external messaging channels.

The catalog is read from ``app/config/channels/*.toml``; secrets declared with
``secret = true`` in the TOML are redacted in ``GET`` responses but persisted
verbatim in ``ChannelModel.config``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import shutil
import uuid
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route

from app.channels import get_channel_registry
from app.channels.notification_filter import normalize_notification_filter
from app.config import load_all_channel_catalogs
from app.storage.conversation_storage import ConversationStorage
from app.utils.logger import logger


_REDACTED = "***"


async def _parse_send_body(
    request: Request, profile: str,
) -> tuple[dict, list[dict], str | None, JSONResponse | None]:
    """Parse a ``/notify`` / ``/message`` body in either of its two forms.

    Returns ``(payload, attachments, spool_dir, error)``. JSON bodies stay as
    before, plus an optional ``attachments`` array of ABSOLUTE server paths —
    validated against the profile's own roots, exactly the boundary the agent's
    tools enforce. ``multipart/form-data`` (the remote-CLI form: the caller has
    no server filesystem to point at) carries the same JSON in a ``payload``
    field plus any number of file parts, written into a throwaway spool inside
    ``uploads_tmp`` — inside on purpose, so the boot wipe and the idle pruner
    cover a crash-leaked spool. The CALLER removes ``spool_dir`` in a
    ``finally`` after delivery.
    """
    from app.channels.attachments import validate_outbound_paths
    from app.utils.uploads_tmp import max_upload_bytes, uploads_tmp_root

    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith("multipart/"):
        from app.api.files import _write_upload

        try:
            form = await request.form()
        except Exception:  # noqa: BLE001
            return {}, [], None, JSONResponse(
                {"error": "Invalid multipart body"}, status_code=400,
            )
        raw_payload = form.get("payload")
        payload: dict = {}
        if raw_payload:
            try:
                parsed = json.loads(str(raw_payload))
                payload = parsed if isinstance(parsed, dict) else {}
            except Exception:  # noqa: BLE001
                return {}, [], None, JSONResponse(
                    {"error": "'payload' must be a JSON object"}, status_code=400,
                )
        spool_dir = os.path.join(uploads_tmp_root(profile), f"outbound-{uuid.uuid4()}")
        os.makedirs(spool_dir, exist_ok=True)
        attachments: list[dict] = []
        cap = max_upload_bytes()
        for value in form.values():
            if not hasattr(value, "filename") or not getattr(value, "filename", None):
                continue
            result = await _write_upload(value, spool_dir, max_bytes=cap)
            if result.get("status") == "error":
                shutil.rmtree(spool_dir, ignore_errors=True)
                return {}, [], None, JSONResponse(
                    {"error": "Upload failed", "detail": result}, status_code=400,
                )
            attachments.append({
                "path": result["path"],
                "name": result.get("saved_as") or result.get("name"),
            })
        return payload, attachments, spool_dir, None

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return {}, [], None, JSONResponse(
            {"error": "Invalid JSON body"}, status_code=400,
        )
    if not isinstance(payload, dict):
        return {}, [], None, JSONResponse(
            {"error": "Body must be a JSON object"}, status_code=400,
        )
    raw_attachments = payload.get("attachments") or []
    if raw_attachments:
        ok, rejected = validate_outbound_paths(profile, raw_attachments)
        if rejected:
            return {}, [], None, JSONResponse(
                {
                    "error": "Invalid attachments",
                    "message": (
                        "Attachment paths must be existing files inside this "
                        "profile's own directories."
                    ),
                    "rejected": rejected,
                },
                status_code=400,
            )
        return payload, ok, None, None
    return payload, [], None, None


def _require_auth(request: Request):
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _secret_field_names(catalog_entry: dict | None) -> set[str]:
    """Return the set of field names declared ``secret = true`` across all modes."""
    secrets: set[str] = set()
    if not catalog_entry:
        return secrets
    for mode in (catalog_entry.get("channel") or {}).get("modes") or []:
        for fname, fdef in (mode.get("fields") or {}).items():
            if isinstance(fdef, dict) and fdef.get("secret"):
                secrets.add(fname)
    return secrets


def _redact(channel: dict, catalog: dict[str, dict]) -> dict:
    catalog_entry = catalog.get(channel["channel_type"])
    secrets = _secret_field_names(catalog_entry)
    if not secrets:
        return channel
    cfg = dict(channel.get("config") or {})
    for k in list(cfg.keys()):
        if k in secrets and cfg[k]:
            cfg[k] = _REDACTED
    return {**channel, "config": cfg}


def _validate_subscribe_auth(config: dict) -> str | None:
    """Return an error message if ``config.subscribe_auth`` is invalid, else None.

    Absent/empty is valid (defaults to ``open``). Only meaningful for
    notification-mode channels; callers gate on mode before calling.
    """
    from app.channels.notification_delivery import SUBSCRIBE_AUTH_METHODS

    val = config.get("subscribe_auth")
    if val in (None, ""):
        return None
    if str(val).strip().lower() not in SUBSCRIBE_AUTH_METHODS:
        return (
            "subscribe_auth must be one of "
            f"{', '.join(SUBSCRIBE_AUTH_METHODS)}"
        )
    return None


def _validate_group_chats_enabled(config: dict, channel_type: str) -> str | None:
    """Return an error message if ``config.group_chats_enabled`` is invalid.

    Absent is valid (off). ``True`` on a platform whose adapter cannot take part
    in a group is refused rather than stored: the toggle would sit on in the UI
    while nothing ever happened, which reads as a Cremind bug instead of as the
    wrong platform.
    """
    val = config.get("group_chats_enabled")
    if val is None:
        return None
    if not isinstance(val, bool):
        return "group_chats_enabled must be true or false"
    if not val:
        return None
    supported = {t["channel_type"] for t in _group_capable_types()}
    if channel_type not in supported:
        return (
            f"{channel_type} channels cannot take part in group chats; "
            f"supported: {', '.join(sorted(supported)) or 'none'}"
        )
    return None


def _group_capable_types() -> list[dict]:
    """Channel types whose adapters can take part in a platform group.

    Guarded because this module is imported where there is no channel subsystem
    (the CLI, tests): a missing adapter catalogue costs the settings page its
    toggle, never a 500.
    """
    try:
        from app.channels.registry import group_capable_channel_types

        return group_capable_channel_types()
    except Exception:  # noqa: BLE001
        logger.exception("channels: could not list the group-capable types")
        return []


def _decorate(channel: dict) -> dict:
    """Add live-runtime ``status`` derived from the registry + persisted state.

    A persisted ``state.link_status == "unlinked"`` marker (set by
    :meth:`BaseChannelAdapter._mark_unlinked` when the platform reports a
    remote-side logout) takes precedence over the live registry status.
    Without that precedence a cold-start after an unlink would briefly
    report ``"stopped"`` before the user sees the durable ``"unlinked"``.
    """
    state = channel.get("state") or {}
    if state.get("link_status") == "unlinked":
        return {**channel, "status": "unlinked"}
    try:
        status = get_channel_registry().status_for(channel["id"])
    except RuntimeError:
        status = "stopped"
    return {**channel, "status": status}


async def create_channel_for_profile(
    conversation_storage: ConversationStorage,
    profile: str,
    payload: dict,
) -> tuple[dict | None, dict | None]:
    """Validate a CreateChannelPayload-shaped dict and persist a channel.

    Shared between the auth-guarded ``POST /api/channels`` handler and the
    setup wizard's ``/api/config/setup`` flow, where channels need to be
    created pre-token alongside the new profile. Returns the raw channel
    row on success (caller is responsible for redaction); on validation
    failure returns an error dict shaped ``{"error": str, "status": int}``
    so callers can either map it to a JSONResponse (single-channel API)
    or accumulate it (batch setup).

    The adapter is started best-effort when ``enabled=True``; start
    failures are logged but do not turn a successful row creation into an
    error, mirroring the long-standing behaviour of POST /api/channels.
    """
    channel_type = (payload.get("channel_type") or "").strip()
    if not channel_type or channel_type == "main":
        return None, {"error": "Invalid channel_type", "status": 400}

    catalog = load_all_channel_catalogs()
    catalog_entry = catalog.get(channel_type)
    if not catalog_entry:
        return None, {
            "error": f"Unknown channel_type: {channel_type!r}",
            "status": 400,
        }

    existing = await conversation_storage.get_channel_by_type(profile, channel_type)
    if existing:
        return None, {
            "error": f"Channel {channel_type!r} is already registered for this profile",
            "status": 409,
        }

    info = catalog_entry.get("channel") or {}
    modes = info.get("modes") or []
    mode = (payload.get("mode") or (modes[0]["id"] if modes else "bot")).strip()
    valid_modes = {m["id"] for m in modes}
    if valid_modes and mode not in valid_modes:
        return None, {
            "error": f"Invalid mode {mode!r} for {channel_type}",
            "status": 400,
        }

    chosen_mode_meta = next((m for m in modes if m["id"] == mode), None)
    if chosen_mode_meta and chosen_mode_meta.get("implemented") is False:
        return None, {
            "error": "Mode not implemented",
            "status": 400,
        }

    auth_modes = info.get("auth_modes") or ["none"]
    auth_mode = (payload.get("auth_mode") or auth_modes[0]).strip()
    if auth_mode not in set(auth_modes):
        return None, {
            "error": f"Invalid auth_mode {auth_mode!r} for {channel_type}",
            "status": 400,
        }

    response_mode = (
        payload.get("response_mode")
        or info.get("default_response_mode")
        or "normal"
    )
    if response_mode not in ("detail", "normal"):
        return None, {
            "error": "response_mode must be 'detail' or 'normal'",
            "status": 400,
        }

    config = payload.get("config") or {}
    if not isinstance(config, dict):
        return None, {"error": "config must be an object", "status": 400}

    if chosen_mode_meta:
        for fname, fdef in (chosen_mode_meta.get("fields") or {}).items():
            if not isinstance(fdef, dict):
                continue
            if fdef.get("required") and not config.get(fname):
                return None, {
                    "error": f"Missing required field {fname!r}",
                    "status": 400,
                }

    # Notification-mode channels carry a structured filter that simple TOML
    # fields can't express; validate + normalize it before persisting.
    if mode == "notification" and "notification_filter" in config:
        try:
            config = {
                **config,
                "notification_filter": normalize_notification_filter(
                    config.get("notification_filter"),
                ),
            }
        except ValueError as exc:
            return None, {"error": f"Invalid notification_filter: {exc}", "status": 400}

    # ``subscribe_auth`` is the unified access-auth setting for every mode
    # (conversational + notification); validate it whenever present.
    auth_err = _validate_subscribe_auth(config)
    if auth_err:
        return None, {"error": auth_err, "status": 400}

    group_err = _validate_group_chats_enabled(config, channel_type)
    if group_err:
        return None, {"error": group_err, "status": 400}

    enabled = bool(payload.get("enabled", True))

    ch = await conversation_storage.create_channel(
        profile=profile,
        channel_type=channel_type,
        mode=mode,
        auth_mode=auth_mode,
        response_mode=response_mode,
        enabled=enabled,
        config=config,
    )

    if enabled:
        try:
            # ``install_if_missing`` pip-installs the channel's optional SDK
            # extras at runtime before the adapter starts (Telegram/Discord/
            # Slack) so a post-setup connect doesn't fail with a missing
            # package — mirrors the browser/claude_code tools.
            ch = await get_channel_registry().start_for_channel(
                ch, install_if_missing=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception(f"channels: failed to start {ch['id']}")

    return ch, None


def get_channel_routes(conversation_storage: ConversationStorage) -> list[Route]:

    async def handle_get_catalog(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        catalog = load_all_channel_catalogs()
        # Which of these can take part in a platform group. Derived from the
        # adapter classes rather than declared in the TOML, so a transport that
        # gains (or loses) group support cannot leave the settings page offering
        # a toggle that does nothing.
        group_capable = {t["channel_type"] for t in _group_capable_types()}
        decorated = {
            channel_type: {
                **entry,
                "channel": {
                    **(entry.get("channel") or {}),
                    "supports_group_chats": channel_type in group_capable,
                },
            }
            for channel_type, entry in (catalog or {}).items()
        }
        return JSONResponse({"channels": decorated})

    async def handle_list_channels(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)

        rows = await conversation_storage.list_channels(profile)
        # The implicit ``main`` channel is the system default for web/CLI
        # conversations — it's auto-created on profile setup and is not
        # user-manageable. Every list-channels surface (web UI, CLI) hides it.
        rows = [r for r in rows if r.get("channel_type") != "main"]
        catalog = load_all_channel_catalogs()
        out = [_decorate(_redact(r, catalog)) for r in rows]
        return JSONResponse({"channels": out})

    async def handle_create_channel(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        ch, err = await create_channel_for_profile(conversation_storage, profile, body)
        if err is not None:
            return JSONResponse(
                {"error": err["error"]}, status_code=err["status"],
            )

        catalog = load_all_channel_catalogs()
        return JSONResponse(
            {"channel": _decorate(_redact(ch, catalog))},
            status_code=201,
        )

    async def handle_get_channel(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        cid = request.path_params["channel_id"]
        ch = await conversation_storage.get_channel(cid)
        if not ch:
            return JSONResponse({"error": "Channel not found"}, status_code=404)
        if ch["profile"] != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        catalog = load_all_channel_catalogs()
        return JSONResponse({"channel": _decorate(_redact(ch, catalog))})

    async def handle_update_channel(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        cid = request.path_params["channel_id"]
        ch = await conversation_storage.get_channel(cid)
        if not ch:
            return JSONResponse({"error": "Channel not found"}, status_code=404)
        if ch["profile"] != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        if ch["channel_type"] == "main":
            return JSONResponse(
                {"error": "Cannot modify the main channel"},
                status_code=400,
            )

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        update: dict[str, Any] = {}
        if "mode" in body:
            update["mode"] = body["mode"]
        if "auth_mode" in body:
            update["auth_mode"] = body["auth_mode"]
        if "response_mode" in body:
            if body["response_mode"] not in ("detail", "normal"):
                return JSONResponse(
                    {"error": "response_mode must be 'detail' or 'normal'"},
                    status_code=400,
                )
            update["response_mode"] = body["response_mode"]
        if "enabled" in body:
            update["enabled"] = bool(body["enabled"])
        if "config" in body and isinstance(body["config"], dict):
            # Merge so the UI can patch a single field without resending
            # secrets it never received in the first place.
            merged = dict(ch.get("config") or {})
            for k, v in body["config"].items():
                # Drop redaction sentinels so we don't accidentally overwrite
                # the real secret with "***".
                if v == _REDACTED:
                    continue
                merged[k] = v
            eff_mode = update.get("mode") or ch.get("mode")
            if eff_mode == "notification" and "notification_filter" in merged:
                try:
                    merged["notification_filter"] = normalize_notification_filter(
                        merged.get("notification_filter"),
                    )
                except ValueError as exc:
                    return JSONResponse(
                        {"error": f"Invalid notification_filter: {exc}"},
                        status_code=400,
                    )
            auth_err = _validate_subscribe_auth(merged)
            if auth_err:
                return JSONResponse({"error": auth_err}, status_code=400)
            group_err = _validate_group_chats_enabled(
                merged, ch.get("channel_type") or "",
            )
            if group_err:
                return JSONResponse({"error": group_err}, status_code=400)
            update["config"] = merged

        updated = await conversation_storage.update_channel(cid, **update)
        if updated is None:
            return JSONResponse({"error": "Channel not found"}, status_code=404)

        # Restart adapter when anything that affects runtime changed.
        # ``install_if_missing`` covers the enable/mode-change case where the
        # channel's SDK extras aren't on disk yet (e.g. switching an existing
        # row to a new platform, or re-enabling on a host that never installed
        # them) — install at runtime before the adapter restarts.
        # ``response_mode`` included because a running adapter reads it off the
        # dict snapshot it was constructed with (``BaseChannelAdapter.channel``),
        # which only a restart replaces — without this, flipping "Reply detail"
        # saves and then does nothing until the process happens to restart.
        runtime_keys = {"mode", "auth_mode", "response_mode", "enabled", "config"}
        if any(k in update for k in runtime_keys):
            try:
                updated = await get_channel_registry().restart_for_channel(
                    cid, install_if_missing=True,
                ) or updated
            except Exception:  # noqa: BLE001
                logger.exception(f"channels: restart failed for {cid}")

        catalog = load_all_channel_catalogs()
        return JSONResponse({"channel": _decorate(_redact(updated, catalog))})

    async def handle_delete_channel(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        cid = request.path_params["channel_id"]
        ch = await conversation_storage.get_channel(cid)
        if not ch:
            return JSONResponse({"error": "Channel not found"}, status_code=404)
        if ch["profile"] != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        if ch["channel_type"] == "main":
            return JSONResponse(
                {"error": "Cannot delete the main channel"},
                status_code=400,
            )

        try:
            await get_channel_registry().stop_for_channel(cid)
        except Exception:  # noqa: BLE001
            logger.exception(f"channels: stop failed for {cid}")
        await conversation_storage.delete_channel(cid)
        return JSONResponse({"success": True})

    async def handle_auth_events_stream(request: Request) -> Any:
        """SSE stream of interactive-pairing events for a channel.

        Used by both WhatsApp's QR flow and Telegram userbot's code/2FA flow.

        Frame kinds:
          - ``{"kind": "qr", "qr": "<data-url>"}`` — render as a QR image (WhatsApp).
          - ``{"kind": "code_required", ...}`` — render an input box for the
            verification code (Telegram). Optional ``error`` field if the
            previous attempt was rejected.
          - ``{"kind": "password_required", ...}`` — render an input box for
            the 2FA password (Telegram).
          - ``{"kind": "ready"}`` — pairing complete; UI can dismiss.
          - ``{"kind": "disconnected", "logged_out": bool}`` — session lost.

        On connect, the latest cached event (most-recent QR / code prompt /
        ``ready``) is replayed once before the live tail starts, so a UI
        client opening the page mid-pairing immediately sees current state.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        cid = request.path_params["channel_id"]
        ch = await conversation_storage.get_channel(cid)
        if not ch:
            return JSONResponse({"error": "Channel not found"}, status_code=404)
        if ch["profile"] != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)

        try:
            adapter = get_channel_registry().get_adapter(cid)
        except RuntimeError:
            adapter = None
        if adapter is None:
            return JSONResponse(
                {"error": "Adapter not running"},
                status_code=400,
            )

        queue = adapter.subscribe_auth_events()

        async def generator():
            def _frame(payload: dict) -> bytes:
                return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

            try:
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        # SSE comment line — keeps proxies / browsers from
                        # closing the idle connection.
                        yield b": keepalive\n\n"
                        continue
                    yield _frame(ev)
            finally:
                try:
                    adapter.unsubscribe_auth_events(queue)
                except Exception:  # noqa: BLE001
                    pass

        headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
        return StreamingResponse(
            generator(), media_type="text/event-stream", headers=headers,
        )

    async def handle_auth_input(request: Request) -> JSONResponse:
        """Forward user-typed pairing input (verification code, 2FA password)
        to the running adapter.

        Body shape: ``{"code": "12345"}`` or ``{"password": "..."}``. The
        adapter consumes whatever field is present.

        Returns ``409`` if the adapter has no auth flow currently waiting
        on input — most commonly because pairing already completed or the
        adapter isn't running.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        cid = request.path_params["channel_id"]
        ch = await conversation_storage.get_channel(cid)
        if not ch:
            return JSONResponse({"error": "Channel not found"}, status_code=404)
        if ch["profile"] != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "Body must be a JSON object"}, status_code=400)

        try:
            adapter = get_channel_registry().get_adapter(cid)
        except RuntimeError:
            adapter = None
        if adapter is None:
            return JSONResponse(
                {"error": "Adapter not running"}, status_code=400,
            )

        accepted = adapter.submit_auth_input(payload)
        if not accepted:
            return JSONResponse(
                {
                    "error": "No auth input expected",
                    "message": (
                        "The adapter is not currently waiting for a "
                        "verification code or password."
                    ),
                },
                status_code=409,
            )
        return JSONResponse({"success": True})

    async def handle_list_senders(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        cid = request.path_params["channel_id"]
        ch = await conversation_storage.get_channel(cid)
        if not ch:
            return JSONResponse({"error": "Channel not found"}, status_code=404)
        if ch["profile"] != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        senders = await conversation_storage.list_senders(cid)
        # Token/cost totals per sender, so the admin page can show usage inline
        # instead of making the operator open each conversation's stats panel.
        # One grouped query over the senders' conversations; best-effort, since
        # a usage-table hiccup must not break subscriber management.
        usage_by_conv: dict[str, dict] = {}
        conv_ids = [s["conversation_id"] for s in senders if s.get("conversation_id")]
        if conv_ids:
            try:
                from app.storage import get_usage_storage
                usage_by_conv = await get_usage_storage().rollup_by_conversation(conv_ids)
            except Exception:  # noqa: BLE001
                logger.exception("channels: failed to roll up sender usage")
        # Redact any active OTP code from the list response.
        redacted = [
            {
                **s,
                "pending_otp": _REDACTED if s.get("pending_otp") else None,
                "usage": usage_by_conv.get(s.get("conversation_id")),
            }
            for s in senders
        ]
        return JSONResponse({"senders": redacted})

    async def handle_set_sender_authenticated(request: Request) -> JSONResponse:
        """Update a channel subscriber (operator action).

        Body: any non-empty subset of ``{"authenticated": bool, "phone":
        str|null, "send_confirmation": "required"|"skip"|null}``.
        ``authenticated`` backs the "Approve"/"Revoke" controls in
        the Subscribers UI and ``cremind channels approve/revoke`` for
        notification channels using ``approval`` subscription auth — but is
        mode-agnostic (it just flips the sender's flag).

        ``phone`` records the contact's number so direct sends can address them
        from a list of phone numbers (``cremind channels set-phone``). It is
        the only path allowed to *overwrite* a number — automatic derivation
        only ever fills an empty one — because a corrected mapping must be able
        to win, while a bad auto-derivation must not silently re-route someone's
        messages. Pass ``null`` to clear it.

        ``send_confirmation`` is this client's override of the profile's "confirm
        before messaging clients" setting: ``"skip"`` lets the agent message them
        without stopping to ask (so an unattended automation can reach them),
        ``"required"`` keeps asking even when the profile setting is off, and
        ``null`` inherits. See :mod:`app.channels.send_policy`.

        The sender must already exist (i.e. they have contacted the channel or
        been messaged); returns 404 otherwise, so a typo can't seed a junk row.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        cid = request.path_params["channel_id"]
        sid = request.path_params["sender_id"]
        ch = await conversation_storage.get_channel(cid)
        if not ch:
            return JSONResponse({"error": "Channel not found"}, status_code=404)
        if ch["profile"] != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "Body must be a JSON object"}, status_code=400)

        fields: dict[str, Any] = {}
        authed = body.get("authenticated")
        if authed is not None:
            if not isinstance(authed, bool):
                return JSONResponse(
                    {"error": "'authenticated' must be a boolean"},
                    status_code=400,
                )
            # Approving clears any outstanding OTP challenge so a stale code
            # can't later be replayed; revoking just drops the subscription.
            fields["authenticated"] = authed
            if authed:
                fields["pending_otp"] = None
                fields["pending_otp_expires_at"] = None

        if "phone" in body:
            raw_phone = body.get("phone")
            if raw_phone in (None, ""):
                fields["phone"] = None
            else:
                from app.channels.direct_send import normalize_phone

                normalized = normalize_phone(str(raw_phone))
                if not normalized:
                    return JSONResponse(
                        {
                            "error": "Invalid phone number",
                            "message": (
                                "Give the number in international form, e.g. "
                                "+84901234567."
                            ),
                        },
                        status_code=400,
                    )
                fields["phone"] = normalized

        if "send_confirmation" in body:
            raw_mode = body.get("send_confirmation")
            if raw_mode in (None, ""):
                # Back to inheriting the profile's confirmation setting.
                fields["send_confirmation"] = None
            else:
                from app.channels.send_policy import CONFIRM_VALUES, normalize_override

                normalized = normalize_override(raw_mode)
                if normalized is None:
                    return JSONResponse(
                        {
                            "error": "Invalid send_confirmation",
                            "message": (
                                "Use one of "
                                f"{', '.join(repr(v) for v in CONFIRM_VALUES)}, "
                                "or null to inherit the profile setting."
                            ),
                        },
                        status_code=400,
                    )
                fields["send_confirmation"] = normalized

        if not fields:
            return JSONResponse(
                {
                    "error": (
                        "Nothing to update: pass 'authenticated', 'phone' "
                        "and/or 'send_confirmation'"
                    )
                },
                status_code=400,
            )

        senders = await conversation_storage.list_senders(cid)
        sender = next((s for s in senders if s.get("sender_id") == sid), None)
        if sender is None:
            return JSONResponse({"error": "Sender not found"}, status_code=404)

        updated = await conversation_storage.update_sender(sender["id"], **fields)
        if updated is None:
            return JSONResponse({"error": "Sender not found"}, status_code=404)
        out = {
            **updated,
            "pending_otp": _REDACTED if updated.get("pending_otp") else None,
        }
        return JSONResponse({"sender": out})

    async def handle_clear_sender_history(request: Request) -> JSONResponse:
        """Wipe one channel subscriber's conversation history.

        Deletes every message in the sender's conversation but KEEPS the
        conversation row, so their next message continues in the same
        conversation and the per-sender usage totals (attributed by
        conversation) survive the wipe.

        Deliberately lighter than deleting a conversation: the sender's skill
        events, file watchers, and schedules stay armed — they are homed on the
        surviving conversation row, and silently disarming someone's automations
        as a side effect of clearing chat history would be wrong. Only the
        message-bound artifacts go: queued turns, the replay buffer, plan files.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        cid = request.path_params["channel_id"]
        sid = request.path_params["sender_id"]
        ch = await conversation_storage.get_channel(cid)
        if not ch:
            return JSONResponse({"error": "Channel not found"}, status_code=404)
        if ch["profile"] != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)

        senders = await conversation_storage.list_senders(cid)
        sender = next((s for s in senders if s.get("sender_id") == sid), None)
        if sender is None:
            return JSONResponse({"error": "Sender not found"}, status_code=404)

        conv_id = sender.get("conversation_id")
        if not conv_id:
            # Never spoke, or their conversation was already removed — nothing
            # to clear, and saying so is friendlier than a 404.
            return JSONResponse(
                {"success": True, "conversation_id": None, "cleared_messages": 0}
            )

        from app.events import queue as event_queue
        from app.events.stream_bus import get_event_stream_bus

        bus = get_event_stream_bus()
        if bus.is_active(conv_id):
            return JSONResponse(
                {
                    "error": (
                        "This subscriber has a run in progress. Wait for it to "
                        "finish before clearing their history."
                    ),
                },
                status_code=409,
            )

        # Queued-but-unstarted turns captured the pre-wipe history; drop them
        # along with the replay buffer and the wiped turns' plan files.
        try:
            event_queue.discard_queue(conv_id)
        except Exception:  # noqa: BLE001
            logger.exception(f"channels: failed to discard queue for {conv_id}")
        try:
            await bus.discard(conv_id)
        except Exception:  # noqa: BLE001
            logger.exception(f"channels: failed to discard stream bus for {conv_id}")
        try:
            from app.utils.plans_dir import remove_conversation_plans
            remove_conversation_plans(profile, conv_id)
        except Exception:  # noqa: BLE001
            logger.exception(f"channels: failed to remove plans for {conv_id}")

        cleared = await conversation_storage.clear_conversation_messages(conv_id)

        try:
            from app.events.conversations_list_bus import publish_conversations_changed
            publish_conversations_changed(profile)
        except Exception:  # noqa: BLE001
            logger.exception("channels: failed to publish conversations changed")

        logger.info(
            f"channels: cleared {cleared} message(s) for sender {sid} on channel {cid}"
        )
        return JSONResponse(
            {"success": True, "conversation_id": conv_id, "cleared_messages": cleared}
        )

    async def handle_delete_sender(request: Request) -> JSONResponse:
        """Delete a channel client outright — as if they had never written.

        The full-erasure counterpart of ``handle_clear_sender_history``, which
        deliberately keeps the person and their automations and only wipes
        messages. This removes everything:

        - their conversation, with every message in it, plus the automations
          homed on it (skill events, file watchers, schedules) — disarmed in
          the live managers, not merely dropped from the DB — and the run rows,
          queued turns, replay buffer and plan files that hung off it;
        - the sender row itself: display name, phone, WhatsApp alias, and their
          access state (``authenticated`` and any outstanding OTP);
        - this adapter's in-memory state for them (busy flag, access-request
          memo, inbound lock).

        Afterwards their next message is a genuine first contact: new sender
        row, new conversation, access gate applied from scratch. Usage rows are
        an exception and survive by design — their conversation FK is
        ``ON DELETE SET NULL``, so historical spend stays in the account
        totals; it simply stops being attributed to anyone.

        Refuses with 409 while a run is in progress, rather than deleting rows
        out from under a live turn.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        cid = request.path_params["channel_id"]
        sid = request.path_params["sender_id"]
        ch = await conversation_storage.get_channel(cid)
        if not ch:
            return JSONResponse({"error": "Channel not found"}, status_code=404)
        if ch["profile"] != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)

        senders = await conversation_storage.list_senders(cid)
        sender = next((s for s in senders if s.get("sender_id") == sid), None)
        if sender is None:
            return JSONResponse({"error": "Sender not found"}, status_code=404)

        conv_id = sender.get("conversation_id")
        if conv_id:
            from app.events.stream_bus import get_event_stream_bus

            if get_event_stream_bus().is_active(conv_id):
                return JSONResponse(
                    {
                        "error": (
                            "This client has a run in progress. Wait for it to "
                            "finish before deleting them."
                        ),
                    },
                    status_code=409,
                )

        # A stopped adapter holds no in-memory state, so not having one is fine.
        try:
            adapter = get_channel_registry().get_adapter(cid)
        except RuntimeError:
            adapter = None

        from app.reset._senders import delete_sender_completely

        summary = await delete_sender_completely(
            conversation_storage, channel=ch, sender=sender, adapter=adapter,
        )
        if not summary.get("deleted"):
            return JSONResponse({"error": "Sender not found"}, status_code=404)

        try:
            from app.api.events import publish_skill_events_admin_changed
            from app.api.file_watchers import publish_file_watchers_admin_changed
            from app.events.conversations_list_bus import publish_conversations_changed
            publish_conversations_changed(profile)
            publish_skill_events_admin_changed(profile)
            publish_file_watchers_admin_changed(profile)
        except Exception:  # noqa: BLE001
            logger.exception("channels: failed to publish change events")

        return JSONResponse({"success": True, **summary})

    async def handle_sender_detail_dispatch(request: Request) -> JSONResponse:
        """Dispatch /api/channels/{id}/senders/{sender_id} by HTTP method."""
        if request.method == "PATCH":
            return await handle_set_sender_authenticated(request)
        if request.method == "DELETE":
            return await handle_delete_sender(request)
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    async def handle_notify_channel(request: Request) -> JSONResponse:
        """Push an ad-hoc message OUT to a notification-mode channel.

        Body: JSON ``{"message": "...", "attachments": ["<abs server path>"]?}``
        or ``multipart/form-data`` with a ``payload`` JSON field plus file
        parts (the remote-CLI form — see ``_parse_send_body``). Delivers
        straight to the channel's recipients (configured ``target_chat_ids`` ∪
        authenticated subscribers) via the live adapter's ``deliver_text`` /
        ``deliver_file`` — a direct push that bypasses the channel's
        ``NotificationFilter`` (an explicit, operator-initiated send). Backs
        ``cremind channels send`` and mirrors the agent's ``send_notification``
        tool. Only valid for ``mode == "notification"`` channels with a
        running adapter.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        cid = request.path_params["channel_id"]
        ch = await conversation_storage.get_channel(cid)
        if not ch:
            return JSONResponse({"error": "Channel not found"}, status_code=404)
        if ch["profile"] != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        if (ch.get("mode") or "") != "notification":
            return JSONResponse(
                {
                    "error": "Not a notification channel",
                    "message": (
                        "Ad-hoc sends are only supported on channels in "
                        "notification mode."
                    ),
                },
                status_code=400,
            )

        payload, attachments, spool_dir, err = await _parse_send_body(request, profile)
        if err is not None:
            return err
        try:
            message = str(payload.get("message") or "").strip()
            if not message and not attachments:
                return JSONResponse(
                    {"error": "'message' is required and cannot be empty"},
                    status_code=400,
                )

            try:
                adapter = get_channel_registry().get_adapter(cid)
            except RuntimeError:
                adapter = None
            if adapter is None:
                return JSONResponse(
                    {
                        "error": "Adapter not running",
                        "message": (
                            "The channel is not currently running, so nothing could "
                            "be delivered."
                        ),
                    },
                    status_code=409,
                )

            try:
                recipients = await adapter.deliver_text(message) if message else 0
                files_delivered = 0
                if attachments:
                    for att in attachments:
                        count = await adapter.deliver_file(
                            att["path"], name=att.get("name"),
                        )
                        recipients = max(recipients, count)
                        if count > 0:
                            files_delivered += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"channels: notify send failed for {cid}")
                return JSONResponse(
                    {"error": "Delivery failed", "message": str(exc)},
                    status_code=502,
                )
            body: dict[str, Any] = {
                "delivered": recipients > 0,
                "recipients": recipients,
            }
            if attachments:
                body["files_delivered"] = files_delivered
            return JSONResponse(body)
        finally:
            if spool_dir:
                shutil.rmtree(spool_dir, ignore_errors=True)

    async def handle_send_channel_message(request: Request) -> JSONResponse:
        """Message specific clients on this channel — one or many.

        Body: ``{"recipients": [{"to", "message"?, "name"?}, ...], "message"?,
        "dry_run"?, "default_country_code"?, "attachments"?}`` — or the same
        as multipart with file parts (see ``_parse_send_body``). Unlike ``/notify`` (which
        broadcasts to the channel's own subscribers), this addresses named
        individuals by platform sender id or phone number, registers anyone the
        platform lets us contact cold, and records each delivered message in
        that client's conversation. Backs ``cremind channels message`` and
        mirrors the agent's ``send_channel_message`` tool.

        ``dry_run`` defaults to TRUE: the caller has to ask for a live send
        explicitly, so a mistyped list costs a preview rather than a hundred
        messages to real people.

        Per-recipient failures come back inside a 200 with their own status —
        the request succeeded, some recipients didn't. Only a malformed request
        or a dead adapter is an HTTP error.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        cid = request.path_params["channel_id"]
        ch = await conversation_storage.get_channel(cid)
        if not ch:
            return JSONResponse({"error": "Channel not found"}, status_code=404)
        if ch["profile"] != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)

        payload, attachments, spool_dir, err = await _parse_send_body(request, profile)
        if err is not None:
            return err
        try:
            from app.channels import direct_send

            try:
                recipients = direct_send.normalize_recipients(payload.get("recipients"))
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)

            dry_run = payload.get("dry_run")
            dry_run = True if dry_run is None else bool(dry_run)

            try:
                adapter = get_channel_registry().get_adapter(cid)
            except RuntimeError:
                adapter = None
            if adapter is None:
                return JSONResponse(
                    {
                        "error": "Adapter not running",
                        "message": (
                            "The channel is not currently running, so nothing could "
                            "be delivered."
                        ),
                    },
                    status_code=409,
                )

            try:
                summary = await direct_send.send_direct_messages(
                    adapters=[adapter],
                    storage=conversation_storage,
                    recipients=recipients,
                    message=payload.get("message"),
                    default_country_code=payload.get("default_country_code"),
                    dry_run=dry_run,
                    initiated_by="api",
                    attachments=attachments or None,
                )
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"channels: direct send failed for {cid}")
                return JSONResponse(
                    {"error": "Delivery failed", "message": str(exc)},
                    status_code=502,
                )
            return JSONResponse(summary)
        finally:
            if spool_dir:
                shutil.rmtree(spool_dir, ignore_errors=True)

    async def handle_messenger_webhook(request: Request):
        """Public Facebook Messenger webhook (GET verify + POST receive).

        Intentionally NOT behind ``_require_auth``: Meta calls this endpoint
        directly with no bearer token. Authenticity is instead established by
        the ``hub.verify_token`` challenge (GET) and the ``X-Hub-Signature-256``
        HMAC over the raw body (POST, when an ``app_secret`` is configured).

        Reads the verify token / app secret straight from the stored channel
        config (``storage.get_channel`` returns the row un-redacted), so GET
        verification works even before the adapter is enabled — Meta verifies
        the callback URL at setup time.
        """
        channel_id = request.path_params["channel_id"]
        ch = await conversation_storage.get_channel(channel_id)
        if not ch or ch.get("channel_type") != "messenger":
            return PlainTextResponse("Not found", status_code=404)
        config = ch.get("config") or {}

        if request.method == "GET":
            params = request.query_params
            mode = params.get("hub.mode")
            token = params.get("hub.verify_token")
            challenge = params.get("hub.challenge")
            verify_token = config.get("verify_token")
            if mode == "subscribe" and verify_token and token == verify_token:
                return PlainTextResponse(challenge or "")
            logger.warning(f"messenger[{channel_id}]: webhook verify rejected")
            return PlainTextResponse("Verification failed", status_code=403)

        # POST — inbound message events.
        raw = await request.body()
        app_secret = config.get("app_secret")
        if app_secret:
            sig = request.headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.new(
                str(app_secret).encode(), raw, hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(sig, expected):
                logger.warning(f"messenger[{channel_id}]: bad webhook signature")
                return PlainTextResponse("Bad signature", status_code=403)

        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001
            return PlainTextResponse("Bad request", status_code=400)

        adapter = get_channel_registry().get_adapter(channel_id)
        if payload.get("object") == "page" and hasattr(adapter, "handle_webhook_message"):
            for entry in payload.get("entry") or []:
                for evt in entry.get("messaging") or []:
                    message = evt.get("message") or {}
                    if message.get("is_echo"):
                        continue
                    sender = (evt.get("sender") or {}).get("id")
                    text = message.get("text")
                    attachments = [
                        a for a in (message.get("attachments") or [])
                        if isinstance(a, dict)
                    ]
                    if sender and (text or attachments):
                        try:
                            await adapter.handle_webhook_message(
                                str(sender), str(text or ""),
                                attachments=attachments or None,
                            )
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                f"messenger[{channel_id}]: inbound handling failed",
                            )
        # Meta requires a prompt 200 regardless of processing outcome.
        return PlainTextResponse("EVENT_RECEIVED")

    async def handle_channels_dispatch(request: Request) -> JSONResponse:
        if request.method == "GET":
            return await handle_list_channels(request)
        if request.method == "POST":
            return await handle_create_channel(request)
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    async def handle_channel_detail_dispatch(request: Request) -> JSONResponse:
        if request.method == "GET":
            return await handle_get_channel(request)
        if request.method == "PATCH":
            return await handle_update_channel(request)
        if request.method == "DELETE":
            return await handle_delete_channel(request)
        return JSONResponse({"error": "Method not allowed"}, status_code=405)

    return [
        Route("/api/channels/catalog", endpoint=handle_get_catalog, methods=["GET"]),
        # Public webhook (no auth) — must be declared before the generic
        # ``/api/channels/{channel_id}`` route. Meta hits this directly.
        Route(
            "/api/channels/webhook/messenger/{channel_id}",
            endpoint=handle_messenger_webhook,
            methods=["GET", "POST"],
        ),
        Route(
            "/api/channels",
            endpoint=handle_channels_dispatch,
            methods=["GET", "POST"],
        ),
        Route(
            "/api/channels/{channel_id}",
            endpoint=handle_channel_detail_dispatch,
            methods=["GET", "PATCH", "DELETE"],
        ),
        Route(
            "/api/channels/{channel_id}/senders",
            endpoint=handle_list_senders,
            methods=["GET"],
        ),
        Route(
            "/api/channels/{channel_id}/senders/{sender_id}",
            endpoint=handle_sender_detail_dispatch,
            methods=["PATCH", "DELETE"],
        ),
        Route(
            "/api/channels/{channel_id}/senders/{sender_id}/messages",
            endpoint=handle_clear_sender_history,
            methods=["DELETE"],
        ),
        Route(
            "/api/channels/{channel_id}/qr",
            endpoint=handle_auth_events_stream,
            methods=["GET"],
        ),
        Route(
            "/api/channels/{channel_id}/auth-events",
            endpoint=handle_auth_events_stream,
            methods=["GET"],
        ),
        Route(
            "/api/channels/{channel_id}/auth-input",
            endpoint=handle_auth_input,
            methods=["POST"],
        ),
        Route(
            "/api/channels/{channel_id}/notify",
            endpoint=handle_notify_channel,
            methods=["POST"],
        ),
        Route(
            "/api/channels/{channel_id}/message",
            endpoint=handle_send_channel_message,
            methods=["POST"],
        ),
    ]
