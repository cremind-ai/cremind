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
import atexit
import os
import socket
import sys

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
from app.api.backup import get_backup_routes
from app.api.config import get_config_routes
from app.api.features import get_features_routes
from app.api.oauth_callback import get_oauth_callback_routes
from app.api.llm import get_llm_routes
from app.api.setup_stream import get_setup_stream_routes
from app.api.skills import get_skill_routes
from app.api.system import get_system_routes
from app.api.tls import get_tls_routes
from app.api.tools import get_tool_routes
from app.api.upgrade import get_upgrade_routes
from app.api.version import get_version_routes
from app.auth import verify_token
from app.config.bootstrap import bootstrap_exists
from app.config.settings import BaseConfig, set_dynamic_config_storage
from app.config.tls_mode import (
    MODE_AFTER_SETUP,
    MODE_AUTO,
    effective_ssl_mode,
    env_supervised,
    https_origin_from_app_url,
    record_boot_tls,
)
from app.runtime import BootedState, get_state
from app.constants import INTRODUCE_ASSISTANT
from app.documents import (
    DocumentSyncService,
    set_service as set_document_service,
)
from app.documents.sync import SHARED_SCOPE
from app.documents.watcher import DocumentWatcher
from app.lib.embedding import LocalEmbeddings
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
from app.tools.builtin import (
    BuiltInToolGroup,
    refresh_builtin_tool_oauth,
    register_builtin_tools,
)
from app.channels.sidecars.bootstrap import start_background_bootstrap
from app.tools.builtin.exec_shell import cleanup_stdout_on_startup
from app.tools.builtin.exec_shell_autostart import run_autostart_on_boot
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
    """JWT auth with signature + revocation verification (secret per request).

    The single chokepoint for every HTTP route: the per-module ``_require_auth``
    helpers scattered across ``app/api`` all just read the ``request.user`` this
    populates.
    """

    def __init__(self, secret_provider, algorithms=None):
        self.secret_provider = secret_provider
        self.algorithms = algorithms or ["HS256"]

    async def authenticate(self, conn: HTTPConnection):
        auth_header = conn.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return None
        secret = self.secret_provider()
        if not secret:
            # Setup mode — no secret exists yet, so everyone is anonymous.
            return None
        token = auth_header.split("Bearer ", 1)[1]
        payload = verify_token(token, secret=secret)
        if payload is None:
            logger.warning(f"[auth] rejected token on {conn.url.path}")
            return None
        return AuthCredentials(["authenticated"]), SimpleUser(payload.get("sub", "anonymous"))


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
            # Defence in depth: A2AAuthGuard has already 401'd an invalid or
            # revoked token by the time this runs. Re-checking here keeps the
            # profile we hand the agent from ever coming out of a dead token.
            payload = verify_token(token, secret=secret)
            if payload is not None:
                state["profile"] = payload.get("profile", "")
                state["sub"] = payload.get("sub", "")
            else:
                logger.warning("[auth] CallContextBuilder rejected the bearer token")
        return ServerCallContext(state=state)


# ── persisted-tool hydration ───────────────────────────────────────────────


_STUB_ERR_LAZY = "Not connected (disabled by all profiles)"


async def _connect_mcp_tool(
    registry: ToolRegistry,
    model_group_mgr: ModelGroupManager,
    row: dict,
):
    """Build a live MCP tool from a persisted row; fall back to a stub on failure.

    The MCP adapter needs a default LLM to satisfy its build contract (it drives
    only the cosmetic model label — MCP dispatch uses native function calling,
    not an inner routing LLM), so we build the single configured model.
    Honors the ``stdio`` vs ``http`` transport branch.
    """
    url = row["source"]
    owner = row["owner_profile"]
    extra = row.get("extra") or {}

    llm = None
    try:
        llm = model_group_mgr.create_llm_for_model(profile=owner or "admin")
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
                )
            else:
                tool = await build_http_mcp_tool(
                    url=url,
                    llm=llm,
                    owner_profile=owner,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"MCP tool '{url}' unreachable: {e}")
    if tool is None:
        tool = build_mcp_stub(
            url=url,
            name=row["name"],
            owner_profile=owner,
            error="LLM not available" if llm is None else "Connection failed",
            extra=extra,
        )
    return tool


