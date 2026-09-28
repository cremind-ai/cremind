"""The simulated gateway's own radio on the wire (docs/protocol.md §11, app/tags/runtime/sim/gateway_radio.py): the
caps, DISCOVER on the gateway's radio, PAIR and SESSION tunnels to tags it connects to itself, their refusals,
queueing and closing — what the gateway firmware's test_radio.c checks, from the host's side."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import pytest

from app.tags.runtime.protocol.ids import (
    GATEWAY_ADDR,
    PROTO_VERSION,
    TAG_CTRL_MSG_MAX,
    CtrlMsg,
    NodeRole,
    OwnerState,
    SerialMsg,
    Status,
    TunnelMode,
    TunnelState,
)
from app.tags.runtime.protocol.msgs import CtrlChallenge, CtrlHello, TagCaps
from app.tags.runtime.secure.codes import parse_code
from app.tags.runtime.sim import BridgeSpec, SimConfig, Simulator, TagSpec
from app.tags.runtime.sim.harness import run_scenario
from app.tags.runtime.sim.radio import ADV_FLAG_SETUP

v2host: Any = sys.modules["v2host"]  # loaded by conftest
Authority, Worker, V2Host, HostTunnel = v2host.Authority, v2host.Worker, v2host.V2Host, v2host.HostTunnel

pytestmark = pytest.mark.timeout(180)

SEED = 97


def config(*, tag_links: int = 2, v1_tags: int = 0, v2_tags: int = 1, bridges: int = 0,
           time_scale: float = 200) -> SimConfig:
    tags = [TagSpec.generate(SEED, i, protocol=2) for i in range(v2_tags)]
    tags += [TagSpec.generate(SEED, 100 + i) for i in range(v1_tags)]
    return SimConfig(seed=SEED, time_scale=time_scale, protocol=2, bridges=[BridgeSpec(provisioned=False)
                                                                           for _ in range(bridges)],
                     tags=tags, gateway_tag_links=tag_links)


async def claimed(sim: Simulator, host: Any) -> tuple[Any, Any]:
    auth, worker = Authority.new(), Worker()
    assert (await v2host.claim(host, auth, worker))["status"] == Status.OK
    return auth, worker


async def open_radio(host: Any, tag_id: int, *, mode: TunnelMode = TunnelMode.PAIR, duration_s: int = 60) -> Any:
    fields: dict[str, Any] = {"op_id": host.new_op_id(), "bridge": GATEWAY_ADDR, "tag_id": tag_id,
                              "duration_s": duration_s}
    if mode != TunnelMode.PAIR:
        fields["mode"] = int(mode)
    reply = await host.ok(SerialMsg.TUNNEL_OPEN, fields)
    return HostTunnel(host, reply["tunnel"], GATEWAY_ADDR, tag_id)


def test_the_caps_and_the_refusals_of_tunnels_on_the_gateways_radio() -> None:
    async def scenario() -> None:
        async with Simulator(config(bridges=1)) as sim, V2Host(sim.gateway_url) as host:
            hello = await host.hello()
            assert hello["caps"]["tag_links"] == 2
            await claimed(sim, host)
            tag_id = sim.config.tags[0].tag_id

            async def refused(fields: dict[str, Any]) -> Status:
                reply = await host.call(SerialMsg.TUNNEL_OPEN, {"op_id": host.new_op_id(), "duration_s": 30,
                                                                "tag_id": tag_id, "bridge": GATEWAY_ADDR, **fields})
                return Status(reply["status"])

            assert await refused({"tag_id": 0}) == Status.INVALID  # the gateway's own endpoint is the serial port
            assert await refused({"duration_s": 0}) == Status.INVALID
            assert await refused({"duration_s": 256}) == Status.INVALID
            assert await refused({"mode": 7}) == Status.INVALID
            # A SESSION tunnel runs on the gateway's own radio only.
            assert await refused({"bridge": 2, "mode": int(TunnelMode.SESSION)}) == Status.INVALID
            first = await open_radio(host, tag_id)
            assert await refused({}) == Status.BUSY  # one tunnel per tag
            # Nothing can be sent before the tag answered.
            reply = await host.call(SerialMsg.TUNNEL_SEND, {"tunnel": first.tunnel, "data": b"\x01"})
            assert reply["status"] == Status.BUSY
            reply = await host.call(SerialMsg.TUNNEL_CLOSE, {"tunnel": first.tunnel})
            assert reply["status"] == Status.OK
            assert host.count(SerialMsg.EVT_TUNNEL, tunnel=first.tunnel, state=TunnelState.CLOSED) == 0  # silent

    run_scenario(scenario())


def test_a_gateway_without_tag_links_refuses_its_own_radio() -> None:
    async def scenario() -> None:
        async with Simulator(config(tag_links=0)) as sim, V2Host(sim.gateway_url) as host:
            assert "tag_links" not in (await host.hello())["caps"]
            await claimed(sim, host)
            reply = await host.call(SerialMsg.TUNNEL_OPEN, {"op_id": host.new_op_id(), "bridge": GATEWAY_ADDR,
                                                            "tag_id": sim.config.tags[0].tag_id, "duration_s": 30})
            assert reply["status"] == Status.UNSUPPORTED
            reply = await host.call(SerialMsg.DISCOVER, {"op_id": host.new_op_id(), "bridge": GATEWAY_ADDR,
                                                         "duration_s": 30, "tag_id": 0})
            assert reply["status"] == Status.NOT_FOUND

    run_scenario(scenario())


def test_the_gateways_radio_discovers_setup_mode_tags_and_stops_when_asked() -> None:
    async def scenario() -> None:
        async with Simulator(config(v2_tags=2)) as sim, V2Host(sim.gateway_url) as host:
            await claimed(sim, host)
            first, second = (t.tag_id for t in sim.config.tags)
            await host.ok(SerialMsg.DISCOVER, {"op_id": host.new_op_id(), "bridge": GATEWAY_ADDR, "duration_s": 100,
                                               "tag_id": first}, expect=Status.ACCEPTED)
            found = await host.wait_event(SerialMsg.EVT_DISCOVERED, tag_id=first, timeout=30)
            assert found.fields["bridge"] == GATEWAY_ADDR and found.fields["flags"] & ADV_FLAG_SETUP
            assert -128 <= found.fields["rssi"] < 0
            tag = sim.tag(first)
            wakes = tag.stats["wakes"]
            await asyncio.wait_for(_until(lambda: tag.stats["wakes"] >= wakes + 2), 30)
            reports = host.count(SerialMsg.EVT_DISCOVERED, tag_id=first)
            assert 1 <= reports <= tag.stats["wakes"] - wakes + 1  # at most once per window
            assert host.count(SerialMsg.EVT_DISCOVERED, tag_id=second) == 0  # the tag_id filter
            await host.ok(SerialMsg.DISCOVER, {"op_id": host.new_op_id(), "bridge": GATEWAY_ADDR, "duration_s": 0,
                                               "tag_id": 0}, expect=Status.ACCEPTED)
            await asyncio.sleep(0.05)
            before = host.count(SerialMsg.EVT_DISCOVERED)
            wakes = tag.stats["wakes"]
            await asyncio.wait_for(_until(lambda: tag.stats["wakes"] >= wakes + 2), 30)
            assert host.count(SerialMsg.EVT_DISCOVERED) == before

    run_scenario(scenario())


def test_a_tag_pairs_through_a_pair_tunnel_on_the_gateways_radio() -> None:
    async def scenario() -> None:
        async with Simulator(config()) as sim, V2Host(sim.gateway_url) as host:
            auth, worker = await claimed(sim, host)
            spec = sim.config.tags[0]
            tunnel = await open_radio(host, spec.tag_id, duration_s=90)
            opened = await tunnel.event(30)
            assert opened["state"] == TunnelState.OPEN and opened["bridge"] == GATEWAY_ADDR
            assert -128 <= opened["rssi"] < 0  # the advertisement the gateway connected on
            from app.tags.runtime.protocol.msgs import Ident2

            tunnel.ident = Ident2.unpack(opened["data"])
            assert (tunnel.ident.role, tunnel.ident.device_id) == (NodeRole.TAG, sim.tag(spec.tag_id).secure.device_id)
            await tunnel.handshake(worker)
            code = next(c["code"] for c in sim.setup_codes() if c["role"] == "tag")
            secret = parse_code(code, role=NodeRole.TAG).secret
            assert await tunnel.pair(auth, worker, secret, os.urandom(32)) == (Status.OK, True)
            await tunnel.close()
            assert sim.tag(spec.tag_id).secure.record.state == OwnerState.OWNED
            radio = sim.gateway.radio
            assert radio is not None and radio.counters["connections"] == 1 and radio.counters["suspend_count"] >= 1

    run_scenario(scenario())


def test_a_session_tunnel_relays_a_tags_frame_session_messages() -> None:
    async def scenario() -> None:
        async with Simulator(config(v2_tags=0, v1_tags=1)) as sim, V2Host(sim.gateway_url) as host:
            await claimed(sim, host)
            spec = sim.config.tags[0]
            tunnel = await open_radio(host, spec.tag_id, mode=TunnelMode.SESSION, duration_s=90)
            opened = await tunnel.event(30)
            assert opened["state"] == TunnelState.OPEN
            caps = TagCaps.unpack(opened["data"])  # exactly the 18 CAPS bytes the transcript binds
            assert (caps.tag_id, caps.width, caps.height) == (spec.tag_id, spec.width, spec.height)

            async def send(data: bytes) -> Status:
                reply = await host.call(SerialMsg.TUNNEL_SEND, {"tunnel": tunnel.tunnel, "data": data})
                return Status(reply["status"])

            assert await send(b"\xc0\x01") == Status.INVALID  # neither a CTRL message nor a DATA record
            assert await send(bytes([CtrlMsg.HELLO]) + bytes(TAG_CTRL_MSG_MAX)) == Status.TOO_LARGE
            hello = bytes([CtrlMsg.HELLO]) + CtrlHello(PROTO_VERSION, spec.tag_id, 1, os.urandom(16)).pack()
            assert await send(hello) == Status.OK
            reply = await tunnel.recv(30)  # the tag's CHALLENGE, reassembled from its CTRL indications
            assert reply[0] == CtrlMsg.CHALLENGE
            challenge = CtrlChallenge.unpack(reply[1:])
            assert challenge.proto == PROTO_VERSION
            await host.call(SerialMsg.TUNNEL_CLOSE, {"tunnel": tunnel.tunnel})
            # The tag saw a dropped link; it can be reached again.
            again = await open_radio(host, spec.tag_id, mode=TunnelMode.SESSION)
            assert (await again.event(30))["state"] == TunnelState.OPEN

    run_scenario(scenario())


def test_provisioning_and_configuration_wait_while_an_attempt_has_the_mesh_suspended() -> None:
    """gateway-firmware.md §16.4: PROVISION answers PROVISIONING_ACTIVE (transient: not remembered), and a node's
    configuration is accepted but waits for the resume."""
    async def scenario() -> None:
        cfg = SimConfig(seed=SEED, time_scale=200, protocol=2, bridges=[BridgeSpec(configured=False)], tags=[],
                        gateway_tag_links=2)
        async with Simulator(cfg) as sim, V2Host(sim.gateway_url) as host:
            await claimed(sim, host)
            radio = sim.gateway.radio
            assert radio is not None
            addr = next(iter(sim.gateway.cdb))
            radio.suspended = True  # as while a connection attempt runs (protocol.md §5.2)
            radio.resumed.clear()
            try:
                provision = {"op_id": host.new_op_id(), "uuid": bytes(16)}
                await host.ok(SerialMsg.PROVISION, provision, expect=Status.PROVISIONING_ACTIVE)
                op = host.new_op_id()
                await host.ok(SerialMsg.CONFIGURE_NODE, {"op_id": op, "addr": addr, "relay": True, "ttl": 5},
                              expect=Status.ACCEPTED)
                await asyncio.sleep(0.3)  # CONFIGURE_MS is 10 ms at this time scale
                assert host.count(SerialMsg.EVT_NODE_CONFIGURED, op_id=op) == 0
            finally:
                radio.suspended = False
                radio.resumed.set()
            configured = await host.wait_event(SerialMsg.EVT_NODE_CONFIGURED, op_id=op, timeout=15)
            assert configured.fields["status"] == Status.OK
            await host.ok(SerialMsg.PROVISION, provision, expect=Status.ACCEPTED)  # the same op_id, taken now

    run_scenario(scenario())


def test_a_tag_not_reached_in_time_closes_the_tunnel() -> None:
    async def scenario() -> None:
        async with Simulator(config()) as sim, V2Host(sim.gateway_url) as host:
            await claimed(sim, host)
            spec = sim.config.tags[0]
            sim.tag(spec.tag_id).out_of_range = True
            tunnel = await open_radio(host, spec.tag_id, duration_s=5)
            assert await tunnel.wait_closed(30) == Status.TIMEOUT

    run_scenario(scenario())


async def _until(predicate: Any) -> None:
    while not predicate():
        await asyncio.sleep(0.01)
