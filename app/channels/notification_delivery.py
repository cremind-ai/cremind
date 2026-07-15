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

Who may subscribe is gated by ``config.subscribe_auth`` (:data:`SUBSCRIBE_AUTH_METHODS`):
``open`` (default — anyone who ``/start``s), ``passcode`` (``/start <passcode>``),
``otp`` (a one-time code shown to the operator that the subscriber must echo),
``approval`` (the operator approves each pending subscriber), or ``allowlist``
(no self-subscribe; only ``config.target_chat_ids`` receive). ``otp`` reuses the
``pending_otp`` columns; ``approval`` a sender stays ``authenticated=False`` until
an operator flips it via ``PATCH /api/channels/{id}/senders/{sender_id}``.

Delivery is **live-only**: we subscribe to the bus and forward entries from
subscribe-time forward, never replaying the ring buffer, so a server restart
does not re-spam old notifications.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any

from app.channels.notification_filter import NotificationFilter, format_notification
from app.events.notifications_buffer import get_event_notifications
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

# Per-channel subscription-authentication methods (stored in
# ``config.subscribe_auth``). ``open`` is the default (and the behavior of
# channels created before this feature): anyone who ``/start``s is subscribed.
SUBSCRIBE_AUTH_METHODS = ("open", "passcode", "otp", "approval", "allowlist")

