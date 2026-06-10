"""Google OAuth for the cremind skills — loopback PKCE, token-less server.

The OAuth code->token exchange happens DIRECTLY between this local machine and
Google (loopback + PKCE, using the org's "Desktop" client). cremind-connect is
never in the token path. Tokens are stored locally in a JSON file on the user's
machine and refreshed locally.

The Google libraries are imported lazily so that account_key / discovery can be
used without them installed.
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .account_key import account_key_for

# Google returns the granted "email" scope in its full URL form
# (.../auth/userinfo.email), which oauthlib flags as a "Scope has changed"
# warning and raises it as an error. The grant is correct, so relax that check.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"

# The OAuth ``state`` is a URL-safe token minted by oauthlib. It becomes an
# inbox filename, so accept only this charset/length — the guard against path
# traversal via a crafted ``state`` in a pasted callback URL. Mirrors
# app/api/oauth_loopback.py's _STATE_RE.
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


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decode (without verifying) a JWT payload — used to read our own id_token's email."""
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg).decode("utf-8"))
    except Exception:
        return {}


def _run_local_server_interruptible(flow, **kwargs) -> Any:
    """Run ``flow.run_local_server`` so that Ctrl+C reliably aborts the wait.

    ``run_local_server`` blocks in ``wsgiref``'s ``handle_request()``. On Windows
    that wait sits inside a WinSock ``select()`` which SIGINT cannot interrupt:
    the Ctrl+C is queued but never delivered until a request actually arrives, so
    ``link`` looks frozen and can't be cancelled. Mirroring the listener's relay
    loop, run the blocking call on a daemon thread and park the MAIN thread in an
    interruptible ``join`` loop. The signal then lands within ~0.5s; the daemon
    thread (and its open socket) is reclaimed when the process exits.
    """
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["creds"] = flow.run_local_server(**kwargs)
        except BaseException as exc:  # surfaced on the main thread below
            box["error"] = exc

    worker = threading.Thread(target=_target, name="oauth-loopback", daemon=True)
    try:
        worker.start()
        while worker.is_alive():
            worker.join(timeout=0.5)
        # Surface a worker exception, or the creds, on the main thread. Kept inside
        # the try so a Ctrl+C landing in this teardown window is also normalized to
        # AuthError rather than leaking a raw KeyboardInterrupt. (SIGINT is only ever
        # delivered to the main thread, so box["error"] is never a KeyboardInterrupt
        # and cannot be double-wrapped here.)
        if "error" in box:
            raise box["error"]
        return box["creds"]
    except KeyboardInterrupt:
        raise AuthError("Linking cancelled (Ctrl+C) before consent completed.")


def _oauth_inbox_dir() -> Path | None:
    """Directory where ``cremind serve``'s persistent loopback listener drops
    captured authorization responses, or None when not running under the backend."""
    system_dir = os.environ.get("CREMIND_SYSTEM_DIR", "").strip()
    if not system_dir:
        return None
    return Path(system_dir) / "oauth_inbox"


def _backend_listener_available(port: int) -> bool:
    """True if the backend's persistent OAuth loopback listener is reachable.

    Requires the Cremind System Directory (so the skill can read the inbox the
    backend writes to) and a live TCP listener on the loopback port. When false,
    ``link`` falls back to an in-subprocess ephemeral loopback server.
    """
    if _oauth_inbox_dir() is None:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def _await_oauth_callback(state: str, *, timeout: float = 600.0) -> str:
    """Block until the backend drops the authorization response for ``state``.

    Returns the raw redirect query (``code=...&state=...&scope=...``). Raises
    AuthError on denial, timeout, or Ctrl+C. The wait is a plain sleep loop on
    the main thread, so SIGINT interrupts it promptly (no thread wrapper needed).
    """
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
                if "error" in parse_qs(query):
                    raise AuthError("Google consent was denied or returned an error.")
                return query
            time.sleep(0.5)
    except KeyboardInterrupt:
        raise AuthError("Linking cancelled (Ctrl+C) before consent completed.")
    raise AuthError(
        "Timed out waiting for Google consent (no callback received within "
        f"{int(timeout)}s). Re-run link and complete the browser consent."
    )


