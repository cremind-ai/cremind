"""A Cremind Connect worker end to end against the simulator (docs/connect-setup.md §8): the daemon on a
secure link pinned to its v2 gateway, and the agent running Cremind's operations — claim the gateway,
find and pair a bridge (static OOB, tunnel, PAIR with both proofs), find and pair a tag (assign and clear
under ``K_epoch`` v2), remove the tag (two-stage release), release the gateway (the worker retires);
recover everything on a replacement computer; pair a released tag again above the epoch it kept."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.tags.runtime.connect.setup_flow import CONTROLLER_SCHEMA, write_private
from app.tags.runtime.connect.workerdir import WorkerSpec, load_worker, write_worker
from app.tags.runtime.protocol.ids import GATEWAY_ADDR, NodeRole, OwnerState
from app.tags.runtime.secrets import FileBackend, SecretStore
from app.tags.runtime.secure import identity
from app.tags.runtime.secure.codes import SetupPayload, parse_code
from app.tags.runtime.sim import BridgeSpec, SimConfig, Simulator, TagSpec

pytestmark = pytest.mark.timeout(300)


def _load(name: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


FakeV2Cremind = _load("fake_v2").FakeV2Cremind
SEED = 131


REPO = Path(__file__).resolve().parents[4]


def config(fontpack: bytes | None = None) -> SimConfig:
    return SimConfig(seed=SEED, time_scale=200, protocol=2, fontpack=fontpack, bridges=[BridgeSpec(provisioned=False)],
                     tags=[TagSpec.generate(SEED, 0, protocol=2)])


@pytest.fixture(scope="module")
def dev_fonts() -> tuple[Any, bytes]:
    """The dev font pack (the worker composes; the simulated bridge renders): removing a tag shows its new
    setup code, which needs fonts. Skipped where the pack was not built (CI without the font cache)."""
    from app.tags.runtime.fonts.fontset import FontSet

    pack, cache = REPO / "fonts" / "out" / "dev" / "fontpack.ctfp", REPO / "fonts" / "cache"
    if not pack.is_file() or not cache.is_dir():
        pytest.skip(f"{pack} or {cache} is missing (run `cremind tags tools fonts fetch` and `fonts build --profile dev`)")
    return FontSet.load(pack, cache), pack.read_bytes()


def make_worker(directory: Path, fake: Any, gateway_id: bytes, gateway_ik: bytes, *,
                bind_gateway: bool = True) -> bytes:
    """A worker directory as the setup window leaves it; the fake knows its controller key."""
    directory.mkdir(parents=True, exist_ok=True)
    priv, pub = identity.x25519_generate()
    write_private(directory / "controller.key",
                  json.dumps({"schema": CONTROLLER_SCHEMA, "private_key": priv.hex()}) + "\n")
    hardware = fake.add_credential("hardware")
    content = fake.add_credential("content", profile="anna")
    store = SecretStore(FileBackend(directory / "secrets.json"))
    store.set_credential("hardware", hardware.value)
    store.set_credential("content", content.value)
    write_worker(directory, WorkerSpec(directory.name, fake.url, "anna", fake.companion_id, gateway_id.hex(), True,
                                       extra={"profile_id": fake.profile_id,
                                              "authority_pub": fake.authority_pub.hex(),
                                              "gateway_ik": gateway_ik.hex()}))
    fake.controller_pub = pub
    if bind_gateway:
        fake.add_binding(gateway_id, "gateway", gateway_ik, 0)
    return priv


async def until(predicate: Any, timeout: float = 60.0, what: str = "") -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {what or predicate}")
        await asyncio.sleep(0.05)


async def run_op(fake: Any, kind: str, args: dict[str, Any], *, secret: bytes | None = None,
                 timeout: float = 120.0) -> dict[str, Any]:
    op_id = fake.queue_operation(kind, args, setup_secret=secret)
    await until(lambda: fake.op(op_id)["state"] in ("succeeded", "failed", "cancelled"), timeout, kind)
    op = fake.op(op_id)
    assert op["state"] == "succeeded", (kind, op)
    return op


async def discover(fake: Any, role: str, label: SetupPayload, bridges: list[str] | None = None) -> dict[str, Any]:
    op_id = fake.queue_operation("discovery", {"role": role, "short_id": label.short_id, "duration_s": 30,
                                               "bridges": bridges or []})
    await until(lambda: fake.op(op_id)["candidates"], 60, f"{role} discovery")
    fake.op(op_id)["state"] = "succeeded"  # a pairing starts from it (start_pairing closes the search)
    return dict(fake.op(op_id)["candidates"][0])


def label(sim: Simulator, role: str) -> SetupPayload:
    code = next(c["code"] for c in sim.setup_codes() if c["role"] == role)
    return parse_code(code, role=NodeRole.BRIDGE if role == "bridge" else NodeRole.TAG)


@dataclass
class Running:
    svc: Any
    agent: Any
    task: asyncio.Task[None]
    stop: asyncio.Event


@contextlib.asynccontextmanager
async def worker(directory: Path, sim: Simulator, paths: Any, fake: Any, fonts: Any = None
                 ) -> AsyncIterator[Running]:
    from app.tags.runtime.connect.worker import build, serve

    svc, agent = build(directory, sim.gateway_url, paths, transport=fake.transport, fonts=fonts)
    agent.discover_every_s = 0.2  # device time runs 200x faster here: a listening window is over in 0.1 s
    stop = asyncio.Event()
    task = asyncio.create_task(serve(svc, agent, stop=stop))
    try:
        yield Running(svc, agent, task, stop)
    finally:
        stop.set()
        if not task.done():
            await asyncio.wait_for(task, 30)


async def claim_and_pair(sim: Simulator, fake: Any) -> tuple[str, str, int]:
    """Claim the gateway, pair the bridge and the tag; returns (bridge device id, tag device id, tag id)."""
    gw = sim.gateway.secure
    assert gw is not None
    await run_op(fake, "claim_gateway", {"device_id": gw.device_id.hex(), "ik": gw.keys.ik_pub.hex(), "gen": 0,
                                         "mode": "claim"})
    bridge_label = label(sim, "bridge")
    cand = await discover(fake, "bridge", bridge_label)
    bridge_id = cand["uuid"]
    await run_op(fake, "pair_bridge", {"role": "bridge", "short_id": bridge_label.short_id, "uuid": bridge_id,
                                       "name": "Hall"}, secret=bridge_label.secret)
    tag_label = label(sim, "tag")
    cand = await discover(fake, "tag", tag_label, [f"br-{bridge_id}"])
    await run_op(fake, "pair_tag", {"role": "tag", "short_id": tag_label.short_id, "tag_id": tag_label.short_id,
                                    "bridge_hw_id": cand["bridge_hw_id"], "bridge_id": "b1", "name": "Desk"},
                 secret=tag_label.secret)
    tag = sim.tag(tag_label.short_id)
    assert tag.secure is not None
    return bridge_id, tag.secure.device_id.hex(), tag_label.short_id


def expected_setup_screen(sim: Simulator, tag_id: int, fonts: Any, payload: SetupPayload) -> bytes:
    """The digest of the planes the bridge renders for the setup-code screen of ``payload``."""
    import hashlib

    from app.tags.runtime.compose.api import TagPanel
    from app.tags.runtime.compose.screen import compose_setup_code
    from app.tags.runtime.render.reference import Panel, render_frame

    spec = sim.tag(tag_id).spec
    screen = compose_setup_code(TagPanel(tag_id, spec.width, spec.height, spec.planes, spec.plane_flags, 0, ""),
                                fonts, payload.code(), payload.qr_text())
    bridge_pack = sim.bridge(0).fontpack
    assert bridge_pack is not None
    frame = render_frame(screen.layout, Panel(spec.width, spec.height, spec.planes, spec.plane_flags), bridge_pack)
    return hashlib.sha256(b"".join(frame.planes)).digest()


def test_worker_claims_pairs_and_releases(paths: Any, dev_fonts: tuple[Any, bytes]) -> None:
    fonts, pack = dev_fonts

    async def scenario() -> None:
        async with Simulator(config(pack)) as sim:
            gw = sim.gateway.secure
            assert gw is not None
            fake = FakeV2Cremind()
            directory = paths.worker_dir("w1")
            make_worker(directory, fake, gw.device_id, gw.keys.ik_pub)
            async with worker(directory, sim, paths, fake, fonts) as run:
                bridge_id, tag_device, tag_id = await claim_and_pair(sim, fake)
                # 8.1: the gateway is ours, pinned to this worker's controller key.
                assert gw.record.state == OwnerState.OWNED and gw.record.gen == 1
                assert gw.record.controller == fake.controller_pub
                assert fake.vault_latest(gw.device_id.hex())["stage"] == "committed"
                # 8.2: the bridge holds the mk the vault has.
                bridge = sim.bridge(0).secure
                assert bridge is not None and bridge.record.state == OwnerState.OWNED
                assert fake.bindings[bridge_id]["state"] == "ready" and fake.bindings[bridge_id]["generation"] == 1
                entry = fake.vault_latest(bridge_id)
                assert entry["stage"] == "committed" and bridge.record.op_key == bytes.fromhex(entry["state"]["mk"])
                # 8.3: the tag holds the root the vault has, is assigned and was cleared.
                tag = sim.tag(tag_id)
                assert tag.secure is not None and tag.secure.record.state == OwnerState.OWNED
                assert fake.bindings[tag_device]["state"] == "ready"
                local = await run.svc.db.run(run.svc.db.find_tag, tag_id)
                assert local is not None and local.epoch >= 1 and local.bridge_addr
                assert fake.vault_latest(tag_device)["state"]["root"] == tag.secure.record.op_key.hex()
                # Heartbeats carry the live generations the worker learned.
                await until(lambda: any(d.get("gen") == 1 and d.get("hw_id") == f"br-{bridge_id}"
                                        for hb in fake.heartbeats for d in hb.get("devices", [])), 90, "heartbeat")

                # 8.6: remove the tag: the release is prepared, the tag shows its fresh setup code, then the
                # release is committed and the assignment goes.
                unpair = await run_op(fake, "unpair", {"device_id": tag_device, "role": "tag",
                                                       "hw_id": f"{tag_id:08X}", "generation": 1,
                                                       "epoch": local.epoch})
                assert tag.secure.record.state == OwnerState.RELEASED
                assert await run.svc.db.run(run.svc.db.find_tag, tag_id) is None
                stages = [body.get("stage") for op_id, body in fake.progress_log if op_id == unpair["id"]]
                assert stages.index("showing_code") < len(stages) - 1
                fresh = label(sim, "tag")  # what the released tag pairs with now
                assert tag.displayed_digest == expected_setup_screen(sim, tag_id, fonts, fresh)

                # Remove the gateway: the bridge is released (and locked), then the gateway; the worker retires.
                await run_op(fake, "release_gateway", {
                    "device_id": gw.device_id.hex(), "role": "gateway",
                    "devices": [{"device_id": bridge_id, "role": "bridge", "generation": 1},
                                {"device_id": gw.device_id.hex(), "role": "gateway", "generation": 1}]})
                assert bridge.record.state == OwnerState.RELEASED and bridge.record.locked
                assert gw.record.state == OwnerState.UNOWNED
                await asyncio.wait_for(run.task, 30)
            spec = load_worker(directory)
            assert spec.enabled is False and spec.extra.get("removed") is True
            assert not (directory / "controller.key").exists() and not (directory / "secrets.json").exists()

    asyncio.run(scenario())


def test_recovery_on_a_replacement_computer(paths: Any) -> None:
    """8.4: a new worker (new controller key, new credentials, the same connection) gets the vault, takes
    the gateway with RECOVER, rekeys the bridge and the tag through tunnels, then assigns and clears the tag."""

    async def scenario() -> None:
        async with Simulator(config()) as sim:
            gw = sim.gateway.secure
            assert gw is not None
            fake = FakeV2Cremind()
            old_dir = paths.worker_dir("old")
            make_worker(old_dir, fake, gw.device_id, gw.keys.ik_pub)
            async with worker(old_dir, sim, paths, fake):
                bridge_id, tag_device, tag_id = await claim_and_pair(sim, fake)
            old_controller = fake.controller_pub
            bridge, tag = sim.bridge(0).secure, sim.tag(tag_id).secure
            assert bridge is not None and tag is not None
            old_mk, old_root = bridge.record.op_key, tag.record.op_key
            for cred in fake.credentials.values():  # the recovery revokes the old worker's credentials
                cred.revoked = True
            new_dir = paths.worker_dir("new")
            make_worker(new_dir, fake, gw.device_id, gw.keys.ik_pub, bind_gateway=False)
            assert fake.controller_pub != old_controller
            async with worker(new_dir, sim, paths, fake) as run:
                op = await run_op(fake, "recover_gateway", {}, timeout=180)
                assert op["result"] == {"pending": []}
                assert gw.record.controller == fake.controller_pub and gw.record.gen == 2
                assert bridge.record.controller == fake.controller_pub and bridge.record.op_key != old_mk
                assert tag.record.op_key != old_root and tag.record.gen == 2
                assert {d["state"] for d in op["devices"].values()} == {"rekeyed"}
                assert fake.vault_latest(tag_device)["state"]["root"] == tag.record.op_key.hex()
                assert fake.vault_latest(bridge_id)["state"]["mk"] == bridge.record.op_key.hex()
                local = await run.svc.db.run(run.svc.db.find_tag, tag_id)
                first_epoch = next(v["state"]["epoch"] for v in fake.vault[tag_device] if v["stage"] == "committed")
                assert local is not None and local.epoch > first_epoch

    asyncio.run(scenario())


def v1_radio_config() -> SimConfig:
    """A gateway with tag links, no bridge, and a v1 tag enrolled over SWD (no setup label)."""
    return SimConfig(seed=SEED, time_scale=200, protocol=2, bridges=[], tags=[TagSpec.generate(SEED, 0, protocol=1)])


def test_a_tag_enrolled_with_the_hardware_tools_is_imported_on_the_gateways_radio(paths: Any, monkeypatch) -> None:
    """A v1 tag joins without a label: the worker takes its secret from the hardware tools on this computer,
    assigns its v1 key on the gateway's radio and clears it; removing it keeps no secret here."""
    from app.tags import operations
    from app.tags.runtime.enroll import local
    from app.tags.runtime.store.db import TagRecord

    async def scenario() -> None:
        async with Simulator(v1_radio_config()) as sim:
            gw = sim.gateway.secure
            assert gw is not None
            spec = sim.config.tags[0]
            tools = TagRecord(tag_id=spec.tag_id, board=spec.board, panel=spec.panel, width=spec.width,
                              height=spec.height, planes=spec.planes, plane_flags=spec.plane_flags,
                              secret_ref="keyring:tag:x", name="Shelf")
            monkeypatch.setattr(local, "local_enrollment", lambda tag_id, config=None: (
                local.LocalEnrollment(tools, spec.secret) if tag_id == spec.tag_id else None))
            fake = FakeV2Cremind()
            directory = paths.worker_dir("w1")
            make_worker(directory, fake, gw.device_id, gw.keys.ik_pub)
            async with worker(directory, sim, paths, fake) as run:
                await run_op(fake, "claim_gateway", {"device_id": gw.device_id.hex(), "ik": gw.keys.ik_pub.hex(),
                                                     "gen": 0, "mode": "claim"})
                gateway_hw = f"gw-{gw.device_id.hex()}"
                await until(lambda: any(g.get("hw_id") == gateway_hw and g.get("tag_links") == 2
                                        for inv in fake.inventories for g in inv.get("gateways", [])), 60,
                            "tag links reported")
                device = operations.v1_device_id(spec.tag_id)
                fake.add_binding(bytes.fromhex(device), "tag", bytes(32), 0)
                op = await run_op(fake, "import_tag", {"role": "tag", "tag_id": spec.tag_id, "device_id": device,
                                                       "name": "", "bridge_hw_id": gateway_hw, "bridge_id": "g1"})
                local_tag = await run.svc.db.run(run.svc.db.find_tag, spec.tag_id)
                assert local_tag is not None and local_tag.bridge_addr == GATEWAY_ADDR and local_tag.epoch >= 1
                assert local_tag.name == "Shelf" and local_tag.secret_ref.startswith("file:")
                assert sim.tag(spec.tag_id).nvs.stored_epoch == local_tag.epoch  # cleared under the v1 key
                assert op["device"]["panel"] == spec.panel and op["device"]["gen"] == 0
                vault = fake.vault_latest(device)["state"]
                assert vault["proto"] == 1 and vault["secret"] == spec.secret.hex() and vault["bridge"] == gateway_hw
                assert fake.bindings[device]["state"] == "ready"

                # A tag the tools here never enrolled: refused, nothing kept.
                missing = fake.queue_operation("import_tag", {"role": "tag", "tag_id": 0x12345678,
                                                              "device_id": operations.v1_device_id(0x12345678),
                                                              "bridge_hw_id": gateway_hw, "bridge_id": "g1"})
                await until(lambda: fake.op(missing)["state"] in ("succeeded", "failed"), 60, "refused import")
                assert fake.op(missing)["state"] == "failed"
                assert fake.op(missing)["error"]["code"] == "not_enrolled_here"
                assert await run.svc.db.run(run.svc.db.find_tag, 0x12345678) is None

                # Removing it: no release (v1 has no ownership); its record and secret leave this worker.
                await run_op(fake, "unpair", {"device_id": device, "role": "tag", "hw_id": f"{spec.tag_id:08X}",
                                              "generation": 0, "epoch": local_tag.epoch, "protocol": 1})
                assert await run.svc.db.run(run.svc.db.find_tag, spec.tag_id) is None
                assert not run.svc.secrets.has_tag_secret(spec.tag_id)

    asyncio.run(scenario())


