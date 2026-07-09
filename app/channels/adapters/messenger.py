"""Messenger adapter — Facebook Graph API (webhook receiver + send).

Meta's Messenger Platform is **inbound-webhook-only**: there is no polling API,
so the adapter does not run a receive loop. Instead, a public route
(``/api/channels/webhook/messenger/{channel_id}`` in :mod:`app.api.channels`)
receives Meta's callbacks and calls :meth:`handle_webhook_message`, which feeds
:meth:`BaseChannelAdapter._handle_inbound`. Replies are sent with the Page
Access Token via the Graph ``/me/messages`` endpoint (core ``httpx`` — no SDK).

Because Meta must reach the callback URL, this channel only works when the
Cremind host is publicly reachable over HTTPS (a real deployment or a tunnel
such as ngrok/cloudflared). The catalog instructions spell this out.

Serves ``bot`` (Page bot) and push-only ``notification`` mode. Note: Meta's
24-hour standard-messaging window means proactive notifications only reach users
who messaged the Page within the last 24 hours (or require message tags).
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.channels.base import BaseChannelAdapter, _split_for_messaging
from app.channels.exceptions import ChannelAuthError
from app.utils.logger import logger

_GRAPH_URL = "https://graph.facebook.com/v21.0/me/messages"
_MESSENGER_TEXT_LIMIT = 2000


class MessengerAdapter(BaseChannelAdapter):
    def __init__(self, channel: dict, storage: Any) -> None:
        super().__init__(channel, storage)
        self._client: Any = None

    def _page_token(self) -> str:
        token = (self.channel.get("config") or {}).get("page_access_token")
        if not token:
            raise ChannelAuthError("Messenger channel missing page_access_token")
        return token

    async def _run(self) -> None:
        # No receive loop — inbound arrives via the public webhook route. We
        # keep an httpx client for outbound sends and park until cancelled so
        # the registry keeps this adapter instance findable by the route.
        import httpx  # core dependency

        # Validate config early so a missing token disables the channel cleanly.
        self._page_token()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0))
        try:
            await asyncio.Event().wait()
        finally:
            client = self._client
            self._client = None
            if client is not None:
                try:
                    await client.aclose()
                except Exception:  # noqa: BLE001
                    pass

    async def handle_webhook_message(self, sender_id: str, text: str) -> None:
        """Entry point called by the public webhook route for each inbound message."""
        await self._handle_inbound_safe(sender_id, None, text)

    async def _handle_inbound_safe(
        self, sender_id: str, display_name: str | None, text: str,
    ) -> None:
        try:
            await self._handle_inbound(sender_id, display_name, text)
        except Exception:  # noqa: BLE001
            logger.exception("messenger: inbound handler failed")

    async def _graph_post(self, body: dict) -> None:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0))
        resp = await self._client.post(
            _GRAPH_URL, params={"access_token": self._page_token()}, json=body,
        )
        if resp.status_code >= 400:
            logger.warning(
                f"messenger[{self.channel_id}]: graph send failed "
                f"({resp.status_code}): {resp.text[:200]}",
            )

    async def _send_text(self, sender_id: str, text: str) -> None:
        for chunk in _split_for_messaging(text, _MESSENGER_TEXT_LIMIT):
            await self._graph_post(
                {"recipient": {"id": sender_id}, "message": {"text": chunk}},
            )

    async def _send_typing(self, sender_id: str) -> None:
        try:
            await self._graph_post(
                {"recipient": {"id": sender_id}, "sender_action": "typing_on"},
            )
        except Exception:  # noqa: BLE001
            logger.debug("messenger: typing indicator dropped", exc_info=True)
