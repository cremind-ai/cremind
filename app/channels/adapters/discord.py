"""Discord adapter — STUB.

Planned implementation: ``discord.py``-based bot in bot mode, listening for
DMs (``messages.guild=False``) and dispatching them to ``_handle_inbound``.
Replies sent via ``channel.send`` on the user's DM channel.
"""

from __future__ import annotations

from app.channels.base import BaseChannelAdapter
from app.channels.exceptions import ChannelNotImplemented


class DiscordAdapter(BaseChannelAdapter):
    async def _run(self) -> None:
        raise ChannelNotImplemented("Discord adapter is not yet implemented.")

    async def _send_text(self, sender_id: str, text: str) -> None:
        raise ChannelNotImplemented("Discord send not implemented")
