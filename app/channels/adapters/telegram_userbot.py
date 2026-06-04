"""Telegram userbot adapter (Telethon-based).

In contrast to the bot adapter (which uses long-polling against an
``@BotFather`` token), this adapter logs in to Telegram as **your own user
account** using Telethon. Inbound DMs from any contact are routed to
:meth:`BaseChannelAdapter._handle_inbound`; the agent's reply is sent back
through the same Telethon client. This is what Telegram colloquially
calls a "userbot" — explicitly permitted by Telegram (it's the use case
Telethon and Pyrogram are designed for).

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

from app.channels.base import BaseChannelAdapter
from app.channels.exceptions import ChannelAuthError, ChannelNotImplemented
from app.config.settings import BaseConfig
from app.utils.logger import logger


class TelegramUserbotAdapter(BaseChannelAdapter):
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
                "telethon is not installed. Add `telethon>=1.36` to "
                "pyproject.toml and reinstall.",
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
        # Skip non-DM messages — groups, channels, and self-chats are not
        # routed to the agent.
        if not event.is_private:
            return
        if not event.message or event.message.out:
            return
        text = event.message.message or ""
        if not text:
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

        await self._handle_inbound(sender_id, display_name, text)

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