def _lazy_mcp_stub_from_row(row: dict):
    extra = row.get("extra") or {}
    return build_mcp_stub(
        url=row["source"],
        name=row["name"],
        owner_profile=row["owner_profile"],
        error=_STUB_ERR_LAZY,
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
        if tool_type != ToolType.MCP.value:
            continue
        tool_id = row["tool_id"]
        if tool_id in enabled_ids:
            tool = await _connect_mcp_tool(registry, model_group_mgr, row)
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
            if tool.tool_type is not ToolType.MCP:
                return False, f"Tool '{tool_id}' is not an MCP tool"
            if not getattr(tool, "is_stub", False):
                return True, None  # already connected

            row = registry.storage.get_tool(tool_id)
            if row is None:
                return False, f"Tool '{tool_id}' not persisted"

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
        from app.events import get_uploads_cleanup_manager

        get_uploads_cleanup_manager().stop()
    except Exception:  # noqa: BLE001
        logger.exception("Error stopping uploads cleanup manager during shutdown")
    try:
        from app.events import get_event_manager

        get_event_manager().stop()
    except Exception:  # noqa: BLE001
        logger.exception("Error stopping skill event manager during shutdown")
    try:
        from app.events.task_timeout_manager import get_task_timeout_manager

        get_task_timeout_manager().stop()
    except Exception:  # noqa: BLE001
        logger.exception("Error stopping task timeout manager during shutdown")
    try:
        stop_all_watchers()
    except Exception:  # noqa: BLE001
        logger.exception("Error stopping skill watchers during shutdown")
    try:
        from app.channels import get_channel_registry

        await get_channel_registry().stop_all()
    except Exception:  # noqa: BLE001
        logger.exception("Error stopping channel adapters during shutdown")
    try:
        from app.tools.builtin.exec_shell import stop_all_managed_processes

        # Sub-bounded on purpose: a wedged taskkill/killpg must not eat the
        # whole SHUTDOWN_TIMEOUT_S budget and stall a container restart. Skipping
        # a kill only costs us an orphan — the thing we already tolerate today.
        await asyncio.wait_for(stop_all_managed_processes(), timeout=3.0)
    except asyncio.TimeoutError:
        logger.warning("Timed out stopping managed processes during shutdown")
    except Exception:  # noqa: BLE001
        logger.exception("Error stopping managed processes during shutdown")
    try:
        from app.api.terminals import close_all_terminals

        await close_all_terminals()
    except Exception:  # noqa: BLE001
        logger.exception("Error closing user terminals during shutdown")


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


def _supervised_env() -> bool:
    """Is something out there that will restart us if we exit?

    Matters for shutdown, not for booting: where a supervisor exists, a hung
    lifespan shutdown is worse than an abrupt one, because it stops the
    supervisor from bringing us back at all (see ``_BoundedShutdownServer``).

    Docker (compose sets ``restart: unless-stopped``), Kubernetes (the kubelet
    restarts the pod; its container exits when ``cremind serve`` does), Electron
    (which respawns us over IPC) and the boot service ``cremind boot enable``
    registers on a native install (systemd/launchd/Scheduled Task, which sets
    ``CREMIND_SUPERVISED``) all qualify. ``CREMIND_SSL=after-setup`` makes this
    load-bearing rather than incidental: the Setup Wizard now asks for a restart
    deliberately, so a wedged shutdown would strand the user mid-setup. A bare
    ``cremind serve`` in a terminal is NOT supervised — it keeps clean-shutdown
    semantics so Ctrl-C doesn't truncate work, and the wizard asks the operator
    to restart by hand instead.
    """
    return (
        os.environ.get("INSTALL_MODE") in ("docker", "kubernetes")
        or os.environ.get("CREMIND_ELECTRON_PARENT") is not None
        or env_supervised()
    )


def _write_server_pid_if_supervised() -> None:
    """Record our PID in ``<system dir>/server.pid`` when under a boot service.

    Only when supervised, and deliberately not ``install.pid``: the desktop app
    tree-kills whatever PID sits in *that* file when it quits, and a
    service-run server must not be collateral. A hand-run ``cremind serve``
    writes nothing, so ``cremind boot disable`` can never mistake a developer's
    terminal server for the one it manages.

    Called after the port pre-flight so a losing duplicate never overwrites the
    PID of the process that actually holds the ports.
    """
    if not env_supervised():
        return
    pid_path = Path(BaseConfig.CREMIND_SYSTEM_DIR) / "server.pid"
    try:
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()), encoding="ascii")
    except OSError as e:
        logger.warning(f"Could not write {pid_path}: {e}")
        return

    def _remove_pid_file() -> None:
        try:
            pid_path.unlink()
        except OSError:
            pass

    atexit.register(_remove_pid_file)


def _resolve_public_port() -> int:
    """The public origin's port: ``CREMIND_UI_PORT``, or 1515.

    ``0`` is a real value, not an error — it means "open no public bind at
    all", which is how the dev loop frees :1515 for Vite and how a deployment
    puts an external proxy in front of the loopback app.
    """
    raw = os.environ.get("CREMIND_UI_PORT", "1515")
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"Invalid CREMIND_UI_PORT={raw!r}; defaulting to 1515.")
        return 1515


