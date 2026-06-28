"""LLM provider and model group API endpoints."""

import json

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.api._auth import require_auth_or_setup_mode
from app.config import load_all_provider_catalogs, load_provider_catalog
from app.config.provider_auth import normalize_provider_auth_methods
from app.lib.llm.factory import SUPPORTED_LLM_PROVIDERS
from app.runtime import BootedState
from app.utils.logger import logger

_GH_CLIENT_ID = "Iv1.b507a08c87ecfe98"
_GH_DEVICE_CODE_URL = "https://github.com/login/device/code"
_GH_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"


def _require_auth(request: Request):
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


def _storage_not_ready() -> JSONResponse:
    """503 response for write handlers invoked before storage is booted."""
    return JSONResponse(
        {"error": "Setup not complete — storage is not ready yet."},
        status_code=503,
    )


def get_llm_routes(state: BootedState) -> list[Route]:
    """LLM provider + model-group routes.

    Registered pre-storage so the Setup Wizard can render the provider
    catalog before ``bootstrap.toml`` exists. Handlers resolve
    ``state.config_storage`` at request time — ``None`` during deferred
    storage, populated post-boot — and gracefully degrade to a catalog-only
    response when no per-profile values are reachable.
    """

    async def handle_list_providers(request: Request) -> JSONResponse:
        """List all available LLM providers with their config status.

        Open during first-run setup so the wizard can show provider
        choices before any JWT exists; gated post-setup.
        """
        config_storage = state.config_storage
        if config_storage is not None:
            denied = require_auth_or_setup_mode(request, config_storage)
            if denied is not None:
                return denied
        profile = getattr(request.user, "username", "") or ""
        catalogs = load_all_provider_catalogs()
        providers = []

        def _stored(key: str) -> str | None:
            if config_storage is None:
                return None
            return config_storage.get("llm_config", key, profile=profile)

        for name in SUPPORTED_LLM_PROVIDERS:
            catalog = catalogs.get(name, {})
            provider_info = catalog.get("provider", {})

            requires_key = provider_info.get("requires_api_key", False)
            requires_sa = provider_info.get("requires_service_account", False)
            legacy_config_fields = provider_info.get("config_fields", {})

            # Normalize auth methods (handles both new and legacy formats)
            auth_methods = normalize_provider_auth_methods(provider_info)

            # Read which auth method is active
            active_auth_method = _stored(f"{name}.auth_method")
            # Default to the first method marked is_default, or the first entry
            if not active_auth_method:
                for am in auth_methods:
                    if am.get("is_default"):
                        active_auth_method = am["id"]
                        break
                if not active_auth_method and auth_methods:
                    active_auth_method = auth_methods[0]["id"]

            # Annotate each auth method's fields with configured status
            auth_methods_response = []
            for am in auth_methods:
                fields_response = {}
                for field_key, field_spec in am.get("fields", {}).items():
                    val = _stored(f"{name}.{field_key}")
                    fields_response[field_key] = {
                        "description": field_spec.get("description", ""),
                        "type": field_spec.get("type", "string"),
                        "secret": field_spec.get("secret", False),
                        "required": field_spec.get("required", True),
                        "default": field_spec.get("default"),
                        "configured": bool(val),
                    }
                am_entry = {
                    "id": am["id"],
                    "label": am.get("label", am["id"]),
                    "hint": am.get("hint", ""),
                    "kind": am.get("kind", "api_key"),
                    "is_default": am.get("is_default", False),
                    "fields": fields_response,
                }
                if am.get("instructions"):
                    am_entry["instructions"] = am["instructions"]
                auth_methods_response.append(am_entry)

            # Determine overall configured status based on active auth method
            configured = False
            for am in auth_methods_response:
                if am["id"] == active_auth_method:
                    if am["kind"] == "none":
                        configured = True
                    else:
                        configured = all(
                            f_spec["configured"]
                            for f_spec in am["fields"].values()
                            if f_spec["required"]
                        )
                    break

            # Build legacy config_fields and current_values for backward compat
            config_fields_status = {}
            current_values = {}
            for field_key, field_spec in legacy_config_fields.items():
                val = _stored(f"{name}.{field_key}")
                config_fields_status[field_key] = {
                    "description": field_spec.get("description", ""),
                    "type": field_spec.get("type", "string"),
                    "secret": field_spec.get("secret", False),
                    "required": field_spec.get("required", True),
                    "default": field_spec.get("default"),
                    "configured": bool(val),
                }
                if val and not field_spec.get("secret", False):
                    current_values[field_key] = val

            provider_entry = {
                "name": name,
                "display_name": provider_info.get("display_name", name),
                "requires_api_key": requires_key,
                "requires_service_account": requires_sa,
                "configured": configured,
                "model_count": len(catalog.get("models", [])),
                "auth_methods": auth_methods_response,
                "active_auth_method": active_auth_method,
            }
            if config_fields_status:
                provider_entry["config_fields"] = config_fields_status
                provider_entry["current_values"] = current_values

            providers.append(provider_entry)

        return JSONResponse({"providers": providers})

    async def handle_get_provider_models(request: Request) -> JSONResponse:
        """List models for a specific provider from the TOML catalog."""
        provider_name = request.path_params["name"]
        catalog = load_provider_catalog(provider_name)

        if not catalog:
            return JSONResponse(
                {"error": f"Provider '{provider_name}' not found"},
                status_code=404,
            )

        return JSONResponse({
            "provider": catalog.get("provider", {}),
            "models": catalog.get("models", []),
        })

    async def handle_update_provider(request: Request) -> JSONResponse:
        """Update provider configuration (API key, auth method, etc.)."""
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        config_storage = state.config_storage
        if config_storage is None:
            return _storage_not_ready()
        profile = getattr(request.user, "username", "") or ""
        provider_name = request.path_params["name"]

        if provider_name not in SUPPORTED_LLM_PROVIDERS:
            return JSONResponse(
                {"error": f"Unknown provider: {provider_name}"},
                status_code=404,
            )

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        # Build a combined secret-field lookup from auth_methods and legacy config_fields
        catalog = load_provider_catalog(provider_name)
        provider_info = catalog.get("provider", {}) if catalog else {}
        secret_fields: set[str] = set()
        for am in normalize_provider_auth_methods(provider_info):
            for fk, fs in am.get("fields", {}).items():
                if fs.get("secret", False):
                    secret_fields.add(fk)
        # Legacy config_fields
        for fk, fs in provider_info.get("config_fields", {}).items():
            if fs.get("secret", False):
                secret_fields.add(fk)

        # Save each config key
        for key, value in body.items():
            full_key = f"{provider_name}.{key}"
            is_secret = (
                key in secret_fields
                or "api_key" in key
                or "service_account" in key
                or "token" in key
            )
            # auth_method itself is not secret
            if key == "auth_method":
                is_secret = False
            str_value = value if isinstance(value, str) else json.dumps(value)
            config_storage.set("llm_config", full_key, str_value, is_secret=is_secret, profile=profile)

        from app.events.settings_state_bus import publish_settings_state_changed
        publish_settings_state_changed(profile)
        return JSONResponse({"success": True})

    async def handle_delete_provider(request: Request) -> JSONResponse:
        """Remove all stored configuration for a provider."""
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        config_storage = state.config_storage
        if config_storage is None:
            return _storage_not_ready()
        profile = getattr(request.user, "username", "") or ""
        provider_name = request.path_params["name"]

        if provider_name not in SUPPORTED_LLM_PROVIDERS:
            return JSONResponse(
                {"error": f"Unknown provider: {provider_name}"},
                status_code=404,
            )

        deleted = config_storage.delete_by_prefix("llm_config", f"{provider_name}.", profile=profile)
        logger.info(f"Deleted {deleted} config key(s) for provider '{provider_name}' (profile={profile})")
        from app.events.settings_state_bus import publish_settings_state_changed
        publish_settings_state_changed(profile)
        return JSONResponse({"success": True, "deleted_keys": deleted})

    async def handle_get_model_groups(request: Request) -> JSONResponse:
        """Get the single configured model (``high``) + optional ``vision`` model,
        and their reasoning_effort.

        The high/low split was removed when the agent moved to native function
        calling; ``high`` is the one model the agent runs on. The key name is
        kept for config back-compat. ``vision`` is an optional override used only
        by image_understanding (falls back to the single model when unset).
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        config_storage = state.config_storage
        if config_storage is None:
            return _storage_not_ready()
        profile = getattr(request.user, "username", "") or ""
        from app.config import settings as dynaconf_settings

        groups = {}
        reasoning_efforts: dict[str, str | None] = {}
        for group in ("high", "vision"):
            # SQLite first
            val = config_storage.get("llm_config", f"model_group.{group}", profile=profile)
            if not val:
                try:
                    val = dynaconf_settings.get(f"llm.model_groups.{group}")
                except Exception:
                    val = None
            groups[group] = val or ""

            # Reasoning effort per group
            re_val = config_storage.get("llm_config", f"model_group.{group}.reasoning_effort", profile=profile)
            reasoning_efforts[group] = re_val or None

        # Also return default provider
        default_provider = config_storage.get("llm_config", "default_provider", profile=profile)
        if not default_provider:
            try:
                default_provider = dynaconf_settings.get("llm.default_provider", "")
            except Exception:
                default_provider = ""

        # Specialized Vision Model feature toggle (opt-in; unset reads as off).
        ve_raw = config_storage.get("llm_config", "model_group.vision.enabled", profile=profile)
        vision_enabled = str(ve_raw).strip().lower() in {"1", "true", "yes", "on"}

        return JSONResponse({
            "model_groups": groups,
            "default_provider": default_provider,
            "reasoning_efforts": reasoning_efforts,
            "vision_enabled": vision_enabled,
        })

    async def handle_update_model_groups(request: Request) -> JSONResponse:
        """Update model group assignments."""
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        config_storage = state.config_storage
        if config_storage is None:
            return _storage_not_ready()
        profile = getattr(request.user, "username", "") or ""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        model_groups = body.get("model_groups", {})
        for group, value in model_groups.items():
            if group in ("high", "vision"):
                config_storage.set("llm_config", f"model_group.{group}", str(value), profile=profile)

        # Save reasoning_effort per group
        reasoning_efforts = body.get("reasoning_efforts", {})
        for group, value in reasoning_efforts.items():
            if group in ("high", "vision"):
                if value:
                    config_storage.set("llm_config", f"model_group.{group}.reasoning_effort", str(value), profile=profile)
                else:
                    config_storage.delete("llm_config", f"model_group.{group}.reasoning_effort", profile=profile)

        default_provider = body.get("default_provider")
        if default_provider:
            config_storage.set("llm_config", "default_provider", default_provider, profile=profile)

        # Specialized Vision Model feature toggle. Kept outside the model_groups
        # loop so it isn't mistaken for a model value. Only written when present.
        vision_enabled = body.get("vision_enabled")
        if vision_enabled is not None:
            config_storage.set(
                "llm_config", "model_group.vision.enabled",
                "true" if vision_enabled else "false", profile=profile,
            )

        from app.events.settings_state_bus import publish_settings_state_changed
        publish_settings_state_changed(profile)
        return JSONResponse({"success": True})

    async def handle_device_code_start(request: Request) -> JSONResponse:
        """Start a GitHub device code flow for Copilot auth."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    _GH_DEVICE_CODE_URL,
                    data={"client_id": _GH_CLIENT_ID, "scope": "read:user"},
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
            return JSONResponse({
                "verification_uri": data.get("verification_uri", "https://github.com/login/device"),
                "user_code": data.get("user_code", ""),
                "device_code": data.get("device_code", ""),
                "expires_in": data.get("expires_in", 900),
                "interval": data.get("interval", 5),
            })
        except Exception as e:
            logger.error(f"Device code start failed: {e}")
            return JSONResponse({"error": str(e)}, status_code=502)

    async def handle_device_code_poll(request: Request) -> JSONResponse:
        """Poll GitHub for a device code access token, then store it.

        When the request is unauthenticated (setup wizard, before any profile
        exists), the token is returned to the caller instead of being persisted:
        the ``llm_config`` table has a FK on ``profiles.name``, so a direct
        write would fail until the wizard's ``completeSetup`` call creates the
        profile. The wizard bundles the returned token into that same call.
        """
        is_authenticated = bool(getattr(request.user, "is_authenticated", False))
        profile = getattr(request.user, "username", "") or ""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        device_code = body.get("device_code", "")
        if not device_code:
            return JSONResponse({"error": "device_code is required"}, status_code=400)

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    _GH_ACCESS_TOKEN_URL,
                    data={
                        "client_id": _GH_CLIENT_ID,
                        "device_code": device_code,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    },
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()

            error = data.get("error")
            if error == "authorization_pending":
                return JSONResponse({"status": "pending"})
            if error == "slow_down":
                return JSONResponse({"status": "pending", "slow_down": True})
            if error == "expired_token":
                return JSONResponse({"status": "expired"})
            if error:
                return JSONResponse({"status": "error", "error": error})

            access_token = data.get("access_token", "")
            if access_token:
                config_storage = state.config_storage
                if is_authenticated and config_storage is not None:
                    config_storage.set("llm_config", "github-copilot.api_key", access_token, is_secret=True, profile=profile)
                    config_storage.set("llm_config", "github-copilot.auth_method", "device_code", profile=profile)
                    return JSONResponse({"status": "complete"})
                return JSONResponse({"status": "complete", "access_token": access_token})

            return JSONResponse({"status": "error", "error": "No access token in response"})
        except Exception as e:
            logger.error(f"Device code poll failed: {e}")
            return JSONResponse({"error": str(e)}, status_code=502)

    return [
        Route("/api/llm/providers", handle_list_providers, methods=["GET"]),
        Route("/api/llm/providers/{name}/models", handle_get_provider_models, methods=["GET"]),
        Route("/api/llm/providers/{name}", handle_update_provider, methods=["PUT"]),
        Route("/api/llm/providers/{name}/config", handle_delete_provider, methods=["DELETE"]),
        Route("/api/llm/model-groups", handle_get_model_groups, methods=["GET"]),
        Route("/api/llm/model-groups", handle_update_model_groups, methods=["PUT"]),
        Route("/api/llm/auth/device-code/start", handle_device_code_start, methods=["POST"]),
        Route("/api/llm/auth/device-code/poll", handle_device_code_poll, methods=["POST"]),
    ]