def test_an_imported_tag_comes_back_on_a_replacement_computer(paths: Any, monkeypatch) -> None:
    """8.4 for a v1 tag: nothing to rekey; the new worker takes its secret and panel from the vault, then
    assigns and clears it above the epoch it had."""
    from app.tags import operations
    from app.tags.runtime.enroll import local
    from app.tags.runtime.store.db import TagRecord

    async def scenario() -> None:
        async with Simulator(v1_radio_config()) as sim:
            gw = sim.gateway.secure
            assert gw is not None
            spec = sim.config.tags[0]
            tools = TagRecord(tag_id=spec.tag_id, board=spec.board, panel=spec.panel, width=spec.width,
                              height=spec.height, planes=spec.planes, plane_flags=spec.plane_flags,
                              secret_ref="keyring:tag:x", name="Shelf")
            monkeypatch.setattr(local, "local_enrollment", lambda tag_id, config=None: (
                local.LocalEnrollment(tools, spec.secret) if tag_id == spec.tag_id else None))
            fake = FakeV2Cremind()
            old_dir = paths.worker_dir("old")
            make_worker(old_dir, fake, gw.device_id, gw.keys.ik_pub)
            gateway_hw = f"gw-{gw.device_id.hex()}"
            device = operations.v1_device_id(spec.tag_id)
            async with worker(old_dir, sim, paths, fake):
                await run_op(fake, "claim_gateway", {"device_id": gw.device_id.hex(), "ik": gw.keys.ik_pub.hex(),
                                                     "gen": 0, "mode": "claim"})
                await until(lambda: any(g.get("hw_id") == gateway_hw and g.get("tag_links") == 2
                                        for inv in fake.inventories for g in inv.get("gateways", [])), 60,
                            "tag links reported")
                fake.add_binding(bytes.fromhex(device), "tag", bytes(32), 0)
                await run_op(fake, "import_tag", {"role": "tag", "tag_id": spec.tag_id, "device_id": device,
                                                  "name": "", "bridge_hw_id": gateway_hw, "bridge_id": "g1"})
            first_epoch = fake.vault_latest(device)["state"]["epoch"]
            monkeypatch.setattr(local, "local_enrollment", lambda tag_id, config=None: None)  # another computer
            for cred in fake.credentials.values():
                cred.revoked = True
            new_dir = paths.worker_dir("new")
            make_worker(new_dir, fake, gw.device_id, gw.keys.ik_pub, bind_gateway=False)
            async with worker(new_dir, sim, paths, fake) as run:
                op = await run_op(fake, "recover_gateway", {}, timeout=180)
                assert op["result"] == {"pending": []}
                assert op["devices"][device]["state"] == "rekeyed" and op["devices"][device]["cleared"] is True
                restored = await run.svc.db.run(run.svc.db.find_tag, spec.tag_id)
                assert restored is not None and restored.epoch > first_epoch and restored.name == "Shelf"
                assert run.svc.secrets.get_tag_secret(spec.tag_id, restored.secret_ref) == spec.secret
                assert sim.tag(spec.tag_id).nvs.stored_epoch == restored.epoch

    asyncio.run(scenario())


