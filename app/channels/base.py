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
import secrets
import time
from abc import ABC, abstractmethod
from typing import Any

from app.channels.notification_delivery import NotificationDeliveryMixin
from app.events import queue as event_queue
from app.events.notifications_buffer import get_event_notifications
from app.events.stream_bus import get_event_stream_bus
from app.config.user_config import replay_reasoning_enabled
from app.utils.common import convert_db_messages_to_history
from app.utils.logger import logger


_OTP_TTL_SECONDS = 600  # 10 minutes
# Telegram caps a single message at 4096 chars; the other platforms have
# similar (looser) caps. Keep some headroom for the markdown wrapper.
_MAX_MESSAGE_CHARS = 3500


class BaseChannelAdapter(NotificationDeliveryMixin, ABC):
    """Lifecycle + inbound handling shared across all channel adapters.

    Subclasses are constructed by :class:`ChannelRegistry` with the channel
    row and a reference to :class:`ConversationStorage`. They own a long-
    running asyncio task started by :meth:`start` and stopped by :meth:`stop`.

    Subclasses must:
    - Implement :meth:`_run` (the receive loop) and :meth:`_send_text`
      (platform-specific send call).
    - Call :meth:`_handle_inbound` for every incoming user message.

    Notification mode (``channel["mode"] == "notification"``) is layered on top
    via :class:`NotificationDeliveryMixin`: the transport's own ``_run`` still
    connects and routes inbound (so ``/start`` subscribe works), but inbound is
    handled as control commands instead of agent dispatch, and a second task
    (:meth:`_run_notification_delivery`) relays the notifications bus outward.
    """

    def __init__(self, channel: dict, storage: Any) -> None:
        self.channel = channel
        self.storage = storage
        self._task: asyncio.Task | None = None
        # Notification-mode delivery loop (mode == "notification" only).
        self._notif_task: asyncio.Task | None = None
        # Per-sender in-flight forwarder tasks. Used both to (a) ack the user
        # when a second message arrives while we're still processing the first
        # ("busy ack" — the closest a bot can come to blocking input on
        # platforms that don't expose that), and (b) serialize forwarders for
        # the same sender so a new forwarder doesn't accidentally absorb the
        # tail of the previous run's events from the shared stream bus.
        self._inflight: dict[str, asyncio.Task] = {}

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

    async def stop(self) -> None:
        # Cancel any in-flight per-sender forwarders so shutdown doesn't
        # leave dangling tasks subscribed to the stream bus.
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

    # ── inbound flow ──

    async def _handle_inbound(
        self, sender_id: str, display_name: str | None, text: str,
    ) -> None:
        """Route an inbound platform message into Cremind.

        If a previous run is still in flight for this sender, send a brief
        "still working" ack and **await** that run before dispatching the
        new one. Awaiting matters for correctness, not just UX: the
        forwarder subscribes to the conversation's stream bus immediately,
        and starting a new forwarder while the prior run is still
        publishing would cause it to consume the tail of the wrong run.
        """
        text = (text or "").strip()
        if not text:
            return

        logger.debug(
            f"[channels:{self.channel_type}] inbound channel_id={self.channel_id} "
            f"from={sender_id} msg_len={len(text)}"
        )

        # Notification-mode channels don't converse: inbound messages are
        # subscribe/unsubscribe control commands, not agent prompts.
        if self._is_notification_mode():
            await self._handle_notification_command(sender_id, display_name, text)
            return

        sender = await self.storage.get_or_create_sender(
            self.channel_id, sender_id, display_name=display_name,
        )

        conversation_id = await self._ensure_conversation(sender, display_name)

        # Access authentication (shared with notification mode via
        # ``config.subscribe_auth``): gate who may talk to the agent. An
        # unauthenticated sender's message is never dispatched — the gate
        # either advances them toward authentication (otp/passcode) or holds
        # them (approval/allowlist) until an operator approves.
        auth = self._subscribe_auth()
        if auth != "open" and not sender["authenticated"]:
            await self._handle_access_gate(sender, auth, text)
            return

        # Busy ack + serialize-per-sender. Both the ack and the await cover
        # the case where the user fires two questions back-to-back.
        inflight = self._inflight.get(sender_id)
        if inflight is not None and not inflight.done():
            await self.send(
                sender_id,
                "⏳ Still working on your previous message — I'll handle this one next.",
            )
            try:
                await inflight
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                # Whatever happened to the previous run, we're free to
                # dispatch the new one now.
                pass

        await self._dispatch_to_agent(conversation_id, sender_id, display_name, text)

    async def _ensure_conversation(self, sender: dict, display_name: str | None) -> str:
        """Return the conversation id for this sender, creating one if absent."""
        conv_id = sender.get("conversation_id")
        if conv_id:
            existing = await self.storage.get_conversation(conv_id)
            if existing:
                return conv_id
        title = display_name or sender["sender_id"]
        conv = await self.storage.create_conversation(
            profile=self.profile,
            title=title[:120] if title else "Untitled Chat",
            channel_id=self.channel_id,
        )
        await self.storage.update_sender(sender["id"], conversation_id=conv["id"])
        return conv["id"]

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

    async def _dispatch_to_agent(
        self, conversation_id: str, sender_id: str,
        display_name: str | None, text: str,
    ) -> None:
        from app.agent.stream_runner import make_run_id

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

        # The forwarder + typing-loop pair runs as a single supervised task.
        # Registering it in ``_inflight`` lets ``_handle_inbound`` ack and
        # serialize subsequent messages from the same sender.
        async def _supervised_forward() -> None:
            typing = asyncio.create_task(
                self._typing_loop(sender_id),
                name=f"channel-typing:{self.channel_type}:{sender_id}",
            )
            try:
                await self._forward_reply(conversation_id, sender_id)
            finally:
                typing.cancel()
                try:
                    await typing
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

        forwarder = asyncio.create_task(
            _supervised_forward(),
            name=f"channel-fwd:{self.channel_type}:{conversation_id}",
        )
        self._inflight[sender_id] = forwarder

        def _clear_inflight(task: asyncio.Task) -> None:
            # Only clear if we're still the registered task — a later
            # message may have replaced us before we observed our completion.
            if self._inflight.get(sender_id) is task:
                self._inflight.pop(sender_id, None)

        forwarder.add_done_callback(_clear_inflight)

        try:
            await event_queue.enqueue_user_message(
                conversation_id=conversation_id,
                run_id=run_id,
                profile=self.profile,
                query=text,
                history_messages=history_messages,
                reasoning=True,
                user_message_metadata={
                    "source": "channel",
                    "channel_id": self.channel_id,
                    "channel_type": self.channel_type,
                    "sender_id": sender_id,
                    "display_name": display_name,
                },
                update_title_from_query=False,
            )
        except Exception:  # noqa: BLE001
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

        No-op when no sender on this channel is bound to ``conversation_id``.
        """
        senders = await self.storage.list_senders(self.channel_id)
        sender = next(
            (s for s in senders if s.get("conversation_id") == conversation_id),
            None,
        )
        if sender is None:
            logger.warning(
                f"channels[{self.channel_type}]: forward_external_run "
                f"no sender bound to conv={conversation_id} "
                f"channel_id={self.channel_id}"
            )
            return
        sender_id = sender["sender_id"]

        existing = self._inflight.get(sender_id)
        if existing is not None and not existing.done():
            logger.info(
                f"channels[{self.channel_type}]: forward_external_run "
                f"forwarder already in flight for sender={sender_id} — "
                f"skipping (existing one will absorb this run's events)"
            )
            return

        logger.info(
            f"channels[{self.channel_type}]: forward_external_run "
            f"conv={conversation_id} sender={sender_id} — spawning forwarder"
        )

        async def _supervised_forward() -> None:
            typing = asyncio.create_task(
                self._typing_loop(sender_id),
                name=f"channel-typing:{self.channel_type}:{sender_id}",
            )
            try:
                await self._forward_reply(conversation_id, sender_id)
            finally:
                typing.cancel()
                try:
                    await typing
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

        forwarder = asyncio.create_task(
            _supervised_forward(),
            name=f"channel-fwd-event:{self.channel_type}:{conversation_id}",
        )
        self._inflight[sender_id] = forwarder

        def _clear_inflight(task: asyncio.Task) -> None:
            if self._inflight.get(sender_id) is task:
                self._inflight.pop(sender_id, None)

        forwarder.add_done_callback(_clear_inflight)

    async def _forward_reply(self, conversation_id: str, sender_id: str) -> None:
        """Forward an in-progress agent run from the stream bus to the platform.

        ``response_mode == "detail"`` sends each ReAct step as its own
        markdown-formatted bubble (Thought → Action → Observation), then the
        final answer as one or more bubbles, so the user sees progress while
        the run is still executing instead of waiting for one giant message
        at the end. ``"normal"`` only sends the final answer (still chunked
        when long).

        Each bubble is sent through :meth:`send` which already isolates
        per-message exceptions, so a transient failure on one bubble can't
        prevent later bubbles from going out.
        """
        bus = get_event_stream_bus()
        queue, replay, _is_active = await bus.subscribe(conversation_id)
        logger.info(
            f"channels[{self.channel_type}]: _forward_reply START "
            f"conv={conversation_id} sender={sender_id} "
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

        async def flush_step(step: dict) -> None:
            nonlocal step_index
            if not detail:
                return
            step_index += 1
            body = _format_step_markdown(step_index, step)
            if body:
                logger.debug(
                    f"channels[{self.channel_type}]: flush_step #{step_index} "
                    f"len={len(body)} sender={sender_id}"
                )
                await self._send_chunked(sender_id, body)

        async def flush_final(text: str) -> None:
            text = text.strip()
            if not text and final_answer_fallback:
                logger.info(
                    f"channels[{self.channel_type}]: flush_final using "
                    f"Final-Answer fallback (text_chunks empty) "
                    f"len={len(final_answer_fallback)} sender={sender_id} "
                    f"conv={conversation_id}"
                )
                text = final_answer_fallback
            if not text:
                logger.error(
                    f"[channels:{self.channel_type}] flush_final empty "
                    f"conv={conversation_id} sender={sender_id} — "
                    f"user receives no response"
                )
                return
            prefix = "*Response*\n\n" if detail and step_index > 0 else ""
            logger.info(
                f"channels[{self.channel_type}]: flush_final sending "
                f"len={len(text)} sender={sender_id} conv={conversation_id}"
            )
            await self._send_chunked(sender_id, prefix + text)

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
                # Skill-event trigger header: the formatted Trigger/Action/
                # Content block already produced by stream_runner. Send it as
                # its own bubble so the platform user sees what triggered the
                # run (mirrors the agent bubble in the web UI).
                content = data.get("content") or ""
                if content:
                    logger.info(
                        f"channels[{self.channel_type}]: forwarding event "
                        f"trigger len={len(content)} sender={sender_id}"
                    )
                    await self._send_chunked(sender_id, content)
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
            elif etype == "complete":
                if current_step is not None:
                    await flush_step(current_step)
                    current_step = None
                await flush_final("".join(text_chunks))
                return True
            elif etype == "error":
                if current_step is not None:
                    await flush_step(current_step)
                    current_step = None
                err_msg = (data or {}).get("message") or "unknown error"
                await self._send_chunked(sender_id, f"_Error:_ {err_msg}")
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
                f"conv={conversation_id} sender={sender_id}"
            )
            return
        finally:
            logger.info(
                f"channels[{self.channel_type}]: _forward_reply END "
                f"conv={conversation_id} sender={sender_id} "
                f"terminated={terminated} steps={step_index} "
                f"text_chunks={len(text_chunks)}"
            )
            await bus.unsubscribe(conversation_id, queue)


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
