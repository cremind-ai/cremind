"""The hardware host's agent: Cremind's searches and connections for this computer.

It runs beside the host's supervisor (which owns the USB ports and the
workers) and talks to Cremind through a :class:`HostClient` — in process for
the backend's own host, over HTTPS with a host credential for a desktop host:

- **Status** — ``hello`` every ``HELLO_EVERY_S`` (and after a change): the
  computer's name, platform and version, its components and USB access, the
  gateways it sees (device ids, never port paths) and its workers.
- **Search** (``host_scan``) — probe every free port now and report what
  answered: each device's identity (role, device id, key, firmware, owner
  state, generation) and, for ports that did not identify, why (busy, no
  access, no answer). Cremind decides what each means for the profile that
  searched and hands it opaque candidates.
- **Connection** (``host_connect``) — for the gateway a candidate named:
  find the port it is on now, take its exclusive lock and ask it again
  (``IDENTIFY``: the same device id and identity key, or nothing happens);
  stage a controller key and two credential secrets
  (``workers/.staging-<operation>``, so a crash cannot lose what Cremind
  accepted); register the worker with Cremind (the companion, its
  credentials by hash, the gateway's binding, the ``claim_gateway``
  operation — a repeat returns the same); write ``workers/<companion_id>``
  and hand it to the supervisor. The worker then claims the gateway, and
  Cremind completes the connection on its first heartbeat after the claim.

Nothing here logs a key, a credential secret or a grant.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import platform
import shutil
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

HELLO_EVERY_S = 20.0
WORK_WAIT_S = 25
RETRY_S = 5.0
LOCK_WAIT_S = 10.0
WORKER_SCHEMA = "cremind/tag-worker@1"


class HostError(Exception):
    """Cremind refused a host request (``status``, ``code`` as the API answers them)."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code

    @property
    def final(self) -> bool:
        """Retrying the same request cannot help (a 4xx other than 408/429)."""
        return self.status is not None and 400 <= self.status < 500 and self.status not in (408, 429)


class HostClient(Protocol):
    """How the agent reaches Cremind (in process, or ``/api/tag-host/v1`` over HTTPS)."""

    async def hello(self, body: dict[str, Any]) -> dict[str, Any]: ...
    async def work(self, wait: int) -> list[dict[str, Any]]: ...
    async def progress(self, op_id: str, body: dict[str, Any]) -> dict[str, Any]: ...
    async def register_worker(self, op_id: str, body: dict[str, Any]) -> dict[str, Any]: ...
    async def aclose(self) -> None: ...


class Supervision(Protocol):
    """What the agent needs from the host's supervisor."""

    paths: Any

    async def scan(self) -> list[dict[str, Any]]: ...
    def port_of(self, device_id: str) -> str | None: ...
    def add_worker(self, worker_id: str) -> None: ...
    def status(self) -> dict[str, Any]: ...