def _link_via_backend_callback(flow, port: int, redirect_uri: str | None = None) -> Any:
    """Authorize via ``cremind serve``'s persistent loopback callback listener.

    The skill builds the consent URL (PKCE is handled by ``flow``), prints it
    for the agent/CLI to surface, then waits for the backend to capture the
    redirect and performs the token exchange locally.

    By default the redirect is ``http://127.0.0.1:<port>/`` — a valid
    Desktop-client loopback the host browser hits directly (Docker publishes
    the port; native shares the loopback). Using 127.0.0.1 rather than
    ``localhost`` dodges the Windows ``localhost`` -> ``::1`` mismatch that can
    refuse an IPv4-only listener.

    When ``redirect_uri`` is supplied (the Kubernetes chart sets
    ``CREMIND_OAUTH_REDIRECT_URI`` to ``<APP_URL>/api/oauth/google/callback``),
    Cremind is fronted by the single-port nginx proxy, so the browser-facing
    redirect differs from the in-pod ``127.0.0.1:<port>``. It must still be a
    loopback origin (Desktop clients reject real hostnames); the proxy routes
    that path to the backend capture handler and the same inbox capture applies.
    """
    flow.redirect_uri = redirect_uri or f"http://127.0.0.1:{port}/"
    auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")
    print(f"Please visit this URL to authorize this application: {auth_url}", flush=True)
    query = _await_oauth_callback(state)
    # oauthlib insists OAuth 2.0 happens over https, so present the response as
    # such. The path is irrelevant to code extraction; only the query matters.
    https_base = re.sub(r"^http://", "https://", flow.redirect_uri)
    sep = "&" if "?" in https_base else "?"
    authorization_response = f"{https_base}{sep}{query}"
    flow.fetch_token(authorization_response=authorization_response)
    return flow.credentials


def submit_callback(response: str) -> dict[str, Any]:
    """Hand a manually-captured OAuth redirect back to the waiting ``link``.

    On remote/headless deployments (Ingress, SSH, or any topology where the
    consent redirect to the loopback URL fails to reach the pod) the browser
    still lands on a URL whose query carries a valid ``code`` + ``state``. The
    user copies that URL; this writes its query into the same per-state inbox
    file ``cremind serve``'s loopback listener would have written
    (``<CREMIND_SYSTEM_DIR>/oauth_inbox/<state>.txt``), so the still-running
    ``link`` picks it up and performs the local PKCE exchange. The auth ``code``
    is useless without the ``code_verifier`` that ``link`` holds, so tokens
    never leave the machine.

    ``response`` may be a full redirect URL or a bare ``code=...&state=...``
    query string. Raises ``AuthError`` on a missing/invalid state, a consent
    error, or when the inbox is unavailable.
    """
    raw = (response or "").strip()
    if not raw:
        raise AuthError("Empty OAuth response; paste the full URL Google redirected you to.")
    # Accept either a full URL (extract its query) or a bare query string.
    query = urlparse(raw).query
    if not query:
        query = raw[1:] if raw.startswith("?") else raw
    params = parse_qs(query)
    if "error" in params:
        raise AuthError("Google consent was denied or returned an error.")
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


