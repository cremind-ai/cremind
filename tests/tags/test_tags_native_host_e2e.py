"""The server's own hardware host end to end: Cremind drives a gateway plugged into its computer.

Nothing is faked between the page and the radio except the USB cable: the
v2 simulator serves the gateway on a local socket (what pyserial opens like a
serial port), and everything else is the product — the host thread and its
supervisor, the host agent reaching Cremind in process, the profile API
(search, connect), the worker (daemon + agent) reaching the connector
services through the in-process adapter, the send gate, and the database.

A search finds the gateway without naming a port; connecting it creates the
worker's records, writes its directory, starts it on the gateway's port, and
the worker claims the gateway under the authority's grant. The connection
appears once the worker reported in. Stopping the host releases the gateway;
starting it again brings the same worker back on its own.

A gateway the older Cremind Connect ran moves in the same way when the host
starts: the same connection, keys and pairing (the gateway is not claimed
again), and Cremind Connect lets go of it.

A gateway alone is enough for the tags near it (docs/protocol.md §11): with
no bridge anywhere, Add tag's search hears the tag on the gateway's own radio,
and the pairing assigns and clears it there.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

pytest.importorskip("cbor2")
pytest.importorskip("serial")

from tests.tags._helpers import body_of, find_handler, tagenv  # noqa: E402,F401
from tests.tags.test_tags_setup import req  # noqa: E402

pytestmark = pytest.mark.timeout(300)


@pytest.fixture(autouse=True)
def _authority_dir(tmp_path, monkeypatch):
    from app.tags import authority

    monkeypatch.setattr(authority, "directory", lambda: tmp_path / ".tag-authority")
    monkeypatch.setenv("CREMIND_TAGS_SIMPLE_SETUP", "1")
    authority.reset_cache()
    yield
    authority.reset_cache()


async def acall(routes, method: str, path: str, request) -> tuple[int, dict]:
    resp = await find_handler(routes, path, method)(request)
    return resp.status_code, body_of(resp)


async def until(check, what: str, timeout: float = 90.0, every: float = 0.1) -> Any:
    """Poll ``check()`` (async) until it returns something truthy."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last: Any = None
    while loop.time() < deadline:
        last = await check()
        if last:
            return last
        await asyncio.sleep(every)
    raise AssertionError(f"timed out waiting for {what} (last: {last!r})")


def simulated_host(paths, sim) -> Any:
    from app.tags.hosting.host import HardwareHost
    from app.tags.hosting.supervisor import SupervisorOptions
    from app.tags.runtime.connect.usb import PortInfo

    port = PortInfo(sim.gateway_url, None, None, None, "Cremind Tag gateway (simulated)", "gateway")
    return HardwareHost(paths, options=SupervisorOptions(list_ports=lambda: [port], scan_interval_s=0.5),
                        check_components=False)


async def reported(host_id: str) -> dict | None:
    """The host as the admin sees it, once it reported running."""
    from app.tags import hosts

    view = await hosts.list_hosts("admin")
    found = next((h for h in view["hosts"] if h["id"] == host_id), None)
    return found if found and found["state"] == "running" else None


async def search(routes, host_id: str) -> dict:
    from app.tags import hosts

    status, out = await acall(routes, "POST", "/api/tags/hosts/{host_id}/scan",
                              req("admin", path={"host_id": host_id}, body={}))
    assert status == 202, out
    scan_id = out["operation"]["id"]

    async def scanned() -> dict | None:
        op = await hosts.get_operation("admin", scan_id)
        return op if op["state"] in ("succeeded", "failed") else None

    return await until(scanned, "the search")


async def connect(routes, host_id: str, candidate_id: str, name: str) -> dict:
    from app.tags import hosts

    status, out = await acall(routes, "POST", "/api/tags/connections",
                              req("admin", body={"host_id": host_id, "candidate_id": candidate_id, "name": name}))
    assert status == 202, out
    connect_id = out["operation"]["id"]

    async def connected() -> dict | None:
        op = await hosts.get_operation("admin", connect_id)
        return op if op["state"] in ("succeeded", "failed", "cancelled") else None

    return await until(connected, "the connection", timeout=120)


