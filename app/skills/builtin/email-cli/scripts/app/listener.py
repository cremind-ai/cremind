import errno
import imaplib
import json
import os
import re
import signal
import socket
import ssl
import time
from pathlib import Path

from . import config, formatter, message
from .imap_client import ImapClient, ImapError


log = config.setup_logging()


class _Reconnect(Exception):
    pass


class _Shutdown(Exception):
    pass


_shutdown = False


def _install_signal_handlers() -> None:
    def handler(signum, frame):
        global _shutdown
        _shutdown = True

    try:
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError):
        pass


def _touch_heartbeat() -> None:
    try:
        config.HEARTBEAT_FILE.touch()
    except OSError as e:
        log.debug("failed to touch heartbeat: %s", e)


def _remove_heartbeat() -> None:
    try:
        config.HEARTBEAT_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _save_state(state: dict) -> None:
    tmp = config.STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(state, f)
    os.replace(tmp, config.STATE_FILE)


def _baseline_state(c: ImapClient) -> dict:
    uidnext = c.uidnext() or 1
    return {
        "last_seen_uid": max(uidnext - 1, 0),
        "uidvalidity": c.uidvalidity or 0,
    }


_WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _sanitize_subject(subject: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", subject or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".")
    cleaned = cleaned[:100].rstrip()
    if not cleaned:
        cleaned = "no-subject"
    if cleaned.lower() in _WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def _write_event(events_dir: Path, parsed: message.ParsedMessage) -> Path:
    events_dir.mkdir(parents=True, exist_ok=True)
    content = formatter.format_event_markdown(parsed)
    base = _sanitize_subject(parsed.subject)
    attempt = 0
    while True:
        name = f"{base}.md" if attempt == 0 else f"{base} ({attempt + 1}).md"
        path = events_dir / name
        try:
            fd = os.open(
                str(path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o644,
            )
        except OSError as e:
            if e.errno == errno.EEXIST:
                attempt += 1
                continue
            raise
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        return path


def _process_new_uids(c: ImapClient, state: dict, events_dir: Path) -> int:
    last = int(state.get("last_seen_uid", 0))
    uids = c.uid_search("UID", f"{last + 1}:*")
    new_uids = sorted(u for u in uids if u > last)
    for uid in new_uids:
        try:
            raw, gm_msgid, labels = c.fetch_raw(uid)
        except ImapError as e:
            log.warning("fetch UID %s failed: %s", uid, e)
            state["last_seen_uid"] = uid
            _save_state(state)
            continue
        if not raw:
            state["last_seen_uid"] = uid
            _save_state(state)
            continue
        parsed = message.parse(raw, gm_msgid=gm_msgid, labels=labels)
        path = _write_event(events_dir, parsed)
        log.info("wrote %s (subject=%r, from=%r)", path.name, parsed.subject, parsed.from_)
        state["last_seen_uid"] = uid
        _save_state(state)
    return len(new_uids)


def _connect_cycle(state: dict, events_dir: Path) -> None:
    with ImapClient() as c:
        c.select("INBOX", readonly=False)
        if state.get("uidvalidity") and c.uidvalidity and c.uidvalidity != state["uidvalidity"]:
            log.warning(
                "UIDVALIDITY changed (%s -> %s); rebaselining",
                state["uidvalidity"], c.uidvalidity,
            )
            state.update(_baseline_state(c))
            _save_state(state)
        elif not state.get("uidvalidity"):
            state["uidvalidity"] = c.uidvalidity or 0
            _save_state(state)

        connected_at = time.monotonic()
        while not _shutdown:
            _touch_heartbeat()
            n = _process_new_uids(c, state, events_dir)
            if n:
                log.debug("processed %s new message(s); last_seen_uid=%s",
                          n, state["last_seen_uid"])
            try:
                c.noop()
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError, ssl.SSLError):
                raise _Reconnect()
            if time.monotonic() - connected_at > config.RECONNECT_MAX_SECONDS:
                log.info("proactive reconnect after %ss", config.RECONNECT_MAX_SECONDS)
                raise _Reconnect()
            for _ in range(config.POLL_INTERVAL):
                if _shutdown:
                    return
                time.sleep(1)


def run() -> None:
    _install_signal_handlers()
    events_dir = config.NEW_EMAIL_DIR
    events_dir.mkdir(parents=True, exist_ok=True)

    # Always baseline on startup so we only react to emails that arrive while
    # the listener is running. Backlog accumulated during Cremind downtime is
    # intentionally skipped to avoid firing subscriptions for stale events.
    log.info("baselining from current UIDNEXT on startup")
    with ImapClient() as c:
        c.select("INBOX", readonly=True)
        state = _baseline_state(c)
    _save_state(state)
    log.info("baseline: last_seen_uid=%s uidvalidity=%s",
             state["last_seen_uid"], state["uidvalidity"])

    backoff = 10
    while not _shutdown:
        try:
            _connect_cycle(state, events_dir)
        except _Reconnect:
            backoff = 10
        except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError,
                ssl.SSLError, socket.error, ImapError) as e:
            log.warning("connection error: %s; reconnecting in %ss", e, backoff)
            for _ in range(backoff):
                if _shutdown:
                    break
                time.sleep(1)
            backoff = min(backoff * 2, 60)
            continue
        except Exception as e:
            log.exception("unexpected error in listener: %s", e)
            for _ in range(backoff):
                if _shutdown:
                    break
                time.sleep(1)
            backoff = min(backoff * 2, 60)
            continue
    _remove_heartbeat()
    log.info("listener shut down cleanly")
