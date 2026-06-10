"""Gmail event listener.

Architecture (token-less relay):
  1. Establish a Gmail users.watch() into the org's Pub/Sub topic (from discovery),
     using THIS user's own token. Renew it well within the 7-day limit.
  2. Connect a WebSocket to the cremind-connect relay and prove account control
     with a fresh Google ID token.
  3. On each `resync` nudge (and once on startup), run an incremental
     history.list() and drop new INBOX messages as markdown into
     events/new_email/ for the cremind file-watcher to pick up.

The relay only sends a nudge; all message content is fetched here with the local
token. No tokens ever leave this machine.
"""
from __future__ import annotations

import errno
import json
import os
import re
import signal
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config, formatter, gmail_api
from .google import auth
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


# --- state ---

def _load_state() -> dict[str, Any]:
    if not config.STATE_FILE.exists():
        return {"account_key": "", "history_id": "", "watch_expiration": 0, "emitted_ids": []}
    try:
        data = json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("history_id", "")
            data.setdefault("watch_expiration", 0)
            data.setdefault("emitted_ids", [])
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"account_key": "", "history_id": "", "watch_expiration": 0, "emitted_ids": []}


def _save_state(state: dict[str, Any]) -> None:
    tmp = config.STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, config.STATE_FILE)


# --- event file writing (atomic, mirrors caldav-calendar) ---

_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def _sanitize(part: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", part or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".")[:100].rstrip()
    if not cleaned:
        cleaned = "no-subject"
    if cleaned.lower() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def _write_event(parsed: dict[str, Any]) -> Path:
    config.NEW_EMAIL_DIR.mkdir(parents=True, exist_ok=True)
    content = formatter.format_email_markdown(parsed, event_type="new_email")
    base = f"{datetime.now().strftime('%Y-%m-%dT%H-%M-%S')} {_sanitize(parsed.get('subject') or '')}"
    attempt = 0
    while True:
        name = f"{base}.md" if attempt == 0 else f"{base} ({attempt + 1}).md"
        path = config.NEW_EMAIL_DIR / name
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

def _added_message_ids(records: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for rec in records:
        for added in rec.get("messagesAdded", []) or []:
            msg = added.get("message", {})
            labels = msg.get("labelIds", []) or []
            if "INBOX" not in labels:
                continue
            mid = msg.get("id")
            if mid and mid not in seen:
                seen.add(mid)
                ordered.append(mid)
    return ordered


def _sync(state: dict[str, Any]) -> int:
    """Incremental sync from the stored historyId. Returns count emitted."""
    with _sync_lock:
        creds, _ = auth.get_credentials(config.TOKEN_PATH)
        svc = gmail_api.build_service(creds)
        start = state.get("history_id") or ""
        if not start:
            state["history_id"] = str(gmail_api.get_profile(svc).get("historyId", ""))
            _save_state(state)
            return 0

        from googleapiclient.errors import HttpError

        try:
            records = gmail_api.list_history(svc, start)
        except HttpError as e:
            if getattr(e, "resp", None) is not None and e.resp.status == 404:
                # historyId too old (offline > ~7 days). Reset the cursor; bounded
                # gap is accepted by design (no full-mailbox replay).
                new_id = str(gmail_api.get_profile(svc).get("historyId", ""))
                log.warning("history too old; cursor reset to %s (gap not replayed)", new_id)
                state["history_id"] = new_id
                _save_state(state)
                return 0
            raise

        # Idempotency: Gmail/Pub/Sub deliver at-least-once (duplicate nudges) and
        # history.list's startHistoryId is inclusive, so the same message id can
        # resurface across syncs. Track emitted ids and skip any already written,
        # so each email yields exactly one .md file.
        emitted_ids: list[str] = state.get("emitted_ids", [])
        seen_ids = set(emitted_ids)
        emitted = 0
        for mid in _added_message_ids(records):
            if mid in seen_ids:
                continue
            try:
                msg = gmail_api.get_message(svc, mid, fmt="full")
                _write_event(formatter.parse_message(msg))
            except HttpError as e:
                log.warning("failed to fetch message %s: %s", mid, e)
                continue
            seen_ids.add(mid)
            emitted_ids.append(mid)
            emitted += 1
        # Bound the dedup memory to the most recent ids.
        state["emitted_ids"] = emitted_ids[-1000:]

        # Advance the cursor to the latest history id we saw (or the profile's).
        latest = ""
        for rec in records:
            rid = rec.get("id")
            if rid:
                latest = rid
        if latest:
            state["history_id"] = str(latest)
        else:
            state["history_id"] = str(gmail_api.get_profile(svc).get("historyId", state["history_id"]))
        _save_state(state)
        if emitted:
            log.info("emitted %d new email(s)", emitted)
        return emitted


# --- watch lifecycle ---

def _ensure_watch(state: dict[str, Any], topic: str) -> None:
    creds, _ = auth.get_credentials(config.TOKEN_PATH)
    svc = gmail_api.build_service(creds)
    resp = gmail_api.watch(svc, topic)
    # watch() returns the current historyId + an expiration (ms epoch).
    hist = str(resp.get("historyId", ""))
    if not state.get("history_id") and hist:
        state["history_id"] = hist
    state["watch_expiration"] = int(resp.get("expiration", 0))
    _save_state(state)
    log.info("gmail watch established (expires %s)", state["watch_expiration"])


def _renew_loop(state: dict[str, Any], topic: str) -> None:
    while not _shutdown.is_set():
        if _shutdown.wait(timeout=config.WATCH_RENEW_INTERVAL):
            return
        try:
            _ensure_watch(state, topic)
        except Exception as e:
            log.warning("watch renewal failed: %s", e)


def run() -> None:
    _install_signal_handlers()
    if not _acquire_single_instance():
        log.error(
            "another gmail listener is already running for this skill (lock: %s); "
            "exiting to avoid duplicate event files", config.LOCK_FILE
        )
        raise SystemExit(1)
    config.NEW_EMAIL_DIR.mkdir(parents=True, exist_ok=True)

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
        topic = disc.gmail_topic()
        ws_url = disc.ws_url()
    except Exception as e:
        log.error("discovery failed: %s", e)
        raise SystemExit(1)

    state = _load_state()
    state["account_key"] = account_key

    # Baseline historyId on first ever run (emit nothing for pre-existing mail).
    if not state.get("history_id"):
        try:
            creds, _ = auth.get_credentials(config.TOKEN_PATH)
            svc = gmail_api.build_service(creds)
            state["history_id"] = str(gmail_api.get_profile(svc).get("historyId", ""))
            _save_state(state)
            log.info("baselined historyId on startup")
        except Exception as e:
            log.warning("baseline failed: %s", e)

    _ensure_watch(state, topic)
    threading.Thread(target=_renew_loop, args=(state, topic), daemon=True).start()

    # Catch up anything that arrived while we were offline, then go live.
    try:
        _sync(state)
    except Exception as e:
        log.warning("initial catch-up sync failed: %s", e)

    relay = RelayClient(
        ws_url=ws_url,
        account_key=account_key,
        resources=["gmail"],
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
    log.info("listening for gmail events (account_key=%s)", account_key)
    try:
        while relay_thread.is_alive() and not _shutdown.is_set():
            relay_thread.join(timeout=0.5)
    except KeyboardInterrupt:
        pass
    _shutdown.set()
    relay.stop()
    relay_thread.join(timeout=5)
    log.info("gmail listener shut down")


def _safe_sync(state: dict[str, Any]) -> None:
    try:
        _sync(state)
    except Exception as e:
        log.warning("sync failed: %s", e)
