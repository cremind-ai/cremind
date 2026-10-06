"""The backend's hardware host: its thread, its lifecycle, and how the server talks to it.

One per Cremind installation: the process that starts hosting takes
``<SYS>/.tag-runtime/host.lock``; another process on the same system folder (a
second ``cremind serve``) reports ``busy_elsewhere`` instead of driving the
same gateways.

The runtime gets a thread and an event loop of its own (``tags-hardware``):
serial reads, text layout and the workers' SQLite never delay the server, and
a worker that misbehaves stays there. The server's loop is remembered: the
in-process connector (:mod:`.local_connector`) runs each worker request on
it, where Cremind's storage lives.

The same host runs **remote** on a desktop gateway computer of a Cremind
server elsewhere (``cremind tags host run``, supervised by the Cremind app):
``remote`` is that computer's enrollment
(:mod:`app.tags.runtime.host.enroll`), the host agent reaches Cremind over
HTTPS with its host credential (:mod:`app.tags.runtime.host.http_client`),
and each worker uses its own connector credentials over HTTPS, as a Cremind
Connect worker did. Nothing there needs Cremind's database.

:func:`start_hosting` (server boot, after storage, migrations and profiles)
and :func:`stop_hosting` (shutdown, an in-app upgrade, a backup restore) are
idempotent and never raise. Hosting runs only when the gateway components
are ready (:mod:`.components`) and hardware setup is on for this server
(``tags_simple_setup``; ``tags_server_hardware`` = ``off`` or
``CREMIND_TAGS_SERVER_HARDWARE=0`` keeps this server's USB out of it).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeVar

from app.utils.logger import logger

from .paths import RuntimePaths, default_paths, host_identity
from .supervisor import Supervisor, SupervisorOptions, WorkerState

T = TypeVar("T")
START_TIMEOUT_S = 15.0
STOP_TIMEOUT_S = 8.0


class HostNotRunning(RuntimeError):
    """The hardware host is not running (see its ``state`` and ``reason``)."""


class HardwareHost:
    """See the module docstring."""

    def __init__(self, paths: RuntimePaths | None = None, *, options: SupervisorOptions | None = None,
                 check_components: bool = True, remote: Any = None) -> None:
        self.paths = paths or default_paths()
        self.options = options
        self.check_components = check_components
        self.remote = remote
        """A desktop gateway computer's enrollment (``None``: the backend's own host)."""
        self.http_transport: Any = None
        """Tests: the HTTP transport a remote host and its workers use."""
        self.state = "stopped"
        """``stopped`` | ``running`` | ``unavailable`` (components) | ``busy_elsewhere`` | ``failed`` |
        ``revoked`` (a remote host Cremind removed)."""
        self.reason: str | None = None
        self.host_id: str | None = None
        self.readiness: dict[str, Any] | None = None
        self.fonts_pack: str | None = None
        self._guard = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server_loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._supervisor: Supervisor | None = None
        self._lock: Any = None
        self._ready = threading.Event()
        self._fonts: Any = None
        self._agent: Any = None
        self.client_factory: Callable[[], Any] | None = None
        """Tests: the host client the agent uses (default: in process, as the server host)."""

    @property
    def running(self) -> bool:
        return self.state == "running" and self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ lifecycle

    def start(self, server_loop: asyncio.AbstractEventLoop) -> bool:
        """Start hosting (blocking up to ``START_TIMEOUT_S``); ``False`` with ``state``/``reason`` when it cannot."""
        with self._guard:
            if self.running:
                return True
            from .components import can_host, readiness
            from .fonts import pinned_pack

            self.paths.ensure()
            self.host_id = self.remote.host_id if self.remote is not None else host_identity(self.paths)["host_id"]
            self.readiness = readiness(self.paths.assets_dir, pinned_pack())
            if self.check_components and not can_host(self.readiness):
                self.state, self.reason = "unavailable", _components_reason(self.readiness)
                return False
            from app.tags.runtime.connect.instance import InstanceLock

            lock = InstanceLock(self.paths.host_lock, self.paths.host_lock_info)
            if not lock.acquire():
                self.state = "busy_elsewhere"
                self.reason = "Another Cremind process on this computer runs the gateways."
                return False
            lock.write_info(host_id=self.host_id)
            from . import logs

            logs.install()
            self._lock, self._server_loop = lock, server_loop
            self._ready.clear()
            self._thread = threading.Thread(target=self._main, name="tags-hardware", daemon=True)
            self._thread.start()
            if not self._ready.wait(START_TIMEOUT_S):
                self.state, self.reason = "failed", "The hardware runtime did not start in time."
                self._shutdown_locked()
                return False
            self.state, self.reason = "running", None
            # The pack it draws with, beside who holds the lock: `cremind tags host prepare` tells whether a
            # restart is due.
            with contextlib.suppress(OSError, RuntimeError):
                lock.write_info(host_id=self.host_id, fonts_pack=self.fonts_pack)
            logger.info(f"[tags] hardware host {self.host_id} running (font pack {self.fonts_pack or 'none'})")
            return True

    def stop(self, why: str = "stopping") -> None:
        with self._guard:
            if self._thread is None:
                return
            logger.info(f"[tags] hardware host stopping ({why})")
            self._shutdown_locked()
            self.state, self.reason = "stopped", why

    def _shutdown_locked(self) -> None:
        loop, stop, thread = self._loop, self._stop, self._thread
        if loop is not None and stop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass
        if thread is not None:
            thread.join(STOP_TIMEOUT_S)
            if thread.is_alive():
                logger.warning("[tags] the hardware runtime did not stop in time; leaving it to the process exit")
        if self._lock is not None:
            self._lock.release()
        self._thread = self._loop = self._stop = self._supervisor = None
        self._lock = None

    def _main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._serve())
        except BaseException:  # noqa: BLE001 - nothing escapes the runtime thread
            logger.exception("[tags] the hardware runtime stopped with an error")
            self.state, self.reason = "failed", "The hardware runtime stopped with an error (see the log)."
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(loop.shutdown_default_executor(timeout=5.0))
            except Exception:  # noqa: BLE001
                logger.debug("[tags] the hardware runtime's loop did not close cleanly", exc_info=True)
            finally:
                loop.close()

    async def _serve(self) -> None:
        from app.tags.runtime.host.agent import HostAgent

        from .local_host import LocalHostClient

        self._stop = asyncio.Event()
        self._fonts, self.fonts_pack = await asyncio.to_thread(self._load_fonts, await self._bridge_packs())
        options = self.options or SupervisorOptions()
        if options.pause_reason is None:
            options.pause_reason = update_in_progress
        self._supervisor = Supervisor(self.paths, self._build_worker, options)
        if self.client_factory is not None:
            client = self.client_factory()
        elif self.remote is not None:
            from app.tags.runtime.host.http_client import HttpHostClient

            client = HttpHostClient(self.remote.server, self.remote.authorization, ca_pem=self.remote.ca_pem,
                                    transport=self.http_transport)
        else:
            client = LocalHostClient(self.host_id or "", self._server_loop)
        self._agent = HostAgent(client, self._supervisor, self.facts())
        self._supervisor.on_change = self._agent.poke
        if self.remote is not None:
            self._agent.on_revoked = self._revoked
        # Before the agent's first status report: it tells Cremind searches may start.
        self.state, self.reason = "running", None
        self._ready.set()
        agent = asyncio.create_task(self._agent.run(self._stop), name="host agent")
        # Gateways the older Cremind Connect ran on this computer move in (app/tags/hosting/migration.py).
        migrating = asyncio.create_task(self._migrate_from_connect(client), name="connect migration")
        try:
            await self._supervisor.run(self._stop)
        finally:
            self._stop.set()
            migrating.cancel()
            for task in (agent, migrating):
                with contextlib.suppress(BaseException):
                    await task

    def facts(self) -> Any:
        """How this host describes itself to Cremind (:class:`~app.tags.runtime.host.agent.HostFacts`)."""
        from app.__version__ import __version__
        from app.tags.runtime.host.agent import HostFacts, computer_name

        from .components import readiness
        from .fonts import pinned_pack

        def capabilities() -> dict[str, Any]:
            # Running, the host draws with the pack it loaded: still another one than the pin is a font update.
            loaded = self.fonts_pack if self.state == "running" else None
            self.readiness = readiness(self.paths.assets_dir, pinned_pack(), loaded)
            return {"readiness": {k: v for k, v in self.readiness.items() if k != "usb"},
                    "usb": self.readiness.get("usb")}

        remote = self.remote
        return HostFacts(name=(remote.host_name if remote is not None and remote.host_name else computer_name()),
                         server_origin=remote.server if remote is not None else server_origin(),
                         capabilities=capabilities, state=lambda: (self.state, self.reason), version=__version__,
                         fonts_pack=lambda: self.fonts_pack, host_id=self.host_id,
                         ca_pem=remote.ca_pem if remote is not None else None,
                         migration=lambda: _migration_summary(self.paths))

    async def _authority_id(self) -> str | None:
        """This server's authority key id: the Cremind Connect workers recording it are this server's."""
        if self.remote is not None:
            return self.remote.server_authority_id or None
        from app.tags.authority import AuthorityUnavailable, get_authority

        from .local_connector import on_server

        async def read() -> str | None:
            try:
                return (await get_authority()).authority_id.hex()
            except AuthorityUnavailable:  # no gateway can be this server's yet: nothing to move
                return None

        return await on_server(read(), self._server_loop, 30.0)

    async def _migrate_from_connect(self, client: Any) -> None:
        from . import migration

        try:
            connect = await asyncio.to_thread(migration.connect_paths)
            if connect is None:
                return
            authority_id = await self._authority_id()
            if not authority_id or self._supervisor is None:
                return
            # A desktop gateway computer serves one profile: only that profile's workers can move to it.
            profile_id = (self.remote.profile_id or None) if self.remote is not None else None
            outcomes = await migration.migrate(self.paths, connect, authority_id=authority_id,
                                               host_id=self.host_id or "", adopt=client.adopt_worker,
                                               start=self._supervisor.add_worker, profile_id=profile_id)
            if outcomes:
                logger.info("[tags] from Cremind Connect: " + ", ".join(f"{o.worker_id} {o.state}" for o in outcomes))
                if self._agent is not None:
                    self._agent.poke()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - Connect keeps its workers; the next start tries again
            logger.exception("[tags] moving gateways from Cremind Connect failed")

    def _revoked(self, exc: Exception) -> None:
        """A remote host's credential stopped working (it was removed, or re-enrolled elsewhere): stop driving
        the gateways, and say why."""
        self.state, self.reason = "revoked", (f"{self.remote.server} no longer accepts this computer ({exc}). "
                                              "Set it up again from the Cremind page.")
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)

    async def report(self) -> None:
        """From the server's loop, while the runtime is not running: say so in the host's record (the page
        shows why, and offers to prepare the components)."""
        from app.tags import hosts
        from app.tags.runtime.host.agent import platform_name

        if self.remote is not None:
            return  # a remote host says so itself when it can reach Cremind
        if self.host_id is None:
            self.paths.ensure()
            self.host_id = host_identity(self.paths)["host_id"]
        facts = self.facts()
        body = {"name": facts.name, "platform": platform_name(), "version": facts.version,
                "capabilities": facts.capabilities(),
                "status": {"state": self.state, "reason": self.reason, "gateways": [], "workers": []}}
        await hosts.host_hello(hosts.HostPrincipal(self.host_id, hosts.SERVER), body)

    async def _bridge_packs(self) -> list[str]:
        """The packs this host's bridges show (the server's own host, from Cremind's records; best effort): what
        it falls back to while the pinned pack is not installed, so their tags keep drawing. Asked only when
        that choice matters: several packs installed, none of them the pinned one."""
        if self.remote is not None:
            return []  # a desktop gateway computer has no records to ask: the fallback is the first pack
        try:
            from app.tags.runtime.resources import font_assets_in

            from .fonts import pinned_pack

            installed = [a.pack_id for a in font_assets_in(self.paths.assets_dir)]
            if len(installed) < 2 or pinned_pack() in installed:
                return []
            from app.tags.hosts import bridge_font_packs

            from .local_connector import on_server

            return await asyncio.wait_for(on_server(bridge_font_packs(self.host_id or ""), self._server_loop, 10.0),
                                          10.0)
        except Exception:  # noqa: BLE001 - the fallback stays the first pack
            logger.debug("[tags] could not read the font packs the bridges show", exc_info=True)
            return []

    def _load_fonts(self, also_prefer: Sequence[str] = ()) -> tuple[Any, str | None]:
        """The verified font pack the workers share (loaded once: about 90 MB of fonts): the pack this Cremind
        pins, else — until the components are prepared — one the bridges show (``also_prefer``), else the first
        installed."""
        try:
            from app.tags.runtime.fonts.fontset import FontSet
            from app.tags.runtime.resources import find_font_assets

            from .fonts import pinned_pack

            pinned = pinned_pack()
            assets = find_font_assets(roots=[self.paths.assets_dir], prefer=pinned, also_prefer=also_prefer)
            if assets is None:
                logger.warning("[tags] no font pack is installed: workers connect and pair, screens wait")
                return None, None
            if pinned and assets.pack_id != pinned:
                logger.warning(f"[tags] font pack {assets.pack_id} is loaded; this Cremind pins {pinned} — prepare "
                               "the gateway computer to update")
            return FontSet.load(assets.pack_path, assets.cache_dir), assets.pack_id
        except Exception:  # noqa: BLE001
            logger.exception("[tags] the installed font pack could not be loaded; screens wait for a good one")
            return None, None

    # ------------------------------------------------------------------ workers

    def _build_worker(self, worker: WorkerState, port: str) -> tuple[Any, Any]:
        """Runs in a thread of the runtime: the daemon and agent of one worker, reaching Cremind in process (or,
        on a remote host, over HTTPS with the worker's own credentials)."""
        from app.tags.runtime.connect.worker import WorkerConfigError, build

        if self.remote is not None:
            return build(worker.directory, port, None, fonts=self._fonts, font_roots=[self.paths.assets_dir],
                         transport=self.http_transport)
        from app.tags.connector_service import WorkerExpectation

        from .local_connector import LocalConnector

        spec = worker.spec
        try:
            expect = WorkerExpectation(companion_id=spec.companion_id, profile_id=str(spec.extra["profile_id"]),
                                       generation=int(spec.extra.get("generation") or 0),
                                       host_id=spec.extra.get("host_id") or None)
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkerConfigError(f"worker.json lacks {exc}") from None
        server_loop = self._server_loop

        def connector(credential: Any) -> LocalConnector:
            return LocalConnector(credential, expect=expect, server_loop=server_loop)

        return build(worker.directory, port, None, fonts=self._fonts, client_factory=connector,
                     font_roots=[self.paths.assets_dir])

    # ------------------------------------------------------------------ the server's side

    async def call(self, fn: Callable[[Supervisor], Awaitable[T]], timeout: float = 30.0) -> T:
        """Run ``fn(supervisor)`` on the runtime's loop and await it from the server's."""
        loop, supervisor = self._loop, self._supervisor
        if not self.running or loop is None or supervisor is None:
            raise HostNotRunning(self.reason or "The hardware host is not running.")
        future = asyncio.run_coroutine_threadsafe(asyncio.wait_for(fn(supervisor), timeout), loop)
        return await asyncio.wrap_future(future)

    async def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"state": self.state, "reason": self.reason, "host_id": self.host_id,
                               "fonts_pack": self.fonts_pack, "readiness": self.readiness}
        if self.running:
            async def snapshot(sup: Supervisor) -> dict[str, Any]:
                return sup.status()

            try:
                out.update(await self.call(snapshot, timeout=5.0))
            except (HostNotRunning, TimeoutError, RuntimeError) as exc:
                out["status_error"] = str(exc)
        return out


