"""Backend orchestration of the Google Picker flow that grants per-file Drive access.

Cremind requests only ``drive.file``, so it reaches nothing the user has not
explicitly picked (plus what it created itself). Google exposes a Picker driven by
OAuth **URL parameters** — no JavaScript, no API key — which keeps the org's
Desktop OAuth client usable. See ``app/skills/builtin/gdrive/scripts/app/grant.py``
for the same flow driven from the skill; ``build_picker_params`` here mirrors that
builder and a parity test keeps the two honest.

Two properties make this flow unusually forgiving, and the design leans on both:

* ``drive.file`` **cannot be combined with any other scope** in a Picker request,
  so this is a separate authorization from the skill's ``link`` (which owns the
  identity/relay token). Nothing here mints or stores a token.
* **The grant lands when the user approves**, not when we process the redirect.
  So the redirect is only how we learn *which* files were picked. If it never
  arrives — a remote install whose ``APP_URL`` the browser cannot reach — we
  diff the reachable-file set instead and still report the truth. That is why
  ``poll_status`` works on installs where redirect capture cannot.

State lives only in-process, like the Calendar connect flow: a restart drops
in-flight grants, which costs the user one extra click and never corrupts state.
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlsplit, parse_qs

from app.calendar import google_discovery
from app.config.settings import BaseConfig
from app.drive import skill_token
from app.utils.logger import logger

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
CALLBACK_PATH = "/api/oauth/google-drive/callback"

DRIVE_FILE_SCOPE = skill_token.DRIVE_FILE_SCOPE

# state -> {profile, redirect_uri, before, ts, picked, status, error}
_pending: Dict[str, Dict[str, Any]] = {}
_PENDING_TTL = 900.0

_HINT_UNREACHABLE_REDIRECT = (
    "After you approve, your browser may show a connection error on the final "
    "redirect — that is expected on this install and does not undo the grant. "
    "Return here and refresh; the files you picked will be listed. The redirect "
    "targets {origin}: if a port-forward or tunnel makes that address reach this "
    "server from your machine (e.g. kubectl port-forward), keep it running while "
    "you approve and the redirect is captured automatically. Otherwise run "
    "'cremind drive grant' from your own machine."
)


class DriveGrantError(RuntimeError):
    pass


def _loopback_origin(url: str) -> Optional[str]:
    """``host[:port]`` when ``url`` names a loopback origin, else ``None``.

    A malformed URL — including a non-numeric or out-of-range port, which makes
    ``urlsplit(...).port`` raise — reads as "not loopback" rather than blowing up
    the caller.
    """
    try:
        parts = urlsplit((url or "").strip())
        if parts.hostname not in ("localhost", "127.0.0.1"):
            return None
        port = parts.port
    except ValueError:
        return None
    return f"{parts.hostname}:{port}" if port else parts.hostname


def _fallback_port() -> int:
    """The public bind port (``CREMIND_UI_PORT``), defaulting to 1515.

    ``0`` means "serve loopback-only behind an external proxy" (see
    ``app/server.py``) and so names no reachable port; that and an unparseable
    value both fall back to 1515, the port every documented port-forward uses
    (``kubectl port-forward svc/cremind 1515:80``).
    """
    try:
        port = int(os.environ.get("CREMIND_UI_PORT", "").strip())
    except ValueError:
        return 1515
    return port if port > 0 else 1515


def redirect_uri() -> str:
    """Loopback callback URL for the Picker redirect.

    Google's *Desktop* client type accepts loopback redirects only — a real
    hostname is rejected outright — so this always names loopback even when
    ``APP_URL`` is a public address. On such installs the browser may still reach
    us through a port-forward or tunnel, so name the loopback origin most likely
    to work, in order: the operator's pinned ``CREMIND_OAUTH_REDIRECT_URI`` (when
    itself loopback — it is the sibling Google-skills redirect, so a forward that
    serves one serves both), then ``APP_URL``'s own explicit port, then the public
    bind port. Never portless: a port-less public ``APP_URL`` (a K8s Ingress) must
    not degrade to ``http://localhost`` (:80), which no deployment serves.

    Capture is best-effort either way — the grant lands when the user approves, so
    a redirect that never arrives costs only the file list, which ``poll_status``
    recovers by diffing.
    """
    base = (BaseConfig.APP_URL or "").strip().rstrip("/")
    local = _loopback_origin(base)
    if local:
        return f"http://{local}{CALLBACK_PATH}"

    pinned_raw = os.environ.get("CREMIND_OAUTH_REDIRECT_URI", "").strip()
    pinned = _loopback_origin(pinned_raw)
    if pinned:
        return f"http://{pinned}{CALLBACK_PATH}"
    if pinned_raw:
        logger.debug(
            "[drive] CREMIND_OAUTH_REDIRECT_URI is not a loopback origin; ignoring "
            "it for the Picker redirect (Desktop clients accept loopback only)"
        )

    try:
        app_port = urlsplit(base).port if base else None
    except ValueError:
        app_port = None
    return f"http://localhost:{app_port or _fallback_port()}{CALLBACK_PATH}"


def capture_is_local() -> bool:
    """Whether the browser can reach our callback (i.e. APP_URL is loopback).

    Shares :func:`_loopback_origin` with :func:`redirect_uri` so the two always
    agree on what counts as loopback — including a malformed port, where the
    redirect falls back to another port and capture therefore is *not* local.
    """
    return _loopback_origin(BaseConfig.APP_URL or "") is not None


def capture_hint() -> Optional[str]:
    if capture_is_local():
        return None
    return _HINT_UNREACHABLE_REDIRECT.format(origin=redirect_uri()[: -len(CALLBACK_PATH)])


def build_picker_params(
    *,
    client_id: str,
    redirect: str,
    state: str,
    file_ids: Optional[List[str]] = None,
    allow_multiple: bool = True,
    allow_folders: bool = True,
    mime_types: Optional[List[str]] = None,
    login_hint: str = "",
) -> Dict[str, str]:
    """The single definition of every Picker-specific parameter name here.

    ``scope`` must remain ``drive.file`` alone; Google rejects a Picker request
    carrying any additional scope.
    """
    params: Dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": DRIVE_FILE_SCOPE,
        "state": state,
        "prompt": "consent",
        "trigger_onepick": "true",
    }
    if allow_multiple:
        params["allow_multiple"] = "true"
    if allow_folders:
        params["allow_folder_selection"] = "true"
    if mime_types:
        params["mimetypes"] = ",".join(mime_types)
    if file_ids:
        params["file_ids"] = ",".join(file_ids)
    if login_hint:
        params["login_hint"] = login_hint
    return params


def _prune() -> None:
    now = time.time()
    for state in [s for s, p in _pending.items() if now - p.get("ts", 0) > _PENDING_TTL]:
        _pending.pop(state, None)


def start(
    profile: str,
    *,
    file_ids: Optional[List[str]] = None,
    allow_multiple: bool = True,
    allow_folders: bool = True,
    mime_types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Begin a grant round. Raises :class:`DriveGrantError` when unavailable."""
    account = skill_token.status(profile)
    if not account["linked"]:
        raise DriveGrantError(
            "Google Drive is not linked yet. Ask the agent to link the gdrive skill "
            "first, then grant files."
        )
    # The grant must be requested with the client the linked token was minted with:
    # it attaches to the (app, user) pair, so naming a different client would land
    # the grant somewhere this token cannot use it. The skill resolves its client
    # from its own scripts/.env first, so prefer what it actually stored and fall
    # back to the broker only when the token predates that field.
    client_id = skill_token.token_client_id(profile)
    if not client_id:
        try:
            client_id = google_discovery.google_client()["client_id"]
        except Exception as exc:  # noqa: BLE001
            raise DriveGrantError(f"could not obtain the Google OAuth client: {exc}") from exc

    try:
        before = skill_token.reachable_ids(profile)
    except skill_token.DriveTokenError as exc:
        raise DriveGrantError(str(exc)) from exc

    state = secrets.token_urlsafe(24)
    redirect = redirect_uri()
    _pending[state] = {
        "profile": profile,
        "redirect_uri": redirect,
        "before": before,
        "ts": time.time(),
        "picked": [],
        "status": "pending",
        "error": None,
    }
    _prune()
    params = build_picker_params(
        client_id=client_id,
        redirect=redirect,
        state=state,
        file_ids=file_ids,
        allow_multiple=allow_multiple,
        allow_folders=allow_folders,
        mime_types=mime_types,
        login_hint=account.get("email") or "",
    )
    return {
        "authorize_url": f"{AUTH_ENDPOINT}?{urlencode(params)}",
        "state": state,
        "capture_hint": capture_hint(),
        "local_capture": capture_is_local(),
    }


