"""REST API for Cremind Tag — the calling profile's own tags.

Every route requires a session JWT; the profile is ``request.user.username``.
A device is visible only to the profile that owns it (``owner_profile``): any
other id answers 404, never 403, so ids are not confirmed across profiles.
Timestamps are epoch **milliseconds**. Errors are ``{"error": <code>,
"message": <sentence>, "detail": <same sentence>}`` (``detail`` so readers of
the connector's error shape work too).

- ``GET    /api/tags``                          overview (each device carries ``pending_count``,
  its active deliveries)
- ``GET    /api/tags/settings``                 own + admin-default + built-in + effective settings;
  ``timezone`` is always an IANA name
- ``PUT    /api/tags/settings``                 ``{enabled?, options?}`` (options replace own overrides)
- ``PATCH  /api/tags/settings``                 ``{enabled?, options?}`` (options merge: ``null`` =
  inherit again; ``routes`` merges per kind)
- ``GET    /api/tags/devices/{id}``             ``{device, deliveries}`` (20 latest; ``device``
  carries ``previews`` and ``pending_count``)
- ``PATCH  /api/tags/devices/{id}``             ``{name}`` (1..128 once stripped) -> ``{device}``
- ``POST   /api/tags/devices/{id}/display``     ``{title, body?, icon?, ttl_s?, replace?}`` -> 201
  ``{delivery}`` (title one line; body keeps its line breaks, blank-line runs
  collapsed; each note is its own card unless ``replace: true``, which takes
  the tag's one replaceable note slot)
- ``POST   /api/tags/devices/{id}/clear``       -> 201 ``{delivery}``
- ``POST   /api/tags/devices/{id}/refresh``     -> 202 ``{command}``
- ``POST   /api/tags/devices/{id}/identify``    -> 202 ``{command}``
- ``GET    /api/tags/devices/{id}/preview``     ``?kind=desired|displayed`` -> ``image/png``
  (header ``X-Tag-Revision``, exposed to CORS; previews never outlive a change of owner)
- ``GET    /api/tags/deliveries``               ``?device=&state=&limit=&before=`` -> ``{deliveries, next_before}``
- ``GET    /api/tags/deliveries/{id}``          -> ``{delivery}``
- ``POST   /api/tags/deliveries/{id}/cancel``   -> ``{delivery, resolved}``: ``resolved`` is the
  ``resolved`` job sent to the companion so it drops a card it may already hold
  (``null`` when the tag has changed hands)
- ``GET    /api/tags/companions``               -> ``{companions}``
- ``GET    /api/tags/credentials``              -> ``{credentials}`` (own content credentials)
- ``POST   /api/tags/credentials``              ``{companion_id, label?}`` -> 201 ``{credential, secret, authorization}``
- ``DELETE /api/tags/credentials/{id}``         -> ``{credential}`` (revoked)

``display`` runs the same sanitiser as the journal and refuses text that looks
like a one-time code (422 ``otp_refused``) — the agent can reach this route
through the CLI, so the rule lives here and not only in the projection.
"""

from __future__ import annotations

import base64
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from app.api._auth import is_admin, require_auth
from app.tags import routing
from app.tags.cards import ICONS
from app.tags.service import TagError
from app.tags.storage import STAGES, TERMINAL_STAGES, get_tag_storage
from app.utils.logger import logger

_MAX_LIST = 200


