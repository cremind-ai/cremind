"""Cremind HTTP server.

Startup sequence:
1. Initialize storage (sync config + async conversation storage).
2. Build the central :class:`ToolRegistry` (storage + scoped config manager).
3. Register intrinsic tools (always, hard-coded).
4. Initialize built-in tools (TOML-driven, child LLM = "low" model group).
5. Hydrate the registry with persisted A2A and MCP tools.
6. Scan + register skills, then start the file watcher with hot-reload.
7. Build the CremindAgent / executor and serve.
"""

from __future__ import annotations

import asyncio
import os

import jwt
import uvicorn
from starlette.applications import Starlette
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    SimpleUser,
)
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from a2a.server.apps import A2AStarletteApplication, CallContextBuilder
from a2a.server.agent_execution.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityScheme,
)

from pathlib import Path

from app.agent.agent import CremindAgent
from app.agent.executor import CremindAgentExecutor
from app.api import get_api_routes
from app.api.config import get_config_routes
from app.api.features import get_features_routes
from app.api.llm import get_llm_routes
from app.api.setup_stream import get_setup_stream_routes
from app.api.skills import get_skill_routes
from app.api.system import get_system_routes
from app.api.tools import get_tool_routes
from app.api.upgrade import get_upgrade_routes
from app.api.version import get_version_routes
from app.config.bootstrap import bootstrap_exists
from app.config.settings import BaseConfig, set_dynamic_config_storage
from app.runtime import BootedState, get_state
from app.constants import INTRODUCE_ASSISTANT
from app.documents import (
    DocumentSyncService,
    set_service as set_document_service,
)
from app.documents.sync import SHARED_SCOPE
from app.documents.watcher import DocumentWatcher
from app.lib.embedding import LocalEmbeddings
from app.lib.llm.factory import create_llm_provider
from app.lib.llm.model_groups import ModelGroupManager
from app.databases import create_database_provider, get_database_provider, set_database_provider
from app.storage import (
    get_autostart_storage,
    get_conversation_storage,
    get_dynamic_config_storage,
    get_tool_storage,
)
from app.tools import (
    ToolConfigManager,
    ToolRegistry,
    ToolType,
    set_tool_registry,
)
from app.tools.a2a import build_a2a_stub, build_a2a_tool
from app.tools.builtin import (
    BuiltInToolGroup,
    refresh_builtin_tool_oauth,
    register_builtin_tools,
)
from app.channels.sidecars.bootstrap import ensure_all_sidecars_installed
from app.tools.builtin.exec_shell import cleanup_stdout_on_startup
from app.tools.builtin.exec_shell_autostart import run_autostart_on_boot
from app.tools.intrinsic import register_intrinsic_tools
from app.tools.mcp import (
    build_http_mcp_tool,
    build_mcp_stub,
    build_stdio_mcp_tool,
)
from app.skills import (
    initialize_profile_skills,
    profile_skills_dir,
    stop_all_watchers,
)
from app.utils.logger import logger

DEFAULT_HOST = BaseConfig.HOST
DEFAULT_PORT = BaseConfig.PORT

# Embedding model and vector store are instantiated lazily inside ``serve()``
# (after the dynamic config storage has been populated). Both are ``None``
# when Vector Embedding is disabled in setup.

_pending_return_urls: dict[tuple[str, str], str] = {}


# ── auth middleware ────────────────────────────────────────────────────────


class JWTAuthBackend(AuthenticationBackend):
    """JWT auth with signature verification (secret resolved per request)."""

    def __init__(self, secret_provider, algorithms=None):
        self.secret_provider = secret_provider
        self.algorithms = algorithms or ["HS256"]

    async def authenticate(self, conn: HTTPConnection):
        auth_header = conn.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return None
        secret = self.secret_provider()
        if not secret:
            return None
        token = auth_header.split("Bearer ", 1)[1]
        try:
            payload = jwt.decode(token, secret, algorithms=self.algorithms)
            return AuthCredentials(["authenticated"]), SimpleUser(payload.get("sub", "anonymous"))
        except jwt.InvalidTokenError as e:
            logger.warning(f"[auth] JWT decode failed on {conn.url.path}: {e}")
            return None


class JWTCallContextBuilder(CallContextBuilder):
    """Build ServerCallContext from a Starlette Request, extracting JWT profile."""

    def __init__(self, secret_provider):
        self.secret_provider = secret_provider

    def build(self, request: Request) -> ServerCallContext:
        state: dict = {}
        secret = self.secret_provider()
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer ") and secret:
            token = auth_header.split("Bearer ", 1)[1]
            try:
                payload = jwt.decode(token, secret, algorithms=["HS256"])
                state["profile"] = payload.get("profile", "")
                state["sub"] = payload.get("sub", "")
            except jwt.InvalidTokenError as e:
                logger.warning(f"[auth] CallContextBuilder JWT decode failed: {e}")
        return ServerCallContext(state=state)


# ── persisted-tool hydration ───────────────────────────────────────────────


_STUB_ERR_LAZY = "Not connected (disabled by all profiles)"


async def _connect_a2a_tool(row: dict):
    """Build a live A2A tool from a persisted row; fall back to a stub on failure."""
    url = row["source"]
    owner = row["owner_profile"]
    try:
        return await build_a2a_tool(url=url, owner_profile=owner)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"A2A tool '{url}' unreachable: {e}")
        return build_a2a_stub(
            url=url,
            name=row["name"],
            owner_profile=owner,
            error=str(e),
        )


