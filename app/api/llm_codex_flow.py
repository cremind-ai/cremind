"""In-process orchestration for the Codex "Sign in with ChatGPT" browser flow.

OpenAI's Codex OAuth client has a **fixed** redirect URI —
``http://localhost:1455/auth/callback`` — so the authorization ``code`` can only
be captured by a listener bound to port 1455 on the machine running the browser.
For a local (native) install that is this backend, so ``start_flow`` spins up a
tiny loopback HTTP server on ``127.0.0.1:1455`` that catches the redirect,
exchanges the code, and stores the tokens.

When that listener can't run — the port is busy (e.g. the Codex CLI is mid-login)
or the server is remote (Docker/K8s, where the browser's ``localhost`` isn't the
server) — ``start_flow`` still succeeds with ``listener_active=False`` and the UI
falls back to letting the user paste the redirect URL, which
``complete_from_redirect_url`` finishes server-side.

State lives only in-process (a restart drops in-flight flows, like the Calendar
connect flow). Nothing here is persisted except, on success, the token rows
written by :func:`app.lib.llm.codex_auth.persist_token_response`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlsplit

from app.api.oauth_callback import _STATE_RE
from app.lib.llm import codex_auth
from app.lib.llm.codex_auth import CodexAuthError
from app.utils.logger import logger

_PENDING_TTL = 600.0  # 10 minutes

# state -> {profile, verifier, config_storage, created_at, status, error,
#           email, plan_type, account_id}
_pending: Dict[str, Dict[str, Any]] = {}

_listener: Optional[asyncio.AbstractServer] = None
_listener_port: Optional[int] = None
_listener_timeout_task: Optional[asyncio.Task] = None
# Fire-and-forget cleanup tasks (kept referenced so they aren't GC'd mid-flight).
_bg_tasks: set = set()


def _schedule(coro) -> None:
    """Run ``coro`` detached. Used so a connection handler can trigger listener
    shutdown without awaiting it inside its own call stack (which would deadlock
    on ``Server.wait_closed`` waiting for that very handler)."""
    try:
        task = asyncio.ensure_future(coro)
    except RuntimeError:
        coro.close()
        return
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)

_SUCCESS_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Cremind</title></head>"
    "<body style='font-family:sans-serif;text-align:center;padding-top:3rem'>"
    "<h1>Signed in to ChatGPT</h1>"
    "<p>You can close this window and return to Cremind.</p>"
    "<script>setTimeout(function(){window.close()},2000)</script></body></html>"
)
_ERROR_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Cremind</title></head>"
    "<body style='font-family:sans-serif;text-align:center;padding-top:3rem'>"
    "<h1>Sign-in failed</h1>"
    "<p>You can close this window and try signing in again from Cremind.</p></body></html>"
)


def _prune() -> None:
    now = time.time()
    for state in [s for s, p in _pending.items() if now - p.get("created_at", 0) > _PENDING_TTL]:
        _pending.pop(state, None)


# ── loopback listener ──────────────────────────────────────────────────────

def _http_response(status_line: str, html: str) -> bytes:
    body = html.encode("utf-8")
    head = (
        f"HTTP/1.1 {status_line}\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode("latin-1") + body


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Serve one loopback callback request. Never raises out — a crash here would
    take down the serving task."""
    try:
        data = b""
        try:
            while b"\r\n\r\n" not in data and len(data) < 16384:
                chunk = await asyncio.wait_for(reader.read(1024), timeout=10.0)
                if not chunk:
                    break
                data += chunk
        except asyncio.TimeoutError:
            pass

        request_line = data.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = request_line.split(" ")
        method = parts[0] if parts else ""
        target = parts[1] if len(parts) > 1 else "/"

        if method != "GET":
            writer.write(_http_response("405 Method Not Allowed", _ERROR_HTML))
            await writer.drain()
            return

        split = urlsplit(target)
        if split.path not in ("/auth/callback", "/callback"):
            writer.write(_http_response("404 Not Found", _ERROR_HTML))
            await writer.drain()
            return

        params = parse_qs(split.query)
        state = (params.get("state") or [""])[0]
        error = (params.get("error") or [""])[0]
        code = (params.get("code") or [""])[0]

        ok = False
        if error:
            _mark_error(state, (params.get("error_description") or [error])[0])
        elif not state or not _STATE_RE.match(state) or not code:
            logger.warning("[codex-flow] loopback callback with missing/invalid state or code")
        else:
            result = await complete_flow(state, code)
            ok = result.get("status") == "complete"

        writer.write(_http_response("200 OK", _SUCCESS_HTML if ok else _ERROR_HTML))
        await writer.drain()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[codex-flow] loopback handler error: {exc}")
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def _start_listener(port: int) -> tuple[bool, Optional[str]]:
    """Start (or reuse) the loopback listener. Returns ``(active, error)``."""
    global _listener, _listener_port, _listener_timeout_task
    if _listener is not None:
        return True, None
    try:
        _listener = await asyncio.start_server(_handle_client, "127.0.0.1", port)
        _listener_port = port
    except OSError as exc:
        _listener = None
        _listener_port = None
        logger.info(f"[codex-flow] could not bind 127.0.0.1:{port}: {exc}")
        return False, (
            f"Port {port} is already in use (the Codex CLI may be signing in). "
            "Complete sign-in by pasting the redirect URL instead."
        )

    async def _timeout() -> None:
        try:
            await asyncio.sleep(_PENDING_TTL)
        except asyncio.CancelledError:
            return
        await stop_listener()

    _listener_timeout_task = asyncio.ensure_future(_timeout())
    logger.info(f"[codex-flow] loopback listener bound on 127.0.0.1:{port}")
    return True, None


