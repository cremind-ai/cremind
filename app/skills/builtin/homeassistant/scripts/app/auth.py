"""Authentication for the Home Assistant skill — two modes, chosen automatically.

- **LLAT**: if `HA_TOKEN` (a Long-Lived Access Token) is set, it is used directly
  as the bearer token. No browser flow, no refresh, ~10-year validity.
- **OAuth 2.0 (IndieAuth)**: if `HA_TOKEN` is NOT set, `link` runs a local browser
  flow against the instance. Home Assistant's auth is IndieAuth-based: there is no
  client_secret, and a loopback `client_id` that equals the `redirect_uri` is
  accepted without HA fetching it. The 30-minute access token is refreshed
  automatically with the refresh token. The SAME `client_id` must be sent on every
  refresh, so it is stored alongside the tokens.

Tokens live only on this machine (`.ha_token.json`, gitignored). No cloud relay.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from . import config
from .errors import AuthError


class TokenStore:
    """Local, atomic JSON token store (gitignored)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def auth_mode() -> str:
    return "llat" if config.HA_TOKEN else "oauth"


def is_authenticated() -> bool:
    if config.HA_TOKEN:
        return True
    return TokenStore(config.TOKEN_PATH).load() is not None


def status() -> dict[str, Any]:
    if config.HA_TOKEN:
        return {"auth": "llat", "authenticated": True}
    data = TokenStore(config.TOKEN_PATH).load()
    return {
        "auth": "oauth",
        "authenticated": data is not None,
        "linked_url": (data or {}).get("ha_url"),
    }


# --------------------------------------------------------------------------- #
# Token retrieval (used by the REST + WebSocket clients)
# --------------------------------------------------------------------------- #

def get_access_token(*, force_refresh: bool = False, min_validity: int = 60) -> str:
    """Return a bearer token valid for at least `min_validity` seconds.

    LLAT mode returns `HA_TOKEN` unchanged. OAuth mode returns the stored access
    token, refreshing first if it is missing, expired, or would expire within
    `min_validity` seconds (so a long-lived WebSocket can outlive its window).
    """
    if config.HA_TOKEN:
        return config.HA_TOKEN

    store = TokenStore(config.TOKEN_PATH)
    data = store.load()
    if not data:
        raise AuthError(
            "Home Assistant is not linked. Either set HA_TOKEN in scripts/.env (a Long-Lived "
            "Access Token from Profile -> Security), or run: uv run scripts/__main__.py link"
        )
    remaining = float(data.get("expires_at", 0)) - time.time()
    if force_refresh or not data.get("access_token") or remaining < min_validity:
        data = _refresh(store, data)
    return data["access_token"]


def _refresh(store: TokenStore, data: dict[str, Any]) -> dict[str, Any]:
    ha_url = (data.get("ha_url") or config.HA_URL).rstrip("/")
    client_id = data.get("client_id")
    refresh_token = data.get("refresh_token")
    if not client_id or not refresh_token:
        raise AuthError("Stored token is incomplete; re-run: uv run scripts/__main__.py link")
    try:
        resp = requests.post(
            f"{ha_url}/auth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            timeout=config.HTTP_TIMEOUT,
            verify=config.HA_VERIFY_SSL,
        )
    except requests.exceptions.RequestException as e:
        raise AuthError(f"Failed to reach Home Assistant to refresh the access token: {e}") from e
    if resp.status_code != 200:
        raise AuthError(
            f"Token refresh was rejected (HTTP {resp.status_code}). The session may have been "
            "revoked or expired. Re-run: uv run scripts/__main__.py link"
        )
    tok = resp.json()
    # Refresh returns a new access_token only (the refresh_token is reused).
    data["access_token"] = tok["access_token"]
    data["expires_at"] = time.time() + int(tok.get("expires_in", 1800))
    store.save(data)
    return data


# --------------------------------------------------------------------------- #
# Loopback OAuth (IndieAuth) browser flow
# --------------------------------------------------------------------------- #