async def worker_running(host, companion_id: str) -> dict | None:
    status = await host.status()
    workers = [w for w in status.get("workers") or [] if w["companion_id"] == companion_id]
    return workers[0] if workers and workers[0]["state"] == "running" and workers[0]["gateway_connected"] else None


def test_the_server_drives_a_gateway_plugged_into_it(tagenv, tmp_path) -> None:
    from app.api.tags_hosts import get_tags_hosts_routes
    from app.api.tags_setup import get_tags_setup_routes
    from app.tags.hosting.paths import RuntimePaths
    from app.tags.runtime.protocol.ids import OwnerState
    from app.tags.runtime.sim import SimConfig, Simulator

    routes = get_tags_hosts_routes()
    setup_routes = get_tags_setup_routes()

    async def scenario() -> None:
        async with Simulator(SimConfig(seed=7, time_scale=200, protocol=2, bridges=[], tags=[])) as sim:
            gw = sim.gateway.secure
            assert gw is not None
            paths = RuntimePaths(tmp_path / ".tag-runtime")
            host = simulated_host(paths, sim)
            loop = asyncio.get_running_loop()
            assert await asyncio.to_thread(host.start, loop), (host.state, host.reason)
            try:
                host_id = host.host_id
                view = await until(lambda: reported(host_id), "the host's first status report")
                assert view["kind"] == "server" and view["access"]["can_use"]

                # Search: the gateway is found and offered without its port.
                scan = await search(routes, host_id)
                assert scan["state"] == "succeeded", scan
                [cand] = scan["candidates"]
                assert cand["device_id"] == gw.device_id.hex() and cand["state"] == "usable", cand
                assert sim.gateway_url not in json.dumps(scan), "a search never names a port"

                # Connect it: the worker is set up here and claims the gateway.
                status, out = await acall(setup_routes, "GET", "/api/tags/connections", req("admin"))
                assert out["connections"] == []
                op = await connect(routes, host_id, cand["id"], "Office gateway")
                assert op["state"] == "succeeded", op
                companion_id = op["companion_id"]
                assert gw.record.state == OwnerState.OWNED and gw.record.gen == 1

                status, out = await acall(setup_routes, "GET", "/api/tags/connections", req("admin"))
                [conn] = out["connections"]
                assert conn["id"] == companion_id and conn["name"] == "Office gateway"
                assert conn["status"] == "connected" and conn["execution_kind"] == "server"
                assert conn["gateway"]["state"] == "ready" and conn["computer"]["host_id"] == host_id
                # The worker's secrets stay on this computer: Cremind keeps their hashes only.
                [worker_dir] = [p for p in paths.workers_dir.iterdir() if p.is_dir()]
                assert (worker_dir / "secrets.json").is_file() and (worker_dir / "controller.key").is_file()

                worker = await until(lambda: worker_running(host, companion_id), "the worker on its gateway")
                assert worker["gate"]["open"] is True, worker

                # Stop: the gateway is released. Start again: the same worker comes back by itself.
                await asyncio.to_thread(host.stop, "test restart")
                assert host.state == "stopped"
                assert await asyncio.to_thread(host.start, loop), (host.state, host.reason)
                again = await until(lambda: worker_running(host, companion_id), "the worker after a restart")
                assert again["companion_id"] == companion_id and host.host_id == host_id, "the same computer"
                status, out = await acall(setup_routes, "GET", "/api/tags/connections", req("admin"))
                assert [c["id"] for c in out["connections"]] == [companion_id]
            finally:
                await asyncio.to_thread(host.stop, "test done")

    asyncio.run(scenario())


