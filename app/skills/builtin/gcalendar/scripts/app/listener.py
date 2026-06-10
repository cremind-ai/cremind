"""Google Calendar event listener.

Architecture (token-less relay):
  1. events.watch() a push channel whose id encodes the routing key
     (cm.<accountKey>.<nonce>) pointing at the org webhook (from discovery),
     using THIS user's own token. Renew well within the channel's expiry.
  2. Connect a WebSocket to the cremind-connect relay and prove account control
     with a fresh Google ID token.
  3. On each `resync` nudge (and once on startup), run an incremental
     events.list(syncToken) and drop changed events as markdown into
     events/event_changed/.
"""
from __future__ import annotations

import errno
import json
import os
import re
import signal
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config, formatter, gcal_api
from .google import auth
from .google.account_key import base32_encode
from .google.discovery import Discovery
from .google.relay_client import RelayClient

log = config.setup_logging()

_shutdown = threading.Event()
_sync_lock = threading.Lock()


def _install_signal_handlers() -> None:
    def handler(signum, frame):
        _shutdown.set()

    try:
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError):
        pass


_instance_lock = None  # held open for the process lifetime to enforce single-instance


def _acquire_single_instance() -> bool:
    """Ensure only ONE listener runs per skill dir. Takes an exclusive OS lock that
    the kernel releases when this process dies (even on crash/force-kill), so there
    are never stale locks. Returns False if another instance already holds it."""
    global _instance_lock
    try:
        f = open(config.LOCK_FILE, "a+")
    except OSError:
        return True  # can't create the lock file; don't block startup
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _instance_lock = f
    return True


def _random_nonce(n: int = 16) -> str:
    return base32_encode(os.urandom(16))[:n]


def _channel_id(account_key: str) -> str:
    # Must match the relay's parser: cm-<26 base32>-<nonce>. Separators are "-"
    # (NOT "."): Google Calendar rejects channel ids containing dots.
    return f"cm-{account_key}-{_random_nonce()}"


# --- state ---

def _load_state() -> dict[str, Any]:
    if not config.STATE_FILE.exists():
        return {}
    try:
        data = json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    tmp = config.STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, config.STATE_FILE)


# --- event file writing ---

