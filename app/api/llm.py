"""LLM provider and model group API endpoints."""

import json
import re

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


# ── Custom providers ──────────────────────────────────────────────────────
#
# A "custom provider" is a user-defined OpenAI-API-compatible endpoint added
# from the LLM Providers page (name + API Base URL + API key + a manual model
# list). Its internal name is ``custom:<slug>``. Definitions live per-profile in
# a single non-secret ``llm_config`` row keyed ``custom_providers`` (a JSON
# ``slug -> {display_name, base_url, models[...]}`` map); each provider's API key
# is a separate secret row ``custom:<slug>.api_key``. No new table / migration.

_CUSTOM_PREFIX = "custom:"


def _is_custom(name: str) -> bool:
    return bool(name) and name.startswith(_CUSTOM_PREFIX)


def _slugify(name: str) -> str:
    """Reduce a display name to a ``[a-z0-9-]`` slug used as the registry key.

    The restricted charset is deliberate: it keeps the ``custom:<slug>.`` config-
    key boundary unambiguous and avoids the ``_``/``%`` SQL ``LIKE`` wildcards
    that ``delete_by_prefix`` does not escape.
    """
    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")


def _unique_slug(base: str, registry: dict) -> str:
    """Return ``base`` (or ``base-2``, ``base-3`` …) not already in the registry."""
    base = base or "custom-provider"
    if base not in registry:
        return base
    i = 2
    while f"{base}-{i}" in registry:
        i += 1
    return f"{base}-{i}"


_REASONING_EFFORTS = ("low", "medium", "high")


def _clean_reasoning_effort(raw) -> list[str]:
    """Keep only the canonical effort levels, in canonical order.

    An empty list means the model has no native reasoning: the app injects its
    ``reasoning`` think-tool and shows no reasoning-effort selector for it.
    """
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    picked = {str(x).strip().lower() for x in raw}
    return [e for e in _REASONING_EFFORTS if e in picked]


def _opt_price(raw: dict, key: str) -> float | None:
    """Parse an optional per-1M price. Blank/absent/invalid → ``None`` (unknown
    cost); a valid ``>= 0`` number is stored (``0`` means a free model)."""
    v = raw.get(key)
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f >= 0 else None


_PRICE_FIELDS = (
    "input_price_per_1m",
    "output_price_per_1m",
    "cache_read_price_per_1m",
    "cache_write_price_per_1m",
)


def _normalize_custom_models(raw_models) -> list[dict]:
    """Coerce user-supplied model rows into the stored shape.

    Each stored model carries: ``id``, ``display_name``, a ``vision`` flag, an
    ``audio`` flag (audio input support), a
    ``supports_reasoning`` flag (whether the model supports reasoning effort),
    the derived ``reasoning_effort`` levels it exposes, and the optional
    per-1M-token prices in :data:`_PRICE_FIELDS` (present only when the user
    entered a value; absent → unknown cost). ``group_hint`` defaults to
    ``"high"`` — no longer user-facing (it only tags usage rows for the
    dashboard). Drops rows without an ``id``; returns ``[]`` for non-list input.
    """
    if not isinstance(raw_models, list):
        return []
    out: list[dict] = []
    for raw in raw_models:
        if not isinstance(raw, dict):
            continue
        mid = str(raw.get("id", "")).strip()
        if not mid:
            continue
        # Reasoning Effort is a yes/no capability. The checkbox sends
        # ``supports_reasoning``; a non-empty ``reasoning_effort`` list (e.g.
        # from a CLI/JSON payload) is also accepted as the "supported" signal.
        # When supported, expose the standard effort levels so the Model /
        # Low-Performance sections can render a selector.
        supports_reasoning = bool(raw.get("supports_reasoning", False)) or bool(
            _clean_reasoning_effort(raw.get("reasoning_effort"))
        )
        model: dict = {
            "id": mid,
            "display_name": str(raw.get("display_name") or mid).strip(),
            "group_hint": "high",
            "vision": bool(raw.get("vision", False)),
            "audio": bool(raw.get("audio", False)),
            "supports_reasoning": supports_reasoning,
            "reasoning_effort": list(_REASONING_EFFORTS) if supports_reasoning else [],
        }
        for key in _PRICE_FIELDS:
            price = _opt_price(raw, key)
            if price is not None:
                model[key] = price
        out.append(model)
    return out


