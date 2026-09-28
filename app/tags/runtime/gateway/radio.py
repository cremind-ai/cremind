"""The gateway's own radio as a bridge (docs/protocol.md §11).

A gateway whose ``HELLO`` caps report ``tag_links`` connects to the tags in its
range itself and relays whole messages (``SESSION`` tunnels on ``bridge``
``GATEWAY_ADDR``). The bridge side of those tags runs here, in the host:

- **Assignments.** ``K_epoch`` per tag, which never leaves the host. The rules
  of a bridge's ``ASSIGN_SET`` / ``ASSIGN_DEL`` apply: an older epoch is
  ``STALE_EPOCH``, a newer one cancels the older epoch's jobs, and at most
  ``max_tags`` tags are held.
- **Jobs.** Layouts and tag commands per tag, in arrival order. A layout is
  checked as a bridge checks it at ``LAYOUT_COMMIT``: the assignment and its
  epoch, pending revisions, the font pack, and the layout's bounds and strikes.
  It replaces an older pending layout of the tag, which ends ``SUPERSEDED``,
  and is rendered ahead from the tag's recorded geometry, so the session
  streams at once.
- **Sessions.** While a tag has jobs, a ``SESSION`` tunnel waits for it on the
  gateway's radio. Once the tunnel opens with the tag's CAPS value, the
  bridge's session runs over it:
  - the handshake, with CAPS bound into the transcript;
  - credits counted per record;
  - ``FRAME_BEGIN`` / ``PLANE_DATA`` / ``FRAME_END`` or ``CMD``, then the tag's
    ``RESULT``.

  The frame is rendered again when the CAPS geometry differs from the recorded
  one. Every §10 rule applies: step deadlines move only on progress, the
  absolute handshake and frame bounds hold, an unauthenticated status ends the
  jobs only after 3 sessions, ``DISPLAY_STATE_UNKNOWN`` is resolved, and the
  tag's stored ACK is honoured.
- **Results.** Exactly one per ``update_id``. Each is the ``ResultEvent`` /
  ``AssignResult`` / ``StageEvent`` a bridge's report would have become, and it
  goes through the client's ordered event pipeline
  (:meth:`~app.tags.runtime.gateway.client.GatewayClient.inject`). A session
  that authenticated adds a ``TagSeen`` (battery, RSSI).

To the rest of the runtime the radio is one more bridge, at ``GATEWAY_ADDR``:
:class:`~app.tags.runtime.gateway.client.GatewayClient` routes ``ASSIGN_TAG``,
``UNASSIGN_TAG``, ``DELIVER_LAYOUT``, ``CANCEL_DELIVERY`` and ``TAG_COMMAND``
for that address here. Everything is held in RAM, unlike on a bridge. The
daemon loads the assignments again at start, and re-delivers what it sent here
before a restart; the tag's own stored result keeps that idempotent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections import Counter, OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..protocol import session as crypto
from ..protocol.ids import (
    GATEWAY_ADDR,
    LAYOUT_HARD_MAX,
    MAX_TAGS_PER_BRIDGE,
    PROTO_VERSION,
    RESULT_FLAG_DUPLICATE,
    RESULT_FLAG_ESCALATED,
    SERIAL_IDEMPOTENCY_SLOTS,
    TAG_PLANE_DATA_MAX,
    CtrlMsg,
    DeliveryStage,
    PlainMsg,
    RecordDir,
    RecordType,
    SerialMsg,
    Status,
    TagCommand,
    TunnelMode,
)
from ..protocol.layout import LayoutError, check_strikes, decode_layout, layout_digest
from ..protocol.msgs import (
    CtrlAuth,
    CtrlAuthOk,
    CtrlChallenge,
    CtrlError,
    CtrlHello,
    MessageError,
    PlainCredit,
    RecCmd,
    RecFrameBegin,
    RecPlaneData,
    RecProgress,
    RecResult,
    TagCaps,
)
from ..render.reference import Frame, GlyphSource, Panel, render_frame
from .client import LOCAL_BOOT
from .errors import GatewayDisconnected, GatewayError, StatusError
from .events import AssignResult, ResultEvent, SessionStarted, StageEvent, TagSeen
from .results import Ack, Timing, status_name
from .tunnel import Tunnel, TunnelError

if TYPE_CHECKING:
    from .client import GatewayClient

log = logging.getLogger(__name__)

TUNNEL_S = 120
"""A ``SESSION`` tunnel waits this long for the tag's advertisement, then closes after this long without a message:
four wake periods, and more than the longest panel refresh."""
OPEN_GRACE_S = 15.0  # the gateway reports a tag it did not reach (TIMEOUT); this only covers a lost event
HANDSHAKE_S = 10.0  # CAPS -> AUTH_OK and the first CREDIT: a bridge's 5 s (§10) plus the serial relay
STEP_S = 10.0  # a step's deadline, moved only by progress (§10): a bridge's 5 s plus the relay
REFRESH_S = 60.0  # the longest panel refresh bound (§10, the bridge's RESULT_TIMEOUT)
RELAY_S = 30.0  # added to every absolute bound for the serial relay
RETRY_PAUSE_S = 1.0  # after a failed session, before the next tunnel (the tag also sleeps meanwhile)
BUSY_PAUSE_S = 1.0  # every own-radio tunnel of the gateway is taken
MAX_JOBS = 64  # layouts and commands waiting across the radio's tags: DELIVER_LAYOUT answers BUSY beyond
REMEMBERED_OPS = 4 * SERIAL_IDEMPOTENCY_SLOTS
REPORTED = 1024  # update_ids remembered for the one-result-per-update_id rule (§10)
UNAUTH_REPEATS = 3
"""Consecutive sessions ending with the same unauthenticated status before it ends the jobs (§10)."""
FINAL_TAG_ERRORS = frozenset({Status.AUTH_FAILED, Status.STALE_EPOCH, Status.VERSION_MISMATCH, Status.NOT_FOUND})
TRANSIENT = frozenset({Status.BUSY, Status.NO_RESOURCES, Status.PROVISIONING_ACTIVE})
TAG_CTRL = frozenset({CtrlMsg.CHALLENGE, CtrlMsg.AUTH_OK, CtrlMsg.ERROR})


@dataclass
class RadioAssignment:
    tag_id: int
    epoch: int
    key: bytes  # K_epoch
    # The unauthenticated status of the last sessions and how many in a row ended with it (§10).
    unauth_status: Status | None = None
    unauth_count: int = 0


@dataclass(eq=False)
class RadioJob:
    kind: str  # "layout" | "cmd"
    update_id: int
    tag_id: int
    epoch: int
    revision: int = 0
    layout: bytes = b""
    layout_digest: bytes = b""
    cmd: int = 0
    accepted_at: float = 0.0
    frame_end_sent: bool = False
    in_session: bool = False
    frame: Frame | None = None  # rendered ahead for `panel`
    panel: Panel | None = None
    render: asyncio.Task[None] | None = None


class _SessionEnd(Exception):
    """The session ended before its jobs (its ``status`` says why; ``OK`` = nothing left to do)."""


def _status(value: int) -> Status:
    """A tag's status byte; an unknown value is a protocol violation (``INVALID``)."""
    try:
        return Status(value)
    except ValueError:
        raise MessageError(f"unknown status {value}") from None


