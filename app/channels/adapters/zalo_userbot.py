"""Zalo personal-account adapter via a ``zca-js`` Node sidecar.

Zalo's personal (non-bot) account has no first-party API; the practical
integration — ported from OpenClaw's ``extensions/zalouser`` — drives a
logged-in Zalo Web session through the unofficial
:pypi-js:`zca-js` library, paired via QR scan. This adapter spawns a Node
sidecar (``app/channels/sidecars/zalo/``) that owns the ``zca-js`` session and
bridges its events to this Python adapter over a localhost WebSocket, exactly
like the WhatsApp/Baileys sidecar.

⚠️ Unofficial: automating a personal Zalo account may violate Zalo's terms and
risk account suspension. The catalog instructions warn about this; the official
Bot API transport is :class:`app.channels.adapters.zalo.ZaloBotAdapter`.

Lifecycle:
    1. Adapter spawns ``node index.js --profile … --channel-id … --working-dir …``.
    2. Sidecar prints ``WS_PORT=<port>``; the adapter connects.
    3. Sidecar emits ``{kind: "qr"|"ready"|"incoming"|"disconnected"|...}``;
       ``incoming`` fans into :meth:`BaseChannelAdapter._handle_inbound`.
    4. Outgoing replies / typing are pushed as ``{kind: "send"|"typing"}`` frames.

Session credentials (cookie / imei / userAgent) live at
``<working_dir>/<profile>/zalo/<channel_id>/credentials.json`` so a paired
session survives restarts.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from app.channels.base import BaseChannelAdapter
from app.channels.exceptions import ChannelAuthError, ChannelNotImplemented
from app.channels.sidecars.bootstrap import is_install_fresh
from app.config.settings import BaseConfig
from app.utils.logger import logger

_SIDECAR_DIR = Path(__file__).resolve().parents[1] / "sidecars" / "zalo"
_SIDECAR_INDEX = _SIDECAR_DIR / "index.js"
_PORT_HEADER = "WS_PORT="


class ZaloUserbotAdapter(BaseChannelAdapter):
    """In-process Zalo personal adapter that delegates platform IO to a Node sidecar."""

    def __init__(self, channel: dict, storage: Any) -> None:
        super().__init__(channel, storage)
        self._proc: asyncio.subprocess.Process | None = None
        self._ws: Any = None
        self._send_lock = asyncio.Lock()

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
            await self._ws.send(json.dumps({
                "kind": "send", "sender_id": sender_id, "text": text,
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
            await self._mark_linked()
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