# One-time subscribe codes live as long as the conversational OTP (10 min).
_SUBSCRIBE_OTP_TTL_SECONDS = 600


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
        """Distinct send targets: static ``config.target_chat_ids`` ∪ subscribers.

        In ``allowlist`` subscription mode there are no self-subscribers
        (``/start`` is refused), so only the statically configured chat ids
        receive — any ``authenticated`` rows left over from a previous method
        are deliberately excluded so switching to allowlist locks the channel
        down immediately.
        """
        targets: list[str] = []
        seen: set[str] = set()

        raw_targets = (self.channel.get("config") or {}).get("target_chat_ids")  # type: ignore[attr-defined]
        for t in _split_csv(raw_targets):
            if t not in seen:
                seen.add(t)
                targets.append(t)

        if self._subscribe_auth() == "allowlist":
            return targets

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

    # ── subscription authentication ──

    def _subscribe_auth(self) -> str:
        """The channel's access-auth method — shared by every channel mode.

        Returns one of :data:`SUBSCRIBE_AUTH_METHODS`. ``config.subscribe_auth``
        is the canonical setting for all modes (notification + conversational);
        several back-compat fallbacks keep pre-existing channels working so an
        upgrade never silently opens a gated channel:

        1. ``config.subscribe_auth`` (canonical, written by the UI/CLI now).
        2. The legacy conversational ``auth_mode`` column — ``password`` →
           ``passcode``, ``otp`` → ``otp`` (bot/userbot channels created before
           unification).
        3. A configured passcode (``subscribe_passcode`` or the legacy
           conversational ``config.password``) → ``passcode``.
        4. ``open`` otherwise.
        """
        cfg = self.channel.get("config") or {}  # type: ignore[attr-defined]
        raw = str(cfg.get("subscribe_auth") or "").strip().lower()
        if raw in SUBSCRIBE_AUTH_METHODS:
            return raw

        legacy = str(self.channel.get("auth_mode") or "").strip().lower()  # type: ignore[attr-defined]
        if legacy == "password":
            return "passcode"
        if legacy in SUBSCRIBE_AUTH_METHODS and legacy != "open":
            # ``otp`` (and any future value already in the canonical set).
            return legacy

        if cfg.get("subscribe_passcode") or cfg.get("password"):
            return "passcode"
        return "open"

    def _help_text(self, auth: str) -> str:
        base = (
            "🔔 This is a Cremind *notification* channel.\n"
            "It delivers alerts here but does not chat.\n\n"
        )
        if auth == "passcode":
            line = "• /start <passcode> — subscribe (a passcode is required)\n"
        elif auth == "otp":
            line = (
                "• /start — request a one-time code, then reply with the code "
                "the admin gives you to subscribe\n"
            )
        elif auth == "approval":
            line = "• /start — request a subscription (an admin must approve you)\n"
        elif auth == "allowlist":
            line = "• Self-subscribe is disabled — an admin adds recipients\n"
        else:  # open
            line = "• /start — subscribe to notifications\n"
        return base + line + "• /stop — unsubscribe"

    async def _mark_subscribed(
        self, sender_id: str, display_name: str | None,
    ) -> None:
        sender = await self.storage.get_or_create_sender(  # type: ignore[attr-defined]
            self.channel_id, sender_id, display_name=display_name,  # type: ignore[attr-defined]
        )
        await self.storage.update_sender(  # type: ignore[attr-defined]
            sender["id"], authenticated=True,
            pending_otp=None, pending_otp_expires_at=None,
        )
        await self.send(  # type: ignore[attr-defined]
            sender_id,
            "✅ Subscribed. You'll receive Cremind notifications here.\n"
            "Send /stop to unsubscribe.",
        )

    def _push_operator_notification(
        self, *, title: str, preview: str, kind: str, extra: dict,
    ) -> None:
        """Best-effort push to the operator's notification bell. Never raises."""
        try:
            get_event_notifications().push(
                profile=self.profile,  # type: ignore[attr-defined]
                conversation_id="",
                conversation_title=title,
                message_preview=preview,
                kind=kind,
                priority="high",
                extra=extra,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                f"[channels:{self.channel_type}:notify] operator push failed"  # type: ignore[attr-defined]
            )

    async def _issue_subscribe_otp(
        self, sender_id: str, display_name: str | None,
    ) -> None:
        sender = await self.storage.get_or_create_sender(  # type: ignore[attr-defined]
            self.channel_id, sender_id, display_name=display_name,  # type: ignore[attr-defined]
        )
        if sender.get("authenticated"):
            await self.send(  # type: ignore[attr-defined]
                sender_id, "✅ You're already subscribed. Send /stop to unsubscribe.",
            )
            return
        code = f"{secrets.randbelow(1_000_000):06d}"
        await self.storage.update_sender(  # type: ignore[attr-defined]
            sender["id"], pending_otp=code,
            pending_otp_expires_at=time.time() + _SUBSCRIBE_OTP_TTL_SECONDS,
        )
        self._push_operator_notification(
            title=display_name or sender_id,
            preview=f"OTP {code} — {self.channel_type} subscribe request",  # type: ignore[attr-defined]
            kind="channel_otp",
            extra={
                "channel_id": self.channel_id,  # type: ignore[attr-defined]
                "channel_type": self.channel_type,  # type: ignore[attr-defined]
                "sender_id": sender_id,
                "sender_name": display_name or "",
                "otp": code,
            },
        )
        await self.send(  # type: ignore[attr-defined]
            sender_id,
            "🔐 To subscribe, reply with the one-time code the admin gives you.",
        )

    async def _verify_subscribe_otp(self, sender: dict, text: str) -> None:
        now = time.time()
        pending = sender.get("pending_otp")
        expires_at = sender.get("pending_otp_expires_at") or 0
        code = (text or "").strip()

        if pending and expires_at > now and code == str(pending):
            await self.storage.update_sender(  # type: ignore[attr-defined]
                sender["id"], authenticated=True,
                pending_otp=None, pending_otp_expires_at=None,
            )
            await self.send(  # type: ignore[attr-defined]
                sender["sender_id"],
                "✅ Subscribed. You'll receive Cremind notifications here.\n"
                "Send /stop to unsubscribe.",
            )
            return

        if pending and expires_at <= now:
            await self._issue_subscribe_otp(
                sender["sender_id"], sender.get("display_name"),
            )
            await self.send(  # type: ignore[attr-defined]
                sender["sender_id"],
                "⌛ That code expired — I've sent the admin a fresh one. "
                "Reply with the new code.",
            )
            return

        await self.send(  # type: ignore[attr-defined]
            sender["sender_id"],
            "❌ Incorrect code. Reply with the one-time code the admin gave you, "
            "or send /start to request a new one.",
        )

    async def _handle_subscribe(
        self, sender_id: str, display_name: str | None, auth: str, arg: str,
    ) -> None:
        if auth == "allowlist":
            await self.send(  # type: ignore[attr-defined]
                sender_id,
                "🔒 Self-subscribe is disabled on this channel. "
                "Ask an admin to add you as a recipient.",
            )
            return

        if auth == "passcode":
            passcode = (self.channel.get("config") or {}).get("subscribe_passcode")  # type: ignore[attr-defined]
            if not passcode:
                await self.send(  # type: ignore[attr-defined]
                    sender_id,
                    "🔒 This channel requires a passcode to subscribe, but none "
                    "is configured yet. Ask an admin to set one.",
                )
                return
            if arg.strip() != str(passcode):
                await self.send(  # type: ignore[attr-defined]
                    sender_id,
                    "🔒 This notification channel is passcode-protected.\n"
                    "Send `/start <passcode>` to subscribe.",
                )
                return
            await self._mark_subscribed(sender_id, display_name)
            return

        if auth == "approval":
            sender = await self.storage.get_or_create_sender(  # type: ignore[attr-defined]
                self.channel_id, sender_id, display_name=display_name,  # type: ignore[attr-defined]
            )
            if sender.get("authenticated"):
                await self.send(  # type: ignore[attr-defined]
                    sender_id,
                    "✅ You're already subscribed. Send /stop to unsubscribe.",
                )
                return
            # Notify the operator once per pending request (the sender row is
            # already the dedupe key — a repeat /start won't create a second).
            self._push_operator_notification(
                title=f"Subscribe request: {self.channel_type}",  # type: ignore[attr-defined]
                preview=(
                    f"{display_name or sender_id} wants to subscribe to the "
                    f"{self.channel_type} notification channel. "  # type: ignore[attr-defined]
                    "Approve it under Settings → Channels."
                ),
                kind="channel_subscribe_request",
                extra={
                    "channel_id": self.channel_id,  # type: ignore[attr-defined]
                    "channel_type": self.channel_type,  # type: ignore[attr-defined]
                    "sender_id": sender_id,
                    "sender_name": display_name or "",
                },
            )
            await self.send(  # type: ignore[attr-defined]
                sender_id,
                "⏳ Your subscription request was sent to the admin for approval. "
                "You'll start receiving notifications once approved.",
            )
            return

        if auth == "otp":
            await self._issue_subscribe_otp(sender_id, display_name)
            return

        # auth == "open" (default)
        await self._mark_subscribed(sender_id, display_name)

    # ── inbound control commands (replaces agent dispatch in notification mode) ──

    async def _handle_notification_command(
        self, sender_id: str, display_name: str | None, text: str,
    ) -> None:
        cmd, arg = _parse_command(text)
        auth = self._subscribe_auth()

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

        if cmd in _SUBSCRIBE_CMDS:
            await self._handle_subscribe(sender_id, display_name, auth, arg)
            return

        # A non-command message while an OTP challenge is outstanding is the
        # subscriber typing their code back.
        if auth == "otp":
            sender = await self.storage.get_or_create_sender(  # type: ignore[attr-defined]
                self.channel_id, sender_id, display_name=display_name,  # type: ignore[attr-defined]
            )
            if sender.get("pending_otp") and not sender.get("authenticated"):
                await self._verify_subscribe_otp(sender, text)
                return

        # /help, /status, or anything else → method-aware usage.
        await self.send(sender_id, self._help_text(auth))  # type: ignore[attr-defined]