def _load_custom_registry(config_storage, profile: str) -> dict:
    """Load the per-profile custom-provider registry (``slug -> definition``)."""
    raw = config_storage.get("llm_config", "custom_providers", profile=profile)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_custom_registry(config_storage, profile: str, registry: dict) -> None:
    config_storage.set(
        "llm_config", "custom_providers", json.dumps(registry),
        is_secret=False, profile=profile,
    )


def _custom_model_response(model: dict) -> dict:
    """Expand a stored custom model into the catalog ``LLMModel`` API shape.

    Surfaces the user's ``reasoning_effort`` list (drives the Reasoning Effort
    selector in the Model / Low-Performance sections) and prices (``None`` when
    unset → unknown cost, so the editor shows a blank field rather than a
    misleading ``0``).
    """
    entry = {
        "id": model["id"],
        "display_name": model.get("display_name", model["id"]),
        "group_hint": model.get("group_hint", "high"),
        "vision": bool(model.get("vision", False)),
        "audio": bool(model.get("audio", False)),
    }
    for key in _PRICE_FIELDS:
        entry[key] = model.get(key)
    effort = _clean_reasoning_effort(model.get("reasoning_effort"))
    if effort:
        entry["reasoning_effort"] = effort
    return entry


def _custom_provider_entry(name: str, defn: dict, configured: bool) -> dict:
    """Build the ``/api/llm/providers`` list entry for a custom provider."""
    return {
        "name": name,
        "display_name": defn.get("display_name", name),
        "requires_api_key": True,
        "requires_service_account": False,
        "configured": configured,
        "model_count": len(defn.get("models", []) or []),
        "auth_methods": [{
            "id": "api_key",
            "label": "API Key",
            "hint": "",
            "kind": "api_key",
            "is_default": True,
            "fields": {
                "api_key": {
                    "description": "API key for this provider",
                    "type": "string",
                    "secret": True,
                    "required": True,
                    "default": None,
                    "configured": configured,
                },
            },
        }],
        "active_auth_method": "api_key",
        "is_custom": True,
        "base_url": defn.get("base_url", ""),
    }