def error_response(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    """The Tags error body: the repo's ``{error, message}`` plus ``detail``."""
    return JSONResponse({"error": code, "message": message, "detail": message, **extra},
                        status_code=status)


def tag_error_response(exc: TagError) -> JSONResponse:
    return error_response(exc.status, exc.code, exc.message, **exc.extra)


async def json_body(request: Request) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return None, error_response(400, "invalid_json", "The request body must be a JSON object.")
    if not isinstance(body, dict):
        return None, error_response(400, "invalid_json", "The request body must be a JSON object.")
    return body, None


def _profile(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _int_param(raw: Any) -> int | None:
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return None
    return value


async def _settings_payload(request: Request, profile: str) -> dict[str, Any]:
    store = get_tag_storage()
    row = await store.get_settings(profile) or {"enabled": False, "options": {}, "updated_at": None}
    defaults = await routing.admin_defaults()
    effective = routing.effective_options(row.get("options"), defaults)
    return {
        "profile": profile,
        "enabled": bool(row.get("enabled")),
        "options": row.get("options") or {},
        "defaults": defaults,
        "builtin": routing.BUILTIN_DEFAULTS,
        "effective": effective,
        "timezone": routing.resolved_timezone(profile, effective),
        "updated_at": row.get("updated_at"),
        "routable_kinds": list(routing.ROUTABLE_KINDS),
        "layouts": list(routing.LAYOUTS),
        "icons": list(ICONS),
        "is_admin": is_admin(request),
    }


def get_tags_routes() -> list[Route]:
    store = get_tag_storage

    async def handle_overview(request: Request) -> JSONResponse:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile(request)
        s = store()
        settings = await s.get_settings(profile)
        devices = await s.list_devices(owner=profile, kind="tag")
        revisions = await s.preview_revisions([d["id"] for d in devices])
        pending = await s.pending_counts([d["id"] for d in devices])
        companions = {c["id"]: c for c in await s.list_companions()}
        for d in devices:
            revs = revisions.get(d["id"], {})
            d["previews"] = {"desired": revs.get("desired"), "displayed": revs.get("displayed")}
            d["pending_count"] = pending.get(d["id"], 0)
            comp = companions.get(d["companion_id"]) or {}
            d["companion_name"] = comp.get("name")
            d["companion_online"] = bool(comp.get("online"))
        counts = await s.delivery_counts(profile)
        counts["devices"] = len(devices)
        return JSONResponse({
            "profile": profile,
            "enabled": bool((settings or {}).get("enabled")),
            "devices": devices,
            "counts": counts,
        })

    async def handle_get_settings(request: Request) -> JSONResponse:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        return JSONResponse(await _settings_payload(request, _profile(request)))

    async def _settings_body(request: Request):
        body, err = await json_body(request)
        if err is not None:
            return None, None, err
        unknown = [k for k in body if k not in ("enabled", "options")]
        if unknown:
            return None, None, error_response(
                422, "invalid_settings", f"Unknown field(s): {', '.join(sorted(unknown))}.",
                details={k: "unknown field" for k in unknown})
        enabled = body.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            return None, None, error_response(422, "invalid_settings", "'enabled' must be true or false.",
                                              details={"enabled": "must be true or false"})
        return body, enabled, None

    async def handle_put_settings(request: Request) -> JSONResponse:
        """Full replace: a present ``options`` becomes the profile's whole
        override set (``{}`` inherits everything)."""
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile(request)
        body, enabled, err = await _settings_body(request)
        if err is not None:
            return err
        replace = "options" in body
        try:
            options = routing.normalize_options(body.get("options")) if replace else None
        except routing.SettingsError as exc:
            return error_response(422, "invalid_settings", str(exc), details=exc.details)
        await store().save_settings(profile, enabled=enabled, options=options, replace_options=replace)
        logger.info(f"[tags] settings saved for {profile} (enabled={enabled}, options={replace})")
        return JSONResponse(await _settings_payload(request, profile))

    async def handle_patch_settings(request: Request) -> JSONResponse:
        """Merge: only the ``options`` keys given change; ``null`` removes an
        override (inherit again); ``routes`` merges per card kind."""
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile(request)
        body, enabled, err = await _settings_body(request)
        if err is not None:
            return err
        patch = body.get("options") if "options" in body else None
        if "options" in body and not isinstance(patch, dict):
            return error_response(422, "invalid_settings", "'options' must be an object.",
                                  details={"options": "must be an object"})
        try:
            await store().save_settings(
                profile, enabled=enabled,
                merge=(lambda current: routing.merge_options(current, patch)) if patch is not None else None,
            )
        except routing.SettingsError as exc:
            return error_response(422, "invalid_settings", str(exc), details=exc.details)
        logger.info(f"[tags] settings patched for {profile} (enabled={enabled}, "
                    f"options={sorted(patch) if patch else []})")
        return JSONResponse(await _settings_payload(request, profile))

    async def handle_get_device(request: Request) -> JSONResponse:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile(request)
        from app.tags.service import owned_tag

        try:
            device = await owned_tag(profile, request.path_params["device_id"])
        except TagError as exc:
            return tag_error_response(exc)
        revs = (await store().preview_revisions([device["id"]])).get(device["id"], {})
        device["previews"] = {"desired": revs.get("desired"), "displayed": revs.get("displayed")}
        device["pending_count"] = (await store().pending_counts([device["id"]])).get(device["id"], 0)
        deliveries = await store().list_deliveries(profile, device_id=device["id"], limit=20)
        return JSONResponse({"device": device, "deliveries": deliveries})

    async def handle_patch_device(request: Request) -> JSONResponse:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile(request)
        body, err = await json_body(request)
        if err is not None:
            return err
        from app.tags import service

        try:
            device = await service.rename_owned_tag(profile, request.path_params["device_id"], body.get("name"))
        except TagError as exc:
            return tag_error_response(exc)
        return JSONResponse({"device": device})

    async def handle_display(request: Request) -> JSONResponse:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        body, err = await json_body(request)
        if err is not None:
            return err
        from app.tags import service

        try:
            delivery = await service.display(_profile(request), request.path_params["device_id"], body)
        except TagError as exc:
            return tag_error_response(exc)
        return JSONResponse({"delivery": delivery}, status_code=201)

    async def handle_clear(request: Request) -> JSONResponse:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        from app.tags import service

        try:
            delivery = await service.clear(_profile(request), request.path_params["device_id"])
        except TagError as exc:
            return tag_error_response(exc)
        return JSONResponse({"delivery": delivery}, status_code=201)

    def _command_handler(kind: str):
        async def handler(request: Request) -> JSONResponse:
            unauth = require_auth(request)
            if unauth is not None:
                return unauth
            from app.tags import service

            profile = _profile(request)
            try:
                command = await service.device_command(profile, request.path_params["device_id"], kind,
                                                       requested_by=profile)
            except TagError as exc:
                return tag_error_response(exc)
            return JSONResponse({"command": command}, status_code=202)
        return handler

    async def handle_preview(request: Request) -> Response:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        kind = request.query_params.get("kind") or "displayed"
        if kind not in ("desired", "displayed"):
            return error_response(400, "invalid_kind", "'kind' must be 'desired' or 'displayed'.")
        from app.tags.service import owned_tag

        try:
            device = await owned_tag(_profile(request), request.path_params["device_id"])
        except TagError as exc:
            return tag_error_response(exc)
        preview = await store().get_preview(device["id"], kind)
        if preview is None:
            return error_response(404, "no_preview", f"The companion has not sent a {kind} preview yet.")
        try:
            png = base64.b64decode(preview["png_base64"])
        except Exception:  # noqa: BLE001
            return error_response(404, "no_preview", "The stored preview is unreadable.")
        return Response(png, media_type="image/png", headers={
            "Cache-Control": "no-store",
            "X-Tag-Revision": str(preview.get("revision") or 0),
        })

    async def handle_list_deliveries(request: Request) -> JSONResponse:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile(request)
        q = request.query_params
        state = q.get("state") or None
        if state and state not in ("active", "terminal") and state not in STAGES and state not in TERMINAL_STAGES:
            return error_response(400, "invalid_state",
                                  "'state' must be 'active', 'terminal' or a delivery stage.")
        limit = _int_param(q.get("limit") or 50)
        if limit is None or limit < 1:
            return error_response(400, "invalid_limit", "'limit' must be a positive whole number.")
        limit = min(limit, _MAX_LIST)
        before = None
        if q.get("before"):
            before = _int_param(q.get("before"))
            if before is None:
                return error_response(400, "invalid_before", "'before' must be a delivery id.")
        device_id = q.get("device") or None
        if device_id:
            from app.tags.service import owned_tag

            try:
                await owned_tag(profile, device_id)
            except TagError as exc:
                return tag_error_response(exc)
        rows = await store().list_deliveries(profile, device_id=device_id, state=state,
                                             limit=limit, before=before)
        next_before = rows[-1]["id"] if len(rows) >= limit else None
        return JSONResponse({"deliveries": rows, "next_before": next_before})

    async def handle_get_delivery(request: Request) -> JSONResponse:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        did = _int_param(request.path_params["delivery_id"])
        row = await store().get_delivery(did, profile=_profile(request)) if did is not None else None
        if row is None:
            return error_response(404, "delivery_not_found", "No delivery with that id.")
        return JSONResponse({"delivery": row})

    async def handle_cancel_delivery(request: Request) -> JSONResponse:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        did = _int_param(request.path_params["delivery_id"])
        if did is None:
            return error_response(404, "delivery_not_found", "No delivery with that id.")
        from app.tags import service

        try:
            return JSONResponse(await service.cancel_delivery(_profile(request), did))
        except TagError as exc:
            return tag_error_response(exc)

    async def handle_companions(request: Request) -> JSONResponse:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        companions = [
            {k: c[k] for k in ("id", "name", "online", "last_seen_at", "version")}
            for c in await store().list_companions()
        ]
        return JSONResponse({"companions": companions})

    async def handle_list_credentials(request: Request) -> JSONResponse:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        creds = await store().list_credentials(profile=_profile(request), kind="content")
        return JSONResponse({"credentials": creds})

    async def handle_create_credential(request: Request) -> JSONResponse:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        body, err = await json_body(request)
        if err is not None:
            return err
        from app.tags import service

        profile = _profile(request)
        try:
            cred, secret = await service.create_content_credential(
                profile, companion_id=body.get("companion_id"), label=body.get("label"),
            )
        except TagError as exc:
            return tag_error_response(exc)
        logger.info(f"[tags] content credential {cred['id']} created for {profile}")
        return JSONResponse(service.secret_payload(cred, secret), status_code=201)

    async def handle_revoke_credential(request: Request) -> JSONResponse:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile(request)
        cred = await store().revoke_credential(request.path_params["credential_id"],
                                               profile=profile, kind="content")
        if cred is None:
            return error_response(404, "credential_not_found", "No credential with that id.")
        logger.info(f"[tags] content credential {cred['id']} revoked by {profile}")
        return JSONResponse({"credential": cred})

    return [
        Route("/api/tags", handle_overview, methods=["GET"]),
        Route("/api/tags/settings", handle_get_settings, methods=["GET"]),
        Route("/api/tags/settings", handle_put_settings, methods=["PUT"]),
        Route("/api/tags/settings", handle_patch_settings, methods=["PATCH"]),
        Route("/api/tags/devices/{device_id}", handle_get_device, methods=["GET"]),
        Route("/api/tags/devices/{device_id}", handle_patch_device, methods=["PATCH"]),
        Route("/api/tags/devices/{device_id}/display", handle_display, methods=["POST"]),
        Route("/api/tags/devices/{device_id}/clear", handle_clear, methods=["POST"]),
        Route("/api/tags/devices/{device_id}/refresh", _command_handler("refresh_tag"), methods=["POST"]),
        Route("/api/tags/devices/{device_id}/identify", _command_handler("identify"), methods=["POST"]),
        Route("/api/tags/devices/{device_id}/preview", handle_preview, methods=["GET"]),
        Route("/api/tags/deliveries", handle_list_deliveries, methods=["GET"]),
        Route("/api/tags/deliveries/{delivery_id}", handle_get_delivery, methods=["GET"]),
        Route("/api/tags/deliveries/{delivery_id}/cancel", handle_cancel_delivery, methods=["POST"]),
        Route("/api/tags/companions", handle_companions, methods=["GET"]),
        Route("/api/tags/credentials", handle_list_credentials, methods=["GET"]),
        Route("/api/tags/credentials", handle_create_credential, methods=["POST"]),
        Route("/api/tags/credentials/{credential_id}", handle_revoke_credential, methods=["DELETE"]),
    ]