class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        params = parse_qs(urlparse(self.path).query)
        if "code" in params or "error" in params:
            self.server.captured = {k: v[0] for k, v in params.items()}  # type: ignore[attr-defined]
            self.server.captured_event.set()  # type: ignore[attr-defined]
            self._respond(
                200,
                "<html><body style='font-family:sans-serif'>"
                "<h3>Home Assistant authorization complete.</h3>"
                "<p>You can close this tab and return to the terminal.</p></body></html>",
            )
        else:
            self._respond(404, "<html><body>Waiting for authorization...</body></html>")

    def _respond(self, code: int, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    def log_message(self, *args) -> None:  # silence default request logging
        pass


def link(*, open_browser: bool = True, timeout: int = 300) -> dict[str, Any]:
    """Run the loopback OAuth2/IndieAuth consent flow and persist tokens locally."""
    ha_url = config.require_url()

    if config.HA_TOKEN:
        return {
            "ok": True,
            "auth": "llat",
            "note": "HA_TOKEN is set, so the skill already authenticates with that Long-Lived "
            "Access Token. OAuth linking is unnecessary; unset HA_TOKEN to use OAuth instead.",
        }

    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.captured = None  # type: ignore[attr-defined]
    server.captured_event = threading.Event()  # type: ignore[attr-defined]
    port = server.server_address[1]
    # client_id == redirect_uri (same scheme+host+port) → HA accepts it without
    # fetching the client_id page. The exact string is reused on every refresh.
    client_id = f"http://127.0.0.1:{port}/"
    state = secrets.token_urlsafe(24)
    authorize_url = f"{ha_url}/auth/authorize?" + urlencode(
        {"client_id": client_id, "redirect_uri": client_id, "state": state, "response_type": "code"}
    )

    worker = threading.Thread(target=server.serve_forever, name="ha-oauth-loopback", daemon=True)
    worker.start()
    try:
        print(
            "Opening your browser to authorize Cremind in Home Assistant.\n"
            f"If it does not open automatically, visit:\n  {authorize_url}\n",
            file=sys.stderr,
            flush=True,
        )
        if open_browser:
            try:
                webbrowser.open(authorize_url)
            except Exception:
                pass
        deadline = time.monotonic() + timeout
        try:
            while not server.captured_event.is_set():  # type: ignore[attr-defined]
                if time.monotonic() > deadline:
                    raise AuthError(
                        f"Timed out after {timeout}s waiting for Home Assistant authorization. "
                        "Re-run `link` and complete the browser consent."
                    )
                server.captured_event.wait(0.5)  # type: ignore[attr-defined]
        except KeyboardInterrupt:
            raise AuthError("Linking cancelled (Ctrl+C) before authorization completed.")
    finally:
        server.shutdown()
        server.server_close()

    captured = server.captured or {}  # type: ignore[attr-defined]
    if "error" in captured:
        raise AuthError(f"Home Assistant authorization was denied: {captured.get('error')}")
    if captured.get("state") != state:
        raise AuthError("OAuth state mismatch (possible CSRF); aborting. Re-run `link`.")
    code = captured.get("code")
    if not code:
        raise AuthError("No authorization code was returned by Home Assistant.")

    tokens = _exchange_code(ha_url, client_id, code)
    data = {
        "ha_url": ha_url,
        "client_id": client_id,
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at": time.time() + int(tokens.get("expires_in", 1800)),
    }
    TokenStore(config.TOKEN_PATH).save(data)
    return {"ok": True, "auth": "oauth", "ha_url": ha_url, "linked": True}


def _exchange_code(ha_url: str, client_id: str, code: str) -> dict[str, Any]:
    try:
        resp = requests.post(
            f"{ha_url}/auth/token",
            data={"grant_type": "authorization_code", "code": code, "client_id": client_id},
            timeout=config.HTTP_TIMEOUT,
            verify=config.HA_VERIFY_SSL,
        )
    except requests.exceptions.RequestException as e:
        raise AuthError(f"Failed to reach Home Assistant for the token exchange: {e}") from e
    if resp.status_code != 200:
        raise AuthError(f"Token exchange failed (HTTP {resp.status_code}): {resp.text[:300]}")
    tok = resp.json()
    if "access_token" not in tok or "refresh_token" not in tok:
        raise AuthError(f"Token response from Home Assistant was missing fields: {tok}")
    return tok


def unlink() -> dict[str, Any]:
    store = TokenStore(config.TOKEN_PATH)
    data = store.load()
    revoked = False
    if data and data.get("refresh_token"):
        ha_url = (data.get("ha_url") or config.HA_URL).rstrip("/")
        try:
            requests.post(
                f"{ha_url}/auth/token",
                data={"token": data["refresh_token"], "action": "revoke"},
                timeout=config.HTTP_TIMEOUT,
                verify=config.HA_VERIFY_SSL,
            )
            revoked = True
        except requests.exceptions.RequestException:
            revoked = False
    store.clear()
    return {"ok": True, "unlinked": True, "revoked": revoked}