def radio_config(fontpack: bytes | None = None) -> SimConfig:
    """A gateway with tag links and no bridge at all (docs/protocol.md §11)."""
    return SimConfig(seed=SEED, time_scale=200, protocol=2, fontpack=fontpack, bridges=[],
                     tags=[TagSpec.generate(SEED, 0, protocol=2)])


async def pair_on_the_gateway(sim: Simulator, fake: Any) -> tuple[str, str, int]:
    """Claim the gateway and pair the tag on its own radio; returns (gateway hw id, tag device id, tag id)."""
    gw = sim.gateway.secure
    assert gw is not None
    await run_op(fake, "claim_gateway", {"device_id": gw.device_id.hex(), "ik": gw.keys.ik_pub.hex(), "gen": 0,
                                         "mode": "claim"})
    gateway_hw = f"gw-{gw.device_id.hex()}"
    await until(lambda: any(g.get("hw_id") == gateway_hw and g.get("tag_links") == 2
                            for inv in fake.inventories for g in inv.get("gateways", [])), 60, "tag links reported")
    tag_label = label(sim, "tag")
    cand = await discover(fake, "tag", tag_label, [gateway_hw])
    assert cand["bridge_hw_id"] == gateway_hw  # heard by the gateway's own radio
    await run_op(fake, "pair_tag", {"role": "tag", "short_id": tag_label.short_id, "tag_id": tag_label.short_id,
                                    "bridge_hw_id": gateway_hw, "bridge_id": "g1", "name": "Desk"},
                 secret=tag_label.secret)
    tag = sim.tag(tag_label.short_id)
    assert tag.secure is not None
    return gateway_hw, tag.secure.device_id.hex(), tag_label.short_id


