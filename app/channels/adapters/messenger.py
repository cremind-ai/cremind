"""Messenger adapter — STUB.

Planned implementation: webhook receiver registered with the Facebook Graph
API. Requires a public callback URL; the adapter exposes a route handler
through the registry and uses the Page Access Token to send replies.
"""

from __future__ import annotations

from app.channels.base import BaseChannelAdapter
from app.channels.exceptions import ChannelNotImplemented


class MessengerAdapter(BaseChannelAdapter):
    async def _run(self) -> None:
        raise ChannelNotImplemented("Messenger adapter is not yet implemented.")

    async def _send_text(self, sender_id: str, text: str) -> None:
        raise ChannelNotImplemented("Messenger send not implemented")
