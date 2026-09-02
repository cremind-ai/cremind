"""Adapter base class and shared inbound-message handling for channels.

Each platform adapter (Telegram, etc.) subclasses :class:`BaseChannelAdapter`
and implements :meth:`start`, :meth:`stop`, and :meth:`send`. The adapter
calls :meth:`_handle_inbound` from its receive loop; this base method:

1. Upserts the :class:`ChannelSenderModel` row for the (channel, sender_id).
2. Resolves (or creates) the per-sender conversation.
3. Applies the channel's auth gate (none / otp / password) before dispatching.
4. Enqueues the user message onto the existing per-conversation queue and
   spawns a one-shot reply-forwarder that buffers the assistant text from the
   conversation stream bus and delivers it back to the platform via
   :meth:`send`.

Auth flow is fully inbound (no UI bypass). For OTP, the code is generated
server-side and surfaced to the web UI through the existing
:mod:`app.events.notifications_buffer`.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import time
from abc import ABC, abstractmethod
from typing import Any, Sequence

from app.channels.attachments import (
    IncomingFile,
    discard_incoming_files,
    file_fallback_text,
    placeholder_text,
    stage_incoming_files,
)
from app.channels.exceptions import ChannelNotImplemented
from app.channels.notification_delivery import NotificationDeliveryMixin
from app.events import queue as event_queue
from app.events.notifications_buffer import get_event_notifications
from app.events.stream_bus import get_event_stream_bus
from app.channels.reply_target import (
    ReplyTarget,
    coerce_target,
    group_key,
    group_target,
    sender_target,
)
from app.config.user_config import replay_reasoning_enabled
from app.utils.common import convert_db_messages_to_history
from app.utils.logger import logger


_OTP_TTL_SECONDS = 600  # 10 minutes
# Telegram caps a single message at 4096 chars; the other platforms have
# similar (looser) caps. Keep some headroom for the markdown wrapper.
_MAX_MESSAGE_CHARS = 3500
# Cap on files auto-delivered with one reply. A runaway loop writing files
# must become a log line, not a message flood on somebody's phone.
_MAX_REPLY_FILES = 5


def _file_size(path: str) -> int | None:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def platform_message_timestamp(message: Any) -> float | None:
    """A platform message's own send time, in epoch seconds, or ``None``.

    Every account that receives a message reports the same send time, which is
    what lets :func:`app.channels.groups.keys.platform_key` tell N copies of one
    message apart from one person repeating themselves. Defensive on purpose: a
    missing or odd timestamp must weaken that key, never raise inside a receive
    loop.
    """
    date = getattr(message, "date", None)
    if date is None:
        return None
    try:
        return float(date.timestamp())
    except (AttributeError, TypeError, ValueError, OSError):
        return None


class PartialSendError(Exception):
    """A chunked send failed after some chunks had already been delivered.

    Raised by :meth:`BaseChannelAdapter.send_strict`. The recipient saw
    ``sent_chunks`` messages before the failure, which the caller needs to know:
    recording nothing would leave the transcript claiming a message the person
    actually received was never sent.
    """

    def __init__(self, sent_chunks: int, cause: Exception) -> None:
        super().__init__(
            f"delivery failed after {sent_chunks} chunk(s): {cause}",
        )
        self.sent_chunks = sent_chunks
        self.cause = cause


class BaseChannelAdapter(NotificationDeliveryMixin, ABC):
    """Lifecycle + inbound handling shared across all channel adapters.

    Subclasses are constructed by :class:`ChannelRegistry` with the channel
    row and a reference to :class:`ConversationStorage`. They own a long-
    running asyncio task started by :meth:`start` and stopped by :meth:`stop`.

    Subclasses must:
    - Implement :meth:`_run` (the receive loop) and :meth:`_send_text`
      (platform-specific send call).
    - Call :meth:`_handle_inbound` for every incoming user message.

    Adapters whose transport can see group rooms declare
    ``supports_group_chats`` and gain a second, independent entry point:
    :meth:`_handle_group_inbound` for a message addressed to a platform group,
    which goes through :mod:`app.channels.groups` rather than the per-sender
    conversation pipeline.

    ``bold_markup`` / ``italic_markup`` exist because one markdown dialect does
    not travel: a single-asterisk ``*bold*`` is bold on Telegram, italic on
    Discord, and two literal asterisks on Zalo, which sends plain text. Anything
    written for a room goes through :meth:`bold` / :meth:`italic` so it reads as
    emphasis wherever it lands.

    Notification mode (``channel["mode"] == "notification"``) is layered on top
    via :class:`NotificationDeliveryMixin`: the transport's own ``_run`` still
    connects and routes inbound (so ``/start`` subscribe works), but inbound is
    handled as control commands instead of agent dispatch, and a second task
    (:meth:`_run_notification_delivery`) relays the notifications bus outward.
    """

    # Whether this transport can take part in a platform group at all. Read off
    # the CLASS (never an instance) by
    # :func:`app.channels.registry.group_capable_channel_types`, so the Channels
    # settings page only offers the toggle where it can work.
    supports_group_chats: bool = False

    # Whether the platform can be asked for a group's member list. False means
    # the roster is only ever who has posted, which the API reports so the UI can
    # say so rather than showing an empty list that looks broken.
    supports_group_roster: bool = False

    # Whether the platform tells us when our account is added to a group. False
    # means a group is discovered by its first message instead — same outcome,
    # one message later.
    supports_group_join_events: bool = False

    # Whether the platform can list the groups the account is ALREADY in. This
    # is what makes pre-existing groups reachable: nobody added the account to
    # them, so no join event is coming and no notification is owed — the
    # operator picks them instead. See :meth:`fetch_joined_groups`.
    supports_group_listing: bool = False

    # Whether inbound messages carry a trustworthy "this author is a bot" flag.
    # False disables the consecutive-bot-messages brake, which has nothing to
    # count without it.
    reports_sender_is_bot: bool = False

    # Whether this transport can deliver a FILE outward. Read off the CLASS
    # like ``supports_group_chats`` (dry-run previews consult it before any
    # adapter instance is involved). False routes every file send through the
    # fallback notice instead — the recipient learns a file exists rather than
    # receiving silence.
    supports_file_send: bool = False

    # Telegram's legacy markdown — the dialect this codebase has always written.
    # Adapters whose platform spells emphasis differently override them; one
    # whose platform has no markup at all sets ``("", "")`` and gets plain text.
    bold_markup: tuple[str, str] = ("*", "*")
    italic_markup: tuple[str, str] = ("_", "_")

    def __init__(self, channel: dict, storage: Any) -> None:
        self.channel = channel
        self.storage = storage
        self._task: asyncio.Task | None = None
        # Notification-mode delivery loop (mode == "notification" only).
        self._notif_task: asyncio.Task | None = None
        # Periodic "what groups is this account in?" reconcile, on the platforms
        # that can answer. Owned here so it dies with the adapter.
        self._group_sweep_task: asyncio.Task | None = None
        # In-flight forwarder tasks, keyed by ``ReplyTarget.key`` — a bare sender
        # id for a DM, ``cg:<group id>`` for a platform group. At most one is
        # live per target, which serializes forwarders so a new one never absorbs
        # the tail of the previous run's events from the shared stream bus. A
        # forwarder ends at the first terminal event it sees, so runs that need
        # one AFTER it are counted in ``_pending_runs`` and get a chained
        # forwarder instead.
        self._inflight: dict[str, asyncio.Task] = {}
        # Per-target count of runs still owed a forwarder (the live one included).
        # A mid-turn message is folded into the running turn and needs no run of
        # its own, so this only grows when a genuinely new run is started.
        self._pending_runs: dict[str, int] = {}
        # Per-sender locks that make the dispatch decision in ``_handle_inbound``
        # atomic. Transports may spawn one ``_handle_inbound`` task per inbound
        # message (e.g. Telegram), so two messages arriving in the same poll
        # batch could otherwise interleave park and forwarder bookkeeping. Kept
        # for the adapter's lifetime (one small lock per distinct sender, like
        # ``_access_requested``); not pruned because a task may hold a reference
        # across the acquire boundary, so removing an "unlocked" entry could
        # hand out two parallel locks.
        self._inbound_locks: dict[str, asyncio.Lock] = {}

        # Sender ids we've already raised an operator "access request"
        # notification for (``approval`` conversational auth), to avoid
        # re-notifying on every message from a still-pending sender. In-memory
        # only — at worst one extra notification per process restart.
        self._access_requested: set[str] = set()

        # Pairing / interactive-auth pub/sub. The same plumbing serves
        # WhatsApp's QR scan flow ({kind: "qr"}), the eventual Telegram
        # userbot code/password flow ({kind: "code_required"} /
        # {kind: "password_required"}), and the terminal {kind: "ready"}
        # event. The latest non-trivial event is cached so a UI client
        # subscribing mid-pairing immediately gets the current state.
        self._auth_subscribers: list[asyncio.Queue] = []
        self._latest_auth_event: dict | None = None

        # Everything this adapter remembers about the platform groups it is in:
        # per-group ordering locks, the inbound dedupe ring, and the loop brakes'
        # counters. Volatile by design — the durable facts are ``channel_groups``
        # rows, and a second source of truth for them is what a restart is for.
        from app.channels.groups.runtime import ChannelGroupRuntime

        self.groups = ChannelGroupRuntime()

    @property
    def channel_id(self) -> str:
        return self.channel["id"]

    @property
    def profile(self) -> str:
        return self.channel["profile"]

    @property
    def channel_type(self) -> str:
        return self.channel["channel_type"]

    @property
    def auth_mode(self) -> str:
        return self.channel.get("auth_mode") or "none"

    @property
    def response_mode(self) -> str:
        return self.channel.get("response_mode") or "normal"

    # ── lifecycle ──

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        logger.info(f"[channels:{self.channel_type}] adapter starting channel_id={self.channel_id}")
        self._task = asyncio.create_task(
            self._run(), name=f"channel:{self.channel_type}:{self.channel_id}",
        )
        # Notification-mode channels also run a bus-consuming delivery loop
        # alongside the transport's receive loop. The transport still receives
        # inbound (for /start subscribe), but replies are control-command acks
        # rather than agent dispatch (see ``_handle_inbound``).
        if self._is_notification_mode():
            self._notif_task = asyncio.create_task(
                self._run_notification_delivery(),
                name=f"channel-notify:{self.channel_type}:{self.channel_id}",
            )
        # Platforms that can enumerate the account's groups get a periodic
        # reconcile, which is the only way to notice a group joined while
        # Cremind was down — no event is replayed for those.
        if type(self).supports_group_listing and self.groups_enabled():
            from app.channels.groups.sweep import run_sweep_loop

            self._group_sweep_task = asyncio.create_task(
                run_sweep_loop(self),
                name=f"channel-groups:{self.channel_type}:{self.channel_id}",
            )

    async def stop(self) -> None:
        # Cancel any in-flight per-sender forwarders so shutdown doesn't
        # leave dangling tasks subscribed to the stream bus. Clearing the
        # registries first also stops the done-callbacks chaining replacements.
        self._pending_runs.clear()
        inflight = list(self._inflight.values())
        self._inflight.clear()
        for fwd in inflight:
            if not fwd.done():
                fwd.cancel()
        for fwd in inflight:
            try:
                await fwd
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        sweep = self._group_sweep_task
        self._group_sweep_task = None
        if sweep is not None and not sweep.done():
            sweep.cancel()
            try:
                await sweep
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # Tear down the notification delivery loop (if any) so it unsubscribes
        # from the bus and doesn't outlive the adapter.
        notif = self._notif_task
        self._notif_task = None
        if notif is not None and not notif.done():
            notif.cancel()
            try:
                await notif
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        task = self._task
        self._task = None
        if task is None or task.done():
            logger.info(f"[channels:{self.channel_type}] adapter stopped channel_id={self.channel_id}")
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        logger.info(f"[channels:{self.channel_type}] adapter stopped channel_id={self.channel_id}")

    @abstractmethod
    async def _run(self) -> None:
        """Long-running receive loop. Must be cancellation-safe."""

    @abstractmethod
    async def _send_text(self, sender_id: str, text: str) -> None:
        """Send a plain text message to ``sender_id`` on the platform."""

    # Public alias exercised by the reply forwarder.
    async def send(self, sender_id: str, text: str) -> None:
        """Send a single message, swallowing exceptions so one bad bubble
        doesn't abandon the rest of the reply stream."""
        try:
            await self._send_text(sender_id, text)
        except Exception:  # noqa: BLE001
            logger.exception(
                f"channels[{self.channel_type}]: send to {sender_id} failed",
            )

    async def send_strict(self, sender_id: str, text: str) -> int:
        """Chunked send that RAISES on the first failure. Returns chunks sent.

        The strict counterpart of :meth:`send` + :meth:`_send_chunked`. Those
        swallow transport errors on purpose — a reply stream shouldn't be
        abandoned because one bubble failed — but the direct-send path
        (:mod:`app.channels.direct_send`) reports per-recipient outcomes and
        only records a message in the client's conversation history once it
        really went out, so it needs the error, not a log line.

        A long message goes out as several platform messages, so failure is not
        all-or-nothing: if an early chunk landed and a later one didn't, the
        recipient really did see part of it. That case raises
        :class:`PartialSendError` carrying the count, so the caller can record
        what was actually delivered instead of pretending nothing was.
        """
        text = (text or "").strip()
        if not text:
            return 0
        sent = 0
        for chunk in _split_for_messaging(text, _MAX_MESSAGE_CHARS):
            try:
                await self._send_text(sender_id, chunk)
            except Exception as exc:  # noqa: BLE001
                if sent:
                    raise PartialSendError(sent, exc) from exc
                raise
            sent += 1
        return sent

    # ── file delivery (outbound) ──

    async def _send_file(
        self, sender_id: str, path: str, *,
        name: str | None = None, mime: str | None = None,
        caption: str | None = None,
    ) -> None:
        """Send one file to a person. Adapters that can, override this and set
        ``supports_file_send``; the rest say so and callers degrade to a text
        notice."""
        raise ChannelNotImplemented(
            f"{self.channel_type} channels cannot send files",
        )

    async def _send_file_to_chat(
        self, chat_id: str, path: str, *,
        name: str | None = None, mime: str | None = None,
        caption: str | None = None,
    ) -> None:
        """Room-addressed twin of :meth:`_send_file` — a chat id is nobody's
        sender id (see :meth:`send_to_chat`)."""
        raise ChannelNotImplemented(
            f"{self.channel_type} channels cannot send files to a chat id",
        )

    async def send_file(
        self, sender_id: str, path: str, *,
        name: str | None = None, mime: str | None = None,
        caption: str | None = None,
    ) -> None:
        """Send one file, swallowing errors (mirror of :meth:`send`).

        On a transport with no file support the recipient gets
        :func:`file_fallback_text` instead — the file's NAME, never its server
        path — so they at least learn something was produced for them.
        """
        display = name or os.path.basename(path)
        try:
            await self._send_file(
                sender_id, path, name=name, mime=mime, caption=caption,
            )
        except ChannelNotImplemented:
            await self.send(sender_id, file_fallback_text(display, _file_size(path)))
        except Exception:  # noqa: BLE001
            logger.exception(
                f"channels[{self.channel_type}]: file send to {sender_id} failed",
            )

    async def send_file_strict(
        self, sender_id: str, path: str, *,
        name: str | None = None, mime: str | None = None,
        caption: str | None = None,
    ) -> None:
        """File send that RAISES (mirror of :meth:`send_strict`).

        ``ChannelNotImplemented`` propagates so programmatic callers (direct
        send, notify) can report "this transport can't carry files" honestly
        instead of logging it away.
        """
        await self._send_file(
            sender_id, path, name=name, mime=mime, caption=caption,
        )

    def _derive_phone(self, sender_id: str) -> str | None:
        """Return the contact's phone (canonical digits) implied by ``sender_id``.

        Default: ``None`` — on most platforms the sender id is an opaque
        account id that says nothing about the person's number. WhatsApp
        overrides this because its ``<digits>@s.whatsapp.net`` JIDs *are* the
        phone number, which is what lets a spreadsheet of numbers resolve to
        known contacts. Platform-derived values are authoritative enough to
        backfill an empty column (see ``get_or_create_sender``), never to
        overwrite one.
        """
        return None

    # ── auth-event pub/sub (consumed by the API's /auth-events SSE) ──

    def subscribe_auth_events(self) -> asyncio.Queue:
        """Return a queue that receives interactive-pairing events.

        On subscribe, the most recent cached event (QR data URL, code
        prompt, ready, …) is replayed once so a UI client opening the
        page mid-flow doesn't have to wait for the next refresh tick.
        """
        queue: asyncio.Queue = asyncio.Queue()
        if self._latest_auth_event is not None:
            queue.put_nowait(self._latest_auth_event)
        self._auth_subscribers.append(queue)
        return queue

    def unsubscribe_auth_events(self, queue: asyncio.Queue) -> None:
        try:
            self._auth_subscribers.remove(queue)
        except ValueError:
            pass

    def _publish_auth_event(self, event: dict) -> None:
        """Fan an event out to all live subscribers and cache it for replay."""
        # Don't cache transient diagnostic kinds — those should not replay
        # to a fresh subscriber as if they were the current state. ``unlinked``
        # is excluded so a re-paired adapter doesn't replay a stale event;
        # the persisted ``state.link_status`` is the durable source of truth.
        if event.get("kind") not in {"send_error", "error", "unlinked"}:
            self._latest_auth_event = event
        for queue in list(self._auth_subscribers):
            try:
                queue.put_nowait(event)
            except Exception:  # noqa: BLE001
                pass

    async def _mark_unlinked(
        self,
        reason: str,
        *,
        auto_disable: bool = True,
        detail: str | None = None,
    ) -> None:
        """Persist a remote-side unlink, broadcast it, and stop the run task.

        Called by an adapter when the platform reports the session has been
        logged out, revoked, or otherwise invalidated from the user's side
        (WhatsApp Linked Devices logout, Telegram Active Sessions revoke).

        Idempotent — if ``state.link_status`` is already ``"unlinked"`` the
        DB write is skipped, but the publish + teardown still fire so a
        late-arriving subscriber and any straggling adapter task are still
        cleaned up.

        Schedules teardown as a detached task because awaiting ``stop()``
        from inside the run loop would cancel the calling task itself.
        """
        current_state = dict(self.channel.get("state") or {})
        if current_state.get("link_status") != "unlinked":
            new_state = {
                **current_state,
                "link_status": "unlinked",
                # Milliseconds, matching the ``updated_at`` column convention
                # in ``ConversationStorage.update_channel``.
                "unlinked_at": time.time() * 1000,
                "unlinked_reason": reason,
            }
            if detail:
                new_state["last_error"] = detail
            update_kwargs: dict[str, Any] = {"state": new_state}
            if auto_disable:
                update_kwargs["enabled"] = False
            try:
                updated = await self.storage.update_channel(
                    self.channel_id, **update_kwargs,
                )
                if updated is not None:
                    self.channel = updated
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"channels[{self.channel_type}]: persist unlink failed",
                )

        self._publish_auth_event({
            "kind": "unlinked",
            "reason": reason,
            "logged_out": True,
            "detail": detail,
        })

        from app.channels.registry import get_channel_registry

        asyncio.create_task(
            get_channel_registry().stop_for_channel(self.channel_id),
            name=f"channel-unlink-stop:{self.channel_id}",
        )

    async def _mark_linked(self) -> None:
        """Clear unlinked-state markers after a successful (re-)pair.

        No-op when the channel is already considered linked, so the common
        case (every successful start) is one cheap read with no DB write.
        """
        state = dict(self.channel.get("state") or {})
        if not any(k in state for k in ("link_status", "unlinked_at", "unlinked_reason")):
            return
        state.pop("link_status", None)
        state.pop("unlinked_at", None)
        state.pop("unlinked_reason", None)
        try:
            updated = await self.storage.update_channel(self.channel_id, state=state)
            if updated is not None:
                self.channel = updated
        except Exception:  # noqa: BLE001
            logger.exception(
                f"channels[{self.channel_type}]: clear unlink failed",
            )

    async def _store_self_identity(
        self,
        *,
        user_id: str,
        username: str | None,
        is_bot: bool,
        mention: str | None = None,
        alt_ids: list[str] | None = None,
        display_name: str | None = None,
    ) -> None:
        """Record which platform account this channel speaks as.

        Knowing our own platform id is what lets a group ignore its own posts. In
        a real group we receive everything, our own messages included, and
        re-ingesting one would turn every answer into a new question.

        ``mention`` is the token that pings this account on that platform, which
        no two of them spell alike: ``@name`` on Telegram, ``<@id>`` on Discord,
        ``<@Uid>`` on Slack, ``@digits`` on WhatsApp. It is what tells the group
        prompt how the agent is addressed, and what
        :func:`app.channels.groups.inbound._mention_visible` looks for when
        deciding whether a woken agent can see why.

        ``display_name`` is the name the OTHER members of a group see above our
        messages — "Lý Nguyen", not the profile name and not the agent's own
        name. In a group people address each other by that name, so without it
        the agent cannot tell a message aimed at itself from one aimed at
        somebody else: it reads "Lý Nguyen, what time is it?" as a question for
        a third party. Several platforms have no username at all (WhatsApp,
        Zalo), which makes this the only handle they have.

        ``alt_ids`` are the other ids the same account is seen under. WhatsApp
        reports a participant as ``<digits>@s.whatsapp.net`` on one device and
        ``<opaque>@lid`` on another, so an echo check against a single id misses
        our own message and the room starts answering itself.

        Persisted on the channel row, so a restart knows who "we" are before any
        adapter has connected. Best-effort: an adapter must still start when the
        write fails.
        """
        if not user_id:
            # Loud, because the consequence is silent and remote: without our own
            # id a group cannot recognise its own posts, and on a platform that
            # flags no bots (WhatsApp, the userbots) this is the only echo
            # defence there is.
            logger.warning(
                f"channels[{self.channel_type}]: no account id reported for "
                f"profile {self.profile} — a group chat cannot recognise this "
                "channel's own messages",
            )
            return
        identity: dict[str, Any] = {
            "user_id": str(user_id),
            "username": (username or "").lstrip("@") or None,
            "is_bot": bool(is_bot),
        }
        # Written only when the platform reports them, so a transport with no
        # mention syntax and one id per account keeps the row it always had.
        if mention:
            identity["mention"] = str(mention)
        if (display_name or "").strip():
            identity["display_name"] = str(display_name).strip()
        other_ids = [
            str(value).strip() for value in (alt_ids or []) if str(value or "").strip()
        ]
        if other_ids:
            identity["alt_ids"] = other_ids
        state = dict(self.channel.get("state") or {})
        if state.get("self_identity") != identity:
            state["self_identity"] = identity
            try:
                updated = await self.storage.update_channel(
                    self.channel_id, state=state,
                )
                self.channel = (
                    updated if updated is not None
                    else {**self.channel, "state": state}
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"channels[{self.channel_type}]: persist self identity failed",
                )

    def submit_auth_input(self, payload: dict) -> bool:
        """Receive user-typed pairing input (verification code, 2FA password).

        Default implementation is a no-op for adapters that don't have an
        interactive flow (e.g. WhatsApp uses passive QR scanning). Adapters
        that do — Telegram userbot — override this to deliver the input
        into their auth-flow ``Future``.

        Returns ``True`` if the input was accepted; ``False`` otherwise (no
        auth in progress or wrong shape). Callers should surface the latter
        as ``HTTP 409``.
        """
        return False

    def reset_session(self) -> None:
        """Erase this channel's persisted pairing session.

        Called by the repair endpoint (``POST /api/channels/{id}/repair``)
        between stopping the adapter and starting it again, so the next start
        has nothing to restore and must run the interactive flow from scratch.

        This exists because a saved session that the platform has since
        invalidated — the same account paired somewhere else, a device revoked
        — is indistinguishable on disk from a good one. Every session-based
        adapter prefers restoring it, so a dead session means the pairing flow
        is never entered again and no QR/code is ever produced. Deleting the
        session is the only way back, and doing it here keeps the channel row
        (its senders, its bound groups) instead of losing them to the
        delete-and-recreate that was previously the only recovery.

        Default is a no-op: adapters authenticated by a config token have no
        session to erase. **Must be called with the adapter stopped** — on
        Windows a live sidecar still holds its credentials file open.
        """
        return None

    def _rmtree_session(self, path: str) -> None:
        """Remove a persisted-session directory, tolerating a lingering lock.

        The sidecar process was killed moments ago and Windows releases its
        file handles asynchronously, so a single ``rmtree`` can silently leave
        the credentials behind — which looks exactly like the bug this is
        meant to fix. Retry briefly, then say so rather than reporting a reset
        that didn't happen.
        """
        for _ in range(3):
            shutil.rmtree(path, ignore_errors=True)
            if not os.path.exists(path):
                return
            time.sleep(0.1)
        if os.path.exists(path):
            logger.warning(
                f"channels[{self.channel_type}]: session directory not fully "
                f"removed (files may still be locked): {path}",
            )

    async def _send_typing(self, sender_id: str) -> None:
        """Tell the platform to show "typing…" to ``sender_id``.

        Default is a no-op; platforms that support typing indicators
        (Telegram, Discord, Messenger, WhatsApp via Baileys) override this.
        Most platforms' indicators auto-expire after a few seconds, so the
        caller is expected to invoke this on a short loop, not just once.
        """
        return None

    async def _typing_loop(self, sender_id: str, *, interval: float = 4.0) -> None:
        """Keep the typing indicator alive until cancelled.

        Telegram's indicator lasts ~5s; Discord ~10s; Messenger ~20s. The
        4-second default fits all three. Failures (transient network errors)
        are logged and ignored — the loop just retries on the next tick.
        Cancellation is the only exit path.
        """
        while True:
            try:
                await self._send_typing(sender_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug(
                    f"channels[{self.channel_type}]: typing indicator failed",
                    exc_info=True,
                )
            await asyncio.sleep(interval)

    async def _send_typing_to_chat(self, chat_id: str) -> None:
        """Show "typing…" in a platform ROOM. Default no-op.

        Separate from :meth:`_send_typing` for the same reason
        :meth:`send_to_chat` is separate from :meth:`_send_text`: a room's id is
        nobody's sender id, and on Telegram it is a negative number that
        addresses no user at all.
        """
        return None

    async def _typing_loop_for(
        self, target: ReplyTarget, *, interval: float = 4.0,
    ) -> None:
        """Keep the right indicator alive for whichever destination this is."""
        while True:
            try:
                if target.is_group:
                    await self._send_typing_to_chat(target.address)
                else:
                    await self._send_typing(target.address)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.debug(
                    f"channels[{self.channel_type}]: typing indicator failed",
                    exc_info=True,
                )
            await asyncio.sleep(interval)

    async def _send_reply(self, target: ReplyTarget, text: str) -> None:
        """Send one bubble to wherever this run is answering."""
        if target.is_group:
            await self.send_to_chat_chunked(target.address, text)
        else:
            await self._send_chunked(target.address, text)

    async def _send_reply_file(
        self, target: ReplyTarget, path: str, *,
        name: str | None = None, mime: str | None = None,
    ) -> None:
        """Send one file to wherever this run is answering (never raises)."""
        if target.is_group:
            await self.send_file_to_chat(target.address, path, name=name, mime=mime)
        else:
            await self.send_file(target.address, path, name=name, mime=mime)

    async def _send_chunked(self, sender_id: str, text: str) -> None:
        """Send ``text`` as one or more messages, each ≤ ``_MAX_MESSAGE_CHARS``.

        Splits on newline boundaries when possible to avoid breaking inside a
        markdown construct. Empty input is a no-op.
        """
        text = (text or "").strip()
        if not text:
            return
        for chunk in _split_for_messaging(text, _MAX_MESSAGE_CHARS):
            await self.send(sender_id, chunk)

    # ── group chats (rooms, not people) ──

    def bold(self, text: str) -> str:
        """``text`` in this platform's bold, or unchanged where it has none."""
        if not text:
            return text
        left, right = self.bold_markup
        return f"{left}{text}{right}"

    def italic(self, text: str) -> str:
        """``text`` in this platform's italic, or unchanged where it has none."""
        if not text:
            return text
        left, right = self.italic_markup
        return f"{left}{text}{right}"

    def groups_enabled(self) -> bool:
        """Whether this channel takes part in platform group chats.

        Three things have to be true: the transport can see rooms at all, the
        operator turned the feature on for this channel, and the channel is not
        in notification mode — a notification channel pushes automation output
        outward and holds no conversations, so there is nothing for it to say in
        a group.
        """
        if not type(self).supports_group_chats:
            return False
        if self._is_notification_mode():
            return False
        return bool((self.channel.get("config") or {}).get("group_chats_enabled"))

    def _auto_send_files_enabled(self) -> bool:
        """Whether reply forwarders auto-deliver the files a run created.

        On by default — an operator turns it off per channel with
        ``config.auto_send_files = false`` (the send tools still work; only
        the automatic delivery stops). Only an explicit ``False`` disables.
        """
        value = (self.channel.get("config") or {}).get("auto_send_files")
        return True if value is None else bool(value)

    def self_identity(self) -> dict:
        """The platform account this channel speaks as, as last recorded.

        ``{}`` before any adapter has connected — every caller treats an unknown
        identity as "not us", which is the safe direction for the echo filter
        (a message wrongly attributed to somebody else is answered; one wrongly
        attributed to us is silently dropped).
        """
        identity = (self.channel.get("state") or {}).get("self_identity")
        return dict(identity) if isinstance(identity, dict) else {}

    async def fetch_joined_groups(self) -> list[dict] | None:
        """Every group this account is already in, or ``None`` if unknowable.

        The counterpart to waiting for a join event. An account is usually in
        groups long before Cremind is told to care about them, and nobody was
        "added" to those — so there is no event coming, no notification to
        raise, and without this no way to reach them but to wait for somebody to
        happen to post. This is what lets the operator simply pick them.

        ``None`` means the platform cannot enumerate them: a Telegram *bot* and
        a Zalo bot have no such API. Adapters that can set
        ``supports_group_listing`` and return
        ``{"platform_chat_id", "title", "chat_type", "member_count"}`` per group.
        """
        return None

    async def fetch_group_roster(self, chat_id: str) -> list[dict] | None:
        """The platform's member list for a room, or ``None`` if it has none.

        ``None`` is not a failure: a Telegram *bot* cannot enumerate a group and
        a Zalo bot cannot see one at all, so their rosters are only ever who has
        posted. Adapters that CAN answer set ``supports_group_roster`` and return
        ``{"member_id", "alt_ids", "display_name", "username", "is_bot", "role"}``
        per member.
        """
        return None

    async def send_to_chat(self, chat_id: str, text: str) -> None:
        """Send a message to a platform CHAT id — a room, not a person.

        Distinct from :meth:`_send_text` because a room's id is nobody's sender
        id: on Telegram a group chat id is negative and belongs to no user, so
        addressing a room through the 1:1 path would deliver the message into
        whichever private chat happens to share that number. Adapters that can
        talk to a room override this; the rest say so.
        """
        raise ChannelNotImplemented(
            f"{self.channel_type} channels cannot send to a chat id",
        )

    async def send_to_chat_chunked(self, chat_id: str, text: str) -> None:
        """Send ``text`` to a room as one or more messages, each within the cap.

        The room-addressed twin of :meth:`_send_chunked`, and swallowing failures
        for the same reason: a mirrored group message is one bubble among
        several, and abandoning the rest because one failed leaves the room with
        half a conversation.
        """
        text = (text or "").strip()
        if not text:
            return
        for chunk in _split_for_messaging(text, _MAX_MESSAGE_CHARS):
            try:
                await self.send_to_chat(chat_id, chunk)
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"channels[{self.channel_type}]: send to chat {chat_id} failed",
                )

    async def send_file_to_chat(
        self, chat_id: str, path: str, *,
        name: str | None = None, mime: str | None = None,
        caption: str | None = None,
    ) -> None:
        """Send one file to a room, swallowing errors (mirror of
        :meth:`send_to_chat_chunked`); degrades to the fallback notice where
        the transport has no file support."""
        display = name or os.path.basename(path)
        try:
            await self._send_file_to_chat(
                chat_id, path, name=name, mime=mime, caption=caption,
            )
        except ChannelNotImplemented:
            await self.send_to_chat_chunked(
                chat_id, file_fallback_text(display, _file_size(path)),
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                f"channels[{self.channel_type}]: file send to chat {chat_id} failed",
            )

    async def _handle_group_inbound(
        self,
        *,
        chat_id: str,
        chat_title: str | None,
        chat_type: str | None,
        sender_id: str,
        sender_username: str | None,
        sender_alt_ids: list[str] | None = None,
        display_name: str | None,
        text: str,
        platform_message_id: str | None,
        sender_is_bot: bool,
        platform_message_date: float | None = None,
        mentioned: bool = False,
        files: Sequence[IncomingFile] | None = None,
    ) -> None:
        """Route a message from a platform group into the channel-group pipeline.

        A SECOND inbound entry point, independent of the 1:1 DM pipeline in
        :meth:`_handle_inbound`. A group message is addressed to a room, not to
        this sender: there is no per-sender conversation to own it, and the
        channel's subscribe gate does not apply — the group's own approval and
        member policy decide who the agent listens to instead.

        ``platform_message_date`` is the platform's own send time, which every
        account that receives the message reports identically. It is what tells
        two copies of one message apart from the same person saying the same
        short thing twice ("status?") — see
        :func:`app.channels.groups.keys.platform_key`.

        ``sender_alt_ids`` carries the other ids this one account is seen under
        where a platform has more than one (WhatsApp's ``@s.whatsapp.net`` and
        ``@lid`` forms). The echo filter and the member policy both try every
        one of them, so which form the transport happened to report does not
        decide whether we recognise the account.

        ``mentioned`` is the adapter's own answer to "did this message address
        us?", computed per platform because no two spell a mention alike. True
        means the agent replies immediately; false sends the message to the
        relevance judge instead.

        Never raises — the caller is a receive loop, and one unroutable message
        must not be able to end it.

        ``files`` are unfetched attachment descriptors; the group pipeline
        stages them only for an approved group and an allowed member, and
        discards them unfetched on every earlier drop.
        """
        body = (text or "").strip()
        if not body and not files:
            return
        from app.channels.groups.inbound import GroupInbound, handle_group_message

        await handle_group_message(
            self,
            GroupInbound(
                chat_id=str(chat_id),
                chat_title=chat_title,
                chat_type=chat_type,
                sender_id=str(sender_id),
                sender_username=sender_username,
                sender_alt_ids=list(sender_alt_ids or []),
                display_name=display_name,
                text=body,
                platform_message_id=platform_message_id,
                sender_is_bot=bool(sender_is_bot),
                platform_message_date=platform_message_date,
                mentioned=bool(mentioned),
                files=list(files or []),
            ),
        )

    # ── inbound flow ──

    def _inbound_lock(self, sender_id: str) -> asyncio.Lock:
        """Return (lazily creating) the per-sender inbound lock.

        Safe without synchronization: the event loop is single-threaded and
        there is no ``await`` between the lookup and the assignment.
        """
        lock = self._inbound_locks.get(sender_id)
        if lock is None:
            lock = asyncio.Lock()
            self._inbound_locks[sender_id] = lock
        return lock

    def forget_sender(self, sender_id: str) -> None:
        """Drop every trace of ``sender_id`` from this adapter's live state.

        The DB side of deleting a channel client is only half the job: this
        adapter also holds per-sender state in memory, and leaving it behind
        would make the "as if they had never written" promise false in visible
        ways — a queued forwarder still chasing their deleted conversation, or
        a remembered access request meaning the operator never gets a fresh
        approval notification when they come back.

        Called by the delete-client endpoint after the rows are gone. Safe to
        call for a sender this adapter has never seen.
        """
        # A forwarder for a deleted conversation has nowhere to publish and
        # would write into rows that no longer exist; drop it, and make sure the
        # done-callback doesn't chain a replacement.
        self._pending_runs.pop(sender_id, None)
        task = self._inflight.pop(sender_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._access_requested.discard(sender_id)
        # Only reclaim the lock when nobody holds it. A task can be parked on
        # ``acquire`` with a reference to this exact object, so removing a held
        # lock would hand the next caller a second one and break the mutual
        # exclusion the entry exists for (see ``__init__``). A held lock is
        # simply left to be reused, which is harmless.
        lock = self._inbound_locks.get(sender_id)
        if lock is not None and not lock.locked():
            self._inbound_locks.pop(sender_id, None)

    async def _handle_inbound(
        self, sender_id: str, display_name: str | None, text: str,
        *, files: Sequence[IncomingFile] | None = None,
    ) -> None:
        """Route an inbound platform message into Cremind.

        Every authenticated message is dispatched. If a turn is already running
        for this sender the message is folded INTO it (see
        :mod:`app.events.user_message_delivery`), so the reply being written
        takes it into account and the person gets one answer covering everything
        they said — rather than the old "I'm thinking…" ack and a silent drop.

        Forwarder serialization is preserved by ``_expect_run``: a folded-in
        message starts no run, and a run that does start while a forwarder is
        live gets a chained one when that forwarder finishes, so no forwarder
        ever absorbs the tail of the wrong run on the shared stream bus.

        ``files`` are attachment descriptors whose bytes have NOT been fetched;
        they are staged (downloaded) only after the sender passes the auth
        gate, so a stranger's payload is never pulled onto disk. A message that
        is only a file still gets a turn — a placeholder stands in for the
        caption the sender didn't write.
        """
        text = (text or "").strip()
        if not text and not files:
            return
        if not text and files:
            text = placeholder_text([f.name for f in files])

        logger.debug(
            f"[channels:{self.channel_type}] inbound channel_id={self.channel_id} "
            f"from={sender_id} msg_len={len(text)} files={len(files or ())}"
        )

        # Notification-mode channels don't converse: inbound messages are
        # subscribe/unsubscribe control commands, not agent prompts — a file
        # has nowhere to go there.
        if self._is_notification_mode():
            await discard_incoming_files(files)
            await self._handle_notification_command(sender_id, display_name, text)
            return

        sender = await self._upsert_sender(sender_id, display_name)

        conversation_id = await self._ensure_conversation(sender, display_name)

        # Access authentication (shared with notification mode via
        # ``config.subscribe_auth``): gate who may talk to the agent. An
        # unauthenticated sender's message is never dispatched — the gate
        # either advances them toward authentication (otp/passcode) or holds
        # them (approval/allowlist) until an operator approves. Their files
        # are discarded unfetched.
        auth = self._subscribe_auth()
        if auth != "open" and not sender["authenticated"]:
            await discard_incoming_files(files)
            await self._handle_access_gate(sender, auth, text)
            return

        attachments = (
            await stage_incoming_files(self, conversation_id, files)
            if files else None
        )

        # The per-sender lock keeps the park attempt and the forwarder
        # bookkeeping inside _dispatch_to_agent atomic, so two messages racing in
        # from the same poll batch can't both decide they are starting the run.
        async with self._inbound_lock(sender_id):
            await self._dispatch_to_agent(
                conversation_id, sender_id, display_name, text,
                attachments=attachments or None,
            )

    async def _upsert_sender(
        self, sender_id: str, display_name: str | None,
    ) -> dict:
        """Upsert the sender row for an inbound message.

        Wraps ``get_or_create_sender`` so every transport also contributes any
        phone number its sender id encodes (see :meth:`_derive_phone`), which
        is what lets the direct-send path later address this contact by number.
        The ``phone`` kwarg is only passed when there is one to contribute, so
        transports that encode no number call through exactly as before.
        """
        phone = self._derive_phone(sender_id)
        extra = {"phone": phone} if phone else {}
        return await self.storage.get_or_create_sender(
            self.channel_id, sender_id, display_name=display_name, **extra,
        )

    async def _ensure_conversation(self, sender: dict, display_name: str | None) -> str:
        """Return the conversation id for this sender, creating one if absent."""
        return await self.storage.ensure_sender_conversation(
            sender, profile=self.profile, channel_id=self.channel_id,
            display_name=display_name,
        )

    # ── auth gates ──

    async def _handle_otp(self, sender: dict, text: str) -> None:
        now = time.time()
        pending = sender.get("pending_otp")
        expires_at = sender.get("pending_otp_expires_at") or 0

        if pending and expires_at > now and text == pending:
            await self.storage.update_sender(
                sender["id"],
                authenticated=True,
                pending_otp=None,
                pending_otp_expires_at=None,
            )
            await self.send(sender["sender_id"], "Authenticated. You can chat now.")
            return

        # Either no pending code, expired, or wrong code → issue (or reissue)
        # an OTP and prompt the user.
        code = f"{secrets.randbelow(1_000_000):06d}"
        await self.storage.update_sender(
            sender["id"],
            pending_otp=code,
            pending_otp_expires_at=now + _OTP_TTL_SECONDS,
        )
        try:
            get_event_notifications().push(
                profile=self.profile,
                conversation_id=sender.get("conversation_id") or "",
                conversation_title=sender.get("display_name") or sender["sender_id"],
                message_preview=f"OTP {code} for {self.channel_type}",
                kind="channel_otp",
                priority="high",
                extra={
                    "channel_id": self.channel_id,
                    "channel_type": self.channel_type,
                    "sender_id": sender["sender_id"],
                    "sender_name": sender.get("display_name") or "",
                    "otp": code,
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception("channels: failed to push OTP notification")
        await self.send(sender["sender_id"], "please provide OTP code")

    async def _handle_access_gate(
        self, sender: dict, auth: str, text: str,
    ) -> None:
        """Advance/hold an unauthenticated conversational sender per ``auth``.

        Called from :meth:`_handle_inbound` when the channel's access method is
        not ``open`` and the sender isn't authenticated yet. The caller returns
        immediately after, so the sender's message is never dispatched to the
        agent until they're authenticated.
        """
        if auth == "otp":
            await self._handle_otp(sender, text)
        elif auth == "passcode":
            await self._handle_passcode(sender, text)
        elif auth in ("approval", "allowlist"):
            # approval raises an operator request + a "pending" ack; allowlist
            # is a silent gate (unknown senders are simply refused, but the row
            # exists so an operator can approve them from the Subscribers list).
            await self._handle_access_request(sender, notify=(auth == "approval"))
        else:  # pragma: no cover — _subscribe_auth only returns known methods
            await self.send(sender["sender_id"], "This channel is not accepting messages.")

    async def _handle_passcode(self, sender: dict, text: str) -> None:
        # ``subscribe_passcode`` is the unified key; ``password`` is the legacy
        # conversational key kept for back-compat.
        config = self.channel.get("config") or {}
        passcode = config.get("subscribe_passcode") or config.get("password")
        if passcode and text.strip() == str(passcode):
            await self.storage.update_sender(sender["id"], authenticated=True)
            await self.send(sender["sender_id"], "Authenticated. You can chat now.")
            return
        await self.send(
            sender["sender_id"],
            "🔒 This channel is passcode-protected. Send the passcode to start chatting.",
        )

    async def _handle_access_request(self, sender: dict, *, notify: bool) -> None:
        """Hold a sender pending operator approval (approval / allowlist)."""
        if not notify:
            # allowlist — flat refusal, no operator notification.
            await self.send(
                sender["sender_id"],
                "🔒 You're not authorized to use this channel. "
                "An admin must grant you access.",
            )
            return

        # approval — notify the operator once, then ack the sender.
        sid = sender["sender_id"]
        if sid not in self._access_requested:
            self._access_requested.add(sid)
            try:
                get_event_notifications().push(
                    profile=self.profile,
                    conversation_id="",
                    conversation_title=f"Access request: {self.channel_type}",
                    message_preview=(
                        f"{sender.get('display_name') or sid} wants to chat on the "
                        f"{self.channel_type} channel. Approve it under Settings → Channels."
                    ),
                    kind="channel_subscribe_request",
                    priority="high",
                    extra={
                        "channel_id": self.channel_id,
                        "channel_type": self.channel_type,
                        "sender_id": sid,
                        "sender_name": sender.get("display_name") or "",
                    },
                )
            except Exception:  # noqa: BLE001
                logger.exception("channels: failed to push access request notification")
        await self.send(
            sid,
            "⏳ Your request to chat is pending admin approval. "
            "You'll be able to chat once approved.",
        )

    # ── dispatch + reply forwarding ──

    def _spawn_forwarder(
        self,
        target: ReplyTarget | str,
        conversation_id: str,
        *,
        name_hint: str = "fwd",
    ) -> asyncio.Task:
        """Start the forwarder + typing-loop pair for one run, as one task.

        Registered in ``_inflight`` under ``target.key`` so at most one is live
        per destination. Its done-callback chains a replacement while runs are
        still owed one, since a forwarder terminates at the first terminal event
        it absorbs.
        """
        target = coerce_target(target)
        key = target.key

        async def _supervised_forward() -> None:
            typing = asyncio.create_task(
                self._typing_loop_for(target),
                name=f"channel-typing:{self.channel_type}:{key}",
            )
            try:
                await self._forward_reply(conversation_id, target)
            finally:
                typing.cancel()
                try:
                    await typing
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

        forwarder = asyncio.create_task(
            _supervised_forward(),
            name=f"channel-{name_hint}:{self.channel_type}:{conversation_id}",
        )
        self._inflight[key] = forwarder

        def _clear_inflight(task: asyncio.Task) -> None:
            # Only clear if we're still the registered task — a later
            # message may have replaced us before we observed our completion.
            # (Also how shutdown avoids chaining: stop() empties _inflight
            # before cancelling, so this returns early.)
            if self._inflight.get(key) is not task:
                return
            self._inflight.pop(key, None)
            remaining = self._pending_runs.get(key, 0) - 1
            if remaining > 0:
                self._pending_runs[key] = remaining
                # Another run is still owed a reply. Safe to subscribe now: the
                # bus clears its replay ring at end_run, so a fresh subscriber
                # replays the NEXT run from its start rather than re-reading the
                # one this forwarder just finished.
                self._spawn_forwarder(target, conversation_id, name_hint="fwd-next")
            else:
                self._pending_runs.pop(key, None)

        forwarder.add_done_callback(_clear_inflight)
        return forwarder

    def _expect_run(
        self, target: ReplyTarget | str, conversation_id: str,
    ) -> None:
        """Record that one more run owes this target a reply, and cover it.

        Spawns a forwarder when none is live; otherwise the live forwarder's
        done-callback chains one, which is what keeps a forwarder from absorbing
        the tail of a run that is not its own.
        """
        target = coerce_target(target)
        self._pending_runs[target.key] = self._pending_runs.get(target.key, 0) + 1
        existing = self._inflight.get(target.key)
        if existing is None or existing.done():
            self._spawn_forwarder(target, conversation_id)

    def _release_expected_run(self, target: ReplyTarget | str) -> None:
        """Undo an ``_expect_run`` whose run never started (enqueue failed)."""
        target = coerce_target(target)
        remaining = self._pending_runs.get(target.key, 0) - 1
        if remaining > 0:
            self._pending_runs[target.key] = remaining
        else:
            self._pending_runs.pop(target.key, None)

    # Public because :mod:`app.channels.groups.dispatch` drives them and is not
    # a subclass. Named for what they do rather than for the bookkeeping under
    # them.

    def expect_run_for(self, target: ReplyTarget, conversation_id: str) -> None:
        """Cover one run that is about to start for ``target``."""
        self._expect_run(target, conversation_id)

    def release_run_for(self, target: ReplyTarget) -> None:
        """Undo :meth:`expect_run_for`, and drop the forwarder it spawned.

        Unlike the 1:1 failure path this also cancels: a room gets no "(internal
        error)" message, so a forwarder left subscribed would sit there until
        some unrelated later run terminated and then post that run's answer.
        """
        self._release_expected_run(target)
        if not self._pending_runs.get(target.key):
            forwarder = self._inflight.get(target.key)
            if forwarder is not None and not forwarder.done():
                forwarder.cancel()

    def ensure_group_forwarder(
        self, target: ReplyTarget, conversation_id: str,
    ) -> None:
        """Make sure a live run's answer reaches this room.

        For a message folded into a turn already running: no new run, so no new
        forwarder is owed — but the running turn may have been started somewhere
        that pointed no forwarder at this group (a schedule, a task result), and
        then nothing would carry its answer out.
        """
        existing = self._inflight.get(target.key)
        if existing is None or existing.done():
            self._expect_run(target, conversation_id)

    def forget_group(self, group_id: str, platform_chat_id: str = "") -> None:
        """Drop a group's in-memory state and stop carrying its replies.

        Called when a group is blocked or forgotten. A forwarder still waiting on
        a run would otherwise post that run's answer into a room the operator has
        just said no to.
        """
        key = group_key(group_id)
        self._pending_runs.pop(key, None)
        task = self._inflight.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
        self.groups.forget_group(group_id, platform_chat_id)

    async def _dispatch_to_agent(
        self, conversation_id: str, sender_id: str,
        display_name: str | None, text: str,
        attachments: list[dict] | None = None,
    ) -> None:
        from app.agent.stream_runner import make_run_id
        from app.events import user_message_delivery

        target = sender_target(sender_id)

        user_message_metadata = {
            "source": "channel",
            "channel_id": self.channel_id,
            "channel_type": self.channel_type,
            "sender_id": sender_id,
            "display_name": display_name,
        }

        # Mid-turn: fold the message into the running turn rather than starting a
        # second one. No new run, so no new forwarder — the live one is already
        # accumulating this turn's text and will send the reply that accounts for
        # what was just said.
        parked = await user_message_delivery.try_park_user_message(
            conversation_id=conversation_id,
            profile=self.profile,
            query=text,
            user_message_metadata=user_message_metadata,
            attachments=attachments,
        )
        if parked is not None and parked.injected:
            # The run may have been started elsewhere (a skill event, a task
            # result) with no forwarder bound to this sender; cover it so the
            # answer still reaches the platform.
            existing = self._inflight.get(target.key)
            if existing is None or existing.done():
                self._expect_run(target, conversation_id)
            return

        run_id = make_run_id(conversation_id, kind="channel")

        history_messages: list[Any] = []
        try:
            db_msgs = await self.storage.get_messages(conversation_id)
            if db_msgs:
                history_messages = convert_db_messages_to_history(
                    db_msgs, include_reasoning=replay_reasoning_enabled(self.profile),
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                f"channels: failed to load history for {conversation_id}",
            )

        self._expect_run(target, conversation_id)

        # A message that was parked and then lost the race to the turn's end is
        # already persisted; run it without persisting it twice.
        existing_user_message_id = (
            parked.message_id if parked is not None else None
        )
        try:
            await event_queue.enqueue_user_message(
                conversation_id=conversation_id,
                run_id=run_id,
                profile=self.profile,
                query=text,
                history_messages=history_messages,
                reasoning=True,
                user_message_metadata=user_message_metadata,
                attachments=attachments,
                push_user_message=existing_user_message_id is None,
                existing_user_message_id=existing_user_message_id,
                update_title_from_query=False,
            )
        except Exception:  # noqa: BLE001
            self._release_expected_run(target)
            forwarder = self._inflight.get(target.key)
            if forwarder is not None and not forwarder.done():
                forwarder.cancel()
            logger.exception("channels: failed to enqueue inbound message")
            await self.send(sender_id, "(internal error: failed to dispatch message)")

    async def forward_external_run(self, conversation_id: str) -> None:
        """Spawn a one-shot reply forwarder for an externally-triggered run.

        Used by the skill-event runner: when a skill event fires for a
        conversation that's bound to an external channel, we still want the
        agent's response to reach the platform (WhatsApp/Telegram/etc.), not
        just the web UI. This mirrors the per-message forwarder pattern in
        :meth:`_dispatch_to_agent` but skips the user-message enqueue (the
        skill-event runner handles that).

        Also covers a platform GROUP conversation, which has no sender row at
        all: without this, a follow-up turn (the mid-turn flush, a task result,
        a schedule) would finish with nobody carrying its answer back to the
        room. Only an approved group is carried — a run finishing in a group the
        operator has since blocked must not post into it.

        No-op when nothing on this channel is bound to ``conversation_id``.
        """
        senders = await self.storage.list_senders(self.channel_id)
        sender = next(
            (s for s in senders if s.get("conversation_id") == conversation_id),
            None,
        )
        if sender is None:
            target = await self._group_target_for_conversation(conversation_id)
            if target is not None:
                logger.info(
                    f"channels[{self.channel_type}]: forward_external_run "
                    f"conv={conversation_id} group={target.group_id} — expecting run"
                )
                self._expect_run(target, conversation_id)
                return
            logger.warning(
                f"channels[{self.channel_type}]: forward_external_run "
                f"no sender bound to conv={conversation_id} "
                f"channel_id={self.channel_id}"
            )
            return
        sender_id = sender["sender_id"]

        # Queue behind any live forwarder rather than skipping. A forwarder ends
        # at the first terminal event it absorbs, so "one is already in flight"
        # does NOT mean this run's events will be covered — it means they will be
        # covered by the forwarder chained after it.
        logger.info(
            f"channels[{self.channel_type}]: forward_external_run "
            f"conv={conversation_id} sender={sender_id} — expecting run"
        )
        self._expect_run(sender_target(sender_id), conversation_id)

    async def release_external_run(self, conversation_id: str) -> None:
        """Undo :meth:`forward_external_run` when the run never started.

        The mirror image, resolving the same target the same way (a bound
        sender, else an approved group). Without it a caller that armed a
        forwarder and then failed to enqueue leaves the expectation standing,
        and the chained forwarder absorbs whatever run happens next on that
        conversation — posting to the platform an answer the user asked for in
        the web UI. No-op when nothing is bound.
        """
        try:
            senders = await self.storage.list_senders(self.channel_id)
            sender = next(
                (s for s in senders if s.get("conversation_id") == conversation_id),
                None,
            )
            target = (
                sender_target(sender["sender_id"]) if sender is not None
                else await self._group_target_for_conversation(conversation_id)
            )
            if target is None:
                return
            logger.info(
                f"channels[{self.channel_type}]: release_external_run "
                f"conv={conversation_id} to={target.key}"
            )
            self.release_run_for(target)
        except Exception:  # noqa: BLE001
            logger.exception(
                f"channels[{self.channel_type}]: could not release the expected "
                f"run for conv={conversation_id}"
            )

    async def _group_target_for_conversation(
        self, conversation_id: str,
    ) -> ReplyTarget | None:
        """The room this conversation belongs to, if it is an approved one."""
        if not type(self).supports_group_chats:
            return None
        try:
            from app.channels.groups.constants import STATUS_APPROVED
            from app.storage import get_channel_group_storage

            group = await get_channel_group_storage().get_group_by_conversation(
                conversation_id
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "channels: could not look up a channel group by conversation",
                exc_info=True,
            )
            return None
        if group is None or group.get("channel_id") != self.channel_id:
            return None
        if group.get("status") != STATUS_APPROVED:
            return None
        return group_target(group)

    async def _forward_reply(
        self, conversation_id: str, target: ReplyTarget | str,
    ) -> None:
        """Forward an in-progress agent run from the stream bus to the platform.

        ``response_mode == "detail"`` sends the run's trigger header (for an
        event-driven run) and each ReAct step as its own markdown-formatted
        bubble (Thought → Action → Observation), then the final answer as one
        or more bubbles, so the user sees progress while the run is still
        executing instead of waiting for one giant message at the end.
        ``"normal"`` sends ONLY the final answer (still chunked when long) —
        no trigger header, no steps: everything else is Cremind's internals,
        and a platform user who asked for just the answer reads them as noise.

        A GROUP target reads the same setting — an operator who asked this
        channel for steps asked for them everywhere it answers — but two things
        still differ. An error becomes a log line rather than a message, because
        an apology posted into a group is read by everyone and explains nothing
        to them. And a ``[silent]`` answer sends nothing at all — that is the
        agent deciding the message was not for it, which in a real group is most
        messages.

        Silence and steps interact, so in a room the LAST step is held until the
        answer is known: a turn that reads a message and decides it was not for
        it would otherwise post its reasoning about staying out of the
        conversation. Only the pending step can be caught this way — a turn that
        runs several steps and goes silent at the end has already posted the
        earlier ones. Buffering every step to the end would fix that and defeat
        the point of streaming progress, so it isn't done.

        Each bubble is sent through :meth:`send` which already isolates
        per-message exceptions, so a transient failure on one bubble can't
        prevent later bubbles from going out.
        """
        target = coerce_target(target)
        bus = get_event_stream_bus()
        queue, replay, _is_active = await bus.subscribe(conversation_id)
        logger.info(
            f"channels[{self.channel_type}]: _forward_reply START "
            f"conv={conversation_id} to={target.key} "
            f"replay={len(replay)} active={_is_active}"
        )
        detail = self.response_mode == "detail"
        text_chunks: list[str] = []
        # Action_Input of the most recent terminal-action ("Final Answer" /
        # "Done") thinking step. Used by ``flush_final`` as a fallback when
        # ``text_chunks`` is empty — which happens when the LLM produces a
        # Final Answer Tool call that yields a degenerate ``DONE`` chunk with
        # empty ``data`` (so stream_runner publishes no ``text`` event).
        final_answer_fallback: str = ""
        current_step: dict | None = None
        step_index = 0
        seen_seqs: set[int] = set()
        terminated = False
        # What this run did with its answer: "sent" | "silent" | "empty". Local
        # to the log lines below — the durable record of it is stamped on the
        # agent's message by the stream runner, not from here (see the note in
        # the ``finally``).
        outcome = "empty"
        # Whether any part of this run has already reached the platform. A turn
        # that answered an interruption has spoken before its final answer, so
        # "nothing to send at the end" is then a finished turn rather than an
        # empty one — and the Final-Answer fallback must not repeat what went
        # out already.
        sent_any = False
        # Files the run created, held for delivery AFTER the final answer —
        # identical in detail and normal modes (files are output, not steps).
        # Deduped by uri and capped; ``absorb``'s "file" branch is the only
        # collector and ``deliver_pending_files`` the only sender, so a file
        # can never go out twice even though it also appears in ``result``
        # events and persisted parts.
        pending_files: list[dict] = []
        seen_file_uris: set[str] = set()
        auto_files = self._auto_send_files_enabled()

        async def flush_step(step: dict) -> None:
            """Send one reasoning step as its own bubble.

            Deliberately does NOT call ``note_agent_post`` for a room. That
            counter is the flood brake, checked against
            ``max_agent_posts_per_minute`` (20) when the NEXT message arrives —
            so a fifteen-step turn would silence the room for a minute and
            notify the operator, i.e. using "answer with steps" would switch the
            agent off. The brake is there to stop runaway conversation, and a
            step is not a conversational turn: what the room hears the agent
            *say* is the interim reply and the answer, and those two do count.
            """
            nonlocal step_index
            if not detail:
                return
            step_index += 1
            body = _format_step_markdown(step_index, step)
            if body:
                logger.debug(
                    f"channels[{self.channel_type}]: flush_step #{step_index} "
                    f"len={len(body)} to={target.key}"
                )
                await self._send_reply(target, body)

        async def flush_interim() -> None:
            """Send what the agent has said so far and start a fresh message.

            A flow break is where a reply to an interruption ends. Buffering it
            with the rest of the turn would deliver it once the work it
            interrupted was over — which is precisely the wait the interruption
            was trying to skip — and glued to the final answer, where it reads as
            a contradiction ("Not yet, still installing." … "Done.").

            The buffer is cleared either way: a segment the agent chose to keep
            silent is spent, not carried into the next message.
            """
            nonlocal outcome, sent_any
            text = "".join(text_chunks)
            text_chunks.clear()
            text = text.strip()
            if target.is_group:
                text = _strip_silent_lines(text)
            if not text:
                return
            logger.info(
                f"channels[{self.channel_type}]: flush_interim sending "
                f"len={len(text)} to={target.key} conv={conversation_id}"
            )
            await self._send_reply(target, text)
            outcome = "sent"
            sent_any = True
            if target.is_group and target.group_id:
                self.groups.note_agent_post(target.group_id)

        def effective_final(text: str) -> str:
            """What ``flush_final`` would send: fallback applied, then stripped.

            Its own function because the terminal-step guard below has to ask
            the same question *before* the answer is sent. Two copies of this
            could disagree, and the way they'd disagree is a room being told the
            agent's reasoning about a message it then stayed silent on.
            """
            text = text.strip()
            if not text and final_answer_fallback and not sent_any:
                # Only when nothing has gone out yet. After an interim flush the
                # fallback holds text that was already sent, and using it here
                # would say the same thing twice.
                text = final_answer_fallback
            if target.is_group:
                text = _strip_silent_lines(text)
            return text

        async def flush_final(raw_text: str) -> None:
            nonlocal outcome, sent_any
            text = effective_final(raw_text)
            if not raw_text.strip() and final_answer_fallback and not sent_any:
                logger.info(
                    f"channels[{self.channel_type}]: flush_final using "
                    f"Final-Answer fallback (text_chunks empty) "
                    f"len={len(final_answer_fallback)} to={target.key} "
                    f"conv={conversation_id}"
                )
            if target.is_group and not text:
                # The agent said this one was not for it. Normal, and the common
                # case in a busy room. Unless it already answered an
                # interruption, in which case the turn did speak and only its
                # tail was silent.
                if not sent_any:
                    outcome = "silent"
                logger.info(
                    f"channels[{self.channel_type}]: staying silent in "
                    f"group={target.group_id} conv={conversation_id}"
                )
                return
            if not text:
                if sent_any:
                    # Everything this turn had to say went out at a flow break.
                    return
                outcome = "empty"
                logger.error(
                    f"[channels:{self.channel_type}] flush_final empty "
                    f"conv={conversation_id} to={target.key} — "
                    f"no response is sent"
                )
                return
            prefix = "*Response*\n\n" if detail and step_index > 0 else ""
            logger.info(
                f"channels[{self.channel_type}]: flush_final sending "
                f"len={len(text)} to={target.key} conv={conversation_id}"
            )
            await self._send_reply(target, prefix + text)
            outcome = "sent"
            sent_any = True
            if target.is_group and target.group_id:
                # Counted only on a message that actually went out, so a turn
                # that stayed silent does not spend the room's rate budget.
                self.groups.note_agent_post(target.group_id)

        async def deliver_pending_files() -> None:
            """Send the run's created files, after the final answer.

            A silent group turn sends nothing — files included: the agent
            decided the message was not for it, and a file landing in the room
            anyway would be the loudest possible way to be wrong about that.
            A DM delivers even when the final text was empty — a turn whose
            entire output is a file is a legitimate answer.
            """
            if not pending_files:
                return
            if target.is_group and not sent_any:
                logger.info(
                    f"channels[{self.channel_type}]: withholding "
                    f"{len(pending_files)} file(s) from silent turn in "
                    f"group={target.group_id} conv={conversation_id}"
                )
                return
            for payload in pending_files:
                info = payload.get("file") or {}
                uri = str(info.get("uri") or "")
                # Re-check existence: a write→move sequence leaves the written
                # uri dangling, and dedupe-by-uri cannot catch a rename.
                if not uri or not os.path.isfile(uri):
                    continue
                logger.info(
                    f"channels[{self.channel_type}]: delivering reply file "
                    f"'{info.get('name') or os.path.basename(uri)}' "
                    f"to={target.key} conv={conversation_id}"
                )
                await self._send_reply_file(
                    target, uri,
                    name=info.get("name"), mime=info.get("mimeType"),
                )

        async def absorb(event: dict) -> bool:
            nonlocal current_step, final_answer_fallback
            seq = event.get("seq")
            if seq in seen_seqs:
                return False
            seen_seqs.add(seq)
            etype = event.get("type")
            data = event.get("data") or {}
            logger.debug(
                f"channels[{self.channel_type}]: absorb seq={seq} type={etype} "
                f"conv={conversation_id}"
            )
            if etype == "event_trigger_message":
                # An automation reporting back: the "trigger" here IS the full
                # result text, and the answer that follows restates it in the
                # agent's own words — sending both posts the same outcome twice.
                # Keyed on the persisted metadata because the frame carries no
                # kind, and on ``trigger`` because the agent's answer row shares
                # ``source`` with the block.
                meta = data.get("metadata") or {}
                if meta.get("source") == "event_task_result" and meta.get("trigger"):
                    return False
                # The formatted Trigger/Action/Content block stream_runner
                # produced for this run. It explains what set the run off, which
                # belongs with the reasoning steps — so it goes out only in
                # detail mode, alongside them. On "Final answer only" the user
                # asked for the answer and nothing else; a block of Trigger:/
                # Action:/Content: scaffolding reads as a glitch, not an answer.
                if not detail:
                    return False
                content = data.get("content") or ""
                if content:
                    logger.info(
                        f"channels[{self.channel_type}]: forwarding event "
                        f"trigger len={len(content)} to={target.key}"
                    )
                    # Through ``_send_reply``, not the 1:1 path: a room's
                    # ``address`` is a platform CHAT id, which on Telegram is a
                    # negative number addressing no user at all.
                    await self._send_reply(target, content)
            elif etype == "text":
                token = data.get("token")
                if token:
                    text_chunks.append(token)
            elif etype == "thinking":
                # Capture the Action_Input of the most recent terminal-action
                # step as a fallback for ``flush_final`` — runs in both detail
                # and normal modes so the fallback is available either way.
                # The terminal-action set mirrors ``_format_step_markdown``.
                action_lower = (data.get("Action") or "").strip().lower()
                if action_lower in {"final answer", "done"}:
                    ai_str = _stringify_action_input(
                        data.get("Action_Input")
                    ).strip()
                    if ai_str:
                        final_answer_fallback = ai_str

                if detail:
                    # A new thinking event marks the start of a new step. Flush
                    # the previous step (with its observation now attached) so
                    # the user sees it before the next round of reasoning begins.
                    if current_step is not None:
                        await flush_step(current_step)
                    current_step = {
                        "thought": (data.get("Thought") or "").strip(),
                        "action": (data.get("Action") or "").strip(),
                        "action_input": data.get("Action_Input") or "",
                        "observation": "",
                    }
            elif etype == "result" and detail and current_step is not None:
                obs_parts = data.get("Observation") or []
                current_step["observation"] = _format_observation_text(obs_parts)
            elif etype == "file":
                # A tool-touched file. Only ``origin == "created"`` is held for
                # delivery: read_file publishes these events too, and
                # auto-forwarding a file the agent merely READ would hand out
                # anything it looked at. This branch is the forwarder's only
                # file collector, and ``deliver_pending_files`` its only
                # sender — files in ``result`` events and persisted parts are
                # never sent from here, so nothing goes out twice.
                if auto_files:
                    origin = str(
                        (data.get("metadata") or {}).get("origin") or "referenced"
                    )
                    uri = str((data.get("file") or {}).get("uri") or "")
                    if (
                        origin == "created"
                        and uri
                        and uri not in seen_file_uris
                        and len(pending_files) < _MAX_REPLY_FILES
                    ):
                        seen_file_uris.add(uri)
                        pending_files.append(data)
            elif etype == "flow_break":
                # The agent stopped to answer something that interrupted it. Send
                # that now as its own message — not terminal, the run continues.
                if current_step is not None:
                    await flush_step(current_step)
                    current_step = None
                await flush_interim()
            elif etype == "complete":
                final_text = "".join(text_chunks)
                if current_step is not None:
                    # In a room the last step is where the agent decides whether
                    # the message was for it at all. Posting it before knowing
                    # the answer would narrate a decision to stay out of the
                    # conversation — so hold it when this turn is about to say
                    # nothing. A DM has no silent outcome and keeps its order.
                    staying_silent = (
                        target.is_group
                        and not sent_any
                        and not effective_final(final_text)
                    )
                    if not staying_silent:
                        await flush_step(current_step)
                    current_step = None
                await flush_final(final_text)
                await deliver_pending_files()
                return True
            elif etype == "error":
                if current_step is not None:
                    await flush_step(current_step)
                    current_step = None
                # A failed run delivers no files: whatever it wrote along the
                # way is the debris of the failure, not an answer.
                pending_files.clear()
                err_msg = (data or {}).get("message") or "unknown error"
                if target.is_group:
                    # Never into a room: everyone there would read an apology
                    # that means nothing to them, and the operator needs the log
                    # line, not the group.
                    logger.error(
                        f"[channels:{self.channel_type}] run failed in "
                        f"group={target.group_id} conv={conversation_id}: {err_msg}"
                    )
                else:
                    await self._send_reply(target, f"_Error:_ {err_msg}")
                return True
            return False

        try:
            for event in replay:
                if await absorb(event):
                    terminated = True
                    break
            if not terminated:
                while True:
                    event = await queue.get()
                    if await absorb(event):
                        terminated = True
                        break
        except asyncio.CancelledError:
            logger.info(
                f"channels[{self.channel_type}]: _forward_reply CANCELLED "
                f"conv={conversation_id} to={target.key}"
            )
            return
        finally:
            logger.info(
                f"channels[{self.channel_type}]: _forward_reply END "
                f"conv={conversation_id} to={target.key} "
                f"terminated={terminated} steps={step_index} "
                f"text_chunks={len(text_chunks)}"
            )
            # NB: the turn's ``channel_group`` outcome stamp is NOT written here.
            # This forwarder runs concurrently with whatever the group says
            # next, so a stamp landing late would mean one turn replays a
            # "[silent]" row that the next one drops — a deletion in the middle
            # of the model's history, and a lost prompt cache every time. The
            # stream runner writes it inline before ``complete`` instead (step
            # 6f), from the same ``strip_silent_lines`` this posts by.
            await bus.unsubscribe(conversation_id, queue)


def _strip_silent_lines(text: str) -> str:
    """Drop the ``[silent]`` sentinel from an answer bound for a room.

    Delegates to :func:`app.groups.render.strip_silent_lines` so that what gets
    posted and what the turn's outcome stamp records can never disagree — the
    stream runner stamps from the same function. (One of the two pure helpers
    this feature shares with Cremind's own rooms; see the package docstring.)
    """
    from app.groups.render import strip_silent_lines

    return strip_silent_lines(text)


# ── Module-level formatters (no adapter state needed) ─────────────────────────


def _format_step_markdown(idx: int, step: dict) -> str:
    """Render one ReAct step as a Telegram-style markdown bubble.

    Uses the legacy ``parse_mode="Markdown"`` syntax (``*bold*``, ``_italic_``,
    `` ``code`` ``, ``` ```block``` ```). Skips the ``Action`` / ``Action_Input``
    sections for terminal "Final Answer" / "Done" pseudo-actions because the
    answer itself is already streaming through the ``text`` events.
    """
    lines: list[str] = [f"*Step {idx}*"]
    thought = step.get("thought") or ""
    action = step.get("action") or ""
    action_input = step.get("action_input")
    observation = step.get("observation") or ""

    if thought:
        lines.append(f"💭 _Thought:_ {thought}")

    is_terminal = action.strip().lower() in {"final answer", "done", ""}
    if action and not is_terminal:
        lines.append(f"→ _Action:_ `{action}`")
        ai_str = _stringify_action_input(action_input)
        if ai_str:
            lines.append(f"```\n{_truncate(ai_str, 600)}\n```")

    if observation:
        lines.append(f"◂ _Observation:_\n{_truncate(observation, 800)}")

    body = "\n".join(lines).strip()
    return body


def _format_observation_text(obs_parts: list) -> str:
    """Flatten an Observation array (text/data/file parts) into a string."""
    fragments: list[str] = []
    for part in obs_parts or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("kind")
        if kind == "text":
            text = (part.get("text") or "").strip()
            if text:
                fragments.append(text)
        elif kind == "file":
            fileinfo = part.get("file") or {}
            name = fileinfo.get("name") or "file"
            fragments.append(f"📎 {name}")
        elif kind == "data":
            data = part.get("data")
            if data is not None:
                try:
                    fragments.append(json.dumps(data, ensure_ascii=False)[:400])
                except Exception:  # noqa: BLE001
                    fragments.append(str(data)[:400])
    return "\n".join(fragments).strip()


def _stringify_action_input(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        return str(value)


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _split_for_messaging(text: str, max_chars: int) -> list[str]:
    """Split ``text`` into ≤ ``max_chars`` chunks, preferring newline boundaries.

    Tries paragraph boundaries first, then single newlines, then a hard cut.
    Each chunk is non-empty.
    """
    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break
        # Prefer a paragraph break.
        cut = remaining.rfind("\n\n", 0, max_chars)
        if cut < max_chars // 2:
            cut = remaining.rfind("\n", 0, max_chars)
        if cut < max_chars // 2:
            cut = max_chars
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    return [c for c in chunks if c]
