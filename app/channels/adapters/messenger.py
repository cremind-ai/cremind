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
import mimetypes
import os
from typing import Any
from urllib.parse import urlparse

from app.channels.attachments import IncomingFile, dest_for
from app.channels.base import BaseChannelAdapter, _split_for_messaging
from app.channels.exceptions import ChannelAuthError
from app.utils.logger import logger

_GRAPH_URL = "https://graph.facebook.com/v21.0/me/messages"
_MESSENGER_TEXT_LIMIT = 2000
# Meta caps Messenger attachments at 25 MB.
_MESSENGER_UPLOAD_LIMIT = 25 * 1024 * 1024
# Webhook attachment types that carry a downloadable payload.url. ``fallback``
# (link previews) and ``location`` have no file behind them.
_ATTACHMENT_TYPES = ("image", "video", "audio", "file")


class MessengerAdapter(BaseChannelAdapter):
    supports_file_send = True

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

    async def handle_webhook_message(
        self, sender_id: str, text: str,
        attachments: list[dict] | None = None,
    ) -> None:
        """Entry point called by the public webhook route for each inbound message.

        ``attachments`` are the raw webhook entries (``{"type", "payload":
        {"url"}}``); the CDN URLs Meta hands out are unauthenticated but
        time-limited, which is fine — the deferred fetch runs within this same
        request's handling.
        """
        files = self._extract_files(attachments)
        await self._handle_inbound_safe(sender_id, None, text, files=files or None)

    def _extract_files(
        self, attachments: list[dict] | None,
    ) -> list[IncomingFile]:
        found: list[IncomingFile] = []
        for index, entry in enumerate(attachments or ()):
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("type") or "")
            if kind not in _ATTACHMENT_TYPES:
                continue
            url = str((entry.get("payload") or {}).get("url") or "")
            if not url:
                continue
            name = os.path.basename(urlparse(url).path) or f"messenger_{kind}_{index}"
            mime, _ = mimetypes.guess_type(name)

            def _make_fetch(url: str = url, name: str = name):
                async def fetch(dest_dir: str) -> str:
                    import httpx

                    dest = dest_for(dest_dir, name)
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(120.0), follow_redirects=True,
                    ) as client:
                        async with client.stream("GET", url) as resp:
                            resp.raise_for_status()
                            with open(dest, "wb") as out:
                                async for chunk in resp.aiter_bytes(1 << 20):
                                    out.write(chunk)
                    return dest

                return fetch

            found.append(IncomingFile(name=name, mime=mime, fetch=_make_fetch()))
        return found

    async def _handle_inbound_safe(
        self, sender_id: str, display_name: str | None, text: str,
        files: Any = None,
    ) -> None:
        try:
            await self._handle_inbound(sender_id, display_name, text, files=files)
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

    async def _send_file(
        self, sender_id: str, path: str, *,
        name: str | None = None, mime: str | None = None,
        caption: str | None = None,
    ) -> None:
        """Multipart Graph send: the attachment rides as ``filedata``.

        Messenger attachments have no caption slot, so a caption goes out as
        its own text message first. Unlike :meth:`_graph_post` (fire-and-log,
        good enough for reply bubbles) this RAISES on a Graph error — strict
        callers record history from the outcome.
        """
        import json as _json

        try:
            size = os.path.getsize(path)
        except OSError as exc:
            raise ValueError(f"cannot read file to send: {path}") from exc
        if size > _MESSENGER_UPLOAD_LIMIT:
            raise ValueError(
                f"'{name or os.path.basename(path)}' is {size} bytes; Messenger "
                f"caps attachments at {_MESSENGER_UPLOAD_LIMIT} bytes",
            )
        if caption:
            await self._send_text(sender_id, caption)

        effective_mime = mime or "application/octet-stream"
        attachment_type = "file"
        for prefix in ("image", "video", "audio"):
            if effective_mime.startswith(prefix + "/"):
                attachment_type = prefix
                break

        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=httpx.Timeout(20.0))
        with open(path, "rb") as handle:
            resp = await self._client.post(
                _GRAPH_URL,
                params={"access_token": self._page_token()},
                data={
                    "recipient": _json.dumps({"id": sender_id}),
                    "message": _json.dumps({
                        "attachment": {
                            "type": attachment_type,
                            "payload": {"is_reusable": False},
                        },
                    }),
                },
                files={
                    "filedata": (
                        name or os.path.basename(path), handle, effective_mime,
                    ),
                },
                timeout=180.0,
            )
        if resp.status_code >= 400:
            raise ChannelAuthError(
                f"Messenger file send failed ({resp.status_code}): "
                f"{resp.text[:300]}",
            )

    async def _send_typing(self, sender_id: str) -> None:
        try:
            await self._graph_post(
                {"recipient": {"id": sender_id}, "sender_action": "typing_on"},
            )
        except Exception:  # noqa: BLE001
            logger.debug("messenger: typing indicator dropped", exc_info=True)