def test_a_gateway_alone_sets_up_a_tag_without_any_bridge(tagenv, tmp_path) -> None:
    from app.api.tags_hosts import get_tags_hosts_routes
    from app.api.tags_setup import get_tags_setup_routes
    from app.tags import operations
    from app.tags.hosting.paths import RuntimePaths
    from app.tags.runtime.protocol.ids import OwnerState
    from app.tags.runtime.sim import SimConfig, Simulator, TagSpec

    routes = get_tags_hosts_routes()
    setup_routes = get_tags_setup_routes()

    async def scenario() -> None:
        config = SimConfig(seed=13, time_scale=200, protocol=2, bridges=[], tags=[TagSpec.generate(13, 0, protocol=2)])
        async with Simulator(config) as sim:
            paths = RuntimePaths(tmp_path / ".tag-runtime")
            host = simulated_host(paths, sim)
            loop = asyncio.get_running_loop()
            assert await asyncio.to_thread(host.start, loop), (host.state, host.reason)
            try:
                host_id = host.host_id
                await until(lambda: reported(host_id), "the host's first status report")
                [cand] = (await search(routes, host_id))["candidates"]
                op = await connect(routes, host_id, cand["id"], "Office gateway")
                assert op["state"] == "succeeded", op
                companion_id = op["companion_id"]

                async def serving() -> dict | None:
                    """The connection, once its gateway reported that it reaches tags itself."""
                    _, out = await acall(setup_routes, "GET", "/api/tags/connections", req("admin"))
                    conns = [c for c in out["connections"] if c["id"] == companion_id]
                    return conns[0] if conns and conns[0]["gateway"]["serves_tags"] else None

                conn = await until(serving, "the gateway's tag links in the inventory")
                assert conn["bridges"] == [] and conn["gateway"]["capacity"] == {"max_tags": 20, "assigned": 0}

                # Add tag: the search hears it on the gateway's own radio.
                code = next(c["code"] for c in sim.setup_codes() if c["role"] == "tag")
                status, out = await acall(setup_routes, "POST", "/api/tags/discovery",
                                          req("admin", body={"role": "tag", "setup_code": code}))
                assert status == 201, out
                disc_id = out["discovery"]["id"]

                async def found() -> dict | None:
                    _, out = await acall(setup_routes, "GET", "/api/tags/discovery/{op_id}",
                                         req("admin", path={"op_id": disc_id}))
                    return out["discovery"] if out["discovery"]["candidates"] else None

                disc = await until(found, "the tag heard by the gateway")
                [tag_cand] = disc["candidates"]
                assert tag_cand["bridge_kind"] == "gateway" and tag_cand["bridge_id"] == conn["gateway"]["id"]
                assert tag_cand["eligible"] and disc["recommended"] == tag_cand["id"]
                status, out = await acall(setup_routes, "POST", "/api/tags/pairings",
                                          req("admin", body={"discovery_id": disc_id, "candidate_id": tag_cand["id"],
                                                             "name": "Desk"}))
                assert status == 201, out
                pairing_id = out["pairing"]["id"]

                async def paired() -> dict | None:
                    view = await operations.get_pairing("admin", pairing_id)
                    return view if view["state"] in ("succeeded", "failed", "cancelled") else None

                done = await until(paired, "the pairing", timeout=150)
                assert done["state"] == "succeeded", done

                # The tag lives on the gateway: owned, assigned and cleared through its radio; no mesh anywhere.
                _, out = await acall(setup_routes, "GET", "/api/tags/connections", req("admin"))
                [conn] = [c for c in out["connections"] if c["id"] == companion_id]
                [tag] = conn["tags"]
                assert tag["bridge_id"] == conn["gateway"]["id"] and tag["state"] == "ready", tag
                assert conn["gateway"]["capacity"]["assigned"] == 1
                sim_tag = sim.tag(config.tags[0].tag_id)
                assert sim_tag.secure is not None and sim_tag.secure.record.state == OwnerState.OWNED
                assert sim_tag.nvs.stored_epoch >= 1, "cleared under its first epoch"
                radio = sim.gateway.radio
                assert radio is not None and radio.counters["connections"] >= 2
            finally:
                await asyncio.to_thread(host.stop, "test done")

    asyncio.run(scenario())


