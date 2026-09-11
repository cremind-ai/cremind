"""Backend OAuth callback routes for the built-in skills and the Google pages.

The Google (gmail, gcalendar, gdocs, gsheets, gdrive) and Atlassian (jira,
confluence) skills run as short-lived subprocesses, so their consent redirect
can't be received by a server inside the subprocess (it dies with the agent
turn). Instead ``cremind serve`` exposes stable callback routes on the
always-running backend:

  GET /api/oauth/callback                  the subprocess skills (Google + Atlassian)
  GET /api/oauth/google-calendar/callback  Calendar & Schedule's own connect
  GET /api/oauth/google-drive/callback     the Drive picker's per-file grants

Google's redirect is an HTTP loopback URL derived from APP_URL
(app/config/oauth_loopback.py) — e.g. ``http://localhost:1515/api/oauth/callback``
behind a Kubernetes ``port-forward``. Once the public port serves HTTPS, the
same-port plaintext recovery listener 307s exactly these three paths (GET only)
to their HTTPS handlers (app/api/tls_recovery.py), so the loopback redirect Google
insists on keeps working. For the skills, the handler writes the raw
authorization response (``code`` + ``state`` + ``scope``) to a per-state file
under ``<CREMIND_SYSTEM_DIR>/oauth_inbox/<state>.txt``; the skill's waiting
``link`` polls that file and performs the token exchange itself, so tokens never
leave the machine and cremind-connect is never in the token path. Calendar exchanges in this process; Drive only
records which files were picked.

Every handler does its work first and only then answers — with a 303 to
app/api/oauth_close.py's page, never with a document at its own URL, which
carries the authorization code. That page tells whichever page opened the
consent and then closes the window; nothing navigates into the app. It reports
"response received", not "linked": for the skills the exchange happens later in
the waiting link, which is what confirms success.

These routes are registered PRE-storage (app/server.py) so a consent redirect
can't 404 while storage is still booting or restarting. The inbox and Drive
handlers touch no DB/registry/agent state; Calendar's exchange needs the
auth-token store and reports ``failed`` if it is not up.
"""
from __future__ import annotations

import os
import re
import time
from typing import Optional

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from app.api.oauth_close import close_redirect
from app.config.settings import BaseConfig
from app.utils import logger

# The OAuth ``state`` is a URL-safe token minted by oauthlib/secrets. It becomes
# a filename here, so accept only this charset/length and reject anything else —
# the guard against path traversal via a crafted ``state``. The handlers apply it
# with ``fullmatch``: ``$`` alone also matches before a trailing newline.
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")

# Drop inbox files older than this so abandoned consent flows don't accumulate.
_INBOX_TTL_S = 600

def oauth_inbox_dir() -> str:
    """Directory where captured authorization responses are dropped for skills."""
    return os.path.join(BaseConfig.CREMIND_SYSTEM_DIR, "oauth_inbox")


def _prune_stale(inbox: str) -> None:
    try:
        now = time.time()
        for name in os.listdir(inbox):
            path = os.path.join(inbox, name)
            try:
                if now - os.path.getmtime(path) > _INBOX_TTL_S:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


def _write_inbox(state: str, query: str) -> None:
    """Atomically drop the raw authorization-response query for ``state``."""
    inbox = oauth_inbox_dir()
    os.makedirs(inbox, exist_ok=True)
    _prune_stale(inbox)
    dst = os.path.join(inbox, f"{state}.txt")
    tmp = dst + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(query)
    os.replace(tmp, dst)


def _respond(
    outcome: str,
    flow: Optional[str],
    reason: Optional[str] = None,
    error_code: Optional[str] = None,
) -> Response:
    """Answer the consent window: send it to the page that closes it.

    A redirect, not a page. This handler's own URL carries the authorization
    code, and a document committed there would leave it in the address bar and
    in history — for the skills it stays redeemable until the waiting ``link``
    exchanges it. Only the outcome travels on, in the fixed vocabulary of
    app/api/oauth_close.py: never the code, the state, or the text of an
    exception.
    """
    return close_redirect(outcome=outcome, flow=flow, reason=reason, error_code=error_code)


