"""Owner of in-process channel adapters: lifecycle + lookup.

The registry is a process-wide singleton. The server boot path calls
:meth:`ChannelRegistry.start_all_enabled` after :meth:`ConversationStorage.initialize`
so adapters can begin polling/subscribing as soon as the DB is ready.

Adapter classes are picked by ``channel_type`` from ``_ADAPTER_CLASSES``.
Unimplemented platforms raise :class:`ChannelNotImplemented`; the registry
catches it, disables the channel row, and stores the message in
``state.last_error`` so the API can show it.

On a user-initiated connect/enable (``install_if_missing=True``) the channel's
optional SDK extras (``python-telegram-bot``, ``telethon``, ``discord.py``,
``slack-bolt``) are pip-installed at runtime *before* the adapter starts — the
adapters import lazily, so the install is usable in-process with no restart.
Because the receive loop runs as a detached task, a fatal error raised inside
``_run`` is caught by a done-callback that disables the channel the same way,
rather than surfacing as an unretrieved-task warning.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.channels.base import BaseChannelAdapter
from app.channels.exceptions import ChannelNotImplemented
from app.utils.logger import logger


def _notify_channel_disabled(channel: dict, reason: str) -> None:
    """Surface an auto-disabled channel as a high-priority notification.

    A channel that fails to start at boot is silently flipped to ``enabled=False``
    with ``state.last_error``. After a restore onto a different host, session-based
    channels (telegram-userbot / whatsapp / zalo) whose session files don't
    transfer will fail exactly this way — the user needs a *warning*, not a
    silently-disabled channel. The notifications buffer replays to UI clients
    that connect after boot. Fully guarded — a notification must never affect
    channel startup.
    """
    try:
        from app.events import get_event_notifications

        ctype = channel.get("channel_type") or "channel"
        mode = channel.get("mode") or "bot"
        get_event_notifications().push(
            profile=channel.get("profile") or "admin",
            conversation_id="",
            conversation_title=f"Channel disabled: {ctype}",
            message_preview=(
                f"The {ctype} channel ({mode}) failed to start and was disabled: "
                f"{reason}. Re-link it under Settings → Channels."
            ),
            kind="channel_disabled",
            priority="high",
            extra={"channel_id": channel.get("id"), "channel_type": ctype},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"channels: disable notification push failed: {exc}")


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
    if channel_type == "zalo":
        if mode == "userbot":
            from app.channels.adapters.zalo_userbot import ZaloUserbotAdapter
            return ZaloUserbotAdapter
        # bot + notification both ride the Zalo Bot API transport.
        from app.channels.adapters.zalo import ZaloBotAdapter
        return ZaloBotAdapter
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

    async def start_for_channel(
        self, channel: dict, *, install_if_missing: bool = False,
    ) -> dict:
        """Build and start an adapter for the given channel row.

        Returns the (possibly mutated) channel dict — on
        :class:`ChannelNotImplemented` the channel is disabled in the DB and
        the dict is updated so callers don't see a stale enabled flag.

        When ``install_if_missing`` is True (user-initiated connect/enable via
        the API/CLI), the channel's optional SDK extras are pip-installed at
        runtime before the adapter starts. Boot (``start_all_enabled``) leaves
        it False so a missing package can't hang server startup on pip — such a
        boot-time failure surfaces through the ``_run`` done-callback instead.
        """
        if channel["channel_type"] == "main":
            return channel

        async with self._lock:
            existing = self._adapters.get(channel["id"])
        if existing:
            await self.stop_for_channel(channel["id"])

        try:
            if install_if_missing:
                await self._ensure_channel_feature_installed(channel)
            adapter_cls = _resolve_adapter_class(
                channel["channel_type"], channel.get("mode") or "bot",
            )
            adapter = adapter_cls(channel, self.storage)
            await adapter.start()
        except ChannelNotImplemented as exc:
            return await self._disable_channel(channel, str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                f"channels: start failed for {channel['channel_type']}",
            )
            return await self._disable_channel(channel, f"start failed: {exc}")

        async with self._lock:
            self._adapters[channel["id"]] = adapter

        # The receive loop runs as a detached task (BaseChannelAdapter.start
        # uses create_task and never awaits it). Attach a done-callback so a
        # fatal error raised *inside* ``_run`` — a missing SDK on an offline
        # boot, a bad token, a dead Node sidecar — disables the channel with a
        # visible ``last_error`` instead of asyncio logging "Task exception was
        # never retrieved" and leaving a row that still reads enabled=True.
        task = adapter._task  # noqa: SLF001
        if task is not None:
            task.add_done_callback(
                lambda t, cid=channel["id"]: self._on_run_task_done(cid, t),
            )
        return channel

    async def _ensure_channel_feature_installed(self, channel: dict) -> None:
        """Install the channel's SDK extras group if it isn't importable yet.

        Runs the (blocking) pip install off the event loop. A failure raises
        :class:`ChannelNotImplemented` so :meth:`start_for_channel`'s handler
        disables the channel with the error. No-op for channels with no Python
        extra (messenger/zalo/whatsapp) or whose deps are already present, so
        the common re-connect path costs only a cheap ``find_spec`` probe.
        """
        from app.features.installer import install_features
        from app.features.manifest import channel_feature_key, is_installed

        key = channel_feature_key(
            channel["channel_type"], channel.get("mode") or "bot",
        )
        if key is None or is_installed(key):
            return
        logger.info(
            f"channels: installing '{key}' for {channel['channel_type']} "
            f"channel_id={channel['id']}",
        )
        result = await asyncio.to_thread(install_features, [key])
        if result.error or key in result.failed or not is_installed(key):
            raise ChannelNotImplemented(
                f"Failed to install dependencies for {channel['channel_type']}: "
                f"{result.error or 'unknown error'}",
            )
        logger.info(
            f"channels: installed '{key}' for {channel['channel_type']}",
        )

    async def _disable_channel(self, channel: dict, reason: str) -> dict:
        """Flip a channel to ``enabled=False`` with a visible ``last_error``.

        Merges ``last_error`` into the existing ``state`` so durable markers
        (``last_update_id``, ``link_status``) survive, persists it, pushes the
        high-priority "Channel disabled" notification, and returns the mutated
        ``channel`` dict. Shared by the synchronous start handlers and the
        ``_run`` done-callback.
        """
        state = {**(channel.get("state") or {}), "last_error": reason}
        await self.storage.update_channel(
            channel["id"], enabled=False, state=state,
        )
        channel["enabled"] = False
        channel["state"] = state
        logger.warning(
            f"channels: {channel['channel_type']} disabled: {reason}",
        )
        _notify_channel_disabled(channel, reason)
        return channel

    def _on_run_task_done(self, channel_id: str, task: asyncio.Task) -> None:
        """Done-callback for an adapter's detached ``_run`` task.

        Deliberate stop/restart cancellation and clean exits are ignored;
        only a genuine exception schedules the disable path. Runs
        synchronously on the loop, so the DB write is deferred to a task.
        """
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is None:
            return
        try:
            asyncio.create_task(
                self._handle_run_failure(channel_id, task, exc),
                name=f"channel-run-failure:{channel_id}",
            )
        except RuntimeError:
            # Event loop is closing (interpreter shutdown) — nothing to do.
            logger.debug(
                f"channels: run-failure handler skipped for {channel_id} "
                "(loop closing)",
            )

    async def _handle_run_failure(
        self, channel_id: str, task: asyncio.Task, exc: BaseException,
    ) -> None:
        """Disable a channel whose ``_run`` loop died with an exception."""
        async with self._lock:
            adapter = self._adapters.get(channel_id)
            # Only act if the failed task is still the registered adapter's — a
            # concurrent restart may have already replaced it with a live one.
            if adapter is None or adapter._task is not task:  # noqa: SLF001
                return
            self._adapters.pop(channel_id, None)
        # Tear the adapter down so a notification-mode channel's delivery loop
        # and any in-flight reply forwarders don't outlive the dead receive
        # loop (``_task`` is already done; ``stop`` handles that gracefully).
        try:
            await adapter.stop()
        except Exception:  # noqa: BLE001
            logger.exception(f"channels: stop failed for {channel_id}")
        channel = await self.storage.get_channel(channel_id)
        if channel is None:
            return
        # An adapter that self-marked as unlinked already persisted
        # enabled=False + link_status; don't clobber that with a generic error.
        if (channel.get("state") or {}).get("link_status") == "unlinked":
            return
        logger.warning(
            f"channels: {channel.get('channel_type')} run loop failed: {exc}",
        )
        await self._disable_channel(channel, f"start failed: {exc}")

    async def stop_for_channel(self, channel_id: str) -> None:
        async with self._lock:
            adapter = self._adapters.pop(channel_id, None)
        if adapter is None:
            return
        try:
            await adapter.stop()
        except Exception:  # noqa: BLE001
            logger.exception(f"channels: stop failed for {channel_id}")

    async def restart_for_channel(
        self, channel_id: str, *, install_if_missing: bool = False,
    ) -> dict | None:
        await self.stop_for_channel(channel_id)
        channel = await self.storage.get_channel(channel_id)
        if not channel or not channel.get("enabled"):
            return channel
        return await self.start_for_channel(
            channel, install_if_missing=install_if_missing,
        )

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

    def notification_adapters_for_profile(
        self, profile: str,
    ) -> list[BaseChannelAdapter]:
        """Live (enabled + started) notification-mode adapters for ``profile``.

        Only channels that started successfully live in ``_adapters`` — one
        that failed to start was flipped ``enabled=False`` and is absent here
        (see :meth:`start_for_channel`). This is the set the
        ``send_notification`` tool can actually push to, and the basis for the
        tool's availability gate. Snapshot with ``list(...)`` before iterating:
        the mutating methods run under ``self._lock`` on the same loop, but
        callers on the hot path (``ReasoningAgent.__init__``) iterate without a
        lock, so the copy guards against a concurrent size change.
        """
        return [
            a
            for a in list(self._adapters.values())
            if a.profile == profile and a._is_notification_mode()  # noqa: SLF001
        ]


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


def has_notification_channel(profile: str) -> bool:
    """True iff ``profile`` has >=1 enabled notification-mode channel live now.

    Safe to call before the registry is initialized (server still booting, or
    CLI / test contexts with no channel subsystem): returns ``False`` instead
    of raising, so callers on the hot path — e.g. the ``send_notification``
    availability gate in ``ReasoningAgent.__init__`` — never crash.
    """
    try:
        registry = get_channel_registry()
    except RuntimeError:
        return False
    return bool(registry.notification_adapters_for_profile(profile))
