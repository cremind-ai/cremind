"""The simulated gateway's own radio (docs/protocol.md §11; the firmware's apps/gateway/src/core/gw_radio.c).

A gateway with tag links (``caps.tag_links``, 2 on the nRF52840) also connects to tags itself. Here it is
one more scanner and central in the simulated air.

**Discovery.** ``DISCOVER`` with ``bridge`` 0 or ``GATEWAY_ADDR`` opens a window. While it is open, every v2
tag advertising setup mode becomes ``EVT_DISCOVERED {bridge: GATEWAY_ADDR}``, rate-limited with the bridges'
reports.

**Tunnels.** ``TUNNEL_OPEN`` with ``bridge`` ``GATEWAY_ADDR`` makes a tunnel that waits for the tag's
advertisement. The gateway connects under the §5.2 rules a bridge follows:

- one attempt at a time, each in its own mesh suspend window;
- the suspend rate limit, and the per-tag back-off with one quick retry after ``CONNECT_FAILED``;
- never while it provisions or configures a node;
- its own segmented sends finish first (at most 500 ms);
- at most ``links`` connections, and a further one only while every connected tunnel has nothing left to
  write.

Once connected it relays whole messages:

- ``PAIR``: ``IDENT`` goes up as ``OPEN``, then ``PAIR`` messages go both ways.
- ``SESSION``: ``CAPS`` goes up as ``OPEN``. Down go the host's ``CTRL`` messages and ``DATA`` records, the
  type byte picking the characteristic and at most 4 records per connection event. Up go the tag's ``CTRL``
  and ``STATUS`` messages.

``OPEN`` carries the RSSI of the advertisement the gateway connected on. A tunnel queues two messages
(``BUSY`` beyond that, and before ``OPEN``). It closes in these cases:

- ``TIMEOUT``: the tag was not reached in ``duration_s``, or nothing moved for that long once connected;
- ``DISCONNECTED``: the link dropped;
- ``INVALID``: a §5.3 violation;
- ``UNSUPPORTED``: a tag without the mode's characteristics.

``TUNNEL_CLOSE`` ends it silently. While the mesh is suspended for an attempt, mesh messages to the gateway
wait (lower-transport retransmissions), and so do its own sends.
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..protocol.fragments import Fragmenter, FragmentError, Reassembler
from ..protocol.ids import (
    BRIDGE_CONN_ATTEMPT_MS,
    BRIDGE_MAX_SUSPENDS_PER_MIN,
    BRIDGE_TAG_BACKOFF_MS,
    DISCOVER_MAX_S,
    GATEWAY_ADDR,
    PAIR_MSG_MAX,
    TAG_ADV_WINDOW_MS,
    TAG_CTRL_MSG_MAX,
    TAG_RECORD_WIRE_MAX,
    GattChr,
    SerialMsg,
    Status,
    TunnelMode,
    TunnelState,
)
from .radio import ADV_FLAG_SETUP, ADV_VERSION, ADV_VERSION_V2, Advert, Air, ConnectFailed, GattLink, LinkLost

if TYPE_CHECKING:
    from .gateway import SimGateway

CONN_INTERVAL_MS = 40.0  # 30–50 ms (§5.2 step 8)
RECORDS_PER_EVENT = 4
SEND_WAIT_MS = 500.0  # §5.2 step 3: own mesh sends first, else a later advertisement
QUEUED = 2  # messages a tunnel holds (protocol.md §11.3)
TUNNEL_GRACE_MS = 5000.0  # as the gateway's mesh tunnels
DISCOVER_GRACE_MS = 2000.0
RECV_SLICE_MS = 600_000.0  # a relay waits for the tag in slices; the idle timer ends a silent tunnel


@dataclass(eq=False)
class RadioTunnel:
    """One tunnel on the gateway's own radio."""

    tunnel: int
    tag_id: int
    mode: TunnelMode
    timeout_s: int
    until_ms: float  # waiting: the tag must be reached by then
    state: str = "waiting"  # waiting | connecting | open | closed
    last_ms: float = 0.0  # connected: the last message either way
    link: GattLink | None = None
    queue: deque[bytes] = field(default_factory=deque)
    queued: asyncio.Event = field(default_factory=asyncio.Event)
    writing: bool = False
    task: asyncio.Task[None] | None = None  # the attempt, then the relay
    watch: asyncio.Task[None] | None = None  # the waiting and idle timers

    @property
    def idle(self) -> bool:
        """Nothing left to write (another tag may be initiated beside it)."""
        return not self.queue and not self.writing