async def _handle_inbox_callback(request: Request) -> Response:
    """Capture a subprocess-skill consent redirect into the per-state inbox.

    Shared by the Google (Desktop) and jira/confluence (Atlassian 3LO) skills via
    a single ``/api/oauth/callback`` route; the per-flow ``state`` (not the path)
    disambiguates flows, and this handler reads only the query. The waiting
    ``link`` polls ``oauth_inbox/<state>.txt`` and finishes the exchange — so the
    inbox is written FIRST, before anything about the browser's way back, and a
    denial is written too so the waiter can report it instead of timing out.
    """
    params = request.query_params
    state = params.get("state", "")
    if not _STATE_RE.fullmatch(state):
        logger.warning("[oauth-callback] callback with missing/invalid state; ignoring")
        return _respond("invalid", None, "nostate")
    # request.url.query is the raw, still-encoded query string — exactly what
    # the skill replays into fetch_token.
    try:
        _write_inbox(state, request.url.query)
    except OSError as e:  # noqa: BLE001
        logger.error(f"[oauth-callback] failed to write inbox file: {e}")
        return _respond("failed", "skill", "inbox")
    if "error" in params:
        logger.info(f"[oauth-callback] consent returned error for state={state[:6]}…")
        return _respond("denied", "skill", "denied", params.get("error"))
    logger.info(f"[oauth-callback] captured authorization response for state={state[:6]}…")
    return _respond("received", "skill")


async def _handle_google_calendar_callback(request: Request) -> Response:
    """Complete the backend-native Google Calendar OAuth exchange.

    Unlike the subprocess skills (which poll the file inbox), the Calendar
    connect flow runs in THIS process: this handler hands ``state`` + ``code``
    to ``app.calendar.google_auth.complete_callback``, which exchanges the code
    and stores per-profile tokens in ``auth_tokens``. So here ``received`` does
    mean connected; the Calendar page still re-reads ``GET /api/calendar/settings``
    to show it.
    """
    params = request.query_params
    state = params.get("state", "")
    if not _STATE_RE.fullmatch(state):
        logger.warning("[oauth-callback] google-calendar callback with missing/invalid state")
        return _respond("invalid", "calendar", "nostate")
    if "error" in params:
        logger.info(f"[oauth-callback] google-calendar consent error for state={state[:6]}…")
        return _respond("denied", "calendar", "denied", params.get("error"))
    code = params.get("code", "")
    if not code:
        logger.warning(f"[oauth-callback] google-calendar callback without a code for state={state[:6]}…")
        return _respond("failed", "calendar", "nocode")
    from app.calendar import google_auth

    try:
        google_auth.complete_callback(state, code)
    except google_auth.GoogleAuthError as exc:
        if "unknown or expired" not in str(exc):
            logger.error(f"[oauth-callback] google-calendar exchange failed: {exc}")
            return _respond("failed", "calendar", "exchange")
        # Expired (ten minutes), already used, or started before a restart
        # emptied the in-memory pending map.
        logger.warning(f"[oauth-callback] google-calendar callback for an unknown state={state[:6]}…")
        return _respond("invalid", "calendar", "expired")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[oauth-callback] google-calendar exchange failed: {exc}")
        return _respond("failed", "calendar", "exchange")
    logger.info(f"[oauth-callback] google-calendar connected for state={state[:6]}…")
    return _respond("received", "calendar")


async def _handle_google_drive_callback(request: Request) -> Response:
    """Record which Drive files the user picked in the Google Picker.

    Unlike every other callback here, nothing is exchanged: the per-file grant
    lands with Google the moment the user approves, so this redirect is only how
    we learn *which* files were picked (``picked_file_ids``). If it never arrives —
    a remote install whose APP_URL the browser can't reach — the grant still holds
    and the Drive page discovers the files by diffing what is reachable.
    """
    params = request.query_params
    state = params.get("state", "")
    if not _STATE_RE.fullmatch(state):
        logger.warning("[oauth-callback] google-drive callback with missing/invalid state")
        return _respond("invalid", "drive", "nostate")
    from app.drive import grant_flow

    try:
        outcome = grant_flow.record_redirect(request.url.query)
    except grant_flow.DriveGrantError as exc:
        # record_redirect's only refusal: a state no round is waiting on.
        logger.warning(f"[oauth-callback] google-drive picker response ignored: {exc}")
        return _respond("invalid", "drive", "expired")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[oauth-callback] google-drive picker response failed: {exc}")
        return _respond("failed", "drive", "picks")
    if outcome.get("status") == "error":
        logger.info(f"[oauth-callback] google-drive picker returned an error for state={state[:6]}…")
        return _respond("denied", "drive", "denied", params.get("error"))
    logger.info(f"[oauth-callback] google-drive picker captured for state={state[:6]}…")
    return _respond("received", "drive")


def get_oauth_callback_routes() -> list[Route]:
    """Backend OAuth callback routes. Registered PRE-storage in app/server.py so a
    consent redirect can't 404 while an account-link is in flight. Mounted under
    ``/api`` so they ride the K8s proxy's existing ``/api`` route to the backend."""
    return [
        Route("/api/oauth/callback", methods=["GET"], endpoint=_handle_inbox_callback),
        Route(
            "/api/oauth/google-calendar/callback",
            methods=["GET"], endpoint=_handle_google_calendar_callback,
        ),
        Route(
            "/api/oauth/google-drive/callback",
            methods=["GET"], endpoint=_handle_google_drive_callback,
        ),
    ]
