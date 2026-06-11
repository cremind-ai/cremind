"""Built-in tools (in-process, child-LLM-driven).

Each tool group lives at ``app.tools.builtin.{module_name}`` and exports:

- ``TOOL_CONFIG`` (dict)              -- static config (name, display_name,
                                         default_model_group, required_config,
                                         optional ``arguments`` / ``oauth`` /
                                         ``llm_parameters``). ``llm_parameters``
                                         carries code-level defaults for the
                                         tool's tool_instructions, system
                                         prompt, provider/model, reasoning
                                         effort and full_reasoning. DB overrides
                                         (from the ``llm``/``meta`` tool-config
                                         scopes) win per-key.
- ``SERVER_NAME`` (str)               -- display name in the UI
- ``get_tools(config: dict) -> list[BuiltInTool]`` -- in-process functions
- ``get_prepare_tools()`` (optional)  -- per-request tool customization callback
- ``_make_server_instructions(working_dir: str)`` (optional) -- dynamic
                                         override for ``tool_instructions``
                                         (wins over ``TOOL_CONFIG["llm_parameters"]["tool_instructions"]``)

Registration is driven by the explicit ``_BUILTIN_MODULE_NAMES`` tuple below.
Adding a new built-in tool means adding a module in this package, exporting
``TOOL_CONFIG``, and appending the module name to the tuple.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Iterable, Optional

from app.config.settings import BaseConfig
from app.lib.llm.base import LLMProvider
from app.lib.llm.factory import create_llm_provider
from app.tools.builtin.adapter import BuiltInToolAdapter
from app.tools.builtin.base import (
    BuiltInTool,
    BuiltInToolResult,
    _StubErrorPayload,
    _StubErrorTool,
)
from app.tools.builtin.gg_calendar import Var as CalendarVar
from app.tools.builtin.tool import BuiltInToolGroup
from app.tools.config_manager import ToolConfigManager
from app.tools.ids import slugify
from app.tools.registry import ToolRegistry
from app.utils.logger import logger

__all__ = [
    "BuiltInTool",
    "BuiltInToolResult",
    "BuiltInToolAdapter",
    "BuiltInToolGroup",
    "register_builtin_tools",
    "refresh_builtin_tool_oauth",
    "get_builtin_tool_config",
    "list_builtin_tool_catalog",
    "feature_keys_for_tool_ids",
    "required_feature_for_tool_id",
]


_BUILTIN_MODULE_NAMES: tuple[str, ...] = (
    "exec_shell",
    "markdown_converter",
    "message_detail",
    "system_file",
    "weather",
    "gg_calendar",
    "gg_places",
    "browser",
    "sleep",
    "register_skill_event",
    "register_file_watcher",
    "documentation_search",
    "change_working_directory",
    "web_search",
    "web_fetch",
)


def list_builtin_tool_catalog() -> list[dict]:
    """Static built-in tool catalog, drawn from ``TOOL_CONFIG`` only.

    Used by ``GET /api/tools`` during the Setup Wizard's deferred-storage
    window — the registry is not built until ``boot_storage_and_post_storage``
    runs, but the wizard still needs to render the tool list. Rows mirror
    the post-enrichment shape of :meth:`ToolRegistry.visible_for_profile`,
    with ``enabled`` / ``configured`` / ``config`` reflecting an empty,
    no-DB baseline.
    """
    rows: list[dict] = []
    for module_name in _BUILTIN_MODULE_NAMES:
        try:
            module = importlib.import_module(f"app.tools.builtin.{module_name}")
        except ImportError as e:
            logger.warning(
                f"Built-in tool module '{module_name}' not importable for catalog: {e}"
            )
            continue

        tool_info = getattr(module, "TOOL_CONFIG", None)
        if not isinstance(tool_info, dict):
            continue
        if not tool_info.get("visible", True):
            continue
        # ``hidden`` tools are registered at runtime but suppressed from the
        # Settings UI and the Setup Wizard catalog — see ToolConfig.hidden.
        if tool_info.get("hidden", False):
            continue

        server_name = getattr(module, "SERVER_NAME", module_name)
        tool_id = slugify(server_name)
        llm_defaults: dict = dict(tool_info.get("llm_parameters") or {})
        required_fields = tool_info.get("required_config", {}) or {}
        locked_raw = tool_info.get("locked_llm_fields") or []
        locked_llm_fields: list[str] = (
            [str(k) for k in locked_raw if isinstance(k, str)]
            if isinstance(locked_raw, list) else []
        )

        # Match ``_is_tool_configured`` semantics when the snapshot is empty:
        # a tool with no required fields, or with defaults on every required
        # field, is "configured".
        configured = True
        if required_fields:
            configured = all(
                field_spec.get("default") is not None
                for field_spec in required_fields.values()
            )

        rows.append({
            "tool_id": tool_id,
            "name": server_name,
            "display_name": server_name,
            "description": (
                llm_defaults.get("description")
                or llm_defaults.get("tool_instructions")
                or server_name
            ),
            "tool_type": "builtin",
            # Default-enabled in the wizard catalog so new installs are
            # useful out of the box. Tools with unfilled required vars
            # still surface a "Needs Config" tag in the UI, and the user
            # can opt out per-tool before submitting.
            "enabled": True,
            "configured": configured,
            "config": {},
            "required_fields": required_fields,
            "llm_defaults": llm_defaults,
            "locked_llm_fields": locked_llm_fields,
            "full_reasoning": bool(llm_defaults.get("full_reasoning", False)),
            "is_stub": False,
            "arguments_schema": tool_info.get("arguments"),
            # Optional feature key whose pip extras must be installed for
            # this tool to function. The Setup Wizard renders this as an
            # "Installs: cremind[…]" hint; the post-setup enable handler
            # uses it to drive the missing-dep pre-flight (HTTP 409).
            "requires_feature": tool_info.get("requires_feature"),
        })
    return rows


def feature_keys_for_tool_ids(tool_ids: Iterable[str]) -> list[str]:
    """Return the de-duplicated feature keys required by these built-in tool ids.

    Reads each module's ``TOOL_CONFIG["requires_feature"]``. Unknown tool
    ids and tools without a feature requirement are silently dropped, so
    callers can pass arbitrary id lists from external payloads.

    Used by:

    - The Setup Wizard (``_features_required_by_setup_payload``) to union
      tool-derived features into the pip install set.
    - The post-setup enable handler (``handle_set_enabled``) to pre-flight
      the feature dependency before flipping the enabled flag.

    The mapping is one-to-one in practice today (every requirement names
    a single key in :data:`app.features.manifest.FEATURES`), but the
    return type is a list so we keep room for tools that fan out across
    multiple groups without breaking call sites.
    """
    out: list[str] = []
    seen: set[str] = set()
    # Build a slug -> module-name index once so callers can pass either
    # the registry slug (``"google_calendar"``) or the module stem
    # (``"gg_calendar"``). The Setup Wizard payload uses slugs.
    slug_to_module: dict[str, str] = {}
    for module_name in _BUILTIN_MODULE_NAMES:
        try:
            module = importlib.import_module(f"app.tools.builtin.{module_name}")
        except ImportError:
            continue
        server_name = getattr(module, "SERVER_NAME", module_name)
        slug_to_module[slugify(server_name)] = module_name
        slug_to_module[module_name] = module_name

    for raw_id in tool_ids:
        if not isinstance(raw_id, str):
            continue
        module_name = slug_to_module.get(raw_id)
        if module_name is None:
            continue
        try:
            module = importlib.import_module(f"app.tools.builtin.{module_name}")
        except ImportError:
            continue
        tool_info = getattr(module, "TOOL_CONFIG", None)
        if not isinstance(tool_info, dict):
            continue
        feature_key = tool_info.get("requires_feature")
        if not isinstance(feature_key, str) or not feature_key:
            continue
        if feature_key in seen:
            continue
        seen.add(feature_key)
        out.append(feature_key)
    return out


def required_feature_for_tool_id(tool_id: str) -> Optional[str]:
    """Return the single feature key required by a built-in tool, or None.

    Convenience wrapper for the post-setup enable handler, which needs
    one tool's feature requirement (not a batch).
    """
    keys = feature_keys_for_tool_ids([tool_id])
    return keys[0] if keys else None


def get_builtin_tool_config(config_name: str) -> dict:
    """Return the schema dict for a built-in tool, matching the old TOML shape.

    Returns ``{"tool": {...}}`` so callers previously reading the TOML can use
    this function as a drop-in replacement. Returns ``{}`` if the module or
    ``TOOL_CONFIG`` export is missing.
    """
    try:
        module = importlib.import_module(f"app.tools.builtin.{config_name}")
    except ImportError:
        return {}
    tool_config = getattr(module, "TOOL_CONFIG", None)
    if not isinstance(tool_config, dict):
        return {}
    return {"tool": tool_config}


def _build_oauth_provider(*, oauth_config: Optional[dict], server_name: str):
    """Return a callable ``adapter -> MCPOAuthClient`` if OAuth is configured."""
    if not oauth_config:
        return None

    client_id = oauth_config.get("client_id", "")
    client_secret = oauth_config.get("client_secret", "")
    if not client_id or not client_secret:
        return None

    from app.tools.mcp.mcp_auth import MCPOAuthClient

    extra_params = oauth_config.get("extra_authorize_params", {})
    auth_metadata = {
        "authorization_endpoint": oauth_config["authorization_endpoint"],
        "token_endpoint": oauth_config["token_endpoint"],
        "scopes_supported": oauth_config.get("scopes")
            or oauth_config.get("scopes_supported", []),
    }

    def factory(_adapter):
        client = MCPOAuthClient(
            server_url=f"builtin://{server_name}",
            server_name=server_name,
            client_id=client_id,
            client_secret=client_secret,
            extra_authorize_params=extra_params,
        )
        client.set_auth_metadata(auth_metadata)
        return client

    return factory


def _register_stub_group(
    registry: ToolRegistry,
    *,
    module_name: str,
    server_name: str,
    payload: _StubErrorPayload,
) -> None:
    """Register a minimal :class:`BuiltInToolGroup` containing a single
    :class:`_StubErrorTool`.

    Used when a built-in module fails to import or its ``get_tools()``
    factory raises / returns nothing. The stub keeps the tool in the
    agent's catalog so any invocation reaches it as a structured error
    instead of disappearing into a "no such tool" miss.
    """
    stub_tool = _StubErrorTool(server_name=server_name, payload=payload)
    description = (
        f"[unavailable] '{server_name}' failed to register at server boot "
        f"({payload.kind}). The tool exposes the captured error to the agent."
    )
    group = BuiltInToolGroup(
        config_name=module_name,
        display_name=server_name,
        description=description,
        functions=[stub_tool],
        # No child LLM / OAuth / prepare_tools — the stub's run() returns
        # the same structured error on any call, so the child-LLM routing
        # layer is pure overhead. ``direct_dispatch`` skips it.
        direct_dispatch=True,
    )
    registry.register_builtin(group, source=module_name)


async def register_builtin_tools(
    *,
    registry: ToolRegistry,
    config_manager: ToolConfigManager,
    llm_factory,
    setup_profile: str = "admin",
    config_storage=None,
    vector_store=None,
) -> None:
    """Register all built-in tool groups from ``_BUILTIN_MODULE_NAMES``.

    Args
    ----
    registry        : the central ToolRegistry.
    config_manager  : ToolConfigManager (used for variable lookup).
    llm_factory     : callable ``(module_name, profile) -> LLMProvider``. May
                      raise -- failures skip that one tool.
    setup_profile   : profile from which to read OAuth client_id/secret at
                      registration time. Per-profile overrides remain effective
                      at execution time (handled by the adapter).
    """
    for module_name in _BUILTIN_MODULE_NAMES:
        try:
            module = importlib.import_module(f"app.tools.builtin.{module_name}")
        except ImportError as e:
            logger.error(f"Built-in tool module '{module_name}' not importable: {e}")
            _register_stub_group(
                registry,
                module_name=module_name,
                server_name=module_name,
                payload=_StubErrorPayload(kind="ImportError", detail=str(e)),
            )
            continue

        tool_info = getattr(module, "TOOL_CONFIG", None)
        if not isinstance(tool_info, dict):
            logger.error(
                f"Built-in tool module '{module_name}' is missing a "
                "TOOL_CONFIG dict; skipping."
            )
            _register_stub_group(
                registry,
                module_name=module_name,
                server_name=getattr(module, "SERVER_NAME", module_name),
                payload=_StubErrorPayload(
                    kind="MissingToolConfig",
                    detail="TOOL_CONFIG dict not exported by the module.",
                ),
            )
            continue

        if not tool_info.get("visible", True):
            logger.info(f"Built-in tool '{module_name}' is marked as not visible; skipping.")
            continue

        config: dict[str, str] = {}
        config["CREMIND_SYSTEM_DIR"] = BaseConfig.CREMIND_SYSTEM_DIR
        config["SQLITE_DB_PATH"] = BaseConfig.SQLITE_DB_PATH

        try:
            functions = module.get_tools(config)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"get_tools failed for built-in '{module_name}': {e}")
            _register_stub_group(
                registry,
                module_name=module_name,
                server_name=getattr(module, "SERVER_NAME", module_name),
                payload=_StubErrorPayload(kind="FactoryError", detail=str(e)),
            )
            continue

        # An empty function list almost always means the tool author
        # silently swallowed a missing-dep error in ``get_tools()`` (the
        # exact anti-pattern this contract is meant to prevent). Substitute
        # a stub so the agent still sees the tool and can report the
        # failure to the user.
        if not functions:
            logger.error(
                f"Built-in tool '{module_name}' returned no functions from "
                "get_tools(); registering a stub so the agent can surface "
                "the failure. Tool authors: keep dep checks inside run()."
            )
            _register_stub_group(
                registry,
                module_name=module_name,
                server_name=getattr(module, "SERVER_NAME", module_name),
                payload=_StubErrorPayload(
                    kind="EmptyFactoryResult",
                    detail=(
                        "get_tools() returned an empty list — likely a "
                        "swallowed missing-dependency error."
                    ),
                ),
            )
            continue

        server_name = getattr(module, "SERVER_NAME", module_name)
        llm_defaults: dict = dict(tool_info.get("llm_parameters") or {})
        instructions = llm_defaults.get("tool_instructions", "") or ""
        if hasattr(module, "_make_server_instructions"):
            instructions = module._make_server_instructions(config["CREMIND_SYSTEM_DIR"])

        prepare_tools_fn = None
        if hasattr(module, "get_prepare_tools"):
            fn = module.get_prepare_tools
            kwargs = {}
            if "vector_store" in inspect.signature(fn).parameters:
                kwargs["vector_store"] = vector_store
            prepare_tools_fn = fn(**kwargs)

        # OAuth provider: read GOOGLE_CLIENT_ID/SECRET from the variable scope
        oauth_provider = None
        oauth_config = tool_info.get("oauth")
        if oauth_config:
            tool_id_for_lookup = slugify(server_name)
            try:
                vars_ = config_manager.get_variables(
                    tool_id_for_lookup, setup_profile, include_secrets=True,
                )
            except Exception:
                vars_ = {}
            populated = dict(oauth_config)
            populated.setdefault("client_id", "")
            populated.setdefault("client_secret", "")
            populated["client_id"] = vars_.get(CalendarVar.CLIENT_ID, populated["client_id"])
            populated["client_secret"] = vars_.get(
                CalendarVar.CLIENT_SECRET, populated["client_secret"],
            )
            oauth_provider = _build_oauth_provider(
                oauth_config=populated, server_name=server_name,
            )

        # Read persisted LLM params from DB (mirrors MCP hydration in server.py).
        # Per-key precedence: DB override > TOOL_CONFIG["llm_parameters"] default.
        tool_id_for_config = slugify(server_name)
        try:
            _llm_params = config_manager.get_llm_params(tool_id_for_config, setup_profile)
        except Exception:  # noqa: BLE001
            _llm_params = {}

        try:
            _meta = config_manager.get_meta(tool_id_for_config, setup_profile)
        except Exception:  # noqa: BLE001
            _meta = {}

        effective_llm: dict = {**llm_defaults, **(_llm_params or {})}
        effective_meta: dict = {
            k: llm_defaults[k]
            for k in ("system_prompt", "description")
            if k in llm_defaults
        }
        effective_meta.update(_meta or {})

        llm: Optional[LLMProvider] = None
        if effective_llm.get("llm_provider") or effective_llm.get("llm_model"):
            try:
                llm = create_llm_provider(
                    provider_name=effective_llm.get("llm_provider")
                        or BaseConfig.get_default_provider(),
                    model_name=effective_llm.get("llm_model") or None,
                    config_storage=config_storage,
                    profile=setup_profile,
                    default_reasoning_effort=effective_llm.get("reasoning_effort"),
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"Per-tool LLM hydrate failed for '{server_name}': {e}. "
                    "Falling back to default model-group LLM."
                )

        if llm is None:
            try:
                llm = llm_factory(module_name, setup_profile)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"No LLM available for built-in tool '{server_name}': {e}. "
                    "Registering as unbound -- will be rebound after setup completes."
                )
                llm = None

        group = BuiltInToolGroup(
            config_name=module_name,
            display_name=server_name,
            description=effective_meta.get("description") or instructions or server_name,
            functions=functions,
            llm=llm,
            arguments_schema=tool_info.get("arguments") or None,
            oauth_provider=oauth_provider,
            prepare_tools=prepare_tools_fn,
            full_reasoning=bool(effective_llm.get("full_reasoning", False)),
            system_prompt=effective_meta.get("system_prompt") or None,
            tool_instructions=instructions or None,
            llm_factory=llm_factory,
            direct_dispatch=bool(tool_info.get("direct_dispatch", False)),
        )
        # ``hidden`` keeps the tool out of ``visible_for_profile`` (Settings UI
        # / GET /api/tools) while ``tools_for_profile`` still exposes it to the
        # reasoning agent — see ToolConfig.hidden.
        if tool_info.get("hidden", False):
            group.hidden = True
        registry.register_builtin(group, source=module_name)


def refresh_builtin_tool_oauth(
    registry: ToolRegistry,
    config_manager: ToolConfigManager,
    tool_id: str,
    profile: str = "admin",
) -> bool:
    """Reload OAuth client for a built-in tool whose variables were updated."""
    tool = registry.get(tool_id)
    if not isinstance(tool, BuiltInToolGroup):
        return False

    schema = get_builtin_tool_config(tool.config_name)
    oauth_config = schema.get("tool", {}).get("oauth")
    if not oauth_config:
        return False

    vars_ = config_manager.get_variables(tool_id, profile, include_secrets=True)
    populated = dict(oauth_config)
    populated.setdefault("client_id", "")
    populated.setdefault("client_secret", "")
    populated["client_id"] = vars_.get(CalendarVar.CLIENT_ID, populated["client_id"])
    populated["client_secret"] = vars_.get(
        CalendarVar.CLIENT_SECRET, populated["client_secret"],
    )

    factory = _build_oauth_provider(
        oauth_config=populated, server_name=tool.name,
    )
    if factory is None:
        return False
    tool.adapter._mcp_auth = factory(tool.adapter)
    logger.info(f"Refreshed OAuth for built-in tool '{tool.name}' (profile={profile})")
    return True
