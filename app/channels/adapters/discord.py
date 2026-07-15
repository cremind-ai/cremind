"""Discord adapter — ``discord.py`` bot in DM mode.

Runs a Discord bot (via :pypi:`discord.py`, an optional extra installed by the
``channel-discord`` feature) that listens for direct messages and dispatches
them to :meth:`BaseChannelAdapter._handle_inbound`. Replies are sent back as
DMs. Guild (server-channel) messages are ignored — this channel is a personal
1:1 bridge, mirroring the DM-only contract of the other adapters.

Serves conversational ``bot`` mode and push-only ``notification`` mode over the
same client (notification behavior lives in
:class:`app.channels.notification_delivery.NotificationDeliveryMixin`).

Requires MESSAGE CONTENT INTENT enabled on the bot (a privileged intent); if it
is off, ``discord.py`` raises on connect and the channel is disabled with the
error surfaced in ``state.last_error``.
"""

from __future__ import annotations

from typing import Any

from app.channels.base import BaseChannelAdapter, _split_for_messaging
from app.channels.exceptions import ChannelAuthError, ChannelNotImplemented
from app.utils.logger import logger

_DISCORD_MSG_LIMIT = 2000


class DiscordAdapter(BaseChannelAdapter):
    def __init__(self, channel: dict, storage: Any) -> None:
        super().__init__(channel, storage)
        self._client: Any = None
        # sender_id (str user id) -> discord.User, so replies don't re-fetch.
        self._users: dict[str, Any] = {}

    def _bot_token(self) -> str:
        token = (self.channel.get("config") or {}).get("bot_token")
        if not token:
            raise ChannelAuthError("Discord channel missing bot_token")
        return token

    def _build_client(self) -> Any:
        try:
            import discord  # type: ignore
        except ImportError as exc:
            raise ChannelNotImplemented(
                "discord.py is not installed. Re-enabling this channel installs "
                "it automatically; to install it manually run "
                "`cremind features install channel.discord.bot`.",
            ) from exc

        intents = discord.Intents.default()
        intents.message_content = True  # privileged — must be enabled in the portal
        client = discord.Client(intents=intents)

        async def on_message(message: Any) -> None:
            # Ignore our own messages, other bots, and anything outside a DM.
            if client.user is not None and message.author.id == client.user.id:
                return
            if getattr(message.author, "bot", False):
                return
            if message.guild is not None:
                return
            text = message.content or ""
            if not text:
                return
            sender_id = str(message.author.id)
            self._users[sender_id] = message.author
            display_name = getattr(message.author, "display_name", None) or getattr(
                message.author, "name", None,
            )
            await self._handle_inbound_safe(sender_id, display_name, text)

        client.event(on_message)
        return client

    async def _run(self) -> None:
        if self.channel.get("mode") not in ("bot", "notification"):
            raise ChannelNotImplemented(
                f"DiscordAdapter does not support mode={self.channel.get('mode')!r}",
            )
        self._client = self._build_client()
        token = self._bot_token()
        try:
            await self._client.start(token)
        except ChannelNotImplemented:
            raise
        except Exception as exc:  # noqa: BLE001
            # discord.LoginFailure (bad token) / PrivilegedIntentsRequired
            # (intent not enabled) both land here as a clean auth error.
            raise ChannelAuthError(f"Discord connect failed: {exc}") from exc

    async def stop(self) -> None:  # type: ignore[override]
        client = self._client
        if client is not None and not client.is_closed():
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
        await super().stop()

    async def _handle_inbound_safe(
        self, sender_id: str, display_name: str | None, text: str,
    ) -> None:
        try:
            await self._handle_inbound(sender_id, display_name, text)
        except Exception:  # noqa: BLE001
            logger.exception("discord: inbound handler failed")

    async def _get_user(self, sender_id: str) -> Any:
        user = self._users.get(sender_id)
        if user is None and self._client is not None:
            try:
                user = await self._client.fetch_user(int(sender_id))
            except Exception:  # noqa: BLE001
                return None
            self._users[sender_id] = user
        return user

    async def _send_text(self, sender_id: str, text: str) -> None:
        user = await self._get_user(sender_id)
        if user is None:
            raise ChannelAuthError(f"Discord user {sender_id} not resolvable")
        for chunk in _split_for_messaging(text, _DISCORD_MSG_LIMIT):
            await user.send(chunk)

    async def _send_typing(self, sender_id: str) -> None:
        user = await self._get_user(sender_id)
        if user is None:
            return
        try:
            dm = user.dm_channel or await user.create_dm()
            # Entering the typing context sends a single typing packet; we exit
            # immediately since the base ``_typing_loop`` re-ticks every few
            # seconds (Discord's indicator lasts ~10s).
            async with dm.typing():
                pass
        except Exception:  # noqa: BLE001
            logger.debug("discord: typing indicator dropped", exc_info=True)