async def _connect_mcp_tool(
    registry: ToolRegistry,
    model_group_mgr: ModelGroupManager,
    row: dict,
):
    """Build a live MCP tool from a persisted row; fall back to a stub on failure.

    Preserves startup's LLM-resolution chain: per-tool LLM override via
    ``config.get_llm_params`` → fallback to ``model_group_mgr`` "low" group.
    Honors ``full_reasoning`` and the ``stdio`` vs ``http`` transport branch.
    """
    url = row["source"]
    owner = row["owner_profile"]
    extra = row.get("extra") or {}
    tool_id = row["tool_id"]

    def _mcp_llm_factory(profile: str) -> LLMProvider:
        """Create an LLM for this MCP tool, respecting per-tool overrides."""
        try:
            _params = registry.config.get_llm_params(tool_id, profile)
        except Exception:  # noqa: BLE001
            _params = {}
        if _params.get("llm_provider") or _params.get("llm_model"):
            return create_llm_provider(
                provider_name=_params.get("llm_provider") or BaseConfig.get_default_provider(),
                model_name=_params.get("llm_model"),
                config_storage=model_group_mgr.config_storage,
                profile=profile,
                default_reasoning_effort=_params.get("reasoning_effort"),
            )
        return model_group_mgr.create_llm_for_group("low", profile=profile)

    try:
        llm_params = registry.config.get_llm_params(tool_id, owner or "admin")
    except Exception:
        llm_params = {}
    llm = None
    try:
        llm = _mcp_llm_factory(owner or "admin")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"No LLM for MCP server '{url}': {e}")

    tool = None
    if llm is not None:
        try:
            if extra.get("transport_type") == "stdio":
                tool = await build_stdio_mcp_tool(
                    command=extra["command"],
                    args=extra.get("args", []),
                    env=extra.get("env"),
                    llm=llm,
                    owner_profile=owner,
                    full_reasoning=bool(llm_params.get("full_reasoning", False)),
                    llm_factory=_mcp_llm_factory,
                )
            else:
                tool = await build_http_mcp_tool(
                    url=url,
                    llm=llm,
                    owner_profile=owner,
                    full_reasoning=bool(llm_params.get("full_reasoning", False)),
                    llm_factory=_mcp_llm_factory,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"MCP tool '{url}' unreachable: {e}")
    if tool is None:
        tool = build_mcp_stub(
            url=url,
            name=row["name"],
            owner_profile=owner,
            error="LLM not available" if llm is None else "Connection failed",
            transport_type=extra.get("transport_type", "http"),
            extra=extra,
        )
    return tool


def _lazy_a2a_stub_from_row(row: dict):
    return build_a2a_stub(
        url=row["source"],
        name=row["name"],
        owner_profile=row["owner_profile"],
        error=_STUB_ERR_LAZY,
    )


def _lazy_mcp_stub_from_row(row: dict):
    extra = row.get("extra") or {}
    return build_mcp_stub(
        url=row["source"],
        name=row["name"],
        owner_profile=row["owner_profile"],
        error=_STUB_ERR_LAZY,
        transport_type=extra.get("transport_type", "http"),
        extra=extra,
    )


async def _hydrate_persisted_tools(
    *,
    registry: ToolRegistry,
    model_group_mgr: ModelGroupManager,
) -> None:
    """Hydrate A2A / MCP tools recorded in the ``tools`` table.

    Lazy initialization: only tools with at least one profile having
    ``enabled = 1`` are actually connected. Everything else is registered
    as a stub and upgraded on-demand via :func:`connect_persisted_tool`
    when a user toggles it on in Settings.
    """
    enabled_ids = registry.storage.list_tool_ids_enabled_by_any_profile()
    persisted = registry.storage.list_tools()

    for row in persisted:
        tool_type = row["tool_type"]
        if tool_type not in (ToolType.A2A.value, ToolType.MCP.value):
            continue
        tool_id = row["tool_id"]
        if tool_id in enabled_ids:
            if tool_type == ToolType.A2A.value:
                tool = await _connect_a2a_tool(row)
            else:
                tool = await _connect_mcp_tool(registry, model_group_mgr, row)
        else:
            if tool_type == ToolType.A2A.value:
                tool = _lazy_a2a_stub_from_row(row)
            else:
                tool = _lazy_mcp_stub_from_row(row)
            logger.info(
                f"{tool_type.upper()} tool '{row['name']}' (tool_id={tool_id}) "
                f"registered as stub — no profile has it enabled"
            )
        registry._tools[tool_id] = tool  # type: ignore[attr-defined]
        tool.tool_id = tool_id


# ── lazy connect (runtime) ─────────────────────────────────────────────────

_pending_connects: dict[str, asyncio.Task] = {}