async def stop_listener() -> None:
    global _listener, _listener_port, _listener_timeout_task
    server, _listener = _listener, None
    _listener_port = None
    task, _listener_timeout_task = _listener_timeout_task, None
    if task is not None:
        try:
            if not task.done():
                task.cancel()
        except Exception:  # noqa: BLE001
            pass
    if server is not None:
        try:
            server.close()
            await server.wait_closed()
        except Exception:  # noqa: BLE001
            # Server may belong to an already-closed loop (e.g. between tests) —
            # the globals are already reset, so it's safe to ignore.
            pass


async def _maybe_stop_listener() -> None:
    """Stop the listener once no flows are still waiting for a callback."""
    if not any(p.get("status") == "pending" for p in _pending.values()):
        await stop_listener()


# ── public flow API ────────────────────────────────────────────────────────

async def start_flow(config_storage, profile: str, *, port: int = codex_auth.CODEX_CALLBACK_PORT) -> Dict[str, Any]:
    """Begin a sign-in flow: register PKCE state and start the loopback listener."""
    _prune()
    verifier, challenge = codex_auth.generate_pkce()
    state = codex_auth.generate_state()
    _pending[state] = {
        "profile": profile,
        "verifier": verifier,
        "config_storage": config_storage,
        "created_at": time.time(),
        "status": "pending",
        "error": None,
        "email": None,
        "plan_type": None,
        "account_id": None,
    }
    listener_active, listener_error = await _start_listener(port)
    return {
        "authorize_url": codex_auth.build_authorize_url(state, challenge),
        "state": state,
        "redirect_uri": codex_auth.CODEX_REDIRECT_URI,
        "listener_active": listener_active,
        "listener_error": listener_error,
        "expires_in": int(_PENDING_TTL),
    }


def get_flow_status(state: str, profile: str) -> Dict[str, Any]:
    """Report the status of a pending flow. Unknown/expired/foreign → ``expired``."""
    _prune()
    pend = _pending.get(state)
    if not pend or pend.get("profile") != profile:
        return {"status": "expired"}
    status = pend.get("status", "pending")
    if status == "complete":
        return {
            "status": "complete",
            "email": pend.get("email"),
            "plan_type": pend.get("plan_type"),
            "account_id": pend.get("account_id"),
        }
    if status == "error":
        return {"status": "error", "error": pend.get("error") or "Sign-in failed"}
    return {"status": "pending"}


def _mark_error(state: str, message: str) -> None:
    pend = _pending.get(state)
    if pend is not None:
        pend["status"] = "error"
        pend["error"] = message