def record_redirect(query: str) -> Dict[str, Any]:
    """Record a captured Picker redirect. Called by the callback route."""
    params = parse_qs(query or "")
    state = (params.get("state") or [""])[0]
    pend = _pending.get(state)
    if not pend:
        raise DriveGrantError("unknown or expired grant state")
    if "error" in params:
        pend["status"] = "error"
        pend["error"] = "Google consent was denied or returned an error."
        return {"profile": pend["profile"], "state": state, "status": "error"}
    raw = (params.get("picked_file_ids") or [""])[0]
    pend["picked"] = [p for p in (part.strip() for part in raw.split(",")) if p]
    pend["status"] = "captured"
    logger.info(
        f"[drive] picker returned {len(pend['picked'])} file(s) for profile={pend['profile']}"
    )
    return {"profile": pend["profile"], "state": state, "status": "captured"}


def complete_from_redirect_url(profile: str, redirect_url: str) -> Dict[str, Any]:
    """Finish a grant from a redirect URL the user pasted."""
    raw = (redirect_url or "").strip()
    if not raw:
        raise DriveGrantError("Paste the full URL your browser was redirected to.")
    query = urlsplit(raw).query or (raw[1:] if raw.startswith("?") else raw)
    params = parse_qs(query)
    state = (params.get("state") or [""])[0]
    pend = _pending.get(state)
    if not pend:
        raise DriveGrantError(
            "That redirect does not match a grant started from this server (it may "
            "have expired). Start a new grant and try again."
        )
    if pend["profile"] != profile:
        raise DriveGrantError("That redirect belongs to a different profile.")
    record_redirect(query)
    return poll_status(profile, state)


