"""Discord adapter — ``discord.py`` bot, in DMs and in server channels.

Runs a Discord bot (via :pypi:`discord.py`, an optional extra installed by the
``channel-discord`` feature) on the gateway. A direct message goes to
:meth:`BaseChannelAdapter._handle_inbound` and is answered as a DM; a message in
a server channel goes to :meth:`BaseChannelAdapter._handle_group_inbound`, which
lands it in the Cremind room that channel is bound to — or, when it is bound to
none, only leaves the channel remembered as one an operator could bind.

Serves conversational ``bot`` mode and push-only ``notification`` mode over the
same client (notification behavior lives in
:class:`app.channels.notification_delivery.NotificationDeliveryMixin`).

Requires MESSAGE CONTENT INTENT enabled on the bot (a privileged intent); if it
is off, ``discord.py`` raises on connect and the channel is disabled with the
error surfaced in ``state.last_error``.
"""

from __future__ import annotations

import os
from typing import Any

from app.channels.attachments import IncomingFile, dest_for
from app.channels.base import BaseChannelAdapter, _split_for_messaging
from app.channels.exceptions import ChannelAuthError, ChannelNotImplemented
from app.utils.logger import logger

_DISCORD_MSG_LIMIT = 2000
# Discord's default per-file upload cap for bots (non-boosted). The API
# rejects bigger uploads anyway; the pre-check turns that into a clear error.
_DISCORD_UPLOAD_LIMIT = 10 * 1024 * 1024


def _is_mentioned(client: Any, message: Any) -> bool:
    """Whether this guild message addresses our bot.

    Three shapes, because Discord has three: the resolved ``mentions`` list, the
    raw ``<@id>`` / ``<@!id>`` token (which survives because the dispatcher keeps
    raw content), and a reply to one of our own messages, which pings by default.

    Never raises — an unexpected message shape reads as "not mentioned" and the
    relevance judge takes it from there.
    """
    try:
        me = getattr(client, "user", None)
        own_id = str(getattr(me, "id", "") or "")
        if not own_id:
            return False
        for mentioned in getattr(message, "mentions", None) or ():
            if str(getattr(mentioned, "id", "") or "") == own_id:
                return True
        content = getattr(message, "content", "") or ""
        if f"<@{own_id}>" in content or f"<@!{own_id}>" in content:
            return True
        reference = getattr(message, "reference", None)
        resolved = getattr(reference, "resolved", None)
        author = getattr(resolved, "author", None)
        if author is not None and str(getattr(author, "id", "") or "") == own_id:
            return True
    except Exception:  # noqa: BLE001
        logger.debug(
            "[channels:discord] could not read the mentions", exc_info=True,
        )
    return False


def _member_role(channel: Any, member: Any) -> str | None:
    """"admin" when this member administers the guild, else ``None``.

    ``None`` rather than "member" because the permission lookup is best-effort:
    a shape that will not answer should say nothing rather than assert the
    member is ordinary.
    """
    try:
        permissions = channel.permissions_for(member)
        return "admin" if getattr(permissions, "administrator", False) else "member"
    except Exception:  # noqa: BLE001
        return None


def _guild_chat_title(message: Any) -> str | None:
    """``Server#channel`` for a guild message, or ``None`` when unnamed.

    Both halves, because neither identifies the room on its own: every server
    the bot sits in has a ``#general``, and a server name says nothing about
    which of its channels was bound.
    """
    guild = getattr(getattr(message, "guild", None), "name", None)
    channel = getattr(getattr(message, "channel", None), "name", None)
    title = "#".join(
        str(part).strip() for part in (guild, channel) if str(part or "").strip()
    )
    return title or None


def _sent_timestamp(message: Any) -> float | None:
    """A Discord message's own send time, in epoch seconds, or ``None``.

    The base :func:`app.channels.base.platform_message_timestamp` reads
    ``message.date``; Discord spells it ``created_at``. Defensive for the same
    reason: the send time only sharpens the dedupe key that tells N copies of
    one message apart from one person repeating themselves, and a missing or odd
    value must weaken that key rather than raise inside the gateway loop.
    """
    created = getattr(message, "created_at", None)
    if created is None:
        return None
    try:
        return float(created.timestamp())
    except (AttributeError, TypeError, ValueError, OSError):
        return None


