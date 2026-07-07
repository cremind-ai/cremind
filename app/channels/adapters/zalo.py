"""Zalo Bot API adapter (long-polling).

Ports OpenClaw's ``extensions/zalo`` (a hand-rolled REST client over the
official Zalo bot platform) to Python using the core ``httpx`` dependency — no
Zalo SDK is required. Each adapter owns one :class:`ZaloBotClient` and runs a
``getUpdates`` long-poll loop in :meth:`_run`.

Serves conversational ``bot`` mode and push-only ``notification`` mode over the
same bot transport (the notification behavior itself lives in
:class:`app.channels.notification_delivery.NotificationDeliveryMixin` on the base
class). The Zalo personal-account transport is the separate
:class:`app.channels.adapters.zalo_userbot.ZaloUserbotAdapter`.

Zalo Bot API quirks (mirrored from OpenClaw):
    - Base URL ``https://bot-api.zaloplatforms.com``, path ``/bot{token}/{method}``,
      every call is ``POST`` + JSON.
    - The bot token is shaped ``<numeric_id>:<secret>`` and embedded in the path.
    - ``getUpdates`` returns a **single** update object (not an array like
      Telegram), and a ``408`` error code means "no updates" (a normal long-poll
      timeout), which we swallow.
    - Text is capped at 2000 characters per message.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.channels.base import BaseChannelAdapter, _split_for_messaging
from app.channels.exceptions import ChannelAuthError, ChannelNotImplemented
from app.utils.logger import logger

_ZALO_API_BASE = "https://bot-api.zaloplatforms.com"
_ZALO_TEXT_LIMIT = 2000
_POLL_TIMEOUT_S = 30


class ZaloApiError(Exception):
    """A non-OK response from the Zalo Bot API."""

    def __init__(self, error_code: int | None, description: str) -> None:
        super().__init__(f"Zalo API error {error_code}: {description}")
        self.error_code = error_code
        self.description = description

    @property
    def is_polling_timeout(self) -> bool:
        # 408 on ``getUpdates`` is the long-poll "no new updates" signal, not a
        # real failure — the caller loops again immediately.
        return self.error_code == 408


class ZaloBotClient:
    """Minimal async client for the Zalo Bot API (one instance per channel)."""

    def __init__(self, token: str) -> None:
        self._token = token
        import httpx  # core dependency

        # A tight, explicit timeout so a hung socket doesn't stall the poll
        # loop forever. ``getUpdates`` overrides the read timeout per-call to
        # sit above the server-side long-poll window.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=5.0),
        )

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:  # noqa: BLE001
            pass

    async def call(
        self, method: str, body: dict | None = None, *, read_timeout: float | None = None,
    ) -> Any:
        """POST to ``/bot{token}/{method}`` and return the ``result`` payload.

        Raises :class:`ZaloApiError` when the API returns ``ok=false``.
        """
        import httpx

        url = f"{_ZALO_API_BASE}/bot{self._token}/{method}"
        kwargs: dict[str, Any] = {"json": body or {}}
        if read_timeout is not None:
            kwargs["timeout"] = httpx.Timeout(
                connect=10.0, read=read_timeout, write=20.0, pool=5.0,
            )
        resp = await self._client.post(url, **kwargs)
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise ZaloApiError(resp.status_code, f"non-JSON response: {resp.text[:200]}") from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            code = payload.get("error_code") if isinstance(payload, dict) else resp.status_code
            desc = payload.get("description") if isinstance(payload, dict) else resp.text[:200]
            raise ZaloApiError(code, str(desc or "unknown error"))
        return payload.get("result")

    async def get_me(self) -> Any:
        return await self.call("getMe")

    async def get_updates(self, timeout: int = _POLL_TIMEOUT_S) -> Any:
        # ``timeout`` is sent as a string per the Zalo API; the HTTP read
        # timeout sits a few seconds above it so the server's long-poll can
        # complete before httpx gives up.
        return await self.call(
            "getUpdates", {"timeout": str(timeout)}, read_timeout=timeout + 10,
        )

    async def send_message(self, chat_id: str, text: str) -> Any:
        return await self.call("sendMessage", {"chat_id": chat_id, "text": text})

    async def send_chat_action(self, chat_id: str, action: str = "typing") -> Any:
        return await self.call("sendChatAction", {"chat_id": chat_id, "action": action})


class ZaloBotAdapter(BaseChannelAdapter):
    def __init__(self, channel: dict, storage: Any) -> None:
        super().__init__(channel, storage)
        self._api: ZaloBotClient | None = None

    def _bot_token(self) -> str:
        token = (self.channel.get("config") or {}).get("bot_token")
        if not token:
            raise ChannelAuthError("Zalo channel missing bot_token")
        return token

    async def _run(self) -> None:
        if self.channel.get("mode") not in ("bot", "notification"):
            raise ChannelNotImplemented(
                f"ZaloBotAdapter does not support mode={self.channel.get('mode')!r}",
            )

        self._api = ZaloBotClient(self._bot_token())
        # Validate the token up-front so a bad credential surfaces as a clean
        # auth error (channel disabled with a helpful message) instead of a
        # silent poll loop that never receives anything.
        try:
            await self._api.get_me()
        except ZaloApiError as exc:
            await self._api.aclose()
            self._api = None
            raise ChannelAuthError(f"Zalo getMe failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            await self._api.aclose()
            self._api = None
            raise ChannelAuthError(f"Zalo bot init failed: {exc}") from exc

        try:
            await self._poll_loop()
        finally:
            api = self._api
            self._api = None
            if api is not None:
                await api.aclose()

    async def _poll_loop(self) -> None:
        assert self._api is not None
        while True:
            try:
                update = await self._api.get_updates(timeout=_POLL_TIMEOUT_S)
            except asyncio.CancelledError:
                raise
            except ZaloApiError as exc:
                if exc.is_polling_timeout:
                    continue  # normal long-poll timeout — poll again
                logger.warning(f"zalo: getUpdates failed ({exc}); backing off 5s")
                await asyncio.sleep(5)
                continue
            except Exception as exc:  # noqa: BLE001
                # httpx read timeout on the long-poll window is expected; treat
                # any transport error as transient and back off briefly.
                logger.debug(f"zalo: getUpdates transport error ({exc}); retrying")
                await asyncio.sleep(2)
                continue

            self._handle_update(update)

    def _handle_update(self, update: Any) -> None:
        if not isinstance(update, dict):
            return
        event_name = update.get("event_name") or ""
        message = update.get("message")
        if not isinstance(message, dict):
            return
        # v1 handles inbound text only; other event kinds (image/sticker/
        # unsupported) are logged and skipped.
        text = message.get("text")
        if event_name != "message.text.received" or not text:
            logger.debug(f"zalo: skipping event={event_name!r} (no text payload)")
            return
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "").strip()
        if not chat_id:
            return
        sender = message.get("from") or {}
        display_name = sender.get("display_name") or sender.get("name")
        asyncio.create_task(
            self._handle_inbound_safe(chat_id, display_name, str(text)),
            name=f"zalo-inbound:{self.channel_id}:{chat_id}",
        )

    async def _handle_inbound_safe(
        self, sender_id: str, display_name: str | None, text: str,
    ) -> None:
        try:
            await self._handle_inbound(sender_id, display_name, text)
        except Exception:  # noqa: BLE001
            logger.exception("zalo: inbound handler failed")

    async def _send_text(self, sender_id: str, text: str) -> None:
        if self._api is None:
            raise ChannelAuthError("Zalo client not connected")
        # ``chat_id`` for a DM is the sender id we captured on inbound.
        for chunk in _split_for_messaging(text, _ZALO_TEXT_LIMIT):
            await self._api.send_message(sender_id, chunk)

    async def _send_typing(self, sender_id: str) -> None:
        if self._api is None:
            return
        try:
            await self._api.send_chat_action(sender_id, "typing")
        except Exception:  # noqa: BLE001
            # Typing is best-effort; the typing loop retries on the next tick.
            logger.debug("zalo: typing action dropped", exc_info=True)
