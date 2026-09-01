"""Telegram userbot adapter (Telethon-based).

In contrast to the bot adapter (which uses long-polling against an
``@BotFather`` token), this adapter logs in to Telegram as **your own user
account** using Telethon. Inbound DMs from any contact are routed to
:meth:`BaseChannelAdapter._handle_inbound`; the agent's reply is sent back
through the same Telethon client. This is what Telegram colloquially
calls a "userbot" — explicitly permitted by Telegram (it's the use case
Telethon and Pyrogram are designed for).

Messages in a group room take the second inbound path
(:meth:`BaseChannelAdapter._handle_group_inbound`) and only for rooms bound to a
Cremind group. Unlike a bot, this account really does see everything in every
room it belongs to, the member bots' mirrors included — those are dropped as
bot-authored so an answer never comes back in as a new question.

Setup:
    1. Visit https://my.telegram.org/auth and create an app to obtain
       ``api_id`` and ``api_hash``.
    2. Pass ``api_id``, ``api_hash``, and your phone (international format)
       in the channel's ``config``.
    3. On first start the adapter publishes a ``code_required`` event over
       the channel's auth-events stream; Telegram sends a verification code
       through the Telegram app itself (or SMS if no other Telegram session
       is connected). The user submits the code via
       ``POST /api/channels/{id}/auth-input`` (the web UI does this from
       the pairing dialog).
    4. If the account has 2FA enabled, a ``password_required`` event
       follows. Same submit path.

Session storage: Telethon's SQLite session at
``<working_dir>/<profile>/telegram/<channel_id>/session.session``. After a
successful pairing the session survives server restarts; deleting the
file forces a fresh code-entry flow on next boot.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.channels.attachments import IncomingFile, dest_for
from app.channels.base import BaseChannelAdapter, platform_message_timestamp
from app.channels.exceptions import ChannelAuthError, ChannelNotImplemented
from app.config.settings import BaseConfig
from app.utils.logger import logger


# How many participants of one group are read. Past this the list stops being
# useful for deciding who the agent may answer and starts being a download.
_ROSTER_LIMIT = 500


def _full_name(user: Any) -> str | None:
    """"First Last" as the room sees it, or ``None``.

    Telegram splits a person's name in two and nobody in a group reads it that
    way, so the two halves are joined before anything downstream sees them.
    """
    parts = [
        str(getattr(user, "first_name", "") or "").strip(),
        str(getattr(user, "last_name", "") or "").strip(),
    ]
    return " ".join(p for p in parts if p) or None


def _participant_role(user: Any) -> str | None:
    """"admin" for a creator or administrator, else ``None`` (unknown/ordinary).

    Telethon attaches the participant record to the user when the query asked
    for one; when it did not, the absence says nothing, so this says nothing.
    """
    participant = getattr(user, "participant", None)
    if participant is None:
        return None
    name = type(participant).__name__
    if "Admin" in name or "Creator" in name:
        return "admin"
    return "member"


class TelegramUserbotAdapter(BaseChannelAdapter):
    # A real account is in the room like any other member and receives
    # everything posted there.
    supports_group_chats = True
    # A real account can enumerate a group, unlike a bot.
    supports_group_roster = True
    supports_group_join_events = True
    reports_sender_is_bot = True
    # A real account's dialog list names every group it is in.
    supports_group_listing = True
    # MTProto has none of the Bot API's 20/50 MB file caps.
    supports_file_send = True

    def __init__(self, channel: dict, storage: Any) -> None:
        super().__init__(channel, storage)
        self._client: Any = None
        # Future the auth flow awaits while waiting for the user to type the
        # code / password into the web UI. ``submit_auth_input`` resolves it.
        self._auth_input_future: asyncio.Future | None = None
        # Telegram entity cache for the current session — keyed by sender_id
        # (the user_id as a string). Lets ``_send_text`` / ``_send_typing``
        # send to a peer without re-resolving on every call.
        self._peer_cache: dict[str, Any] = {}

    # ── config accessors ──

    def _api_id(self) -> int:
        raw = (self.channel.get("config") or {}).get("api_id")
        if not raw:
            raise ChannelAuthError("Telegram userbot channel missing api_id")
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ChannelAuthError(f"api_id must be numeric, got {raw!r}") from exc

    def _api_hash(self) -> str:
        raw = (self.channel.get("config") or {}).get("api_hash")
        if not raw:
            raise ChannelAuthError("Telegram userbot channel missing api_hash")
        return str(raw)

    def _phone(self) -> str:
        raw = (self.channel.get("config") or {}).get("phone")
        if not raw:
            raise ChannelAuthError("Telegram userbot channel missing phone")
        return str(raw).strip()

    def _session_path(self) -> Path:
        base = Path(BaseConfig.CREMIND_SYSTEM_DIR)
        d = base / self.profile / "telegram" / self.channel_id
        d.mkdir(parents=True, exist_ok=True)
        return d / "session"

    # ── auth-input bridge ──

    def submit_auth_input(self, payload: dict) -> bool:  # type: ignore[override]
        fut = self._auth_input_future
        if fut is None or fut.done():
            return False
        fut.set_result(payload)
        return True

    async def _wait_for_auth_input(self) -> dict:
        loop = asyncio.get_running_loop()
        self._auth_input_future = loop.create_future()
        try:
            return await self._auth_input_future
        finally:
            self._auth_input_future = None

    # ── lifecycle ──

    async def _run(self) -> None:
        if self.channel.get("mode") != "userbot":
            raise ChannelNotImplemented(
                "TelegramUserbotAdapter handles only mode='userbot'",
            )

        try:
            from telethon import TelegramClient, events  # type: ignore
        except ImportError as exc:
            raise ChannelNotImplemented(
                "telethon is not installed. Re-enabling this channel installs "
                "it automatically; to install it manually run "
                "`cremind features install channel.telegram.userbot`.",
            ) from exc

        try:
            api_id = self._api_id()
            api_hash = self._api_hash()
        except ChannelAuthError:
            raise

        self._client = TelegramClient(
            str(self._session_path()), api_id, api_hash,
            # Telethon's default device-info strings; we keep them so
            # Telegram's "Active Sessions" list shows a recognisable entry.
            system_version="Cremind",
            app_version="0.1",
            device_model="Cremind",
        )

        try:
            await self._client.connect()
            await self._authenticate_if_needed()
        except (ChannelAuthError, ChannelNotImplemented):
            await self._safe_disconnect()
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                f"telegram-userbot[{self.channel_id}]: auth flow failed",
            )
            self._publish_auth_event({"kind": "error", "error": str(exc)})
            await self._safe_disconnect()
            raise ChannelAuthError(f"Telegram userbot auth failed: {exc}") from exc

        await self._mark_linked()
        # Which account we are. A bound group uses it to recognise our own
        # posts, and the group roster shows the ``@username``. Best-effort — the
        # session is already authorised, so a failed getMe is not fatal.
        try:
            me = await self._client.get_me()
            username = getattr(me, "username", None)
            await self._store_self_identity(
                user_id=str(me.id),
                username=username,
                is_bot=False,
                # Telegram pings an account by its ``@username``; an account
                # that never set one cannot be addressed in a room.
                mention=f"@{username}" if username else None,
                display_name=_full_name(me),
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                f"telegram-userbot[{self.channel_id}]: get_me failed; "
                "self identity unknown",
                exc_info=True,
            )
        self._publish_auth_event({"kind": "ready"})
        logger.info(f"telegram-userbot[{self.channel_id}]: authorised")

        @self._client.on(events.NewMessage(incoming=True))
        async def _on_new_message(event):  # noqa: ANN001
            try:
                await self._dispatch_event(event)
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"telegram-userbot[{self.channel_id}]: dispatch failed",
                )

        @self._client.on(events.ChatAction)
        async def _on_chat_action(event):  # noqa: ANN001
            try:
                await self._dispatch_chat_action(event)
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"telegram-userbot[{self.channel_id}]: chat action failed",
                )

        # Telethon raises distinct error classes when the server invalidates
        # our auth key. Generic ``Exception`` / ``RPCError`` are network
        # blips; only the named classes below are remote-unlink signals.
        from telethon.errors import (  # type: ignore
            AuthKeyDuplicatedError,
            AuthKeyUnregisteredError,
            SessionRevokedError,
            UserDeactivatedBanError,
            UserDeactivatedError,
        )

        try:
            await self._client.run_until_disconnected()
        except asyncio.CancelledError:
            raise
        except (AuthKeyUnregisteredError, SessionRevokedError) as exc:
            await self._mark_unlinked(
                reason="logged_out_remote",
                detail=f"Telegram session revoked from Active Sessions: {exc}",
            )
        except AuthKeyDuplicatedError as exc:
            await self._mark_unlinked(
                reason="logged_out_remote",
                detail=f"Telegram session displaced by another login: {exc}",
            )
        except (UserDeactivatedError, UserDeactivatedBanError) as exc:
            await self._mark_unlinked(
                reason="auth_revoked",
                detail=f"Telegram account deactivated/banned: {exc}",
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                f"telegram-userbot[{self.channel_id}]: client loop ended unexpectedly",
            )
        finally:
            await self._safe_disconnect()

    async def stop(self) -> None:  # type: ignore[override]
        await self._safe_disconnect()
        await super().stop()

    async def _safe_disconnect(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    # ── interactive auth ──

    async def _authenticate_if_needed(self) -> None:
        from telethon.errors import (  # type: ignore
            FloodWaitError, PasswordHashInvalidError, PhoneCodeExpiredError,
            PhoneCodeInvalidError, PhoneNumberInvalidError,
            SessionPasswordNeededError,
        )

        if await self._client.is_user_authorized():
            return

        phone = self._phone()
        try:
            sent = await self._client.send_code_request(phone)
        except PhoneNumberInvalidError as exc:
            raise ChannelAuthError("Telegram rejected the phone number") from exc
        except FloodWaitError as exc:
            raise ChannelAuthError(
                f"Telegram is rate-limiting code requests; try again in {exc.seconds}s",
            ) from exc

        # Code loop — the user can mistype, so retry on PhoneCodeInvalidError.
        password_required = False
        while True:
            self._publish_auth_event({"kind": "code_required", "phone": phone})
            payload = await self._wait_for_auth_input()
            code = (payload or {}).get("code")
            if not code:
                self._publish_auth_event({
                    "kind": "code_required", "phone": phone,
                    "error": "Provide the verification code Telegram just sent.",
                })
                continue
            try:
                await self._client.sign_in(
                    phone=phone, code=str(code).strip(),
                    phone_code_hash=sent.phone_code_hash,
                )
                return
            except PhoneCodeInvalidError:
                self._publish_auth_event({
                    "kind": "code_required", "phone": phone,
                    "error": "Invalid code — try again.",
                })
                continue
            except PhoneCodeExpiredError:
                sent = await self._client.send_code_request(phone)
                self._publish_auth_event({
                    "kind": "code_required", "phone": phone,
                    "error": "Code expired; a new one was sent.",
                })
                continue
            except SessionPasswordNeededError:
                password_required = True
                break

        if not password_required:
            return

        # 2FA password loop.
        while True:
            self._publish_auth_event({"kind": "password_required"})
            payload = await self._wait_for_auth_input()
            password = (payload or {}).get("password")
            if not password:
                self._publish_auth_event({
                    "kind": "password_required",
                    "error": "Provide your two-step verification password.",
                })
                continue
            try:
                await self._client.sign_in(password=str(password))
                return
            except PasswordHashInvalidError:
                self._publish_auth_event({
                    "kind": "password_required",
                    "error": "Invalid password — try again.",
                })
                continue

    # ── inbound dispatch ──

    async def _dispatch_event(self, event: Any) -> None:
        """Route one incoming message: a DM to the agent, a bound room to its group."""
        if not event.is_private:
            await self._dispatch_group_event(event)
            return
        if not event.message or event.message.out:
            return
        text = event.message.message or ""
        files = self._extract_files(event.message)
        if not text and not files:
            return

        sender_id = str(event.sender_id)
        try:
            sender = await event.get_sender()
        except Exception:  # noqa: BLE001
            sender = None
        display_name = self._format_display_name(sender) or sender_id

        # Cache the input peer so reply / typing don't have to re-resolve.
        try:
            self._peer_cache[sender_id] = await event.get_input_chat()
        except Exception:  # noqa: BLE001
            pass

        await self._handle_inbound(
            sender_id, display_name, text, files=files or None,
        )

    async def _dispatch_group_event(self, event: Any) -> None:
        """Route a group/supergroup message into the channel-group pipeline.

        Every group this account is in is handed over; the pipeline decides what
        to do with it, and an unapproved group gets no further than a pending row
        the operator is asked about once.

        Broadcast channels are skipped outright — nobody is talking in them.
        """
        if not getattr(event, "is_group", False):
            return
        # Telethon reports a supergroup as both a group and a channel; a legacy
        # group is a group only. The distinction matters downstream: legacy
        # groups number messages per account, so two members report different
        # ids for one message and the dedupe key has to be the content instead.
        chat_type = "supergroup" if getattr(event, "is_channel", False) else "group"

        chat_id = str(getattr(event, "chat_id", "") or "")
        if not chat_id:
            return
        message = getattr(event, "message", None)
        if message is None or getattr(message, "out", False):
            return
        text = getattr(message, "message", "") or ""
        files = self._extract_files(message)
        if not text and not files:
            return

        sender_id = str(event.sender_id)
        try:
            sender = await event.get_sender()
        except Exception:  # noqa: BLE001
            sender = None

        # Cache the input chat under the ROOM's id, not the sender's: a reply to
        # a group is addressed to the group.
        try:
            self._peer_cache[chat_id] = await event.get_input_chat()
        except Exception:  # noqa: BLE001
            pass

        message_id = getattr(message, "id", None)
        await self._handle_group_inbound(
            chat_id=chat_id,
            chat_title=await self._chat_title(event),
            chat_type=chat_type,
            sender_id=sender_id,
            sender_username=getattr(sender, "username", None),
            display_name=self._format_display_name(sender) or None,
            text=text,
            platform_message_id=(
                str(message_id) if message_id is not None else None
            ),
            # Telegram's own send time. In a legacy group each account numbers
            # messages from its own sequence, so the id cannot identify a message
            # across adapters and this is what does.
            platform_message_date=platform_message_timestamp(message),
            # Unlike a bot, a real account DOES receive other bots' messages —
            # including other Cremind agents' — so the flag has to be carried
            # honestly from here. It is what the consecutive-bot-messages brake
            # counts.
            sender_is_bot=bool(getattr(sender, "bot", False)),
            mentioned=await self._is_mentioned(event, message),
            files=files or None,
        )

    def _extract_files(self, message: Any) -> list[IncomingFile]:
        """Attachment descriptors for one Telethon message — nothing fetched.

        Telethon's ``message.file`` wraps every media kind (document, photo,
        video, voice…) with a uniform name/size/mime surface, so one branch
        covers them all. Stickers are skipped — they are reactions, not files.
        ``fetch`` downloads via ``download_media`` only after the base adapter
        clears the sender.
        """
        if getattr(message, "media", None) is None:
            return []
        wrapper = getattr(message, "file", None)
        if wrapper is None:
            return []
        if getattr(message, "sticker", None) is not None:
            return []
        message_id = getattr(message, "id", None) or "msg"
        ext = getattr(wrapper, "ext", None) or ""
        name = getattr(wrapper, "name", None) or f"media_{message_id}{ext}"
        mime = getattr(wrapper, "mime_type", None)
        size = getattr(wrapper, "size", None)

        async def fetch(dest_dir: str) -> str:
            if self._client is None:
                raise ChannelAuthError("Telegram userbot client not connected")
            dest = dest_for(dest_dir, name)
            saved = await self._client.download_media(message, file=dest)
            if not saved:
                raise ValueError(f"download_media returned nothing for '{name}'")
            return str(saved)

        return [IncomingFile(name=name, mime=mime, size=size, fetch=fetch)]

    async def _is_mentioned(self, event: Any, message: Any) -> bool:
        """Whether this group message addresses our own account.

        Telethon precomputes ``message.mentioned`` for both an ``@username`` and
        a reply to us, which is the whole question — the entity walk below is
        only a fallback for the shapes it does not set (an inline
        ``MessageEntityMentionName`` on an account with no username).

        Never raises: a message shaped unexpectedly reads as "not mentioned", and
        the relevance judge picks it up from there.
        """
        try:
            if bool(getattr(message, "mentioned", False)):
                return True
            identity = self.self_identity()
            own_id = str(identity.get("user_id") or "")
            username = str(identity.get("username") or "").lstrip("@").lower()
            text = getattr(message, "message", "") or ""
            for entity in getattr(message, "entities", None) or ():
                entity_user = getattr(entity, "user_id", None)
                if own_id and entity_user is not None and str(entity_user) == own_id:
                    return True
                offset = getattr(entity, "offset", None)
                length = getattr(entity, "length", None)
                if username and offset is not None and length is not None:
                    slice_ = text[int(offset):int(offset) + int(length)]
                    if slice_.lstrip("@").lower() == username:
                        return True
        except Exception:  # noqa: BLE001
            logger.debug(
                "telegram-userbot: could not read the mention entities",
                exc_info=True,
            )
        return False

    async def _dispatch_chat_action(self, event: Any) -> None:
        """Notice this account being added to (or joining) a group.

        Telethon reports every membership change in every group, so the filter
        is narrow: only an add/join, only one naming US, and only in a group.
        """
        if not getattr(event, "is_group", False):
            return
        if not (getattr(event, "user_added", False) or getattr(event, "user_joined", False)):
            return
        chat_id = str(getattr(event, "chat_id", "") or "")
        if not chat_id:
            return
        own_id = str(self.self_identity().get("user_id") or "")
        if not own_id:
            return
        user_ids = [str(uid) for uid in (getattr(event, "user_ids", None) or ())]
        if own_id not in user_ids:
            return

        from app.channels.groups.inbound import handle_group_joined

        await handle_group_joined(
            self,
            chat_id=chat_id,
            chat_title=await self._chat_title(event),
            chat_type=(
                "supergroup" if getattr(event, "is_channel", False) else "group"
            ),
        )

    async def fetch_joined_groups(self) -> list[dict] | None:
        """Every group and supergroup this account is in, from its dialog list.

        Broadcast channels are excluded: an account "in" one is a subscriber
        reading announcements, not a member of a conversation, and Telethon
        reports both as ``is_channel``.
        """
        if self._client is None:
            return None
        out: list[dict] = []
        try:
            async for dialog in self._client.iter_dialogs():
                if not getattr(dialog, "is_group", False):
                    continue
                chat_id = str(getattr(dialog, "id", "") or "")
                if not chat_id:
                    continue
                entity = getattr(dialog, "entity", None)
                out.append({
                    "platform_chat_id": chat_id,
                    "title": getattr(dialog, "title", None) or None,
                    "chat_type": (
                        "supergroup"
                        if getattr(entity, "megagroup", False) else "group"
                    ),
                    "member_count": getattr(entity, "participants_count", None),
                })
        except Exception:  # noqa: BLE001
            logger.warning(
                "telegram-userbot: could not list the account's groups",
                exc_info=True,
            )
            return None
        return out

    async def fetch_group_roster(self, chat_id: str) -> list[dict] | None:
        """The group's participants, as a real account can read them.

        Capped: a large supergroup can hold hundreds of thousands of members and
        nothing here needs more than the people who might actually talk.
        """
        if self._client is None:
            return None
        try:
            participants = await self._client.get_participants(
                int(chat_id), limit=_ROSTER_LIMIT,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                f"telegram-userbot: could not list the participants of {chat_id}",
                exc_info=True,
            )
            return None
        out: list[dict] = []
        for user in participants or ():
            user_id = str(getattr(user, "id", "") or "")
            if not user_id:
                continue
            out.append({
                "member_id": user_id,
                "display_name": self._format_display_name(user) or None,
                "username": getattr(user, "username", None),
                "is_bot": bool(getattr(user, "bot", False)),
                "role": _participant_role(user),
            })
        return out

    async def _chat_title(self, event: Any) -> str | None:
        """Best-effort room title; never worth a failed dispatch."""
        chat = getattr(event, "chat", None)
        if chat is None:
            try:
                chat = await event.get_chat()
            except Exception:  # noqa: BLE001
                return None
        return getattr(chat, "title", None)

    @staticmethod
    def _format_display_name(sender: Any) -> str:
        if sender is None:
            return ""
        first = getattr(sender, "first_name", None) or ""
        last = getattr(sender, "last_name", None) or ""
        username = getattr(sender, "username", None) or ""
        full = " ".join(p for p in (first, last) if p).strip()
        if full and username:
            return f"{full} (@{username})"
        return full or (f"@{username}" if username else "")

    # ── outbound ──

    async def _resolve_peer(self, sender_id: str) -> Any:
        cached = self._peer_cache.get(sender_id)
        if cached is not None:
            return cached
        try:
            peer = await self._client.get_input_entity(int(sender_id))
        except (ValueError, TypeError):
            peer = await self._client.get_input_entity(sender_id)
        self._peer_cache[sender_id] = peer
        return peer

    async def _send_text(self, sender_id: str, text: str) -> None:
        if self._client is None:
            raise ChannelAuthError("Telegram userbot client not connected")
        peer = await self._resolve_peer(sender_id)
        await self._client.send_message(peer, text, parse_mode="md")

    async def send_to_chat(self, chat_id: str, text: str) -> None:
        """Send to a room by its chat id, through the same peer cache as DMs."""
        if self._client is None:
            raise ChannelAuthError("Telegram userbot client not connected")
        peer = await self._resolve_peer(str(chat_id))
        await self._client.send_message(peer, text, parse_mode="md")

    async def _send_file(
        self, sender_id: str, path: str, *,
        name: str | None = None, mime: str | None = None,
        caption: str | None = None,
    ) -> None:
        await self._send_file_to_peer(sender_id, path, name, caption)

    async def _send_file_to_chat(
        self, chat_id: str, path: str, *,
        name: str | None = None, mime: str | None = None,
        caption: str | None = None,
    ) -> None:
        await self._send_file_to_peer(str(chat_id), path, name, caption)

    async def _send_file_to_peer(
        self, peer_id: str, path: str, name: str | None, caption: str | None,
    ) -> None:
        """``send_file`` as a document, so the bytes and filename survive.

        ``force_document=True`` keeps Telegram from recompressing images; the
        attributes give the file its intended name when it differs from the
        path's basename.
        """
        if self._client is None:
            raise ChannelAuthError("Telegram userbot client not connected")
        peer = await self._resolve_peer(peer_id)
        from telethon.tl.types import DocumentAttributeFilename  # type: ignore

        attributes = [DocumentAttributeFilename(name)] if name else None
        await self._client.send_file(
            peer, path,
            caption=(caption or "")[:1024] or None,
            force_document=True,
            attributes=attributes,
        )

    async def _send_typing(self, sender_id: str) -> None:
        if self._client is None:
            return
        try:
            peer = await self._resolve_peer(sender_id)
            # ``client.action(peer, 'typing')`` is a context manager that
            # sets the action on enter and clears it on exit. Using it
            # bare like this fires one typing pulse, which Telegram clients
            # render as "typing…" for ~5s.
            async with self._client.action(peer, "typing"):
                pass
        except Exception:  # noqa: BLE001
            # Typing is best-effort; the loop will retry next tick.
            pass