UPDATE_STALE_S = 30 * 60.0


def update_in_progress() -> str | None:
    """An in-app update is replacing Cremind's files (``<SYS>/.upgrade.lock``, written by every update path:
    the API, ``cremind upgrade apply`` and the desktop app). A lock older than ``UPDATE_STALE_S`` is a crashed
    update and holds nothing."""
    import json
    import time
    from pathlib import Path

    from app.config.settings import BaseConfig

    lock = Path(BaseConfig.CREMIND_SYSTEM_DIR) / ".upgrade.lock"
    try:
        started = float(json.loads(lock.read_text(encoding="utf-8")).get("started_at") or 0)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, AttributeError):
        started = lock.stat().st_mtime if lock.exists() else 0.0
    if time.time() - started > UPDATE_STALE_S:
        return None
    return "an update is being installed"


def server_origin() -> str:
    """The backend's own origin, recorded in the worker directories it creates (informational: its
    workers reach it in process)."""
    from urllib.parse import urlsplit

    from app.config.settings import BaseConfig

    parts = urlsplit(str(getattr(BaseConfig, "APP_URL", "") or ""))
    if parts.scheme in ("http", "https") and parts.hostname:
        return f"{parts.scheme}://{parts.netloc}"
    return "http://localhost:1515"


def _migration_summary(paths: RuntimePaths) -> dict[str, int]:
    from .migration import summary

    return summary(paths)


