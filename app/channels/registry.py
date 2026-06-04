"""Owner of in-process channel adapters: lifecycle + lookup.

The registry is a process-wide singleton. The server boot path calls
:meth:`ChannelRegistry.start_all_enabled` after :meth:`ConversationStorage.initialize`
so adapters can begin polling/subscribing as soon as the DB is ready.

Adapter classes are picked by ``channel_type`` from ``_ADAPTER_CLASSES``.
Unimplemented platforms raise :class:`ChannelNotImplemented`; the registry
catches it, disables the channel row, and stores the message in
``state.last_error`` so the API can show it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.channels.base import BaseChannelAdapter
from app.channels.exceptions import ChannelNotImplemented
from app.utils.logger import logger


def _resolve_adapter_class(
    channel_type: str, mode: str,
) -> type[BaseChannelAdapter]:
    """Pick the adapter class for ``(channel_type, mode)``.

    Looks up the most specific (type, mode) match first, then falls back
    to the type-only default. Raises :class:`ChannelNotImplemented` when
    no adapter is registered for the pair.

    Imports are lazy so a fresh install isn't forced to import every
    platform SDK on boot — the Telegram bot adapter only pulls in
    ``python-telegram-bot``, the userbot adapter only pulls in Telethon,
    and so on.
    """

    if channel_type == "telegram":
        if mode == "userbot":
            from app.channels.adapters.telegram_userbot import (
                TelegramUserbotAdapter,
            )
            return TelegramUserbotAdapter
        from app.channels.adapters.telegram import TelegramAdapter
        return TelegramAdapter
    if channel_type == "whatsapp":
        from app.channels.adapters.whatsapp import WhatsappAdapter
        return WhatsappAdapter
    if channel_type == "discord":
        from app.channels.adapters.discord import DiscordAdapter
        return DiscordAdapter
    if channel_type == "messenger":
        from app.channels.adapters.messenger import MessengerAdapter
        return MessengerAdapter
    if channel_type == "slack":
        from app.channels.adapters.slack import SlackAdapter
        return SlackAdapter
    raise ChannelNotImplemented(
        f"No adapter registered for channel_type={channel_type!r} mode={mode!r}",
    )


class ChannelRegistry:
    def __init__(self, storage: Any) -> None:
        self.storage = storage
        # Keyed by channel id (uuid). channel_type alone isn't unique because
        # different profiles can each register telegram, etc.
        self._adapters: dict[str, BaseChannelAdapter] = {}
        self._lock = asyncio.Lock()

    # ── lifecycle ──

    async def start_all_enabled(self) -> None:
        """Start every enabled non-``main`` channel. Called once at server boot."""
        rows = await self.storage.list_enabled_external_channels()
        for ch in rows:
            await self.start_for_channel(ch)

    async def stop_all(self) -> None:
        async with self._lock:
            adapters = list(self._adapters.values())
            self._adapters.clear()
        for adapter in adapters:
            try:
                await adapter.stop()
            except Exception:  # noqa: BLE001
                logger.exception(f"channels: stop failed for {adapter.channel_id}")

    async def start_for_channel(self, channel: dict) -> dict:
        """Build and start an adapter for the given channel row.

        Returns the (possibly mutated) channel dict — on
        :class:`ChannelNotImplemented` the channel is disabled in the DB and
        the dict is updated so callers don't see a stale enabled flag.
        """
        if channel["channel_type"] == "main":
            return channel

        async with self._lock:
            existing = self._adapters.get(channel["id"])
        if existing:
            await self.stop_for_channel(channel["id"])

        try:
            adapter_cls = _resolve_adapter_class(
                channel["channel_type"], channel.get("mode") or "bot",
            )
            adapter = adapter_cls(channel, self.storage)
            await adapter.start()
        except ChannelNotImplemented as exc:
            await self.storage.update_channel(
                channel["id"],
                enabled=False,
                state={"last_error": str(exc)},
            )
            channel["enabled"] = False
            channel["state"] = {"last_error": str(exc)}
            logger.warning(
                f"channels: {channel['channel_type']} disabled: {exc}",
            )
            return channel
        except Exception as exc:  # noqa: BLE001
            await self.storage.update_channel(
                channel["id"],
                enabled=False,
                state={"last_error": f"start failed: {exc}"},
            )
            channel["enabled"] = False
            channel["state"] = {"last_error": f"start failed: {exc}"}
            logger.exception(
                f"channels: start failed for {channel['channel_type']}",
            )
            return channel

        async with self._lock:
            self._adapters[channel["id"]] = adapter
        return channel

    async def stop_for_channel(self, channel_id: str) -> None:
        async with self._lock:
            adapter = self._adapters.pop(channel_id, None)
        if adapter is None:
            return
        try:
            await adapter.stop()
        except Exception:  # noqa: BLE001
            logger.exception(f"channels: stop failed for {channel_id}")

    async def restart_for_channel(self, channel_id: str) -> dict | None:
        await self.stop_for_channel(channel_id)
        channel = await self.storage.get_channel(channel_id)
        if not channel or not channel.get("enabled"):
            return channel
        return await self.start_for_channel(channel)

    # ── status helpers (used by the API to decorate list responses) ──

    def status_for(self, channel_id: str) -> str:
        adapter = self._adapters.get(channel_id)
        if adapter is None:
            return "stopped"
        task = adapter._task  # noqa: SLF001
        if task is None or task.done():
            return "stopped"
        return "running"

    def get_adapter(self, channel_id: str) -> BaseChannelAdapter | None:
        """Return the live adapter instance, if any.

        The API's QR SSE endpoint uses this to subscribe to a per-channel
        pairing event stream that the adapter exposes via
        ``subscribe_qr`` / ``unsubscribe_qr``.
        """
        return self._adapters.get(channel_id)


_instance: ChannelRegistry | None = None


def get_channel_registry(storage: Any | None = None) -> ChannelRegistry:
    """Return the process-wide registry, lazily creating it on first call.

    ``storage`` is required on the first call (to wire the registry to its
    storage); subsequent calls may omit it.
    """
    global _instance
    if _instance is None:
        if storage is None:
            raise RuntimeError(
                "ChannelRegistry not initialized — pass storage on first call",
            )
        _instance = ChannelRegistry(storage)
    return _instance
