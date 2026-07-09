"""Slack adapter — ``slack-bolt`` Socket Mode bot.

Runs a Slack app in Socket Mode (via :pypi:`slack-bolt`, an optional extra
installed by the ``channel-slack`` feature), so no public URL is needed — the
app connects outbound over a WebSocket using an app-level token. It listens for
direct messages (``message.im`` events) and dispatches them to
:meth:`BaseChannelAdapter._handle_inbound`; replies go back via
``chat.postMessage``.

Serves conversational ``bot`` mode and push-only ``notification`` mode over the
same app (notification behavior lives in
:class:`app.channels.notification_delivery.NotificationDeliveryMixin`).

Needs two tokens (declared in the catalog): a Bot User OAuth token (``xoxb-``)
and an App-Level token (``xapp-``) with ``connections:write``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.channels.base import BaseChannelAdapter
from app.channels.exceptions import ChannelAuthError, ChannelNotImplemented
from app.utils.logger import logger


class SlackAdapter(BaseChannelAdapter):
    def __init__(self, channel: dict, storage: Any) -> None:
        super().__init__(channel, storage)
        self._app: Any = None
        self._handler: Any = None
        # Slack user id -> IM (DM) channel id, captured from inbound events so
        # replies don't need an extra ``conversations.open`` round-trip.
        self._im_channels: dict[str, str] = {}
        # Slack user id -> display name (best-effort, for conversation titles).
        self._names: dict[str, str] = {}

    def _tokens(self) -> tuple[str, str]:
        config = self.channel.get("config") or {}
        bot_token = config.get("bot_token")
        app_token = config.get("app_token")
        if not bot_token:
            raise ChannelAuthError("Slack channel missing bot_token (xoxb-)")
        if not app_token:
            raise ChannelAuthError("Slack channel missing app_token (xapp-)")
        return bot_token, app_token

    def _build(self) -> tuple[Any, Any]:
        try:
            from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler  # type: ignore
            from slack_bolt.async_app import AsyncApp  # type: ignore
        except ImportError as exc:
            raise ChannelNotImplemented(
                "slack-bolt is not installed. Run `pip install slack-bolt>=1.18` "
                "to enable Slack channels.",
            ) from exc

        bot_token, app_token = self._tokens()
        app = AsyncApp(
            token=bot_token,
            # Socket Mode never verifies inbound HTTP request signatures, but
            # bolt wants a signing secret at construction; a placeholder is
            # harmless here. Token validation happens on socket connect.
            signing_secret="unused-in-socket-mode",
            token_verification_enabled=False,
        )

        async def on_message(event: dict, *args: Any, **kwargs: Any) -> None:
            # Only direct messages; skip bot echoes and edit/delete subtypes.
            if event.get("channel_type") != "im":
                return
            if event.get("bot_id") or event.get("subtype"):
                return
            user_id = event.get("user")
            text = event.get("text") or ""
            if not user_id or not text:
                return
            im_channel = event.get("channel")
            if im_channel:
                self._im_channels[str(user_id)] = str(im_channel)
            display_name = await self._resolve_name(str(user_id))
            await self._handle_inbound_safe(str(user_id), display_name, text)

        app.event("message")(on_message)
        handler = AsyncSocketModeHandler(app, app_token)
        return app, handler

    async def _run(self) -> None:
        if self.channel.get("mode") not in ("bot", "notification"):
            raise ChannelNotImplemented(
                f"SlackAdapter does not support mode={self.channel.get('mode')!r}",
            )
        self._app, self._handler = self._build()
        try:
            # ``connect_async`` opens the Socket Mode WebSocket; we then park on
            # an Event so ``_run`` stays alive (and cancellable) for the life of
            # the adapter.
            await self._handler.connect_async()
        except ChannelNotImplemented:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ChannelAuthError(f"Slack connect failed: {exc}") from exc
        await asyncio.Event().wait()

    async def stop(self) -> None:  # type: ignore[override]
        handler = self._handler
        if handler is not None:
            try:
                await handler.close_async()
            except Exception:  # noqa: BLE001
                pass
        await super().stop()

    async def _handle_inbound_safe(
        self, sender_id: str, display_name: str | None, text: str,
    ) -> None:
        try:
            await self._handle_inbound(sender_id, display_name, text)
        except Exception:  # noqa: BLE001
            logger.exception("slack: inbound handler failed")

    async def _resolve_name(self, user_id: str) -> str | None:
        if user_id in self._names:
            return self._names[user_id]
        if self._app is None:
            return None
        try:
            resp = await self._app.client.users_info(user=user_id)
            profile = (resp.get("user") or {}).get("profile") or {}
            name = profile.get("display_name") or (resp.get("user") or {}).get("real_name")
            if name:
                self._names[user_id] = name
            return name
        except Exception:  # noqa: BLE001
            return None

    async def _resolve_channel(self, sender_id: str) -> str | None:
        """Map a send target to a Slack channel id.

        Conversational replies reuse the IM channel cached from inbound. For
        notification targets (``target_chat_ids``) a value already shaped like a
        channel id (``C``/``D``/``G``…) is used as-is; a user id (``U``/``W``…)
        is resolved to its IM channel via ``conversations.open``.
        """
        cached = self._im_channels.get(sender_id)
        if cached:
            return cached
        if sender_id[:1] in ("U", "W"):
            if self._app is None:
                return None
            try:
                resp = await self._app.client.conversations_open(users=sender_id)
                channel_id = (resp.get("channel") or {}).get("id")
                if channel_id:
                    self._im_channels[sender_id] = channel_id
                return channel_id
            except Exception:  # noqa: BLE001
                return None
        # Already a channel id (or a group/DM id supplied directly).
        return sender_id

    async def _send_text(self, sender_id: str, text: str) -> None:
        if self._app is None:
            raise ChannelAuthError("Slack app not connected")
        channel_id = await self._resolve_channel(sender_id)
        if not channel_id:
            raise ChannelAuthError(f"Slack channel for {sender_id} not resolvable")
        await self._app.client.chat_postMessage(channel=channel_id, text=text)