async def connect_persisted_tool(
    registry: ToolRegistry,
    model_group_mgr: ModelGroupManager,
    tool_id: str,
) -> tuple[bool, str | None]:
    """Upgrade a stub A2A/MCP tool to a live connection, in place.

    Returns ``(success, error_message)``:
    - ``(True, None)`` — tool is now connected (or was already connected).
    - ``(False, "...")`` — connection attempt failed; stub remains in place.

    Concurrent calls for the same ``tool_id`` share a single in-flight task,
    so rapid toggle-on/off/on clicks collapse to one connection attempt.
    Callers that want fire-and-forget semantics should wrap in
    ``asyncio.create_task(...)``.
    """
    existing = _pending_connects.get(tool_id)
    if existing is not None and not existing.done():
        return await existing

    async def _do() -> tuple[bool, str | None]:
        try:
            tool = registry.get(tool_id)
            if tool is None:
                return False, f"Tool '{tool_id}' not registered"
            if tool.tool_type not in (ToolType.A2A, ToolType.MCP):
                return False, f"Tool '{tool_id}' is not an A2A/MCP tool"
            if not getattr(tool, "is_stub", False):
                return True, None  # already connected

            row = registry.storage.get_tool(tool_id)
            if row is None:
                return False, f"Tool '{tool_id}' not persisted"

            if tool.tool_type is ToolType.A2A:
                new_tool = await _connect_a2a_tool(row)
            else:
                new_tool = await _connect_mcp_tool(registry, model_group_mgr, row)

            if getattr(new_tool, "is_stub", False):
                err = getattr(new_tool, "connection_error", None) or "connection failed"
                return False, str(err)
            await registry.replace_tool(tool_id, new_tool)
            logger.info(f"Lazy-connected tool '{tool_id}'")
            return True, None
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Lazy connect failed for '{tool_id}'")
            return False, str(e)

    task = asyncio.create_task(_do())
    _pending_connects[tool_id] = task
    try:
        return await task
    finally:
        _pending_connects.pop(tool_id, None)


# ── main ───────────────────────────────────────────────────────────────────


def _silence_proactor_connection_reset(loop: asyncio.AbstractEventLoop) -> None:
    """Suppress noisy ConnectionResetError tracebacks from the Windows ProactorEventLoop.

    When a client aborts an HTTP request, _ProactorBasePipeTransport._call_connection_lost
    calls socket.shutdown() on an already-dead socket, which raises WinError 10054. The
    exception is harmless (the connection is already gone) but the default handler logs
    a full traceback for every drop. Filter just that one case; defer to default for the rest.
    """
    default_handler = loop.get_exception_handler()

    def _handler(loop, context):
        exc = context.get("exception")
        if isinstance(exc, ConnectionResetError):
            return
        if default_handler is not None:
            default_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)


# Hard cap on the time the lifespan shutdown hook may spend cleaning
# up. If channel adapters or skill watchers stall (network calls
# without their own timeouts), the wrapper below cancels them and
# lets the lifespan dispatcher unblock. Under Docker / Electron this
# is the difference between a clean container restart and the in-app
# upgrade stalling forever on the dying container.
SHUTDOWN_TIMEOUT_S = 8.0


async def _do_shutdown() -> None:
    """The actual cleanup body. Module-level so tests can patch it."""
    try:
        stop_all_watchers()
    except Exception:  # noqa: BLE001
        logger.exception("Error stopping skill watchers during shutdown")
    try:
        from app.channels import get_channel_registry

        await get_channel_registry().stop_all()
    except Exception:  # noqa: BLE001
        logger.exception("Error stopping channel adapters during shutdown")


