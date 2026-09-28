"""The hardware host's supervisor: this computer's gateways and their workers.

It runs on the hardware runtime's own event loop (:mod:`.host`), never on the
server's. Every ``SCAN_INTERVAL_S``:

- **Ports** — the candidate serial ports (Cremind Tag's USB ids, plus
  ``CREMIND_CONNECT_EXTRA_PORTS`` for development boards and the simulator)
  are listed; a port that is new, changed, or free again after a worker used
  it is probed (``IDENTIFY``: role, device id, owner state) unless a worker
  holds it. A port name is only an observation: a gateway is known by its
  device id, wherever it shows up.
- **Workers** — every enabled worker directory whose gateway is attached
  gets a worker: the delivery daemon and its agent, reaching Cremind through
  the in-process connector (:mod:`.local_connector`) with what the worker
  believes it is (companion, profile UUID, generation, this host). Never two
  workers for one gateway: each takes the gateway's OS lock
  (``locks/gateway-<device_id>.lock``, so a second Cremind process — or a
  Cremind Connect that still runs — cannot drive it too) and opens the port
  exclusively. A worker that ends is restarted with back-off (1 s doubling to
  60 s, reset after a minute of running); a gateway that reappears, also on
  another port, restarts its worker at once. A worker Cremind removed
  disables its own directory and is not started again.

Hardware errors stay here: a worker failing, a port refusing to open or a
probe hanging never reaches the server's loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import RuntimePaths

log = logging.getLogger("app.tags.runtime.hosting")

SCAN_INTERVAL_S = 2.0
BACKOFF_INITIAL_S = 1.0
BACKOFF_MAX_S = 60.0
HEALTHY_RUN_S = 60.0
STOP_GRACE_S = 5.0
PROBE_TIMEOUT_S = 20.0
# When to probe a port again after an unsuccessful probe (None: not until it is plugged in again).
RETRY_AFTER_S: dict[str | None, float | None] = {"busy": 5.0, "no_answer": 10.0, "gone": 2.0, "no_access": 30.0,
                                                 "error": 30.0, "v1_firmware": None}


class GatewayBusy(RuntimeError):
    """Another process holds the gateway's lock (a second Cremind, or Cremind Connect)."""


@dataclass
class PortState:
    info: Any
    """:class:`app.tags.runtime.connect.usb.PortInfo`."""
    probe: Any = None
    """:class:`app.tags.runtime.connect.probe.ProbeResult` of the last probe."""
    probed_at: float | None = None
    held_by: str | None = None
    fresh: bool = True
    stale: bool = False

    @property
    def identity(self) -> Any:
        return self.probe.identity if self.probe is not None and not self.stale else None

    @property
    def gateway_id(self) -> str | None:
        identity = self.identity
        return identity.device_id_hex if identity is not None and identity.is_gateway else None

    def as_json(self) -> dict[str, Any]:
        identity = self.probe.identity if self.probe is not None else None
        return {"device": self.info.device, "usb_id": self.info.usb_id, "held_by": self.held_by,
                "identity": identity.as_json(include_challenge=False) if identity is not None else None,
                "reason": self.probe.reason if self.probe is not None else None,
                "detail": self.probe.detail if self.probe is not None else None}


