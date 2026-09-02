"""Who gets told the agent is composing, and on which of the two hooks.

A room's "typing…" and a person's are different platform calls — on Zalo they
are different HTTP services entirely — so :class:`BaseChannelAdapter` splits
them across ``_send_typing`` (a sender) and ``_send_typing_to_chat`` (a room),
and ``_typing_loop_for`` picks between them from the reply target. Both default
to a silent no-op, which is the trap this file exists for: an adapter that
declares rooms and forgets the room hook shows nothing in a group, reports no
error, and looks exactly like a platform that does not support the feature.
Three adapters had shipped that way.

The tests drive the real base-class machinery with a stub adapter, so nothing
here needs a platform SDK, a socket, or a running channel.
"""

from __future__ import annotations

import asyncio

from app.channels.base import BaseChannelAdapter
from app.channels.reply_target import group_target, sender_target


class _Adapter(BaseChannelAdapter):
    """The smallest adapter that can be asked to show a typing indicator."""

    channel_type = "stub"

    def __init__(self) -> None:
        super().__init__(
            {"id": "c1", "profile": "dog", "channel_type": "stub",
             "mode": "bot", "config": {}, "state": {}},
            storage=None,
        )
        self.senders: list[str] = []
        self.chats: list[str] = []

    async def _run(self) -> None:  # pragma: no cover - never started here
        raise NotImplementedError

    async def _send_text(self, sender_id: str, text: str) -> None:
        raise NotImplementedError

    async def _send_typing(self, sender_id: str) -> None:
        self.senders.append(sender_id)

    async def _send_typing_to_chat(self, chat_id: str) -> None:
        self.chats.append(chat_id)


async def _wait_for(predicate, *, tries: int = 200) -> None:
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.005)


async def _pulse(adapter: _Adapter, target, *, ticks: int = 1) -> None:
    """Run the real loop until ``ticks`` pulses land, then cancel it.

    The interval is squeezed rather than the loop mocked, so the thing under
    test is the one that ships — cancellation included, since being cancelled is
    its only exit path.
    """
    task = asyncio.create_task(adapter._typing_loop_for(target, interval=0.01))
    await _wait_for(lambda: len(adapter.senders) + len(adapter.chats) >= ticks)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_a_room_is_told_on_the_room_hook():
    """A group's id is nobody's sender id — the ROOM call has to be the one
    made, or the platform is addressed as if the room were a person."""
    adapter = _Adapter()
    target = group_target({"id": "g1", "platform_chat_id": "68307916561724197"})

    asyncio.run(_pulse(adapter, target))

    assert adapter.chats == ["68307916561724197"]
    assert adapter.senders == []


def test_a_person_is_told_on_the_sender_hook():
    adapter = _Adapter()

    asyncio.run(_pulse(adapter, sender_target("1644772063")))

    assert adapter.senders == ["1644772063"]
    assert adapter.chats == []


def test_the_indicator_is_kept_alive_rather_than_pulsed_once():
    """Every platform's indicator expires within seconds, so a single pulse
    would leave a long turn looking idle for most of its length."""
    adapter = _Adapter()

    asyncio.run(_pulse(adapter, sender_target("u1"), ticks=3))

    assert len(adapter.senders) >= 3


def test_a_failing_tick_does_not_end_the_loop():
    """Typing is best-effort: a transient platform error costs one pulse, not
    the indicator for the rest of the run."""
    adapter = _Adapter()
    calls: list[str] = []

    async def _flaky(sender_id: str) -> None:
        calls.append(sender_id)
        if len(calls) == 1:
            raise RuntimeError("transient")

    adapter._send_typing = _flaky  # type: ignore[method-assign]

    async def scenario():
        task = asyncio.create_task(
            adapter._typing_loop_for(sender_target("u1"), interval=0.01),
        )
        await _wait_for(lambda: len(calls) >= 3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    assert len(calls) >= 3


def test_the_forwarder_owns_the_indicator_and_takes_it_down_with_it():
    """The typing loop is spawned as the forwarder's sibling and cancelled in
    its ``finally`` — otherwise a finished run would leave a room showing
    "typing…" until the process ends."""
    adapter = _Adapter()
    target = group_target({"id": "g1", "platform_chat_id": "C1"})

    async def scenario():
        release = asyncio.Event()

        async def _forward(conversation_id: str, tgt) -> None:
            await release.wait()

        adapter._forward_reply = _forward  # type: ignore[method-assign]

        forwarder = adapter._spawn_forwarder(target, "conv-1")
        await _wait_for(lambda: bool(adapter.chats))
        assert adapter.chats, "no indicator while the forwarder was running"

        release.set()
        await forwarder
        seen = len(adapter.chats)
        await asyncio.sleep(0.05)
        return seen

    seen = asyncio.run(scenario())

    assert len(adapter.chats) == seen, "indicator still pulsing after the run"


def test_every_room_capable_adapter_implements_the_room_hook():
    """The drift guard.

    ``_send_typing_to_chat`` defaults to a silent no-op, so forgetting it on an
    adapter that declares rooms is invisible at runtime and in review — which is
    how the Zalo bot and the Telegram userbot both shipped without it.

    There is deliberately no allowlist. A platform that genuinely cannot show an
    indicator — Slack, whose Web API has no typing method, whose legacy RTM frame
    a Socket Mode app cannot send, and whose ``agents.sessions.setStatus`` is
    scoped to app-DM assistant sessions — satisfies this by overriding the hook
    with an explicit no-op that says so. That is the point: what is being
    asserted is that somebody decided, not that every platform can.
    """
    from app.config import load_all_channel_catalogs

    from app.channels.registry import _resolve_adapter_class

    missing: list[str] = []
    for channel_type, catalog in load_all_channel_catalogs().items():
        info = catalog.get("channel") or {}
        for mode in info.get("modes") or []:
            if not mode.get("implemented"):
                continue
            try:
                cls = _resolve_adapter_class(channel_type, str(mode.get("id") or ""))
            except Exception:  # noqa: BLE001 - an unimplemented pair proves nothing
                continue
            if not getattr(cls, "supports_group_chats", False):
                continue
            if cls._send_typing_to_chat is BaseChannelAdapter._send_typing_to_chat:
                missing.append(f"{channel_type}/{mode.get('id')} ({cls.__name__})")

    assert not missing, (
        "these adapters declare group chats but never show the agent composing "
        f"in one: {sorted(set(missing))}"
    )
