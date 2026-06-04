"""Settings → Config endpoints.

Drives the per-profile general-config UI: schema discovery, current values,
partial updates, and per-key reset. Storage lives in the ``user_config``
SQLite table; the schema (groups, types, defaults, ranges) is declared in
:mod:`app.config.config_schema`.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.config.config_schema import CONFIG_SCHEMA, all_keys, lookup
from app.config.user_config import get_user_config, resolve_default
from app.storage.dynamic_config_storage import DynamicConfigStorage


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _require_auth(request: Request):
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


def _serialize_schema() -> dict:
    """Project ``CONFIG_SCHEMA`` into the JSON shape consumed by the UI."""
    groups: dict[str, dict] = {}
    for group_name, group in CONFIG_SCHEMA.items():
        fields: dict[str, dict] = {}
        for field_name, field in group.fields.items():
            entry: dict = {
                "type": field.type,
                "default": resolve_default(f"{group_name}.{field_name}"),
            }
            if field.label is not None:
                entry["label"] = field.label
            if field.description is not None:
                entry["description"] = field.description
            if field.min is not None:
                entry["min"] = field.min
            if field.max is not None:
                entry["max"] = field.max
            if field.step is not None:
                entry["step"] = field.step
            if field.enum is not None:
                entry["enum"] = list(field.enum)
            fields[field_name] = entry
        groups[group_name] = {
            "label": group.label,
            "description": group.description,
            "fields": fields,
        }
    return {"groups": groups}


def get_user_config_routes(config_storage: DynamicConfigStorage) -> list[Route]:
    """Routes for the Settings → Config page."""

    async def handle_get_schema(request: Request) -> JSONResponse:
        return JSONResponse(_serialize_schema())

    async def handle_get_user_config(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)
        stored = config_storage.get_all("user_config", profile=profile)
        values: dict[str, object] = {}
        defaults: dict[str, object] = {}
        for key in all_keys():
            defaults[key] = resolve_default(key)
            if key in stored:
                # Coerce stored string back to declared type for the UI.
                _, _, field = lookup(key)
                values[key] = field.coerce(stored[key])
            else:
                values[key] = None
        return JSONResponse({
            "profile": profile,
            "values": values,
            "defaults": defaults,
        })

    async def handle_update_user_config(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)
        values = body.get("values") or {}
        if not isinstance(values, dict):
            return JSONResponse({"error": "'values' must be an object"}, status_code=400)

        errors: dict[str, str] = {}
        coerced: dict[str, str] = {}
        for key, raw in values.items():
            try:
                _, _, field = lookup(key)
            except KeyError:
                errors[key] = "unknown config key"
                continue
            try:
                value = field.coerce(raw)
                field.validate(value)
            except ValueError as exc:
                errors[key] = str(exc)
                continue
            coerced[key] = _stringify(value)

        if errors:
            return JSONResponse({"error": "validation failed", "details": errors}, status_code=400)

        for key, stored_value in coerced.items():
            config_storage.set("user_config", key, stored_value, profile=profile)

        return JSONResponse({"success": True, "updated": list(coerced)})

    async def handle_reset_user_config_key(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        key = request.path_params.get("key", "")
        try:
            lookup(key)
        except KeyError:
            return JSONResponse({"error": "unknown config key"}, status_code=404)
        profile = _profile_from_request(request)
        if not profile:
            return JSONResponse({"error": "Profile is required"}, status_code=400)
        deleted = config_storage.delete("user_config", key, profile=profile)
        return JSONResponse({"success": True, "deleted": deleted})

    return [
        Route("/api/config/schema", handle_get_schema, methods=["GET"]),
        Route("/api/config/user", handle_get_user_config, methods=["GET"]),
        Route("/api/config/user", handle_update_user_config, methods=["PUT"]),
        Route("/api/config/user/{key}", handle_reset_user_config_key, methods=["DELETE"]),
    ]


def _stringify(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
