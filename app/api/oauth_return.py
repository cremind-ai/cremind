"""The way back from an OAuth consent page to the Cremind page that opened it.

Google (and Atlassian) end a consent by redirecting the browser to one of the
backend callbacks in app/api/oauth_callback.py. What that tab should show next
depends on something the callback cannot see: which profile, and which page,
started the flow. Consent is opened from a Settings page, the Calendar page, the
Drive section or a link the agent posted in chat — sometimes as a popup,
sometimes as a plain tab, sometimes on an origin an HTTPS switch has since moved.
A static "you can close this window" page strands every case but the popup.

So the round trip is split into three steps, keyed so nothing replayable is kept:

1. When consent is opened, the SPA (or the Calendar/Drive start APIs) records
   ``{profile, route, flow}`` under ``sha256(state)`` — :func:`record_context`.
   The raw ``state`` never touches disk: it is the value that ties a callback to
   its flow, and a directory listing must not hand it out. The authorization
   ``code`` is never seen by this module at all.
2. The callback, after doing its real work (inbox write, token exchange, picker
   record), calls :func:`complete`. That claims-and-deletes the context and mints
   a fresh one-time ``ref``, and the browser is 303'd to
   ``/#/oauth-return?ref=<ref>`` — a URL carrying neither code nor state, so
   browser history, a Referer header or a screenshot of the address bar leaks
   nothing that could be replayed against the flow.
3. The SPA's oauth-return view POSTs the ref to :func:`consume`
   (unauthenticated: the consent tab may hold no session, and the unguessable
   ref is itself the capability), learns the outcome and where to go, then
   closes the popup or restores the original page.

Why disk rather than memory or the DB: a consent can straddle a restart (the
HTTPS switch restarts the server, so does an upgrade), and the callbacks are
registered PRE-storage (app/server.py), so a table could be unavailable exactly
when they answer. Records under ``<CREMIND_SYSTEM_DIR>/oauth_returns`` are 0600,
written atomically, expire after ten minutes (the window the skill inbox and the
Calendar flow already use), are pruned on every write and capped in number —
an abandoned flow costs a few hundred bytes for ten minutes at most.

Profile isolation: a context belongs to the profile whose session recorded it.
The first profile to record a state keeps it — another profile's attempt is
refused rather than overwriting — and the route is coerced under that profile's
own prefix, so a return can only ever lead back into the profile that started
the flow. The SPA additionally restores that route only when the browser holds
that profile's token; otherwise it falls back to Cremind home.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from app.api._auth import require_auth
from app.config.settings import BaseConfig
from app.utils import logger

#: Lifetime of a recorded context and of a minted ref.
TTL_S = 600
#: Records kept at once (contexts + refs). Reaching it evicts the oldest: losing
#: a context only costs the user the way back to their page, never the link.
MAX_RECORDS = 1024
FLOWS = frozenset({"skill", "calendar", "drive"})
#: ``received``: the response was captured (for Calendar, the backend exchange
#: also succeeded). ``denied``: the provider returned ``error=...``. ``failed``:
#: Cremind could not process the response. ``invalid``: the flow did not know
#: the state (typically an expired consent).
OUTCOMES = frozenset({"received", "denied", "failed", "invalid"})
#: The hash route the SPA mounts for this (see ui/src/router).
RETURN_PATH = "/#/oauth-return"

# Duplicated from app/api/oauth_callback.py rather than imported: that module
# imports this one, and the two must accept exactly the same states (a test
# pins them equal). Every pattern here is applied with ``fullmatch``: ``$``
# alone also matches before a trailing newline.
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
# ``secrets.token_urlsafe(32)``: always 43 characters of this alphabet.
_REF_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_PROFILE_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_ROUTE = 2048
_MAX_MESSAGE = 500
_NO_STORE = {"Cache-Control": "no-store"}
_REDIRECT_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}

# Serialises the read-check-write of record_context against complete/consume in
# this process; the os.replace claims below keep "exactly one consumer" true
# even beyond it.
_lock = threading.Lock()


def _now() -> float:
    return time.time()


def directory() -> Path:
    return Path(BaseConfig.CREMIND_SYSTEM_DIR) / "oauth_returns"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _context_path(state: str) -> Path:
    return directory() / f"ctx-{_digest(state)}.json"


def _ref_path(ref: str) -> Path:
    return directory() / f"ref-{_digest(ref)}.json"


def _write(path: Path, value: dict) -> None:
    """Atomic, owner-only write: a reader sees the old record or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path: Path) -> Optional[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _claim(path: Path) -> Optional[dict]:
    """Take ``path`` out of the store and return its record — at most once.

    ``os.replace`` to a unique name is the claim: of any number of concurrent
    callers exactly one moves the file, and every other one finds it gone.
    """
    claimed = path.with_name(f".{path.name}.{secrets.token_hex(8)}.claim")
    try:
        os.replace(path, claimed)
    except OSError:
        return None
    try:
        return _read(claimed)
    finally:
        claimed.unlink(missing_ok=True)


def _one_of(value: Any, allowed: frozenset) -> bool:
    # isinstance first: a JSON list/object is unhashable and would raise on ``in``.
    return isinstance(value, str) and value in allowed


def _live(record: Optional[dict]) -> bool:
    expires = (record or {}).get("expires_at")
    return (isinstance(expires, (int, float)) and not isinstance(expires, bool)
            and expires >= _now())


def _prune(folder: Path) -> None:
    """Drop expired records and crash leftovers, then the oldest over the cap.

    Judged by mtime rather than by opening every file: each record is written
    once (a same-profile re-record rewrites it whole), so its mtime is when its
    ``expires_at`` started counting.
    """
    now = _now()
    records: list[tuple[float, Path]] = []
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        path = folder / name
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime + TTL_S < now:
            path.unlink(missing_ok=True)
        elif name.endswith(".json") and not name.startswith("."):
            records.append((mtime, path))
    # Leave room for the record about to be written.
    excess = len(records) - (MAX_RECORDS - 1)
    if excess > 0:
        records.sort()
        for _mtime, path in records[:excess]:
            path.unlink(missing_ok=True)


def _normalise_route(route: Any, profile: str) -> str:
    """A local SPA route inside ``profile``'s own pages.

    Structurally unusable values (not a path, protocol-relative, backslashes,
    control characters, oversized) are refused. A well-formed route that points
    anywhere but this profile — another profile's prefix, the bare ``/``, a dot
    segment that a router might resolve out of the prefix — is coerced to the
    profile's home, so what was recorded can never lead into someone else's pages.
    """
    if (not isinstance(route, str) or len(route) > _MAX_ROUTE
            or not route.startswith("/") or route.startswith("//")
            or "\\" in route or _CONTROL_RE.search(route)):
        raise ValueError("A local application route is required.")
    path = route.split("?", 1)[0].split("#", 1)[0]
    segments = path.split("/")[1:]
    if (not segments or segments[0] != profile
            or any(unquote(segment) in (".", "..") for segment in segments)):
        return f"/{profile}"
    return route


def _clean_message(message: Any) -> Optional[str]:
    if not isinstance(message, str):
        return None
    text = _CONTROL_RE.sub(" ", message).strip()
    return text[:_MAX_MESSAGE] or None


def record_context(state: Any, *, profile: str, route: Any, flow: str) -> dict:
    """Remember which page of ``profile`` opened the consent carrying ``state``.

    Raises ``ValueError`` for an unusable state/profile/route/flow and
    ``PermissionError`` when another profile already recorded this state (first
    writer wins; the same profile re-recording simply overwrites).
    """
    if not isinstance(state, str) or not _STATE_RE.fullmatch(state):
        raise ValueError("A valid OAuth state is required.")
    if not isinstance(profile, str) or not _PROFILE_RE.fullmatch(profile):
        raise ValueError("A valid profile is required.")
    if not _one_of(flow, FLOWS):
        raise ValueError(f"flow must be one of: {', '.join(sorted(FLOWS))}.")
    route = _normalise_route(route, profile)
    path = _context_path(state)
    with _lock:
        existing = _read(path)
        if _live(existing) and existing.get("profile") != profile:
            raise PermissionError("This consent is already tracked for another profile.")
        _prune(path.parent)
        expires_at = int(_now()) + TTL_S
        _write(path, {"profile": profile, "route": route, "flow": flow,
                      "expires_at": expires_at})
    return {"recorded": True, "expires_at": expires_at}


def complete(state: Any, *, outcome: str, flow: str, message: Optional[str] = None) -> str:
    """Close the loop for ``state``: claim its context, mint the one-time ref.

    Called by the callbacks after they have done their work. The context is
    deleted in the same step, so a replayed callback (the same state again)
    finds none and gets a context-less ref: it can report an outcome but can no
    longer steer anyone to a page. ``flow`` is the callback's own — which route
    Google actually hit — not whatever the recorder guessed.
    """
    if not _one_of(outcome, OUTCOMES):
        raise ValueError(f"outcome must be one of: {', '.join(sorted(OUTCOMES))}.")
    if not _one_of(flow, FLOWS):
        raise ValueError(f"flow must be one of: {', '.join(sorted(FLOWS))}.")
    ref = secrets.token_urlsafe(32)
    folder = directory()
    with _lock:
        context = _claim(_context_path(state)) if isinstance(state, str) and state else None
        profile = route = None
        if _live(context):
            candidate = context.get("profile")
            if isinstance(candidate, str) and _PROFILE_RE.fullmatch(candidate):
                profile = candidate
                try:
                    route = _normalise_route(context.get("route"), profile)
                except ValueError:
                    route = f"/{profile}"
        folder.mkdir(parents=True, exist_ok=True)
        _prune(folder)
        _write(_ref_path(ref), {
            "outcome": outcome, "flow": flow, "message": _clean_message(message),
            "profile": profile, "route": route, "expires_at": int(_now()) + TTL_S,
        })
    return ref


def consume(ref: Any) -> dict:
    """Redeem a ref exactly once. Unknown, used or expired raises ``LookupError``."""
    if not isinstance(ref, str) or not _REF_RE.fullmatch(ref):
        raise LookupError("unknown")
    with _lock:
        record = _claim(_ref_path(ref))
    if not _live(record) or not _one_of(record.get("outcome"), OUTCOMES):
        raise LookupError("unknown")
    flow = record.get("flow")
    profile = record.get("profile")
    route = None
    if isinstance(profile, str) and _PROFILE_RE.fullmatch(profile):
        # Re-checked on the way out as well as in: this is what the SPA is
        # about to navigate to.
        try:
            route = _normalise_route(record.get("route"), profile)
        except ValueError:
            route = f"/{profile}"
    else:
        profile = None
    return {
        "outcome": record["outcome"],
        "flow": flow if _one_of(flow, FLOWS) else None,
        "profile": profile,
        "route": route,
        "message": _clean_message(record.get("message")),
    }


def return_redirect(ref: str) -> RedirectResponse:
    """303 the consent tab to the SPA's return view.

    Relative on purpose: the callback's own origin is the one the browser just
    proved it can reach (after an HTTPS switch, the HTTPS one), so the SPA opens
    there too. 303 turns whatever reached the callback into a plain GET.
    """
    return RedirectResponse(f"{RETURN_PATH}?ref={ref}", status_code=303,
                            headers=_REDIRECT_HEADERS)


def invalid_state_redirect() -> RedirectResponse:
    """For a callback whose ``state`` is missing or malformed: nothing to match."""
    return RedirectResponse(f"{RETURN_PATH}?error=invalid_state", status_code=303,
                            headers=_REDIRECT_HEADERS)


async def _json_object(request: Request) -> Optional[dict]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — empty body, bad JSON, wrong charset
        return None
    return body if isinstance(body, dict) else None


async def _handle_record_context(request: Request) -> JSONResponse:
    unauth = require_auth(request)
    if unauth is not None:
        return unauth
    profile = str(getattr(request.user, "username", "") or "")
    if not profile:
        return JSONResponse({"error": "A profile session is required."}, status_code=400)
    body = await _json_object(request)
    if body is None:
        return JSONResponse({"error": "A JSON object body is required."}, status_code=400)
    try:
        recorded = record_context(body.get("state"), profile=profile, route=body.get("route"),
                                  flow=body.get("flow") or "skill")
    except PermissionError:
        return JSONResponse({"error": "conflict"}, status_code=409)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except OSError as exc:
        logger.warning(f"[oauth-return] could not record a consent context: {exc}")
        return JSONResponse({"error": "unavailable"}, status_code=503)
    return JSONResponse(recorded, headers=_NO_STORE)


async def _handle_consume(request: Request) -> JSONResponse:
    body = await _json_object(request)
    if body is None:
        return JSONResponse({"error": "A JSON object body is required."}, status_code=400,
                            headers=_NO_STORE)
    try:
        result = consume(body.get("ref"))
    except LookupError:
        return JSONResponse({"error": "unknown"}, status_code=404, headers=_NO_STORE)
    return JSONResponse(result, headers=_NO_STORE)


def get_oauth_return_routes() -> list[Route]:
    """The SPA's half of the return: record on consent, consume on landing.

    Registered PRE-storage in app/server.py next to the callbacks they serve —
    a return view has to work whenever a callback can answer."""
    return [
        Route("/api/oauth/return/context", _handle_record_context, methods=["POST"]),
        Route("/api/oauth/return/consume", _handle_consume, methods=["POST"]),
    ]