class SimGatewayRadio:
    """The simulated gateway's scanner and central (see the module docstring)."""

    def __init__(self, gateway: SimGateway, air: Air, *, links: int = 2, tunnels: int = 8) -> None:
        self.gateway = gateway
        self.air = air
        self.clock = gateway.clock
        self.links = links
        self.max_tunnels = tunnels
        self.name = "gateway"
        self.tunnels: dict[int, RadioTunnel] = {}
        self.suspended = False
        self.resumed = asyncio.Event()
        self.resumed.set()
        self.counters: Counter[str] = Counter()
        self._initiating: int | None = None
        self._suspend_times: deque[float] = deque()
        self._backoff_until: dict[int, float] = {}
        self._quick_retry_until: dict[int, float] = {}
        self._discover_until = 0.0
        self._discover_tag = 0
        air.scanners.append(self)

    # -- state -------------------------------------------------------------------------------

    def owns(self, tunnel: int) -> bool:
        return tunnel in self.tunnels

    def reset(self) -> None:
        """A reboot: tunnels, links, windows and back-offs are RAM (the gateway's tasks are cancelled already)."""
        for tunnel in self.tunnels.values():
            tunnel.state = "closed"
            if tunnel.link is not None:
                tunnel.link.disconnect("gateway rebooted")
        self.tunnels.clear()
        self.suspended = False
        self.resumed.set()
        self._initiating = None
        self._suspend_times.clear()
        self._backoff_until.clear()
        self._quick_retry_until.clear()
        self._discover_until = 0.0

    def _spawn(self, coro: Any, name: str) -> asyncio.Task[Any]:
        return self.gateway._tasks.spawn(coro, f"radio {name}")

    # -- serial requests (SimGateway) ------------------------------------------------------------

    def discover(self, duration_s: int, tag_id: int) -> None:
        """The own radio's discovery window (``duration_s`` 0 closes it)."""
        if duration_s > DISCOVER_MAX_S:
            return
        self._discover_until = (self.clock.now_ms() + duration_s * 1000.0 + DISCOVER_GRACE_MS) if duration_s else 0.0
        self._discover_tag = tag_id

    def open(self, f: dict[str, Any]) -> dict[str, Any]:
        tag_id, duration = f["tag_id"], f["duration_s"]
        try:
            mode = TunnelMode(f.get("mode", TunnelMode.PAIR))
        except ValueError:
            return {"status": Status.INVALID, "text": "unknown tunnel mode"}
        if tag_id == 0:
            return {"status": Status.INVALID, "text": "a tag on the gateway's own radio needs its tag id"}
        if not 1 <= duration <= 0xFF:
            return {"status": Status.INVALID, "text": "duration_s is 1..255"}
        if any(t.tag_id == tag_id for t in self.tunnels.values()) or len(self.tunnels) >= self.max_tunnels:
            self.counters["busy"] += 1
            return {"status": Status.BUSY, "text": "a tunnel to this tag is open, or every one is taken"}
        now = self.clock.now_ms()
        tunnel = RadioTunnel(self.gateway._alloc_tunnel(), tag_id, mode, duration, now + duration * 1000.0)
        self.tunnels[tunnel.tunnel] = tunnel
        tunnel.watch = self._spawn(self._watch(tunnel), f"tunnel {tunnel.tunnel} timers")
        self.counters["tunnels"] += 1
        return {"status": Status.OK, "tunnel": tunnel.tunnel}

    def send(self, tunnel_id: int, data: bytes) -> dict[str, Any]:
        tunnel = self.tunnels[tunnel_id]
        if not data:
            return {"status": Status.INVALID, "text": "an empty tunnel message"}
        if tunnel.mode == TunnelMode.SESSION:
            kind = data[0]
            if 0x01 <= kind <= 0x0F:
                limit = TAG_CTRL_MSG_MAX
            elif 0x10 <= kind <= 0x2F:
                limit = TAG_RECORD_WIRE_MAX
            else:
                return {"status": Status.INVALID, "text": f"message type {kind:#04x} is neither CTRL nor DATA"}
        else:
            limit = PAIR_MSG_MAX
        if len(data) > limit:
            return {"status": Status.TOO_LARGE, "text": f"at most {limit} bytes"}
        if tunnel.state != "open" or len(tunnel.queue) >= QUEUED:
            self.counters["send_busy"] += 1
            return {"status": Status.BUSY}
        tunnel.queue.append(bytes(data))
        tunnel.queued.set()
        tunnel.last_ms = self.clock.now_ms()
        return {"status": Status.OK}

    def close(self, tunnel_id: int) -> dict[str, Any]:
        self._end(self.tunnels[tunnel_id], None)  # the host asked: no event
        self.counters["closed_by_host"] += 1
        return {"status": Status.OK}

    # -- scanning and initiating (§5.2) ------------------------------------------------------------

    def scanning(self) -> bool:
        return not self.suspended

    def on_advert(self, data: bytes, rssi: int) -> None:
        advert = Advert.parse(data, (ADV_VERSION, ADV_VERSION_V2))
        if advert is None:
            return
        tag_id, now = advert.tag_id, self.clock.now_ms()
        self.counters["adverts_seen"] += 1
        if (advert.version == ADV_VERSION_V2 and advert.flags & ADV_FLAG_SETUP and now < self._discover_until
                and self._discover_tag in (0, tag_id)):
            self.gateway._report_discovered(GATEWAY_ADDR, tag_id, rssi, advert.flags)
        tunnel = next((t for t in self.tunnels.values() if t.tag_id == tag_id and t.state == "waiting"), None)
        if tunnel is None:
            return
        reason = self._decision(tag_id, now)
        if reason is None or reason == "quick_retry":
            self._initiating = tag_id
            tunnel.state = "connecting"
            tunnel.task = self._spawn(self._attempt(tunnel, rssi, retry=reason == "quick_retry"),
                                      f"attempt {tag_id:08X}")

    def _decision(self, tag_id: int, now: float) -> str | None:
        """Whether an advertisement of a waiting tunnel's tag starts an attempt (§5.2 steps 1-2, §11.3)."""
        if self._initiating is not None:
            return "busy_initiating"
        connected = [t for t in self.tunnels.values() if t.state in ("connecting", "open")]
        if len(connected) >= self.links:
            return "busy_links"
        if any(not t.idle for t in connected):
            return "busy_streaming"
        if self.gateway._provisioning or self.gateway._node_ops:
            return "node_busy"
        retry = False
        if now < self._backoff_until.get(tag_id, 0.0):
            if now < self._quick_retry_until.get(tag_id, 0.0):
                retry = True
            else:
                self.counters["backoff_skips"] += 1
                return "backoff"
        while self._suspend_times and now - self._suspend_times[0] >= 60000.0:
            self._suspend_times.popleft()
        if len(self._suspend_times) >= BRIDGE_MAX_SUSPENDS_PER_MIN:
            self.counters["rate_limited"] += 1
            return "rate_limited"
        if retry:
            self._quick_retry_until.pop(tag_id, None)
            self.counters["quick_retries"] += 1
            return "quick_retry"
        return None

    def _backoff(self, tag_id: int, *, retry_allowed: bool) -> None:
        now = self.clock.now_ms()
        self._backoff_until[tag_id] = now + BRIDGE_TAG_BACKOFF_MS
        if retry_allowed:
            self._quick_retry_until[tag_id] = now + TAG_ADV_WINDOW_MS
        else:
            self._quick_retry_until.pop(tag_id, None)

    async def _attempt(self, tunnel: RadioTunnel, rssi: int, *, retry: bool) -> None:
        """One initiation (§5.2 steps 3-7) and, when it connects, the relay."""
        clock = self.clock
        try:
            lock = self.gateway._seg_lock
            if lock.locked():  # step 3: our own segmented send first, or a later advertisement
                deadline = clock.now_ms() + SEND_WAIT_MS
                while lock.locked() and clock.now_ms() < deadline:
                    await clock.sleep_ms(20.0)
                if lock.locked():
                    self.counters["deferred"] += 1
                    tunnel.state = "waiting" if tunnel.state == "connecting" else tunnel.state
                    return
            started = clock.now_ms()
            self._suspend_times.append(started)
            self.counters["suspend_count"] += 1
            self.suspended = True
            self.resumed.clear()
            link: GattLink | None = None
            try:
                link = await self.air.connect(self.name, tunnel.tag_id, BRIDGE_CONN_ATTEMPT_MS)
            except ConnectFailed:
                pass
            finally:
                self.suspended = False
                self.resumed.set()
            self.counters["suspend_max_ms"] = max(self.counters["suspend_max_ms"], int(clock.now_ms() - started))
            if self._initiating == tunnel.tag_id:
                self._initiating = None
            if tunnel.state != "connecting":  # closed meanwhile
                if link is not None:
                    link.disconnect("gateway closed the tunnel")
                return
            if link is None:
                self.counters["connect_failed"] += 1
                self._backoff(tunnel.tag_id, retry_allowed=not retry)
                tunnel.state = "waiting"
                return
            await self._relay(tunnel, link, rssi)
        finally:
            if self._initiating == tunnel.tag_id:
                self._initiating = None

    # -- the relay ------------------------------------------------------------------------------

    def _event(self, tunnel: RadioTunnel, state: TunnelState, **fields: Any) -> None:
        self.gateway._emit(SerialMsg.EVT_TUNNEL, {"tunnel": tunnel.tunnel, "bridge": GATEWAY_ADDR,
                                                  "tag_id": tunnel.tag_id, "state": state, **fields}, retained=False)

    async def _relay(self, tunnel: RadioTunnel, link: GattLink, rssi: int) -> None:
        clock = self.clock
        tunnel.link = link
        tunnel.state = "open"
        tunnel.last_ms = clock.now_ms()
        self.counters["connections"] += 1
        session = tunnel.mode == TunnelMode.SESSION
        down: asyncio.Task[None] | None = None
        try:
            try:
                value = await link.read(GattChr.CAPS if session else GattChr.IDENT)
            except ValueError:  # no such characteristic: not a tag of this mode (a v1 tag has no IDENT)
                self._end(tunnel, Status.UNSUPPORTED)
                return
            tunnel.last_ms = clock.now_ms()
            self._event(tunnel, TunnelState.OPEN, data=value, rssi=max(-128, min(127, rssi)))
            down = self._spawn(self._pump_down(tunnel, link), f"tunnel {tunnel.tunnel} down")
            rx = {GattChr.CTRL: Reassembler(TAG_CTRL_MSG_MAX), GattChr.STATUS: Reassembler(TAG_RECORD_WIRE_MAX)} \
                if session else {GattChr.PAIR: Reassembler(PAIR_MSG_MAX)}
            while tunnel.state == "open":
                chr_, value = await link.central_recv(clock, RECV_SLICE_MS)
                reassembler = rx.get(chr_)
                if reassembler is None:
                    raise FragmentError(f"unexpected value on {chr_!r}")
                message = reassembler.feed(value)
                if message is not None and tunnel.state == "open":
                    tunnel.last_ms = clock.now_ms()
                    self.counters["messages_up"] += 1
                    self._event(tunnel, TunnelState.DATA, data=message)
        except LinkLost:
            self._end(tunnel, Status.DISCONNECTED)
        except TimeoutError:
            self._end(tunnel, Status.TIMEOUT)
        except FragmentError:
            self._end(tunnel, Status.INVALID)
        finally:
            if down is not None:
                down.cancel()
            link.disconnect("gateway ended the relay")

    async def _pump_down(self, tunnel: RadioTunnel, link: GattLink) -> None:
        """The host's messages to the tag, in order, fragmented per characteristic (§5.3)."""
        tx = {GattChr.CTRL: Fragmenter(TAG_CTRL_MSG_MAX), GattChr.DATA: Fragmenter(TAG_RECORD_WIRE_MAX),
              GattChr.PAIR: Fragmenter(PAIR_MSG_MAX)}
        records = 0
        try:
            while True:
                while not tunnel.queue:
                    tunnel.queued.clear()
                    await tunnel.queued.wait()
                message = tunnel.queue[0]
                tunnel.writing = True
                if tunnel.mode == TunnelMode.PAIR:
                    chr_ = GattChr.PAIR
                else:
                    chr_ = GattChr.CTRL if message[0] < 0x10 else GattChr.DATA
                for value in tx[chr_].split(message):
                    await link.write(chr_, value)
                tunnel.queue.popleft()
                tunnel.writing = False
                tunnel.last_ms = self.clock.now_ms()
                self.counters["messages_down"] += 1
                if chr_ == GattChr.DATA:
                    records += 1
                    if records % RECORDS_PER_EVENT == 0:  # at most 4 records per connection event
                        await self.clock.sleep_ms(CONN_INTERVAL_MS)
        except LinkLost:
            return
        except FragmentError:
            self._end(tunnel, Status.INVALID)

    async def _watch(self, tunnel: RadioTunnel) -> None:
        """Not reached within ``duration_s``, or silent that long once connected: ``TIMEOUT``."""
        clock = self.clock
        while tunnel.state != "closed":
            now = clock.now_ms()
            if tunnel.state == "waiting":
                if now >= tunnel.until_ms:
                    self._end(tunnel, Status.TIMEOUT)
                    return
                await clock.sleep_ms(min(tunnel.until_ms - now, 1000.0))
            elif tunnel.state == "connecting":
                await clock.sleep_ms(100.0)  # the attempt is bounded; its outcome decides
            else:
                remaining = tunnel.last_ms + tunnel.timeout_s * 1000.0 + TUNNEL_GRACE_MS - now
                if remaining <= 0:
                    self._end(tunnel, Status.TIMEOUT)
                    return
                await clock.sleep_ms(remaining)

    def _end(self, tunnel: RadioTunnel, status: Status | None) -> None:
        """Forget the tunnel, drop its link, and tell the host (``EVT_TUNNEL CLOSED``) unless it asked."""
        if tunnel.state == "closed":
            return
        tunnel.state = "closed"
        self.tunnels.pop(tunnel.tunnel, None)
        current = asyncio.current_task()
        for task in (tunnel.task, tunnel.watch):
            if task is not None and task is not current and not task.done():
                task.cancel()
        if tunnel.link is not None:
            tunnel.link.disconnect("gateway closed the tunnel")
        if self._initiating == tunnel.tag_id:
            self._initiating = None
        if status is not None:
            self.counters[f"closed_{status.name.lower()}"] += 1
            self._event(tunnel, TunnelState.CLOSED, status=status)


__all__ = ["RadioTunnel", "SimGatewayRadio"]