def _components_reason(readiness: dict[str, Any]) -> str:
    from .components import OUTDATED

    # An outdated font pack stands in nobody's way: screens keep working with it.
    blocked = [c for c in readiness.get("components") or [] if c.get("state") not in ("ready", OUTDATED)]
    if not blocked:
        return "The gateway components are not ready."
    return " ".join(c.get("detail") or f"{c['key']}: {c['state']}" for c in blocked)


# ── the process-wide host ─────────────────────────────────────────────────

_host: HardwareHost | None = None


def get_host() -> HardwareHost:
    global _host
    if _host is None:
        _host = HardwareHost()
    return _host


def server_hardware_enabled() -> bool:
    """Whether this server hosts gateways on its own USB ports."""
    env = os.environ.get("CREMIND_TAGS_SERVER_HARDWARE")
    if env is not None and env.strip():
        return env.strip().lower() in ("1", "true", "on", "yes")
    try:
        from app.config.settings import get_dynamic

        value = get_dynamic("server_config", "tags_server_hardware")
    except Exception:  # noqa: BLE001 - no config storage yet
        value = None
    if isinstance(value, str) and value.strip().lower() == "off":
        return False
    from app.tags.operations import simple_setup_enabled

    return simple_setup_enabled()


async def start_hosting() -> dict[str, Any]:
    """Boot (or after preparing components): start the hardware host when it should run; never raises."""
    host = get_host()
    try:
        if not server_hardware_enabled():
            host.state, host.reason = "disabled", "Hardware setup is off on this server."
        else:
            await asyncio.to_thread(host.start, asyncio.get_running_loop())
    except Exception:  # noqa: BLE001
        logger.exception("[tags] the hardware host could not start")
        host.state, host.reason = "failed", "The hardware host could not start (see the log)."
    if host.state != "running":
        logger.info(f"[tags] hardware host not running: {host.state} ({host.reason})")
        try:
            await host.report()
        except Exception:  # noqa: BLE001 - no storage yet, or no host tables: nothing to show
            logger.debug("[tags] could not record the hardware host's state", exc_info=True)
    return {"state": host.state, "reason": host.reason}


_tasks: set[asyncio.Task[Any]] = set()


def schedule_start() -> None:
    """Boot: :func:`start_hosting` in the background (boot never waits for the hardware)."""
    task = asyncio.get_running_loop().create_task(start_hosting(), name="tags hardware host")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def stop_hosting(why: str = "stopping") -> None:
    """Shutdown, an update, a restore: stop every worker and release the gateways; never raises."""
    host = _host
    if host is None:
        return
    try:
        await asyncio.to_thread(host.stop, why)
    except Exception:  # noqa: BLE001
        logger.exception("[tags] the hardware host did not stop cleanly")


__all__ = ["HardwareHost", "HostNotRunning", "get_host", "schedule_start", "server_hardware_enabled", "start_hosting",
           "stop_hosting", "update_in_progress"]
