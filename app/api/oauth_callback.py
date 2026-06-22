"""Backend OAuth callback routes for the built-in skills and A2A tool auth.

The gmail/gcalendar/jira/confluence skills run as short-lived subprocesses, so
their OAuth consent redirect can't be received by a server inside the subprocess
(it dies with the agent turn). Instead ``cremind serve`` exposes stable callback
routes on the always-running backend — fronted by the nginx proxy under the K8s
chart, published directly in Docker/native:

  GET /api/oauth/callback             gmail, gcalendar, jira, confluence (built-in skills)
  GET /api/oauth/a2a/callback         A2A tool auth     (external OAuth providers)

The browser-facing redirect URL is derived from ``APP_URL`` (see
app/config/system_vars.py) — e.g. ``http://localhost:1515/api/oauth/callback``
behind a Kubernetes ``port-forward``. For the skills, the handler writes the raw
authorization response (``code`` + ``state`` + ``scope``) to a per-state file
under ``<CREMIND_SYSTEM_DIR>/oauth_inbox/<state>.txt``; the skill subprocess polls
that file and performs the token exchange itself, so tokens never leave the
machine and cremind-connect is never in the token path. A2A tool auth runs in
THIS process, so its callback resolves an in-process Future instead
(app/tools/a2a/oauth_rendezvous.py) — no file inbox needed.

These routes are registered PRE-storage (app/server.py) so an account-link
driven over the A2A endpoint — available before storage finishes booting — can
never hit a 404. The handlers touch no DB/registry/agent state, so that is safe.
"""
from __future__ import annotations

import os
import re
import time

from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

from app.config.settings import BaseConfig
from app.utils import logger

# The OAuth ``state`` is a URL-safe token minted by oauthlib/secrets. It becomes
# a filename here, so accept only this charset/length and reject anything else —
# the guard against path traversal via a crafted ``state``.
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")

# Drop inbox files older than this so abandoned consent flows don't accumulate.
_INBOX_TTL_S = 600

_SUCCESS_HTML = (
    b"<!doctype html><html><head><meta charset='utf-8'><title>Cremind</title></head>"
    b"<body style='font-family:sans-serif;text-align:center;padding-top:3rem'>"
    b"<h1>Authentication complete</h1>"
    b"<p>You can close this window and return to Cremind.</p>"
    b"<script>setTimeout(function(){window.close()},2000)</script></body></html>"
)
_ERROR_HTML = (
    b"<!doctype html><html><head><meta charset='utf-8'><title>Cremind</title></head>"
    b"<body style='font-family:sans-serif;text-align:center;padding-top:3rem'>"
    b"<h1>Authentication failed</h1>"
    b"<p>You can close this window and try linking again.</p></body></html>"
)


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


async def _handle_inbox_callback(request: Request) -> HTMLResponse:
    """Capture a subprocess-skill consent redirect into the per-state inbox.

    Shared by the gmail/gcalendar (Google Desktop) and jira/confluence (Atlassian
    3LO) skills via a single ``/api/oauth/callback`` route; the per-flow ``state``
    (not the path) disambiguates flows, and this handler reads only the query. The
    waiting ``link`` in the skill subprocess polls ``oauth_inbox/<state>.txt`` and
    finishes the exchange.
    """
    params = request.query_params
    state = params.get("state", "")
    if not _STATE_RE.match(state):
        logger.warning("[oauth-callback] callback with missing/invalid state; ignoring")
        return HTMLResponse(_ERROR_HTML, status_code=400)
    # request.url.query is the raw, still-encoded query string — exactly what
    # the skill replays into fetch_token.
    try:
        _write_inbox(state, request.url.query)
    except OSError as e:  # noqa: BLE001
        logger.error(f"[oauth-callback] failed to write inbox file: {e}")
        return HTMLResponse(_ERROR_HTML, status_code=500)
    if "error" in params:
        logger.info(f"[oauth-callback] consent returned error for state={state[:6]}…")
        return HTMLResponse(_ERROR_HTML, status_code=200)
    logger.info(f"[oauth-callback] captured authorization response for state={state[:6]}…")
    return HTMLResponse(_SUCCESS_HTML, status_code=200)


async def _handle_google_calendar_callback(request: Request) -> HTMLResponse:
    """Complete the backend-native Google Calendar OAuth exchange.

    Unlike the subprocess skills (which poll the file inbox), the Calendar
    connect flow runs in THIS process: this handler hands ``state`` + ``code``
    to ``app.calendar.google_auth.complete_callback``, which exchanges the code
    and stores per-profile tokens in ``auth_tokens``. The popup then closes and
    the Calendar page polls ``GET /api/calendar/settings`` to see "connected".
    """
    params = request.query_params
    state = params.get("state", "")
    if not _STATE_RE.match(state):
        logger.warning("[oauth-callback] google-calendar callback with missing/invalid state")
        return HTMLResponse(_ERROR_HTML, status_code=400)
    if "error" in params:
        logger.info(f"[oauth-callback] google-calendar consent error for state={state[:6]}…")
        return HTMLResponse(_ERROR_HTML, status_code=200)
    code = params.get("code", "")
    if not code:
        return HTMLResponse(_ERROR_HTML, status_code=400)
    try:
        from app.calendar.google_auth import complete_callback
        complete_callback(state, code)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[oauth-callback] google-calendar exchange failed: {exc}")
        return HTMLResponse(_ERROR_HTML, status_code=200)
    logger.info(f"[oauth-callback] google-calendar connected for state={state[:6]}…")
    return HTMLResponse(_SUCCESS_HTML, status_code=200)


async def _handle_a2a_callback(request: Request) -> HTMLResponse:
    """Resolve the in-process Future for an A2A OAuth consent redirect.

    A2A tool auth runs in THIS process (not a subprocess), so the authorization
    code is handed back through an in-memory rendezvous keyed by ``state`` rather
    than the file inbox the skill subprocesses use.
    """
    from app.tools.a2a.oauth_rendezvous import resolve

    params = request.query_params
    state = params.get("state", "")
    if not state:
        logger.warning("[oauth-callback] a2a callback with missing state; ignoring")
        return HTMLResponse(_ERROR_HTML, status_code=400)
    if "error" in params:
        logger.info("[oauth-callback] a2a consent returned an error")
        resolve(state, None)
        return HTMLResponse(_ERROR_HTML, status_code=200)
    code = params.get("code", "")
    if not code or not resolve(state, code):
        logger.warning("[oauth-callback] a2a callback with no code or unknown state")
        return HTMLResponse(_ERROR_HTML, status_code=400)
    logger.info("[oauth-callback] a2a captured authorization code")
    return HTMLResponse(_SUCCESS_HTML, status_code=200)


def get_oauth_callback_routes() -> list[Route]:
    """Backend OAuth callback routes. Registered PRE-storage in app/server.py so a
    consent redirect can't 404 while an account-link (driven over the pre-storage
    A2A endpoint) is in flight. Mounted under ``/api`` so they ride the K8s
    proxy's existing ``/api`` route to the backend."""
    return [
        Route("/api/oauth/callback", methods=["GET"], endpoint=_handle_inbox_callback),
        Route("/api/oauth/a2a/callback", methods=["GET"], endpoint=_handle_a2a_callback),
        Route(
            "/api/oauth/google-calendar/callback",
            methods=["GET"], endpoint=_handle_google_calendar_callback,
        ),
    ]
