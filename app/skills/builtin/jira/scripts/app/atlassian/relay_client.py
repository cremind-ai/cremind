"""WebSocket client for the cremind-connect relay (Atlassian variant).

Connects to {ws_url}?account=<key>&resources=<...> presenting a short-lived
relay-session JWT in the ``X-Cremind-Session`` header. Atlassian issues no OpenID
id_token, so — unlike the Google relay client — the bootstrap credential is a
relay-session minted by the backend (from the user's access token via /me). A
fresh session is fetched on every (re)connect.

The relay replies with a `hello` and thereafter pushes `resync` nudges; each nudge
triggers a local incremental pull. A nudge may additively carry the triggering Jira
webhook event type (`event`) and issue `key` for classification — but never issue
*content* (the relay still sees no Jira data).
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Optional
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
        session_provider: Callable[[], str],
        on_resync: Callable[[dict[str, Any]], None],
        logger: Optional[logging.Logger] = None,
    ):
        self.ws_url = ws_url
        self.account_key = account_key
        self.resources = resources
        self.session_provider = session_provider
        self.on_resync = on_resync
        self.log = logger or logging.getLogger("relay")
        self._stop = threading.Event()
        self._app = None  # current WebSocketApp; set while a connection is live

    def stop(self) -> None:
        self._stop.set()
        app = self._app
        if app is not None:
            try:
                app.close()
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
            self.log.info("relay session established")
        elif mtype == "resync":
            # The backend may additively carry the triggering Jira webhook event type
            # and issue key; absent (older backend) → None, and the listener falls back
            # to a plain cursor pull.
            meta = {"source": msg.get("source", ""), "event": msg.get("event"), "key": msg.get("key")}
            self.log.info("resync nudge received (source=%s event=%s key=%s)", meta["source"], meta["event"], meta["key"])
            try:
                self.on_resync(meta)
            except Exception:
                self.log.exception("on_resync handler failed")
        elif mtype == "error":
            self.log.warning("relay error: %s", msg.get("code"))

    def run_forever(self) -> None:
        import websocket  # websocket-client

        backoff = 5
        while not self._stop.is_set():
            try:
                session = self.session_provider()
            except Exception as e:
                self.log.warning("could not mint relay session (%s); retrying in %ss", e, backoff)
                self._sleep(backoff)
                backoff = min(backoff * 2, 120)
                continue

            app = websocket.WebSocketApp(
                self._url(),
                header=[f"X-Cremind-Session: {session}"],
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
