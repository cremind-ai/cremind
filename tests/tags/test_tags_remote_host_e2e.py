"""A desktop gateway computer end to end, over real HTTP.

Cremind runs as an HTTP server (the setup bootstrap, host and connector APIs
on a local port — what a container or a NAS would serve); "this computer" is a
separate runtime folder that only reaches it over HTTP, as the Cremind app on
another computer does:

1. the profile starts ``enroll_host``; the computer completes the
   ``cremind://tags/setup`` link (bind with its installation key, approve,
   wait for the page's confirmation of the same words, redeem with the hash
   of a credential it keeps);
2. its hardware host runs remote: status reports, work long-polls and
   progress over ``/api/tag-host/v1`` with that credential;
3. the profile searches it and connects the simulated gateway; the worker
   the computer set up claims it through ``/api/tag-connector/v1`` with its
   own credentials;
4. the profile removes the computer: its credential stops working and the
   host stops by itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time
from typing import Any

import pytest

pytest.importorskip("cbor2")
pytest.importorskip("serial")

from tests.tags._helpers import body_of, find_handler, tagenv  # noqa: E402,F401
from tests.tags.test_tags_setup import call, req  # noqa: E402

pytestmark = pytest.mark.timeout(300)


@pytest.fixture(autouse=True)
def _authority_dir(tmp_path, monkeypatch):
    from app.tags import authority

    monkeypatch.setattr(authority, "directory", lambda: tmp_path / ".tag-authority")
    monkeypatch.setenv("CREMIND_TAGS_SIMPLE_SETUP", "1")
    authority.reset_cache()
    yield
    authority.reset_cache()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def cremind_http():
    """The APIs another computer talks to, served over HTTP on a free local port."""
    import uvicorn
    from starlette.applications import Starlette

    from app.api.tag_connector import get_tag_connector_routes
    from app.api.tag_host import get_tag_host_routes
    from app.api.tag_setup_bootstrap import get_tag_setup_bootstrap_routes

    app = Starlette(routes=[*get_tag_setup_bootstrap_routes(), *get_tag_host_routes(), *get_tag_connector_routes()])
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off",
                                          timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run, name="cremind http", daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the HTTP server did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(10)


async def acall(routes, method: str, path: str, request) -> tuple[int, dict]:
    resp = await find_handler(routes, path, method)(request)
    return resp.status_code, body_of(resp)


async def until(check, what: str, timeout: float = 90.0, every: float = 0.1) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last: Any = None
    while loop.time() < deadline:
        last = await check()
        if last:
            return last
        await asyncio.sleep(every)
    raise AssertionError(f"timed out waiting for {what} (last: {last!r})")


def enroll_computer(origin: str, paths: Any) -> Any:
    """The profile starts the setup; the computer completes the link while the page confirms."""
    from app.api.tags_setup import get_tags_setup_routes
    from app.tags.runtime.host.enroll import enroll

    routes = get_tags_setup_routes()
    status, out = call(routes, "POST", "/api/tags/setup-sessions",
                       req("p1", body={"operation": "enroll_host", "server_url": origin}))
    assert status == 201, out
    session_id, link = out["session"]["id"], out["launch_url"]
    assert link.startswith("cremind://tags/setup?")
    outcome: dict[str, Any] = {}
    seen: list[str] = []

    def computer() -> None:
        try:
            outcome["enrollment"] = enroll(
                link, paths, approve=lambda bound: seen.append(bound.verification_phrase) or True,
                on_event=lambda name, fields: seen.append(name), version="test", poll_every_s=0.1,
                computer="DESK-LAB")
        except Exception as exc:  # noqa: BLE001 - reported by the assertion below
            outcome["error"] = exc

    thread = threading.Thread(target=computer, name="enroll")
    thread.start()
    deadline = time.monotonic() + 60
    while True:
        status, out = call(routes, "GET", "/api/tags/setup-sessions/{session_id}",
                           req("p1", path={"session_id": session_id}))
        if out["session"]["state"] == "waiting_for_confirmation":
            break
        assert time.monotonic() < deadline and "error" not in outcome, (out, outcome)
        time.sleep(0.05)
    # The page shows the same words the computer showed.
    assert out["session"]["verification_phrase"] == seen[1]
    status, out = call(routes, "POST", "/api/tags/setup-sessions/{session_id}/confirm",
                       req("p1", path={"session_id": session_id}, body={}))
    assert status == 200, out
    thread.join(60)
    assert "error" not in outcome, outcome
    assert seen[0] == "bound" and seen[-1] == "enrolled"
    return outcome["enrollment"]


def test_a_desktop_computer_drives_a_gateway_for_a_remote_cremind(tagenv, tmp_path) -> None:
    from app.api.tags_hosts import get_tags_hosts_routes
    from app.api.tags_setup import get_tags_setup_routes
    from app.tags import hosts
    from app.tags.hosting.host import HardwareHost
    from app.tags.hosting.paths import RuntimePaths
    from app.tags.hosting.supervisor import SupervisorOptions
    from app.tags.runtime.connect.usb import PortInfo
    from app.tags.runtime.host.enroll import load_enrollment
    from app.tags.runtime.protocol.ids import OwnerState
    from app.tags.runtime.sim import SimConfig, Simulator

    paths = RuntimePaths(tmp_path / "desk" / ".tag-runtime")
    with cremind_http() as origin:
        enrollment = enroll_computer(origin, paths)
        assert load_enrollment(paths).host_id == enrollment.host_id
        assert enrollment.secret not in (paths.enrollment_file.read_text(encoding="utf-8"))

        routes = get_tags_hosts_routes()
        setup_routes = get_tags_setup_routes()

        async def scenario() -> None:
            async with Simulator(SimConfig(seed=11, time_scale=200, protocol=2, bridges=[], tags=[])) as sim:
                gw = sim.gateway.secure
                port = PortInfo(sim.gateway_url, None, None, None, "Cremind Tag gateway (simulated)", "gateway")
                host = HardwareHost(paths, options=SupervisorOptions(list_ports=lambda: [port], scan_interval_s=0.5),
                                    check_components=False, remote=enrollment)
                loop = asyncio.get_running_loop()
                assert await asyncio.to_thread(host.start, loop), (host.state, host.reason)
                try:
                    async def listed() -> dict | None:
                        view = await hosts.list_hosts("p1")
                        found = next((h for h in view["hosts"] if h["id"] == enrollment.host_id), None)
                        return found if found and found["state"] == "running" and found["online"] else None

                    view = await until(listed, "the computer's first status report")
                    assert view["kind"] == "desktop" and view["name"] == "DESK-LAB" and view["access"]["can_use"]

                    status, out = await acall(routes, "POST", "/api/tags/hosts/{host_id}/scan",
                                              req("p1", path={"host_id": enrollment.host_id}, body={}))
                    assert status == 202, out
                    scan_id = out["operation"]["id"]

                    async def scanned() -> dict | None:
                        op = await hosts.get_operation("p1", scan_id)
                        return op if op["state"] in ("succeeded", "failed") else None

                    scan = await until(scanned, "the search")
                    [cand] = scan["candidates"]
                    assert cand["state"] == "usable" and cand["device_id"] == gw.device_id.hex(), scan

                    status, out = await acall(routes, "POST", "/api/tags/connections",
                                              req("p1", body={"host_id": enrollment.host_id,
                                                              "candidate_id": cand["id"], "name": "Lab gateway"}))
                    assert status == 202, out
                    connect_id = out["operation"]["id"]

                    async def connected() -> dict | None:
                        op = await hosts.get_operation("p1", connect_id)
                        return op if op["state"] in ("succeeded", "failed", "cancelled") else None

                    op = await until(connected, "the connection", timeout=150)
                    assert op["state"] == "succeeded", op
                    assert gw.record.state == OwnerState.OWNED and gw.record.gen == 1
                    status, out = await acall(setup_routes, "GET", "/api/tags/connections", req("p1"))
                    [conn] = out["connections"]
                    assert conn["execution_kind"] == "desktop" and conn["computer"]["name"] == "DESK-LAB"
                    assert conn["status"] == "connected" and conn["name"] == "Lab gateway"

                    # Removed on the page: the computer's credential stops working, and it stops by itself.
                    status, out = await acall(routes, "DELETE", "/api/tags/hosts/{host_id}",
                                              req("p1", path={"host_id": enrollment.host_id}))
                    assert status == 200, out
                    host._agent.poke()  # report now instead of in 20 s

                    async def revoked() -> bool:
                        return host.state == "revoked"

                    await until(revoked, "the computer to notice it was removed", timeout=60)
                    assert "no longer accepts this computer" in (host.reason or "")
                finally:
                    await asyncio.to_thread(host.stop, "test done")

        asyncio.run(scenario())
