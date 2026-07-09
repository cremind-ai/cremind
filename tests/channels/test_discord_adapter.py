"""Unit tests for the Discord adapter (app/channels/adapters/discord.py).

The ``discord.py`` SDK is an optional extra not installed in the dev
environment, so these tests cover the SDK-independent logic: outbound chunking
to Discord's 2000-char limit and mode gating.
"""

from __future__ import annotations

import asyncio

from app.channels.adapters.discord import DiscordAdapter
from app.channels.exceptions import ChannelNotImplemented


def _adapter(mode: str = "bot") -> DiscordAdapter:
    channel = {
        "id": "c1",
        "profile": "admin",
        "channel_type": "discord",
        "mode": mode,
        "config": {"bot_token": "tok"},
    }
    return DiscordAdapter(channel, storage=object())


class _FakeUser:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text):
        self.sent.append(text)


def test_send_text_splits_to_discord_limit():
    adapter = _adapter()
    user = _FakeUser()
    adapter._users["u1"] = user  # pre-seed so no fetch/client is needed

    async def run():
        await adapter._send_text("u1", "y" * 5000)

    asyncio.run(run())
    assert len(user.sent) >= 3
    assert all(len(chunk) <= 2000 for chunk in user.sent)


def test_run_rejects_unsupported_mode():
    adapter = _adapter(mode="userbot")

    async def run():
        await adapter._run()

    try:
        asyncio.run(run())
        assert False, "expected ChannelNotImplemented"
    except ChannelNotImplemented as exc:
        assert "mode" in str(exc)


def test_missing_token_is_auth_error():
    from app.channels.exceptions import ChannelAuthError

    adapter = _adapter()
    adapter.channel["config"] = {}
    try:
        adapter._bot_token()
        assert False, "expected ChannelAuthError"
    except ChannelAuthError:
        pass
