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

Every handler does its work first and only then answers — with a 303 to the
SPA's ``/#/oauth-return`` view carrying a one-time ref (app/api/oauth_return.py)
and never the code or state. That view closes the consent popup, or leads a
consent tab back to the page that opened it. It reports "response received", not
"linked": for the skills the exchange happens later in the waiting link, which is
what confirms success.

These routes are registered PRE-storage (app/server.py) so a consent redirect
can't 404 while storage is still booting or restarting. The inbox and Drive
handlers touch no DB/registry/agent state; Calendar's exchange needs the
auth-token store and reports ``failed`` if it is not up.
"""
from __future__ import annotations

import os
import re
import time
from typing import Mapping, Optional

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from app.api import oauth_return
from app.config.settings import BaseConfig
from app.utils import logger

# The OAuth ``state`` is a URL-safe token minted by oauthlib/secrets. It becomes
# a filename here, so accept only this charset/length and reject anything else —
# the guard against path traversal via a crafted ``state``. The handlers apply it
# with ``fullmatch``: ``$`` alone also matches before a trailing newline.
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")

# Drop inbox files older than this so abandoned consent flows don't accumulate.
_INBOX_TTL_S = 600

# An RFC 6749 ``error`` code is a short token; anything else is not echoed.
_ERROR_CODE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

# Only when the return store itself cannot be written (an unwritable system
# directory): the callback's own work is already done, so say what we know
# rather than pretend the response went missing.
_RECEIVED_HTML = (
    b"<!doctype html><html><head><meta charset='utf-8'><title>Cremind</title></head>"
    b"<body style='font-family:sans-serif;text-align:center;padding-top:3rem'>"
    b"<h1>Response received</h1>"
    b"<p>Return to Cremind: the page that started this confirms when the account "
    b"is linked.</p></body></html>"
)
_ERROR_HTML = (
    b"<!doctype html><html><head><meta charset='utf-8'><title>Cremind</title></head>"
    b"<body style='font-family:sans-serif;text-align:center;padding-top:3rem'>"
    b"<h1>Authorization did not complete</h1>"
    b"<p>You can close this window and try linking again from Cremind.</p></body></html>"
)
_FALLBACK_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
# Fixed wording on purpose: exception text is logged, never put in front of the
# browser.
_CALENDAR_FAILED = "Google Calendar could not be connected. Try again from the Calendar page."


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


def _denial_message(params: Mapping[str, str]) -> str:
    code = params.get("error", "")
    if isinstance(code, str) and _ERROR_CODE_RE.fullmatch(code):
        return f"Authorization was not granted ({code})."
    return "Authorization was not granted."


def _respond(state: str, outcome: str, flow: str, message: Optional[str] = None) -> Response:
    """Send the consent tab to the SPA's return view with a fresh one-time ref.

    The ref (never the code or the state) is all the URL carries; it resolves to
    ``outcome`` plus, when the SPA recorded one, the page that opened consent.
    """
    try:
        ref = oauth_return.complete(state, outcome=outcome, flow=flow, message=message)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            f"[oauth-callback] could not record the {flow} return for state={state[:6]}…: {exc}"
        )
        body = _RECEIVED_HTML if outcome == "received" else _ERROR_HTML
        return HTMLResponse(body, status_code=200, headers=_FALLBACK_HEADERS)
    return oauth_return.return_redirect(ref)


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
        return oauth_return.invalid_state_redirect()
    # request.url.query is the raw, still-encoded query string — exactly what
    # the skill replays into fetch_token.
    try:
        _write_inbox(state, request.url.query)
    except OSError as e:  # noqa: BLE001
        logger.error(f"[oauth-callback] failed to write inbox file: {e}")
        return _respond(state, "failed", "skill",
                        "Cremind could not save the authorization response. Try linking again.")
    if "error" in params:
        logger.info(f"[oauth-callback] consent returned error for state={state[:6]}…")
        return _respond(state, "denied", "skill", _denial_message(params))
    logger.info(f"[oauth-callback] captured authorization response for state={state[:6]}…")
    return _respond(state, "received", "skill")


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
        return oauth_return.invalid_state_redirect()
    if "error" in params:
        logger.info(f"[oauth-callback] google-calendar consent error for state={state[:6]}…")
        return _respond(state, "denied", "calendar", _denial_message(params))
    code = params.get("code", "")
    if not code:
        logger.warning(f"[oauth-callback] google-calendar callback without a code for state={state[:6]}…")
        return _respond(state, "failed", "calendar",
                        "Google's response carried no authorization code. Try connecting again.")
    from app.calendar import google_auth

    try:
        google_auth.complete_callback(state, code)
    except google_auth.GoogleAuthError as exc:
        if "unknown or expired" not in str(exc):
            logger.error(f"[oauth-callback] google-calendar exchange failed: {exc}")
            return _respond(state, "failed", "calendar", _CALENDAR_FAILED)
        # Expired (ten minutes), already used, or started before a restart
        # emptied the in-memory pending map.
        logger.warning(f"[oauth-callback] google-calendar callback for an unknown state={state[:6]}…")
        return _respond(state, "invalid", "calendar",
                        "This Google Calendar consent expired or was already used. "
                        "Start again from the Calendar page.")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[oauth-callback] google-calendar exchange failed: {exc}")
        return _respond(state, "failed", "calendar", _CALENDAR_FAILED)
    logger.info(f"[oauth-callback] google-calendar connected for state={state[:6]}…")
    return _respond(state, "received", "calendar")


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
        return oauth_return.invalid_state_redirect()
    from app.drive import grant_flow

    try:
        outcome = grant_flow.record_redirect(request.url.query)
    except grant_flow.DriveGrantError as exc:
        # record_redirect's only refusal: a state no round is waiting on.
        logger.warning(f"[oauth-callback] google-drive picker response ignored: {exc}")
        return _respond(state, "invalid", "drive",
                        "This Drive picker round expired or was already finished. "
                        "Start again from the Drive section.")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[oauth-callback] google-drive picker response failed: {exc}")
        return _respond(state, "failed", "drive",
                        "Cremind could not record the picked files. Try again from the Drive section.")
    if outcome.get("status") == "error":
        logger.info(f"[oauth-callback] google-drive picker returned an error for state={state[:6]}…")
        return _respond(state, "denied", "drive", _denial_message(params))
    logger.info(f"[oauth-callback] google-drive picker captured for state={state[:6]}…")
    return _respond(state, "received", "drive")


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