def test_a_gateway_alone_pairs_and_clears_a_tag_on_its_own_radio(paths: Any) -> None:
    """No bridge: the tag pairs through a PAIR tunnel on the gateway's radio, the worker keeps its K_epoch and
    clears it through a SESSION tunnel; the inventory reports the gateway's tag links and its tag."""

    async def scenario() -> None:
        async with Simulator(radio_config()) as sim:
            gw = sim.gateway.secure
            assert gw is not None
            fake = FakeV2Cremind()
            directory = paths.worker_dir("w1")
            make_worker(directory, fake, gw.device_id, gw.keys.ik_pub)
            async with worker(directory, sim, paths, fake) as run:
                gateway_hw, tag_device, tag_id = await pair_on_the_gateway(sim, fake)
                tag = sim.tag(tag_id)
                assert tag.secure is not None and tag.secure.record.state == OwnerState.OWNED
                assert fake.bindings[tag_device]["state"] == "ready"
                local = await run.svc.db.run(run.svc.db.find_tag, tag_id)
                assert local is not None and local.bridge_addr == GATEWAY_ADDR and local.epoch >= 1
                assert tag.nvs.stored_epoch == local.epoch  # the clear under the new epoch went through
                assert fake.vault_latest(tag_device)["state"]["bridge"] == gateway_hw
                radio = sim.gateway.radio
                assert radio is not None and radio.counters["connections"] >= 2  # PAIR, then the clear's session
                await until(lambda: any(g.get("hw_id") == gateway_hw and g.get("assigned") == 1
                                        for inv in fake.inventories for g in inv.get("gateways", [])), 60,
                            "the gateway's tag in the inventory")
                last = fake.inventories[-1]
                assert all(b["hw_id"] != gateway_hw for b in last["bridges"])  # the radio is no bridge
                assert not any(d.get("kind") == "bridge" for hb in fake.heartbeats for d in hb.get("devices", []))

    asyncio.run(scenario())


