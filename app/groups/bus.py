"""In-memory pub/sub for one group's live timeline.

Mirrors :class:`app.events.stream_bus.ConversationStreamBus`, minus the parts
that only make sense for a single run. Two differences matter:

* **The ring is never cleared.** A conversation's bus clears its replay ring at
  ``end_run`` because the run is the unit of interest. A room has no runs — it
  has a continuous history — so the ring is a rolling window a reconnecting
  client can catch up from.
* **No per-profile fan-out.** A conversation belongs to one profile; a group
  belongs to several, and every member (plus the admin) subscribes to the same
  group id, so there is nothing to route.

Frames: ``message`` (a timeline row), ``message_routing`` (who the router woke
for a row already sent — the classification does not exist yet when the row goes
out, see :func:`app.groups.fanout._stamp_routing`), ``agent_status`` (a member
started or finished thinking), ``seat_event`` (one step of a member's running
turn, mirrored off its seat), ``group_updated`` (name/members/settings
changed), ``deleted``. The SSE endpoint synthesises ``ready`` on connect.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from app.utils.logger import logger

_RING_CAP = 200

# How many deleted rooms stay refused. A frame can only arrive for a deleted
# room from something already in flight when it went — a seat turn's closing
# status, its mirrored steps, a fan-out that read the group a moment earlier —
# so the window is one turn wide and a handful of ids covers every room that
# could be inside it.
_TOMBSTONE_CAP = 64


class GroupStreamBus:
    def __init__(self) -> None:
        self._subs: Dict[str, List[asyncio.Queue]] = {}
        self._ring: Dict[str, List[Dict[str, Any]]] = {}
        self._seq: Dict[str, int] = {}
        self._deleted: "OrderedDict[str, None]" = OrderedDict()

    async def publish(
        self,
        group_id: str,
        event_type: str,
        data: Optional[Dict[str, Any]] = None,
        *,
        ephemeral: bool = False,
    ) -> None:
        """Fan a frame out to the room, and (unless ``ephemeral``) remember it.

        An ephemeral frame is live-only: it still takes a ``seq``, so a client
        counting frames sees no reordering, but it never enters the replay ring.
        That is what keeps a member's turn — which emits a step frame per tool
        call — from evicting the room's actual messages out of a 200-entry
        window nobody could then catch up from. The consequence to know about:
        **ring seqs have gaps**, so a client must not read a missing seq as a
        dropped frame. Live turns are recovered on connect from the seats
        themselves (``ConversationStreamBus.snapshot``), not from here.
        """
        if not group_id or group_id in self._deleted:
            return
        self._seq[group_id] = self._seq.get(group_id, 0) + 1
        event = {"seq": self._seq[group_id], "type": event_type, "data": data or {}}
        if not ephemeral:
            ring = self._ring.setdefault(group_id, [])
            ring.append(event)
            if len(ring) > _RING_CAP:
                del ring[: len(ring) - _RING_CAP]
        for queue in list(self._subs.get(group_id, [])):
            try:
                queue.put_nowait(event)
            except Exception:  # noqa: BLE001 - a full/closed subscriber queue
                logger.debug(f"[group] dropping frame for a stalled subscriber of {group_id}")

    async def subscribe(
        self, group_id: str,
    ) -> Tuple[asyncio.Queue, List[Dict[str, Any]]]:
        """Return ``(queue, replay)`` — the buffered window, then the live tail."""
        queue: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(group_id, []).append(queue)
        return queue, list(self._ring.get(group_id, []))

    async def unsubscribe(self, group_id: str, queue: asyncio.Queue) -> None:
        subs = self._subs.get(group_id)
        if not subs:
            return
        try:
            subs.remove(queue)
        except ValueError:
            pass
        if not subs:
            self._subs.pop(group_id, None)

    def discard(self, group_id: str, *, deleted: bool = False) -> None:
        """Drop a group's buffered state; with ``deleted``, refuse it for good.

        The two are separate because the plain form is also how a caller throws
        away frames it does not want (a test clearing the setup chatter, a
        reset), and those groups go on publishing afterwards.

        ``deleted=True`` is the room being removed, and it has to be enforced
        here rather than left to the publishers: several of them outlive the
        room by design — a seat turn's closing status, the step frames mirrored
        off that turn, a fan-out that read the group a moment before it went —
        and each re-created the counter (and, for a non-ephemeral frame, the
        ring) that nothing would pop again. Asking every publisher to re-read
        the room instead would put a database call on the path of every tool
        call in every room. The tombstone list holds ids and is bounded, so it
        costs one dict lookup per frame and cannot grow without limit.
        """
        self._subs.pop(group_id, None)
        self._ring.pop(group_id, None)
        self._seq.pop(group_id, None)
        if deleted and group_id:
            self._deleted[group_id] = None
            self._deleted.move_to_end(group_id)
            while len(self._deleted) > _TOMBSTONE_CAP:
                self._deleted.popitem(last=False)

    def snapshot(self, group_id: str) -> List[Dict[str, Any]]:
        return list(self._ring.get(group_id, []))


_instance: Optional[GroupStreamBus] = None


def get_group_stream_bus() -> GroupStreamBus:
    global _instance
    if _instance is None:
        _instance = GroupStreamBus()
    return _instance