async def complete_flow(state: str, code: str) -> Dict[str, Any]:
    """Exchange ``code`` for tokens and persist them. Shared by the loopback
    listener and the paste-URL endpoint. Records the outcome on the pending
    entry so a UI poll can observe it; never raises."""
    pend = _pending.get(state)
    if not pend:
        return {"status": "error", "error": "Unknown or expired sign-in request."}
    if pend.get("status") == "complete":
        return {
            "status": "complete",
            "email": pend.get("email"),
            "plan_type": pend.get("plan_type"),
            "account_id": pend.get("account_id"),
        }

    config_storage = pend["config_storage"]
    profile = pend["profile"]
    try:
        tok = await codex_auth.exchange_code(code, pend["verifier"])
        creds = codex_auth.persist_token_response(config_storage, profile, tok)
        config_storage.set("llm_config", "openai.auth_method", "codex_oauth", is_secret=False, profile=profile)
    except CodexAuthError as exc:
        logger.warning(f"[codex-flow] token exchange failed for state={state[:6]}…: {exc}")
        _mark_error(state, str(exc))
        return {"status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[codex-flow] unexpected exchange error: {exc}")
        _mark_error(state, "Sign-in failed. Please try again.")
        return {"status": "error", "error": "Sign-in failed. Please try again."}

    pend["status"] = "complete"
    pend["email"] = creds.email
    pend["plan_type"] = creds.plan_type
    pend["account_id"] = creds.account_id
    # Switching OpenAI to the Codex backend changes the servable model set. Clear
    # any model group left pointing at an API-key-only model (e.g. a stale
    # ``model_group.low = openai/gpt-4.1-mini``) so it falls back to the high group
    # instead of 4xx-ing at request time — the same reconciliation the Settings
    # provider PATCH performs. Non-fatal: a cleanup failure must not undo a
    # successful sign-in (the resolution-time guard in ModelGroupManager also
    # self-heals any residual stale value).
    try:
        from app.lib.llm.model_group_reconcile import reconcile_model_groups_for_auth
        cleared = reconcile_model_groups_for_auth(
            config_storage, "openai", "codex_oauth", profile,
        )
        if cleared:
            logger.info(
                f"[codex-flow] cleared stale model group(s) after ChatGPT "
                f"sign-in (profile={profile}): {cleared}"
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[codex-flow] model-group reconciliation failed: {exc}")
    try:
        from app.events.settings_state_bus import publish_settings_state_changed
        publish_settings_state_changed(profile)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[codex-flow] settings-state publish failed: {exc}")
    logger.info(f"[codex-flow] ChatGPT sign-in complete for profile={profile} email={creds.email}")
    # Detached: if called from inside the loopback connection handler, awaiting
    # stop_listener here would deadlock (wait_closed waits for this handler).
    _schedule(_maybe_stop_listener())
    return {
        "status": "complete",
        "email": creds.email,
        "plan_type": creds.plan_type,
        "account_id": creds.account_id,
    }


async def complete_from_redirect_url(profile: str, redirect_url: str, state_hint: Optional[str] = None) -> Dict[str, Any]:
    """Finish a flow from a pasted redirect URL (remote/port-busy fallback).

    Accepts either a full URL (``http://localhost:1455/auth/callback?...``) or a
    bare query string (``code=...&state=...``). The pasted ``state`` must match a
    pending flow owned by ``profile``.
    """
    if not redirect_url or not redirect_url.strip():
        return {"status": "error", "error": "Paste the full redirect URL."}
    raw = redirect_url.strip()
    split = urlsplit(raw)
    query = split.query or (raw if "=" in raw and "://" not in raw else "")
    params = parse_qs(query)

    error = (params.get("error") or [""])[0]
    if error:
        return {"status": "error", "error": (params.get("error_description") or [error])[0]}

    state = (params.get("state") or [""])[0]
    code = (params.get("code") or [""])[0]
    if not state or not code:
        return {"status": "error", "error": "The pasted URL is missing the code or state."}
    if state_hint and state_hint != state:
        return {"status": "error", "error": "The pasted URL does not match this sign-in request."}
    if not _STATE_RE.match(state):
        return {"status": "error", "error": "The pasted URL has an invalid state."}

    pend = _pending.get(state)
    if not pend or pend.get("profile") != profile:
        return {"status": "error", "error": "Unknown or expired sign-in request. Start again."}

    return await complete_flow(state, code)


async def cancel_flow(state: str, profile: str) -> None:
    pend = _pending.get(state)
    if pend is not None and pend.get("profile") == profile:
        _pending.pop(state, None)
    await _maybe_stop_listener()
