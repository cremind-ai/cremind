"""Unit tests for the Slack adapter (app/channels/adapters/slack.py).

``slack-bolt`` is an optional extra not installed in the dev environment, so
these tests cover the SDK-independent logic: the send-target resolution
(cached IM / user-id → conversations.open / channel-id passthrough) and mode
gating.
"""

from __future__ import annotations

import asyncio

from app.channels.adapters.slack import SlackAdapter
from app.channels.exceptions import ChannelNotImplemented


def _adapter(mode: str = "bot") -> SlackAdapter:
    channel = {
        "id": "c1",
        "profile": "admin",
        "channel_type": "slack",
        "mode": mode,
        "config": {"bot_token": "xoxb-1", "app_token": "xapp-1"},
    }
    return SlackAdapter(channel, storage=object())


class _FakeClient:
    def __init__(self):
        self.opened: list[str] = []

    async def conversations_open(self, users):
        self.opened.append(users)
        return {"channel": {"id": "D-opened"}}


class _FakeApp:
    def __init__(self):
        self.client = _FakeClient()


def test_resolve_channel_uses_cached_im():
    adapter = _adapter()
    adapter._im_channels["U1"] = "D-cached"

    async def run():
        return await adapter._resolve_channel("U1")

    assert asyncio.run(run()) == "D-cached"


def test_resolve_channel_opens_im_for_user_id():
    adapter = _adapter()
    app = _FakeApp()
    adapter._app = app

    async def run():
        return await adapter._resolve_channel("U9")

    assert asyncio.run(run()) == "D-opened"
    assert app.client.opened == ["U9"]
    # Resolution is cached for reuse.
    assert adapter._im_channels["U9"] == "D-opened"


def test_resolve_channel_passthrough_for_channel_id():
    adapter = _adapter()

    async def run():
        return await adapter._resolve_channel("C12345")

    # A value that isn't a user id (U/W) is treated as an already-usable id.
    assert asyncio.run(run()) == "C12345"


def test_run_rejects_unsupported_mode():
    adapter = _adapter(mode="userbot")

    async def run():
        await adapter._run()

    try:
        asyncio.run(run())
        assert False, "expected ChannelNotImplemented"
    except ChannelNotImplemented as exc:
        assert "mode" in str(exc)
