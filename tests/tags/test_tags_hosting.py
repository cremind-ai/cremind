"""The backend's hardware host (app/tags/hosting): the supervisor that maps
gateways on USB ports to their workers, the host thread around it, and the
font pack it draws with — the pinned one, an older one only until the
components are prepared again, which then removes what nothing uses.

Ports, probes and workers are fakes here; the real worker against the
simulator and Cremind's own services is exercised end to end elsewhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("cbor2")

from sqlalchemy import text  # noqa: E402

from app.tags.hosting.paths import RuntimePaths, host_identity  # noqa: E402
from app.tags.hosting.supervisor import GatewayBusy, Supervisor, SupervisorOptions  # noqa: E402
from app.tags.runtime.connect.probe import GatewayIdentity, ProbeResult  # noqa: E402
from app.tags.runtime.connect.usb import PortInfo  # noqa: E402
from app.tags.runtime.connect.workerdir import WorkerSpec, load_worker, write_worker  # noqa: E402

GW_A = "a1" * 16
GW_B = "b2" * 16
# Font pack ids, in directory-name order: an older pack, the pinned one, one a bridge shows, one held open.
OLD, PIN, SHOWN, HELD = "a1" * 8, "b2" * 8, "c3" * 8, "d4" * 8


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


# ── the font pack a host draws with ─────────────────────────────────────────


def fake_pack(assets: Path, pack_id: str) -> Path:
    """An installed pack as the loaders see it: its layout (nothing here reads the fonts), read-only like a real
    installed copy."""
    directory = assets / "fonts" / pack_id
    (directory / "cache" / "noto").mkdir(parents=True)
    (directory / "fontpack.ctfp").write_bytes(b"pack " + pack_id.encode())
    (directory / "fontpack.json").write_text(json.dumps({"pack_id": pack_id, "faces": []}), encoding="utf-8")
    font = directory / "cache" / "noto" / "NotoSans-Regular.ttf"
    font.write_bytes(b"font")
    os.chmod(font, stat.S_IREAD)
    return directory


def installed(assets: Path) -> list[str]:
    return sorted(p.name for p in (assets / "fonts").iterdir())


def pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pack_id: str = PIN) -> None:
    """This Cremind's release pins ``pack_id``."""
    from app.tags.hosting import fonts

    lock = tmp_path / "bundle.json"
    lock.write_text(json.dumps({"schema": "cremind/tag-font-bundle@1", "pack_id": pack_id}), encoding="utf-8")
    monkeypatch.setattr(fonts, "LOCK_FILE", lock)