def poll_status(profile: str, state: str) -> Dict[str, Any]:
    """Report what a grant round achieved.

    Reports captured picks when the redirect arrived, and always diffs the
    reachable set against the pre-grant snapshot so newly granted files surface
    even when the redirect never came back.
    """
    pend = _pending.get(state)
    if not pend or pend["profile"] != profile:
        return {"status": "unknown", "files": []}
    if pend["status"] == "error":
        return {"status": "error", "error": pend["error"], "files": []}

    try:
        now_ids = skill_token.reachable_ids(profile)
    except skill_token.DriveTokenError as exc:
        return {"status": pend["status"], "files": [], "error": str(exc)}

    added = sorted(now_ids - set(pend["before"]))
    picked = list(pend["picked"])
    ids = picked or added
    files: List[Dict[str, Any]] = []
    for fid in ids:
        meta = skill_token.get_file(profile, fid)
        if meta:
            files.append(meta)

    if files:
        skill_token.record_grants(profile, files)
        pend["status"] = "completed"
        # Consume the round's findings so polling again reports only what is new
        # since: fold the files into the baseline and drop the captured picks.
        pend["before"] = now_ids
        pend["picked"] = []
    result: Dict[str, Any] = {"status": pend["status"], "files": files}
    if picked and len(files) < len(picked):
        result["unverified"] = [fid for fid in picked if fid not in {f["id"] for f in files}]
        result["note"] = (
            "Some picked files could not be read back. Confirm the approval used the "
            "linked Google account."
        )
    return result


def cancel(profile: str, state: str) -> None:
    pend = _pending.get(state)
    if pend and pend["profile"] == profile:
        _pending.pop(state, None)


def abandon_rounds(profile: str) -> int:
    """Drop every in-flight Picker round belonging to ``profile``. Returns the count.

    Called when the gdrive link is torn down: a surviving round would keep polling
    :func:`poll_status`, which diffs ``reachable_ids`` against a token that no
    longer exists. Rounds are keyed by OAuth ``state`` with the profile inside the
    value, so this filters rather than clearing the whole dict — another profile's
    round must be untouched.
    """
    doomed = [state for state, pend in _pending.items() if pend.get("profile") == profile]
    for state in doomed:
        _pending.pop(state, None)
    return len(doomed)
