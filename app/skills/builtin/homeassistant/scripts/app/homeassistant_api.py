"""HTTP + WebSocket clients for a Home Assistant instance.

Auth is a Long-Lived Access Token (LLAT): a Bearer header for REST, and the
`auth` handshake message for the WebSocket. No OAuth / refresh logic.
"""
from __future__ import annotations

import json
import ssl
from typing import Any, Optional

import requests

from . import auth, config
from .errors import HaError


def _silence_insecure_warning() -> None:
    try:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass


class HaRestClient:
    """Lazy REST client over the Home Assistant /api. Use as a context manager."""

    def __init__(self) -> None:
        self._base = config.require_url().rstrip("/")
        self._session: Optional[requests.Session] = None

    def __enter__(self) -> "HaRestClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> None:
        s = requests.Session()
        s.headers.update({"Content-Type": "application/json"})
        s.verify = config.HA_VERIFY_SSL
        if not config.HA_VERIFY_SSL:
            _silence_insecure_warning()
        self._session = s

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
        self._session = None

    def _request(self, method: str, path: str, *, json_body: Any = None, _retry: bool = True) -> Any:
        if self._session is None:
            self.connect()
        url = f"{self._base}{path}"
        token = auth.get_access_token()
        try:
            resp = self._session.request(
                method,
                url,
                json=json_body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=config.HTTP_TIMEOUT,
            )
        except requests.exceptions.SSLError as e:
            raise HaError(
                f"SSL error connecting to {url}: {e}. "
                "If your instance uses a self-signed certificate, set HA_VERIFY_SSL=false in scripts/.env."
            ) from e
        except requests.exceptions.RequestException as e:
            raise HaError(
                f"Failed to reach Home Assistant at {self._base}: {e}. "
                "Verify HA_URL is correct and the instance is reachable from this machine."
            ) from e
        if resp.status_code in (401, 403):
            # In OAuth mode the access token may have just expired; refresh once and retry.
            if _retry and auth.auth_mode() == "oauth":
                auth.get_access_token(force_refresh=True)
                return self._request(method, path, json_body=json_body, _retry=False)
            raise HaError(
                "Authentication failed - the token may be wrong, expired, or revoked. "
                "Re-run `link`, or set a valid HA_TOKEN.",
                status=resp.status_code,
            )
        if resp.status_code >= 400:
            raise HaError(
                f"Home Assistant returned HTTP {resp.status_code} for {path}: {resp.text[:300]}",
                status=resp.status_code,
            )
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def get_config(self) -> dict:
        return self._request("GET", "/api/config")

    def get_states(self) -> list[dict]:
        return self._request("GET", "/api/states")

    def get_state(self, entity_id: str) -> dict:
        try:
            return self._request("GET", f"/api/states/{entity_id}")
        except HaError as e:
            if e.status == 404:
                raise HaError(
                    f"Entity not found: {entity_id}. Use `list-entities` to see available entities."
                ) from e
            raise

    def call_service(self, domain: str, service: str, data: dict | None = None) -> Any:
        return self._request("POST", f"/api/services/{domain}/{service}", json_body=data or {})


class HaWebSocketClient:
    """Blocking WebSocket client for the HA event stream.

    Uses websocket-client's create_connection (not WebSocketApp) so the auth
    handshake and command/result correlation are deterministic and synchronous.
    """

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        verify_ssl: bool | None = None,
    ) -> None:
        self._url = url or config.ws_url()
        self._token = token
        self._verify_ssl = config.HA_VERIFY_SSL if verify_ssl is None else verify_ssl
        self._ws = None
        self._id = 0

    def connect(self, timeout: int | None = None) -> None:
        import websocket  # websocket-client

        if self._token is None:
            # Fetch a token that will outlast this connection's reconnect window, so
            # an OAuth access token does not expire mid-socket (LLAT mode ignores this).
            self._token = auth.get_access_token(min_validity=config.RECONNECT_MAX_SECONDS + 120)
        sslopt = None
        if not self._verify_ssl:
            sslopt = {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}
        self._ws = websocket.create_connection(
            self._url,
            timeout=timeout or config.HTTP_TIMEOUT,
            sslopt=sslopt,
        )

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _send(self, payload: dict) -> None:
        self._ws.send(json.dumps(payload))

    def _recv_json(self) -> dict:
        raw = self._ws.recv()
        if raw is None or raw == "":
            raise HaError("WebSocket closed by server")
        return json.loads(raw)

    def authenticate(self) -> None:
        msg = self._recv_json()
        # Spec: server greets with auth_required. Be tolerant if a proxy reorders it.
        self._send({"type": "auth", "access_token": self._token})
        result = self._recv_json()
        if result.get("type") == "auth_required":
            # We answered before the greeting arrived; send again after it.
            self._send({"type": "auth", "access_token": self._token})
            result = self._recv_json()
        if result.get("type") == "auth_invalid":
            raise HaError(
                f"WebSocket auth rejected: {result.get('message', 'invalid token')}. Check HA_TOKEN."
            )
        if result.get("type") != "auth_ok":
            raise HaError(f"Unexpected auth response from Home Assistant: {result}")

    def subscribe_events(self, event_type: str = "state_changed") -> int:
        msg_id = self._next_id()
        self._send({"id": msg_id, "type": "subscribe_events", "event_type": event_type})
        while True:
            msg = self._recv_json()
            if msg.get("type") == "result" and msg.get("id") == msg_id:
                if not msg.get("success", False):
                    raise HaError(f"subscribe_events failed: {msg.get('error')}")
                return msg_id
            # Tolerate any stray messages before the ack.

    def ping(self) -> None:
        self._send({"id": self._next_id(), "type": "ping"})

    def recv(self, timeout: float | None = None) -> dict:
        """Receive one parsed message. On socket timeout, websocket-client raises
        WebSocketTimeoutException (caught by the listener to send a keepalive)."""
        if timeout is not None:
            self._ws.settimeout(timeout)
        raw = self._ws.recv()
        if raw is None or raw == "":
            raise HaError("WebSocket closed by server")
        return json.loads(raw)

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        self._ws = None
