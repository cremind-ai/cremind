"""Per-file Drive access grants via the Google Picker (desktop/mobile flow).

Cremind's shared OAuth client requests only the *sensitive* ``drive.file`` scope,
which reaches **only** files this app created or files the user explicitly picked.
Knowing a file's id or URL is never sufficient — the user has to grant it.

Google exposes a Picker driven purely by OAuth **URL parameters** (no JavaScript,
no API key, no App ID, no registered JS origins), so the org's *Desktop* OAuth
client keeps working. The consent screen shows a file browser; the redirect then
carries ``picked_file_ids`` alongside the usual ``code``.

Two hard constraints from Google shape this module:

1. ``drive.file`` is the **only** scope allowed in a Picker request — it cannot be
   combined with ``openid``/``email``. So this is a *separate* authorization step
   from ``link`` (which requests ``openid email drive.file`` and owns the identity
   / relay token). Nothing here writes the token store.
2. A grant attaches to the (app, user) pair and **persists** across token refresh
   and re-consent, so after a pick the token ``link`` already stored can reach the
   picked files. We never need the tokens this flow would mint.

``build_picker_params`` is deliberately the single place any Picker URL parameter
name appears — this surface is newer and more sparsely documented than the rest of
Google's OAuth API, so a rename should be a one-line fix here.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from .google import auth

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"

_FOLDER_MIME = "application/vnd.google-apps.folder"

_LANDED_HTML = b"""<!doctype html><meta charset="utf-8"><title>Cremind</title>
<body style="font:15px system-ui;padding:2.5rem;max-width:34rem">
<h3>Drive access granted</h3>
<p>You can close this tab and return to Cremind.</p></body>"""


def _pkce() -> tuple[str, str]:
    """Return (verifier, S256 challenge).

    We do not need the tokens this flow mints, but Google may still require a
    challenge for the client type, and holding the verifier keeps
    exchange-and-discard available as a fallback (see ``run_grant``).
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def build_picker_params(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    file_ids: list[str] | None = None,
    allow_multiple: bool = True,
    allow_folders: bool = True,
    mime_types: list[str] | None = None,
    login_hint: str = "",
) -> dict[str, str]:
    """Build the query parameters for a Picker authorization request.

    THE single definition of every Picker-specific parameter name. ``scope`` must
    stay ``drive.file`` alone — adding any other scope makes Google reject the
    request (constraint 1 in the module docstring), which the tests assert.
    """
    params: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": DRIVE_FILE_SCOPE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        # Always re-prompt: the point of the round trip is to show the file
        # browser, which a silent re-auth would skip.
        "prompt": "consent",
        # Turns an ordinary consent request into the Picker flow.
        "trigger_onepick": "true",
    }
    if allow_multiple:
        params["allow_multiple"] = "true"
    if allow_folders:
        params["allow_folder_selection"] = "true"
    if mime_types:
        params["mimetypes"] = ",".join(mime_types)
    if file_ids:
        # Pre-navigates/filters the Picker to specific files — the supported way
        # to handle "the user pasted a Drive link we can't read yet".
        params["file_ids"] = ",".join(file_ids)
    if login_hint:
        params["login_hint"] = login_hint
    return params


def build_authorize_url(**kwargs: Any) -> tuple[str, dict[str, str]]:
    """Return (url, params) for a Picker authorization request."""
    params = build_picker_params(**kwargs)
    return f"{_AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}", params


def parse_picked_ids(query: str) -> list[str]:
    """Extract ``picked_file_ids`` from a redirect query string."""
    parsed = urllib.parse.parse_qs(query)
    raw = (parsed.get("picked_file_ids") or [""])[0]
    return [fid for fid in (part.strip() for part in raw.split(",")) if fid]


def _capture_via_local_server(port_box: dict[str, Any], *, timeout: float) -> str:
    """Serve exactly one redirect on an ephemeral loopback port; return its query.

    Used when there is no backend inbox (a standalone skill run). Mirrors the
    ephemeral-loopback fallback ``auth.link`` uses.
    """
    box: dict[str, Any] = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib signature
            box["query"] = urllib.parse.urlparse(self.path).query
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(_LANDED_HTML)))
            self.end_headers()
            self.wfile.write(_LANDED_HTML)

        def log_message(self, *_args: Any) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port_box["port"] = server.server_port
    port_box["ready"].set()
    worker = threading.Thread(target=server.handle_request, daemon=True)
    worker.start()
    deadline = time.monotonic() + timeout
    try:
        while worker.is_alive() and time.monotonic() < deadline:
            worker.join(timeout=0.5)
    except KeyboardInterrupt:
        raise auth.AuthError("Grant cancelled (Ctrl+C) before the Picker completed.")
    finally:
        server.server_close()
    query = box.get("query")
    if not query:
        raise auth.AuthError(
            f"Timed out waiting for the Picker ({int(timeout)}s). Re-run grant and "
            "complete the browser step."
        )
    if "error" in urllib.parse.parse_qs(query):
        raise auth.AuthError("Google consent was denied or returned an error.")
    return query