class DiscordAdapter(BaseChannelAdapter):
    # A bot invited to a server sees every message in the channels it can read,
    # and can post to a channel id it was never DMed from.
    supports_group_chats = True
    # ``channel.members`` answers, but only as fully as the Server Members
    # privileged intent allows — see :meth:`fetch_group_roster`.
    supports_group_roster = True
    # ``on_guild_join`` is guild-level, and a guild is many channels, so there
    # is no per-channel "you were added" to report. A channel becomes known by
    # its first message, or by the operator picking it after the bot joins the
    # server — which ``on_guild_join`` prompts them to do.
    supports_group_join_events = False
    reports_sender_is_bot = True
    # Every text channel of every server the bot is in.
    supports_group_listing = True
    supports_file_send = True

    # Discord reads a single asterisk as ITALIC, so the base ``*bold*`` default
    # would quietly emphasise every mirrored name and step header the wrong way.
    bold_markup = ("**", "**")

    def __init__(self, channel: dict, storage: Any) -> None:
        super().__init__(channel, storage)
        self._client: Any = None
        # sender_id (str user id) -> discord.User, so replies don't re-fetch.
        self._users: dict[str, Any] = {}
        # chat_id (str channel id) -> discord channel, for the ones the gateway
        # cache never answers for. Only HTTP-fetched channels land here; a
        # cached one is read from the gateway on every call, so this cannot go
        # stale ahead of it.
        self._channels: dict[str, Any] = {}

    def _bot_token(self) -> str:
        token = (self.channel.get("config") or {}).get("bot_token")
        if not token:
            raise ChannelAuthError("Discord channel missing bot_token")
        return token

    def _build_client(self) -> Any:
        try:
            import discord  # type: ignore
        except ImportError as exc:
            raise ChannelNotImplemented(
                "discord.py is not installed. Re-enabling this channel installs "
                "it automatically; to install it manually run "
                "`cremind features install channel.discord.bot`.",
            ) from exc

        intents = discord.Intents.default()
        intents.message_content = True  # privileged — must be enabled in the portal
        client = discord.Client(intents=intents)

        async def on_message(message: Any) -> None:
            await self._dispatch_message(client, message)

        async def on_ready() -> None:
            # Who this bot is on Discord. A bound room needs it to recognise (and
            # ignore) our own mirrored posts, and the roster in the group prompt
            # tells the other agents to address us by the mention below.
            # Best-effort: a gateway that is otherwise healthy must still serve.
            me = getattr(client, "user", None)
            if me is None:
                return
            try:
                await self._store_self_identity(
                    user_id=str(me.id),
                    username=getattr(me, "name", None),
                    is_bot=True,
                    # Discord pings by id: "@dogbot" typed into a message body is
                    # ordinary text, "<@123>" is what actually notifies.
                    mention=f"<@{me.id}>",
                    display_name=(
                        getattr(me, "display_name", None) or getattr(me, "name", None)
                    ),
                )
            except Exception:  # noqa: BLE001
                logger.debug("discord: could not record self identity", exc_info=True)

        async def on_guild_join(guild: Any) -> None:
            # Joining a server is not joining a channel — the bot lands in many
            # at once, and pending-rowing all of them would be a wall of
            # decisions. One notification pointing at the picker instead.
            try:
                if not self.groups_enabled():
                    return
                from app.channels.groups.inbound import notify_server_joined

                await notify_server_joined(
                    self, server_name=getattr(guild, "name", None) or "a server",
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "discord: could not announce the new server", exc_info=True,
                )

        client.event(on_message)
        client.event(on_ready)
        client.event(on_guild_join)
        return client

    async def _dispatch_message(self, client: Any, message: Any) -> None:
        """Route one gateway message to the room path or to the DM path.

        Split out of the ``on_message`` closure so the routing decision is
        testable without a gateway, and ordered deliberately: our own message,
        then the guild branch, then other bots, then the DM.

        The guild branch has to come BEFORE the bot filter. Unlike Telegram,
        Discord does deliver one bot's messages to another, so another Cremind
        agent's post arrives here — and it has to reach
        :func:`app.channels.groups.inbound.handle_group_message` flagged
        ``sender_is_bot=True``, which is both how a group of assistants stays
        discoverable and what the consecutive-bot-messages brake counts.
        Filtering those out this early leaves a channel whose traffic is all
        bots looking, to Cremind, like a channel nobody has ever written in.
        """
        author = getattr(message, "author", None)
        if author is None:
            return
        own_id = str(getattr(getattr(client, "user", None), "id", "") or "")
        if own_id and str(getattr(author, "id", "") or "") == own_id:
            return

        files = self._extract_files(message)

        if getattr(message, "guild", None) is not None:
            channel_id = getattr(getattr(message, "channel", None), "id", None)
            if channel_id is None:
                return
            message_id = getattr(message, "id", None)
            await self._handle_group_inbound_safe(
                chat_id=str(channel_id),
                chat_title=_guild_chat_title(message),
                # Read only by :data:`app.channels.groups.keys._PER_ACCOUNT_MESSAGE_IDS`,
                # and a Discord snowflake is global — every member bot in the
                # channel reports the same message id — so this pair must stay
                # OUT of that set, or one message would post once per bot.
                chat_type="guild_text",
                sender_id=str(author.id),
                sender_username=getattr(author, "name", None),
                display_name=getattr(author, "display_name", None),
                # RAW content, not ``clean_content``: the "<@id>" tokens are how
                # an agent knows it was the one addressed, and stripping them to
                # readable names would leave the room unable to tell.
                text=getattr(message, "content", None) or "",
                platform_message_id=(
                    str(message_id) if message_id is not None else None
                ),
                platform_message_date=_sent_timestamp(message),
                sender_is_bot=bool(getattr(author, "bot", False)),
                mentioned=_is_mentioned(client, message),
                files=files or None,
            )
            return

        if getattr(author, "bot", False):
            return

        text = getattr(message, "content", None) or ""
        if not text and not files:
            return
        sender_id = str(author.id)
        self._users[sender_id] = author
        display_name = getattr(author, "display_name", None) or getattr(
            author, "name", None,
        )
        await self._handle_inbound_safe(
            sender_id, display_name, text, files=files or None,
        )

    def _extract_files(self, message: Any) -> list[IncomingFile]:
        """Attachment descriptors from ``message.attachments`` — unfetched.

        Each ``fetch`` calls the attachment's own ``save`` (a CDN download)
        only after the base adapter clears the sender.
        """
        found: list[IncomingFile] = []
        for attachment in getattr(message, "attachments", None) or ():
            name = getattr(attachment, "filename", None) or (
                f"discord_file_{getattr(attachment, 'id', 'unknown')}"
            )
            size = getattr(attachment, "size", None)
            mime = getattr(attachment, "content_type", None)

            def _make_fetch(attachment: Any = attachment, name: str = name):
                async def fetch(dest_dir: str) -> str:
                    dest = dest_for(dest_dir, name)
                    await attachment.save(dest)
                    return dest

                return fetch

            found.append(IncomingFile(
                name=name, mime=mime,
                size=int(size) if isinstance(size, (int, float)) else None,
                fetch=_make_fetch(),
            ))
        return found

    async def _run(self) -> None:
        if self.channel.get("mode") not in ("bot", "notification"):
            raise ChannelNotImplemented(
                f"DiscordAdapter does not support mode={self.channel.get('mode')!r}",
            )
        self._client = self._build_client()
        token = self._bot_token()
        try:
            await self._client.start(token)
        except ChannelNotImplemented:
            raise
        except Exception as exc:  # noqa: BLE001
            # discord.LoginFailure (bad token) / PrivilegedIntentsRequired
            # (intent not enabled) both land here as a clean auth error.
            raise ChannelAuthError(f"Discord connect failed: {exc}") from exc

    async def stop(self) -> None:  # type: ignore[override]
        client = self._client
        if client is not None and not client.is_closed():
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
        await super().stop()

    async def _handle_inbound_safe(
        self, sender_id: str, display_name: str | None, text: str,
        files: Any = None,
    ) -> None:
        try:
            await self._handle_inbound(sender_id, display_name, text, files=files)
        except Exception:  # noqa: BLE001
            logger.exception("discord: inbound handler failed")

    async def _handle_group_inbound_safe(self, **kwargs: Any) -> None:
        try:
            await self._handle_group_inbound(**kwargs)
        except Exception:  # noqa: BLE001
            logger.exception("discord: group inbound handler failed")

    async def _get_user(self, sender_id: str) -> Any:
        user = self._users.get(sender_id)
        if user is None and self._client is not None:
            try:
                user = await self._client.fetch_user(int(sender_id))
            except Exception:  # noqa: BLE001
                return None
            self._users[sender_id] = user
        return user

    async def _get_channel(self, chat_id: str) -> Any:
        """Resolve a channel id to something with ``.send()``, or ``None``.

        ``get_channel`` is a lookup in the gateway's own cache and answers
        ``None`` for a channel this session has not seen traffic in yet — the
        normal state right after a restart, and exactly when the mirror wants to
        post — so a miss falls back to the HTTP fetch.

        A fetched channel is remembered the way :meth:`_get_user` remembers a
        fetched user, because this is no longer called once per message: the
        typing indicator asks every four seconds for the whole length of a run,
        and a channel the gateway will never cache (one whose guild is
        unavailable, say) would otherwise mean an HTTP round trip per tick.
        """
        client = self._client
        if client is None:
            return None
        try:
            channel = client.get_channel(int(chat_id))
        except (TypeError, ValueError, AttributeError):
            return None
        if channel is not None:
            return channel
        cached = self._channels.get(chat_id)
        if cached is not None:
            return cached
        try:
            channel = await client.fetch_channel(int(chat_id))
        except Exception:  # noqa: BLE001
            logger.debug(f"discord: channel {chat_id} not resolvable", exc_info=True)
            return None
        self._channels[chat_id] = channel
        return channel

    async def _send_text(self, sender_id: str, text: str) -> None:
        user = await self._get_user(sender_id)
        if user is None:
            raise ChannelAuthError(f"Discord user {sender_id} not resolvable")
        for chunk in _split_for_messaging(text, _DISCORD_MSG_LIMIT):
            await user.send(chunk)

    async def _send_file(
        self, sender_id: str, path: str, *,
        name: str | None = None, mime: str | None = None,
        caption: str | None = None,
    ) -> None:
        user = await self._get_user(sender_id)
        if user is None:
            raise ChannelAuthError(f"Discord user {sender_id} not resolvable")
        await self._send_file_to(user, path, name, caption)

    async def _send_file_to_chat(
        self, chat_id: str, path: str, *,
        name: str | None = None, mime: str | None = None,
        caption: str | None = None,
    ) -> None:
        channel = await self._get_channel(chat_id)
        if channel is None:
            raise ChannelAuthError(f"Discord channel {chat_id} not resolvable")
        await self._send_file_to(channel, path, name, caption)

    async def _send_file_to(
        self, destination: Any, path: str, name: str | None, caption: str | None,
    ) -> None:
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            raise ValueError(f"cannot read file to send: {path}") from exc
        if size > _DISCORD_UPLOAD_LIMIT:
            raise ValueError(
                f"'{name or os.path.basename(path)}' is {size} bytes; Discord "
                f"caps bot uploads at {_DISCORD_UPLOAD_LIMIT} bytes",
            )
        import discord  # type: ignore

        await destination.send(
            content=(caption or "")[:_DISCORD_MSG_LIMIT] or None,
            file=discord.File(path, filename=name or os.path.basename(path)),
        )

    async def fetch_joined_groups(self) -> list[dict] | None:
        """Every text channel this bot can read, across every server it is in.

        Titled "Server / #channel" because a channel name alone is ambiguous —
        half the servers a bot joins have a ``#general`` — and the operator is
        picking from one flat list.
        """
        client = self._client
        if client is None:
            return None
        out: list[dict] = []
        try:
            for guild in getattr(client, "guilds", None) or ():
                me = getattr(guild, "me", None)
                for channel in getattr(guild, "text_channels", None) or ():
                    try:
                        if me is not None and not channel.permissions_for(
                            me,
                        ).read_messages:
                            continue
                    except Exception:  # noqa: BLE001
                        # A permission model we cannot read is not a reason to
                        # hide the channel; approving it is still the operator's
                        # decision, and a channel the bot cannot read simply
                        # never produces a message.
                        pass
                    channel_id = str(getattr(channel, "id", "") or "")
                    if not channel_id:
                        continue
                    out.append({
                        "platform_chat_id": channel_id,
                        "title": (
                            f"{getattr(guild, 'name', '?')} / "
                            f"#{getattr(channel, 'name', channel_id)}"
                        ),
                        "chat_type": "text_channel",
                        "member_count": getattr(guild, "member_count", None),
                    })
        except Exception:  # noqa: BLE001
            logger.warning(
                "[channels:discord] could not list the bot's channels",
                exc_info=True,
            )
            return None
        return out

    async def fetch_group_roster(self, chat_id: str) -> list[dict] | None:
        """Everyone Discord will name in this channel.

        Complete only with the **Server Members** privileged intent enabled for
        the application; without it ``channel.members`` is whatever the gateway
        happened to cache, which is usually the people who have spoken. That is
        not an error and is not worth failing over — the API reports the count
        so the UI can say the list may be partial.
        """
        client = self._client
        if client is None:
            return None
        try:
            channel = client.get_channel(int(chat_id))
            if channel is None:
                channel = await client.fetch_channel(int(chat_id))
        except Exception:  # noqa: BLE001
            logger.warning(
                f"[channels:discord] could not resolve channel {chat_id}",
                exc_info=True,
            )
            return None
        members = getattr(channel, "members", None)
        if not members:
            return None
        out: list[dict] = []
        for member in members:
            member_id = str(getattr(member, "id", "") or "")
            if not member_id:
                continue
            out.append({
                "member_id": member_id,
                "display_name": getattr(member, "display_name", None),
                "username": getattr(member, "name", None),
                "is_bot": bool(getattr(member, "bot", False)),
                "role": _member_role(channel, member),
            })
        return out

    async def _send_typing_to_chat(self, chat_id: str) -> None:
        """Show the typing indicator in a guild channel.

        Resolved through :meth:`_get_channel` rather than ``client.get_channel``:
        the gateway cache answers ``None`` for a channel this session has not
        seen traffic in yet — the state right after a restart, and exactly when
        a run is composing its first mirrored reply — so a cache-only lookup is
        how the answer arrives with no "typing…" ahead of it.
        """
        channel = await self._get_channel(chat_id)
        if channel is None:
            return
        try:
            # Awaiting ``typing()`` sends ONE packet; the ``async with`` form
            # would start a second re-tick loop alongside ``_typing_loop_for``.
            # Discord's indicator lasts ~10s. (Note the contrast with Telethon,
            # whose same-looking helper sends nothing until its task runs.)
            await channel.typing()
        except Exception:  # noqa: BLE001
            # Also where a bound forum/category channel lands: not Messageable.
            logger.debug(
                "[channels:discord] group typing indicator failed", exc_info=True,
            )

    async def send_to_chat(self, chat_id: str, text: str) -> None:
        """Post into a server channel by its id — a room, not a person.

        Re-splits at Discord's own 2000-character cap even though the caller
        (:meth:`BaseChannelAdapter.send_to_chat_chunked`) has already split at
        3500: Discord rejects an over-long message outright rather than
        truncating it, so a mirrored bubble between the two limits would not
        arrive shortened, it would not arrive at all.
        """
        channel = await self._get_channel(chat_id)
        if channel is None:
            raise ChannelAuthError(f"Discord channel {chat_id} not resolvable")
        for chunk in _split_for_messaging(text, _DISCORD_MSG_LIMIT):
            await channel.send(chunk)

    async def _send_typing(self, sender_id: str) -> None:
        user = await self._get_user(sender_id)
        if user is None:
            return
        try:
            dm = user.dm_channel or await user.create_dm()
            # Entering the typing context sends a single typing packet; we exit
            # immediately since the base ``_typing_loop_for`` re-ticks every few
            # seconds (Discord's indicator lasts ~10s).
            async with dm.typing():
                pass
        except Exception:  # noqa: BLE001
            logger.debug("discord: typing indicator dropped", exc_info=True)