def _models_for_auth_method(catalog: dict, auth_method: str | None) -> list[dict]:
    """Filter a catalog's models to those visible for ``auth_method``.

    A model entry may declare ``auth_methods = ["api_key", ...]`` to restrict it
    to specific auth methods (e.g. OpenAI's Codex OAuth serves a different model
    set than the API-key path). A model with no ``auth_methods`` key is visible
    for every method — keeping every other provider's catalog unchanged.
    """
    models = catalog.get("models", []) or []
    if not auth_method:
        return models
    return [
        m for m in models
        if not (isinstance(m, dict) and m.get("auth_methods"))
        or auth_method in m.get("auth_methods", [])
    ]


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
                    elif am["kind"] == "oauth":
                        # Browser-OAuth methods carry no manually-entered fields,
                        # so an ``all(...)`` over an empty field set would be
                        # vacuously true. "Configured" means a token was captured
                        # — the access token lands in ``<provider>.oauth_token``.
                        configured = bool(_stored(f"{name}.oauth_token"))
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
                "model_count": len(_models_for_auth_method(catalog, active_auth_method)),
                "auth_methods": auth_methods_response,
                "active_auth_method": active_auth_method,
            }
            if config_fields_status:
                provider_entry["config_fields"] = config_fields_status
                provider_entry["current_values"] = current_values

            providers.append(provider_entry)

        # Append user-defined custom providers from the per-profile registry.
        if config_storage is not None:
            for slug, defn in _load_custom_registry(config_storage, profile).items():
                name = f"{_CUSTOM_PREFIX}{slug}"
                configured = bool(_stored(f"{name}.api_key"))
                providers.append(_custom_provider_entry(name, defn, configured))

        return JSONResponse({"providers": providers})

    async def handle_get_provider_models(request: Request) -> JSONResponse:
        """List models for a specific provider.

        Built-in providers read from the static TOML catalog (open pre-auth so
        the setup wizard can populate model dropdowns). Custom providers read
        from the per-profile registry and therefore require auth + storage.
        """
        provider_name = request.path_params["name"]

        if _is_custom(provider_name):
            unauth = _require_auth(request)
            if unauth is not None:
                return unauth
            config_storage = state.config_storage
            if config_storage is None:
                return _storage_not_ready()
            profile = getattr(request.user, "username", "") or ""
            defn = _load_custom_registry(config_storage, profile).get(
                provider_name[len(_CUSTOM_PREFIX):]
            )
            if not defn:
                return JSONResponse(
                    {"error": f"Provider '{provider_name}' not found"},
                    status_code=404,
                )
            return JSONResponse({
                "provider": {
                    "name": provider_name,
                    "display_name": defn.get("display_name", provider_name),
                    "base_url": defn.get("base_url", ""),
                },
                "models": [_custom_model_response(m) for m in defn.get("models", []) or []],
            })

        catalog = load_provider_catalog(provider_name)

        if not catalog:
            return JSONResponse(
                {"error": f"Provider '{provider_name}' not found"},
                status_code=404,
            )

        # Filter by the active auth method (some providers, e.g. OpenAI's Codex
        # OAuth, serve a different model set than the API-key path). A ?auth_method
        # query overrides so the setup wizard can preview a method's models before
        # saving; otherwise fall back to the stored method, then the catalog default.
        active = request.query_params.get("auth_method")
        if not active:
            config_storage = state.config_storage
            if config_storage is not None:
                profile = getattr(request.user, "username", "") or ""
                try:
                    active = config_storage.get("llm_config", f"{provider_name}.auth_method", profile=profile)
                except Exception:  # noqa: BLE001
                    active = None
        if not active:
            for am in normalize_provider_auth_methods(catalog.get("provider", {})):
                if am.get("is_default"):
                    active = am["id"]
                    break

        return JSONResponse({
            "provider": catalog.get("provider", {}),
            "models": _models_for_auth_method(catalog, active),
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

        if not _is_custom(provider_name) and provider_name not in SUPPORTED_LLM_PROVIDERS:
            return JSONResponse(
                {"error": f"Unknown provider: {provider_name}"},
                status_code=404,
            )

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        # Custom providers: merge definition fields into the registry rather than
        # writing per-field ``<provider>.<key>`` rows. The API key is still a
        # secret row; skip it when absent or the redaction sentinel so re-saving
        # without retyping the key doesn't clobber the stored value.
        if _is_custom(provider_name):
            slug = provider_name[len(_CUSTOM_PREFIX):]
            registry = _load_custom_registry(config_storage, profile)
            defn = registry.get(slug)
            if defn is None:
                return JSONResponse(
                    {"error": f"Unknown provider: {provider_name}"},
                    status_code=404,
                )
            if "display_name" in body and str(body["display_name"]).strip():
                defn["display_name"] = str(body["display_name"]).strip()
            if "base_url" in body and str(body["base_url"]).strip():
                defn["base_url"] = str(body["base_url"]).strip()
            if "models" in body:
                models = _normalize_custom_models(body.get("models"))
                if not models:
                    return JSONResponse(
                        {"error": "A custom provider needs at least one model with an id."},
                        status_code=400,
                    )
                defn["models"] = models
            registry[slug] = defn
            _save_custom_registry(config_storage, profile, registry)

            api_key = body.get("api_key")
            if api_key and api_key != "***":
                config_storage.set(
                    "llm_config", f"{provider_name}.api_key", str(api_key),
                    is_secret=True, profile=profile,
                )
            from app.events.settings_state_bus import publish_settings_state_changed
            publish_settings_state_changed(profile)
            return JSONResponse({"success": True})

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

        # Switching auth method can change the visible model set (e.g. OpenAI's
        # Codex OAuth vs API key). Clear any model-group assignment that now
        # points at a model this method can't serve, so a stale selection can't
        # silently 4xx at request time. Report which groups were cleared so the
        # UI can prompt a re-selection.
        cleared_model_groups: list[str] = []
        if "auth_method" in body:
            visible_ids = {
                m.get("id") for m in _models_for_auth_method(catalog, body.get("auth_method"))
                if isinstance(m, dict)
            }
            prefix = f"{provider_name}/"
            for group in ("high", "vision", "audio", "low", "plan"):
                gv = config_storage.get("llm_config", f"model_group.{group}", profile=profile)
                if gv and gv.startswith(prefix) and gv[len(prefix):] not in visible_ids:
                    config_storage.delete("llm_config", f"model_group.{group}", profile=profile)
                    cleared_model_groups.append(group)

        from app.events.settings_state_bus import publish_settings_state_changed
        publish_settings_state_changed(profile)
        return JSONResponse({"success": True, "cleared_model_groups": cleared_model_groups})

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

        if not _is_custom(provider_name) and provider_name not in SUPPORTED_LLM_PROVIDERS:
            return JSONResponse(
                {"error": f"Unknown provider: {provider_name}"},
                status_code=404,
            )

        # Removes the api_key (and any other ``<provider>.*``) rows. The slug's
        # ``[a-z0-9-]`` charset guarantees the ``LIKE`` prefix can't over-match a
        # sibling (``custom:acme.`` never matches ``custom:acme-2.api_key``).
        deleted = config_storage.delete_by_prefix("llm_config", f"{provider_name}.", profile=profile)

        if _is_custom(provider_name):
            slug = provider_name[len(_CUSTOM_PREFIX):]
            registry = _load_custom_registry(config_storage, profile)
            if slug in registry:
                del registry[slug]
                _save_custom_registry(config_storage, profile, registry)
            # Drop any model-group assignment that pointed at this provider so it
            # doesn't leave a dangling ``custom:<slug>/model`` reference.
            for group in ("high", "vision", "audio", "low", "plan"):
                gv = config_storage.get("llm_config", f"model_group.{group}", profile=profile)
                if gv and gv.split("/", 1)[0] == provider_name:
                    config_storage.delete("llm_config", f"model_group.{group}", profile=profile)

        logger.info(f"Deleted {deleted} config key(s) for provider '{provider_name}' (profile={profile})")
        from app.events.settings_state_bus import publish_settings_state_changed
        publish_settings_state_changed(profile)
        return JSONResponse({"success": True, "deleted_keys": deleted})

    async def handle_create_custom_provider(request: Request) -> JSONResponse:
        """Create a new user-defined OpenAI-compatible custom provider.

        Body: ``{display_name, base_url, api_key, models: [...]}``. Slugifies the
        name (de-duped within the profile), stores the definition in the
        ``custom_providers`` registry and the key in a secret row. Returns the
        internal ``{"name": "custom:<slug>"}`` for the UI to re-select.
        """
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

        display_name = str(body.get("display_name") or "").strip()
        base_url = str(body.get("base_url") or "").strip()
        api_key = body.get("api_key")
        if not display_name:
            return JSONResponse({"error": "A provider name is required."}, status_code=400)
        if not base_url:
            return JSONResponse({"error": "An API Base URL is required."}, status_code=400)
        models = _normalize_custom_models(body.get("models"))
        if not models:
            return JSONResponse(
                {"error": "Add at least one model (each needs an id)."},
                status_code=400,
            )

        registry = _load_custom_registry(config_storage, profile)
        slug = _unique_slug(_slugify(display_name), registry)
        registry[slug] = {"display_name": display_name, "base_url": base_url, "models": models}
        _save_custom_registry(config_storage, profile, registry)

        name = f"{_CUSTOM_PREFIX}{slug}"
        if api_key and api_key != "***":
            config_storage.set(
                "llm_config", f"{name}.api_key", str(api_key),
                is_secret=True, profile=profile,
            )

        from app.events.settings_state_bus import publish_settings_state_changed
        publish_settings_state_changed(profile)
        logger.info(f"Created custom provider '{name}' (profile={profile})")
        return JSONResponse({"success": True, "name": name})

    async def handle_get_model_groups(request: Request) -> JSONResponse:
        """Get the configured reasoning model (``high``) plus the optional
        ``vision``, ``audio``, ``low``, and ``plan`` models, and their reasoning_effort.

        ``high`` is the one model the agent reasons on. ``vision`` is an optional
        override used only by image_understanding; ``audio`` is an optional
        override used only by audio_understanding. ``low`` is the optional
        low-performance / cheap model used for lightweight auxiliary tasks (e.g.
        the skill-event matching gate). ``plan`` is the optional model used during
        plan mode's planning phase (the agent switches back to ``high`` once a
        plan is accepted). All optional groups fall back to the single model when
        unset.
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
        for group in ("high", "vision", "audio", "low", "plan"):
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

        # Specialized Audio Model feature toggle (opt-in; unset reads as off).
        ae_raw = config_storage.get("llm_config", "model_group.audio.enabled", profile=profile)
        audio_enabled = str(ae_raw).strip().lower() in {"1", "true", "yes", "on"}

        return JSONResponse({
            "model_groups": groups,
            "default_provider": default_provider,
            "reasoning_efforts": reasoning_efforts,
            "vision_enabled": vision_enabled,
            "audio_enabled": audio_enabled,
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
            if group in ("high", "vision", "audio", "low", "plan"):
                config_storage.set("llm_config", f"model_group.{group}", str(value), profile=profile)

        # Save reasoning_effort per group
        reasoning_efforts = body.get("reasoning_efforts", {})
        for group, value in reasoning_efforts.items():
            if group in ("high", "vision", "audio", "low", "plan"):
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

        # Specialized Audio Model feature toggle (mirrors vision_enabled).
        audio_enabled = body.get("audio_enabled")
        if audio_enabled is not None:
            config_storage.set(
                "llm_config", "model_group.audio.enabled",
                "true" if audio_enabled else "false", profile=profile,
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

    async def handle_codex_oauth_start(request: Request) -> JSONResponse:
        """Begin the OpenAI Codex 'Sign in with ChatGPT' browser flow.

        Mints PKCE state and (for a local install) starts a loopback listener on
        port 1455 to catch the redirect. Returns the authorize URL plus whether
        the listener is active — when it isn't (port busy, remote server), the
        client falls back to pasting the redirect URL.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        config_storage = state.config_storage
        if config_storage is None:
            return _storage_not_ready()
        profile = getattr(request.user, "username", "") or ""
        from app.api import llm_codex_flow
        try:
            result = await llm_codex_flow.start_flow(config_storage, profile)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Codex OAuth start failed: {e}")
            return JSONResponse({"error": str(e)}, status_code=502)
        return JSONResponse(result)

    async def handle_codex_oauth_status(request: Request) -> JSONResponse:
        """Report the status of a pending Codex sign-in flow (polled by the UI)."""
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        if state.config_storage is None:
            return _storage_not_ready()
        profile = getattr(request.user, "username", "") or ""
        st = request.query_params.get("state", "")
        from app.api.oauth_callback import _STATE_RE
        if not st or not _STATE_RE.match(st):
            return JSONResponse({"error": "valid state is required"}, status_code=400)
        from app.api import llm_codex_flow
        return JSONResponse(llm_codex_flow.get_flow_status(st, profile))

    async def handle_codex_oauth_complete(request: Request) -> JSONResponse:
        """Complete a Codex sign-in from a pasted redirect URL (remote/port-busy
        fallback). Always 200 with a ``{status: complete|error}`` body so the UI
        can surface the message rather than a bare HTTP error."""
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        if state.config_storage is None:
            return _storage_not_ready()
        profile = getattr(request.user, "username", "") or ""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
        redirect_url = str(body.get("redirect_url") or "")
        state_hint = body.get("state")
        from app.api import llm_codex_flow
        result = await llm_codex_flow.complete_from_redirect_url(
            profile, redirect_url, str(state_hint) if state_hint else None,
        )
        return JSONResponse(result)

    async def handle_codex_oauth_cancel(request: Request) -> JSONResponse:
        """Abandon a pending Codex sign-in flow and free the loopback listener."""
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        if state.config_storage is None:
            return _storage_not_ready()
        profile = getattr(request.user, "username", "") or ""
        try:
            body = await request.json()
        except Exception:
            body = {}
        st = str(body.get("state") or "")
        from app.api import llm_codex_flow
        if st:
            await llm_codex_flow.cancel_flow(st, profile)
        return JSONResponse({"success": True})

    return [
        Route("/api/llm/providers", handle_list_providers, methods=["GET"]),
        Route("/api/llm/providers/custom", handle_create_custom_provider, methods=["POST"]),
        Route("/api/llm/providers/{name}/models", handle_get_provider_models, methods=["GET"]),
        Route("/api/llm/providers/{name}", handle_update_provider, methods=["PUT"]),
        Route("/api/llm/providers/{name}/config", handle_delete_provider, methods=["DELETE"]),
        Route("/api/llm/model-groups", handle_get_model_groups, methods=["GET"]),
        Route("/api/llm/model-groups", handle_update_model_groups, methods=["PUT"]),
        Route("/api/llm/auth/device-code/start", handle_device_code_start, methods=["POST"]),
        Route("/api/llm/auth/device-code/poll", handle_device_code_poll, methods=["POST"]),
        Route("/api/llm/auth/codex/start", handle_codex_oauth_start, methods=["POST"]),
        Route("/api/llm/auth/codex/status", handle_codex_oauth_status, methods=["GET"]),
        Route("/api/llm/auth/codex/complete", handle_codex_oauth_complete, methods=["POST"]),
        Route("/api/llm/auth/codex/cancel", handle_codex_oauth_cancel, methods=["POST"]),
    ]