async def _on_shutdown() -> None:
    """Lifespan-shutdown hook registered on the Starlette app.

    Bounds the inner cleanup with ``asyncio.wait_for`` so a hung
    channel adapter can't park ``cremind serve`` forever. uvicorn's
    ``timeout_graceful_shutdown`` only bounds the *connection-drain*
    phase, not the lifespan dispatcher (see uvicorn/server.py:271-301
    and uvicorn/lifespan/on.py:64-70), so the timeout has to live
    HERE, in the hook itself.
    """
    try:
        await asyncio.wait_for(_do_shutdown(), timeout=SHUTDOWN_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning(
            f"on_shutdown exceeded {SHUTDOWN_TIMEOUT_S}s; abandoning "
            "graceful cleanup so the supervisor can restart us."
        )


async def main(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
    _silence_proactor_connection_reset(asyncio.get_running_loop())

    # 0. Purge stale exec_shell stdout directories from previous runs.
    cleanup_stdout_on_startup()

    # 0a. Clear the in-app upgrade status file if the previous run
    #     terminated (done/failed). Leaves an in-flight upgrade alone
    #     so a crashed detached runner can still be observed.
    try:
        from app.upgrade.status import clear_if_terminal as _clear_upgrade_status

        _clear_upgrade_status()
    except Exception as e:  # noqa: BLE001
        # Best-effort; a missing module or unreadable file mustn't
        # block startup. The /api/upgrade/status endpoint handles a
        # corrupt file gracefully on its own.
        logger.debug(f"[boot] upgrade-status clear best-effort failed: {e}")

    # 0a'. If the previous run left a recent terminal status file
    #      (still inside TERMINAL_GRACE_S), append a boot marker so the
    #      renderer's first post-reconnect poll observes an unambiguous
    #      "the new backend is up on version X" line in the log tail.
    #      This is the positive signal the renderer needs to settle the
    #      UI out of ``applying`` even when the version-change reload
    #      hasn't fired yet.
    try:
        import time as _time

        from app.__version__ import __version__ as _version
        from app.upgrade import status as _upgrade_status

        _path = _upgrade_status.status_path()
        if _path.is_file():
            _state = _upgrade_status.read()
            _phase = _state.get("phase")
            _finished = _state.get("finished_at")
            if _phase in ("done", "failed") and isinstance(
                _finished, (int, float)
            ) and _time.time() - _finished < _upgrade_status.TERMINAL_GRACE_S:
                _upgrade_status.append_log(
                    f"[boot] backend {_version} started"
                )
    except Exception as e:  # noqa: BLE001
        # Same best-effort rationale as the clear above.
        logger.debug(f"[boot] upgrade-status boot-marker best-effort failed: {e}")

    # 0b. Verify channel sidecars' node_modules; reinstall if missing or
    #     stale. Blocks startup so adapters never see a half-installed tree.
    ensure_all_sidecars_installed()

    # 0c. Runtime state. Storage-dependent objects are populated by the
    #     deferred boot closure below.
    state = get_state()
    state.boot_lock = asyncio.Lock()

    # ── Deferred agent executor ──────────────────────────────────────────
    #
    # The A2A application is constructed *before* storage is materialised
    # (deferred mode) or before the agent itself exists. Wrap with a
    # delegator that resolves to the real executor once
    # ``boot_storage_and_post_storage`` populates state.
    class _DeferredAgentExecutor:
        def __init__(self, state_ref: BootedState):
            self._state = state_ref

        def __getattr__(self, item):
            real = self._state.agent_executor
            if real is None:
                raise RuntimeError("Cremind setup is not complete")
            return getattr(real, item)

    deferred_executor = _DeferredAgentExecutor(state)

    # ── Agent card (storage-free) ────────────────────────────────────────
    skill = AgentSkill(
        id=BaseConfig.AGENT_ID,
        name=BaseConfig.AGENT_NAME,
        description=INTRODUCE_ASSISTANT,
        tags=["assistant"],
        examples=[
            "Help me with my tasks",
            "What can you do?",
            "Tell me a joke",
            "What's the weather like today?",
            "Set a reminder for me",
            "Control my smart home devices",
        ],
    )
    agent_card_kwargs = dict(
        name=BaseConfig.AGENT_NAME,
        description=INTRODUCE_ASSISTANT,
        url=BaseConfig.APP_URL,
        version="1.0.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        skills=[skill],
    )
    jwt_secret = BaseConfig.get_jwt_secret()
    if jwt_secret:
        agent_card_kwargs["security_schemes"] = {
            "bearerAuth": SecurityScheme(
                root=HTTPAuthSecurityScheme(
                    scheme="bearer",
                    bearer_format="JWT",
                    type="http",
                    description="JWT Bearer token authentication",
                )
            ),
        }
        agent_card_kwargs["security"] = [{"bearerAuth": []}]
    agent_card = AgentCard(**agent_card_kwargs)

    request_handler = DefaultRequestHandler(
        agent_executor=deferred_executor,
        task_store=InMemoryTaskStore(),
    )
    context_builder = JWTCallContextBuilder(secret_provider=BaseConfig.get_jwt_secret)
    a2a_app = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
        context_builder=context_builder,
    )
    routes = a2a_app.routes()

    async def test_endpoint(request: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")

    routes.append(Route(path="/test", methods=["GET"], endpoint=test_endpoint))

    # Always-available pre-storage routes. The wizard endpoints in
    # ``get_config_routes`` self-gate on ``state.storage_ready`` and the
    # ``POST /api/config/setup`` handler triggers the deferred boot.
    # ``get_llm_routes`` and ``get_tool_routes`` resolve their dependencies
    # (config_storage, registry, connect_persisted_tool) through ``state``
    # at request time, so the same registration serves the Setup Wizard
    # (registry=None → catalog-only) and the post-boot UI seamlessly.
    routes.extend(get_version_routes())
    routes.extend(get_upgrade_routes())
    routes.extend(get_system_routes())
    routes.extend(get_features_routes())
    routes.extend(get_config_routes(state))
    routes.extend(get_llm_routes(state))
    routes.extend(get_tool_routes(state))
    routes.extend(get_skill_routes(state))
    routes.extend(get_setup_stream_routes())

    middleware_stack = [
        Middleware(
            CORSMiddleware,
            allow_origins=BaseConfig.CORS_ALLOWED_ORIGINS,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        ),
        Middleware(
            AuthenticationMiddleware,
            backend=JWTAuthBackend(secret_provider=BaseConfig.get_jwt_secret),
        ),
    ]
    from app.middleware import A2AAuthGuard

    middleware_stack.append(Middleware(A2AAuthGuard))

    app = Starlette(
        routes=routes,
        middleware=middleware_stack,
        on_shutdown=[_on_shutdown],
    )

    # ── Deferred storage + post-storage boot ─────────────────────────────
    #
    # Runs once: either eagerly at process start (when ``bootstrap.toml``
    # exists) or from inside ``handle_setup`` after the Setup Wizard
    # writes ``bootstrap.toml``. Guarded by ``state.boot_lock`` so a
    # concurrent setup re-attempt observes ``storage_ready`` and bails.
    async def boot_storage_and_post_storage() -> None:
        async with state.boot_lock:
            if state.storage_ready:
                return

            import time as _boot_time

            _boot_start = _boot_time.monotonic()

            # 1. Database provider + storage. Provider is selected from
            #    bootstrap.toml; storage modules obtain their engines from
            #    it lazily.
            set_database_provider(create_database_provider())
            config_storage = get_dynamic_config_storage()
            set_dynamic_config_storage(config_storage)
            conversation_storage = get_conversation_storage()
            await conversation_storage.initialize()
            tool_storage = get_tool_storage()

            # 2. Registry
            config_manager = ToolConfigManager(tool_storage)
            registry = ToolRegistry(tool_storage, config_manager)
            set_tool_registry(registry)

            # 3. Intrinsic tools
            register_intrinsic_tools(registry)

            # 3b. Embedding model + vector store (only when enabled).
            from app.config.embedding_state import embedding_state, initialize_embedding_subsystem

            embedding: LocalEmbeddings | None = None
            vector_store = None
            if BaseConfig.is_embedding_enabled():
                try:
                    embedding, vector_store = initialize_embedding_subsystem()
                    provider_name = BaseConfig.get_vectorstore_provider()
                    logger.info(f"Vector store connected (provider={provider_name})")
                except Exception:
                    logger.exception("Vector embedding subsystem failed to initialize at boot.")
                    embedding = None
                    vector_store = None
            else:
                embedding_state.mark_disabled()
                logger.info("Vector embedding disabled — skipping model load and vector store.")

            # 4. Model groups + built-in tools
            model_group_mgr = ModelGroupManager(config_storage)

            def _builtin_llm_factory(tool_id: str, profile: str):
                """Create an LLM for a built-in tool, respecting per-tool overrides."""
                try:
                    _llm_params = config_manager.get_llm_params(tool_id, profile)
                except Exception:  # noqa: BLE001
                    _llm_params = {}
                if _llm_params.get("llm_provider") or _llm_params.get("llm_model"):
                    return create_llm_provider(
                        provider_name=_llm_params.get("llm_provider") or BaseConfig.get_default_provider(),
                        model_name=_llm_params.get("llm_model") or None,
                        config_storage=config_storage,
                        profile=profile,
                        default_reasoning_effort=_llm_params.get("reasoning_effort"),
                    )
                return model_group_mgr.create_llm_for_group("low", profile=profile)

            try:
                await register_builtin_tools(
                    registry=registry,
                    config_manager=config_manager,
                    llm_factory=_builtin_llm_factory,
                    setup_profile="admin",
                    config_storage=config_storage,
                    vector_store=vector_store,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Built-in tool registration failed: {e}")

            # 4b. If setup is already complete, rebind any built-in tools
            #     that registered with llm=None.
            if config_storage.is_setup_complete():
                for tool in registry.all_tools():
                    if isinstance(tool, BuiltInToolGroup) and not tool.is_llm_bound:
                        try:
                            llm = _builtin_llm_factory(tool.config_name, "admin")
                            tool.update_runtime_config(llm=llm)
                        except Exception as e:  # noqa: BLE001
                            logger.warning(f"Could not bind LLM to built-in tool '{tool.name}' " f"at startup: {e}")

            # 5. Hydrate persisted A2A / MCP tools
            await _hydrate_persisted_tools(registry=registry, model_group_mgr=model_group_mgr)
            logger.info(f"[boot] hydrated {len(registry.storage.list_tools())} persisted tools")

            # 6. Skills -- per-profile sync
            known_profiles = [
                row["name"] for row in await conversation_storage.list_profiles() if not row["name"].startswith("__")
            ]
            logger.info(f"[boot] initialising skills for {len(known_profiles)} profile(s)")
            try:
                removed = registry.purge_legacy_skill_rows(str(profile_skills_dir(p)) for p in known_profiles)
                if removed:
                    logger.info(f"Purged {removed} legacy skill row(s)")
            except Exception:  # noqa: BLE001
                logger.exception("Legacy skill-row purge failed")

            loop = asyncio.get_running_loop()
            for profile_name in known_profiles:
                try:
                    await initialize_profile_skills(profile_name, registry, loop=loop)
                except Exception:  # noqa: BLE001
                    logger.exception(f"Skill init failed for profile '{profile_name}'")

            # 6b. Documentation Search
            #
            # The reconcile step embeds existing ``.md`` files; the watcher
            # picks up live edits. They're independent — the reconcile is
            # synchronous and the watcher arms after it returns.
            document_service = None
            try:
                document_service = DocumentSyncService(
                    working_dir=Path(BaseConfig.CREMIND_SYSTEM_DIR),
                    vector_store=vector_store,
                    embedding=embedding,
                )
                set_document_service(document_service)

                bundled_docs = Path(__file__).resolve().parents[1] / "documents"
                document_service.seed_shared_from_app(bundled_docs)

                document_service.full_reconcile(SHARED_SCOPE)
                DocumentWatcher(
                    scope=SHARED_SCOPE,
                    directory=document_service.shared_dir(),
                    sync_service=document_service,
                ).start()

                for profile_name in known_profiles:
                    try:
                        document_service.full_reconcile(profile_name)
                    except Exception:  # noqa: BLE001
                        logger.exception(f"Document reconcile failed for profile '{profile_name}'")
                    try:
                        DocumentWatcher(
                            scope=profile_name,
                            directory=document_service.profile_dir(profile_name),
                            sync_service=document_service,
                        ).start()
                    except Exception:  # noqa: BLE001
                        logger.exception(f"Document watcher failed for profile '{profile_name}'")
            except Exception:  # noqa: BLE001
                logger.exception("Documentation Search subsystem failed to initialize")

            # 7. High-group LLM (admin) + CremindAgent
            runner = None
            if config_storage.is_setup_complete():
                try:
                    runner = model_group_mgr.create_llm_for_group("high", profile="admin")
                except ValueError as e:
                    logger.warning(f"Failed to create 'high' group LLM: {e}")

            cremind_agent = CremindAgent(
                registry=registry,
                embedding=embedding,
                runner=runner,
                model_group_mgr=model_group_mgr,
                config_storage=config_storage,
                vector_store=vector_store,
                conversation_storage=conversation_storage,
            )

            # 7b. Eager embedding sync
            for profile_name in known_profiles:
                try:
                    cremind_agent.update_embeddings(profile_name)
                except Exception:  # noqa: BLE001
                    logger.exception(f"Eager embedding sync failed for profile '{profile_name}'")

            # 7c. Autostart long-running processes
            try:
                asyncio.create_task(run_autostart_on_boot(get_autostart_storage()))
            except Exception:  # noqa: BLE001
                logger.exception("Failed to schedule autostart run on boot")

            # 7d. Skill event manager
            try:
                from app.events import get_event_manager
                from app.events import runner as event_runner

                event_runner.set_globals(
                    cremind_agent=cremind_agent,
                    conversation_storage=conversation_storage,
                )
                get_event_manager().start(loop)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to start skill event manager")

            # 7e. File watcher manager
            try:
                from app.events import get_file_watcher_manager

                get_file_watcher_manager().start(loop)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to start file watcher manager")

            # 8. Build the real agent executor and the post-setup callback.
            agent_executor = CremindAgentExecutor(
                cremind_agent,
                conversation_storage=conversation_storage,
            )

            async def on_first_setup(profile: str) -> None:
                # Built-in tools registered at startup may have llm=None.
                # Rebind and refresh OAuth using the freshly-saved profile
                # credentials.
                for tool in registry.all_tools():
                    if not isinstance(tool, BuiltInToolGroup):
                        continue
                    try:
                        llm = _builtin_llm_factory(tool.config_name, profile)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"Could not bind LLM to built-in tool '{tool.name}' " f"after setup: {e}")
                        continue
                    tool.update_runtime_config(llm=llm)
                    try:
                        refresh_builtin_tool_oauth(
                            registry,
                            config_manager,
                            tool.tool_id,
                            profile=profile,
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(f"OAuth refresh failed for built-in tool '{tool.name}'")
                try:
                    await initialize_profile_skills(
                        profile,
                        registry,
                        loop=asyncio.get_running_loop(),
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(f"Post-setup skill init failed for profile '{profile}'")

                if document_service is not None:
                    try:
                        document_service.full_reconcile(profile)
                    except Exception:  # noqa: BLE001
                        logger.exception(f"Post-setup document reconcile failed for profile '{profile}'")
                    try:
                        DocumentWatcher(
                            scope=profile,
                            directory=document_service.profile_dir(profile),
                            sync_service=document_service,
                        ).start()
                    except Exception:  # noqa: BLE001
                        logger.exception(f"Post-setup document watcher failed for profile '{profile}'")

                cremind_agent.update_embeddings()
                logger.info("Post-setup: built-in tools rebound and skills synced")

            def _mcp_llm_factory(tool_id: str | None = None, profile: str = "admin"):
                """Create an LLM for an MCP tool, respecting per-tool overrides."""
                if tool_id:
                    try:
                        _params = config_manager.get_llm_params(tool_id, profile)
                    except Exception:  # noqa: BLE001
                        _params = {}
                    if _params.get("llm_provider") or _params.get("llm_model"):
                        return create_llm_provider(
                            provider_name=_params.get("llm_provider") or BaseConfig.get_default_provider(),
                            model_name=_params.get("llm_model") or None,
                            config_storage=config_storage,
                            profile=profile,
                            default_reasoning_effort=_params.get("reasoning_effort"),
                        )
                try:
                    return model_group_mgr.create_llm_for_group("low", profile=profile)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"Failed to create MCP LLM: {e}")
                    return None

            async def _connect_persisted_tool(tool_id: str) -> tuple[bool, str | None]:
                return await connect_persisted_tool(registry, model_group_mgr, tool_id)

            # 9. Register post-storage API routes onto the live Starlette
            #    app. The wizard config routes are already mounted; the
            #    rest come from ``get_api_routes``.
            api_routes = get_api_routes(
                registry=registry,
                pending_return_urls=_pending_return_urls,
                mcp_llm_factory=_mcp_llm_factory,
                conversation_storage=conversation_storage,
                config_storage=config_storage,
                connect_persisted_tool=_connect_persisted_tool,
                drop_profile_embeddings=cremind_agent.drop_profile_embeddings,
                agent_executor=agent_executor,
            )
            app.router.routes.extend(api_routes)

            # 10. Publish into shared state. Order matters: do this BEFORE
            #     starting channel adapters so any adapter that immediately
            #     consults ``state`` resolves cleanly.
            state.config_storage = config_storage
            state.conversation_storage = conversation_storage
            state.registry = registry
            state.config_manager = config_manager
            state.model_group_mgr = model_group_mgr
            state.cremind_agent = cremind_agent
            state.agent_executor = agent_executor
            state.document_service = document_service
            state.embedding = embedding
            state.vector_store = vector_store
            state.on_first_setup = on_first_setup
            state.connect_persisted_tool = _connect_persisted_tool
            state.storage_ready = True

            # 11. Start in-process channel adapters for every enabled
            #     non-main channel. Schema (and auto-created main channels)
            #     is in place after ``conversation_storage.initialize``.
            try:
                from app.channels import get_channel_registry

                await get_channel_registry(conversation_storage).start_all_enabled()
            except Exception:  # noqa: BLE001
                logger.exception("Error starting channel adapters during boot")

            try:
                _db_backend = get_database_provider().name
            except Exception:  # noqa: BLE001
                _db_backend = "unknown"
            logger.info(
                f"[boot] complete: profiles={len(known_profiles)} "
                f"tools={len(list(registry.all_tools()))} "
                f"db_backend={_db_backend} "
                f"embedding={'on' if embedding is not None else 'off'} "
                f"elapsed={_boot_time.monotonic() - _boot_start:.1f}s"
            )

    state.boot_fn = boot_storage_and_post_storage

    # Run the boot now in normal mode; in deferred mode the wizard's
    # POST /api/config/setup triggers it instead.
    if bootstrap_exists():
        await boot_storage_and_post_storage()
    else:
        logger.info(
            "No bootstrap.toml — entering deferred-storage mode. "
            "The Setup Wizard's POST /api/config/setup will materialise storage."
        )

    # ``timeout_graceful_shutdown`` bounds connection drain (SSE streams
    # under /api/upgrade/stream and /api/services/logs/stream would
    # otherwise hold the server alive). It does NOT bound the lifespan
    # shutdown hook — that's handled in _on_shutdown itself via
    # asyncio.wait_for. Keep both.
    config = uvicorn.Config(
        app, host=host, port=port, timeout_graceful_shutdown=10
    )

    import os
    import threading

    # Detect supervised environments where a hung shutdown would block
    # a container restart: Docker (INSTALL_MODE set by the entrypoint /
    # docker-compose template) and Electron (CREMIND_ELECTRON_PARENT set
    # by the Electron main process when it spawns us). Bare ``cremind
    # serve`` in dev keeps the old clean-shutdown behaviour so Ctrl-C
    # doesn't truncate work.
    _supervised = (
        os.environ.get("INSTALL_MODE") == "docker"
        or os.environ.get("CREMIND_ELECTRON_PARENT") is not None
    )

    class _BoundedShutdownServer(uvicorn.Server):
        """uvicorn.Server with a hard exit-deadline on SIGTERM/SIGINT.

        The detached upgrade runner SIGTERMs us at the end of an
        ``apply`` and expects the container's supervisor (Docker /
        Electron) to bring us back on the new wheel. If the lifespan
        shutdown hangs — e.g. an asyncio task that ignores cancellation,
        a deadlock between background tasks, an OS-level issue — the
        supervisor can't recover us because we never exit. A daemon
        timer guarantees ``os._exit(0)`` runs within ``deadline``
        seconds of the first signal, no matter what. ``os._exit`` skips
        Python finalisers, which is intentional: a hung finaliser is
        exactly what we're guarding against.
        """

        # Slightly longer than _on_shutdown's 8s asyncio.wait_for plus
        # uvicorn's own timeout_graceful_shutdown=10, so the normal
        # clean-shutdown path is the typical winner.
        _deadline_s = 12.0

        def handle_exit(self, sig, frame):
            if _supervised:
                threading.Timer(
                    self._deadline_s,
                    lambda: os._exit(0),
                ).start()
            super().handle_exit(sig, frame)

    server = _BoundedShutdownServer(config)

    # ── Bundled SPA listener ───────────────────────────────────────────────
    #
    # The web UI ships inside the cremind wheel at ``app/static/ui/`` (the
    # CI step in ``scripts/build_ui.sh`` populates it). When that
    # directory exists, ``cremind serve`` opens a second uvicorn listener on
    # ``CREMIND_UI_PORT`` (default 1515) and serves the SPA. The browser
    # then sees:
    #
    #   http://<host>:1112    A2A protocol + REST API (existing)
    #   http://<host>:1515    SPA (only when bundled)
    #
    # ``CREMIND_UI_DIR`` overrides the on-disk location — used by the
    # Docker image, which builds the SPA in a separate stage and points
    # the env var at ``/opt/cremind-ui`` so the in-process serve picks
    # up that build instead of the (possibly stale) wheel-bundled copy.
    #
    # ``CREMIND_UI_PORT=0`` disables the listener entirely (e.g., when
    # running behind a sibling nginx that already serves the SPA).
    ui_server = _build_ui_server(host=host)
    if ui_server is None:
        await server.serve()
    else:
        # Run both listeners as tasks. ``asyncio.gather`` propagates the
        # first exception, which on a clean shutdown is just a
        # ``CancelledError`` from the API server's signal handler — the
        # UI server's task gets cancelled too, which is what we want.
        await asyncio.gather(server.serve(), ui_server.serve())


class _CachingStaticFiles(StaticFiles):
    """``StaticFiles`` with sensible Cache-Control for SPA bundles.

    Vite emits content-hashed assets under ``/assets/`` (e.g.
    ``assets/index-abc123.js``); the URL is effectively a content
    address, so we cache forever and skip revalidation. Everything
    else (``index.html``, top-level files, ``html=True`` fallbacks for
    deep links) is cacheable only with revalidation: the file content
    can change without the URL changing, and the renderer's post-
    upgrade reload must reliably pick up a fresh ``index.html``.

    Without this the renderer can serve a cached pre-upgrade
    ``index.html`` whose embedded asset-hash references point at the
    old bundle — exactly the "UI didn't update" symptom seen after
    test48 shipped.
    """

    def file_response(
        self,
        full_path,  # PathLike
        stat_result,
        scope,
        status_code: int = 200,
    ):
        response = super().file_response(full_path, stat_result, scope, status_code)
        # ``scope["path"]`` is the request path inside this mount (the
        # mount prefix has already been consumed by Starlette routing),
        # so ``/electron-renderer/assets/foo.js`` becomes ``/assets/foo.js``
        # here. The ``html=True`` fallback (deep-link routes that map to
        # index.html) hits the ``else`` branch — correct, because what
        # gets served is index.html and we want it revalidated.
        if "/assets/" in scope.get("path", ""):
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable"
            )
        else:
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


def _build_ui_server(*, host: str) -> uvicorn.Server | None:
    """Return a uvicorn server for the bundled SPA, or None if disabled.

    Skipped quietly when ``CREMIND_UI_PORT=0`` or when the SPA directory
    is missing (e.g., a dev install that hasn't run scripts/build_ui.sh).
    """
    raw_port = os.environ.get("CREMIND_UI_PORT", "1515")
    try:
        ui_port = int(raw_port)
    except ValueError:
        logger.warning(f"Invalid CREMIND_UI_PORT={raw_port!r}; SPA listener disabled.")
        return None
    if ui_port == 0:
        return None

    # Resolution order:
    #   1. Wheel-bundled SPA at app/static/ui/   ← ALWAYS preferred when
    #                                              present, because that
    #                                              copy is upgraded in
    #                                              lockstep with the
    #                                              running ``cremind serve``
    #                                              code (both come from
    #                                              the same wheel via pip).
    #   2. CREMIND_UI_DIR override                ← fallback when the wheel
    #                                              has no bundled SPA
    #                                              (typical dev install
    #                                              before scripts/build_ui.sh
    #                                              has run).
    #
    # Why this order: under Docker the image used to set
    # ``CREMIND_UI_DIR=/opt/cremind-ui`` pointing at a SPA copied in during
    # ``docker build``. That directory is on the image overlay, not a
    # volume — so the in-app upgrade (which pip-installs a new wheel
    # into the cremind-venv volume) could never refresh it. Result:
    # backend was upgraded but the SPA bytes served from port 1515 were
    # stuck on whatever version the image was built with. Preferring
    # the wheel-bundled SPA fixes this — the same pip install that
    # brings the new backend brings the new SPA, and the override
    # becomes harmless.
    from pathlib import Path

    wheel_ui_dir = Path(__file__).resolve().parent / "static" / "ui"
    override = os.environ.get("CREMIND_UI_DIR")

    if wheel_ui_dir.is_dir() and (wheel_ui_dir / "index.html").is_file():
        ui_dir = wheel_ui_dir
        if override:
            logger.warning(
                f"SPA: ignoring CREMIND_UI_DIR={override!r}; the wheel-bundled "
                f"SPA at {ui_dir} is the source of truth (it upgrades with "
                "the wheel). To use the override, rm -rf app/static/ui in "
                "your install first."
            )
        else:
            logger.info(f"SPA: serving wheel-bundled UI at {ui_dir}")
    elif override:
        ui_dir = Path(override)
        if not (ui_dir.is_dir() and (ui_dir / "index.html").is_file()):
            logger.info(
                f"SPA not present at CREMIND_UI_DIR={ui_dir}; UI listener disabled "
                "(run scripts/build_ui.sh or fix the CREMIND_UI_DIR path)."
            )
            return None
        logger.info(
            f"SPA: serving CREMIND_UI_DIR fallback at {ui_dir} "
            "(no wheel-bundled SPA found — dev install?)"
        )
    else:
        logger.info(
            f"SPA not present at {wheel_ui_dir}; UI listener disabled "
            "(run scripts/build_ui.sh or set CREMIND_UI_DIR=/path/to/built/ui)."
        )
        return None

    # ``html=True`` makes StaticFiles fall back to index.html for any
    # missing path — required for client-side routing under hash mode
    # to keep working when the user reloads on a deep link.
    #
    # The Electron-renderer bundle (built with __IS_ELECTRON__: true) is
    # mounted ahead of the web SPA so the Electron shell can request
    # /electron-renderer/ and get a bundle whose titlebar/drag region
    # renders correctly. Browsers continue to hit ``/`` and get the web
    # bundle as before.
    #
    # Resolution mirrors the web-bundle logic above: wheel-bundled wins,
    # CREMIND_UI_ELECTRON_DIR is a fallback for dev installs that don't
    # have a sibling ``-electron`` directory.
    wheel_electron_dir = wheel_ui_dir.with_name(wheel_ui_dir.name + "-electron")
    electron_override = os.environ.get("CREMIND_UI_ELECTRON_DIR")

    if wheel_electron_dir.is_dir() and (wheel_electron_dir / "index.html").is_file():
        ui_electron_dir: Path | None = wheel_electron_dir
        if electron_override:
            logger.warning(
                f"SPA: ignoring CREMIND_UI_ELECTRON_DIR={electron_override!r}; "
                f"the wheel-bundled Electron-renderer at {ui_electron_dir} is "
                "the source of truth."
            )
    elif electron_override and Path(electron_override).is_dir() and (
        Path(electron_override) / "index.html"
    ).is_file():
        ui_electron_dir = Path(electron_override)
        logger.info(
            f"SPA: Electron-renderer from CREMIND_UI_ELECTRON_DIR fallback at "
            f"{ui_electron_dir} (no wheel-bundled electron bundle)."
        )
    else:
        ui_electron_dir = None

    routes: list = []
    if ui_electron_dir is not None:
        routes.append(
            Mount(
                "/electron-renderer",
                app=_CachingStaticFiles(directory=str(ui_electron_dir), html=True),
                name="ui-electron",
            )
        )
        logger.info(f"SPA listener: /electron-renderer → {ui_electron_dir}")
    else:
        logger.info(
            "Electron-renderer bundle not present; "
            "/electron-renderer mount disabled (Electron shell will fall "
            "back to the asar copy)."
        )
    routes.append(Mount("/", app=_CachingStaticFiles(directory=str(ui_dir), html=True), name="ui"))
    spa_app = Starlette(routes=routes)
    cfg = uvicorn.Config(spa_app, host=host, port=ui_port, log_level="warning")
    logger.info(f"SPA listener: http://{host}:{ui_port} (serving {ui_dir})")
    return uvicorn.Server(cfg)