@dataclass
class WorkerState:
    worker_id: str
    directory: Path
    spec: Any = None
    error: str | None = None
    task: asyncio.Task[None] | None = None
    stop: asyncio.Event | None = None
    port: str | None = None
    started_at: float | None = None
    next_start: float = 0.0
    failures: int = 0
    restarts: int = 0
    last_error: str | None = None
    stopping: bool = False
    conflict: bool = False
    svc: Any = None
    agent: Any = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def state(self, now: float) -> str:
        if self.spec is None:
            return "invalid"
        if self.stopping:
            return "stopping"
        if self.running:
            return "running"
        if not self.spec.enabled:
            return "disabled"
        if self.conflict:
            return "conflict"
        if self.next_start > now:
            return "backoff"
        return "waiting_for_gateway"

    def as_json(self, now: float) -> dict[str, Any]:
        spec = self.spec
        live = self.svc if self.running else None
        gate = getattr(live, "gate", None)
        gateway = getattr(live, "gateway", None)
        connected = bool(gateway is not None and getattr(gateway, "connected", False))
        return {"worker_id": self.worker_id, "companion_id": spec.companion_id if spec else None,
                "profile": spec.profile if spec else None,
                "gateway_device_id": spec.gateway_device_id if spec else None,
                "enabled": bool(spec.enabled) if spec else False, "state": self.state(now),
                "gateway_connected": connected, "gate": gate.as_json() if gate is not None else None,
                "last_error": self.error or self.last_error,
                "restarts": self.restarts,
                "retry_in_s": round(max(0.0, self.next_start - now), 1) if self.state(now) == "backoff" else None}


WorkerFactory = Callable[[WorkerState, str], tuple[Any, Any]]
"""Builds ``(daemon, agent)`` for a worker directory and a port (runs in a thread)."""


@dataclass
class SupervisorOptions:
    """Injection points (tests); the defaults are the real thing."""

    list_ports: Callable[[], list[Any]] | None = None
    probe: Callable[[str], Any] | None = None
    clock: Callable[[], float] = time.monotonic
    scan_interval_s: float = SCAN_INTERVAL_S
    serve: Callable[..., Any] | None = None
    pause_reason: Callable[[], str | None] | None = None
    """Why hosting must hold still right now (an update replacing Cremind's files), or ``None``."""
    extra: dict[str, Any] = field(default_factory=dict)