def test_a_tag_on_the_gateways_radio_is_removed_with_its_fresh_code_shown(paths: Any,
                                                                          dev_fonts: tuple[Any, bytes]) -> None:
    """The release (two stages through PAIR tunnels on the gateway's radio) and the setup-code screen the host
    renders itself and streams through a SESSION tunnel."""
    fonts, pack = dev_fonts

    async def scenario() -> None:
        async with Simulator(radio_config(pack)) as sim:
            gw = sim.gateway.secure
            assert gw is not None
            fake = FakeV2Cremind()
            directory = paths.worker_dir("w1")
            make_worker(directory, fake, gw.device_id, gw.keys.ik_pub)
            async with worker(directory, sim, paths, fake, fonts) as run:
                _gateway_hw, tag_device, tag_id = await pair_on_the_gateway(sim, fake)
                local = await run.svc.db.run(run.svc.db.find_tag, tag_id)
                assert local is not None
                await run_op(fake, "unpair", {"device_id": tag_device, "role": "tag", "hw_id": f"{tag_id:08X}",
                                              "generation": 1, "epoch": local.epoch})
                tag = sim.tag(tag_id)
                assert tag.secure is not None and tag.secure.record.state == OwnerState.RELEASED
                assert await run.svc.db.run(run.svc.db.find_tag, tag_id) is None
                fresh = label(sim, "tag")
                assert tag.displayed_digest == expected_radio_setup_screen(sim, tag_id, fonts, fresh)

    asyncio.run(scenario())