def _resolve_tls(
    ssl_certfile: str | None,
    ssl_keyfile: str | None,
    public_port: int,
) -> tuple[str, str] | None:
    """The public bind's ``(certfile, keyfile)``, or ``None`` to serve plain HTTP.

    Validated here rather than in ``BaseConfig`` because settings.py is imported
    by every CLI command, where a stale cert path must not be fatal. Checked
    alongside the port pre-flight for the same reason that check exists: a
    misconfiguration should stop the process before boot has any side effects.
    """
    certfile = (ssl_certfile or BaseConfig.SSL_CERTFILE or "").strip()
    keyfile = (ssl_keyfile or BaseConfig.SSL_KEYFILE or "").strip()
    mode = effective_ssl_mode()
    auto = mode == MODE_AUTO
    after_setup = mode == MODE_AFTER_SETUP
    # after-setup defers TLS until the Setup Wizard has written bootstrap.toml,
    # so the wizard itself runs over plain HTTP and can hand the user the CA to
    # trust before any HTTPS page loads. Past that point it is exactly auto.
    generated = auto or (after_setup and bootstrap_exists())

    if not certfile and not keyfile and not (auto or after_setup):
        if mode:
            # An unrecognised value is a typo in something the operator
            # deliberately set. Silently serving plain HTTP is the worst
            # reading of that; failing the boot over a stale exported variable
            # is too harsh, and warn-don't-fail is what the other "TLS was
            # asked for but won't happen" paths below already do.
            logger.warning(
                f"Ignoring unknown CREMIND_SSL={BaseConfig.SSL_MODE!r} — serving "
                'plain HTTP. Valid values are "" (plain HTTP), "auto" (HTTPS '
                'with a generated local CA), and "after-setup" (plain HTTP '
                "until the Setup Wizard completes, then HTTPS)."
            )
        return None

    # An external proxy owns the origin in these two modes, so it — not this
    # process — is what a browser connects to and what should hold the
    # certificate. Warn rather than fail: the env may be set globally.
    if public_port == 0:
        logger.warning(
            "TLS is configured but CREMIND_UI_PORT=0, so no public bind exists — "
            "serving loopback-only over plain HTTP. Terminate TLS at whatever "
            "proxy fronts this process."
        )
        return None
    if os.environ.get("CREMIND_ELECTRON_PARENT") is not None:
        logger.warning(
            "TLS is configured but this process was started by the Electron app, "
            "which loads the UI over http://127.0.0.1 — ignoring TLS. It applies "
            "to server deployments."
        )
        return None

    if after_setup and not generated and not certfile and not keyfile:
        # The wizard phase. Generate the CA now anyway — eagerly, before
        # serving anything — so /ca.pem and its fingerprint are live while the
        # wizard walks the user through trusting it. Then serve plain HTTP:
        # the certificate is worth nothing until that trust step has happened,
        # and a warning on the very first page is what this mode exists to
        # avoid.
        from app.config.tls_auto import ensure_local_tls, tls_dir

        ensure_local_tls(BaseConfig.CREMIND_SYSTEM_DIR, BaseConfig.SSL_AUTO_HOSTS)
        next_origin = https_origin_from_app_url(BaseConfig.APP_URL)
        logger.info(
            "CREMIND_SSL=after-setup and the Setup Wizard has not completed — "
            "serving plain HTTP so the wizard can run without a certificate "
            "warning. The wizard hands you the local CA to trust, then restarts "
            f"this server into HTTPS{f' at {next_origin}' if next_origin else ''}. "
            "CA file: "
            f"{os.path.join(tls_dir(BaseConfig.CREMIND_SYSTEM_DIR), 'ca.pem')}"
        )
        return None

    if generated and not certfile and not keyfile:
        from app.config.tls_auto import ensure_local_tls, tls_dir

        certfile, keyfile = ensure_local_tls(
            BaseConfig.CREMIND_SYSTEM_DIR, BaseConfig.SSL_AUTO_HOSTS
        )
        logger.info(
            f"CREMIND_SSL={mode} — serving a locally-signed certificate. Browsers "
            "warn until the CA is trusted, once per device: on this machine run "
            "`cremind tls trust`; from another device download "
            f"https://<this-host>:{public_port}/ca.pem and run "
            "`cremind tls trust --file <download>` (or `cremind tls trust "
            "--print-only` for the command to run by hand). CA file: "
            f"{os.path.join(tls_dir(BaseConfig.CREMIND_SYSTEM_DIR), 'ca.pem')}"
        )
    elif not certfile or not keyfile:
        missing, given = (
            ("CREMIND_SSL_CERTFILE", "CREMIND_SSL_KEYFILE")
            if not certfile
            else ("CREMIND_SSL_KEYFILE", "CREMIND_SSL_CERTFILE")
        )
        logger.error(
            f"Cannot start: {given} is set but {missing} is not — TLS needs both. "
            "Set it, or unset both to serve plain HTTP (or use CREMIND_SSL=auto)."
        )
        raise SystemExit(1)

    # Paths are interpolated plainly, not with !r: repr doubles every backslash,
    # which makes a Windows path in an error message hard to read back.
    for label, path in (("certificate", certfile), ("private key", keyfile)):
        if not os.path.isfile(path):
            logger.error(
                f"Cannot start: the TLS {label} does not exist at {path}. "
                "Fix the path, or unset CREMIND_SSL_CERTFILE/CREMIND_SSL_KEYFILE "
                "to serve plain HTTP."
            )
            raise SystemExit(1)
        if not os.access(path, os.R_OK):
            logger.error(f"Cannot start: the TLS {label} at {path} is not readable.")
            raise SystemExit(1)

    # APP_URL is what the agent card advertises and what OAuth redirects derive
    # from, so an http:// value here sends browsers to a port that now speaks TLS.
    if BaseConfig.APP_URL.startswith("http://"):
        logger.warning(
            f"TLS is on but APP_URL is {BaseConfig.APP_URL!r} — the agent card and "
            "OAuth redirects will point at http://. Change it to https:// (and "
            "CORS_ALLOWED_ORIGINS with it, if that is set to explicit origins)."
        )

    return certfile, keyfile


