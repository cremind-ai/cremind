"""Zalo Bot API adapter (long-polling).

A hand-rolled REST client over the official Zalo bot platform, written in
Python using the core ``httpx`` dependency — no Zalo SDK is required. Each adapter owns one :class:`ZaloBotClient` and runs a
``getUpdates`` long-poll loop in :meth:`_run`.

Serves conversational ``bot`` mode and push-only ``notification`` mode over the
same bot transport (the notification behavior itself lives in
:class:`app.channels.notification_delivery.NotificationDeliveryMixin` on the base
class). The Zalo personal-account transport is the separate
:class:`app.channels.adapters.zalo_userbot.ZaloUserbotAdapter`.

A message posted in a Zalo group takes the second inbound path
(:meth:`BaseChannelAdapter._handle_group_inbound`) rather than the DM pipeline.
The bot has to be a member of the group and someone has to post in it before
Cremind knows the room exists — Zalo announces no membership change the way
Telegram's ``my_chat_member`` does.

Zalo Bot API quirks:
    - Base URL ``https://bot-api.zaloplatforms.com``, path ``/bot{token}/{method}``,
      every call is ``POST`` + JSON.
    - The bot token is shaped ``<numeric_id>:<secret>`` and embedded in the path.
    - ``getUpdates`` returns a **single** update object (not an array like
      Telegram), and a ``408`` error code means "no updates" (a normal long-poll
      timeout), which we swallow.
    - Text is capped at 2000 characters per message.
    - Message text is rendered literally: there is no markdown dialect at all.
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


def _message_epoch(value: Any) -> float | None:
    """A Zalo message's own send time in epoch seconds, or ``None``.

    Zalo reports a bare number where
    :func:`app.channels.base.platform_message_timestamp` expects a ``datetime``,
    so it gets its own coercion. Defensive because the time only sharpens the
    dedupe key: an odd value must weaken that key, never raise inside the poll
    loop.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class ZaloBotAdapter(BaseChannelAdapter):
    # A Zalo bot receives updates from every group it has been added to, and can
    # post back to that chat id.
    supports_group_chats = True
    # The Bot API names no members and reports no joins: a group is
    # discovered when somebody speaks in it, and the roster is whoever has.
    supports_group_roster = False
    supports_group_join_events = False
    reports_sender_is_bot = False

    # Zalo has no markdown: whatever the mirror wraps arrives as literal
    # characters, so a room's ``*Name*`` would reach it as two stray asterisks.
    bold_markup = ("", "")
    italic_markup = ("", "")

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
            me = await self._api.get_me()
        except ZaloApiError as exc:
            await self._api.aclose()
            self._api = None
            raise ChannelAuthError(f"Zalo getMe failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            await self._api.aclose()
            self._api = None
            raise ChannelAuthError(f"Zalo bot init failed: {exc}") from exc

        await self._store_self_from_get_me(me)

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

    async def _store_self_from_get_me(self, me: Any) -> None:
        """Record which bot account this channel speaks as, from ``getMe``.

        A bound group uses it to recognise (and drop) the posts its own member
        agents mirrored into the room. ``ZaloBotClient.call`` already unwraps the
        envelope's ``result``, so this is the bot object itself.

        Best-effort: the token has just validated, and an unfamiliar payload
        shape is no reason to refuse to poll — it costs the group layer one of
        its two echo defences, nothing more.
        """
        if not isinstance(me, dict):
            return
        user_id = str(me.get("id") or "").strip()
        if not user_id:
            return
        await self._store_self_identity(
            user_id=user_id,
            username=me.get("account_name"),
            is_bot=True,
            # No ``mention``: on Zalo a mention is a structured annotation
            # attached to a message, not a token anybody can type into one, so
            # there is no string an agent could write that would ping this bot.
            # …which makes the display name the ONLY way a person in a Zalo
            # group can address this account, so it is worth reaching for.
            display_name=str(
                me.get("display_name") or me.get("name") or "",
            ).strip() or None,
        )

    def _handle_update(self, update: Any) -> None:
        """Route one polled update to the DM pipeline or to a group room.

        The chat type is the whole routing decision, and it is read explicitly:
        a Zalo chat id is an opaque string, so a room's is indistinguishable
        from a person's and any id-shape heuristic would eventually answer a
        room in someone's DM, or fold everyone in a room into one conversation.
        """
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
        chat_type = str(chat.get("chat_type") or "").strip().lower()

        if chat_type in ("group", "supergroup"):
            # A group message belongs to the room, and it used to fall through
            # to the DM path below — which keys the conversation on the CHAT id,
            # so every member of the room shared one conversation and the reply
            # was addressed to the room as if it were a person.
            sender_id = str(sender.get("id") or "").strip()
            if not sender_id:
                return
            message_id = message.get("message_id")
            asyncio.create_task(
                self._handle_group_inbound_safe(
                    chat_id=chat_id,
                    chat_title=chat.get("title") or chat.get("name"),
                    chat_type=chat_type,
                    sender_id=sender_id,
                    # Zalo gives a group member a display name and no handle, so
                    # the name is all there is to attribute the post to.
                    sender_username=None,
                    display_name=sender.get("display_name") or sender.get("name"),
                    text=str(text),
                    platform_message_id=(
                        str(message_id) if message_id is not None else None
                    ),
                    platform_message_date=_message_epoch(message.get("date")),
                    sender_is_bot=bool(sender.get("is_bot")),
                ),
                name=f"zalo-group-inbound:{self.channel_id}:{chat_id}",
            )
            return

        # A DM keys on the chat id, not the sender id: that is the id
        # ``sendMessage`` wants back, and the two are not interchangeable.
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

    async def _handle_group_inbound_safe(self, **kwargs: Any) -> None:
        try:
            await self._handle_group_inbound(**kwargs)
        except Exception:  # noqa: BLE001
            logger.exception("zalo: group inbound handler failed")

    async def _send_text(self, sender_id: str, text: str) -> None:
        if self._api is None:
            raise ChannelAuthError("Zalo client not connected")
        # ``chat_id`` for a DM is the sender id we captured on inbound.
        for chunk in _split_for_messaging(text, _ZALO_TEXT_LIMIT):
            await self._api.send_message(sender_id, chunk)

    async def send_to_chat(self, chat_id: str, text: str) -> None:
        """Send to a room by its chat id — same call, an id nobody owns.

        Not routed through :meth:`_send_text` because that one documents its
        argument as a sender id; a group's chat id belongs to no user, and the
        two only look alike.
        """
        if self._api is None:
            raise ChannelAuthError("Zalo client not connected")
        for chunk in _split_for_messaging(text, _ZALO_TEXT_LIMIT):
            await self._api.send_message(chat_id, chunk)

    async def _send_typing(self, sender_id: str) -> None:
        if self._api is None:
            return
        try:
            await self._api.send_chat_action(sender_id, "typing")
        except Exception:  # noqa: BLE001
            # Typing is best-effort; the typing loop retries on the next tick.
            logger.debug("zalo: typing action dropped", exc_info=True)