def _exchange_and_discard(
    *, code: str, verifier: str, client_id: str, client_secret: str, redirect_uri: str
) -> None:
    """Redeem the Picker's authorization code and throw the tokens away.

    Only called when the grant does not appear to have taken effect at consent
    time. The durable per-file grant is what we are after; the access/refresh
    tokens this returns are redundant with the ones ``link`` already stored, so
    they are deliberately not persisted.
    """
    body = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        }
    ).encode("ascii")
    req = urllib.request.Request(
        auth.GOOGLE_TOKEN_URI,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except Exception:  # noqa: BLE001 - best-effort; the caller re-verifies anyway
        pass


def record_grants(path: Path, entries: list[dict[str, Any]]) -> None:
    """Append granted-file provenance to a local JSON cache (best effort).

    Not a source of truth — under ``drive.file`` the authoritative set is whatever
    ``files.list`` returns. This only remembers *how* a file was reached so the UI
    can distinguish "you picked this" from "Cremind created this".
    """
    try:
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        if not isinstance(existing, list):
            existing = []
    except (OSError, json.JSONDecodeError):
        existing = []
    known = {e.get("id") for e in existing if isinstance(e, dict)}
    existing.extend(e for e in entries if e.get("id") not in known)
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def run_grant(
    *,
    token_path: Path,
    grants_path: Path,
    client_id: str,
    client_secret: str,
    redirect_uri: str | None,
    build_service,
    get_file,
    list_files,
    file_ids: list[str] | None = None,
    allow_multiple: bool = True,
    allow_folders: bool = True,
    mime_types: list[str] | None = None,
    wait: bool = True,
    timeout: float = 600.0,
) -> dict[str, Any]:
    """Run the Picker grant flow and report which files became reachable.

    ``build_service``/``get_file``/``list_files`` are injected so this module stays
    independent of the Drive API wrapper (and testable without it).
    """
    account = auth.load_account(token_path)
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce()

    use_inbox = bool(redirect_uri) and auth._oauth_inbox_dir() is not None
    port_box: dict[str, Any] = {"ready": threading.Event()}
    capture: threading.Thread | None = None
    box: dict[str, Any] = {}

    if use_inbox:
        effective_redirect = redirect_uri or ""
    else:
        # Bind first so the redirect_uri can name the real port.
        def _run() -> None:
            try:
                box["query"] = _capture_via_local_server(port_box, timeout=timeout)
            except BaseException as exc:  # surfaced on the main thread below
                box["error"] = exc

        capture = threading.Thread(target=_run, name="picker-capture", daemon=True)
        capture.start()
        if not port_box["ready"].wait(timeout=10):
            raise auth.AuthError("Could not open a local port to receive the Picker redirect.")
        effective_redirect = f"http://localhost:{port_box['port']}/"

    url, _params = build_authorize_url(
        client_id=client_id,
        redirect_uri=effective_redirect,
        state=state,
        code_challenge=challenge,
        file_ids=file_ids,
        allow_multiple=allow_multiple,
        allow_folders=allow_folders,
        mime_types=mime_types,
        login_hint=account.get("email", ""),
    )

    if not wait:
        return {
            "authorize_url": url,
            "state": state,
            "waiting": False,
            "note": (
                "Open the URL, pick the files, and approve. Access applies as soon as "
                "consent completes; re-run a Drive command to use the files."
            ),
        }

    print(f"Please visit this URL to choose the files to share with Cremind: {url}", flush=True)

    if use_inbox:
        query = auth._await_oauth_callback(state, timeout=timeout)
    else:
        assert capture is not None
        while capture.is_alive():
            capture.join(timeout=0.5)
        if "error" in box:
            raise box["error"]
        query = box.get("query", "")

    params = urllib.parse.parse_qs(query)
    returned_state = (params.get("state") or [""])[0]
    if returned_state and returned_state != state:
        raise auth.AuthError("The Picker response did not match this request (state mismatch).")
    picked = parse_picked_ids(query)
    code = (params.get("code") or [""])[0]

    creds, _data = auth.get_credentials(token_path)
    svc = build_service(creds)

    verified, failed = _verify(svc, picked, get_file)
    if picked and not verified and code:
        # The grant did not appear to take effect at consent time; redeem the
        # code (tokens discarded) and re-check.
        _exchange_and_discard(
            code=code,
            verifier=verifier,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=effective_redirect,
        )
        verified, failed = _verify(svc, picked, get_file)

    for item in verified:
        if item.get("mime_type") == _FOLDER_MIME:
            item["children_visible"] = _children_visible(svc, item["id"], list_files)

    if verified:
        record_grants(
            grants_path,
            [
                {"id": i["id"], "name": i.get("name", ""), "mime_type": i.get("mime_type", ""),
                 "granted_at": time.time(), "source": "picker"}
                for i in verified
            ],
        )

    result: dict[str, Any] = {
        "granted": len(verified),
        "files": verified,
        "picked_file_ids": picked,
    }
    if failed:
        result["unverified"] = failed
        result["note"] = (
            "Some picked files could not be read back. Re-run grant for them, or "
            "confirm the account you approved with is the linked one "
            f"({account.get('email', 'unknown')})."
        )
    if not picked:
        result["note"] = "No files were picked, so nothing was granted."
    return result


def _verify(svc, picked: list[str], get_file) -> tuple[list[dict[str, Any]], list[str]]:
    verified: list[dict[str, Any]] = []
    failed: list[str] = []
    for fid in picked:
        try:
            meta = get_file(svc, file_id=fid)
        except Exception:  # noqa: BLE001 - any failure means "not reachable yet"
            failed.append(fid)
            continue
        verified.append(
            {
                "id": meta.get("id", fid),
                "name": meta.get("name", ""),
                "mime_type": meta.get("mimeType", ""),
                "web_view_link": meta.get("webViewLink", ""),
            }
        )
    return verified, failed


def _children_visible(svc, folder_id: str, list_files) -> bool:
    """Whether files inside a granted folder are reachable.

    Google does not document whether picking a folder extends the grant to its
    contents, so report what is actually true for this grant rather than assuming.
    """
    try:
        resp = list_files(svc, query=f"'{folder_id}' in parents and trashed = false", page_size=1)
    except Exception:  # noqa: BLE001
        return False
    return bool(resp.get("files"))
