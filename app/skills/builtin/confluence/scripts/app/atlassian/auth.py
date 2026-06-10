"""Atlassian OAuth 2.0 (3LO) for the cremind skills — backend-mediated.

Atlassian Cloud 3LO is a CONFIDENTIAL flow: no public PKCE, and the client_secret
is REQUIRED at the token exchange. So unlike the Google skills (which exchange
loopback + PKCE directly with Google), we cannot complete the flow on the client.
Instead:

  1. The auth code is captured on the SAME persistent loopback listener the Google
     skills use (``cremind serve`` writes the redirect query to
     ``<CREMIND_SYSTEM_DIR>/oauth_inbox/<state>.txt``). Atlassian requires a FIXED,
     pre-registered callback URL, so the loopback port must be set and must match
     ``http://127.0.0.1:<port>/`` registered in the Atlassian developer console.
  2. The code is POSTed to cremind-connect (``/oauth/atlassian/exchange``), which
     holds the client_secret and performs the exchange, returning the tokens.
  3. Tokens are stored locally on this machine (``scripts/.atlassian_token.json``);
     cremind-connect stores nothing. Refresh is also proxied through the backend
     (it needs the secret), but the tokens still live only here. Atlassian rotates
     refresh tokens, so the newest one is persisted after every refresh.

The relay subscription cannot use a Google-style id_token (Atlassian issues none),
so ``fresh_relay_session`` mints a short-lived relay-session via the backend.

No third-party deps: HTTP is plain ``urllib``.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from .account_key import account_key_for

AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
ACCESSIBLE_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
ME_URL = "https://api.atlassian.com/me"

# The OAuth ``state`` becomes an inbox filename; accept only this charset/length
# (guards against path traversal via a crafted ``state`` in a pasted URL).
# Mirrors app/api/oauth_loopback.py's _STATE_RE.
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


class AuthError(RuntimeError):
    pass


class TokenStore:
    """Local, atomic JSON token store (gitignored)."""

    def __init__(self, path: Path):
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


# --- low-level HTTP (stdlib) ---

def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> Any:
    data = None
    hdrs = {"Accept": "application/json", "User-Agent": "cremind-skill/1.0"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace") if e.fp else ""
        raise AuthError(f"{method} {url} failed ({e.code}): {detail[:300]}") from e
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        raise AuthError(f"{method} {url} failed: {e}") from e


# --- backend loopback callback capture (shared with the Google skills) ---

def _oauth_inbox_dir() -> Path | None:
    system_dir = os.environ.get("CREMIND_SYSTEM_DIR", "").strip()
    if not system_dir:
        return None
    return Path(system_dir) / "oauth_inbox"


def _backend_listener_available(port: int) -> bool:
    if port <= 0 or _oauth_inbox_dir() is None:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def _await_oauth_callback(state: str, *, timeout: float = 600.0) -> str:
    inbox = _oauth_inbox_dir()
    if inbox is None:  # pragma: no cover - guarded by _backend_listener_available
        raise AuthError("CREMIND_SYSTEM_DIR is not set; cannot receive the OAuth callback.")
    path = inbox / f"{state}.txt"
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if path.exists():
                try:
                    query = path.read_text(encoding="utf-8")
                finally:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                parsed = parse_qs(query)
                if "error" in parsed:
                    raise AuthError("Atlassian consent was denied or returned an error.")
                code = (parsed.get("code") or [""])[0]
                if not code:
                    raise AuthError("OAuth callback contained no authorization code.")
                return code
            time.sleep(0.5)
    except KeyboardInterrupt:
        raise AuthError("Linking cancelled (Ctrl+C) before consent completed.")
    raise AuthError(
        "Timed out waiting for Atlassian consent (no callback received within "
        f"{int(timeout)}s). Re-run link and complete the browser consent."
    )


def submit_callback(response: str) -> dict[str, Any]:
    """Hand a manually-captured OAuth redirect back to the waiting ``link``.

    On remote/headless deployments (Kubernetes, SSH, …) the browser is redirected
    to the registered loopback callback (``http://127.0.0.1:<port>/``) which it
    can't reach, but the URL still carries a valid ``code`` + ``state``. The user
    copies it; this writes the raw query into the same per-state inbox file the
    backend loopback listener would have written
    (``<CREMIND_SYSTEM_DIR>/oauth_inbox/<state>.txt``), so the still-running
    ``link`` reads the code and completes the backend-mediated exchange.

    Crucially, the ``redirect_uri`` sent at exchange is ``link``'s OWN local value
    (still ``http://127.0.0.1:<port>/``, the value registered in the Atlassian
    app) — it is never derived from the pasted URL — so the exchange matches the
    registration regardless of which host the user actually pasted.

    ``response`` may be a full redirect URL or a bare ``code=...&state=...`` query.
    """
    raw = (response or "").strip()
    if not raw:
        raise AuthError("Empty OAuth response; paste the full URL Atlassian redirected you to.")
    query = urlparse(raw).query
    if not query:
        query = raw[1:] if raw.startswith("?") else raw
    params = parse_qs(query)
    if "error" in params:
        raise AuthError("Atlassian consent was denied or returned an error.")
    state = (params.get("state") or [""])[0]
    if not _STATE_RE.match(state):
        raise AuthError(
            "Could not find a valid 'state' in the pasted response. Paste the "
            "entire URL from your browser's address bar (it contains "
            "state=... and code=...)."
        )
    if "code" not in params:
        raise AuthError("The pasted response has no 'code'; paste the full redirect URL after approving.")
    inbox = _oauth_inbox_dir()
    if inbox is None:
        raise AuthError("CREMIND_SYSTEM_DIR is not set; cannot deliver the OAuth response.")
    inbox.mkdir(parents=True, exist_ok=True)
    dst = inbox / f"{state}.txt"
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.write_text(query, encoding="utf-8")
    os.replace(tmp, dst)
    return {"submitted": True, "state": state}


# --- accessible resources / identity ---

def accessible_resources(access_token: str) -> list[dict[str, Any]]:
    return _http_json("GET", ACCESSIBLE_RESOURCES_URL, headers={"Authorization": f"Bearer {access_token}"}) or []


def me(access_token: str) -> dict[str, Any]:
    return _http_json("GET", ME_URL, headers={"Authorization": f"Bearer {access_token}"}) or {}


def pick_site(resources: list[dict[str, Any]], site_url_hint: str = "") -> tuple[str, str]:
    """Return (cloud_id, site_url). Match the hint if given, else take the first."""
    if not resources:
        raise AuthError(
            "This Atlassian account has no accessible sites for the granted scopes. "
            "Confirm the user approved access to a site."
        )
    if site_url_hint:
        hint = site_url_hint.rstrip("/")
        for r in resources:
            if (r.get("url", "").rstrip("/")) == hint:
                return r.get("id", ""), r.get("url", "")
        raise AuthError(f"No accessible Atlassian site matches {site_url_hint!r}.")
    first = resources[0]
    return first.get("id", ""), first.get("url", "")


# --- token response handling ---

def _apply_token_response(data: dict[str, Any], resp: dict[str, Any]) -> None:
    access = resp.get("access_token")
    if not access:
        raise AuthError("Atlassian token response contained no access_token.")
    data["access_token"] = access
    # Atlassian rotates refresh tokens: persist the newest one each time.
    if resp.get("refresh_token"):
        data["refresh_token"] = resp["refresh_token"]
    if resp.get("scope"):
        data["scopes"] = str(resp["scope"]).split()
    expires_in = int(resp.get("expires_in", 3600) or 3600)
    data["expiry"] = time.time() + expires_in


# --- public API ---

def link(
    *,
    token_path: Path,
    connect_url: str,
    client_id: str,
    scopes: list[str],
    port: int,
    site_url_hint: str = "",
) -> dict[str, Any]:
    """Run the backend-mediated 3LO consent flow and persist tokens locally."""
    if not client_id:
        raise AuthError("No Atlassian client id (discovery doc has none; set ATLASSIAN_CLIENT_ID to override).")
    if not _backend_listener_available(port):
        raise AuthError(
            "Atlassian linking requires the Cremind backend loopback listener on a "
            f"fixed port (CREMIND_OAUTH_CALLBACK_PORT={port or 'unset'}). Atlassian only "
            "allows a single, pre-registered callback URL, so the standalone/ephemeral "
            "fallback used by the Google skills is not available here. Run under "
            "`cremind serve` and register http://127.0.0.1:<port>/ as the app's callback URL."
        )

    state = secrets.token_urlsafe(24)
    redirect_uri = f"http://127.0.0.1:{port}/"
    params = {
        "audience": "api.atlassian.com",
        "client_id": client_id,
        "scope": " ".join(scopes),
        "redirect_uri": redirect_uri,
        "state": state,
        "response_type": "code",
        "prompt": "consent",
    }
    auth_url = f"{AUTHORIZE_URL}?{urlencode(params)}"
    print(f"Please visit this URL to authorize this application: {auth_url}", flush=True)

    code = _await_oauth_callback(state)
    tokens = _http_json(
        "POST",
        f"{connect_url.rstrip('/')}/oauth/atlassian/exchange",
        body={"code": code, "redirectUri": redirect_uri},
    )

    data: dict[str, Any] = {}
    _apply_token_response(data, tokens)

    profile = me(data["access_token"])
    email = profile.get("email", "")
    if not email:
        raise AuthError("Atlassian /me returned no email (was the 'read:me' scope granted?).")

    cloud_id, site_url = pick_site(accessible_resources(data["access_token"]), site_url_hint)

    data.update(
        {
            "email": email,
            "account_id": profile.get("account_id", ""),
            "account_key": account_key_for("atlassian", email),
            "cloud_id": cloud_id,
            "site_url": site_url,
        }
    )
    TokenStore(token_path).save(data)
    return data


def load_account(token_path: Path) -> dict[str, Any]:
    data = TokenStore(token_path).load()
    if not data:
        raise AuthError("Account not linked. Run: uv run scripts/__main__.py link")
    return data


def _refresh(store: TokenStore, data: dict[str, Any], connect_url: str) -> dict[str, Any]:
    rt = data.get("refresh_token")
    if not rt:
        raise AuthError("No refresh token stored; re-run link.")
    resp = _http_json(
        "POST",
        f"{connect_url.rstrip('/')}/oauth/atlassian/refresh",
        body={"refreshToken": rt},
    )
    _apply_token_response(data, resp)
    store.save(data)
    return data


def get_access_token(token_path: Path, connect_url: str, *, force: bool = False) -> tuple[str, dict[str, Any]]:
    """Return (access_token, data), refreshing via the backend if near expiry."""
    store = TokenStore(token_path)
    data = load_account(token_path)
    if force or time.time() >= float(data.get("expiry", 0)) - 60:
        data = _refresh(store, data, connect_url)
    return data["access_token"], data


def fresh_relay_session(token_path: Path, connect_url: str) -> str:
    """Mint a short-lived relay-session JWT for the WebSocket subscription."""
    access_token, _ = get_access_token(token_path, connect_url)
    resp = _http_json(
        "POST",
        f"{connect_url.rstrip('/')}/oauth/atlassian/relay-session",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    session = resp.get("session")
    if not session:
        raise AuthError("relay-session bootstrap returned no session token.")
    return session