def _mk_hypercorn_config(host: str, port: int, certfile: str, keyfile: str):
    """Hypercorn config for the TLS public bind.

    Split out from the serve section so it can be asserted on without binding
    a socket.
    """
    from hypercorn.config import Config as HypercornConfig

    cfg = HypercornConfig()
    cfg.bind = [f"{host}:{port}"]
    cfg.certfile = certfile
    cfg.keyfile = keyfile
    if BaseConfig.SSL_KEYFILE_PASSWORD:
        cfg.keyfile_password = BaseConfig.SSL_KEYFILE_PASSWORD
    # The whole point of the TLS path: browsers negotiate HTTP/2 through ALPN
    # and stop being limited to ~6 connections per origin. http/1.1 stays in
    # the list so non-h2 clients (and the WebSocket upgrade) still work.
    cfg.alpn_protocols = ["h2", "http/1.1"]
    # Parity with the uvicorn binds' timeout_graceful_shutdown=10.
    cfg.graceful_timeout = 10.0
    cfg.accesslog = None
    return cfg


def _port_taken(bind_host: str, bind_port: int) -> bool:
    """Whether something already holds this address.

    The probe's whole job is to predict the bind ``main()`` is about to
    attempt, so it resolves the address family the way uvicorn and hypercorn
    both do — a colon in the host means IPv6 — rather than assuming IPv4.
    Assuming it meant that ``HOST=::`` failed the probe with a ``gaierror``
    that ``OSError`` quietly caught, and every dual-stack boot died claiming
    the port was in use.

    ``SO_REUSEADDR`` is set on Linux only, which needs the three-way story
    behind a two-way-looking flag:

    - **Linux** — it excuses sockets already on their way out (TIME_WAIT, and
      the FIN_WAIT_2 a vanished peer can pin for a minute), and only when the
      departing socket carried the flag too, which a previous Cremind's always
      did. A live ``listen()`` keeps the address regardless. Since both real
      servers set it, a strict probe here is *more* conservative than the bind
      it is predicting: it reports taken a port the server would have got.
    - **macOS/BSD** — the flag also lets a live listener on a *different*
      local address share the port, so setting it would blind the check to a
      second Cremind bound to 127.0.0.1 while this one binds 0.0.0.0.
    - **Windows** — worse still: it lets a second live listener bind over the
      first outright. Note this is the one place the probe deliberately stops
      predicting the real bind: uvicorn and hypercorn set the flag there too,
      so they *would* take a port out from under a running Cremind. Refusing
      instead, with a message naming the conflict, beats two servers fighting
      over one port and whichever the OS happens to favour.

    Linux is also where it is most needed. A pod's network namespace outlives
    its container, so a restarting server always comes back to find its
    predecessor's connections still closing on the same port; left strict, the
    probe refused to boot on every one of those restarts until the remnants
    aged out a minute later.

    macOS and Windows stay strict, and a native install with a boot service is
    now a restarting server there too (``cremind boot``). Their supervisors are
    paced for it rather than relaxing this probe: launchd throttles a respawn
    to ~5s and the Windows loop waits 2s, both long enough for a closing
    listener to clear, and a genuine clash still gets one clear line instead of
    two servers quietly fighting over a port.
    """
    family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        if sys.platform.startswith("linux"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((bind_host, bind_port))
        except OSError:
            return True
    return False


def _require_free_ports(host: str, public_port: int, loopback_port: int) -> None:
    """Stop with one clear line if a port this server needs is already taken.

    Worth doing up front because both servers bind their socket only AFTER the
    ASGI lifespan has run — uvicorn, and hypercorn on the TLS path, which
    creates its sockets after ``wait_for_startup``. Left to them, a clash
    starts the whole installation — watchers, every channel adapter, the Node
    sidecars — then tears it down again, and the one line that matters ends up
    buried under the teardown's own warnings and a ``SystemExit`` raised
    inside a gathered task.

    A racing bind in the gap between this check and the server's own is
    possible and harmless: that path simply fails the way it does today.
    """
    checks = [("127.0.0.1", loopback_port, False)]
    if public_port != 0:
        checks.insert(0, (host, public_port, True))
    for bind_host, bind_port, is_public in checks:
        if not _port_taken(bind_host, bind_port):
            continue
        if is_public:
            what = "the public origin (CREMIND_UI_PORT)"
            remedy = (
                "Another Cremind is already serving, or a dev server has it — "
                "the Vite HMR server uses this port too, and the backend must "
                "then run with CREMIND_UI_PORT=0 so it binds only the internal "
                "API (see CONTRIBUTING.md). Stop the other process, or choose "
                "another port."
            )
        else:
            what = "the internal API port (PORT)"
            remedy = (
                "Another Cremind is already running — its internal API holds "
                "this port. Stop it, or set PORT to a free one."
            )
        logger.error(
            f"Cannot start: {bind_host}:{bind_port} is already in use — that "
            f"is {what}. {remedy}"
        )
        raise SystemExit(1)


async def main(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
):
    _silence_proactor_connection_reset(asyncio.get_running_loop())

    # 0. Can we have the ports at all? Asked FIRST, before anything is started.
    #
    #    uvicorn binds its socket only after the ASGI lifespan has run, so
    #    without this a port clash boots the entire installation — watchers,
    #    every channel adapter, the Node sidecars — and then tears it all down
    #    again, with the one line that matters buried under the teardown's own
    #    warnings and a SystemExit raised inside a gathered task. Nothing here
    #    has side effects, so failing at this point leaves no process, no
    #    sidecar and no half-written state behind.
    public_port = _resolve_public_port()
    _require_free_ports(host, public_port, port)
    #    Now that the ports are ours, tell the boot service (and the
    #    uninstallers, which have always stopped this PID) which process to
    #    manage. No-op unless something is actually supervising us.
    _write_server_pid_if_supervised()

    #    Resolved next, for the same reason: a bad cert path (or CREMIND_SSL=auto
    #    with an unwritable system dir) should stop us here, not after the whole
    #    installation has started. Generating the auto certificate at this point
    #    also keeps that cost off the request path.
    tls = _resolve_tls(ssl_certfile, ssl_keyfile, public_port)
    # What the Setup Wizard is told about TLS has to be what we actually did,
    # not a recomputation — between the wizard writing bootstrap.toml and the
    # restart landing, recomputing would claim HTTPS while we serve plain HTTP.
    record_boot_tls(tls is not None)

    # 0''. Purge stale exec_shell stdout directories from previous runs.
    cleanup_stdout_on_startup()

    # 0'. Wipe temporary chat-upload folders left behind by the previous run.
    #     They are ephemeral by design (a file the user asked to keep has
    #     already been moved into their working dir). Best-effort — a locked
    #     file must never block boot.
    try:
        from app.utils.uploads_tmp import wipe_all_on_startup

        wipe_all_on_startup()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[boot] uploads_tmp wipe best-effort failed: {e}")

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

    # 0b. Warm channel sidecars' node_modules. Deliberately off the critical
    #     path: a cold `npm ci` is ~66MB and would delay the bind past the
    #     window the installers wait for /health. Adapters call
    #     ensure_sidecar_ready() when a channel is enabled, so a half-installed
    #     tree is still never handed to one.
    start_background_bootstrap()

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
    # Backup & Restore registered pre-storage: a restore must work in setup
    # mode (fresh install, storage not yet booted) and its status/stream
    # endpoints must answer immediately after the restore-triggered restart,
    # before storage comes up. Handlers resolve ``state`` lazily at request time.
    routes.extend(get_backup_routes(state))
    routes.extend(get_system_routes())
    # The local-CA download. Pre-storage and unauthenticated because the
    # certificate warning it fixes appears before login — often on the very
    # first visit to the Setup Wizard — and the browser showing that warning
    # is precisely the client with no token to present.
    routes.extend(get_tls_routes())
    routes.extend(get_features_routes())
    routes.extend(get_config_routes(state))
    routes.extend(get_llm_routes(state))
    routes.extend(get_tool_routes(state))
    routes.extend(get_skill_routes(state))
    routes.extend(get_setup_stream_routes())
    # OAuth callback routes (Google/Atlassian skills + A2A tool auth). Registered
    # PRE-storage because they only write a per-state inbox file / resolve an
    # in-process Future (no DB/agent needed) — and account-linking is driven over
    # the A2A endpoint, itself available pre-storage, so the consent redirect can
    # arrive before/independently of the post-storage boot. Keeping these routes
    # pre-storage guarantees they answer whenever a link is in flight.
    routes.extend(get_oauth_callback_routes())

    from app.middleware import ConnectionHeaderFilter

    middleware_stack = [
        # Outermost, so it also covers the SPA fallback, the mounted A2A app,
        # and the routes appended to the live app after storage boots.
        Middleware(ConnectionHeaderFilter),
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

    # ── Single public origin: serve the SPA from THIS app ──────────────────
    # The web UI, API, A2A, OAuth callbacks, SSE, and the PTY WebSocket are all
    # served by one app on the single public port (see the serve section). The
    # SPA is installed as the router *default* — the catch-all consulted only
    # after every real route misses, including the post-storage routes appended
    # to the live app at boot — so it never shadows /api or the A2A root.
    # /electron-renderer is a prefix mount (matched before the default).
    _spa_web_static, _spa_electron_mount = _build_spa_components()
    if _spa_electron_mount is not None:
        routes.append(_spa_electron_mount)
    if _spa_web_static is not None:
        # Explicit GET / → SPA index. The A2A root route is POST-only, so a bare
        # GET / would otherwise 405 (Starlette partial-matches the POST route and
        # never reaches the router default). A full-method match here wins over
        # that partial match; POST / still routes to A2A. HEAD covers probes.
        async def _serve_spa_index(request: Request):
            return await _spa_web_static.get_response("index.html", request.scope)

        routes.append(Route("/", _serve_spa_index, methods=["GET", "HEAD"]))

    app = Starlette(
        routes=routes,
        middleware=middleware_stack,
        on_shutdown=[_on_shutdown],
    )
    if _spa_web_static is not None:
        # Everything not matched by a route (hashed /assets/*, favicon, etc.)
        # falls here. With hash routing the only SPA path the server sees is `/`
        # (handled above); this covers static files and any stray deep link.
        app.router.default = _SpaFallback(_spa_web_static)

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

            # 3. Embedding model + vector store (only when enabled).
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
                """Create the child LLM for a built-in tool's internal LLM step.

                The single configured model, except ``image_understanding`` /
                ``audio_understanding`` which resolve the optional vision / audio
                models respectively (``create_llm_for_tool``). ``tool_id`` may be
                a module name (boot) or a slug (runtime refresh) — both contain
                the tool's substring.
                """
                return model_group_mgr.create_llm_for_tool(tool_id, profile=profile)

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

            # 6a. Clean slate: wipe any stale skill event files left over from a
            # previous run *before* listeners spawn (7c) and the watch arms
            # (7d), so operation begins with no junk. The blanket per-profile
            # watch keeps the folders clean from then on.
            try:
                from app.events import wipe_event_folders_on_startup

                wipe_event_folders_on_startup(known_profiles)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to wipe skill event folders on startup")

            # 6a-bis. Event-run boot recovery: any run left 'running' by a crash
            # can never resume, so mark it failed (interrupted). 'pending' runs
            # survive untouched — their reply path is fully DB-backed. Runs before
            # the managers start (7d–7f) so no fresh run races the sweep.
            try:
                from app.storage import get_event_run_storage

                await get_event_run_storage().recover_after_restart()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to run event-run boot recovery")

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

                bundled_docs = Path(__file__).resolve().parent / "documents" / "bundled"
                document_service.seed_shared_from_app(bundled_docs)

                document_service.full_reconcile(SHARED_SCOPE)
                DocumentWatcher(
                    scope=SHARED_SCOPE,
                    directory=document_service.shared_dir(),
                    sync_service=document_service,
                ).start()

                # One-shot cleanup for upgraded installs: the retired `cli`
                # built-in tool once indexed CLI-reference docs into a separate
                # `cli` scope. They now live in the shared corpus above, so drop
                # any leftover `cli`-scope points (the on-disk tree is removed by
                # seed_shared_from_app).
                document_service.prune_scope("cli")

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

            # 7. Single configured model (admin) + CremindAgent
            runner = None
            if config_storage.is_setup_complete():
                try:
                    runner = model_group_mgr.create_llm_for_model(profile="admin")
                except (ValueError, ImportError) as e:
                    # ValueError: the provider is misconfigured or its
                    # credentials are missing. ImportError (incl.
                    # ModuleNotFoundError): the provider's optional SDK isn't
                    # installed — e.g. the `openai` extra that backs
                    # github-copilot and every OpenAI-compatible provider.
                    # Either way, degrade gracefully: leave runner=None so the
                    # server still boots and the user can fix the model-group
                    # config from the UI, rather than hard-crashing the whole
                    # process the way an uncaught ModuleNotFoundError would.
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

            # 7c. Autostart long-running processes
            try:
                asyncio.create_task(run_autostart_on_boot(get_autostart_storage()))
            except Exception:  # noqa: BLE001
                logger.exception("Failed to schedule autostart run on boot")

            # 7c-bis. Sweep abandoned blueprint-import staging sessions (24h TTL).
            try:
                from app.blueprint.store import sweep_stale_sessions

                await asyncio.to_thread(sweep_stale_sessions)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to sweep stale blueprint import sessions")

            # 7d. Skill event manager
            try:
                from app.events import get_event_manager
                from app.events import runner as event_runner

                event_runner.set_globals(
                    cremind_agent=cremind_agent,
                    conversation_storage=conversation_storage,
                )
                event_manager = get_event_manager()
                event_manager.start(loop)
                # Arm one recursive watch per profile over its skills tree so
                # every event-listener skill's events/ folder is always
                # monitored (junk deleted, subscribed events fanned out).
                for profile_name in known_profiles:
                    try:
                        event_manager.watch_profile(profile_name, registry)
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            f"Failed to arm event watch for profile '{profile_name}'"
                        )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to start skill event manager")

            # 7e. File watcher manager
            try:
                from app.events import get_file_watcher_manager

                get_file_watcher_manager().start(loop)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to start file watcher manager")

            # 7f. Schedule (time-based) event manager — the Calendar & Schedule
            # trigger engine. Reuses the skill-event runner globals set in 7d.
            try:
                from app.events import get_schedule_manager

                get_schedule_manager().start(loop)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to start schedule manager")

            # 7f-bis. Event tasks. First hand back any one-shot result a crash
            # stranded (boot recovery in 6b has already flipped interrupted runs
            # to 'failed', which is what makes them deliverable), then start the
            # deadline sweep so a task whose event never fires still reports
            # back instead of hanging its conversation forever.
            try:
                from app.events.event_task_delivery import sweep_undelivered
                from app.events.task_timeout_manager import get_task_timeout_manager

                await sweep_undelivered()
                get_task_timeout_manager().start(loop)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to start event task delivery/timeout")

            # 7f-ter. Mid-turn user messages. Their routing state lives in
            # memory, so a crash leaves the rows parked at 'pending' — a state
            # history hides on purpose. Release them (making them visible again)
            # and answer them, so a message sent moments before a restart is not
            # silently lost.
            try:
                from app.events.user_message_delivery import (
                    sweep_stranded_mid_turn_messages,
                )
                await sweep_stranded_mid_turn_messages()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to sweep stranded mid-turn messages")

            # 7f-quater. Group chats: load the membership index, make sure
            # every member has its seat, and finish whatever a crash left
            # half-done. Before anything can post, so the tool gate and the
            # co-membership check do not read an empty room.
            try:
                from app.groups import boot as groups_boot

                await groups_boot.initialize()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to initialize group chats")

            # 7g. Temporary chat-upload pruner — periodically removes idle
            # per-conversation upload folders so the temp tree never grows
            # unbounded during a long-lived process.
            try:
                from app.events import get_uploads_cleanup_manager

                get_uploads_cleanup_manager().start(loop)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to start uploads cleanup manager")

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

                logger.info("Post-setup: built-in tools rebound and skills synced")

            def _mcp_llm_factory(profile: str = "admin"):
                """Build the default LLM for an MCP tool's adapter.

                MCP dispatch uses native function calling (no inner routing
                LLM); this only supplies the adapter's cosmetic model label.
                """
                try:
                    return model_group_mgr.create_llm_for_model(profile=profile)
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

    # 0a''. Apply a pending restore staged by a previous run's detached runner
    #       (Phase 2). Runs BEFORE storage boots so it can drop+reimport the DB
    #       and swap file trees with no open handles. Also sweeps a stale
    #       terminal restore-status file. Never raises — a failed restore rolls
    #       back from its safety backup and boot continues.
    try:
        from app.backup.pending import apply_pending_restore_if_any

        await asyncio.to_thread(apply_pending_restore_if_any)
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[boot] pending-restore apply hook failed unexpectedly: {e}")

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
    import os
    import threading

    _supervised = _supervised_env()

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

    # ── Single public origin + internal loopback ──────────────────────────
    #
    # One app, two binds. The PUBLIC port (CREMIND_UI_PORT, default 1515) on
    # ``host`` is the single origin the browser uses — UI + API + A2A + OAuth +
    # SSE + the PTY WebSocket, all same-origin. The INTERNAL port (PORT, default
    # 1112) binds 127.0.0.1 ONLY (never published) for the local ``cremind`` CLI
    # (CREMIND_SERVER). ``CREMIND_UI_PORT=0`` opens no public bind — an external
    # proxy fronts the loopback app instead.
    #
    # The public server owns the ASGI lifespan (so ``_on_shutdown`` runs once);
    # the loopback server is started with ``lifespan="off"`` to avoid a second
    # shutdown of the same app.
    # ``public_port`` was resolved at the top of main(), with the port check.

    def _mk_config(bind_host: str, bind_port: int, *, lifespan: str) -> uvicorn.Config:
        return uvicorn.Config(
            app, host=bind_host, port=bind_port,
            timeout_graceful_shutdown=10, lifespan=lifespan,
        )

    if tls is not None:
        # ── TLS: hypercorn serves the public bind, uvicorn the loopback ──
        #
        # uvicorn speaks only HTTP/1.1, and HTTP/2 is the reason this path
        # exists (see _mk_hypercorn_config), so the public bind moves to
        # hypercorn whenever TLS is on. The loopback bind stays on uvicorn and
        # stays plain HTTP, which is what keeps the CLI, the skills'
        # CREMIND_SERVER, and the sidecars working untouched.
        #
        # Hypercorn always runs the ASGI lifespan — it has no "off" switch — so
        # here the PUBLIC server owns it and the loopback keeps lifespan="off".
        # Same invariant as the plain-HTTP path: _on_shutdown runs exactly once.
        from hypercorn.asyncio import serve as hypercorn_serve

        certfile, keyfile = tls
        hypercorn_config = _mk_hypercorn_config(host, public_port, certfile, keyfile)

        loop = asyncio.get_running_loop()
        shutdown_event = asyncio.Event()

        class _SignalFanoutServer(_BoundedShutdownServer):
            """Relays uvicorn's signal handling to hypercorn.

            Hypercorn installs no signal handlers of its own once given an
            explicit ``shutdown_trigger`` (hypercorn/asyncio/run.py), so the
            loopback uvicorn server is the single registrant and its
            ``handle_exit`` is the one entry point for SIGINT/SIGTERM/SIGBREAK
            on both platforms. It starts the supervised hard-exit timer
            (inherited), stops itself, and releases hypercorn's trigger.

            ``call_soon_threadsafe`` because handle_exit runs in a signal
            context, which must not touch the loop directly.
            """

            def handle_exit(self, sig, frame):
                loop.call_soon_threadsafe(shutdown_event.set)
                super().handle_exit(sig, frame)

        internal = _SignalFanoutServer(_mk_config("127.0.0.1", port, lifespan="off"))
        logger.info(
            f"Serving on public https://{host}:{public_port} (HTTP/2 + HTTP/1.1 via ALPN) "
            f"(+ internal loopback http://127.0.0.1:{port})"
        )
        await asyncio.gather(
            hypercorn_serve(app, hypercorn_config, shutdown_trigger=shutdown_event.wait),
            internal.serve(),
        )
        return

    servers: list[uvicorn.Server] = []
    if public_port != 0:
        servers.append(_BoundedShutdownServer(_mk_config(host, public_port, lifespan="auto")))
        servers.append(_BoundedShutdownServer(_mk_config("127.0.0.1", port, lifespan="off")))
        logger.info(
            f"Serving on public http://{host}:{public_port} "
            f"(+ internal loopback http://127.0.0.1:{port})"
        )
    else:
        servers.append(_BoundedShutdownServer(_mk_config("127.0.0.1", port, lifespan="auto")))
        logger.info(f"CREMIND_UI_PORT=0 — serving loopback-only on http://127.0.0.1:{port}")

    if len(servers) == 1:
        await servers[0].serve()
    else:
        # asyncio.gather propagates the first exception; on a clean shutdown
        # that's a CancelledError from a signal handler and the sibling
        # server's task is cancelled too, which is what we want.
        await asyncio.gather(*(s.serve() for s in servers))


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


class _SpaFallback:
    """Router ``default``: serve the SPA for unmatched HTTP requests (the
    deep-link / ``index.html`` catch-all) and close any unmatched WebSocket,
    without tripping ``StaticFiles``' http-only assertion. Installed only when a
    built SPA is present, so it runs after every real route — including the
    post-storage routes appended at boot — has missed."""

    def __init__(self, static: "_CachingStaticFiles"):
        self._static = static

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            await self._static(scope, receive, send)
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1000})