def test_screens_on_the_gateways_radio_survive_a_worker_restart(paths: Any, dev_fonts: tuple[Any, bytes]) -> None:
    """Content reaches a tag on the gateway's radio; a screen sent while the tag is away is lost with the worker
    (the radio keeps its jobs in RAM) and goes out again after the restart, under the assignment the new worker
    loaded from its inventory (K_epoch derived again)."""
    fonts, pack = dev_fonts

    async def scenario() -> None:
        async with Simulator(radio_config(pack)) as sim:
            gw = sim.gateway.secure
            assert gw is not None
            fake = FakeV2Cremind()
            directory = paths.worker_dir("w1")
            make_worker(directory, fake, gw.device_id, gw.keys.ik_pub)
            async with worker(directory, sim, paths, fake, fonts) as run:
                gateway_hw, _tag_device, tag_id = await pair_on_the_gateway(sim, fake)
                local = await run.svc.db.run(run.svc.db.find_tag, tag_id)
                assert local is not None
                hw = f"{tag_id:08X}"
                fake.add_tag(hw, owner="anna", epoch=local.epoch, bridge_hw_id=gateway_hw)
                first = fake.add_job("anna", hw, title="Before the restart")
                await until(lambda: fake.delivery(first)["stage"] == "displayed", 90, "the first screen")
                shown = sim.tag(tag_id).displayed_digest
                sim.tag(tag_id).out_of_range = True  # the next screen cannot reach the tag
                second = fake.add_job("anna", hw, title="After the restart")
                await until(lambda: fake.delivery(second)["stage"] in ("gateway_received", "bridge_received"), 60,
                            "the second screen sent")
            assert sim.tag(tag_id).displayed_digest == shown
            sim.tag(tag_id).out_of_range = False
            async with worker(directory, sim, paths, fake, fonts) as run:
                await until(lambda: fake.delivery(second)["stage"] == "displayed", 90, "the second screen")
                assert sim.tag(tag_id).displayed_digest != shown
                radio = run.svc.gateway.radio
                assert radio is not None and tag_id in radio.assignments  # loaded again at start

    asyncio.run(scenario())