class GatewayRadio:
    """The bridge side of the tags on the gateway's own radio (see the module docstring)."""

    def __init__(self, client: GatewayClient, *, glyphs: Callable[[], GlyphSource | None],
                 pack_id: Callable[[], bytes | None], panel_of: Callable[[int], Panel | None] | None = None,
                 clock: Callable[[], float] = time.monotonic, rng: Callable[[int], bytes] = secrets.token_bytes,
                 tunnel_s: int = TUNNEL_S, retry_pause_s: float = RETRY_PAUSE_S, max_tags: int = MAX_TAGS_PER_BRIDGE,
                 max_jobs: int = MAX_JOBS) -> None:
        self.client = client
        self.glyphs = glyphs
        self.pack_id = pack_id
        self.panel_of = panel_of
        self.clock = clock
        self.rng = rng
        self.tunnel_s = max(1, min(int(tunnel_s), 255))
        self.retry_pause_s = retry_pause_s
        self.max_tags = max_tags
        self.max_jobs = max_jobs
        self.assignments: dict[int, RadioAssignment] = {}
        self.jobs: dict[int, list[RadioJob]] = {}
        self.counters: Counter[str] = Counter()
        self._by_update: dict[int, RadioJob] = {}
        self._acks: OrderedDict[int, Ack] = OrderedDict()
        self._reported: OrderedDict[int, None] = OrderedDict()
        self._workers: dict[int, asyncio.Task[None]] = {}
        self._seq = 0
        self._closed = False

    # -- state ------------------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """The connected gateway serves tags on its own radio (``caps.tag_links`` > 0)."""
        hello = self.client.hello_info
        return hello is not None and hello.caps.tag_links > 0

    def capacity(self) -> dict[str, int]:
        """Tags the radio can hold and holds (a bridge's ``max_tags`` / ``assigned``)."""
        return {"max_tags": self.max_tags, "assigned": len(self.assignments)}

    def owns(self, update_id: int) -> bool:
        """``update_id`` is a delivery or command waiting here."""
        return update_id in self._by_update

    def pending(self, tag_id: int | None = None) -> int:
        if tag_id is not None:
            return len(self.jobs.get(tag_id, []))
        return sum(len(jobs) for jobs in self.jobs.values())

    def load(self, tag_id: int, epoch: int, key: bytes) -> None:
        """An assignment this host made before a restart (the daemon reads its inventory at start)."""
        current = self.assignments.get(tag_id)
        if current is None or current.epoch <= epoch:
            self.assignments[tag_id] = RadioAssignment(tag_id, epoch, key)

    def on_session(self, event: SessionStarted) -> None:
        """A new gateway session: after a reboot every tunnel is gone, so each tag's worker starts over."""
        if not event.boot_changed:
            return
        for task in list(self._workers.values()):
            task.cancel()
        self._workers.clear()
        for tag_id in list(self.jobs):
            self._kick(tag_id)

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._workers.values())
        tasks += [j.render for jobs in self.jobs.values() for j in jobs if j.render is not None]
        self._workers.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task

    # -- requests (GatewayClient) -------------------------------------------------------------

    def _remembered(self, msg: SerialMsg, op_id: int) -> Ack | None:
        ack = self._acks.get(op_id)
        if ack is None:
            return None
        self.counters["duplicate_ops"] += 1
        return Ack(msg, ack.status, Status.DUPLICATE, ack.text, op_id,
                   {**ack.fields, "detail": int(Status.DUPLICATE)})

    def _answer(self, msg: SerialMsg, op_id: int, status: Status, text: str | None = None) -> Ack:
        fields: dict[str, Any] = {"status": int(status)}
        if text:
            fields["text"] = text
        ack = Ack(msg, status, None, text, op_id, fields)
        if status not in TRANSIENT:  # §10: a transient refusal is not remembered, so a retry can succeed
            self._acks[op_id] = ack
            self._acks.move_to_end(op_id)
            while len(self._acks) > REMEMBERED_OPS:
                self._acks.popitem(last=False)
        return ack

    def _unavailable(self, msg: SerialMsg, op_id: int) -> Ack:
        return self._answer(msg, op_id, Status.NOT_FOUND, "the gateway serves no tag on its own radio")

    def _check_link(self, msg: SerialMsg) -> None:
        """Like any request, one for the radio needs the gateway connected (its answer says what the radio can do)."""
        if not self.client.connected:
            raise GatewayDisconnected(f"{msg.name}: the gateway is not connected")

    def assign(self, op_id: int, tag_id: int, epoch: int, key: bytes) -> Ack:
        """``ASSIGN_TAG`` for the radio: answered at once, the outcome as an ``AssignResult``."""
        self._check_link(SerialMsg.ASSIGN_TAG)
        if (dup := self._remembered(SerialMsg.ASSIGN_TAG, op_id)) is not None:
            return dup
        if not self.enabled:
            return self._unavailable(SerialMsg.ASSIGN_TAG, op_id)
        status = self._assign_set(tag_id, epoch, key)
        ack = self._answer(SerialMsg.ASSIGN_TAG, op_id, Status.ACCEPTED)
        self._assign_result(op_id, tag_id, epoch, status)
        return ack

    def unassign(self, op_id: int, tag_id: int, epoch: int) -> Ack:
        """``UNASSIGN_TAG`` for the radio (a bridge's ``ASSIGN_DEL``: an absent assignment is ``OK``)."""
        self._check_link(SerialMsg.UNASSIGN_TAG)
        if (dup := self._remembered(SerialMsg.UNASSIGN_TAG, op_id)) is not None:
            return dup
        if not self.enabled:
            return self._unavailable(SerialMsg.UNASSIGN_TAG, op_id)
        current = self.assignments.get(tag_id)
        if current is None:
            status = Status.OK
        elif current.epoch > epoch:
            status = Status.STALE_EPOCH
        else:
            del self.assignments[tag_id]
            self._cancel_tag(tag_id, older_than=epoch + 1)
            status = Status.OK
        ack = self._answer(SerialMsg.UNASSIGN_TAG, op_id, Status.ACCEPTED)
        self._assign_result(op_id, tag_id, epoch, status)
        return ack

    async def deliver(self, op_id: int, *, tag_id: int, epoch: int, revision: int, update_id: int,
                      fontpack_id: bytes, layout: bytes) -> Ack:
        """``DELIVER_LAYOUT`` for the radio: ``ACCEPTED`` (stage ``GATEWAY_RECEIVED``), then the checks a bridge
        makes at ``LAYOUT_COMMIT`` — a refusal is the delivery's result — and the job."""
        msg = SerialMsg.DELIVER_LAYOUT
        self._check_link(msg)
        if (dup := self._remembered(msg, op_id)) is not None:
            return dup
        if not self.enabled:
            return self._unavailable(msg, op_id)
        if not layout:
            return self._answer(msg, op_id, Status.INVALID, "empty layout")
        if len(layout) > LAYOUT_HARD_MAX:
            return self._answer(msg, op_id, Status.TOO_LARGE)
        if self.pending() >= self.max_jobs:
            self.counters["busy"] += 1
            return self._answer(msg, op_id, Status.BUSY)
        ack = self._answer(msg, op_id, Status.ACCEPTED)
        self._accept_layout(tag_id=tag_id, epoch=epoch, revision=revision, update_id=update_id,
                            fontpack_id=bytes(fontpack_id), layout=bytes(layout))
        return ack

    def cancel(self, op_id: int, update_id: int) -> Ack:
        """``CANCEL_DELIVERY`` of a job waiting here: ``CANCELLED`` unless its session already has it."""
        msg = SerialMsg.CANCEL_DELIVERY
        self._check_link(msg)
        if (dup := self._remembered(msg, op_id)) is not None:
            return dup
        job = self._by_update.get(update_id)
        if job is None:
            return self._answer(msg, op_id, Status.NOT_FOUND)
        if job.in_session:
            return self._answer(msg, op_id, Status.ACCEPTED)  # its session ends it, as at a bridge
        self._finish(job, Status.CANCELLED)
        return self._answer(msg, op_id, Status.OK)

    def command(self, op_id: int, *, tag_id: int, epoch: int, cmd: int) -> Ack:
        """``TAG_COMMAND`` for the radio (``update_id`` = ``op_id``): a bridge's ``TAG_CMD`` rules."""
        msg = SerialMsg.TAG_COMMAND
        self._check_link(msg)
        if (dup := self._remembered(msg, op_id)) is not None:
            return dup  # a repeated command queues no second job
        if not self.enabled:
            return self._unavailable(msg, op_id)
        ack = self._answer(msg, op_id, Status.ACCEPTED)
        current = self.assignments.get(tag_id)
        if current is None or current.epoch < epoch:
            self._result(op_id, tag_id, epoch, 0, Status.NOT_ASSIGNED)
        elif current.epoch > epoch:
            self._result(op_id, tag_id, epoch, 0, Status.STALE_EPOCH)
        elif cmd not in (TagCommand.CLEAR, TagCommand.SLEEP):
            self._result(op_id, tag_id, epoch, 0, Status.UNSUPPORTED)  # IDENTIFY / REFRESH are companion-level
        else:
            self._add_job(RadioJob("cmd", op_id, tag_id, epoch, cmd=cmd, accepted_at=self.clock()))
        return ack

    # -- assignments and jobs ------------------------------------------------------------------

    def _assign_set(self, tag_id: int, epoch: int, key: bytes) -> Status:
        current = self.assignments.get(tag_id)
        if current is not None and current.epoch > epoch:
            return Status.STALE_EPOCH
        if current is None and len(self.assignments) >= self.max_tags:
            self.counters["assign_full"] += 1
            return Status.NO_RESOURCES
        if current is not None and current.epoch < epoch:
            self._cancel_tag(tag_id, older_than=epoch)
        self.assignments[tag_id] = RadioAssignment(tag_id, epoch, key)  # the unauthenticated count restarts
        self._kick(tag_id)
        return Status.OK

    def _cancel_tag(self, tag_id: int, older_than: int) -> None:
        for job in [j for j in self.jobs.get(tag_id, []) if j.epoch < older_than and not j.in_session]:
            self._finish(job, Status.CANCELLED)

    def _accept_layout(self, *, tag_id: int, epoch: int, revision: int, update_id: int, fontpack_id: bytes,
                       layout: bytes) -> None:
        """The checks of a bridge's ``LAYOUT_COMMIT`` (§3.3) after the transfer's own: the assignment, the revision,
        the font pack, the layout's bounds and strikes; a refusal ends the delivery with its status."""
        digest = layout_digest(layout)
        current = self.assignments.get(tag_id)
        if current is None or current.epoch < epoch:
            self._result(update_id, tag_id, epoch, revision, Status.NOT_ASSIGNED)
            return
        if current.epoch > epoch:
            self._result(update_id, tag_id, epoch, revision, Status.STALE_EPOCH)
            return
        for job in self.jobs.get(tag_id, []):
            if job.kind != "layout" or job.epoch != epoch:
                continue
            if job.revision > revision or (job.revision == revision and job.layout_digest != digest):
                self._result(update_id, tag_id, epoch, revision, Status.STALE_REVISION)
                return
            if job.revision == revision:  # the same revision sent again (a lost result): it adopts the new id
                self._by_update.pop(job.update_id, None)
                job.update_id = update_id
                self._by_update[update_id] = job
                self._stage(job, DeliveryStage.BRIDGE_RECEIVED)
                self._kick(tag_id)
                return
        pack, glyphs = self.pack_id(), self.glyphs()
        if pack is None or glyphs is None or bytes(pack) != fontpack_id:
            self._result(update_id, tag_id, epoch, revision, Status.FONTPACK_MISMATCH)
            return
        try:
            check_strikes(decode_layout(layout), glyphs.has_strike)
        except LayoutError as exc:
            self._result(update_id, tag_id, epoch, revision, exc.status)
            return
        job = RadioJob("layout", update_id, tag_id, epoch, revision, layout, digest, accepted_at=self.clock())
        for older in [j for j in self.jobs.get(tag_id, []) if j.kind == "layout" and not j.in_session]:
            self._finish(older, Status.SUPERSEDED)
        self._stage(job, DeliveryStage.BRIDGE_RECEIVED)
        self._add_job(job)
        if self.panel_of is not None:
            job.render = asyncio.get_running_loop().create_task(self._render_ahead(job),
                                                                name=f"radio render {tag_id:08X}")

    def _add_job(self, job: RadioJob) -> None:
        self.jobs.setdefault(job.tag_id, []).append(job)
        self._by_update[job.update_id] = job
        self._kick(job.tag_id)

    async def _render_ahead(self, job: RadioJob) -> None:
        """Render with the tag's recorded geometry now, so the session streams as soon as the tag connects."""
        assert self.panel_of is not None
        try:
            panel = await asyncio.to_thread(self.panel_of, job.tag_id)
            glyphs = self.glyphs()
            if panel is None or glyphs is None:
                return
            job.frame = await asyncio.to_thread(render_frame, job.layout, panel, glyphs)
            job.panel = panel
        except LayoutError:
            pass  # the session renders with the CAPS geometry and reports the status
        except Exception:
            log.exception("radio: rendering ahead for tag %08X failed", job.tag_id)

    async def _frame(self, job: RadioJob, panel: Panel) -> Frame:
        """The job's frame for the geometry the tag authenticated; rendered now when it differs."""
        if job.render is not None and not job.render.done():
            await asyncio.wait({job.render}, timeout=30)  # never raises the render's own outcome
        if job.frame is not None and job.panel == panel:
            return job.frame
        glyphs = self.glyphs()
        if glyphs is None:
            raise LayoutError(Status.FONTPACK_MISMATCH, "no font pack")
        frame = await asyncio.to_thread(render_frame, job.layout, panel, glyphs)
        job.frame, job.panel = frame, panel
        return frame

    def _unauth_status(self, tag_id: int, epoch: int, status: Status) -> bool:
        """Count an unauthenticated status; True when it is the ``UNAUTH_REPEATS``-th in a row (§10)."""
        a = self.assignments.get(tag_id)
        if a is None or a.epoch != epoch:
            return False  # the assignment changed meanwhile: nothing to end
        if a.unauth_count > 0 and a.unauth_status == status:
            a.unauth_count += 1
        else:
            a.unauth_status, a.unauth_count = status, 1
        if a.unauth_count < UNAUTH_REPEATS:
            return False
        a.unauth_count = 0
        self.counters["unauth_final"] += 1
        return True

    def _authenticated(self, tag_id: int, epoch: int, *, battery_mv: int, rssi: int | None) -> None:
        a = self.assignments.get(tag_id)
        if a is not None and a.epoch == epoch:
            a.unauth_count = 0
        self.client.inject(TagSeen(boot_id=LOCAL_BOOT, bridge=GATEWAY_ADDR, tag_id=tag_id,
                                   rssi=rssi if rssi is not None else 0, battery_mv=battery_mv, flags=0,
                                   raw={"bridge": GATEWAY_ADDR, "tag_id": tag_id, "rssi": rssi,
                                        "battery_mv": battery_mv, "flags": 0}))

    # -- results -----------------------------------------------------------------------------

    def _finish(self, job: RadioJob, status: Status, *, digest8: bytes = bytes(8), battery_mv: int = 0,
                timing: Timing | None = None, stored_epoch: int = 0, flags: int = 0) -> None:
        jobs = self.jobs.get(job.tag_id, [])
        if job in jobs:
            jobs.remove(job)
        if not jobs:
            self.jobs.pop(job.tag_id, None)
        if self._by_update.get(job.update_id) is job:
            del self._by_update[job.update_id]
        if job.render is not None and not job.render.done():
            job.render.cancel()
        self.counters[f"result_{status.name}"] += 1
        self._result(job.update_id, job.tag_id, job.epoch, job.revision, status, digest8=digest8,
                     battery_mv=battery_mv, timing=timing, stored_epoch=stored_epoch, flags=flags)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _result(self, update_id: int, tag_id: int, epoch: int, revision: int, status: Status, *,
                digest8: bytes = bytes(8), battery_mv: int = 0, timing: Timing | None = None, stored_epoch: int = 0,
                flags: int = 0) -> None:
        """Exactly one result per ``update_id`` (§10); a later one is dropped."""
        if update_id in self._reported:
            self.counters["repeated_results"] += 1
            return
        self._reported[update_id] = None
        while len(self._reported) > REPORTED:
            self._reported.popitem(last=False)
        timing = timing or Timing()
        digest8 = bytes(digest8[:8]).ljust(8, b"\x00")
        self.client.inject(ResultEvent(
            boot_id=LOCAL_BOOT, event_seq=self._next_seq(), update_id=update_id, bridge=GATEWAY_ADDR, tag_id=tag_id,
            epoch=epoch, revision=revision, status=status, digest=digest8, battery_mv=battery_mv, timing=timing,
            flags=flags, stored_epoch=stored_epoch,
            raw={"update_id": update_id, "bridge": GATEWAY_ADDR, "tag_id": tag_id, "epoch": epoch,
                 "revision": revision, "status": int(status), "digest": digest8, "battery_mv": battery_mv,
                 "timing": timing.as_dict(), "flags": flags, "stored_epoch": stored_epoch}))

    def _assign_result(self, op_id: int, tag_id: int, epoch: int, status: Status) -> None:
        self.counters[f"assign_{status.name}"] += 1
        self.client.inject(AssignResult(
            boot_id=LOCAL_BOOT, event_seq=self._next_seq(), op_id=op_id, bridge=GATEWAY_ADDR, tag_id=tag_id,
            epoch=epoch, status=status,
            raw={"op_id": op_id, "bridge": GATEWAY_ADDR, "tag_id": tag_id, "epoch": epoch, "status": int(status)}))

    def _stage(self, job: RadioJob, stage: DeliveryStage) -> None:
        self.client.inject(StageEvent(boot_id=LOCAL_BOOT, update_id=job.update_id, tag_id=job.tag_id,
                                      revision=job.revision, stage=stage,
                                      raw={"update_id": job.update_id, "tag_id": job.tag_id,
                                           "revision": job.revision, "stage": int(stage)}))

    # -- sessions ------------------------------------------------------------------------------

    def _wants(self, tag_id: int) -> bool:
        return bool(self.jobs.get(tag_id)) and tag_id in self.assignments

    def _kick(self, tag_id: int) -> None:
        """Start the tag's worker when it has jobs and none runs."""
        if self._closed or not self._wants(tag_id):
            return
        task = self._workers.get(tag_id)
        if task is None or task.done():
            self._workers[tag_id] = asyncio.get_running_loop().create_task(self._serve(tag_id),
                                                                          name=f"radio tag {tag_id:08X}")

    async def _serve(self, tag_id: int) -> None:
        """While the tag has jobs: a ``SESSION`` tunnel waits for it, then one session runs its jobs."""
        me = asyncio.current_task()
        try:
            while not self._closed and self._wants(tag_id):
                if not self.enabled or not self.client.connected:
                    await asyncio.sleep(self.retry_pause_s)
                    continue
                try:
                    tunnel = await Tunnel.open(self.client, bridge=GATEWAY_ADDR, tag_id=tag_id,
                                               duration_s=self.tunnel_s, mode=TunnelMode.SESSION)
                except StatusError as exc:
                    self.counters[f"open_{status_name(exc.status).lower()}"] += 1
                    await asyncio.sleep(BUSY_PAUSE_S if exc.status == Status.BUSY else self.retry_pause_s * 5)
                    continue
                except GatewayError as exc:
                    log.info("radio: tag %08X: no tunnel (%s)", tag_id, exc)
                    await asyncio.sleep(self.retry_pause_s)
                    continue
                ok = True
                try:
                    try:
                        caps = await tunnel.wait_opened(self.tunnel_s + OPEN_GRACE_S)
                    except TunnelError as exc:
                        self.counters["not_reached"] += 1  # asleep or out of range: wait for it again
                        log.debug("radio: tag %08X not reached (%s)", tag_id, exc)
                        continue
                    self.counters["sessions"] += 1
                    ok = await _Session(self, tunnel, tag_id, caps).guarded()
                finally:
                    await tunnel.close(say_goodbye=False)
                if not ok:
                    await asyncio.sleep(self.retry_pause_s)
        finally:
            if self._workers.get(tag_id) is me:
                del self._workers[tag_id]


