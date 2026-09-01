"""WhatsApp adapter via a Baileys Node sidecar.

WhatsApp has no first-party bot API; the practical integration is to drive
WhatsApp Web from a controlled session and pair it with a phone via QR
scan (or pairing code). The adapter spawns a Node sidecar (located at
``app/channels/sidecars/whatsapp/``) that uses the
:pypi-js:`@whiskeysockets/baileys` library and bridges its events to this
Python adapter over a localhost WebSocket on an ephemeral port.

Lifecycle:
    1. Adapter spawns ``node index.js --profile … --channel-id … --working-dir …``.
    2. Sidecar prints ``WS_PORT=<port>`` to stdout once its WebSocket
       server is listening; the adapter parses that line and connects.
    3. Sidecar emits ``{kind: "qr"|"ready"|"incoming"|"incoming_group"|
       "disconnected"|...}`` JSON frames; the adapter consumes them.
       ``incoming`` fans out into :meth:`BaseChannelAdapter._handle_inbound`;
       ``incoming_group`` is a message written in a ``@g.us`` room and goes to
       :meth:`BaseChannelAdapter._handle_group_inbound` instead, because it
       belongs to the room rather than to whoever sent it.
    4. Outgoing replies and the typing indicator are pushed to the
       sidecar as ``{kind: "send"|"typing"}`` frames.

Auth-state on disk lives at
``<working_dir>/<profile>/whatsapp/<channel_id>/session/`` so a paired
session survives restarts. Deleting that directory forces a fresh
QR-scan pairing on next start.

Prerequisites (surfaced as :class:`ChannelNotImplemented` if missing):
    - Node 18+ on PATH.
    - ``node_modules/`` inside ``app/channels/sidecars/whatsapp/``. Starting
      the channel installs it on demand (see
      :func:`app.channels.sidecars.bootstrap.ensure_sidecar_ready`), so this
      only fails when npm is absent or the install itself does.

Pairing UX:
    The latest QR (data URL) is published on a per-adapter pub-sub queue
    that the API's ``GET /api/channels/{id}/qr`` SSE endpoint subscribes
    to. The web UI's Channels page renders the QR until the sidecar
    reports ``ready``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

from app.channels.attachments import IncomingFile, files_from_sidecar_frame
from app.channels.base import BaseChannelAdapter
from app.channels.exceptions import ChannelAuthError, ChannelNotImplemented
from app.channels.sidecars.bootstrap import ensure_sidecar_ready
from app.config.settings import BaseConfig
from app.utils.logger import logger


_SIDECAR_DIR = Path(__file__).resolve().parents[1] / "sidecars" / "whatsapp"
_SIDECAR_INDEX = _SIDECAR_DIR / "index.js"
_PORT_HEADER = "WS_PORT="

# How long to wait for the sidecar's correlated answer to a send / resolve.
# Generous: Baileys may be mid-reconnect, and a false "timed out" would cost a
# direct-send recipient their history entry (see ``send_strict``).
_ACK_TIMEOUT = 20.0
# File uploads carry the whole payload to WhatsApp's servers before the ack
# comes back, so they get a far longer leash than a text send.
_FILE_ACK_TIMEOUT = 180.0

_PN_SUFFIX = "@s.whatsapp.net"
_LID_SUFFIX = "@lid"

# The ``chat_type`` reported for a WhatsApp room. Deliberately not the bare word
# "group": WhatsApp numbers messages per chat, so one message id identifies it
# on every account that received it and the message-id dedupe key applies —
# whereas ``("telegram", "group")`` in ``app.channels.groups.keys`` means "ids are per
# account, fingerprint the content instead". Naming this "group" would opt
# WhatsApp into a weaker key it does not need.
_GROUP_CHAT_TYPE = "whatsapp_group"


def _normalize_lid(value: str | None) -> str | None:
    """Return a linked-identity alias in canonical full-JID form, or ``None``.

    Baileys has returned the ``lid`` both bare and suffixed across versions, and
    the two forms must not both end up in the ``wa_lid`` column: the inbound
    adoption lookup is an exact match, so a stored bare id would silently never
    match an incoming ``<id>@lid`` and the contact would fork in two. Normalize
    on the way in so there is only ever one shape in the database.
    """
    text = str(value or "").strip()
    if not text:
        return None
    return text if text.endswith(_LID_SUFFIX) else f"{text}{_LID_SUFFIX}"


class WhatsappAdapter(BaseChannelAdapter):
    """In-process WhatsApp adapter that delegates platform IO to a Node sidecar."""

    # The linked account is an ordinary member of every room it belongs to and
    # receives everything posted there. ``bold_markup`` / ``italic_markup`` are
    # inherited on purpose — WhatsApp really does spell emphasis ``*bold*`` and
    # ``_italic_``, the same dialect the defaults were written for.
    supports_group_chats = True
    supports_group_roster = True
    supports_group_join_events = True
    # ``groupFetchAllParticipating`` names every group this number is in.
    supports_group_listing = True
    # Every WhatsApp account is a person's, ours included: the platform has
    # no bot flag, so the consecutive-bot-messages brake has nothing to
    # count and the id check is the whole echo defence.
    reports_sender_is_bot = False
    # ``send_file`` control frame → Baileys media message. The frame carries a
    # PATH, never bytes — sidecar and server share the filesystem, and the WS
    # has a 4 MiB frame cap.
    supports_file_send = True

    def __init__(self, channel: dict, storage: Any) -> None:
        super().__init__(channel, storage)
        self._proc: asyncio.subprocess.Process | None = None
        self._ws: Any = None
        self._send_lock = asyncio.Lock()
        # Correlation id -> future awaiting the sidecar's answer. Sends and
        # phone resolutions are request/response over one WebSocket, so the
        # reply loop resolves the future the caller is parked on.
        self._pending: dict[str, asyncio.Future] = {}
        self._request_seq = 0

    # ── lifecycle (overrides _run; start/stop are inherited) ──

    async def _run(self) -> None:
        await ensure_sidecar_ready(_SIDECAR_DIR, label="WhatsApp", node_hint="Node 18+")
        try:
            await self._spawn_sidecar()
        except Exception as exc:  # noqa: BLE001
            # Surface the underlying cause in the central log before the
            # wrapping ChannelAuthError swallows the traceback.
            logger.warning(f"[channels:whatsapp] sidecar startup failed channel_id={self.channel_id}: {exc}")
            await self._teardown()
            if isinstance(exc, (ChannelAuthError, ChannelNotImplemented)):
                raise
            raise ChannelAuthError(f"WhatsApp sidecar failed to start: {exc}") from exc

        try:
            await self._reader_loop()
        finally:
            await self._teardown()

    async def stop(self) -> None:  # type: ignore[override]
        # Tear down the sidecar before the base class cancels the run task,
        # otherwise Baileys can hold the process open via outstanding
        # network handles for several seconds.
        await self._teardown()
        await super().stop()

    # ── platform IO (called by the base class) ──

    async def _send_text(self, sender_id: str, text: str) -> None:
        """Send one message and wait for the sidecar to confirm it went out.

        Writing the frame only proves it reached the local Node process, which
        is why this awaits a correlated ``send_ack``: an unpaired session or a
        rejected JID must surface as an exception, not as a silent success that
        the direct-send path would record in someone's conversation history.
        Confirmation means WhatsApp's servers accepted the message — not that
        it was delivered to the recipient's device.
        """
        if self._ws is None:
            raise ChannelAuthError("WhatsApp sidecar not connected")
        request_id, fut = self._new_request()
        try:
            async with self._send_lock:
                await self._ws.send(json.dumps({
                    "kind": "send", "sender_id": sender_id, "text": text,
                    "request_id": request_id,
                }))
            reply = await asyncio.wait_for(fut, timeout=_ACK_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise ChannelAuthError(
                f"WhatsApp sidecar did not confirm the send within "
                f"{_ACK_TIMEOUT:.0f}s (recipient {sender_id})",
            ) from exc
        finally:
            self._pending.pop(request_id, None)
        if not reply.get("ok"):
            raise ChannelAuthError(
                f"WhatsApp send failed: {reply.get('error') or 'unknown error'}",
            )

    async def send_to_chat(self, chat_id: str, text: str) -> None:
        """Post into a ``@g.us`` room, through the same acked path as a DM.

        The sidecar's ``send`` addresses whatever JID it is handed, so a room id
        needs no separate control frame — but it does need the correlated ack:
        a mirrored post that WhatsApp rejected has to surface as an exception
        rather than as a room that quietly stops hearing the agents.
        """
        await self._send_text(str(chat_id), text)

    async def _send_file(
        self, sender_id: str, path: str, *,
        name: str | None = None, mime: str | None = None,
        caption: str | None = None,
    ) -> None:
        await self._send_file_frame(sender_id, path, name, mime, caption)

    async def _send_file_to_chat(
        self, chat_id: str, path: str, *,
        name: str | None = None, mime: str | None = None,
        caption: str | None = None,
    ) -> None:
        # Like ``send``, the sidecar addresses whatever JID it is handed.
        await self._send_file_frame(str(chat_id), path, name, mime, caption)

    async def _send_file_frame(
        self, jid: str, path: str, name: str | None, mime: str | None,
        caption: str | None,
    ) -> None:
        """``send_file`` control frame, awaited like a text send.

        The frame carries the file's absolute PATH — never bytes — because the
        WS has a 4 MiB frame cap and the sidecar shares this filesystem by
        design (it already takes ``--working-dir``).
        """
        if self._ws is None:
            raise ChannelAuthError("WhatsApp sidecar not connected")
        abs_path = os.path.abspath(path)
        if not os.path.isfile(abs_path):
            raise ValueError(f"cannot read file to send: {path}")
        request_id, fut = self._new_request()
        try:
            async with self._send_lock:
                await self._ws.send(json.dumps({
                    "kind": "send_file",
                    "sender_id": jid,
                    "path": abs_path,
                    "name": name or os.path.basename(abs_path),
                    "mime": mime,
                    "caption": caption,
                    "request_id": request_id,
                }))
            reply = await asyncio.wait_for(fut, timeout=_FILE_ACK_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise ChannelAuthError(
                f"WhatsApp sidecar did not confirm the file send within "
                f"{_FILE_ACK_TIMEOUT:.0f}s (recipient {jid})",
            ) from exc
        finally:
            self._pending.pop(request_id, None)
        if not reply.get("ok"):
            raise ChannelAuthError(
                f"WhatsApp file send failed: {reply.get('error') or 'unknown error'}",
            )

    async def _send_typing_to_chat(self, chat_id: str) -> None:
        """The sidecar's presence update takes any JID, a room's included."""
        await self._send_typing(str(chat_id))

    async def resolve_phone(self, phone: str) -> dict:
        """Look up ``phone`` on WhatsApp — ``{exists, jid, lid}``.

        Used before cold-messaging a number nobody has messaged us from: it
        answers whether the number is on WhatsApp at all (so we don't create a
        contact and a history entry for a send that will never land) and
        returns the canonical JID plus the ``@lid`` alias the contact may reply
        from. Raises on transport failure; ``exists=False`` is a normal answer.
        """
        if self._ws is None:
            raise ChannelAuthError("WhatsApp sidecar not connected")
        digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
        if not digits:
            return {"exists": False, "jid": None, "lid": None}
        request_id, fut = self._new_request()
        try:
            async with self._send_lock:
                await self._ws.send(json.dumps({
                    "kind": "resolve", "phone": digits, "request_id": request_id,
                }))
            reply = await asyncio.wait_for(fut, timeout=_ACK_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise ChannelAuthError(
                f"WhatsApp sidecar did not answer the number lookup within "
                f"{_ACK_TIMEOUT:.0f}s",
            ) from exc
        finally:
            self._pending.pop(request_id, None)
        if not reply.get("ok"):
            raise ChannelAuthError(
                f"WhatsApp number lookup failed: {reply.get('error') or 'unknown error'}",
            )
        return {
            "exists": bool(reply.get("exists")),
            "jid": reply.get("jid") or f"{digits}{_PN_SUFFIX}",
            "lid": _normalize_lid(reply.get("lid")),
        }

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

    def _derive_phone(self, sender_id: str) -> str | None:
        """WhatsApp pn-JIDs carry the contact's number; ``@lid`` ones don't."""
        sid = (sender_id or "").strip()
        if not sid.endswith(_PN_SUFFIX):
            return None
        digits = sid[: -len(_PN_SUFFIX)]
        return digits if digits.isdigit() else None

    async def _send_typing(self, sender_id: str) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps({
                "kind": "typing", "sender_id": sender_id,
            }))
        except Exception as e:  # noqa: BLE001
            # Typing is best-effort; the typing-loop will retry on next tick.
            logger.debug(f"[channels:whatsapp] typing send dropped: {e}")

    # ── helpers ──

    def _resolve_working_dir(self) -> str:
        # ``BaseConfig.CREMIND_SYSTEM_DIR`` already expands ``~`` and
        # normalises path separators (the codebase's canonical accessor —
        # don't re-roll). The sidecar also re-expands ``~`` defensively in
        # case a non-canonical value ever flows through.
        return BaseConfig.CREMIND_SYSTEM_DIR

    def _media_spool_dir(self) -> str:
        """Where the sidecar spools inbound media before Python claims it.

        Next to the session dir, inside the profile's slice. The sidecar
        downloads media at receipt (Baileys' media keys are only reliably
        usable near the event) and puts the PATH in the frame; the descriptor's
        ``fetch`` then moves the file into the conversation's upload dir, and
        every drop path deletes it. Wiped on each spawn so a crash never
        accumulates orphans.
        """
        return os.path.join(
            self._resolve_working_dir(), self.profile, "whatsapp",
            self.channel_id, "media_spool",
        )

    def _prepare_media_spool(self) -> str:
        media_dir = self._media_spool_dir()
        shutil.rmtree(media_dir, ignore_errors=True)
        os.makedirs(media_dir, exist_ok=True)
        return media_dir

    async def _spawn_sidecar(self) -> None:
        try:
            import websockets  # type: ignore  # noqa: F401
        except ImportError as exc:
            raise ChannelNotImplemented(
                "Python `websockets` library is missing. Add `websockets` "
                "to ``pyproject.toml`` and reinstall.",
            ) from exc

        working_dir = self._resolve_working_dir()
        media_dir = self._prepare_media_spool()
        from app.utils.uploads_tmp import max_upload_bytes

        cmd = [
            "node", str(_SIDECAR_INDEX),
            "--profile", self.profile,
            "--channel-id", self.channel_id,
            "--working-dir", working_dir,
            "--media-dir", media_dir,
            "--media-max-bytes", str(max_upload_bytes()),
        ]
        logger.info(
            f"whatsapp[{self.channel_id}]: spawning sidecar — {' '.join(cmd)}",
        )
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(_SIDECAR_DIR),
        )
        port = await self._read_ws_port()
        # Tail stderr in the background so panic output is visible in logs.
        asyncio.create_task(
            self._tail_stderr(),
            name=f"whatsapp-sidecar-stderr:{self.channel_id}",
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
                    f"whatsapp[{self.channel_id}] sidecar: {chunk.decode(errors='replace').rstrip()}",
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
                    logger.warning(
                        f"whatsapp[{self.channel_id}]: dropped non-JSON frame from sidecar",
                    )
                    continue
                await self._handle_sidecar_event(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"whatsapp[{self.channel_id}]: WS reader exited: {exc}",
            )

    async def _handle_sidecar_event(self, msg: dict) -> None:
        kind = msg.get("kind")
        if kind == "qr":
            qr = msg.get("qr")
            raw = msg.get("raw")
            # Forward both the rendered data-URL (web UI consumes ``qr``)
            # and the underlying string (CLI consumes ``raw`` and renders
            # it as a Unicode-block QR via mdp/qrterminal).
            if qr or raw:
                self._publish_auth_event({"kind": "qr", "qr": qr, "raw": raw})
        elif kind == "ready":
            self._publish_auth_event({"kind": "ready"})
            logger.info(f"whatsapp[{self.channel_id}]: paired and ready")
            await self._mark_linked()
            await self._note_self_identity(msg)
        elif kind == "incoming":
            sender_id = str(msg.get("sender_id") or "").strip()
            display_name = msg.get("display_name")
            text = msg.get("text") or ""
            files = self._files_from_frame(msg)
            if not sender_id or (not text and not files):
                return
            # Run inbound handling concurrently: nothing blocks a message any
            # more (one arriving mid-turn is folded into the running turn), and
            # the per-sender inbound lock keeps that decision atomic.
            asyncio.create_task(
                self._handle_inbound_safe(
                    sender_id, display_name, text, files=files or None,
                ),
                name=f"whatsapp-inbound:{self.channel_id}:{sender_id}",
            )
        elif kind == "group_joined":
            self._route_group_joined(msg)
        elif kind in ("group_metadata_result", "list_groups_result"):
            self._resolve_pending(msg)
        elif kind == "incoming_group":
            self._route_group_message(msg)
        elif kind == "disconnected":
            logged_out = bool(msg.get("logged_out"))
            self._publish_auth_event(
                {"kind": "disconnected", "logged_out": logged_out},
            )
            logger.info(
                f"whatsapp[{self.channel_id}]: sidecar reported disconnect "
                f"(logged_out={logged_out})",
            )
            # logged_out=True means the user revoked the linked-device
            # session from their phone — that's a remote unlink, not a
            # transient drop, so persist it and disable the channel.
            # logged_out=False is left to Baileys' own auto-reconnect.
            if logged_out:
                await self._mark_unlinked(
                    reason="logged_out_remote",
                    detail="WhatsApp linked-device session was logged out from your phone.",
                )
        elif kind == "send_ack":
            self._resolve_pending(msg)
        elif kind == "resolve_result":
            self._resolve_pending(msg)
        elif kind == "send_error":
            # Correlated failures belong to whoever is awaiting them; an
            # uncorrelated one (legacy fire-and-forget send) only gets logged.
            if not self._resolve_pending({**msg, "ok": False}):
                logger.warning(
                    f"whatsapp[{self.channel_id}]: send_error — "
                    f"sender={msg.get('sender_id')} err={msg.get('error')}",
                )
        elif kind == "error":
            logger.warning(
                f"whatsapp[{self.channel_id}]: sidecar error — {msg.get('error')}",
            )

    async def _note_self_identity(self, msg: dict) -> None:
        """Record which WhatsApp account this linked device speaks as.

        The room's echo filter has nothing else to work with. WhatsApp flags no
        message as bot-authored — every participant is somebody's account — so
        the mirrors we post ourselves come back looking exactly like a person
        talking, and only our own ids can tell them apart.

        Both JID forms are registered as alternates because a participant is
        reported as ``<digits>@s.whatsapp.net`` on one device and
        ``<opaque>@lid`` on another, and matching on one form alone lets our own
        post back in on the run where the forms disagree.
        """
        digits = "".join(ch for ch in str(msg.get("self_id") or "") if ch.isdigit())
        if not digits:
            logger.debug(
                f"whatsapp[{self.channel_id}]: sidecar reported no self id; "
                "a bound room cannot recognise our own posts",
            )
            return
        alt_ids = [f"{digits}{_PN_SUFFIX}"]
        lid = _normalize_lid(msg.get("self_lid"))
        if lid:
            alt_ids.insert(0, lid)
        await self._store_self_identity(
            user_id=digits,
            # WhatsApp has no usernames — a person is a number and a pushName —
            # so the number carries the roster handle as well.
            username=None,
            is_bot=False,
            mention=f"@{digits}",
            alt_ids=alt_ids,
            # The pushName, which is the only thing a group shows above our
            # messages — the number itself is not what anybody addresses.
            display_name=str(msg.get("self_name") or "").strip() or None,
        )

    def _route_group_message(self, msg: dict) -> None:
        """Spawn the room path for one ``incoming_group`` frame.

        Spawned rather than awaited so one room's message can't hold up the
        WebSocket reader that the DM path, the send acks and the pairing events
        all share.
        """
        chat_id = str(msg.get("chat_id") or "").strip()
        sender_id = str(msg.get("sender_id") or "").strip()
        text = msg.get("text") or ""
        files = self._files_from_frame(msg)
        if not chat_id or not sender_id or (not text and not files):
            return
        message_id = msg.get("message_id")
        timestamp = msg.get("timestamp")
        asyncio.create_task(
            self._handle_group_inbound_safe(
                chat_id=chat_id,
                chat_title=msg.get("chat_title"),
                chat_type=_GROUP_CHAT_TYPE,
                sender_id=sender_id,
                sender_username=None,
                sender_alt_ids=self._group_sender_alt_ids(
                    sender_id, msg.get("sender_alt_ids"),
                ),
                display_name=msg.get("display_name"),
                text=text,
                platform_message_id=str(message_id) if message_id else None,
                platform_message_date=(
                    float(timestamp) if isinstance(timestamp, (int, float)) else None
                ),
                # There is no bot flag to carry honestly: every WhatsApp account
                # is a person's, ours included, so the bot-author brake cannot
                # fire here and the id check is the whole story.
                sender_is_bot=False,
                mentioned=self._is_mentioned(msg),
                files=files or None,
            ),
            name=f"whatsapp-group-inbound:{self.channel_id}:{chat_id}",
        )

    def _files_from_frame(self, msg: dict) -> list[IncomingFile]:
        """Descriptors for a frame's spooled media files (shared helper)."""
        return files_from_sidecar_frame(msg)

    def _is_mentioned(self, msg: dict) -> bool:
        """Whether this group message pings or quotes our own account.

        WhatsApp carries both as annotations rather than in the text, so the
        sidecar extracts them and this compares them against every id we are
        known by — the ``@s.whatsapp.net`` form, the ``@lid`` form, and the bare
        digits — because which one a mention names depends on the sender's
        client.
        """
        identity = self.self_identity()
        own = [str(identity.get("user_id") or ""), *(identity.get("alt_ids") or ())]
        for jid in list(own):
            digits = self._derive_phone(jid)
            if digits:
                own.append(digits)
        reported = [
            *(msg.get("mentioned_ids") or []),
            msg.get("quoted_sender_id") or "",
        ]
        candidates: list[str] = []
        for value in reported:
            text = str(value or "").strip()
            if not text:
                continue
            candidates.append(text)
            digits = self._derive_phone(text)
            if digits:
                candidates.append(digits)
        from app.channels.groups.keys import ids_overlap

        return ids_overlap(own, candidates)

    def _route_group_joined(self, msg: dict) -> None:
        """Spawn the discovery path for a room this number was just added to."""
        chat_id = str(msg.get("chat_id") or "").strip()
        if not chat_id:
            return
        asyncio.create_task(
            self._note_group_joined(chat_id, msg.get("chat_title")),
            name=f"whatsapp-group-joined:{self.channel_id}:{chat_id}",
        )

    async def _note_group_joined(self, chat_id: str, chat_title: Any) -> None:
        from app.channels.groups.inbound import handle_group_joined

        await handle_group_joined(
            self,
            chat_id=chat_id,
            chat_title=chat_title,
            chat_type=_GROUP_CHAT_TYPE,
        )

    async def fetch_joined_groups(self) -> list[dict] | None:
        """Every WhatsApp group this number is in, via ``groupFetchAllParticipating``."""
        if self._ws is None:
            return None
        request_id, fut = self._new_request()
        try:
            async with self._send_lock:
                await self._ws.send(json.dumps({
                    "kind": "list_groups",
                    "request_id": request_id,
                }))
            reply = await asyncio.wait_for(fut, timeout=_ACK_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(
                f"whatsapp[{self.channel_id}]: the sidecar did not answer the "
                "group-list request",
            )
            return None
        finally:
            self._pending.pop(request_id, None)
        if not reply.get("ok"):
            logger.warning(
                f"whatsapp[{self.channel_id}]: group list failed: "
                f"{reply.get('error') or 'unknown error'}",
            )
            return None
        out: list[dict] = []
        for group in reply.get("groups") or []:
            chat_id = str((group or {}).get("id") or "").strip()
            if not chat_id:
                continue
            out.append({
                "platform_chat_id": chat_id,
                "title": group.get("name") or None,
                "chat_type": "group",
                "member_count": group.get("member_count"),
            })
        return out

    async def fetch_group_roster(self, chat_id: str) -> list[dict] | None:
        """The room's participants, via the sidecar's ``groupMetadata`` call.

        Both JID forms are kept per participant: WhatsApp reports the same
        person as ``<digits>@s.whatsapp.net`` on one device and ``<opaque>@lid``
        on another, and a member policy written against one form has to match
        the other.
        """
        if self._ws is None:
            return None
        request_id, fut = self._new_request()
        try:
            async with self._send_lock:
                await self._ws.send(json.dumps({
                    "kind": "group_metadata",
                    "chat_id": str(chat_id),
                    "request_id": request_id,
                }))
            reply = await asyncio.wait_for(fut, timeout=_ACK_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(
                f"whatsapp[{self.channel_id}]: the sidecar did not answer the "
                f"member-list request for {chat_id}",
            )
            return None
        finally:
            self._pending.pop(request_id, None)
        if not reply.get("ok"):
            logger.warning(
                f"whatsapp[{self.channel_id}]: member list for {chat_id} failed: "
                f"{reply.get('error') or 'unknown error'}",
            )
            return None
        out: list[dict] = []
        for participant in reply.get("participants") or []:
            member_id = str(participant.get("id") or "").strip()
            if not member_id:
                continue
            alts: list[str] = []
            lid = str(participant.get("lid") or "").strip()
            if lid and lid != member_id:
                alts.append(lid)
            for jid in [member_id, *alts]:
                digits = self._derive_phone(jid)
                if digits and digits not in alts:
                    alts.append(digits)
            admin = participant.get("admin")
            out.append({
                "member_id": member_id,
                "alt_ids": alts,
                "display_name": self._derive_phone(member_id) or None,
                "username": None,
                "is_bot": False,
                "role": "admin" if admin else "member",
            })
        return out

    def _group_sender_alt_ids(
        self, sender_id: str, reported: Any,
    ) -> list[str]:
        """The other ids this participant may be recognised by.

        Beyond the JID forms the sidecar saw, the bare phone digits are added:
        a JID is not something anyone can look up, whereas the number is on the
        contact card, so ``whatsapp:<digits>`` is what the group's User accounts
        are documented to take and what an operator will actually paste in.
        """
        out: list[str] = []
        for value in reported or []:
            text = str(value or "").strip()
            if text and text != sender_id and text not in out:
                out.append(text)
        for jid in [sender_id, *out]:
            digits = self._derive_phone(jid)
            if digits and digits not in out:
                out.append(digits)
        return out

    async def _handle_group_inbound_safe(self, **kwargs: Any) -> None:
        try:
            await self._handle_group_inbound(**kwargs)
        except Exception:  # noqa: BLE001
            logger.exception("whatsapp: group inbound handler failed")

    async def _upsert_sender(
        self, sender_id: str, display_name: str | None,
    ) -> dict:
        """Upsert the sender, adopting a cold-contact row when the ``@lid`` matches.

        Multi-device WhatsApp may deliver this person's messages from an opaque
        ``<id>@lid`` JID even though we first reached them at
        ``<digits>@s.whatsapp.net`` (a direct-send cold contact records the
        ``@lid`` alias precisely so we can recognise this). Creating a fresh row
        would give the same human a second contact and a second conversation,
        losing the history of what we already sent them — so re-point the
        existing row at the identity they actually write from.
        """
        sid = (sender_id or "").strip()
        if sid.endswith(_LID_SUFFIX):
            # Look up the alias exactly as it is stored — full JID form, the
            # same shape ``_normalize_lid`` wrote at registration.
            existing = await self.storage.get_sender_by_wa_lid(self.channel_id, sid)
            if existing and existing["sender_id"] != sid:
                logger.info(
                    f"whatsapp[{self.channel_id}]: adopting cold-contact row "
                    f"{existing['sender_id']} -> {sid} (lid match)",
                )
                updated = await self.storage.update_sender(
                    existing["id"], sender_id=sid,
                    **({"display_name": display_name} if display_name else {}),
                )
                if updated:
                    return updated
        return await super()._upsert_sender(sender_id, display_name)

    async def _handle_inbound_safe(
        self, sender_id: str, display_name: str | None, text: str,
        files: Any = None,
    ) -> None:
        try:
            await self._handle_inbound(sender_id, display_name, text, files=files)
        except Exception:  # noqa: BLE001
            logger.exception("whatsapp: inbound handler failed")

    async def _teardown(self) -> None:
        ws = self._ws
        proc = self._proc
        self._ws = None
        self._proc = None
        # Fail anyone parked on a round trip: the sidecar is going away, so no
        # ack is ever coming and waiting out the timeout would be pointless.
        pending = list(self._pending.items())
        self._pending.clear()
        for _, fut in pending:
            if not fut.done():
                fut.set_result({"ok": False, "error": "sidecar shut down"})
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
        # Drop auth subscribers — they'll get a fresh queue on the next start.
        self._auth_subscribers.clear()
        self._latest_auth_event = None