def expected_radio_setup_screen(sim: Simulator, tag_id: int, fonts: Any, payload: SetupPayload) -> bytes:
    """The digest of the setup-code screen as the host renders it for a tag on the gateway's radio."""
    import hashlib

    from app.tags.runtime.compose.api import TagPanel
    from app.tags.runtime.compose.screen import compose_setup_code
    from app.tags.runtime.layout.fonts import FontContext
    from app.tags.runtime.render.reference import Panel, render_frame

    spec = sim.tag(tag_id).spec
    screen = compose_setup_code(TagPanel(tag_id, spec.width, spec.height, spec.planes, spec.plane_flags, 0, ""),
                                fonts, payload.code(), payload.qr_text())
    frame = render_frame(screen.layout, Panel(spec.width, spec.height, spec.planes, spec.plane_flags),
                         FontContext.for_fontset(fonts).pack)
    return hashlib.sha256(b"".join(frame.planes)).digest()


def test_a_released_tag_pairs_again_with_its_fresh_code(paths: Any, dev_fonts: tuple[Any, bytes]) -> None:
    """A removed tag shows a fresh setup code; pairing it again works (the label's code no longer does), and
    the new assignment lands above the epoch the tag kept (its STALE_EPOCH raises the floor)."""

    fonts, pack = dev_fonts

    async def scenario() -> None:
        async with Simulator(config(pack)) as sim:
            gw = sim.gateway.secure
            assert gw is not None
            fake = FakeV2Cremind()
            directory = paths.worker_dir("w1")
            make_worker(directory, fake, gw.device_id, gw.keys.ik_pub)
            async with worker(directory, sim, paths, fake, fonts) as run:
                bridge_id, tag_device, tag_id = await claim_and_pair(sim, fake)
                first = await run.svc.db.run(run.svc.db.find_tag, tag_id)
                assert first is not None
                original = label(sim, "tag")
                await run_op(fake, "unpair", {"device_id": tag_device, "role": "tag", "hw_id": f"{tag_id:08X}",
                                              "generation": 1, "epoch": first.epoch})
                fresh_label = label(sim, "tag")  # the code the released tag pairs with now
                assert fresh_label.short_id == original.short_id and fresh_label.secret != original.secret
                old_root = bytes.fromhex(fake.vault_latest(tag_device)["state"]["root"])
                sim.tag(tag_id).nvs.stored_epoch = 5  # a tag with a longer history keeps its epoch through a release
                cand = await discover(fake, "tag", fresh_label, [f"br-{bridge_id}"])
                await run_op(fake, "pair_tag", {"role": "tag", "short_id": fresh_label.short_id, "tag_id": tag_id,
                                                "bridge_hw_id": cand["bridge_hw_id"], "bridge_id": "b1",
                                                "name": "Desk"}, secret=fresh_label.secret)
                tag = sim.tag(tag_id).secure
                assert tag is not None and tag.record.state == OwnerState.OWNED and tag.record.op_key != old_root
                again = await run.svc.db.run(run.svc.db.find_tag, tag_id)
                assert again is not None and again.epoch > 5  # assigned again above the epoch the tag reported
                assert sim.tag(tag_id).nvs.stored_epoch == again.epoch  # and the clear under it went through

    asyncio.run(scenario())
