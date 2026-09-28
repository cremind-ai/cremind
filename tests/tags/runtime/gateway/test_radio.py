"""Tags on the gateway's own radio (app/tags/runtime/gateway/radio.py, docs/protocol.md §11) against the simulator.

A v2 gateway with tag links and no bridge at all serves a tag itself: the host assigns it (``K_epoch`` never
leaves the host), clears it, renders and delivers screens through ``SESSION`` tunnels, and every outcome
comes back as the ``AssignResult`` / ``StageEvent`` / ``ResultEvent`` a bridge's report would have been.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from app.tags.runtime.fontpack.format import FontPack
from app.tags.runtime.gateway.client import LOCAL_BOOT, GatewayClient
from app.tags.runtime.gateway.events import AssignResult, GatewayEvent, ResultEvent, StageEvent, TagSeen
from app.tags.runtime.gateway.link import SecureOptions
from app.tags.runtime.gateway.radio import GatewayRadio
from app.tags.runtime.protocol.ids import (
    GATEWAY_ADDR,
    RESULT_FLAG_DUPLICATE,
    Color,
    DeliveryStage,
    GrantOp,
    NodeRole,
    Status,
    TagCommand,
)
from app.tags.runtime.protocol.layout import Glyph, Glyphs, Icon, Layout, Rect, encode_layout
from app.tags.runtime.protocol.session import derive_k_epoch
from app.tags.runtime.render.reference import Panel, render_frame
from app.tags.runtime.secure import grants, identity
from app.tags.runtime.sim import BridgeSpec, SimConfig, Simulator, TagSpec

pytestmark = pytest.mark.timeout(120)

REPO = Path(__file__).resolve().parents[4]
FIXTURES = REPO / "app" / "tags" / "runtime" / "protocol" / "pinned" / "fixtures"
SEED = 71
OWNER = b"\x22" * 16


def card(variant: int = 0) -> bytes:
    """A 400x300 status card on the fixture pack's 24 px strikes; each variant has its own digest."""
    glyphs = tuple(Glyph(2 + (i % 17), 14 if i else 0, 0) for i in range(6))
    commands = (Rect(4 + variant, 4, 392 - 2 * variant, 292, 2, Color.BLACK), Icon(2, 24, Color.BLACK, 16, 16),
                Glyphs(1, 24, Color.BLACK, 52, 36, glyphs), Rect(16, 250, 200 + 10 * variant, 12, 0, Color.BLACK))
    return encode_layout(Layout(400, 300, 0, Color.WHITE, commands))


@dataclass
class Rig:
    sim: Simulator
    client: GatewayClient
    radio: GatewayRadio
    pack: FontPack
    events: list[GatewayEvent] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def tags(self) -> list[TagSpec]:
        return self.sim.config.tags

    async def wait(self, predicate: Callable[[GatewayEvent], bool], timeout: float = 60.0) -> GatewayEvent:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for event in self.events:
                if predicate(event):
                    return event
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AssertionError(f"no matching event; saw {[type(e).__name__ for e in self.events]}")
            self.changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait(), remaining)

    async def result(self, update_id: int, timeout: float = 60.0) -> ResultEvent:
        event = await self.wait(lambda e: isinstance(e, ResultEvent) and e.update_id == update_id, timeout)
        assert isinstance(event, ResultEvent)
        return event

    async def assigned(self, op_id: int) -> AssignResult:
        event = await self.wait(lambda e: isinstance(e, AssignResult) and e.op_id == op_id)
        assert isinstance(event, AssignResult)
        return event

    async def assign(self, tag: TagSpec, epoch: int = 1) -> None:
        op = self.client.new_op_id()
        ack = await self.client.assign_tag(GATEWAY_ADDR, tag.tag_id, epoch, derive_k_epoch(tag.secret, tag.tag_id,
                                                                                          epoch), op_id=op)
        assert ack.status == Status.ACCEPTED, ack
        assert (await self.assigned(op)).status == Status.OK

    async def deliver(self, tag: TagSpec, layout: bytes, revision: int, *, epoch: int = 1,
                      update_id: int | None = None) -> int:
        update = update_id if update_id is not None else self.client.new_op_id()
        ack = await self.client.deliver_layout(bridge=GATEWAY_ADDR, tag_id=tag.tag_id, epoch=epoch,
                                               revision=revision, update_id=update, fontpack_id=self.pack.pack_id,
                                               layout=layout, op_id=update)
        assert ack.status == Status.ACCEPTED, ack
        return update

    def expected_digest(self, tag: TagSpec, layout: bytes) -> bytes:
        return render_frame(layout, Panel(tag.width, tag.height, tag.planes, tag.plane_flags), self.pack).digest


