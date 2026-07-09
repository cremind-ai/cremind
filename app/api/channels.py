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
            ch = await get_channel_registry().start_for_channel(ch)
        except Exception:  # noqa: BLE001
            logger.exception(f"channels: failed to start {ch['id']}")

    return ch, None


def get_channel_routes(conversation_storage: ConversationStorage) -> list[Route]:

    async def handle_get_catalog(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        catalog = load_all_channel_catalogs()
        return JSONResponse({"channels": catalog})

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
            update["config"] = merged

        updated = await conversation_storage.update_channel(cid, **update)
        if updated is None:
            return JSONResponse({"error": "Channel not found"}, status_code=404)

        # Restart adapter when anything that affects runtime changed.
        runtime_keys = {"mode", "auth_mode", "enabled", "config"}
        if any(k in update for k in runtime_keys):
            try:
                updated = await get_channel_registry().restart_for_channel(cid) or updated
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
        # Redact any active OTP code from the list response.
        redacted = [
            {**s, "pending_otp": _REDACTED if s.get("pending_otp") else None}
            for s in senders
        ]
        return JSONResponse({"senders": redacted})

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
                    if sender and text:
                        try:
                            await adapter.handle_webhook_message(str(sender), str(text))
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
    ]