class Supervisor:
    """See the module docstring."""

    def __init__(self, paths: RuntimePaths, factory: WorkerFactory, options: SupervisorOptions | None = None) -> None:
        self.paths = paths
        self.factory = factory
        self.opts = options or SupervisorOptions()
        self._ports: dict[str, PortState] = {}
        self._workers: dict[str, WorkerState] = {}
        self._probe_locks: dict[str, asyncio.Lock] = {}
        self._wake = asyncio.Event()
        self._stopping = False
        self.ticks = 0
        self.last_scan_error: str | None = None
        self.paused: str | None = None
        self.on_change: Callable[[], None] | None = None
        """Called when a worker starts or ends (the host agent reports its status then)."""

    # ------------------------------------------------------------------ run

    async def run(self, stop: asyncio.Event) -> None:
        """Scan and supervise until ``stop`` is set; then stop every worker."""
        try:
            while not stop.is_set():
                try:
                    await self.tick()
                    self.last_scan_error = None
                except Exception as exc:  # noqa: BLE001 - one bad scan never ends hosting
                    self.last_scan_error = f"{type(exc).__name__}: {exc}"
                    log.exception("hosting: scan failed")
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(_first(self._wake.wait(), stop.wait()), self.opts.scan_interval_s)
        finally:
            self._stopping = True
            await self.stop_all()

    def wake(self) -> None:
        self._wake.set()

    async def tick(self) -> None:
        self.ticks += 1
        reason = self.opts.pause_reason() if self.opts.pause_reason is not None else None
        if reason:
            if self.paused != reason:
                log.info("hosting: holding still (%s): workers stop until it is over", reason)
            self.paused = reason
            await self.stop_all()
            return
        if self.paused:
            log.info("hosting: resuming after %s", self.paused)
            self.paused = None
        ports = await asyncio.to_thread(self._list_ports)
        due = self._update_ports(ports)
        if due:
            await asyncio.gather(*(self._probe(state.info) for state in due))
        self._sync_workers()
        self._restart_moved()
        self._start_due()

    # ------------------------------------------------------------------ ports

    def _list_ports(self) -> list[Any]:
        if self.opts.list_ports is not None:
            return list(self.opts.list_ports())
        from app.tags.runtime.connect.usb import list_candidate_ports

        return list_candidate_ports()

    def _probe_fn(self, device: str) -> Any:
        if self.opts.probe is not None:
            return self.opts.probe(device)
        from app.tags.runtime.connect.probe import probe_port

        return probe_port(device)

    def _update_ports(self, ports: list[Any]) -> list[PortState]:
        now = self.opts.clock()
        seen = {p.device: p for p in ports}
        for device in list(self._ports):
            if device not in seen:
                log.info("hosting: port %s disappeared", device)
                del self._ports[device]
        for info in ports:
            state = self._ports.get(info.device)
            if state is not None and state.info.key != info.key:
                state = None  # another device under the same port name
            if state is None:
                holder = next((w.worker_id for w in self._workers.values() if w.running and w.port == info.device),
                              None)
                self._ports[info.device] = PortState(info, held_by=holder)
                log.info("hosting: port %s appeared (%s)", info.device, info.usb_id or info.role_hint)
            else:
                state.info = info
        due = []
        for state in self._ports.values():
            if state.held_by is not None:
                continue
            if state.probe is None or state.stale:
                due.append(state)
                continue
            if state.probe.identity is not None:
                continue
            retry = RETRY_AFTER_S.get(state.probe.reason, 30.0)
            if retry is not None and state.probed_at is not None and now - state.probed_at >= retry:
                due.append(state)
        return due

    async def _probe(self, info: Any) -> Any:
        lock = self._probe_locks.setdefault(info.device, asyncio.Lock())
        async with lock:
            state = self._ports.get(info.device)
            if state is None or state.held_by is not None:
                return None
            try:
                result = await asyncio.wait_for(asyncio.to_thread(self._probe_fn, info.device), PROBE_TIMEOUT_S)
            except TimeoutError:
                log.warning("hosting: probing %s timed out", info.device)
                return None
            state = self._ports.get(info.device)
            if state is None or state.info.key != info.key or state.held_by is not None:
                return result
            state.probe, state.probed_at, state.stale = result, self.opts.clock(), False
            if result.identity is not None:
                if state.fresh and result.identity.is_gateway:
                    self._gateway_appeared(result.identity.device_id_hex)
                state.fresh = False
                log.info("hosting: %s is a %s (%s…, %s, gen %d)", info.device, result.identity.role_name,
                         result.identity.device_id_hex[:8], result.identity.owner_state_name, result.identity.gen)
            else:
                log.info("hosting: %s: %s (%s)", info.device, result.reason, result.detail)
            return result

    def _gateway_appeared(self, device_id: str) -> None:
        now = self.opts.clock()
        for worker in self._workers.values():
            if worker.spec is not None and worker.spec.gateway_device_id == device_id and not worker.running:
                worker.next_start = min(worker.next_start, now)

    async def scan(self) -> list[dict[str, Any]]:
        """Probe every free candidate port now (for a gateway search); held ports are reported, never opened.

        Each entry: the port (for this host only), its identity or why there is none, ``in_use`` and the
        worker holding it."""
        ports = await asyncio.to_thread(self._list_ports)
        self._update_ports(ports)
        free = [s.info for s in self._ports.values() if s.held_by is None]
        for info in free:
            state = self._ports.get(info.device)
            if state is not None:
                state.stale = True  # a fresh challenge for every free port
        if free:
            await asyncio.gather(*(self._probe(info) for info in free))
        out = []
        for state in sorted(self._ports.values(), key=lambda p: p.info.device):
            item = state.as_json()
            item["in_use"] = state.held_by is not None
            if state.held_by is not None:
                worker = self._workers.get(state.held_by)
                item["gateway_device_id"] = worker.spec.gateway_device_id if worker and worker.spec else None
            out.append(item)
        return out

    def port_of(self, device_id: str) -> str | None:
        """The free port a gateway was last seen on (by its device id)."""
        for state in self._ports.values():
            if state.held_by is None and state.gateway_id == device_id:
                return state.info.device
        return None

    # ------------------------------------------------------------------ workers

    def _sync_workers(self) -> None:
        from app.tags.runtime.connect.workerdir import WorkerDirError, list_workers

        found = list_workers(self.paths.workers_dir)
        names = set()
        for directory, spec in found:
            names.add(directory.name)
            worker = self._workers.get(directory.name)
            if worker is None:
                worker = self._workers[directory.name] = WorkerState(directory.name, directory)
            if isinstance(spec, WorkerDirError):
                worker.error = str(spec)
                if not worker.running:
                    worker.spec = None
                continue
            previous, worker.spec, worker.error = worker.spec, spec, None
            moved = previous is not None and previous.gateway_device_id != spec.gateway_device_id
            if worker.running and not worker.stopping and (not spec.enabled or moved):
                self._request_stop(worker, "disabled" if not spec.enabled else "its gateway changed")
        for name in [n for n in self._workers if n not in names]:
            worker = self._workers[name]
            if worker.running:
                self._request_stop(worker, "its directory is gone")
            else:
                del self._workers[name]

    def _restart_moved(self) -> None:
        """A running worker whose port vanished while its gateway answers on another port: move it there."""
        for worker in self._workers.values():
            if not worker.running or worker.stopping or worker.port is None or worker.spec is None:
                continue
            if worker.port in self._ports:
                continue
            if self.port_of(worker.spec.gateway_device_id) is not None:
                self._request_stop(worker, "its gateway moved to another port", restart_now=True)

    def _start_due(self) -> None:
        now = self.opts.clock()
        busy = {w.spec.gateway_device_id for w in self._workers.values() if w.running and w.spec is not None}
        for worker in sorted(self._workers.values(), key=lambda w: w.worker_id):
            spec = worker.spec
            worker.conflict = False
            if spec is None or not spec.enabled or worker.running or worker.stopping:
                continue
            if spec.gateway_device_id in busy:
                worker.conflict = True  # another worker directory drives this gateway
                continue
            if worker.next_start > now:
                continue
            port = self.port_of(spec.gateway_device_id)
            if port is None:
                continue
            self._start(worker, port)
            busy.add(spec.gateway_device_id)

    def _start(self, worker: WorkerState, port: str) -> None:
        worker.stop = asyncio.Event()
        worker.port = port
        worker.started_at = self.opts.clock()
        state = self._ports.get(port)
        if state is not None:
            state.held_by = worker.worker_id
        worker.task = asyncio.create_task(self._run_worker(worker, port), name=f"worker {worker.worker_id}")
        worker.task.add_done_callback(lambda task, w=worker: self._ended(w, task))
        log.info("hosting: worker %s started on %s", worker.worker_id, port)
        self._changed()

    async def _run_worker(self, worker: WorkerState, port: str) -> None:
        from app.tags.runtime.connect.instance import InstanceLock

        from . import logs

        spec = worker.spec
        assert spec is not None and worker.stop is not None
        lock = InstanceLock(self.paths.gateway_lock(spec.gateway_device_id))
        if not await asyncio.to_thread(lock.acquire):
            raise GatewayBusy("another program drives this gateway (a second Cremind, or Cremind Connect)")
        token = logs.WORKER.set(worker.worker_id)
        try:
            svc, agent = await asyncio.to_thread(self.factory, worker, port)
            worker.svc, worker.agent = svc, agent
            serve = self.opts.serve
            if serve is None:
                from app.tags.runtime.connect.worker import serve
            await serve(svc, agent, stop=worker.stop)
        finally:
            logs.WORKER.reset(token)
            await asyncio.to_thread(lock.release)

    def _changed(self) -> None:
        if self.on_change is not None:
            with contextlib.suppress(Exception):
                self.on_change()

    def _ended(self, worker: WorkerState, task: asyncio.Task[None]) -> None:
        self._changed()
        now = self.opts.clock()
        ran = now - (worker.started_at or now)
        port = self._ports.get(worker.port or "")
        if port is not None and port.held_by == worker.worker_id:
            port.held_by = None
            port.stale = True  # look at it afresh before using it again
        worker.port = None
        requested = worker.stopping
        worker.stopping = False
        error = None
        if task.cancelled():
            error = "cancelled"
        elif task.exception() is not None:
            exc = task.exception()
            error = f"{type(exc).__name__}: {exc}"
            if not isinstance(exc, GatewayBusy):
                log.error("hosting: worker %s failed: %s", worker.worker_id, error, exc_info=exc)
        if requested:
            worker.next_start = now if worker.next_start <= now else worker.next_start
            self._wake.set()
            return
        if ran >= HEALTHY_RUN_S:
            worker.failures = 0
        worker.failures += 1
        worker.restarts += 1
        delay = min(BACKOFF_MAX_S, BACKOFF_INITIAL_S * 2 ** (worker.failures - 1))
        worker.next_start = now + delay
        worker.last_error = error or f"ended after {ran:.0f} s"
        log.warning("hosting: worker %s ended (%s); starting it again in %.0f s", worker.worker_id,
                    worker.last_error, delay)

    def _request_stop(self, worker: WorkerState, why: str, *, restart_now: bool = False) -> None:
        if worker.stop is None or worker.stopping:
            return
        log.info("hosting: stopping worker %s (%s)", worker.worker_id, why)
        worker.stopping = True
        if restart_now:
            worker.next_start = self.opts.clock()
        worker.stop.set()

    async def stop_worker(self, worker_id: str, *, disable: bool = False) -> bool:
        """Stop one worker (and, with ``disable``, keep it stopped: ``enabled: false``)."""
        worker = self._workers.get(worker_id)
        if worker is None:
            return False
        if disable and worker.spec is not None:
            from dataclasses import replace

            from app.tags.runtime.connect.workerdir import write_worker

            worker.spec = replace(worker.spec, enabled=False)
            await asyncio.to_thread(write_worker, worker.directory, worker.spec)
        if worker.running:
            self._request_stop(worker, "asked to")
            await self._wait_ended([worker])
        return True

    def add_worker(self, worker_id: str) -> None:
        """A worker directory was just written (a connect, a migration): look at it now."""
        worker = self._workers.get(worker_id)
        if worker is not None:
            worker.next_start = min(worker.next_start, self.opts.clock())
            worker.failures = 0
        self._wake.set()

    async def stop_all(self) -> None:
        running = [w for w in self._workers.values() if w.running]
        for worker in running:
            self._request_stop(worker, "the hardware host is stopping")
        await self._wait_ended(running)

    async def _wait_ended(self, workers: list[WorkerState]) -> None:
        tasks = [w.task for w in workers if w.task is not None and not w.task.done()]
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=STOP_GRACE_S)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=5.0)

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        """A snapshot (call it on the supervisor's loop)."""
        now = self.opts.clock()
        return {"workers": [w.as_json(now) for w in sorted(self._workers.values(), key=lambda w: w.worker_id)],
                "ports": [p.as_json() for p in sorted(self._ports.values(), key=lambda p: p.info.device)],
                "scans": self.ticks, "scan_error": self.last_scan_error, "paused": self.paused}

    def worker(self, worker_id: str) -> WorkerState | None:
        return self._workers.get(worker_id)


async def _first(*aws: Any) -> None:
    tasks = [asyncio.ensure_future(a) for a in aws]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()


__all__ = ["BACKOFF_INITIAL_S", "BACKOFF_MAX_S", "GatewayBusy", "PortState", "SCAN_INTERVAL_S", "Supervisor",
           "SupervisorOptions", "WorkerState"]
