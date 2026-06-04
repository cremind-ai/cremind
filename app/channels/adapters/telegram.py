"""Telegram bot adapter (long-polling).

Uses :pypi:`python-telegram-bot` (added to ``pyproject.toml`` as part of the
channels feature). Each adapter instance owns one :class:`telegram.Bot` and
runs ``getUpdates`` long-polling in :meth:`_run`.

Bot-mode only in v1; ``mode == "normal"`` (user account) is not supported.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.channels.base import BaseChannelAdapter
from app.channels.exceptions import ChannelAuthError, ChannelNotImplemented
from app.utils.logger import logger


class TelegramAdapter(BaseChannelAdapter):
    def __init__(self, channel: dict, storage: Any) -> None:
        super().__init__(channel, storage)
        self._bot: Any = None

    def _bot_token(self) -> str:
        token = (self.channel.get("config") or {}).get("bot_token")
        if not token:
            raise ChannelAuthError("Telegram channel missing bot_token")
        return token

    async def _build_bot(self) -> Any:
        """Construct a fresh :class:`telegram.Bot` with a private httpx pool.

        ``HTTPXRequest`` is configured explicitly so we get short, predictable
        timeouts (instead of httpx's defaults that can hang for ~15s on every
        retry) and a tight pool. The pool is per-Bot, so :meth:`_reset_bot`
        can drop a poisoned pool by simply discarding the Bot instance.
        """
        try:
            from telegram import Bot  # type: ignore
            from telegram.request import HTTPXRequest  # type: ignore
        except ImportError as exc:
            raise ChannelNotImplemented(
                "python-telegram-bot is not installed. "
                "Run `pip install python-telegram-bot>=21` to enable Telegram channels.",
            ) from exc
        request = HTTPXRequest(
            connection_pool_size=2,
            connect_timeout=10.0,
            read_timeout=20.0,
            write_timeout=20.0,
            pool_timeout=5.0,
        )
        return Bot(self._bot_token(), request=request)

    async def _reset_bot(self) -> None:
        """Discard the current bot's httpx pool so the next send opens fresh.

        Long idles between sends (e.g. while the agent waits on stdin from a
        long-running shell process) can leave the underlying TLS connection
        in the pool half-dead — Windows / NATs silently kill idle sockets
        and httpx surfaces it as a ``BrokenResourceError`` on the next send.
        Tearing down the bot sheds the dead pool entries unconditionally.
        """
        bot = self._bot
        self._bot = None
        if bot is None:
            return
        try:
            await bot.shutdown()
        except Exception as e:  # noqa: BLE001
            # Shutdown may itself fail on a broken pool; that's fine — we
            # just want the references gone so the GC reclaims the client.
            logger.debug(f"[channels:telegram] bot shutdown during reset raised: {e}")

    async def _run(self) -> None:
        if self.channel.get("mode") != "bot":
            raise ChannelNotImplemented("Telegram normal (user-account) mode is not implemented")

        try:
            self._bot = await self._build_bot()
        except (ChannelNotImplemented, ChannelAuthError):
            raise
        except Exception as exc:  # noqa: BLE001
            raise ChannelAuthError(f"Telegram bot init failed: {exc}") from exc

        offset = (self.channel.get("state") or {}).get("last_update_id")
        offset = int(offset) + 1 if offset else None

        while True:
            try:
                if self._bot is None:
                    self._bot = await self._build_bot()
                updates = await self._bot.get_updates(
                    offset=offset, timeout=30, allowed_updates=["message"],
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # NetworkError typically means the pool is stale (after a
                # long idle). Throw it away so the next iteration opens a
                # fresh httpx client.
                from telegram.error import NetworkError  # type: ignore
                if isinstance(exc, NetworkError):
                    await self._reset_bot()
                logger.warning(f"telegram: getUpdates failed ({exc}); backing off 5s")
                await asyncio.sleep(5)
                continue

            for update in updates:
                offset = update.update_id + 1
                msg = getattr(update, "message", None)
                if msg is None or not msg.text:
                    continue
                user = msg.from_user
                sender_id = str(user.id) if user else str(msg.chat.id)
                display_name = (
                    " ".join(filter(None, [user.first_name, user.last_name]))
                    if user else None
                )
                # Spawn instead of awaiting so two different senders in the
                # same poll batch don't block each other. Per-sender ordering
                # is preserved inside ``_handle_inbound`` via ``_inflight``.
                asyncio.create_task(
                    self._handle_inbound_safe(sender_id, display_name, msg.text),
                    name=f"telegram-inbound:{self.channel_id}:{sender_id}",
                )

            if updates:
                # Persist offset so a server restart doesn't replay the same
                # updates (Telegram retains them for 24h until acked by offset).
                try:
                    await self.storage.update_channel(
                        self.channel_id,
                        state={**(self.channel.get("state") or {}), "last_update_id": offset - 1},
                    )
                    self.channel["state"] = {
                        **(self.channel.get("state") or {}),
                        "last_update_id": offset - 1,
                    }
                except Exception:  # noqa: BLE001
                    logger.exception("telegram: failed to persist last_update_id")

    async def _handle_inbound_safe(
        self, sender_id: str, display_name: str | None, text: str,
    ) -> None:
        try:
            await self._handle_inbound(sender_id, display_name, text)
        except Exception:  # noqa: BLE001
            logger.exception("telegram: inbound handler failed")

    async def _send_text(self, sender_id: str, text: str) -> None:
        if self._bot is None:
            self._bot = await self._build_bot()
        # Telegram's chat_id for direct messages == the user id.
        chat_id = int(sender_id)
        await self._send_with_retry(chat_id, text)

    async def _send_typing(self, sender_id: str) -> None:
        """Show "typing…" to ``sender_id`` for ~5 seconds.

        Called by :meth:`BaseChannelAdapter._typing_loop` on a short cadence
        so the indicator stays visible for the duration of the run. No
        retry: if the platform call fails, the loop will tick again in a
        few seconds; logging here would be too noisy.
        """
        from telegram.constants import ChatAction  # type: ignore
        from telegram.error import NetworkError  # type: ignore
        if self._bot is None:
            self._bot = await self._build_bot()
        try:
            await self._bot.send_chat_action(
                chat_id=int(sender_id), action=ChatAction.TYPING,
            )
        except NetworkError:
            # Same stale-pool failure mode the message-send path handles.
            await self._reset_bot()

    async def _send_with_retry(self, chat_id: int, text: str) -> None:
        """Send with markdown + transient-error retry + plain-text fallback.

        On :class:`telegram.error.NetworkError` (which wraps httpx connection
        failures including the stale-pool ``BrokenResourceError`` we hit when
        the agent has been idle on a long-running tool), the underlying bot
        is torn down via :meth:`_reset_bot` and rebuilt before each retry —
        otherwise httpx would keep handing us the same dead connection from
        its pool and the retries would fail identically. On a plain
        :class:`telegram.error.BadRequest` (typically Markdown parse errors
        caused by stray ``*``/``_``/`` ` `` characters in LLM output), the
        same payload is retried once without ``parse_mode`` so the user
        still gets the content as plain text.
        """
        from telegram.error import BadRequest, NetworkError  # type: ignore

        attempts = 0
        last_exc: Exception | None = None
        max_attempts = 4
        while attempts < max_attempts:
            attempts += 1
            if self._bot is None:
                try:
                    self._bot = await self._build_bot()
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    await asyncio.sleep(0.75 * attempts)
                    continue
            try:
                await self._bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
                return
            except BadRequest:
                # Markdown parse failure (or some other 400) — retry as plain
                # text once. Same reset semantics if THAT raises NetworkError.
                try:
                    await self._bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        disable_web_page_preview=True,
                    )
                    return
                except NetworkError as exc:
                    last_exc = exc
                    await self._reset_bot()
                    await asyncio.sleep(0.75 * attempts)
                    continue
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    break
            except NetworkError as exc:
                last_exc = exc
                logger.warning(
                    f"telegram: send failed (attempt {attempts}/{max_attempts}); "
                    f"resetting connection pool. cause={exc}",
                )
                await self._reset_bot()
                await asyncio.sleep(0.75 * attempts)
                continue
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                break
        if last_exc is not None:
            raise last_exc
