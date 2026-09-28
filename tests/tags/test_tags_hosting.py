"""The backend's hardware host (app/tags/hosting): the supervisor that maps
gateways on USB ports to their workers, and the host thread around it.

Ports, probes and workers are fakes here; the real worker against the
simulator and Cremind's own services is exercised end to end elsewhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("cbor2")

from app.tags.hosting.paths import RuntimePaths, host_identity  # noqa: E402
from app.tags.hosting.supervisor import GatewayBusy, Supervisor, SupervisorOptions  # noqa: E402
from app.tags.runtime.connect.probe import GatewayIdentity, ProbeResult  # noqa: E402
from app.tags.runtime.connect.usb import PortInfo  # noqa: E402
from app.tags.runtime.connect.workerdir import WorkerSpec, load_worker, write_worker  # noqa: E402

GW_A = "a1" * 16
GW_B = "b2" * 16


def port(device: str, serial: str = "S1") -> PortInfo:
    return PortInfo(device, 0x1209, 0x0002, serial, "Cremind Tag gateway", "gateway")


def identity(device_id: str) -> GatewayIdentity:
    return GatewayIdentity(device_id=bytes.fromhex(device_id), ik=bytes(32), role=1, proto=2, fw="0.2.0", build="t",
                           board=1, owner_state=1, gen=1, authority_id=None, challenge=bytes(16))


class World:
    """What the supervisor can see: ports, and the gateway answering on each."""

    def __init__(self) -> None:
        self.ports: dict[str, tuple[PortInfo, str | None]] = {}
        self.probes: list[str] = []

    def plug(self, device: str, gateway: str | None, serial: str = "S1") -> None:
        self.ports[device] = (port(device, serial), gateway)

    def unplug(self, device: str) -> None:
        self.ports.pop(device, None)

    def list_ports(self) -> list[PortInfo]:
        return [info for info, _ in self.ports.values()]

    def probe(self, device: str) -> ProbeResult:
        self.probes.append(device)
        entry = self.ports.get(device)
        if entry is None:
            return ProbeResult(device, reason="gone")
        if entry[1] is None:
            return ProbeResult(device, reason="no_answer", detail="silent")
        return ProbeResult(device, identity=identity(entry[1]))


class Workers:
    """A fake worker: runs until asked to stop, or until the test makes it fail."""

    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []
        self.fail: dict[str, BaseException] = {}

    def factory(self, worker: Any, port_name: str) -> tuple[Any, Any]:
        self.started.append((worker.worker_id, port_name))
        return object(), object()

    async def serve(self, svc: Any, agent: Any, *, stop: asyncio.Event) -> None:
        worker_id = self.started[-1][0]
        while not stop.is_set():
            if worker_id in self.fail:
                raise self.fail.pop(worker_id)
            await asyncio.sleep(0.01)


def make_dir(paths: RuntimePaths, worker_id: str, gateway: str, *, enabled: bool = True) -> Path:
    directory = paths.worker_dir(worker_id)
    directory.mkdir(parents=True, exist_ok=True)
    write_worker(directory, WorkerSpec(worker_id, "http://localhost:1515", "p1", worker_id, gateway, enabled,
                                       extra={"profile_id": "pid1", "generation": 0}))
    return directory


def supervisor(tmp_path: Path, world: World, workers: Workers, **options: Any) -> Supervisor:
    paths = RuntimePaths(tmp_path / ".tag-runtime").ensure()
    opts = SupervisorOptions(list_ports=world.list_ports, probe=world.probe, scan_interval_s=0.05,
                             serve=workers.serve, **options)
    return Supervisor(paths, workers.factory, opts)


async def until(predicate: Any, timeout: float = 5.0, what: str = "") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


@contextlib.asynccontextmanager
async def running(sup: Supervisor):
    stop = asyncio.Event()
    task = asyncio.create_task(sup.run(stop))
    try:
        yield
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


def test_a_worker_starts_when_its_gateway_is_plugged_in_and_follows_it_to_another_port(tmp_path):
    world, workers = World(), Workers()
    sup = supervisor(tmp_path, world, workers)
    make_dir(sup.paths, "w1", GW_A)

    async def go():
        async with running(sup):
            await asyncio.sleep(0.2)
            assert workers.started == []  # nothing plugged in yet
            world.plug("COM7", GW_A)
            await until(lambda: workers.started == [("w1", "COM7")], what="the worker on COM7")
            assert sup.status()["workers"][0]["state"] == "running"
            # Unplugged and plugged back under another name: the worker moves with its gateway.
            world.unplug("COM7")
            world.plug("COM9", GW_A, serial="S1")
            await until(lambda: ("w1", "COM9") in workers.started, what="the worker on COM9")
            assert sup.worker("w1").port == "COM9"

    asyncio.run(go())


def test_a_failing_worker_is_restarted_with_back_off(tmp_path, monkeypatch):
    import app.tags.hosting.supervisor as supervisor_mod

    monkeypatch.setattr(supervisor_mod, "BACKOFF_INITIAL_S", 0.05)
    world, workers = World(), Workers()
    sup = supervisor(tmp_path, world, workers)
    make_dir(sup.paths, "w1", GW_A)
    world.plug("COM7", GW_A)

    async def go():
        async with running(sup):
            await until(lambda: len(workers.started) == 1, what="the first start")
            workers.fail["w1"] = RuntimeError("the gateway link broke")
            await until(lambda: len(workers.started) == 2, what="a restart")
            state = sup.worker("w1")
            assert state.restarts == 1 and "the gateway link broke" in (state.last_error or "")

    asyncio.run(go())


def test_disabled_and_duplicate_workers_do_not_run(tmp_path):
    world, workers = World(), Workers()
    sup = supervisor(tmp_path, world, workers)
    make_dir(sup.paths, "off", GW_B, enabled=False)
    make_dir(sup.paths, "w1", GW_A)
    make_dir(sup.paths, "w2", GW_A)  # another directory for the same gateway
    world.plug("COM7", GW_A)
    world.plug("COM8", GW_B, serial="S2")

    async def go():
        async with running(sup):
            await until(lambda: workers.started, what="a worker")
            await asyncio.sleep(0.3)
            assert workers.started == [("w1", "COM7")]
            states = {w["worker_id"]: w["state"] for w in sup.status()["workers"]}
            assert states == {"off": "disabled", "w1": "running", "w2": "conflict"}

    asyncio.run(go())


def test_a_gateway_another_program_holds_is_not_driven(tmp_path, monkeypatch):
    import app.tags.hosting.supervisor as supervisor_mod
    from app.tags.runtime.connect.instance import InstanceLock

    monkeypatch.setattr(supervisor_mod, "BACKOFF_INITIAL_S", 0.05)
    world, workers = World(), Workers()
    sup = supervisor(tmp_path, world, workers)
    make_dir(sup.paths, "w1", GW_A)
    world.plug("COM7", GW_A)
    other = InstanceLock(sup.paths.gateway_lock(GW_A))
    assert other.acquire()  # e.g. a Cremind Connect worker that still runs

    async def go():
        async with running(sup):
            await until(lambda: sup.worker("w1") is not None and sup.worker("w1").restarts >= 1, what="a refusal")
            assert workers.started == []
            assert GatewayBusy.__name__ in (sup.worker("w1").last_error or "")
            other.release()
            await until(lambda: workers.started == [("w1", "COM7")], what="the start once released")

    asyncio.run(go())


def test_an_update_in_progress_holds_every_worker(tmp_path):
    world, workers = World(), Workers()
    holding: list[str | None] = [None]
    sup = supervisor(tmp_path, world, workers, pause_reason=lambda: holding[0])
    make_dir(sup.paths, "w1", GW_A)
    world.plug("COM7", GW_A)

    async def go():
        async with running(sup):
            await until(lambda: sup.worker("w1") is not None and sup.worker("w1").running, what="the worker")
            holding[0] = "an update is being installed"
            await until(lambda: not sup.worker("w1").running, what="the worker to stop")
            assert sup.status()["paused"] == "an update is being installed"
            holding[0] = None
            await until(lambda: sup.worker("w1").running, what="the worker again")

    asyncio.run(go())


def test_a_scan_reports_free_gateways_and_the_ports_in_use(tmp_path):
    world, workers = World(), Workers()
    sup = supervisor(tmp_path, world, workers)
    make_dir(sup.paths, "w1", GW_A)
    world.plug("COM7", GW_A)
    world.plug("COM8", GW_B, serial="S2")
    world.plug("COM9", None, serial="S3")

    async def go():
        async with running(sup):
            await until(lambda: workers.started, what="the worker")
            found = {p["device"]: p for p in await sup.scan()}
            assert found["COM7"]["in_use"] and found["COM7"]["gateway_device_id"] == GW_A
            assert not found["COM8"]["in_use"] and found["COM8"]["identity"]["device_id"] == GW_B
            assert "challenge" not in found["COM8"]["identity"]
            assert found["COM9"]["identity"] is None and found["COM9"]["reason"] == "no_answer"

    asyncio.run(go())


def test_the_host_identity_is_created_once(tmp_path):
    paths = RuntimePaths(tmp_path / ".tag-runtime")
    first = host_identity(paths)
    assert host_identity(paths) == first
    assert json.loads(paths.host_file.read_text(encoding="utf-8"))["host_id"] == first["host_id"]


def test_the_host_runs_its_own_thread_and_one_process_holds_the_gateways(tmp_path):
    from app.tags.hosting.host import HardwareHost

    world, workers = World(), Workers()
    paths = RuntimePaths(tmp_path / ".tag-runtime")
    make_dir(paths.ensure(), "w1", GW_A)
    world.plug("COM7", GW_A)
    options = SupervisorOptions(list_ports=world.list_ports, probe=world.probe, scan_interval_s=0.05,
                                serve=workers.serve, pause_reason=lambda: None)
    host = HardwareHost(paths, options=options, check_components=False)
    host._build_worker = workers.factory  # type: ignore[method-assign]  # no real daemon here

    async def go():
        assert await asyncio.to_thread(host.start, asyncio.get_running_loop())
        try:
            second = HardwareHost(paths, options=options, check_components=False)
            assert not second.start(asyncio.get_running_loop())
            assert second.state == "busy_elsewhere"
            await until(lambda: workers.started == [("w1", "COM7")], what="the worker")
            status = await host.status()
            assert status["state"] == "running" and status["workers"][0]["state"] == "running"
            assert status["host_id"] == json.loads(paths.host_file.read_text(encoding="utf-8"))["host_id"]
        finally:
            await asyncio.to_thread(host.stop, "test over")
        assert host.state == "stopped"
        assert json.loads(paths.host_file.read_text(encoding="utf-8")).keys() == {"host_id", "created_at"}
        third = HardwareHost(paths, options=options, check_components=False)
        third._build_worker = workers.factory  # type: ignore[method-assign]
        assert await asyncio.to_thread(third.start, asyncio.get_running_loop())  # the lock was released
        assert third.host_id == host.host_id, "the same computer after a restart (its grants, its connections)"
        await asyncio.to_thread(third.stop, "test over")

    asyncio.run(go())