def link(
    *,
    token_path: Path,
    client_id: str,
    client_secret: str,
    scopes: list[str],
    open_browser: bool = True,
    port: int = 0,
    bind_addr: str | None = None,
    redirect_uri: str | None = None,
) -> dict[str, Any]:
    """Run the loopback PKCE consent flow and persist tokens locally.

    ``port``/``bind_addr`` control the ephemeral callback server. They default
    to the library's behavior (random port bound to localhost), which works
    when the consenting browser shares this machine's loopback. In the Docker
    desktop image the browser runs on the host, so the callback must arrive
    through a PUBLISHED port: pass a fixed ``port`` and ``bind_addr="0.0.0.0"``
    so Docker-forwarded traffic reaches the server (a 127.0.0.1 bind would
    refuse it). ``host`` stays "localhost" so the advertised redirect_uri is
    ``http://localhost:<port>/`` — what the host browser hits, and a valid
    loopback redirect for the org's Desktop OAuth client.

    ``redirect_uri`` overrides the advertised loopback redirect for the
    backend-listener path — set by the Kubernetes chart to route the callback
    through its single nginx proxy port (see ``_link_via_backend_callback``).
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": GOOGLE_AUTH_URI,
            "token_uri": GOOGLE_TOKEN_URI,
            # Ignored: run_local_server overwrites flow.redirect_uri with
            # http://localhost:<bound-port>/ before building the auth URL.
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes)
    if port and _backend_listener_available(port):
        # Preferred path under ``cremind serve``: the backend hosts ONE
        # persistent loopback callback server (app/api/oauth_loopback.py), so
        # consent survives the agent turn / subprocess teardown that killed the
        # old per-link server. The skill still does the PKCE token exchange.
        creds = _link_via_backend_callback(flow, port, redirect_uri=redirect_uri)
    else:
        # Fallback for a standalone CLI run (no backend listening): spin up an
        # ephemeral loopback server in this process, as before.
        creds = _run_local_server_interruptible(
            flow,
            host="localhost",
            bind_addr=bind_addr,
            port=port,
            access_type="offline",
            prompt="consent",
            open_browser=open_browser,
        )
    if not creds.refresh_token:
        raise AuthError(
            "Google did not return a refresh token. Revoke prior access at "
            "https://myaccount.google.com/permissions and re-run link."
        )

    id_token = getattr(creds, "id_token", None) or ""
    claims = _decode_jwt_payload(id_token) if id_token else {}
    email = claims.get("email", "")
    if not email:
        raise AuthError("id_token did not contain an email claim (was 'openid email' requested?)")

    data: dict[str, Any] = {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "id_token": id_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "scopes": list(creds.scopes or scopes),
        "email": email,
        "account_key": account_key_for("google", email),
        "expiry": creds.expiry.timestamp() if creds.expiry else 0,
    }
    TokenStore(token_path).save(data)
    return data


def _build_credentials(data: dict[str, Any]):
    from google.oauth2.credentials import Credentials

    return Credentials(
        token=data.get("access_token"),
        refresh_token=data.get("refresh_token"),
        token_uri=GOOGLE_TOKEN_URI,
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes"),
    )


def _persist(store: TokenStore, data: dict[str, Any], creds) -> None:
    data["access_token"] = creds.token
    if getattr(creds, "id_token", None):
        data["id_token"] = creds.id_token
    if creds.refresh_token:
        data["refresh_token"] = creds.refresh_token
    data["expiry"] = creds.expiry.timestamp() if creds.expiry else 0
    store.save(data)


def load_account(token_path: Path) -> dict[str, Any]:
    data = TokenStore(token_path).load()
    if not data:
        raise AuthError(
            "Account not linked. Run: uv run scripts/__main__.py link"
        )
    return data


def get_credentials(token_path: Path, *, force_refresh: bool = False):
    """Return (credentials, data), refreshing the access token if needed."""
    from google.auth.transport.requests import Request

    store = TokenStore(token_path)
    data = load_account(token_path)
    creds = _build_credentials(data)
    if force_refresh or not creds.valid:
        creds.refresh(Request())
        _persist(store, data, creds)
    return creds, data


def fresh_id_token(token_path: Path) -> str:
    """Force a token refresh to obtain a fresh (short-lived) Google ID token.

    The relay verifies this to authorize a subscription. It grants no API access.
    """
    from google.auth.transport.requests import Request

    store = TokenStore(token_path)
    data = load_account(token_path)
    creds = _build_credentials(data)
    creds.refresh(Request())  # refresh always returns a fresh id_token when openid scope is granted
    _persist(store, data, creds)
    id_token = getattr(creds, "id_token", None) or data.get("id_token") or ""
    if not id_token:
        raise AuthError("could not obtain a fresh id_token")
    return id_token
