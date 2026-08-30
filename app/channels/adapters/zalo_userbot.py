"""Zalo personal-account adapter via a ``zca-js`` Node sidecar.

Zalo's personal (non-bot) account has no first-party API; the practical
integration drives a logged-in Zalo Web session through the unofficial
:pypi-js:`zca-js` library, paired via QR scan. This adapter spawns a Node
sidecar (``app/channels/sidecars/zalo/``) that owns the ``zca-js`` session and
bridges its events to this Python adapter over a localhost WebSocket, exactly
like the WhatsApp/Baileys sidecar.

⚠️ Unofficial: automating a personal Zalo account may violate Zalo's terms and
risk account suspension. The catalog instructions warn about this; the official
Bot API transport is :class:`app.channels.adapters.zalo.ZaloBotAdapter`.

Messages in a group thread take the second inbound path
(:meth:`BaseChannelAdapter._handle_group_inbound`); the sidecar used to drop them
outright. This account really is in every room it belongs to and sees the mirrors
the member agents post there, which the group layer drops by their account ids —
so :meth:`BaseChannelAdapter._store_self_identity` matters here, not just in a
roster.

Lifecycle:
    1. Adapter spawns ``node index.js --profile … --channel-id … --working-dir …``.
    2. Sidecar prints ``WS_PORT=<port>``; the adapter connects.
    3. Sidecar emits ``{kind: "qr"|"ready"|"incoming"|"incoming_group"|
       "disconnected"|...}``; ``incoming`` fans into
       :meth:`BaseChannelAdapter._handle_inbound` and ``incoming_group`` into
       :meth:`BaseChannelAdapter._handle_group_inbound`.
    4. Outgoing replies / typing are pushed as ``{kind: "send"|"typing"}`` frames,
       carrying ``thread_type`` when the target is a room rather than a person.

Session credentials (cookie / imei / userAgent) live at
``<working_dir>/<profile>/zalo/<channel_id>/credentials.json`` so a paired
session survives restarts.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from app.channels.base import BaseChannelAdapter, _split_for_messaging
from app.channels.exceptions import ChannelAuthError, ChannelNotImplemented
from app.channels.sidecars.bootstrap import is_install_fresh
from app.config.settings import BaseConfig
from app.utils.logger import logger

_SIDECAR_DIR = Path(__file__).resolve().parents[1] / "sidecars" / "zalo"
_SIDECAR_INDEX = _SIDECAR_DIR / "index.js"
_PORT_HEADER = "WS_PORT="
# The sidecar cuts an over-long ``send`` frame at exactly this many characters
# rather than splitting it, so everything outbound is split here first — the
# base class's own limit is looser than Zalo's and a long reply used to arrive
# with its tail missing and nothing to say so.
_ZALO_TEXT_LIMIT = 2000
# zca-js ThreadType.Group. The sidecar assumes a user thread when a frame omits
# it, which for a room id would mean sending to whoever else owns that number.
_THREAD_GROUP = 1

# How long the sidecar has to answer a member-list request.
_ROSTER_TIMEOUT = 20.0


def _frame_epoch(value: Any) -> float | None:
    """The send time a sidecar frame reports, in epoch seconds, or ``None``.

    The sidecar computes it from ``data.ts`` and JSON-encodes a missing or
    unparseable stamp as ``null``. Only sharpens the dedupe key, so an odd value
    weakens that key rather than costing the message.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class ZaloUserbotAdapter(BaseChannelAdapter):
    """In-process Zalo personal adapter that delegates platform IO to a Node sidecar."""

    # A personal account is in the room like any other member and receives
    # everything posted there.
    supports_group_chats = True
    supports_group_roster = True
    # Best-effort: zca-js emits a group event on some builds and not others,
    # so a group may still be discovered by its first message.
    supports_group_join_events = True
    # ``getAllGroups`` + ``getGroupInfo`` name every group the account is in.
    supports_group_listing = True
    # Zalo tells a personal account nothing about whether an author is
    # automated, so the bot-streak brake has nothing to count here.
    reports_sender_is_bot = False

    # Zalo has no markdown: whatever the mirror wraps arrives as literal
    # characters, so a room's ``*Name*`` would reach it as two stray asterisks.
    bold_markup = ("", "")
    italic_markup = ("", "")

    def __init__(self, channel: dict, storage: Any) -> None:
        super().__init__(channel, storage)
        self._proc: asyncio.subprocess.Process | None = None
        self._ws: Any = None
        self._send_lock = asyncio.Lock()
        # Correlated request/response over the sidecar socket. Only the member
        # list uses it — everything else the sidecar sends is a one-way frame.
        self._pending: dict[str, asyncio.Future] = {}
        self._request_seq = 0

    # ── lifecycle (overrides _run; start/stop are inherited) ──

    async def _run(self) -> None:
        import shutil

        if shutil.which("node") is None:
            raise ChannelNotImplemented(
                "Node.js is not installed or not on PATH. Install Node 20+ "
                "to use the Zalo personal-account channel.",
            )
        if not _SIDECAR_INDEX.exists():
            raise ChannelNotImplemented(f"Zalo sidecar source missing: {_SIDECAR_INDEX}")
        fresh, reason = is_install_fresh(_SIDECAR_DIR)
        if not fresh:
            raise ChannelNotImplemented(
                f"Zalo sidecar dependencies are not ready: {reason}. "
                f"Restart the server (startup auto-installs) or run "
                f"`npm ci` manually in {_SIDECAR_DIR}.",
            )

        try:
            await self._spawn_sidecar()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[channels:zalo] sidecar startup failed channel_id={self.channel_id}: {exc}",
            )
            await self._teardown()
            if isinstance(exc, (ChannelAuthError, ChannelNotImplemented)):
                raise
            raise ChannelAuthError(f"Zalo sidecar failed to start: {exc}") from exc

        try:
            await self._reader_loop()
        finally:
            await self._teardown()

    async def stop(self) -> None:  # type: ignore[override]
        await self._teardown()
        await super().stop()

    # ── platform IO (called by the base class) ──

    async def _send_text(self, sender_id: str, text: str) -> None:
        if self._ws is None:
            raise ChannelAuthError("Zalo sidecar not connected")
        async with self._send_lock:
            for chunk in _split_for_messaging(text, _ZALO_TEXT_LIMIT):
                await self._ws.send(json.dumps({
                    "kind": "send", "sender_id": sender_id, "text": chunk,
                }))

    async def send_to_chat(self, chat_id: str, text: str) -> None:
        """Send to a room by its thread id, saying that it IS one.

        The lock spans the whole message so a room's bubbles arrive in the order
        they were written rather than interleaved with a DM's.
        """
        if self._ws is None:
            raise ChannelAuthError("Zalo sidecar not connected")
        async with self._send_lock:
            for chunk in _split_for_messaging(text, _ZALO_TEXT_LIMIT):
                await self._ws.send(json.dumps({
                    "kind": "send", "sender_id": str(chat_id), "text": chunk,
                    "thread_type": _THREAD_GROUP,
                }))

    async def _send_typing(self, sender_id: str) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps({"kind": "typing", "sender_id": sender_id}))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[channels:zalo] typing send dropped: {e}")

    # ── helpers ──

    async def _spawn_sidecar(self) -> None:
        try:
            import websockets  # type: ignore  # noqa: F401
        except ImportError as exc:
            raise ChannelNotImplemented(
                "Python `websockets` library is missing.",
            ) from exc

        working_dir = BaseConfig.CREMIND_SYSTEM_DIR
        cmd = [
            "node", str(_SIDECAR_INDEX),
            "--profile", self.profile,
            "--channel-id", self.channel_id,
            "--working-dir", working_dir,
        ]
        logger.info(f"zalo[{self.channel_id}]: spawning sidecar — {' '.join(cmd)}")
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(_SIDECAR_DIR),
        )
        port = await self._read_ws_port()
        asyncio.create_task(
            self._tail_stderr(), name=f"zalo-sidecar-stderr:{self.channel_id}",
        )
        import websockets  # type: ignore
        self._ws = await websockets.connect(
            f"ws://127.0.0.1:{port}",
            ping_interval=20,
            ping_timeout=20,
            max_size=4 * 1024 * 1024,
        )

    async def _read_ws_port(self) -> int:
        if self._proc is None or self._proc.stdout is None:
            raise ChannelAuthError("Sidecar did not provide a stdout pipe")
        try:
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=20)
        except asyncio.TimeoutError as exc:
            raise ChannelAuthError(
                "Sidecar did not announce its WebSocket port within 20s — "
                "is `npm install` complete and Node working?",
            ) from exc
        line_str = line.decode(errors="replace").strip()
        if not line_str.startswith(_PORT_HEADER):
            stderr_tail = b""
            if self._proc.stderr is not None:
                try:
                    stderr_tail = await asyncio.wait_for(self._proc.stderr.read(2000), timeout=1)
                except asyncio.TimeoutError:
                    pass
            raise ChannelAuthError(
                f"Unexpected sidecar handshake: {line_str!r}\n"
                f"stderr: {stderr_tail.decode(errors='replace')[:1000]}",
            )
        try:
            return int(line_str[len(_PORT_HEADER):])
        except ValueError as exc:
            raise ChannelAuthError(f"Sidecar emitted bad port: {line_str!r}") from exc

    async def _tail_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            while True:
                chunk = await self._proc.stderr.readline()
                if not chunk:
                    break
                logger.warning(
                    f"zalo[{self.channel_id}] sidecar: {chunk.decode(errors='replace').rstrip()}",
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass

    async def _reader_loop(self) -> None:
        if self._ws is None:
            return
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except Exception:  # noqa: BLE001
                    logger.warning(f"zalo[{self.channel_id}]: dropped non-JSON frame from sidecar")
                    continue
                await self._handle_sidecar_event(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"zalo[{self.channel_id}]: WS reader exited: {exc}")

    async def _handle_sidecar_event(self, msg: dict) -> None:
        kind = msg.get("kind")
        if kind == "qr":
            qr = msg.get("qr")
            raw = msg.get("raw")
            if qr or raw:
                self._publish_auth_event({"kind": "qr", "qr": qr, "raw": raw})
        elif kind == "ready":
            self._publish_auth_event({"kind": "ready"})
            logger.info(f"zalo[{self.channel_id}]: paired and ready")
            # Which account we are, so a bound room recognises the posts its own
            # member agents mirrored into it. This is the only echo defence on
            # this transport — zca-js reports no bot flag, and the sidecar's
            # ``isSelf`` covers only our own posts, not a sibling member's — so
            # a sidecar that could not work the uid out is worth the warning
            # ``_store_self_identity`` logs for an empty id.
            await self._store_self_identity(
                user_id=str(msg.get("self_id") or ""),
                username=None,
                is_bot=False,
                # No ``mention``: on Zalo a mention is a structured annotation on
                # the message, not a token that can be typed into its text.
            )
            await self._mark_linked()
        elif kind == "self_info":
            # Arrives just after ``ready`` (the sidecar looks the profile up
            # without holding pairing up for it). Zalo has no username and no
            # typeable mention token, so this display name is the only handle a
            # group member can address the agent by.
            name = str(msg.get("self_name") or "").strip()
            identity = self.self_identity()
            if name and identity.get("user_id"):
                await self._store_self_identity(
                    user_id=str(identity["user_id"]),
                    username=identity.get("username"),
                    is_bot=bool(identity.get("is_bot")),
                    display_name=name,
                )
        elif kind == "incoming_group":
            chat_id = str(msg.get("chat_id") or "").strip()
            sender_id = str(msg.get("sender_id") or "").strip()
            text = msg.get("text") or ""
            if not chat_id or not sender_id or not text:
                return
            message_id = str(msg.get("message_id") or "").strip()
            asyncio.create_task(
                self._handle_group_inbound_safe(
                    chat_id=chat_id,
                    chat_title=msg.get("chat_title"),
                    # Labelled as Zalo's own thread kind rather than a bare
                    # "group": the ``(channel_type, chat_type)`` pair decides
                    # whether message ids are per-account, and a zca-js thread
                    # numbers them per chat like everything except a legacy
                    # Telegram group.
                    chat_type="zalo_group",
                    sender_id=sender_id,
                    sender_username=None,
                    display_name=msg.get("display_name"),
                    text=text,
                    platform_message_id=message_id or None,
                    platform_message_date=_frame_epoch(msg.get("timestamp")),
                    # Zalo tells a personal account nothing about whether the
                    # author was an automated one, so the honest answer is no.
                    # The id check is the whole echo defence here.
                    sender_is_bot=False,
                    mentioned=self._is_mentioned(msg),
                ),
                name=f"zalo-group-inbound:{self.channel_id}:{chat_id}",
            )
        elif kind == "group_joined":
            chat_id = str(msg.get("chat_id") or "").strip()
            if chat_id:
                asyncio.create_task(
                    self._note_group_joined(chat_id, msg.get("chat_title")),
                    name=f"zalo-group-joined:{self.channel_id}:{chat_id}",
                )
        elif kind == "group_left":
            chat_id = str(msg.get("chat_id") or "").strip()
            if chat_id:
                asyncio.create_task(
                    self._note_group_left(chat_id),
                    name=f"zalo-group-left:{self.channel_id}:{chat_id}",
                )
        elif kind in ("group_info_result", "list_groups_result"):
            self._resolve_pending(msg)
        elif kind == "incoming":
            sender_id = str(msg.get("sender_id") or "").strip()
            display_name = msg.get("display_name")
            text = msg.get("text") or ""
            if not sender_id or not text:
                return
            asyncio.create_task(
                self._handle_inbound_safe(sender_id, display_name, text),
                name=f"zalo-inbound:{self.channel_id}:{sender_id}",
            )
        elif kind == "disconnected":
            logged_out = bool(msg.get("logged_out"))
            self._publish_auth_event({"kind": "disconnected", "logged_out": logged_out})
            logger.info(
                f"zalo[{self.channel_id}]: sidecar reported disconnect (logged_out={logged_out})",
            )
            if logged_out:
                await self._mark_unlinked(
                    reason="logged_out_remote",
                    detail="Zalo session was logged out remotely.",
                )
        elif kind == "send_error":
            logger.warning(
                f"zalo[{self.channel_id}]: send_error — "
                f"sender={msg.get('sender_id')} err={msg.get('error')}",
            )
        elif kind == "error":
            logger.warning(f"zalo[{self.channel_id}]: sidecar error — {msg.get('error')}")

    async def _handle_inbound_safe(
        self, sender_id: str, display_name: str | None, text: str,
    ) -> None:
        try:
            await self._handle_inbound(sender_id, display_name, text)
        except Exception:  # noqa: BLE001
            logger.exception("zalo: inbound handler failed")

    def _is_mentioned(self, msg: dict) -> bool:
        """Whether this room message pings or quotes our own account.

        On Zalo a mention is a structured annotation, never text, so the sidecar
        extracts the uids and this compares them; the quoted message's owner
        counts too, because replying to somebody is how a Zalo conversation
        continues.
        """
        own = str(self.self_identity().get("user_id") or "")
        if not own:
            return False
        candidates = [
            *(msg.get("mentioned_ids") or []),
            msg.get("quoted_sender_id") or "",
        ]
        return any(str(value or "").strip() == own for value in candidates)

    async def _note_group_joined(self, chat_id: str, chat_title: Any) -> None:
        from app.channels.groups.inbound import handle_group_joined

        await handle_group_joined(
            self, chat_id=chat_id, chat_title=chat_title, chat_type="zalo_group",
        )

    async def _note_group_left(self, chat_id: str) -> None:
        from app.channels.groups.inbound import handle_group_left

        await handle_group_left(self, chat_id=chat_id)

    def _new_request(self) -> tuple[str, asyncio.Future]:
        """Register and return a (request_id, future) pair for one round trip."""
        self._request_seq += 1
        request_id = f"r{self._request_seq}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        return request_id, fut

    def _resolve_pending(self, msg: dict) -> bool:
        """Complete the future waiting on this frame's ``request_id``."""
        request_id = msg.get("request_id")
        if not request_id:
            return False
        fut = self._pending.pop(request_id, None)
        if fut is None or fut.done():
            return True
        fut.set_result(msg)
        return True

    async def _request(self, payload: dict, *, what: str) -> dict | None:
        """One correlated round trip to the sidecar, or ``None``.

        The sidecar answers most things by pushing an event nobody waits on;
        these two are questions, so they carry a ``request_id`` and a future.
        """
        if self._ws is None:
            return None
        request_id, fut = self._new_request()
        try:
            async with self._send_lock:
                await self._ws.send(json.dumps({**payload, "request_id": request_id}))
            reply = await asyncio.wait_for(fut, timeout=_ROSTER_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(
                f"zalo-userbot[{self.channel_id}]: the sidecar did not answer "
                f"the {what} request",
            )
            return None
        finally:
            self._pending.pop(request_id, None)
        if not reply.get("ok"):
            logger.warning(
                f"zalo-userbot[{self.channel_id}]: {what} failed: "
                f"{reply.get('error') or 'unknown error'}",
            )
            return None
        return reply

    async def fetch_joined_groups(self) -> list[dict] | None:
        """Every Zalo group this account belongs to, via ``getAllGroups``."""
        reply = await self._request(
            {"kind": "list_groups"}, what="group list",
        )
        if reply is None:
            return None
        out: list[dict] = []
        for group in reply.get("groups") or []:
            chat_id = str((group or {}).get("id") or "").strip()
            if not chat_id:
                continue
            out.append({
                "platform_chat_id": chat_id,
                "title": group.get("name") or None,
                "chat_type": "zalo_group",
                "member_count": group.get("member_count"),
            })
        return out

    async def fetch_group_roster(self, chat_id: str) -> list[dict] | None:
        """The room's members, via the sidecar's ``getGroupInfo`` call."""
        reply = await self._request(
            {"kind": "group_info", "chat_id": str(chat_id)},
            what=f"member list for {chat_id}",
        )
        if reply is None:
            return None
        out: list[dict] = []
        for member in reply.get("members") or []:
            member_id = str(member.get("id") or "").strip()
            if not member_id:
                continue
            out.append({
                "member_id": member_id,
                "display_name": member.get("display_name"),
                "username": None,
                "is_bot": False,
                "role": "admin" if member.get("is_admin") else "member",
            })
        return out

    async def _send_typing_to_chat(self, chat_id: str) -> None:
        """Typing in a room: the same control frame with the group thread type."""
        if self._ws is None:
            return
        try:
            async with self._send_lock:
                await self._ws.send(json.dumps({
                    "kind": "typing",
                    "sender_id": str(chat_id),
                    "thread_type": _THREAD_GROUP,
                }))
        except Exception:  # noqa: BLE001
            logger.debug("zalo-userbot: group typing indicator failed", exc_info=True)

    async def _handle_group_inbound_safe(self, **kwargs: Any) -> None:
        try:
            await self._handle_group_inbound(**kwargs)
        except Exception:  # noqa: BLE001
            logger.exception("zalo: group inbound handler failed")

    async def _teardown(self) -> None:
        ws = self._ws
        proc = self._proc
        self._ws = None
        self._proc = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        if proc is not None:
            if proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except (asyncio.TimeoutError, ProcessLookupError):
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
                except Exception:  # noqa: BLE001
                    pass
        self._auth_subscribers.clear()
        self._latest_auth_event = None