_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def _sanitize(part: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", part or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".")[:100].rstrip()
    if not cleaned:
        cleaned = "no-summary"
    if cleaned.lower() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def _write_event(ev: dict[str, Any]) -> Path:
    config.EVENT_CHANGED_DIR.mkdir(parents=True, exist_ok=True)
    content = formatter.format_event_markdown(ev, event_type="event_changed", calendar=config.CALENDAR_ID)
    summary = ev.get("summary") or ("(cancelled)" if ev.get("status") == "cancelled" else "")
    base = f"{datetime.now().strftime('%Y-%m-%dT%H-%M-%S')} {_sanitize(summary)}"
    attempt = 0
    while True:
        name = f"{base}.md" if attempt == 0 else f"{base} ({attempt + 1}).md"
        path = config.EVENT_CHANGED_DIR / name
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except OSError as e:
            if e.errno == errno.EEXIST:
                attempt += 1
                continue
            raise
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        return path


# --- sync ---

def _sync(state: dict[str, Any]) -> int:
    with _sync_lock:
        from googleapiclient.errors import HttpError

        creds, _ = auth.get_credentials(config.TOKEN_PATH)
        svc = gcal_api.build_service(creds)
        sync_token = state.get("sync_token") or ""
        if not sync_token:
            state["sync_token"] = gcal_api.initial_sync_token(svc, calendar_id=config.CALENDAR_ID)
            _save_state(state)
            return 0
        try:
            changes, new_token = gcal_api.incremental_changes(
                svc, calendar_id=config.CALENDAR_ID, sync_token=sync_token
            )
        except HttpError as e:
            if getattr(e, "resp", None) is not None and e.resp.status == 410:
                log.warning("syncToken expired (410); re-baselining (gap not replayed)")
                state["sync_token"] = gcal_api.initial_sync_token(svc, calendar_id=config.CALENDAR_ID)
                _save_state(state)
                return 0
            raise

        emitted = 0
        for ev in changes:
            try:
                _write_event(ev)
                emitted += 1
            except OSError as e:
                log.warning("failed to write event %s: %s", ev.get("id"), e)
        state["sync_token"] = new_token
        _save_state(state)
        if emitted:
            log.info("emitted %d changed event(s)", emitted)
        return emitted


# --- watch lifecycle ---

def _ensure_watch(state: dict[str, Any], webhook_url: str, account_key: str) -> None:
    creds, _ = auth.get_credentials(config.TOKEN_PATH)
    svc = gcal_api.build_service(creds)

    # Stop the previous channel (best effort) before opening a new one.
    old_id, old_res = state.get("channel_id"), state.get("resource_id")
    if old_id and old_res:
        try:
            gcal_api.stop_channel(svc, channel_id=old_id, resource_id=old_res)
        except Exception as e:
            log.debug("stop old channel failed (ignored): %s", e)

    channel_id = _channel_id(account_key)
    token = _random_nonce(24)  # opaque unless the relay runs in HMAC mode
    resp = gcal_api.watch(
        svc,
        calendar_id=config.CALENDAR_ID,
        channel_id=channel_id,
        address=webhook_url,
        token=token,
    )
    state["channel_id"] = channel_id
    state["resource_id"] = resp.get("resourceId", "")
    state["watch_expiration"] = int(resp.get("expiration", 0))
    _save_state(state)
    log.info("calendar watch established (channel=%s, expires=%s)", channel_id, state["watch_expiration"])


def _renew_loop(state: dict[str, Any], webhook_url: str, account_key: str) -> None:
    while not _shutdown.is_set():
        if _shutdown.wait(timeout=config.WATCH_RENEW_INTERVAL):
            return
        try:
            _ensure_watch(state, webhook_url, account_key)
        except Exception as e:
            log.warning("watch renewal failed: %s", e)


def run() -> None:
    _install_signal_handlers()
    if not _acquire_single_instance():
        log.error(
            "another calendar listener is already running for this skill (lock: %s); "
            "exiting to avoid duplicate event files", config.LOCK_FILE
        )
        raise SystemExit(1)
    config.EVENT_CHANGED_DIR.mkdir(parents=True, exist_ok=True)

    # Wait for the account to be linked instead of exiting. This lets the listener
    # be started before `link` has run — it begins receiving events automatically as
    # soon as linking completes, with no restart. _shutdown.wait keeps Ctrl+C
    # responsive while waiting.
    data = None
    announced = False
    while not _shutdown.is_set():
        try:
            _, data = auth.get_credentials(config.TOKEN_PATH)
            break
        except auth.AuthError as e:
            if not announced:
                log.warning("%s — waiting; will start automatically once linked", e)
                announced = True
            _shutdown.wait(timeout=5)
    if data is None:
        return  # shutdown requested before the account was linked

    account_key = data["account_key"]
    disc = Discovery(config.CREMIND_CONNECT_URL)
    try:
        webhook_url = disc.calendar_webhook_url()
        ws_url = disc.ws_url()
    except Exception as e:
        log.error("discovery failed: %s", e)
        raise SystemExit(1)

    state = _load_state()
    state["account_key"] = account_key

    # Baseline syncToken on first run (emit nothing for pre-existing events).
    if not state.get("sync_token"):
        try:
            creds, _ = auth.get_credentials(config.TOKEN_PATH)
            svc = gcal_api.build_service(creds)
            state["sync_token"] = gcal_api.initial_sync_token(svc, calendar_id=config.CALENDAR_ID)
            _save_state(state)
            log.info("baselined syncToken on startup")
        except Exception as e:
            log.warning("baseline failed: %s", e)

    _ensure_watch(state, webhook_url, account_key)
    threading.Thread(target=_renew_loop, args=(state, webhook_url, account_key), daemon=True).start()

    try:
        _sync(state)
    except Exception as e:
        log.warning("initial catch-up sync failed: %s", e)

    relay = RelayClient(
        ws_url=ws_url,
        account_key=account_key,
        resources=["calendar"],
        id_token_provider=lambda: auth.fresh_id_token(config.TOKEN_PATH),
        on_resync=lambda _source: _safe_sync(state),
        logger=log,
    )
    # Run the websocket loop in a daemon thread and keep the MAIN thread parked in
    # an interruptible wait. If the main thread blocked inside relay.run_forever()'s
    # socket loop, Windows would never deliver SIGINT/KeyboardInterrupt and Ctrl+C
    # would be a no-op. Here the signal lands within ~0.5s; relay.stop() then closes
    # the live socket, and the daemon thread is killed on exit regardless.
    relay_thread = threading.Thread(target=relay.run_forever, name="relay", daemon=True)
    relay_thread.start()
    log.info("listening for calendar events (account_key=%s)", account_key)
    try:
        while relay_thread.is_alive() and not _shutdown.is_set():
            relay_thread.join(timeout=0.5)
    except KeyboardInterrupt:
        pass
    _shutdown.set()
    relay.stop()
    relay_thread.join(timeout=5)
    log.info("calendar listener shut down")


def _safe_sync(state: dict[str, Any]) -> None:
    try:
        _sync(state)
    except Exception as e:
        log.warning("sync failed: %s", e)
