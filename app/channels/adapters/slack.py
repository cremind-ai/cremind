"""Slack adapter — ``slack-bolt`` Socket Mode bot.

Runs a Slack app in Socket Mode (via :pypi:`slack-bolt`, an optional extra
installed by the ``channel-slack`` feature), so no public URL is needed — the
app connects outbound over a WebSocket using an app-level token. It listens for
direct messages (``message.im`` events) and dispatches them to
:meth:`BaseChannelAdapter._handle_inbound`; replies go back via
``chat.postMessage``.

A message posted in a channel, a private channel or a group DM
(``message.channels`` / ``message.groups`` / ``message.mpim``) takes the second
inbound path instead (:meth:`BaseChannelAdapter._handle_group_inbound`), so it
lands on the room's timeline rather than in the sender's private conversation.
Those events only arrive once the app carries the matching ``*:history`` scopes
and has been invited into the channel.

Serves conversational ``bot`` mode and push-only ``notification`` mode over the
same app (notification behavior lives in
:class:`app.channels.notification_delivery.NotificationDeliveryMixin`).

Needs two tokens (declared in the catalog): a Bot User OAuth token (``xoxb-``)
and an App-Level token (``xapp-``) with ``connections:write``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.channels.base import BaseChannelAdapter
from app.channels.exceptions import ChannelAuthError, ChannelNotImplemented
from app.utils.logger import logger

# Slack conversation kinds that are a room rather than a 1:1 DM: a public
# channel, a private channel (Slack still calls those "group" in the event
# payload) and a multi-person DM.
# How many channel members are read in one go. Past this the list stops being
# useful for deciding who the agent may answer and starts being a download.
_ROSTER_LIMIT = 200

# Pages of the app's own conversation list to walk when offering groups to pick
# from. A workspace can hold thousands; past a few hundred the picker is not a
# list anybody chooses from, so this bounds the calls rather than the usefulness.
_CONVERSATION_PAGES = 5

_GROUP_CHANNEL_TYPES = ("channel", "group", "mpim")
# Subtypes a room message may carry and still be worth ingesting. ``None`` is an
# ordinary post; ``thread_broadcast`` is a threaded reply the author also sent to
# the channel; ``bot_message`` is how every mirror the other members post arrives,
# and dropping those here would hide them from the only layer that can tell our
# own room's echo from an unrelated bot. Everything else (edits, joins, deletes,
# file shares) has no row on the timeline.
_GROUP_KEEP_SUBTYPES = (None, "bot_message", "thread_broadcast")


def _ts_seconds(ts: str | None) -> float | None:
    """Slack's ``ts`` read as epoch seconds.

    Slack numbers a message by its send time ("1700000000.000300"), so the same
    string is both the message id and the timestamp
    :func:`app.channels.groups.keys.platform_key` wants. It never goes through
    :func:`app.channels.base.platform_message_timestamp`, which reads a ``.date``
    attribute off an SDK object — here there is no object, just a string.
    """
    if not ts:
        return None
    try:
        return float(ts)
    except (TypeError, ValueError):
        return None


class SlackAdapter(BaseChannelAdapter):
    # A bot invited into a channel sees the messages posted there and can post
    # back to that channel id. Slack's mrkdwn spells emphasis the same way the
    # base class defaults do (``*bold*``, ``_italic_``), so the markup is left
    # alone.
    supports_group_chats = True
    supports_group_roster = True
    supports_group_join_events = True
    reports_sender_is_bot = True
    # ``users.conversations`` names every channel the app is a member of.
    supports_group_listing = True

    def __init__(self, channel: dict, storage: Any) -> None:
        super().__init__(channel, storage)
        self._app: Any = None
        self._handler: Any = None
        # Slack user id -> IM (DM) channel id, captured from inbound events so
        # replies don't need an extra ``conversations.open`` round-trip.
        self._im_channels: dict[str, str] = {}
        # Slack user id -> display name (best-effort, for conversation titles).
        self._names: dict[str, str] = {}
        # Slack user id -> ``name`` handle, filled by the same ``users.info``
        # call as the display name (see :meth:`_resolve_user`).
        self._handles: dict[str, str] = {}
        # Slack channel id -> channel name, for a binding title a person can
        # recognise (an event only ever carries the id).
        self._channel_names: dict[str, str] = {}

    def _tokens(self) -> tuple[str, str]:
        config = self.channel.get("config") or {}
        bot_token = config.get("bot_token")
        app_token = config.get("app_token")
        if not bot_token:
            raise ChannelAuthError("Slack channel missing bot_token (xoxb-)")
        if not app_token:
            raise ChannelAuthError("Slack channel missing app_token (xapp-)")
        return bot_token, app_token

    def _build(self) -> tuple[Any, Any]:
        try:
            from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler  # type: ignore
            from slack_bolt.async_app import AsyncApp  # type: ignore
        except ImportError as exc:
            raise ChannelNotImplemented(
                "slack-bolt is not installed. Re-enabling this channel installs "
                "it automatically; to install it manually run "
                "`cremind features install channel.slack.bot`.",
            ) from exc

        bot_token, app_token = self._tokens()
        app = AsyncApp(
            token=bot_token,
            # Socket Mode never verifies inbound HTTP request signatures, but
            # bolt wants a signing secret at construction; a placeholder is
            # harmless here. Token validation happens on socket connect.
            signing_secret="unused-in-socket-mode",
            token_verification_enabled=False,
        )

        async def on_message(event: dict, *args: Any, **kwargs: Any) -> None:
            await self._dispatch_message_event(event)

        async def on_member_joined(event: dict, *args: Any, **kwargs: Any) -> None:
            await self._dispatch_member_joined(event)

        app.event("message")(on_message)
        # Slack tells an app when it is added to a channel, which is what lets a
        # group appear for approval before anybody has spoken in it. Needs the
        # ``member_joined_channel`` event subscribed on the app.
        app.event("member_joined_channel")(on_member_joined)
        handler = AsyncSocketModeHandler(app, app_token)
        return app, handler

    async def _run(self) -> None:
        if self.channel.get("mode") not in ("bot", "notification"):
            raise ChannelNotImplemented(
                f"SlackAdapter does not support mode={self.channel.get('mode')!r}",
            )
        self._app, self._handler = self._build()
        try:
            # ``connect_async`` opens the Socket Mode WebSocket; we then park on
            # an Event so ``_run`` stays alive (and cancellable) for the life of
            # the adapter.
            await self._handler.connect_async()
        except ChannelNotImplemented:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ChannelAuthError(f"Slack connect failed: {exc}") from exc

        # Who this app is in the workspace. A bound room needs it to recognise
        # (and ignore) our own mirrored posts, and the group roster shows the
        # mention so the other agents can address us. Best-effort: a failed
        # auth.test is no reason to drop a working socket.
        try:
            resp = await self._app.client.auth_test()
            user_id = str(resp.get("user_id") or "")
            if user_id:
                await self._store_self_identity(
                    user_id=user_id,
                    username=resp.get("user"),
                    is_bot=True,
                    # Slack pings an account by ``<@U…>`` and nothing else — an
                    # ``@handle`` typed into a message is plain text there.
                    mention=f"<@{user_id}>",
                    # ``auth.test`` gives only the handle; the name people
                    # actually see is on the profile.
                    display_name=await self._self_display_name(user_id),
                )
        except Exception:  # noqa: BLE001
            logger.debug(
                "[channels:slack] auth.test failed; self identity unknown",
                exc_info=True,
            )

        await asyncio.Event().wait()

    async def stop(self) -> None:  # type: ignore[override]
        handler = self._handler
        if handler is not None:
            try:
                await handler.close_async()
            except Exception:  # noqa: BLE001
                pass
        await super().stop()

    async def _handle_inbound_safe(
        self, sender_id: str, display_name: str | None, text: str,
    ) -> None:
        try:
            await self._handle_inbound(sender_id, display_name, text)
        except Exception:  # noqa: BLE001
            logger.exception("slack: inbound handler failed")

    async def _dispatch_message_event(self, event: dict) -> None:
        """Route one ``message`` event to the room's path or to the sender's DM.

        Split out of the ``on_message`` closure so the routing decision is
        testable without a socket — and without ``slack-bolt``, which is an
        optional extra.
        """
        channel_type = event.get("channel_type")
        if channel_type in _GROUP_CHANNEL_TYPES:
            if event.get("subtype") not in _GROUP_KEEP_SUBTYPES:
                return
            await self._handle_group_event(event)
            return
        # Only direct messages; skip bot echoes and edit/delete subtypes.
        if channel_type != "im":
            return
        if event.get("bot_id") or event.get("subtype"):
            return
        user_id = event.get("user")
        text = event.get("text") or ""
        if not user_id or not text:
            return
        im_channel = event.get("channel")
        if im_channel:
            self._im_channels[str(user_id)] = str(im_channel)
        display_name = await self._resolve_name(str(user_id))
        await self._handle_inbound_safe(str(user_id), display_name, text)

    async def _handle_group_event(self, event: dict) -> None:
        """Hand one channel / private-channel / group-DM message to the room.

        Bot authors are reported rather than dropped: this adapter cannot tell
        the mirrors its siblings just posted from an unrelated app's message, and
        :mod:`app.channels.groups.inbound` can.
        """
        chat_id = str(event.get("channel") or "")
        text = event.get("text") or ""
        sender_is_bot = (
            bool(event.get("bot_id")) or event.get("subtype") == "bot_message"
        )
        # A post made by an app carries no ``user`` at all, so its ``bot_id`` is
        # the only identity it has.
        sender_id = str(event.get("user") or event.get("bot_id") or "")
        if not chat_id or not text or not sender_id:
            return

        display_name: str | None = None
        handle: str | None = None
        if not sender_is_bot:
            # ``users.info`` on a bot id is a guaranteed 404, and the group layer
            # is about to drop the message anyway.
            display_name, handle = await self._resolve_user(sender_id)

        ts = str(event.get("ts") or "") or None
        await self._handle_group_inbound(
            chat_id=chat_id,
            chat_title=await self._resolve_channel_name(chat_id),
            chat_type=event.get("channel_type"),
            sender_id=sender_id,
            sender_username=handle,
            display_name=display_name,
            text=text,
            platform_message_id=ts,
            platform_message_date=_ts_seconds(ts),
            sender_is_bot=sender_is_bot,
            mentioned=self._is_mentioned(event, text),
        )

    async def _dispatch_member_joined(self, event: dict) -> None:
        """Notice this app being added to a channel.

        Fired for every member joining every channel the app can see, so the
        filter is one comparison: only the event naming US is a join of ours.
        """
        own_id = str(self.self_identity().get("user_id") or "")
        if not own_id or str(event.get("user") or "") != own_id:
            return
        chat_id = str(event.get("channel") or "")
        if not chat_id:
            return
        from app.channels.groups.inbound import handle_group_joined

        await handle_group_joined(
            self,
            chat_id=chat_id,
            chat_title=await self._resolve_channel_name(chat_id),
            chat_type=event.get("channel_type") or "channel",
        )

    def _is_mentioned(self, event: dict, text: str) -> bool:
        """Whether this channel message addresses our app.

        Slack writes a mention into the text as ``<@U…>``, so the token is the
        answer. A threaded reply to one of OUR messages counts too:
        ``parent_user_id`` names the author of the thread it hangs off, and
        answering in a thread the app started is how Slack conversations
        continue without re-tagging.
        """
        own_id = str(self.self_identity().get("user_id") or "")
        if not own_id:
            return False
        if f"<@{own_id}>" in (text or ""):
            return True
        return str(event.get("parent_user_id") or "") == own_id

    async def fetch_joined_groups(self) -> list[dict] | None:
        """Every conversation this app has been invited to.

        ``users.conversations`` answers for the app's own user, which is exactly
        the question — a workspace has channels this app is not in, and being in
        one is what makes it a group the agent could take part in. Paginated,
        with a hard page cap: a large workspace can list thousands, and past the
        first few hundred nobody is picking from a list anyway.
        """
        if self._app is None:
            return None
        out: list[dict] = []
        cursor: str | None = None
        try:
            for _page in range(_CONVERSATION_PAGES):
                response = await self._app.client.users_conversations(
                    types="public_channel,private_channel,mpim",
                    exclude_archived=True,
                    limit=_ROSTER_LIMIT,
                    **({"cursor": cursor} if cursor else {}),
                )
                for channel in (response or {}).get("channels") or []:
                    chat_id = str((channel or {}).get("id") or "")
                    if not chat_id:
                        continue
                    out.append({
                        "platform_chat_id": chat_id,
                        "title": channel.get("name") or None,
                        "chat_type": "mpim" if channel.get("is_mpim") else "channel",
                        "member_count": channel.get("num_members"),
                    })
                cursor = (
                    ((response or {}).get("response_metadata") or {}).get("next_cursor")
                    or None
                )
                if not cursor:
                    break
        except Exception:  # noqa: BLE001
            logger.warning(
                "[channels:slack] could not list the app's conversations",
                exc_info=True,
            )
            return None
        return out

    async def fetch_group_roster(self, chat_id: str) -> list[dict] | None:
        """The channel's members, resolved to names.

        Two calls deep — ids first, then one ``users.info`` each — so it is
        capped and the per-user lookups reuse the cache the DM path already
        fills. Needs ``channels:read`` / ``groups:read``; without them Slack
        answers ``missing_scope`` and the roster stays "who has posted".
        """
        if self._app is None:
            return None
        try:
            response = await self._app.client.conversations_members(
                channel=chat_id, limit=_ROSTER_LIMIT,
            )
            member_ids = list((response or {}).get("members") or [])[:_ROSTER_LIMIT]
        except Exception:  # noqa: BLE001
            logger.warning(
                f"[channels:slack] could not list the members of {chat_id}",
                exc_info=True,
            )
            return None
        out: list[dict] = []
        for user_id in member_ids:
            display_name, handle = await self._resolve_user(str(user_id))
            out.append({
                "member_id": str(user_id),
                "display_name": display_name,
                "username": handle,
                # Slack does not say on the member list, and ``users.info``
                # would double the calls to learn something the message events
                # already carry.
                "is_bot": False,
                "role": None,
            })
        return out

    async def _resolve_name(self, user_id: str) -> str | None:
        name, _handle = await self._resolve_user(user_id)
        return name

    async def _resolve_user(self, user_id: str) -> tuple[str | None, str | None]:
        """Display name and ``name`` handle for a Slack user id, both cached.

        One ``users.info`` call answers both, so the group path gets the handle
        for free: it is what the room's roster and the "unknown sender" log show
        an operator, who otherwise has nothing but a ``U…`` id to go on.
        """
        cached = (self._names.get(user_id), self._handles.get(user_id))
        if cached != (None, None):
            return cached
        if self._app is None:
            return None, None
        try:
            resp = await self._app.client.users_info(user=user_id)
            user = resp.get("user") or {}
            profile = user.get("profile") or {}
            name = profile.get("display_name") or user.get("real_name")
            handle = user.get("name")
            if name:
                self._names[user_id] = name
            if handle:
                self._handles[user_id] = handle
            return name, handle
        except Exception:  # noqa: BLE001
            return None, None

    async def _self_display_name(self, user_id: str) -> str | None:
        """The name this app shows above its own messages in a channel.

        ``auth.test`` returns only the handle, and a Slack app's handle is often
        nothing like the name people see and address it by.
        """
        name, _handle = await self._resolve_user(user_id)
        return name

    async def _resolve_channel_name(self, channel_id: str) -> str | None:
        """The channel's name, cached, for the binding title.

        A Slack message event carries the channel id and nothing else, and
        ``C09ABCDEF`` is not a room anybody recognises in the list of chats
        offered for binding.
        """
        if channel_id in self._channel_names:
            return self._channel_names[channel_id]
        if self._app is None:
            return None
        try:
            resp = await self._app.client.conversations_info(channel=channel_id)
            name = (resp.get("channel") or {}).get("name")
            if name:
                self._channel_names[channel_id] = name
            return name
        except Exception:  # noqa: BLE001
            return None

    async def _resolve_channel(self, sender_id: str) -> str | None:
        """Map a send target to a Slack channel id.

        Conversational replies reuse the IM channel cached from inbound. For
        notification targets (``target_chat_ids``) a value already shaped like a
        channel id (``C``/``D``/``G``…) is used as-is; a user id (``U``/``W``…)
        is resolved to its IM channel via ``conversations.open``.
        """
        cached = self._im_channels.get(sender_id)
        if cached:
            return cached
        if sender_id[:1] in ("U", "W"):
            if self._app is None:
                return None
            try:
                resp = await self._app.client.conversations_open(users=sender_id)
                channel_id = (resp.get("channel") or {}).get("id")
                if channel_id:
                    self._im_channels[sender_id] = channel_id
                return channel_id
            except Exception:  # noqa: BLE001
                return None
        # Already a channel id (or a group/DM id supplied directly).
        return sender_id

    async def _send_text(self, sender_id: str, text: str) -> None:
        if self._app is None:
            raise ChannelAuthError("Slack app not connected")
        channel_id = await self._resolve_channel(sender_id)
        if not channel_id:
            raise ChannelAuthError(f"Slack channel for {sender_id} not resolvable")
        await self._app.client.chat_postMessage(channel=channel_id, text=text)

    async def send_to_chat(self, chat_id: str, text: str) -> None:
        """Post a mirrored message into a room by its channel id.

        Posted straight, without :meth:`_resolve_channel`: a room id is nobody's
        sender id, and the IM cache it consults is keyed by user.
        """
        if self._app is None:
            raise ChannelAuthError("Slack app not connected")
        await self._app.client.chat_postMessage(channel=chat_id, text=text)
