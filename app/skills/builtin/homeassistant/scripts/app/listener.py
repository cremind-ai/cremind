"""Persistent WebSocket listener that subscribes to Home Assistant state changes
and drops them as markdown events.

Connects directly to the instance's /api/websocket with a Long-Lived Access Token,
subscribes to `state_changed`, and writes one markdown file per (filtered) change.
No cloud relay — this mirrors the local imap-email / caldav-calendar listeners.
"""
from __future__ import annotations

import errno
import fnmatch
import json
import os
import re
import signal
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import websocket  # websocket-client

from . import classify, config, formatter
from .homeassistant_api import HaError, HaRestClient, HaWebSocketClient


log = config.setup_logging()


class _Reconnect(Exception):
    pass


_shutdown = False
_EMITTED_CAP = 500


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


def _empty_state() -> dict[str, Any]:
    return {"ha_url": "", "entities": {}, "emitted": []}


def _load_state() -> dict[str, Any]:
    if not config.STATE_FILE.exists():
        return _empty_state()
    try:
        with open(config.STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("state file unreadable (%s); starting fresh", e)
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    data.setdefault("ha_url", "")
    data.setdefault("entities", {})
    data.setdefault("emitted", [])
    return data


def _save_state(state: dict[str, Any]) -> None:
    tmp = config.STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(state, f)
    os.replace(tmp, config.STATE_FILE)


_WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _sanitize(part: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", part or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".")
    cleaned = cleaned[:100].rstrip()
    if not cleaned:
        cleaned = "entity"
    if cleaned.lower() in _WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def _filename_prefix() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


def _write_event(events_dir: Path, entity: dict[str, Any], event_type: str) -> Path:
    events_dir.mkdir(parents=True, exist_ok=True)
    content = formatter.format_event_markdown(entity, event_type=event_type)
    base = f"{_filename_prefix()} {_sanitize(entity.get('friendly_name') or entity.get('entity_id') or '')}"
    attempt = 0
    while True:
        name = f"{base}.md" if attempt == 0 else f"{base} ({attempt + 1}).md"
        path = events_dir / name
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


def _entity_matches(entity_id: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    return any(fnmatch.fnmatch(entity_id, p) for p in patterns)


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


def _record_emitted(state: dict[str, Any], marker: str) -> None:
    emitted = state.setdefault("emitted", [])
    emitted.append(marker)
    if len(emitted) > _EMITTED_CAP:
        del emitted[: len(emitted) - _EMITTED_CAP]


def _handle_state_changed(data: dict[str, Any], state: dict[str, Any]) -> Optional[str]:
    """Process one `state_changed` payload. Returns the emitted event type, or None."""
    entity_id = data.get("entity_id") or ""
    if not entity_id:
        return None
    if not _entity_matches(entity_id, config.HA_ENTITY_FILTER):
        return None
    new_state = data.get("new_state")
    if not new_state:
        # Entity was removed; nothing to emit.
        return None

    last_updated = new_state.get("last_updated") or ""
    entities = state.setdefault("entities", {})
    prev = entities.get(entity_id)
    if last_updated and prev and last_updated <= prev:
        return None
    marker = f"{entity_id}:{last_updated}"
    if marker in state.get("emitted", []):
        return None

    old_state = data.get("old_state")
    previous = old_state.get("state", "") if isinstance(old_state, dict) else ""
    attrs = new_state.get("attributes") or {}
    new_str = new_state.get("state", "")

    event_type = classify.classify(entity_id, attrs, previous, new_str)
    if event_type is None:
        # Unclassifiable churn (e.g. unknown<->unavailable); record so we don't re-eval.
        entities[entity_id] = last_updated
        _save_state(state)
        return None

    entity_dict = {
        "entity_id": entity_id,
        "friendly_name": attrs.get("friendly_name") or entity_id,
        "domain": _domain(entity_id),
        "state": new_str,
        "previous_state": previous,
        "last_changed": new_state.get("last_changed", ""),
        "last_updated": last_updated,
        "attributes": attrs,
    }
    path = _write_event(config.event_dir(event_type), entity_dict, event_type)
    log.info("wrote %s/%s (%s = %s)", event_type, path.name, entity_id, new_str)
    entities[entity_id] = last_updated
    _record_emitted(state, marker)
    _save_state(state)
    return event_type


def _baseline(state: dict[str, Any]) -> None:
    """Snapshot the current entity last_updated values; emit nothing."""
    with HaRestClient() as c:
        states = c.get_states()
    entities: dict[str, str] = {}
    for s in states:
        eid = s.get("entity_id") or ""
        if eid and _entity_matches(eid, config.HA_ENTITY_FILTER):
            entities[eid] = s.get("last_updated") or ""
    state["ha_url"] = config.HA_URL
    state["entities"] = entities
    state["emitted"] = []
    _save_state(state)
    log.info("baseline complete: tracking %d entit%s",
             len(entities), "y" if len(entities) == 1 else "ies")


def _connect_cycle(state: dict[str, Any]) -> None:
    ws = HaWebSocketClient()
    ws.connect()
    try:
        ws.authenticate()
        ws.subscribe_events("state_changed")
        log.info(
            "subscribed to state_changed%s",
            f" (filter: {', '.join(config.HA_ENTITY_FILTER)})"
            if config.HA_ENTITY_FILTER else " (ALL entities)",
        )
        connected_at = time.monotonic()
        while not _shutdown:
            if time.monotonic() - connected_at > config.RECONNECT_MAX_SECONDS:
                log.info("proactive reconnect after %ss", config.RECONNECT_MAX_SECONDS)
                raise _Reconnect()
            _touch_heartbeat()
            try:
                msg = ws.recv(timeout=config.WS_RECV_TIMEOUT)
            except (websocket.WebSocketTimeoutException, socket.timeout):
                # Idle: send a keepalive ping and loop (re-checks shutdown / proactive reconnect).
                try:
                    ws.ping()
                except Exception as e:
                    raise _Reconnect() from e
                continue
            mtype = msg.get("type")
            if mtype == "event":
                event = msg.get("event") or {}
                if event.get("event_type") == "state_changed":
                    _handle_state_changed(event.get("data") or {}, state)
            # result acks and pong replies are ignored.
    finally:
        ws.close()


def run() -> None:
    _install_signal_handlers()
    for name in classify.EVENT_TYPES:
        config.event_dir(name).mkdir(parents=True, exist_ok=True)

    if not config.HA_ENTITY_FILTER:
        log.warning(
            "HA_ENTITY_FILTER is not set - listening to ALL entities. Busy instances emit "
            "many state_changed events; set HA_ENTITY_FILTER (e.g. 'light.*,switch.*') in "
            "scripts/.env to limit the number of event files."
        )

    state = _load_state()
    if state.get("ha_url") and state["ha_url"] != config.HA_URL:
        log.warning("HA_URL changed (%s -> %s); wiping state to re-baseline",
                    state["ha_url"], config.HA_URL)
        state = _empty_state()
        try:
            config.STATE_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    backoff = 10

    if not state.get("ha_url"):
        log.info("baselining entity state on startup")
        while not _shutdown:
            try:
                _baseline(state)
                backoff = 10
                break
            except (HaError, OSError, socket.error) as e:
                log.warning("baseline failed: %s; retrying in %ss", e, backoff)
                _sleep_interruptible(backoff)
                backoff = min(backoff * 2, 60)

    while not _shutdown:
        try:
            _connect_cycle(state)
        except _Reconnect:
            backoff = 10
        except HaError as e:
            # Includes auth_invalid: keep retrying so a replaced/renewed token recovers
            # without a manual restart.
            log.warning("connection error: %s; reconnecting in %ss", e, backoff)
            _sleep_interruptible(backoff)
            backoff = min(backoff * 2, 60)
        except (websocket.WebSocketException, OSError, socket.error) as e:
            log.warning("websocket error: %s; reconnecting in %ss", e, backoff)
            _sleep_interruptible(backoff)
            backoff = min(backoff * 2, 60)
        except Exception as e:
            log.exception("unexpected error in listener: %s", e)
            _sleep_interruptible(backoff)
            backoff = min(backoff * 2, 60)

    _remove_heartbeat()
    log.info("listener shut down cleanly")


def _sleep_interruptible(seconds: int) -> None:
    for _ in range(max(1, int(seconds))):
        if _shutdown:
            return
        time.sleep(1)