class _Session:
    """The bridge side of one tag session over a ``SESSION`` tunnel (§5.4–§5.6, §10)."""

    def __init__(self, radio: GatewayRadio, tunnel: Tunnel, tag_id: int, caps_bytes: bytes) -> None:
        self.radio = radio
        self.tunnel = tunnel
        self.tag_id = tag_id
        self.caps_bytes = caps_bytes
        self.opened_at = radio.clock()
        self.sender: crypto.RecordSender | None = None
        self.receiver: crypto.RecordReceiver | None = None
        self.credits = 0
        self.credit_grants = 0
        self.ctrl_inbox: deque[bytes] = deque()
        self.status = Status.OK
        self.job: RadioJob | None = None
        self.refreshing_reported = False
        self.epoch = 0
        self.tag_epoch = 0  # the tag's stored epoch (CHALLENGE / ERROR; after AUTH_OK at least `epoch`)
        self.step_deadline = 0.0

    async def guarded(self) -> bool:
        """Run the session; True when it ended well (nothing to back off for)."""
        try:
            await self.run()
            self.radio.counters["sessions_ok"] += 1
            return True
        except _SessionEnd:
            ok = self.status == Status.OK
            self.radio.counters["sessions_ok" if ok else "sessions_fail"] += 1
            return ok
        except (TunnelError, TimeoutError) as exc:
            self.radio.counters["sessions_fail"] += 1
            log.info("radio: session with tag %08X ended: %s", self.tag_id, exc)
            return False
        except (MessageError, crypto.AuthError, ValueError) as exc:
            self.radio.counters["sessions_fail"] += 1
            self.radio.counters["protocol_errors"] += 1
            log.warning("radio: session with tag %08X failed: %s", self.tag_id, exc)
            return False

    # -- transport ------------------------------------------------------------------------------

    def _progress(self) -> None:
        """A complete handshake message, an authenticated record or a CREDIT with n > 0 (§10)."""
        self.step_deadline = self.radio.clock() + STEP_S

    async def send_record(self, record_type: RecordType, plaintext: bytes) -> None:
        assert self.sender is not None
        await self.tunnel.send(self.sender.seal(record_type, plaintext))
        self.credits -= 1

    async def pump(self, deadline: float, *, step: bool = True) -> RecResult | None:
        """One message from the tag; returns a RESULT once one is complete. ``step``: the step deadline applies
        too (not while the tag refreshes its panel: only the absolute bound does then)."""
        remaining = (min(deadline, self.step_deadline) if step else deadline) - self.radio.clock()
        if remaining <= 0:
            raise TimeoutError(f"tag {self.tag_id:08X}: no progress")
        message = await self.tunnel.recv(remaining)
        if not message:
            raise MessageError("an empty message")
        kind = message[0]
        if kind in TAG_CTRL:
            if kind == CtrlMsg.ERROR and self.receiver is not None:
                self.ctrl_error(message)  # established: a plaintext ERROR is still unauthenticated
            self.ctrl_inbox.append(message)
            return None
        if kind == PlainMsg.CREDIT:
            if self.receiver is None:
                raise MessageError("a CREDIT before AUTH_OK")  # §10: the session ends INVALID
            credits = PlainCredit.unpack(message[1:]).credits
            self.credits += credits
            self.credit_grants += 1
            if credits:
                self._progress()
            return None
        if self.receiver is None:
            raise MessageError(f"message {kind:#04x} before AUTH_OK")
        record_type, plaintext = self.receiver.open(message)
        self._progress()
        if record_type == RecordType.PROGRESS:
            job = self.job
            if job is not None and RecProgress.unpack(plaintext).stage == DeliveryStage.REFRESHING:
                if not self.refreshing_reported:
                    self.refreshing_reported = True
                    self.radio._stage(job, DeliveryStage.REFRESHING)
            return None
        if record_type == RecordType.RESULT:
            return RecResult.unpack(plaintext)
        raise MessageError(f"unexpected record type {record_type:#04x}")

    async def recv_ctrl(self, deadline: float) -> bytes:
        while not self.ctrl_inbox:
            await self.pump(deadline)
        self._progress()
        return self.ctrl_inbox.popleft()

    async def wait_credit(self, deadline: float) -> RecResult | None:
        while self.credits <= 0:
            result = await self.pump(deadline)
            if result is not None:
                return result
        return None

    async def wait_result(self, deadline: float) -> RecResult:
        """The tag's RESULT after FRAME_END or CMD (the panel refresh: no progress is due meanwhile)."""
        while True:
            result = await self.pump(deadline, step=False)
            if result is not None:
                return result

    def ctrl_error(self, message: bytes) -> None:
        """A tag ``ERROR{status, stored_epoch}``: note the stored epoch, then :meth:`tag_error`."""
        error = CtrlError.unpack(message[1:])
        self.tag_epoch = error.stored_epoch
        self.tag_error(_status(error.status))

    def tag_error(self, status: Status) -> None:
        """A status from outside an authenticated record (§10): unauthenticated, so it backs off like a link
        failure until it repeats in ``UNAUTH_REPEATS`` consecutive sessions for this tag and epoch; only then does
        it end the tag's jobs of that epoch, carrying the tag's stored epoch and ``RESULT_FLAG_ESCALATED``."""
        radio = self.radio
        self.status = status
        if status in FINAL_TAG_ERRORS:
            radio.counters["unauth_statuses"] += 1
            if radio._unauth_status(self.tag_id, self.epoch, status):
                for job in list(radio.jobs.get(self.tag_id, [])):
                    if job.epoch == self.epoch:
                        radio._finish(job, status, stored_epoch=self.tag_epoch, flags=RESULT_FLAG_ESCALATED)
        raise _SessionEnd

    def finish(self, job: RadioJob, status: Status, **kwargs: Any) -> None:
        """End a job from this session: its result carries the tag's stored epoch (§3.4)."""
        self.radio._finish(job, status, stored_epoch=self.tag_epoch, **kwargs)

    # -- the session ----------------------------------------------------------------------------

    async def run(self) -> None:
        radio = self.radio
        assignment = radio.assignments.get(self.tag_id)
        if assignment is None:
            raise _SessionEnd
        self.epoch = assignment.epoch
        handshake_deadline = radio.clock() + HANDSHAKE_S
        self._progress()
        caps = TagCaps.unpack(self.caps_bytes)  # the transcript binds exactly these bytes (§5.4)
        if caps.proto != PROTO_VERSION:  # CAPS is read before the handshake: unauthenticated statuses
            self.tag_error(Status.VERSION_MISMATCH)
        if caps.tag_id != self.tag_id:
            self.tag_error(Status.NOT_FOUND)
        hello = bytes([CtrlMsg.HELLO]) + CtrlHello(PROTO_VERSION, self.tag_id, self.epoch, radio.rng(16)).pack()
        await self.tunnel.send(hello)
        challenge_msg = await self.recv_ctrl(handshake_deadline)
        if challenge_msg[0] == CtrlMsg.ERROR:
            self.ctrl_error(challenge_msg)
        if challenge_msg[0] != CtrlMsg.CHALLENGE:
            raise MessageError("expected CHALLENGE")
        challenge = CtrlChallenge.unpack(challenge_msg[1:])
        self.tag_epoch = challenge.stored_epoch
        if challenge.proto != PROTO_VERSION:
            self.tag_error(Status.VERSION_MISMATCH)
        th = crypto.transcript_hash(self.caps_bytes, hello, challenge_msg)
        mac = crypto.mac_b(assignment.key, th)
        await self.tunnel.send(bytes([CtrlMsg.AUTH]) + CtrlAuth(mac).pack())
        reply = await self.recv_ctrl(handshake_deadline)
        if reply[0] == CtrlMsg.ERROR:
            self.ctrl_error(reply)
        if reply[0] != CtrlMsg.AUTH_OK:
            raise MessageError("expected AUTH_OK")
        try:
            crypto.verify_mac_t(assignment.key, th, mac, CtrlAuthOk.unpack(reply[1:]).mac_t)
        except crypto.AuthError:
            self.tag_error(Status.AUTH_FAILED)
        self.tag_epoch = max(self.tag_epoch, self.epoch)  # the tag persisted the epoch before AUTH_OK (§5.4)
        radio._authenticated(self.tag_id, self.epoch, battery_mv=challenge.battery_mv, rssi=self.tunnel.open_rssi)
        k_b2t, k_t2b = crypto.session_keys(assignment.key, th)
        self.sender = crypto.RecordSender(k_b2t, RecordDir.B2T)
        self.receiver = crypto.RecordReceiver(k_t2b, RecordDir.T2B)
        while self.credit_grants == 0:  # CREDIT{caps.credits} follows AUTH_OK
            await self.pump(handshake_deadline)
        panel = Panel.from_caps(caps)
        for job in list(radio.jobs.get(self.tag_id, [])):
            if job not in radio.jobs.get(self.tag_id, []):
                continue  # finished meanwhile (cancelled, superseded)
            if job.epoch != self.epoch:
                self.finish(job, Status.NOT_ASSIGNED)
                continue
            self.job = job
            self.refreshing_reported = False
            job.in_session = True
            try:
                await self.run_job(job, panel, challenge)
            finally:
                job.in_session = False
                self.job = None

    def timing(self, job: RadioJob, started: float, refresh_ms: int) -> Timing:
        clock = self.radio.clock()
        return Timing(wake_ms=int(max(0.0, self.opened_at - job.accepted_at) * 1000), mesh_ms=0,
                      transfer_ms=int(max(0.0, clock - started) * 1000), refresh_ms=refresh_ms, suspend_ms=0)

    def finish_from(self, job: RadioJob, result: RecResult, started: float) -> None:
        # RESULT.flags bit0: the tag answered with its stored ACK -> RESULT_FLAG_DUPLICATE (§3.4).
        self.finish(job, _status(result.status), digest8=result.digest, battery_mv=result.battery_mv,
                    timing=self.timing(job, started, result.refresh_ms), flags=result.flags & RESULT_FLAG_DUPLICATE)

    async def run_job(self, job: RadioJob, panel: Panel, challenge: CtrlChallenge) -> None:
        radio = self.radio
        started = radio.clock()
        if job.kind == "cmd":
            radio._stage(job, DeliveryStage.TRANSFERRING)
            deadline = started + STEP_S + REFRESH_S + RELAY_S
            early = await self.wait_credit(deadline)
            if early is not None:
                self.finish_from(job, early, started)
                return
            await self.send_record(RecordType.CMD, RecCmd(job.cmd, job.update_id).pack())
            self.finish_from(job, await self.wait_result(radio.clock() + REFRESH_S + RELAY_S), started)
            return
        if (job.frame_end_sent and challenge.flags & 1
                and (challenge.stored_epoch, challenge.displayed_rev) == (job.epoch, job.revision)):
            self.finish(job, Status.DISPLAY_STATE_UNKNOWN, battery_mv=challenge.battery_mv,
                        timing=self.timing(job, started, 0))
            return
        try:
            frame = await radio._frame(job, panel)
        except LayoutError as exc:  # e.g. the logical size does not fit this panel (§4.4 Rotation)
            self.finish(job, exc.status)
            return
        radio._stage(job, DeliveryStage.TRANSFERRING)
        started = radio.clock()
        plane_len = len(frame.planes[0])
        # §10: FRAME_BEGIN to RESULT within 60 s + 1 s per KiB of plane data + the refresh bound.
        deadline = started + 60.0 + plane_len * len(frame.planes) / 1024 + REFRESH_S + RELAY_S
        self._progress()
        early = await self.wait_credit(deadline)
        if early is not None:
            self.finish_from(job, early, started)
            return
        grants = self.credit_grants
        await self.send_record(RecordType.FRAME_BEGIN, RecFrameBegin(job.revision, job.update_id, frame.digest,
                                                                     len(frame.planes), plane_len).pack())
        while self.credit_grants == grants:  # FRAME_BEGIN's credit, or the tag's immediate RESULT
            result = await self.pump(deadline)
            if result is not None:
                self.finish_from(job, result, started)
                return
        for plane, data in enumerate(frame.planes):
            for offset in range(0, plane_len, TAG_PLANE_DATA_MAX):
                early = await self.wait_credit(deadline)
                if early is not None:
                    self.finish_from(job, early, started)
                    return
                await self.send_record(RecordType.PLANE_DATA, RecPlaneData(
                    plane, offset, data[offset : offset + TAG_PLANE_DATA_MAX]).pack())
        early = await self.wait_credit(deadline)
        if early is not None:
            self.finish_from(job, early, started)
            return
        await self.send_record(RecordType.FRAME_END, b"")
        job.frame_end_sent = True
        self.finish_from(job, await self.wait_result(deadline), started)


__all__ = ["GatewayRadio", "RadioAssignment", "RadioJob", "TUNNEL_S"]
