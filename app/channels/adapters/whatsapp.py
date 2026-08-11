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
    3. Sidecar emits ``{kind: "qr"|"ready"|"incoming"|"disconnected"|...}``
       JSON frames; the adapter consumes them. ``incoming`` fans out into
       :meth:`BaseChannelAdapter._handle_inbound`.
    4. Outgoing replies and the typing indicator are pushed to the
       sidecar as ``{kind: "send"|"typing"}`` frames.

Auth-state on disk lives at
``<working_dir>/<profile>/whatsapp/<channel_id>/session/`` so a paired
session survives restarts. Deleting that directory forces a fresh
QR-scan pairing on next start.

Prerequisites (surfaced as :class:`ChannelNotImplemented` if missing):
    - Node 18+ on PATH.
    - ``npm install`` has been run once inside
      ``app/channels/sidecars/whatsapp/`` so ``node_modules/`` exists.

Pairing UX:
    The latest QR (data URL) is published on a per-adapter pub-sub queue
    that the API's ``GET /api/channels/{id}/qr`` SSE endpoint subscribes
    to. The web UI's Channels page renders the QR until the sidecar
    reports ``ready``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from app.channels.base import BaseChannelAdapter
from app.channels.exceptions import ChannelAuthError, ChannelNotImplemented
from app.channels.sidecars.bootstrap import is_install_fresh
from app.config.settings import BaseConfig
from app.utils.logger import logger


_SIDECAR_DIR = Path(__file__).resolve().parents[1] / "sidecars" / "whatsapp"
_SIDECAR_INDEX = _SIDECAR_DIR / "index.js"
_PORT_HEADER = "WS_PORT="

# How long to wait for the sidecar's correlated answer to a send / resolve.
# Generous: Baileys may be mid-reconnect, and a false "timed out" would cost a
# direct-send recipient their history entry (see ``send_strict``).
_ACK_TIMEOUT = 20.0

_PN_SUFFIX = "@s.whatsapp.net"
_LID_SUFFIX = "@lid"


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
        self._check_prereqs()
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

    def _check_prereqs(self) -> None:
        if shutil.which("node") is None:
            raise ChannelNotImplemented(
                "Node.js is not installed or not on PATH. Install Node 18+ "
                "to use the WhatsApp channel.",
            )
        if not _SIDECAR_INDEX.exists():
            raise ChannelNotImplemented(
                f"WhatsApp sidecar source missing: {_SIDECAR_INDEX}",
            )
        fresh, reason = is_install_fresh(_SIDECAR_DIR)
        if not fresh:
            raise ChannelNotImplemented(
                f"WhatsApp sidecar dependencies are not ready: {reason}. "
                f"Restart the server (startup auto-installs) or run "
                f"`npm ci` manually in {_SIDECAR_DIR}.",
            )

    def _resolve_working_dir(self) -> str:
        # ``BaseConfig.CREMIND_SYSTEM_DIR`` already expands ``~`` and
        # normalises path separators (the codebase's canonical accessor —
        # don't re-roll). The sidecar also re-expands ``~`` defensively in
        # case a non-canonical value ever flows through.
        return BaseConfig.CREMIND_SYSTEM_DIR

    async def _spawn_sidecar(self) -> None:
        try:
            import websockets  # type: ignore  # noqa: F401
        except ImportError as exc:
            raise ChannelNotImplemented(
                "Python `websockets` library is missing. Add `websockets` "
                "to ``pyproject.toml`` and reinstall.",
            ) from exc

        working_dir = self._resolve_working_dir()
        cmd = [
            "node", str(_SIDECAR_INDEX),
            "--profile", self.profile,
            "--channel-id", self.channel_id,
            "--working-dir", working_dir,
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
        elif kind == "incoming":
            sender_id = str(msg.get("sender_id") or "").strip()
            display_name = msg.get("display_name")
            text = msg.get("text") or ""
            if not sender_id or not text:
                return
            # Run inbound handling concurrently so per-sender serialisation
            # in ``_inflight`` is the only blocking layer.
            asyncio.create_task(
                self._handle_inbound_safe(sender_id, display_name, text),
                name=f"whatsapp-inbound:{self.channel_id}:{sender_id}",
            )
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
    ) -> None:
        try:
            await self._handle_inbound(sender_id, display_name, text)
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