class Log:
    """The host's logger, recording ``(level, message)``."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def __getattr__(self, level: str) -> Any:
        return lambda message, *args, **kwargs: self.lines.append((level, str(message)))


def test_the_pin_names_the_pack_this_cremind_draws_with(tmp_path):
    from app.tags.hosting import fonts
    from app.tags.runtime import resources

    lock = tmp_path / "bundle.json"
    assert resources.pinned_pack_id(lock) is None, "a checkout without a pin"
    lock.write_text("{not json", encoding="utf-8")
    assert resources.pinned_pack_id(lock) is None
    lock.write_text(json.dumps({"pack_id": PIN, "url": "https://example.org/x.tar.gz"}), encoding="utf-8")
    assert resources.pinned_pack_id(lock) == PIN
    # The release's own pin: the pack a worker prefers is the one the components install.
    assert resources.pinned_pack_id() == fonts.pinned_pack() == (fonts.bundle_lock() or {}).get("pack_id")


def test_the_pinned_pack_is_found_first_then_one_the_bridges_show_then_the_first_installed(tmp_path):
    from app.tags.runtime.resources import find_font_assets

    assets, other = tmp_path / "assets", tmp_path / "other"
    for pack in (SHOWN, OLD):
        fake_pack(assets, pack)

    def found(*args: Any, **kwargs: Any) -> str | None:
        hit = find_font_assets(*args, roots=[assets], **kwargs)
        return hit.pack_id if hit is not None else None

    # Never in the order packs were installed: their directories keep the archive's times.
    assert found() == OLD and found(prefer=PIN) == OLD
    assert found(prefer=PIN, also_prefer=[HELD, SHOWN]) == SHOWN
    fake_pack(assets, PIN)
    assert found(prefer=PIN, also_prefer=[SHOWN]) == PIN
    assert found(OLD, prefer=PIN) == OLD and found(HELD, prefer=PIN) is None, "an id asked for is exact"
    # Across asset roots: the pinned pack wherever it is, else the first root's first pack.
    fake_pack(other, HELD)
    assert find_font_assets(roots=[other, assets], prefer=PIN).pack_id == PIN
    assert find_font_assets(roots=[other, assets], prefer="e5" * 8).pack_id == HELD


def test_an_older_pack_keeps_the_host_ready_with_a_font_update(tmp_path, monkeypatch):
    from app.tags.hosting import components
    from app.tags.hosting.host import _components_reason

    monkeypatch.setattr(components, "platform_support", lambda: components.Component("platform", "ready", "Test"))
    monkeypatch.setattr(components, "packages", lambda: components.Component("packages", "ready"))
    assets = tmp_path / "assets"

    def check(*args: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        doc = components.readiness(assets, *args)
        return doc, next(c for c in doc["components"] if c["key"] == "fonts")

    doc, fonts = check(PIN)
    assert fonts["state"] == "missing" and doc["state"] == "partial" and doc["fonts_update"] is False
    fake_pack(assets, OLD)
    doc, fonts = check(PIN)
    # Not "partial" (screens waiting): the older pack still draws every screen.
    assert fonts["state"] == "outdated" and doc["state"] == "ready" and doc["fonts_update"] is True
    assert f"Tag screens use font pack {OLD}; this Cremind draws with {PIN}" in fonts["detail"]
    assert "screens keep working" in fonts["detail"] and components.can_host(doc)
    assert check()[1]["state"] == "ready", "without a pin, any pack is the right one"
    fake_pack(assets, PIN)
    doc, fonts = check(PIN)
    assert fonts["state"] == "ready" and doc["fonts_update"] is False and fonts["detail"] == f"{OLD}, {PIN}"
    # Installed, but the running host still draws with the older pack: a restart is due.
    doc, fonts = check(PIN, OLD)
    assert fonts["state"] == "outdated" and doc["fonts_update"] is True and f"font pack {OLD};" in fonts["detail"]
    assert check(PIN, PIN)[1]["state"] == "ready"
    # What keeps a host from starting never names an outdated pack.
    monkeypatch.setattr(components, "packages", lambda: components.Component("packages", "missing", "No ICU."))
    doc = components.readiness(assets, PIN, OLD)
    assert doc["state"] == "missing" and _components_reason(doc) == "No ICU."


def test_the_host_draws_with_the_pinned_pack_and_says_when_it_cannot(tmp_path, monkeypatch):
    from app.tags.hosting import host as host_mod
    from app.tags.runtime.fonts import fontset

    pin(tmp_path, monkeypatch)
    log = Log()
    monkeypatch.setattr(host_mod, "logger", log)
    monkeypatch.setattr(fontset.FontSet, "load", classmethod(lambda cls, pack, cache=None: ("fonts", pack.parent.name)))
    paths = RuntimePaths(tmp_path / ".tag-runtime").ensure()
    for pack in (OLD, SHOWN):
        fake_pack(paths.assets_dir, pack)
    host = host_mod.HardwareHost(paths, check_components=False)
    assert host._load_fonts() == (("fonts", OLD), OLD), "until the pinned pack is installed: the first one"
    assert host._load_fonts([SHOWN])[1] == SHOWN, "...or the one the bridges show"
    assert ("warning", f"[tags] font pack {SHOWN} is loaded; this Cremind pins {PIN} — prepare the gateway "
                       "computer to update") in log.lines
    fake_pack(paths.assets_dir, PIN)
    log.lines.clear()
    assert host._load_fonts([SHOWN]) == (("fonts", PIN), PIN)
    assert not [line for line in log.lines if line[0] == "warning"]


def test_the_server_host_asks_which_packs_the_bridges_show_only_when_that_matters(tmp_path, monkeypatch):
    import app.tags.hosts as hosts_mod
    from app.tags.hosting.host import HardwareHost

    pin(tmp_path, monkeypatch)
    asked: list[str] = []

    async def bridge_font_packs(host_id: str) -> list[str]:
        asked.append(host_id)
        return [SHOWN]

    monkeypatch.setattr(hosts_mod, "bridge_font_packs", bridge_font_packs)
    paths = RuntimePaths(tmp_path / ".tag-runtime").ensure()
    host = HardwareHost(paths, check_components=False)
    host.host_id = "h-srv"
    fake_pack(paths.assets_dir, OLD)
    assert asyncio.run(host._bridge_packs()) == [] and asked == [], "one pack: nothing to choose"
    fake_pack(paths.assets_dir, SHOWN)
    assert asyncio.run(host._bridge_packs()) == [SHOWN] and asked == ["h-srv"]
    fake_pack(paths.assets_dir, PIN)
    assert asyncio.run(host._bridge_packs()) == [] and asked == ["h-srv"], "the pinned pack is there"
    (paths.assets_dir / "fonts" / PIN).rename(paths.assets_dir / "fonts" / ".gone")
    host.remote = SimpleNamespace()
    assert asyncio.run(host._bridge_packs()) == [] and asked == ["h-srv"], "a desktop computer has no records to ask"


def test_pruning_keeps_the_packs_in_use_and_leaves_one_it_cannot_move_whole(tmp_path, monkeypatch):
    from app.tags.hosting import fonts
    from app.tags.runtime.resources import load_font_assets

    assets = tmp_path / "assets"
    for pack in (OLD, PIN, SHOWN, HELD):
        fake_pack(assets, pack)
    real_rename = os.rename

    def rename(src: Any, dst: Any) -> None:
        if Path(src).name == HELD:  # Windows: a process still has one of its files open
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        real_rename(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    lines: list[str] = []
    assert fonts.remove_other_packs(assets, {PIN, SHOWN}, lines.append) == [OLD]
    assert installed(assets) == [PIN, SHOWN, HELD], "nothing left behind in fonts/ (.tmp is gone too)"
    assert load_font_assets(assets / "fonts" / HELD).pack_id == HELD, "the pack that could not move is whole"
    assert any(f"Removed font pack {OLD}" in line for line in lines)
    assert any(HELD in line and "removed next time" in line for line in lines)
    monkeypatch.setattr(os, "rename", real_rename)
    assert fonts.remove_other_packs(assets, PIN) == [SHOWN, HELD]
    assert installed(assets) == [PIN]


# ── preparing the components switches to the pinned pack ───────────────────


def bridge_behind(env: Any, host_id: str, pack: str, hw_id: str = "B1") -> None:
    """A gateway driven from ``host_id``, with a bridge whose active font pack is ``pack``."""
    from app.tags import service

    companion, _cred, _secret = asyncio.run(service.register_companion("Desk PC", created_by="admin"))
    asyncio.run(env.store.upsert_inventory(companion["id"], {
        "gateways": [{"hw_id": f"gw-{hw_id}", "fw": "0.2.0", "board": 1, "port": "COM7"}],
        "bridges": [{"hw_id": hw_id, "addr": 2, "fw": "0.2.0", "fontpack_id": pack}], "tags": []}))
    with env.engine.begin() as c:
        c.execute(text("UPDATE tag_companions SET host_id = :h WHERE id = :i"), {"h": host_id, "i": companion["id"]})


@pytest.fixture
def preparing(tagenv, tmp_path, monkeypatch):
    """The admin prepares the server's computer: the release pins PIN (installed already), the packages are
    there, and the server's host is a fake that records stops and starts and draws with the pack a real one
    would load. ``prepare()`` runs one preparation and answers its operation."""
    from app.tags import hosts
    from app.tags.hosting import fonts
    from app.tags.hosting import host as host_mod
    from app.tags.hosting import prepare as prepare_mod
    from app.tags.runtime.resources import find_font_assets

    pin(tmp_path, monkeypatch)
    monkeypatch.setattr(prepare_mod, "_install_packages", lambda say: (True, "already installed"))
    monkeypatch.setattr(prepare_mod, "run_in_background", lambda op_id: None)
    monkeypatch.setattr(fonts, "ensure_installed",
                        lambda assets_dir, say: fonts.Installed(PIN, False, f"Font pack {PIN} is installed."))
    host = SimpleNamespace(paths=RuntimePaths(tmp_path / ".tag-runtime").ensure(), host_id="h-srv", running=True,
                           fonts_pack=OLD, events=[], loads=True)

    async def stop_hosting(why: str = "stopping") -> None:
        host.events.append(("stop", why))
        host.running = False

    async def start_hosting() -> dict[str, Any]:
        if not host.running:
            host.events.append(("start",))
            assets = find_font_assets(roots=[host.paths.assets_dir], prefer=PIN) if host.loads else None
            host.running, host.fonts_pack = True, assets.pack_id if assets is not None else None
        return {"state": "running", "reason": None}

    monkeypatch.setattr(host_mod, "get_host", lambda: host)
    monkeypatch.setattr(host_mod, "start_hosting", start_hosting)
    monkeypatch.setattr(host_mod, "stop_hosting", stop_hosting)
    asyncio.run(hosts.host_hello(hosts.HostPrincipal("h-srv", hosts.SERVER), {"name": "Office PC", "status": {}}))

    def prepare() -> dict[str, Any]:
        op_id = asyncio.run(hosts.start_prepare("admin", "h-srv"))["id"]
        asyncio.run(prepare_mod.prepare(op_id))
        return asyncio.run(hosts.get_operation("admin", op_id))

    return SimpleNamespace(env=tagenv, host=host, prepare=prepare)


def test_prepare_switches_a_host_still_on_an_older_pack_and_removes_what_nothing_uses(preparing):
    host = preparing.host
    for pack in (OLD, PIN, SHOWN):
        fake_pack(host.paths.assets_dir, pack)
    bridge_behind(preparing.env, "h-srv", SHOWN)
    bridge_behind(preparing.env, "h-other", OLD, hw_id="B2")  # another computer's bridge keeps nothing here
    op = preparing.prepare()
    assert op["state"] == "succeeded", op
    assert host.events == [("stop", f"switching to font pack {PIN}"), ("start",)] and host.fonts_pack == PIN
    assert installed(host.paths.assets_dir) == [PIN, SHOWN], "the pack a bridge here still shows stays"
    assert f"Switching to font pack {PIN}…" in op["log"]
    assert any(f"Removed font pack {OLD}" in line for line in op["log"])
    fonts = next(c for c in op["readiness"]["components"] if c["key"] == "fonts")
    assert fonts["state"] == "ready" and op["readiness"]["fonts_update"] is False
    # Prepared again: nothing to switch, nothing more to remove.
    op = preparing.prepare()
    assert op["state"] == "succeeded" and len(host.events) == 2 and installed(host.paths.assets_dir) == [PIN, SHOWN]


def test_a_pack_that_cannot_be_removed_never_fails_the_preparation(preparing, monkeypatch):
    import app.tags.hosts as hosts_mod

    host = preparing.host
    host.fonts_pack = PIN  # drawing with the pinned pack already: no restart
    for pack in (OLD, PIN, HELD):
        fake_pack(host.paths.assets_dir, pack)
    real_rename = os.rename

    def rename(src: Any, dst: Any) -> None:
        if Path(src).name == HELD:
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        real_rename(src, dst)

    monkeypatch.setattr(os, "rename", rename)
    op = preparing.prepare()
    assert op["state"] == "succeeded" and host.events == []
    assert installed(host.paths.assets_dir) == [PIN, HELD]
    assert any(HELD in line and "removed next time" in line for line in op["log"])

    # Which packs the bridges show is not known: nothing is removed.
    async def unknown(host_id: str) -> list[str]:
        raise RuntimeError("the database is gone")

    known = hosts_mod.bridge_font_packs
    monkeypatch.setattr(hosts_mod, "bridge_font_packs", unknown)
    monkeypatch.setattr(os, "rename", real_rename)
    assert preparing.prepare()["state"] == "succeeded" and installed(host.paths.assets_dir) == [PIN, HELD]

    # A host that could not load the pinned pack keeps every other one.
    monkeypatch.setattr(hosts_mod, "bridge_font_packs", known)
    host.running, host.loads = False, False
    assert preparing.prepare()["state"] == "succeeded" and host.fonts_pack is None
    assert installed(host.paths.assets_dir) == [PIN, HELD]


def test_a_font_update_reaches_the_gateway_page(tagenv, tmp_path, monkeypatch):
    from app.tags import hosts
    from app.tags.hosting.host import HardwareHost

    pin(tmp_path, monkeypatch)
    paths = RuntimePaths(tmp_path / ".tag-runtime").ensure()
    fake_pack(paths.assets_dir, OLD)
    host = HardwareHost(paths, check_components=False)
    host.host_id, host.state, host.fonts_pack = "h-srv", "running", OLD
    asyncio.run(hosts.host_hello(hosts.HostPrincipal("h-srv", hosts.SERVER), {
        "name": "Office PC", "capabilities": host.facts().capabilities(), "status": {"state": "running"}}))
    view = next(h for h in asyncio.run(hosts.list_hosts("admin"))["hosts"] if h["id"] == "h-srv")
    assert view["state"] == "running" and view["readiness"]["fonts_update"] is True
    assert next(c for c in view["readiness"]["components"] if c["key"] == "fonts")["state"] == "outdated"


def test_the_bridges_a_host_drives_name_their_packs_most_common_first(tagenv):
    from app.tags.hosts import bridge_font_packs

    bridge_behind(tagenv, "h-srv", SHOWN, hw_id="B1")
    bridge_behind(tagenv, "h-srv", OLD, hw_id="B2")
    bridge_behind(tagenv, "h-srv", SHOWN, hw_id="B3")
    bridge_behind(tagenv, "h-other", HELD, hw_id="B4")
    assert asyncio.run(bridge_font_packs("h-srv")) == [SHOWN, OLD]
    assert asyncio.run(bridge_font_packs("h-none")) == []