async def _claim(client: GatewayClient, controller_pub: bytes) -> None:
    """connect-setup.md §8.1: CLAIM the unowned gateway with a grant for this controller key."""
    ident = client.identity
    assert ident is not None
    status = await client.status()
    sk, pub = identity.ed25519_generate()
    raw = grants.Grant(GrantOp.CLAIM, ident.device_id, NodeRole.GATEWAY, pub, OWNER, controller_pub,
                       int(status["gen"]), int(status["gen"]) + 1, bytes(status["challenge"])).encode()
    answer = await client.claim(raw, grants.sign(raw, sk))
    assert answer["status"] == Status.OK, answer


@contextlib.asynccontextmanager
async def rig(*, tag_links: int = 2, tags: int = 1, bridges: int = 0, time_scale: float = 50.0,
              **radio_options: Any) -> AsyncIterator[Rig]:
    pack_bytes = (FIXTURES / "fontpack_test.ctfp").read_bytes()
    pack = FontPack(pack_bytes)
    specs = [TagSpec.generate(SEED, i) for i in range(tags)]
    config = SimConfig(seed=SEED, time_scale=time_scale, protocol=2, fontpack=pack_bytes,
                       bridges=[BridgeSpec(provisioned=False) for _ in range(bridges)], tags=specs,
                       gateway_tag_links=tag_links)
    panels = {t.tag_id: Panel(t.width, t.height, t.planes, t.plane_flags) for t in specs}
    async with Simulator(config) as sim:
        gw = sim.gateway.secure
        assert gw is not None
        priv, pub = identity.x25519_generate()
        client = GatewayClient(sim.gateway_url, reconnect=True, request_timeout=2.0,
                               secure=SecureOptions(priv, expect_device_id=gw.device_id, expect_ik=gw.keys.ik_pub,
                                                    role=NodeRole.GATEWAY))
        radio = GatewayRadio(client, glyphs=lambda: pack, pack_id=lambda: pack.pack_id,
                             panel_of=panels.get, retry_pause_s=0.05, **radio_options)
        client.radio = radio
        r = Rig(sim, client, radio, pack)

        async def record(event: GatewayEvent) -> None:
            r.events.append(event)
            r.changed.set()

        client.add_event_handler(record)
        try:
            await client.connect()
            await _claim(client, pub)
            yield r
        finally:
            await client.close()


def run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, 110))


def test_the_gateway_alone_assigns_clears_and_shows_a_screen() -> None:
    async def scenario() -> None:
        async with rig() as r:
            tag = r.tags[0]
            assert r.client.hello_info is not None and r.client.hello_info.caps.tag_links == 2
            assert r.radio.enabled
            await r.assign(tag)
            assert r.radio.capacity() == {"max_tags": 20, "assigned": 1}

            clear = r.client.new_op_id()
            ack = await r.client.tag_command(bridge=GATEWAY_ADDR, tag_id=tag.tag_id, epoch=1, cmd=TagCommand.CLEAR,
                                             op_id=clear)
            assert ack.status == Status.ACCEPTED
            cleared = await r.result(clear)
            assert cleared.status == Status.OK and cleared.bridge == GATEWAY_ADDR and cleared.boot_id == LOCAL_BOOT
            assert cleared.stored_epoch == 1  # the tag authenticated epoch 1 and persisted it

            layout = card(1)
            update = await r.deliver(tag, layout, 1)
            result = await r.result(update)
            assert result.status == Status.OK, result
            assert result.digest == r.expected_digest(tag, layout)[:8]
            assert result.timing.refresh_ms > 0 and result.stored_epoch == 1
            assert r.sim.tag(tag.tag_id).displayed_digest == r.expected_digest(tag, layout)
            stages = [e.stage for e in r.events if isinstance(e, StageEvent) and e.update_id == update]
            assert stages[:2] == [DeliveryStage.BRIDGE_RECEIVED, DeliveryStage.TRANSFERRING]
            assert DeliveryStage.REFRESHING in stages
            seen = [e for e in r.events if isinstance(e, TagSeen) and e.tag_id == tag.tag_id]
            assert seen and seen[-1].bridge == GATEWAY_ADDR and seen[-1].battery_mv > 0 and seen[-1].rssi < 0
            # No mesh at all: the gateway connected to the tag itself.
            assert r.sim.gateway.radio is not None and r.sim.gateway.radio.counters["connections"] >= 2

            # The same revision again (a result the host lost): the tag answers from its stored ACK.
            again = await r.deliver(tag, layout, 1)
            repeat = await r.result(again)
            assert repeat.status == Status.OK and repeat.flags & RESULT_FLAG_DUPLICATE

    run(scenario())


