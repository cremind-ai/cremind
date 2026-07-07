"""Notification-mode behavior mixed into every channel adapter.

A channel in ``mode == "notification"`` does not converse. Instead it relays
matching entries from the profile-scoped notifications bus
(:class:`app.events.notifications_bus.NotificationsStreamBus`) to the user's
chat. This mixin holds the platform-agnostic behavior; :class:`BaseChannelAdapter`
inherits it and calls into it from ``start`` / ``stop`` / ``_handle_inbound``.
The actual send goes through each transport's :meth:`_send_text` (via
``_send_chunked``), so no platform-specific code lives here.

Recipients reuse :class:`app.storage.models.ChannelSenderModel`: a sender row
with ``authenticated=True`` means "subscribed". That flag's only other consumer
is the ``_handle_inbound`` auth gate, which notification mode never reaches — so
the overload is conflict-free. Recipients survive restart (they live in the DB),
and are union'd with any static ``config.target_chat_ids`` (for groups/channels
a bot can't be ``/start``ed in via DM).

Delivery is **live-only**: we subscribe to the bus and forward entries from
subscribe-time forward, never replaying the ring buffer, so a server restart
does not re-spam old notifications.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.channels.notification_filter import NotificationFilter, format_notification
from app.events.notifications_bus import get_notifications_stream_bus
from app.utils.logger import logger


def _split_csv(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    else:
        items = [p.strip() for p in str(value).split(",")]
    return [i for i in items if i]


def _parse_command(text: str) -> tuple[str, str]:
    """Split an inbound message into (lowercased command, argument).

    Strips a trailing ``@botname`` from the command token (Telegram appends it
    in groups). Returns ``("", "")`` for empty input.
    """
    parts = (text or "").strip().split(maxsplit=1)
    if not parts:
        return "", ""
    head = parts[0].split("@", 1)[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    return head, arg


_SUBSCRIBE_CMDS = {"/start", "/subscribe"}
_UNSUBSCRIBE_CMDS = {"/stop", "/unsubscribe"}
_HELP_CMDS = {"/help", "/status"}

_HELP_TEXT = (
    "🔔 This is a Cremind *notification* channel.\n"
    "It delivers alerts here but does not chat.\n\n"
    "• /start — subscribe to notifications\n"
    "• /stop — unsubscribe"
)


class NotificationDeliveryMixin:
    """Notification-mode delivery + subscription behavior.

    Only active when ``self.channel["mode"] == "notification"``; on
    conversational channels these methods are defined but never invoked.
    Relies on attributes/methods provided by :class:`BaseChannelAdapter`
    (``channel``, ``profile``, ``channel_id``, ``channel_type``, ``storage``,
    ``send`` / ``_send_chunked``).
    """

    def _is_notification_mode(self) -> bool:
        return (self.channel.get("mode") or "") == "notification"  # type: ignore[attr-defined]

    # ── delivery loop ──

    async def _run_notification_delivery(self) -> None:
        """Subscribe to the profile notifications bus; filter + fan out to recipients.

        Cancellation-safe: unsubscribes in ``finally`` on both normal exit and
        cancellation (adapter ``stop``). Never dies on a single bad entry.
        """
        bus = get_notifications_stream_bus()
        queue = bus.subscribe(self.profile)  # type: ignore[attr-defined]
        filt = NotificationFilter.parse(self.channel.get("config"))  # type: ignore[attr-defined]
        logger.info(
            f"[channels:{self.channel_type}:notify] delivery subscribed "  # type: ignore[attr-defined]
            f"channel_id={self.channel_id} profile={self.profile}"  # type: ignore[attr-defined]
        )
        try:
            while True:
                entry = await queue.get()
                try:
                    if not filt.matches(entry):
                        continue
                    recipients = await self._notification_recipients()
                    if not recipients:
                        logger.info(
                            f"[channels:{self.channel_type}:notify] no recipients; "  # type: ignore[attr-defined]
                            f"dropping kind={entry.get('kind')}"
                        )
                        continue
                    text = format_notification(entry)
                    for target in recipients:
                        await self._send_chunked(target, text)  # type: ignore[attr-defined]
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception(
                        f"[channels:{self.channel_type}:notify] delivery failed for one entry"  # type: ignore[attr-defined]
                    )
        except asyncio.CancelledError:
            pass
        finally:
            bus.unsubscribe(self.profile, queue)  # type: ignore[attr-defined]
            logger.info(
                f"[channels:{self.channel_type}:notify] delivery unsubscribed "  # type: ignore[attr-defined]
                f"channel_id={self.channel_id}"  # type: ignore[attr-defined]
            )

    async def _notification_recipients(self) -> list[str]:
        """Distinct send targets: static ``config.target_chat_ids`` ∪ subscribers."""
        targets: list[str] = []
        seen: set[str] = set()

        raw_targets = (self.channel.get("config") or {}).get("target_chat_ids")  # type: ignore[attr-defined]
        for t in _split_csv(raw_targets):
            if t not in seen:
                seen.add(t)
                targets.append(t)

        try:
            senders = await self.storage.list_senders(self.channel_id)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            logger.exception(
                f"[channels:{self.channel_type}:notify] failed to list subscribers"  # type: ignore[attr-defined]
            )
            senders = []
        for s in senders:
            if not s.get("authenticated"):
                continue
            sid = s.get("sender_id")
            if sid and sid not in seen:
                seen.add(sid)
                targets.append(sid)
        return targets

    # ── inbound control commands (replaces agent dispatch in notification mode) ──

    async def _handle_notification_command(
        self, sender_id: str, display_name: str | None, text: str,
    ) -> None:
        cmd, arg = _parse_command(text)

        if cmd in _SUBSCRIBE_CMDS:
            passcode = (self.channel.get("config") or {}).get("subscribe_passcode")  # type: ignore[attr-defined]
            if passcode and arg.strip() != str(passcode):
                await self.send(  # type: ignore[attr-defined]
                    sender_id,
                    "🔒 This notification channel is passcode-protected.\n"
                    "Send `/start <passcode>` to subscribe.",
                )
                return
            sender = await self.storage.get_or_create_sender(  # type: ignore[attr-defined]
                self.channel_id, sender_id, display_name=display_name,  # type: ignore[attr-defined]
            )
            await self.storage.update_sender(sender["id"], authenticated=True)  # type: ignore[attr-defined]
            await self.send(  # type: ignore[attr-defined]
                sender_id,
                "✅ Subscribed. You'll receive Cremind notifications here.\n"
                "Send /stop to unsubscribe.",
            )
            return

        if cmd in _UNSUBSCRIBE_CMDS:
            sender = await self.storage.get_or_create_sender(  # type: ignore[attr-defined]
                self.channel_id, sender_id, display_name=display_name,  # type: ignore[attr-defined]
            )
            await self.storage.update_sender(sender["id"], authenticated=False)  # type: ignore[attr-defined]
            await self.send(  # type: ignore[attr-defined]
                sender_id,
                "🔕 Unsubscribed. Send /start to receive notifications again.",
            )
            return

        # /help, /status, or anything else → usage.
        await self.send(sender_id, _HELP_TEXT)  # type: ignore[attr-defined]
