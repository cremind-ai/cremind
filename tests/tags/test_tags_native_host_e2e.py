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


def test_the_server_drives_a_gateway_plugged_into_it(tagenv, tmp_path) -> None:
    from app.api.tags_hosts import get_tags_hosts_routes
    from app.api.tags_setup import get_tags_setup_routes
    from app.tags import hosts
    from app.tags.hosting.host import HardwareHost
    from app.tags.hosting.paths import RuntimePaths
    from app.tags.hosting.supervisor import SupervisorOptions
    from app.tags.runtime.connect.usb import PortInfo
    from app.tags.runtime.protocol.ids import OwnerState
    from app.tags.runtime.sim import SimConfig, Simulator

    routes = get_tags_hosts_routes()
    setup_routes = get_tags_setup_routes()

    async def scenario() -> None:
        async with Simulator(SimConfig(seed=7, time_scale=200, protocol=2, bridges=[], tags=[])) as sim:
            gw = sim.gateway.secure
            assert gw is not None
            paths = RuntimePaths(tmp_path / ".tag-runtime")
            port = PortInfo(sim.gateway_url, None, None, None, "Cremind Tag gateway (simulated)", "gateway")
            host = HardwareHost(paths, options=SupervisorOptions(list_ports=lambda: [port], scan_interval_s=0.5),
                                check_components=False)
            loop = asyncio.get_running_loop()
            assert await asyncio.to_thread(host.start, loop), (host.state, host.reason)
            try:
                host_id = host.host_id

                async def listed() -> dict | None:
                    view = await hosts.list_hosts("admin")
                    found = next((h for h in view["hosts"] if h["id"] == host_id), None)
                    return found if found and found["state"] == "running" else None

                view = await until(listed, "the host's first status report")
                assert view["kind"] == "server" and view["access"]["can_use"]

                # Search: the gateway is found and offered without its port.
                status, out = await acall(routes, "POST", "/api/tags/hosts/{host_id}/scan",
                                          req("admin", path={"host_id": host_id}, body={}))
                assert status == 202, out
                scan_id = out["operation"]["id"]

                async def scanned() -> dict | None:
                    op = await hosts.get_operation("admin", scan_id)
                    return op if op["state"] in ("succeeded", "failed") else None

                scan = await until(scanned, "the search")
                assert scan["state"] == "succeeded", scan
                [cand] = scan["candidates"]
                assert cand["device_id"] == gw.device_id.hex() and cand["state"] == "usable", cand
                assert sim.gateway_url not in json.dumps(scan), "a search never names a port"

                # Connect it: the worker is set up here and claims the gateway.
                status, out = await acall(setup_routes, "GET", "/api/tags/connections", req("admin"))
                assert out["connections"] == []
                status, out = await acall(routes, "POST", "/api/tags/connections",
                                          req("admin", body={"host_id": host_id, "candidate_id": cand["id"],
                                                             "name": "Office gateway"}))
                assert status == 202, out
                connect_id = out["operation"]["id"]

                async def connected() -> dict | None:
                    op = await hosts.get_operation("admin", connect_id)
                    return op if op["state"] in ("succeeded", "failed", "cancelled") else None

                op = await until(connected, "the connection", timeout=120)
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

                async def worker_running(h=None) -> dict | None:
                    status = await (h or host).status()
                    workers = [w for w in status.get("workers") or [] if w["companion_id"] == companion_id]
                    return workers[0] if workers and workers[0]["state"] == "running" \
                        and workers[0]["gateway_connected"] else None

                worker = await until(worker_running, "the worker on its gateway")
                assert worker["gate"]["open"] is True, worker

                # Stop: the gateway is released. Start again: the same worker comes back by itself.
                await asyncio.to_thread(host.stop, "test restart")
                assert host.state == "stopped"
                assert await asyncio.to_thread(host.start, loop), (host.state, host.reason)
                again = await until(worker_running, "the worker after a restart")
                assert again["companion_id"] == companion_id
                status, out = await acall(setup_routes, "GET", "/api/tags/connections", req("admin"))
                assert [c["id"] for c in out["connections"]] == [companion_id]
            finally:
                await asyncio.to_thread(host.stop, "test done")

    asyncio.run(scenario())
