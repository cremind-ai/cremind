"""Slack adapter — STUB.

Planned implementation: ``slack-bolt``'s socket-mode app bound to ``message.im``
events; replies via ``app.client.chat_postMessage``. Requires both a Bot User
OAuth Token (xoxb-) and an App-Level Token (xapp-) with ``connections:write``.
"""

from __future__ import annotations

from app.channels.base import BaseChannelAdapter
from app.channels.exceptions import ChannelNotImplemented


class SlackAdapter(BaseChannelAdapter):
    async def _run(self) -> None:
        raise ChannelNotImplemented("Slack adapter is not yet implemented.")

    async def _send_text(self, sender_id: str, text: str) -> None:
        raise ChannelNotImplemented("Slack send not implemented")
