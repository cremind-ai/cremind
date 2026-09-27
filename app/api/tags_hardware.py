"""REST API for Cremind Tag hardware — admin profile only (``require_admin``).

Companions, gateways, bridges and tags are system-wide; this is where the admin
registers companions, queues hardware operations, and decides which profile
owns which tag. Timestamps are epoch milliseconds; errors are ``{"error",
"message", "detail"}``. Secrets appear only in the create / rotate responses.

- ``GET    /api/tags/hardware``                             ``{companions, devices, commands}``
  (each tag carries ``pending_count``; a tag whose clear failed 3 times reads
  status ``clear_failed`` — claim or release it again to retry)
- ``POST   /api/tags/hardware/companions``                  ``{name}`` -> 201 ``{companion, credential, secret, authorization}``
- ``POST   /api/tags/hardware/companions/{id}/rotate``      -> ``{credential, secret, authorization, revoked}``
- ``DELETE /api/tags/hardware/companions/{id}``             -> ``{deleted: true}``
- ``POST   /api/tags/hardware/commands``                    ``{companion_id, kind, args?}`` -> 202 ``{command}``
- ``GET    /api/tags/hardware/commands/{id}``               -> ``{command}``
- ``POST   /api/tags/hardware/tags/{id}/claim``             ``{owner, bridge_id?, name?}`` -> ``{device, commands}``
  (the tag starts clean: name = ``name`` or ``""``, revisions 0, previews gone)
- ``POST   /api/tags/hardware/tags/{id}/assign``            ``{bridge_id}`` -> ``{device, command}``
- ``POST   /api/tags/hardware/tags/{id}/release``           -> ``{device, commands}``
- ``PATCH  /api/tags/hardware/devices/{id}``                ``{name}`` (1..128 once stripped) -> ``{device}``
- ``DELETE /api/tags/hardware/devices/{id}``                -> ``{deleted: true, device, last_epoch}``;
  409 ``tag_owned`` (with ``device``) for a tag a profile owns — release it first
- ``GET    /api/tags/hardware/defaults``                    -> ``{defaults, builtin}``
- ``PUT    /api/tags/hardware/defaults``                    ``{defaults}`` (full replace) -> ``{defaults, builtin}``
- ``PATCH  /api/tags/hardware/defaults``                    ``{defaults}`` (merge; ``null`` removes a
  default, ``routes`` merges per kind) -> ``{defaults, builtin}``

Command kinds an admin may queue directly: ``scan_unprovisioned {duration_s}``,
``provision_bridge {uuid, name?}``, ``configure_bridge {hw_id}``,
``remove_bridge {hw_id}``, ``identify {hw_id}``, ``refresh_tag {tag_id}``,
``install_fontpack {bridge_hw_id}``, ``collect_diagnostics {}``.
``assign_tag`` / ``clear_tag`` come only from claim / assign / release.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.api._auth import require_admin
from app.api.tags import error_response, json_body, tag_error_response
from app.tags import routing, service
from app.tags.service import TagError
from app.tags.storage import COMMAND_ACTIVE, get_tag_storage
from app.utils.logger import logger


def _admin(request: Request) -> str:
    return getattr(request.user, "username", "") or "admin"


def get_tags_hardware_routes(config_storage=None) -> list[Route]:
    store = get_tag_storage

    async def handle_inventory(request: Request) -> JSONResponse:
        denied = require_admin(request)
        if denied is not None:
            return denied
        s = store()
        companions = await s.list_companions()
        hardware = await s.list_credentials(kind="hardware")
        for c in companions:
            c["credentials"] = [h for h in hardware if h["companion_id"] == c["id"]]
        devices = await s.list_devices()
        pending = await s.pending_counts([d["id"] for d in devices if d["kind"] == "tag"])
        for d in devices:
            if d["kind"] == "tag":
                d["pending_count"] = pending.get(d["id"], 0)
        active = await s.list_commands(statuses=COMMAND_ACTIVE, limit=200)
        recent = await s.list_commands(limit=50)
        seen = {c["id"] for c in active}
        commands = active + [c for c in recent if c["id"] not in seen]
        return JSONResponse({"companions": companions, "devices": devices, "commands": commands})

    async def handle_register(request: Request) -> JSONResponse:
        denied = require_admin(request)
        if denied is not None:
            return denied
        body, err = await json_body(request)
        if err is not None:
            return err
        try:
            companion, cred, secret = await service.register_companion(body.get("name"),
                                                                       created_by=_admin(request))
        except TagError as exc:
            return tag_error_response(exc)
        logger.info(f"[tags] companion {companion['id']} registered ({companion['name']})")
        return JSONResponse({"companion": companion, **service.secret_payload(cred, secret)},
                            status_code=201)

    async def handle_rotate(request: Request) -> JSONResponse:
        denied = require_admin(request)
        if denied is not None:
            return denied
        try:
            cred, secret, revoked = await service.rotate_companion(
                request.path_params["companion_id"], created_by=_admin(request),
            )
        except TagError as exc:
            return tag_error_response(exc)
        logger.info(f"[tags] companion {cred['companion_id']} credential rotated")
        return JSONResponse({**service.secret_payload(cred, secret), "revoked": revoked})

    async def handle_delete_companion(request: Request) -> JSONResponse:
        denied = require_admin(request)
        if denied is not None:
            return denied
        cid = request.path_params["companion_id"]
        if not await store().delete_companion(cid):
            return error_response(404, "companion_not_found", "No companion with that id.")
        logger.info(f"[tags] companion {cid} deleted")
        return JSONResponse({"deleted": True})

    async def handle_create_command(request: Request) -> JSONResponse:
        denied = require_admin(request)
        if denied is not None:
            return denied
        body, err = await json_body(request)
        if err is not None:
            return err
        companion_id = body.get("companion_id")
        kind = body.get("kind")
        if not isinstance(companion_id, str) or not companion_id:
            return error_response(422, "invalid_companion", "'companion_id' is required.")
        if not isinstance(kind, str) or not kind:
            return error_response(422, "unknown_command", "'kind' is required.")
        try:
            command = await service.admin_command(companion_id, kind, body.get("args"),
                                                  requested_by=_admin(request))
        except TagError as exc:
            return tag_error_response(exc)
        return JSONResponse({"command": command}, status_code=202)

    async def handle_get_command(request: Request) -> JSONResponse:
        denied = require_admin(request)
        if denied is not None:
            return denied
        command = await store().get_command(request.path_params["command_id"])
        if command is None:
            return error_response(404, "command_not_found", "No command with that id.")
        return JSONResponse({"command": command})

    async def handle_claim(request: Request) -> JSONResponse:
        denied = require_admin(request)
        if denied is not None:
            return denied
        body, err = await json_body(request)
        if err is not None:
            return err
        try:
            result = await service.claim_tag(
                request.path_params["device_id"], owner=body.get("owner"),
                bridge_id=body.get("bridge_id"), name=body.get("name"),
                requested_by=_admin(request),
            )
        except TagError as exc:
            return tag_error_response(exc)
        logger.info(f"[tags] tag {result['device']['id']} claimed for {result['device']['owner_profile']} "
                    f"(epoch {result['device']['epoch']})")
        return JSONResponse(result)

    async def handle_assign(request: Request) -> JSONResponse:
        denied = require_admin(request)
        if denied is not None:
            return denied
        body, err = await json_body(request)
        if err is not None:
            return err
        try:
            result = await service.assign_tag(request.path_params["device_id"],
                                              bridge_id=body.get("bridge_id"), requested_by=_admin(request))
        except TagError as exc:
            return tag_error_response(exc)
        return JSONResponse(result)

    async def handle_release(request: Request) -> JSONResponse:
        denied = require_admin(request)
        if denied is not None:
            return denied
        try:
            result = await service.release_tag(request.path_params["device_id"], requested_by=_admin(request))
        except TagError as exc:
            return tag_error_response(exc)
        logger.info(f"[tags] tag {result['device']['id']} released (epoch {result['device']['epoch']})")
        return JSONResponse(result)

    async def handle_patch_device(request: Request) -> JSONResponse:
        denied = require_admin(request)
        if denied is not None:
            return denied
        body, err = await json_body(request)
        if err is not None:
            return err
        try:
            device = await service.rename_device(request.path_params["device_id"], body.get("name"))
        except TagError as exc:
            return tag_error_response(exc)
        return JSONResponse({"device": device})

    async def handle_delete_device(request: Request) -> JSONResponse:
        denied = require_admin(request)
        if denied is not None:
            return denied
        try:
            device = await service.forget_device(request.path_params["device_id"])
        except TagError as exc:
            return tag_error_response(exc)
        logger.info(f"[tags] device {device['id']} ({device['kind']} {device['hw_id']}) forgotten "
                    f"at epoch {device['epoch']}")
        return JSONResponse({"deleted": True, "device": device, "last_epoch": device["epoch"]})

    async def handle_get_defaults(request: Request) -> JSONResponse:
        denied = require_admin(request)
        if denied is not None:
            return denied
        return JSONResponse({"defaults": routing.read_admin_defaults(), "builtin": routing.BUILTIN_DEFAULTS})

    async def handle_put_defaults(request: Request) -> JSONResponse:
        denied = require_admin(request)
        if denied is not None:
            return denied
        body, err = await json_body(request)
        if err is not None:
            return err
        return await _write_defaults(body, routing.write_admin_defaults)

    async def handle_patch_defaults(request: Request) -> JSONResponse:
        denied = require_admin(request)
        if denied is not None:
            return denied
        body, err = await json_body(request)
        if err is not None:
            return err
        if not isinstance(body.get("defaults"), dict):
            return error_response(422, "invalid_settings", "'defaults' must be an object.",
                                  details={"defaults": "must be an object"})
        return await _write_defaults(body, routing.patch_admin_defaults)

    async def _write_defaults(body: dict, write) -> JSONResponse:
        cfg = config_storage
        if cfg is None:
            from app.runtime import get_state

            cfg = get_state().config_storage
        if cfg is None:
            return error_response(503, "storage_not_ready", "Storage is not ready yet.")
        try:
            defaults = write(body.get("defaults"), cfg)
        except routing.SettingsError as exc:
            return error_response(422, "invalid_settings", str(exc), details=exc.details)
        return JSONResponse({"defaults": defaults, "builtin": routing.BUILTIN_DEFAULTS})

    return [
        Route("/api/tags/hardware", handle_inventory, methods=["GET"]),
        Route("/api/tags/hardware/companions", handle_register, methods=["POST"]),
        Route("/api/tags/hardware/companions/{companion_id}/rotate", handle_rotate, methods=["POST"]),
        Route("/api/tags/hardware/companions/{companion_id}", handle_delete_companion, methods=["DELETE"]),
        Route("/api/tags/hardware/commands", handle_create_command, methods=["POST"]),
        Route("/api/tags/hardware/commands/{command_id}", handle_get_command, methods=["GET"]),
        Route("/api/tags/hardware/tags/{device_id}/claim", handle_claim, methods=["POST"]),
        Route("/api/tags/hardware/tags/{device_id}/assign", handle_assign, methods=["POST"]),
        Route("/api/tags/hardware/tags/{device_id}/release", handle_release, methods=["POST"]),
        Route("/api/tags/hardware/devices/{device_id}", handle_patch_device, methods=["PATCH"]),
        Route("/api/tags/hardware/devices/{device_id}", handle_delete_device, methods=["DELETE"]),
        Route("/api/tags/hardware/defaults", handle_get_defaults, methods=["GET"]),
        Route("/api/tags/hardware/defaults", handle_put_defaults, methods=["PUT"]),
        Route("/api/tags/hardware/defaults", handle_patch_defaults, methods=["PATCH"]),
    ]