def test_refusals_come_back_as_a_bridge_would_report_them() -> None:
    async def scenario() -> None:
        async with rig() as r:
            tag = r.tags[0]
            # Not assigned yet: the delivery ends NOT_ASSIGNED; a command too.
            update = await r.deliver(tag, card(), 1)
            assert (await r.result(update)).status == Status.NOT_ASSIGNED
            op = r.client.new_op_id()
            await r.client.tag_command(bridge=GATEWAY_ADDR, tag_id=tag.tag_id, epoch=1, cmd=TagCommand.CLEAR,
                                       op_id=op)
            assert (await r.result(op)).status == Status.NOT_ASSIGNED
            await r.assign(tag, epoch=3)
            # An older epoch: STALE_EPOCH, for an assignment and a delivery alike.
            op = r.client.new_op_id()
            await r.client.assign_tag(GATEWAY_ADDR, tag.tag_id, 2, derive_k_epoch(tag.secret, tag.tag_id, 2), op_id=op)
            assert (await r.assigned(op)).status == Status.STALE_EPOCH
            update = await r.deliver(tag, card(), 1, epoch=2)
            assert (await r.result(update)).status == Status.STALE_EPOCH
            # Another font pack, and a layout whose strike the pack lacks.
            update = r.client.new_op_id()
            await r.client.deliver_layout(bridge=GATEWAY_ADDR, tag_id=tag.tag_id, epoch=3, revision=1,
                                          update_id=update, fontpack_id=b"\x01" * 8, layout=card(), op_id=update)
            assert (await r.result(update)).status == Status.FONTPACK_MISMATCH
            odd = encode_layout(Layout(400, 300, 0, Color.WHITE, (Glyphs(1, 32, Color.BLACK, 5, 40, (Glyph(2, 0, 0),)),)))
            update = await r.deliver(tag, odd, 1, epoch=3)
            assert (await r.result(update)).status == Status.FONTPACK_MISMATCH
            # IDENTIFY / REFRESH are companion-level: UNSUPPORTED at once.
            op = r.client.new_op_id()
            await r.client.tag_command(bridge=GATEWAY_ADDR, tag_id=tag.tag_id, epoch=3, cmd=TagCommand.IDENTIFY,
                                       op_id=op)
            assert (await r.result(op)).status == Status.UNSUPPORTED
            # A repeated op id does no new work: the remembered answer with DUPLICATE.
            ack = await r.client.tag_command(bridge=GATEWAY_ADDR, tag_id=tag.tag_id, epoch=3,
                                             cmd=TagCommand.IDENTIFY, op_id=op)
            assert ack.duplicate and ack.status == Status.ACCEPTED
            assert sum(1 for e in r.events if isinstance(e, ResultEvent) and e.update_id == op) == 1

    run(scenario())


def test_a_newer_layout_supersedes_a_waiting_one_and_unassign_cancels() -> None:
    async def scenario() -> None:
        async with rig() as r:
            tag = r.tags[0]
            await r.assign(tag)
            first = await r.deliver(tag, card(1), 1)
            second = await r.deliver(tag, card(2), 2)
            superseded = await r.result(first)
            assert superseded.status in (Status.SUPERSEDED, Status.OK)  # OK only if the tag woke in between
            shown = await r.result(second)
            assert shown.status == Status.OK
            assert r.sim.tag(tag.tag_id).displayed_digest == r.expected_digest(tag, card(2))
            # An older revision than one already waiting (or shown) is refused by the tag or here.
            waiting = await r.deliver(tag, card(3), 5)
            op = r.client.new_op_id()
            ack = await r.client.unassign_tag(GATEWAY_ADDR, tag.tag_id, 1, op_id=op)
            assert ack.status == Status.ACCEPTED and (await r.assigned(op)).status == Status.OK
            ended = await r.result(waiting)
            assert ended.status in (Status.CANCELLED, Status.OK)  # OK only if its session had begun
            assert r.radio.capacity()["assigned"] == 0

    run(scenario())


def test_a_gateway_without_tag_links_answers_not_found() -> None:
    async def scenario() -> None:
        async with rig(tag_links=0) as r:
            tag = r.tags[0]
            assert r.client.hello_info is not None and r.client.hello_info.caps.tag_links == 0
            assert not r.radio.enabled
            ack = await r.client.assign_tag(GATEWAY_ADDR, tag.tag_id, 1, derive_k_epoch(tag.secret, tag.tag_id, 1))
            assert ack.status == Status.NOT_FOUND
            ack = await r.client.deliver_layout(bridge=GATEWAY_ADDR, tag_id=tag.tag_id, epoch=1, revision=1,
                                                update_id=7, fontpack_id=r.pack.pack_id, layout=card())
            assert ack.status == Status.NOT_FOUND

    run(scenario())


def test_two_tags_share_the_gateways_radio() -> None:
    async def scenario() -> None:
        async with rig(tags=3) as r:
            for tag in r.tags:
                await r.assign(tag)
            updates = {tag.tag_id: await r.deliver(tag, card(i), 1) for i, tag in enumerate(r.tags)}
            for i, tag in enumerate(r.tags):
                result = await r.result(updates[tag.tag_id], timeout=90)
                assert result.status == Status.OK, (tag.tag_id, result)
                assert r.sim.tag(tag.tag_id).displayed_digest == r.expected_digest(tag, card(i))

    run(scenario())