def _build_spa_components():
    """Resolve the bundled SPA into components to attach to the merged app.

    Returns ``(web_static, electron_mount)``:
    - ``web_static`` — a ``_CachingStaticFiles`` to install as the app's router
      ``default`` (the catch-all SPA / deep-link ``index.html`` fallback), or
      ``None`` when no built SPA is present (e.g. a dev install that hasn't run
      scripts/build_ui.sh — the API still serves; Vite serves the SPA separately).
    - ``electron_mount`` — a ``Mount`` for ``/electron-renderer`` (the Electron
      shell bundle), or ``None`` when absent.

    The whole app (API + A2A + OAuth + this SPA) is served on the single public
    port, so the browser is same-origin and there is no separate SPA listener;
    see ``main()`` for the public/loopback bind split.
    """
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
                f"SPA not present at CREMIND_UI_DIR={ui_dir}; SPA serving disabled "
                "(run scripts/build_ui.sh or fix the CREMIND_UI_DIR path)."
            )
            return None, None
        logger.info(
            f"SPA: serving CREMIND_UI_DIR fallback at {ui_dir} "
            "(no wheel-bundled SPA found — dev install?)"
        )
    else:
        logger.info(
            f"SPA not present at {wheel_ui_dir}; SPA serving disabled "
            "(run scripts/build_ui.sh or set CREMIND_UI_DIR=/path/to/built/ui)."
        )
        return None, None

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

    electron_mount = None
    if ui_electron_dir is not None:
        electron_mount = Mount(
            "/electron-renderer",
            app=_CachingStaticFiles(directory=str(ui_electron_dir), html=True),
            name="ui-electron",
        )
        logger.info(f"SPA: /electron-renderer → {ui_electron_dir}")
    else:
        logger.info(
            "Electron-renderer bundle not present; /electron-renderer disabled "
            "(Electron shell falls back to the asar copy)."
        )
    web_static = _CachingStaticFiles(directory=str(ui_dir), html=True)
    logger.info(f"SPA: serving {ui_dir} as the single-origin catch-all (router default)")
    return web_static, electron_mount
