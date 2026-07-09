"""WebSocket client for the cremind-connect relay.

Connects to {ws_url}?account=<key>&resources=<...> presenting a fresh Google ID
token (Authorization: Bearer) to prove account control. The relay replies with a
`hello` (carrying a relay-session token) and thereafter pushes `resync` nudges.
Each nudge triggers a local incremental sync — the relay sends NO Google data.

Reconnects with backoff; a fresh ID token is minted on every (re)connect. A
protocol-level ping keeps NAT mappings open.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Optional
from urllib.parse import quote

PING_INTERVAL = 240  # seconds; keep NAT open without spamming
PING_TIMEOUT = 30


class RelayClient:
    def __init__(
        self,
        *,
        ws_url: str,
        account_key: str,
        resources: list[str],
        id_token_provider: Callable[[], str],
        on_resync: Callable[[str], None],
        logger: Optional[logging.Logger] = None,
    ):
        self.ws_url = ws_url
        self.account_key = account_key
        self.resources = resources
        self.id_token_provider = id_token_provider
        self.on_resync = on_resync
        self.log = logger or logging.getLogger("relay")
        self._stop = threading.Event()
        self._session: str | None = None
        self._app = None  # current WebSocketApp; set while a connection is live

    def stop(self) -> None:
        self._stop.set()
        app = self._app
        if app is not None:
            try:
                app.close()  # unblock a live app.run_forever() immediately
            except Exception:
                pass

    def _url(self) -> str:
        res = quote(",".join(self.resources))
        return f"{self.ws_url}?account={quote(self.account_key)}&resources={res}"

    def _on_message(self, _ws, message: str) -> None:
        try:
            msg = json.loads(message)
        except (ValueError, TypeError):
            return
        mtype = msg.get("type")
        if mtype == "hello":
            self._session = msg.get("session")
            self.log.info("relay session established")
        elif mtype == "resync":
            source = msg.get("source", "")
            self.log.info("resync nudge received (source=%s)", source)
            try:
                self.on_resync(source)
            except Exception:
                self.log.exception("on_resync handler failed")
        elif mtype == "error":
            self.log.warning("relay error: %s", msg.get("code"))

    def run_forever(self) -> None:
        import websocket  # websocket-client

        backoff = 5
        while not self._stop.is_set():
            try:
                id_token = self.id_token_provider()
            except Exception as e:
                self.log.warning("could not mint id_token (%s); retrying in %ss", e, backoff)
                self._sleep(backoff)
                backoff = min(backoff * 2, 120)
                continue

            app = websocket.WebSocketApp(
                self._url(),
                header=[f"Authorization: Bearer {id_token}"],
                on_message=self._on_message,
                on_open=lambda _ws: self.log.info("connected to relay"),
                on_close=lambda _ws, *a: self.log.info("relay connection closed"),
                on_error=lambda _ws, err: self.log.warning("relay ws error: %s", err),
            )
            self._app = app
            try:
                app.run_forever(ping_interval=PING_INTERVAL, ping_timeout=PING_TIMEOUT)
            except Exception as e:
                self.log.warning("relay run_forever error: %s", e)
            finally:
                self._app = None

            if self._stop.is_set():
                break
            self.log.info("reconnecting to relay in %ss", backoff)
            self._sleep(backoff)
            backoff = min(backoff * 2, 120)

    def _sleep(self, seconds: int) -> None:
        self._stop.wait(timeout=seconds)
