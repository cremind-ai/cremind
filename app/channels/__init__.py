"""Channels feature: receive messages from external messaging platforms.

Each external platform (Telegram, WhatsApp, Discord, Messenger, Slack) has a
:class:`BaseChannelAdapter` subclass that runs as an in-process asyncio task.
Adapters are owned by :class:`ChannelRegistry`, started on server boot for
every enabled channel row, and stopped on server shutdown or when a channel
is disabled/deleted.

See :mod:`app.channels.base` for the inbound message flow (sender lookup,
OTP/password gating, agent dispatch, reply buffering) and
:mod:`app.channels.registry` for lifecycle.
"""

from app.channels.exceptions import ChannelNotImplemented
from app.channels.registry import ChannelRegistry, get_channel_registry

__all__ = ["ChannelNotImplemented", "ChannelRegistry", "get_channel_registry"]