class ConnectFailed(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class HostFacts:
    """How this host describes itself (``hello``)."""

    name: str
    server_origin: str
    """What a worker directory records as its server (the backend's own origin, or the server a desktop host
    was enrolled with)."""
    capabilities: Callable[[], dict[str, Any]]
    state: Callable[[], tuple[str, str | None]] = lambda: ("running", None)
    version: str = ""
    fonts_pack: Callable[[], str | None] = lambda: None
    host_id: str | None = None
    """Recorded in each worker directory: the worker runs on this host (Cremind checks it)."""


def computer_name() -> str:
    for candidate in (os.environ.get("COMPUTERNAME"), socket.gethostname(), platform.node()):
        if candidate:
            return candidate.split(".")[0][:64]
    return "This computer"


def platform_name() -> str:
    import sys

    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


class HostAgent:
    """See the module docstring."""

    def __init__(self, client: HostClient, supervisor: Supervision, facts: HostFacts, *,
                 clock: Callable[[], float] = time.monotonic, probe: Callable[[str], Any] | None = None) -> None:
        self.client = client
        self.supervisor = supervisor
        self.facts = facts
        self.clock = clock
        self._probe = probe
        self._hello_now = asyncio.Event()
        self._running: dict[str, asyncio.Task[None]] = {}
        self.last_hello_error: str | None = None
        self.handled = 0

    # ------------------------------------------------------------------ loops

    async def run(self, stop: asyncio.Event) -> None:
        tasks = [asyncio.create_task(self._hello_loop(stop), name="host hello"),
                 asyncio.create_task(self._work_loop(stop), name="host work")]
        try:
            await stop.wait()
        finally:
            for task in [*tasks, *self._running.values()]:
                task.cancel()
            for task in [*tasks, *self._running.values()]:
                with contextlib.suppress(BaseException):
                    await task

    def poke(self) -> None:
        """Report status now (a worker started or stopped, components changed)."""
        self._hello_now.set()

    def status_report(self) -> dict[str, Any]:
        sup = self.supervisor.status()
        state, reason = self.facts.state()
        gateways = []
        workers_by_device = {w.get("gateway_device_id"): w for w in sup.get("workers") or []}
        for port in sup.get("ports") or []:
            identity = port.get("identity") or {}
            if identity.get("role") != "gateway":
                continue
            worker = workers_by_device.get(identity.get("device_id")) or {}
            gateways.append({"device_id": identity.get("device_id"), "state": identity.get("owner_state"),
                             "in_use": bool(port.get("held_by")), "companion_id": worker.get("companion_id")})
        for worker in sup.get("workers") or []:  # a gateway its worker holds (its port shows no fresh identity)
            device = worker.get("gateway_device_id")
            if worker.get("state") == "running" and not any(g["device_id"] == device for g in gateways):
                gateways.append({"device_id": device, "state": "owned", "in_use": True,
                                 "companion_id": worker.get("companion_id")})
        return {"state": "paused" if sup.get("paused") and state == "running" else state,
                "reason": sup.get("paused") or reason, "fonts_pack": self.facts.fonts_pack(),
                "paused": sup.get("paused"), "gateways": gateways, "workers": sup.get("workers") or []}

    async def hello(self) -> dict[str, Any]:
        body = {"name": self.facts.name, "platform": platform_name(), "version": self.facts.version,
                "capabilities": {**self.facts.capabilities(), "arch": platform.machine().lower()},
                "status": self.status_report()}
        return await self.client.hello(body)

    async def _hello_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self._hello_now.clear()
            delay = HELLO_EVERY_S
            try:
                answer = await self.hello()
                self.last_hello_error = None
                delay = float(answer.get("hello_every_s") or HELLO_EVERY_S)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - Cremind unreachable: try again soon
                self.last_hello_error = str(exc)
                log.info("host: status report failed (%s); again in %.0f s", exc, RETRY_S)
                delay = RETRY_S
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._hello_now.wait(), delay)

    async def _work_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                items = await self.client.work(WORK_WAIT_S)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.info("host: work poll failed (%s); again in %.0f s", exc, RETRY_S)
                await asyncio.sleep(RETRY_S)
                continue
            for item in items:
                op_id = str(item.get("id") or "")
                if op_id and op_id not in self._running:
                    task = asyncio.create_task(self.handle(item), name=f"host {item.get('kind')} {op_id}")
                    self._running[op_id] = task
                    task.add_done_callback(lambda _t, key=op_id: self._running.pop(key, None))

    # ------------------------------------------------------------------ work

    async def handle(self, item: dict[str, Any]) -> None:
        op_id, kind = str(item["id"]), str(item.get("kind"))
        try:
            if kind == "host_scan":
                await self._scan(op_id)
            elif kind == "host_connect":
                await self._connect(op_id, dict(item.get("args") or {}))
            else:
                await self._report(op_id, state="failed", error={"code": "unsupported",
                                                                 "message": f"This computer cannot run {kind}."})
        except asyncio.CancelledError:
            raise
        except ConnectFailed as exc:
            log.warning("host: %s %s failed: %s (%s)", kind, op_id, exc.code, exc)
            with contextlib.suppress(Exception):
                await self._report(op_id, state="failed", error={"code": exc.code, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - reported, never raised into the loop
            log.exception("host: %s %s failed", kind, op_id)
            with contextlib.suppress(Exception):
                await self._report(op_id, state="failed", error={"code": "host_error",
                                                                 "message": f"The computer could not finish: {exc}"})
        finally:
            self.handled += 1
            self.poke()

    async def _report(self, op_id: str, **fields: Any) -> dict[str, Any]:
        return await self.client.progress(op_id, {k: v for k, v in fields.items() if v is not None})

    async def _scan(self, op_id: str) -> None:
        await self._report(op_id, stage="scanning", detail="Looking at this computer's USB ports")
        ports = await self.supervisor.scan()
        found, unidentified = [], []
        for port in ports:
            identity = port.get("identity")
            if identity:
                found.append({**{k: identity.get(k) for k in ("device_id", "ik", "role", "proto", "fw", "build",
                                                              "board", "owner_state", "gen", "authority_id")},
                              "in_use": bool(port.get("in_use"))})
            elif port.get("in_use") and port.get("gateway_device_id"):
                continue  # our own worker's gateway: reported by status, not searchable
            elif port.get("reason"):
                unidentified.append({"reason": port.get("reason"), "detail": port.get("detail") or ""})
        await self._report(op_id, state="succeeded", found=found, unidentified=unidentified)

    async def _connect(self, op_id: str, args: dict[str, Any]) -> None:
        device = str(args.get("device_id") or "").lower()
        ik = str(args.get("ik") or "").lower()
        if len(device) != 32 or len(ik) != 64:
            raise ConnectFailed("invalid_connection", "The connection names no gateway.")
        await self._report(op_id, stage="preparing", detail="Looking for the gateway")
        port = self.supervisor.port_of(device)
        if port is None:
            await self.supervisor.scan()
            port = self.supervisor.port_of(device)
        if port is None:
            raise ConnectFailed("gateway_not_found", "The gateway is not plugged into this computer any more.")
        from app.tags.runtime.connect.instance import InstanceLock

        lock = InstanceLock(self.supervisor.paths.gateway_lock(device))
        deadline = self.clock() + LOCK_WAIT_S
        while not await asyncio.to_thread(lock.acquire):
            if self.clock() >= deadline:
                raise ConnectFailed("gateway_busy", "Another program is using the gateway (close it and try again).")
            await asyncio.sleep(0.5)
        try:
            await self._report(op_id, stage="checking", detail="Checking the gateway")
            identity = await self._identify(port)
        finally:
            await asyncio.to_thread(lock.release)
        if identity.get("device_id") != device or identity.get("ik") != ik:
            raise ConnectFailed("gateway_changed", "Another gateway answered on that port; search again.")
        staged = await asyncio.to_thread(_stage, self.supervisor.paths.workers_dir, op_id)
        await self._report(op_id, stage="registering", detail="Setting up the gateway's worker")
        try:
            registered = await self.client.register_worker(op_id, {
                "controller_pub": staged.controller_pub.hex(),
                "credentials": {"hardware_sha256": _sha256(staged.hardware_secret),
                                "content_sha256": _sha256(staged.content_secret)},
                "gateway": {k: identity.get(k) for k in ("device_id", "ik", "role", "proto", "fw", "board", "gen",
                                                         "owner_state")}})
        except HostError as exc:
            if exc.final:
                await asyncio.to_thread(shutil.rmtree, staged.directory, True)
                raise ConnectFailed(exc.code or "refused", str(exc)) from None
            raise
        worker_id = await asyncio.to_thread(_finish_worker_dir, self.supervisor.paths.workers_dir, staged, registered,
                                            identity, self.facts.server_origin, self._host_id())
        self.supervisor.add_worker(worker_id)
        log.info("host: connection %s: worker %s set up for gateway %s…", op_id, worker_id, device[:8])
        await self._report(op_id, stage="claiming", detail="Connecting to the gateway")

    def _host_id(self) -> str | None:
        return self.facts.host_id

    async def _identify(self, port: str) -> dict[str, Any]:
        probe = self._probe
        if probe is None:
            from app.tags.runtime.connect.probe import probe_port as probe
        result = await asyncio.to_thread(probe, port)
        if result.identity is None:
            reasons = {"busy": ("gateway_busy", "Another program is using the gateway (close it and try again)."),
                       "no_access": ("usb_access_denied", "This computer does not allow Cremind to open the gateway's "
                                                          "USB port."),
                       "v1_firmware": ("unsupported_firmware", "This gateway needs a firmware update first.")}
            code, text = reasons.get(result.reason or "", ("gateway_not_found", "The gateway did not answer."))
            raise ConnectFailed(code, text)
        return result.identity.as_json(include_challenge=False)


# ---------------------------------------------------------------------------
# The worker directory a connection leaves behind
# ---------------------------------------------------------------------------


def _sha256(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _stage(workers_dir: Path, op_id: str) -> Any:
    from app.tags.runtime.connect.setup_flow import Staged

    workers_dir.mkdir(parents=True, exist_ok=True)
    return Staged.load_or_create(workers_dir, op_id)


def _finish_worker_dir(workers_dir: Path, staged: Any, registered: dict[str, Any], gateway: dict[str, Any],
                       server_origin: str, host_id: str | None) -> str:
    """Turn the staging directory into ``workers/<companion_id>``."""
    from app.tags.runtime.connect.setup_flow import STAGING_FILE
    from app.tags.runtime.connect.workerdir import WorkerSpec, write_worker
    from app.tags.runtime.secrets import FileBackend, SecretStore

    companion_id = str(registered["companion_id"])
    creds = registered.get("credentials") or {}
    server = registered.get("server") or {}
    profile = registered.get("profile") or {}
    store = SecretStore(FileBackend(staged.directory / "secrets.json"))
    store.set_credential("hardware", f"{creds['hardware_id']}.{staged.hardware_secret}")
    store.set_credential("content", f"{creds['content_id']}.{staged.content_secret}")
    spec = WorkerSpec(companion_id, server_origin, str(profile.get("name") or ""), companion_id,
                      str(gateway["device_id"]).lower(), True, extra={
                          "schema": WORKER_SCHEMA, "profile_id": str(profile.get("id") or ""),
                          "installation_id": str(server.get("installation_id") or ""),
                          "authority_pub": str(server.get("authority_pub") or ""),
                          "authority_id": str(server.get("authority_id") or ""),
                          "gateway_ik": str(gateway["ik"]).lower(),
                          "operation": "recover_gateway" if registered.get("recovery_operation_id")
                          else "connect_gateway",
                          "operation_id": registered.get("operation_id"), "generation": int(registered.get("generation") or 0),
                          "host_id": host_id,
                          "credentials": {"hardware_id": creds.get("hardware_id"), "content_id": creds.get("content_id")},
                      })
    write_worker(staged.directory, spec)
    (staged.directory / STAGING_FILE).unlink(missing_ok=True)
    target = workers_dir / companion_id
    if target.exists():
        old = target.with_name(f".replaced-{companion_id}-{int(time.time())}")
        os.replace(target, old)
        shutil.rmtree(old, ignore_errors=True)
    os.replace(staged.directory, target)
    return companion_id


__all__ = ["ConnectFailed", "HostAgent", "HostClient", "HostError", "HostFacts", "computer_name", "platform_name"]
