"""Telegram bot adapter (long-polling).

Uses :pypi:`python-telegram-bot` (added to ``pyproject.toml`` as part of the
channels feature). Each adapter instance owns one :class:`telegram.Bot` and
runs ``getUpdates`` long-polling in :meth:`_run`.

Serves two modes over the same bot transport: conversational ``bot`` mode and
``notification`` mode (outbound alerts, no conversation — the notification
behavior itself lives in
:class:`app.channels.notification_delivery.NotificationDeliveryMixin` on the
base class). The user-account transport is the separate userbot adapter.

Messages from a group room take the second inbound path
(:meth:`BaseChannelAdapter._handle_group_inbound`) rather than the DM pipeline.
Telegram only delivers those to a bot whose privacy mode is disabled (or which
is a chat admin) — otherwise the bot sees commands and @mentions only.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.channels.attachments import IncomingFile, dest_for
from app.channels.base import BaseChannelAdapter, platform_message_timestamp
from app.channels.exceptions import ChannelAuthError, ChannelNotImplemented
from app.utils.logger import logger

# Hard Bot API caps, both below Cremind's own upload ceiling: ``getFile``
# refuses files over 20 MB and uploads over 50 MB. The userbot transport has
# neither limit — the bundled doc points people there for big files.
_TG_BOT_DOWNLOAD_LIMIT = 20 * 1024 * 1024
_TG_BOT_UPLOAD_LIMIT = 50 * 1024 * 1024


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


class TelegramAdapter(BaseChannelAdapter):
    # A bot sees room messages once its privacy mode is disabled (or it is a
    # chat admin), and can post to a chat id it was never DMed from.
    supports_group_chats = True
    # The Bot API cannot enumerate a group — ``get_chat_administrators`` is
    # the most it will answer — so the roster is administrators plus whoever
    # has posted.
    supports_group_roster = True
    supports_group_join_events = True
    reports_sender_is_bot = True
    supports_file_send = True

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
                "python-telegram-bot is not installed. Re-enabling this "
                "channel installs it automatically; to install it manually run "
                "`cremind features install channel.telegram.bot`.",
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
        # This adapter powers both conversational bot mode and notification
        # mode (a notification channel reuses the bot transport — same token,
        # same getUpdates loop — and layers on ``NotificationDeliveryMixin``
        # behavior via the base class). The userbot transport is a separate
        # adapter, so anything else here is unsupported.
        if self.channel.get("mode") not in ("bot", "notification"):
            raise ChannelNotImplemented(
                f"TelegramAdapter does not support mode={self.channel.get('mode')!r}",
            )

        try:
            self._bot = await self._build_bot()
        except (ChannelNotImplemented, ChannelAuthError):
            raise
        except Exception as exc:  # noqa: BLE001
            raise ChannelAuthError(f"Telegram bot init failed: {exc}") from exc

        # Who this bot is on Telegram. A bound group needs it to recognise (and
        # ignore) our own mirrored posts, and the group roster shows the
        # ``@username`` so the other agents can address us. Best-effort: a
        # failed getMe is no reason to refuse to poll.
        try:
            me = await self._bot.get_me()
            username = getattr(me, "username", None)
            await self._store_self_identity(
                user_id=str(me.id),
                username=username,
                is_bot=True,
                # Telegram pings an account by its ``@username`` and nothing
                # else; a bot without one cannot be addressed in a room at all.
                mention=f"@{username}" if username else None,
                display_name=_full_name(me),
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "[channels:telegram] get_me failed; self identity unknown",
                exc_info=True,
            )

        offset = (self.channel.get("state") or {}).get("last_update_id")
        offset = int(offset) + 1 if offset else None

        while True:
            try:
                if self._bot is None:
                    self._bot = await self._build_bot()
                updates = await self._bot.get_updates(
                    offset=offset, timeout=30,
                    # ``my_chat_member`` is how we learn we were added to a
                    # group: a chat id is not something a person can look up in
                    # Telegram, so the group settings page offers the rooms the
                    # bots have actually been added to.
                    allowed_updates=["message", "my_chat_member"],
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
                await self._handle_update(update)

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

    async def _handle_update(self, update: Any) -> None:
        """Route one polled update to the DM pipeline or to a group room.

        Split out of :meth:`_run` so the routing decision is testable without a
        live bot. Message handling is spawned rather than awaited so two
        different senders in the same poll batch don't block each other;
        per-sender ordering is preserved inside ``_handle_inbound`` via
        ``_inflight``.
        """
        member_update = getattr(update, "my_chat_member", None)
        if member_update is not None:
            self._note_chat_membership(member_update)
            return

        msg = getattr(update, "message", None)
        if msg is None:
            return
        files = self._extract_files(msg)
        # A media message's text lives in ``caption``; a file with no caption
        # still has to reach the pipeline (the base synthesizes a placeholder).
        text = msg.text or getattr(msg, "caption", None) or ""
        if not text and not files:
            return

        chat = getattr(msg, "chat", None)
        chat_type = getattr(chat, "type", None)
        user = getattr(msg, "from_user", None)

        if chat_type in ("group", "supergroup"):
            # A group message belongs to the room. It used to fall through to
            # the private-chat path below, whose reply is sent to
            # ``chat_id=int(sender_id)`` — so a question asked in a group was
            # answered in the asker's DM instead.
            if user is None:
                return
            if self._is_self(user):
                return
            message_id = getattr(msg, "message_id", None)
            asyncio.create_task(
                self._handle_group_inbound_safe(
                    chat_id=str(chat.id),
                    chat_title=getattr(chat, "title", None),
                    chat_type=chat_type,
                    sender_id=str(user.id),
                    sender_username=getattr(user, "username", None),
                    display_name=" ".join(
                        filter(None, [
                            getattr(user, "first_name", None),
                            getattr(user, "last_name", None),
                        ]),
                    ) or None,
                    text=text,
                    platform_message_id=(
                        str(message_id) if message_id is not None else None
                    ),
                    # Telegram's own send time, identical on every account that
                    # received this message — which is what makes it usable for
                    # telling copies apart from repeats in a legacy group.
                    platform_message_date=platform_message_timestamp(msg),
                    sender_is_bot=bool(getattr(user, "is_bot", False)),
                    mentioned=self._is_mentioned(msg),
                    files=files or None,
                ),
                name=f"telegram-group-inbound:{self.channel_id}:{chat.id}",
            )
            return

        sender_id = str(user.id) if user else str(msg.chat.id)
        display_name = _full_name(user) if user else None
        asyncio.create_task(
            self._handle_inbound_safe(sender_id, display_name, text, files=files or None),
            name=f"telegram-inbound:{self.channel_id}:{sender_id}",
        )

    def _extract_files(self, msg: Any) -> list[IncomingFile]:
        """Attachment descriptors for one message — no bytes are fetched here.

        Covers documents, photos (largest size), video, audio, voice notes and
        video notes. Stickers are deliberately skipped: people use them as
        reactions, and "understand this webp of a cat in sunglasses" is not a
        request anyone made. Each descriptor's ``fetch`` runs ``getFile`` only
        after the base adapter has decided the sender may talk to the agent.
        """
        message_id = getattr(msg, "message_id", None) or "msg"
        found: list[IncomingFile] = []

        document = getattr(msg, "document", None)
        if document is not None:
            found.append(self._incoming_media(
                document,
                getattr(document, "file_name", None) or f"document_{message_id}",
                getattr(document, "mime_type", None),
            ))
        photos = list(getattr(msg, "photo", None) or ())
        if photos:
            # PhotoSize entries are ordered smallest→largest; take the original.
            found.append(self._incoming_media(
                photos[-1], f"photo_{message_id}.jpg", "image/jpeg",
            ))
        video = getattr(msg, "video", None)
        if video is not None:
            found.append(self._incoming_media(
                video,
                getattr(video, "file_name", None) or f"video_{message_id}.mp4",
                getattr(video, "mime_type", None) or "video/mp4",
            ))
        audio = getattr(msg, "audio", None)
        if audio is not None:
            found.append(self._incoming_media(
                audio,
                getattr(audio, "file_name", None) or f"audio_{message_id}.mp3",
                getattr(audio, "mime_type", None),
            ))
        voice = getattr(msg, "voice", None)
        if voice is not None:
            found.append(self._incoming_media(
                voice, f"voice_{message_id}.ogg",
                getattr(voice, "mime_type", None) or "audio/ogg",
            ))
        video_note = getattr(msg, "video_note", None)
        if video_note is not None:
            found.append(self._incoming_media(
                video_note, f"video_note_{message_id}.mp4", "video/mp4",
            ))
        return found

    def _incoming_media(
        self, media: Any, name: str, mime: str | None,
    ) -> IncomingFile:
        file_id = getattr(media, "file_id", None)
        size = getattr(media, "file_size", None)

        async def fetch(dest_dir: str) -> str:
            if size and size > _TG_BOT_DOWNLOAD_LIMIT:
                raise ValueError(
                    f"'{name}' is {size} bytes; Telegram bots can only download "
                    f"files up to {_TG_BOT_DOWNLOAD_LIMIT} bytes (use the "
                    "userbot transport for bigger files)",
                )
            if self._bot is None:
                self._bot = await self._build_bot()
            tg_file = await self._bot.get_file(file_id)
            dest = dest_for(dest_dir, name)
            await tg_file.download_to_drive(custom_path=dest)
            return dest

        return IncomingFile(name=name, mime=mime, size=size, fetch=fetch)

    def _is_self(self, user: Any) -> bool:
        """Whether an update was written by this very bot.

        Telegram does not deliver one bot's messages to another bot, so this
        only fires on our own posts coming back — cheap insurance that costs
        nothing when the identity has not been recorded yet.
        """
        identity = (self.channel.get("state") or {}).get("self_identity") or {}
        own = str(identity.get("user_id") or "")
        return bool(own) and str(getattr(user, "id", "")) == own

    def _is_mentioned(self, msg: Any) -> bool:
        """Whether this message addresses our bot.

        Three ways Telegram says so, and the entities are the only reliable one:
        searching the raw text for ``@name`` would match somebody spelling the
        bot's name inside a sentence, and would miss a ``text_mention``, which
        is how a user without a username is tagged.

        - a ``mention`` entity whose slice equals our ``@username``;
        - a ``text_mention`` entity carrying our user id;
        - a reply to one of our own messages, which in a Telegram group is how
          people continue a conversation with the bot without re-typing its name.

        Defensive throughout: this runs on every group message, and a message
        shaped unexpectedly must read as "not mentioned", never raise.
        """
        try:
            identity = self.self_identity()
            own_id = str(identity.get("user_id") or "")
            username = str(identity.get("username") or "").lstrip("@").lower()

            reply = getattr(msg, "reply_to_message", None)
            if reply is not None and own_id:
                replied_to = getattr(reply, "from_user", None)
                if str(getattr(replied_to, "id", "")) == own_id:
                    return True

            text = getattr(msg, "text", "") or ""
            for entity in getattr(msg, "entities", None) or ():
                etype = getattr(entity, "type", None)
                if etype == "text_mention" and own_id:
                    user = getattr(entity, "user", None)
                    if str(getattr(user, "id", "")) == own_id:
                        return True
                elif etype == "mention" and username:
                    offset = getattr(entity, "offset", None)
                    length = getattr(entity, "length", None)
                    if offset is None or length is None:
                        continue
                    slice_ = text[int(offset):int(offset) + int(length)]
                    if slice_.lstrip("@").lower() == username:
                        return True
        except Exception:  # noqa: BLE001
            logger.debug(
                "[channels:telegram] could not read the mention entities",
                exc_info=True,
            )
        return False

    def _note_chat_membership(self, member_update: Any) -> None:
        """React to being added to (or removed from) a group.

        Telegram is the only bot transport that reports this, which is why a
        group here becomes visible the moment the bot is added rather than when
        somebody next speaks. Only a transition INTO the group counts: Telegram
        sends ``my_chat_member`` for every status change, including our own
        promotion to admin, and treating those as joins would re-ask about a
        group the operator already answered for.

        Defensive throughout (plain ``getattr``): this runs on every membership
        change Telegram reports, in rooms that may have nothing to do with
        Cremind, and it must never be able to break the poll loop.
        """
        chat = getattr(member_update, "chat", None)
        chat_type = getattr(chat, "type", None)
        if chat is None or chat_type not in ("group", "supergroup"):
            return
        chat_id = str(getattr(chat, "id", "") or "")
        if not chat_id:
            return
        old_status = str(
            getattr(getattr(member_update, "old_chat_member", None), "status", "")
            or ""
        )
        new_status = str(
            getattr(getattr(member_update, "new_chat_member", None), "status", "")
            or ""
        )
        if new_status not in ("member", "administrator"):
            return
        if old_status not in ("left", "kicked", ""):
            return
        asyncio.create_task(
            self._note_group_joined(
                chat_id=chat_id,
                chat_title=getattr(chat, "title", None),
                chat_type=chat_type,
            ),
            name=f"telegram-group-joined:{self.channel_id}:{chat_id}",
        )

    async def _note_group_joined(
        self, *, chat_id: str, chat_title: str | None, chat_type: str | None,
    ) -> None:
        from app.channels.groups.inbound import handle_group_joined

        await handle_group_joined(
            self, chat_id=chat_id, chat_title=chat_title, chat_type=chat_type,
        )

    async def fetch_group_roster(self, chat_id: str) -> list[dict] | None:
        """The group's administrators — all the Bot API will name.

        Ordinary members are unreachable: Telegram gives a bot no way to list
        them, by design. The rest of the roster fills in as people post, which is
        why the member list in the UI says where each entry came from.
        """
        if self._bot is None:
            self._bot = await self._build_bot()
        try:
            admins = await self._bot.get_chat_administrators(chat_id=int(chat_id))
        except Exception:  # noqa: BLE001
            logger.warning(
                f"[channels:telegram] could not list the administrators of "
                f"{chat_id}",
                exc_info=True,
            )
            return None
        out: list[dict] = []
        for entry in admins or ():
            user = getattr(entry, "user", None)
            user_id = str(getattr(user, "id", "") or "")
            if not user_id:
                continue
            out.append({
                "member_id": user_id,
                "display_name": " ".join(
                    filter(None, [
                        getattr(user, "first_name", None),
                        getattr(user, "last_name", None),
                    ]),
                ) or None,
                "username": getattr(user, "username", None),
                "is_bot": bool(getattr(user, "is_bot", False)),
                "role": "admin",
            })
        return out

    async def _handle_inbound_safe(
        self, sender_id: str, display_name: str | None, text: str,
        files: Any = None,
    ) -> None:
        try:
            await self._handle_inbound(sender_id, display_name, text, files=files)
        except Exception:  # noqa: BLE001
            logger.exception("telegram: inbound handler failed")

    async def _handle_group_inbound_safe(self, **kwargs: Any) -> None:
        try:
            await self._handle_group_inbound(**kwargs)
        except Exception:  # noqa: BLE001
            logger.exception("telegram: group inbound handler failed")

    async def _send_text(self, sender_id: str, text: str) -> None:
        if self._bot is None:
            self._bot = await self._build_bot()
        # Telegram's chat_id for direct messages == the user id.
        chat_id = int(sender_id)
        await self._send_with_retry(chat_id, text)

    async def send_to_chat(self, chat_id: str, text: str) -> None:
        """Send to a room by its chat id (negative for Telegram groups)."""
        if self._bot is None:
            self._bot = await self._build_bot()
        await self._send_with_retry(int(chat_id), text)

    async def _send_file(
        self, sender_id: str, path: str, *,
        name: str | None = None, mime: str | None = None,
        caption: str | None = None,
    ) -> None:
        await self._send_document_with_retry(int(sender_id), path, name, caption)

    async def _send_file_to_chat(
        self, chat_id: str, path: str, *,
        name: str | None = None, mime: str | None = None,
        caption: str | None = None,
    ) -> None:
        await self._send_document_with_retry(int(chat_id), path, name, caption)

    async def _send_typing_to_chat(self, chat_id: str) -> None:
        """Show "typing…" in a group, addressed by the room's own (negative) id."""
        try:
            if self._bot is None:
                self._bot = await self._build_bot()
            await self._bot.send_chat_action(chat_id=int(chat_id), action="typing")
        except Exception:  # noqa: BLE001
            logger.debug(
                "[channels:telegram] group typing indicator failed", exc_info=True,
            )

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

    async def _send_document_with_retry(
        self, chat_id: int, path: str, name: str | None, caption: str | None,
    ) -> None:
        """``send_document`` with the same stale-pool reset as text sends.

        Everything goes out as a document (not ``send_photo``) so the file
        arrives byte-identical with its filename — Telegram recompresses
        photos. Captions are clipped to Telegram's 1024-char cap.
        """
        import os

        from telegram.error import NetworkError  # type: ignore

        try:
            size = os.path.getsize(path)
        except OSError as exc:
            raise ValueError(f"cannot read file to send: {path}") from exc
        if size > _TG_BOT_UPLOAD_LIMIT:
            raise ValueError(
                f"'{name or os.path.basename(path)}' is {size} bytes; Telegram "
                f"bots can only upload files up to {_TG_BOT_UPLOAD_LIMIT} bytes",
            )
        clipped = (caption or "")[:1024] or None

        attempts = 0
        last_exc: Exception | None = None
        max_attempts = 3
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
                with open(path, "rb") as handle:
                    await self._bot.send_document(
                        chat_id=chat_id,
                        document=handle,
                        filename=name or os.path.basename(path),
                        caption=clipped,
                        read_timeout=120.0,
                        write_timeout=120.0,
                    )
                return
            except NetworkError as exc:
                last_exc = exc
                logger.warning(
                    f"telegram: file send failed (attempt {attempts}/{max_attempts}); "
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