def test_a_gateway_cremind_connect_ran_moves_into_the_server(tagenv, tmp_path, monkeypatch) -> None:
    import shutil
    from dataclasses import replace
    from types import SimpleNamespace

    from sqlalchemy import text

    from app.api.tags_hosts import get_tags_hosts_routes
    from app.tags.hosting import migration
    from app.tags.hosting.paths import RuntimePaths
    from app.tags.runtime.connect.setup_flow import read_controller_key
    from app.tags.runtime.connect.workerdir import load_worker, write_worker
    from app.tags.runtime.sim import SimConfig, Simulator

    routes = get_tags_hosts_routes()
    connect_dir = SimpleNamespace(workers_dir=tmp_path / "connect" / "workers",
                                  service_lock=tmp_path / "connect" / "service.lock")
    connect_dir.workers_dir.mkdir(parents=True)
    told: list[tuple[str, str]] = []

    class ConnectService:
        """Cremind Connect's service over its IPC: it lets go of a worker when told to."""

        def __init__(self, _paths) -> None:
            pass

        def disable(self, worker_id: str) -> None:
            told.append(("disable", worker_id))

        def enable(self, worker_id: str) -> None:
            told.append(("enable", worker_id))

        def running(self, worker_id: str) -> bool:
            return False

        def retire(self) -> None:
            told.append(("retire", ""))

    monkeypatch.setattr(migration, "connect_paths", lambda: connect_dir)
    monkeypatch.setattr(migration, "ConnectService", ConnectService)

    async def scenario() -> None:
        async with Simulator(SimConfig(seed=11, time_scale=200, protocol=2, bridges=[], tags=[])) as sim:
            gw = sim.gateway.secure
            assert gw is not None
            paths = RuntimePaths(tmp_path / ".tag-runtime")
            host = simulated_host(paths, sim)
            loop = asyncio.get_running_loop()
            assert await asyncio.to_thread(host.start, loop), (host.state, host.reason)
            try:
                host_id = host.host_id
                await until(lambda: reported(host_id), "the host's first status report")
                [cand] = (await search(routes, host_id))["candidates"]
                op = await connect(routes, host_id, cand["id"], "Hall gateway")
                assert op["state"] == "succeeded", op
                companion_id = op["companion_id"]
                await until(lambda: worker_running(host, companion_id), "the worker on its gateway")
                await asyncio.to_thread(host.stop, "test: Cremind Connect runs it")

                # What Cremind Connect had: the worker's directory in its folder, the connection run by no host.
                [worker_dir] = [p for p in paths.workers_dir.iterdir() if p.is_dir()]
                source = connect_dir.workers_dir / worker_dir.name
                shutil.move(str(worker_dir), str(source))
                spec = load_worker(source)
                write_worker(source, replace(spec, extra={k: v for k, v in spec.extra.items() if k != "host_id"}))
                with tagenv.engine.begin() as c:
                    c.execute(text("UPDATE tag_companions SET execution_kind = 'legacy_external', host_id = NULL "
                                   "WHERE id = :c"), {"c": companion_id})
                key, generation = read_controller_key(source), gw.record.gen

                # The host starts: the worker moves in and drives the same gateway, paired as it was.
                assert await asyncio.to_thread(host.start, loop), (host.state, host.reason)
                assert host.host_id == host_id

                async def accepted() -> dict | None:
                    """Running on its gateway, and Cremind accepted the worker as it was (its gate opened)."""
                    worker = await worker_running(host, companion_id)
                    return worker if worker and worker["gate"]["open"] else None

                await until(accepted, "the worker moved in")
                assert gw.record.gen == generation, "the gateway was not claimed again"
                moved = paths.workers_dir / spec.worker_id
                assert read_controller_key(moved) == key
                assert load_worker(moved).extra["migrated_from"] == "cremind-connect"
                left = load_worker(source)
                assert left.enabled is False and left.extra["migrated_to"] == "cremind"
                assert told == [("disable", spec.worker_id), ("retire", "")], "Connect has nothing left to run"
                row = tagenv.engine.connect().execute(text(
                    "SELECT execution_kind, host_id FROM tag_companions WHERE id = :c"), {"c": companion_id}).one()
                assert tuple(row) == ("server", host_id)

                async def move_reported() -> dict | None:
                    view = await reported(host_id)
                    return view if view and view["migration"].get("moved") else None

                view = await until(move_reported, "the move in the host's status report")
                assert view["migration"] == {"moved": 1, "failed": 0, "rolled_back": 0}
            finally:
                await asyncio.to_thread(host.stop, "test done")

    asyncio.run(scenario())
