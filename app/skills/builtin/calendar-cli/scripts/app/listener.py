"""Polling listener that diffs CalDAV calendars and emits markdown events.

Primary diff strategy: RFC 6578 sync-collection (`objects_by_sync_token`).
Fallback (per-calendar, set once per session if sync-token fails): full
listing + ETag map diff.
"""
from __future__ import annotations

import errno
import json
import os
import re
import signal
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config, formatter, ical
from .caldav_client import CalDAVClient, CalDAVError


log = config.setup_logging()


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


def _load_state() -> dict[str, Any]:
    if not config.STATE_FILE.exists():
        return {"caldav_url": "", "calendars": {}}
    try:
        with open(config.STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("state file unreadable (%s); starting fresh", e)
        return {"caldav_url": "", "calendars": {}}
    if not isinstance(data, dict):
        return {"caldav_url": "", "calendars": {}}
    data.setdefault("caldav_url", "")
    data.setdefault("calendars", {})
    return data


def _save_state(state: dict[str, Any]) -> None:
    tmp = config.STATE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(state, f, indent=2)
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
        cleaned = "no-summary"
    if cleaned.lower() in _WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def _filename_prefix() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


def _write_event(events_dir: Path, event_dict: dict[str, Any], event_type: str) -> Path:
    events_dir.mkdir(parents=True, exist_ok=True)
    content = formatter.format_event_markdown(event_dict, event_type=event_type)
    base = f"{_filename_prefix()} {_sanitize(event_dict.get('summary') or '')}"
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


def _calendar_name(cal) -> str:
    name = getattr(cal, "name", None) or ""
    if name:
        return str(name)
    try:
        return str(cal.get_display_name() or cal.url)
    except Exception:
        return str(cal.url)


def _event_dict_from_caldav_object(obj, calendar_name: str) -> dict[str, Any] | None:
    try:
        instance = obj.icalendar_instance
    except Exception:
        try:
            instance = ical.find_vevent(obj.data.encode("utf-8") if isinstance(obj.data, str) else obj.data)
            if instance is None:
                return None
            base = ical.parse_vevent_to_dict(instance, calendar_name=calendar_name, detail="full")
            base["href"] = str(obj.url)
            base["etag"] = obj.etag or ""
            return base
        except Exception:
            return None

    master = None
    for comp in instance.walk("VEVENT"):
        if "RECURRENCE-ID" not in comp:
            master = comp
            break
    if master is None:
        for comp in instance.walk("VEVENT"):
            master = comp
            break
    if master is None:
        return None

    base = ical.parse_vevent_to_dict(master, calendar_name=calendar_name, detail="full")
    base["href"] = str(obj.url)
    base["etag"] = obj.etag or ""
    return base


def _uid_etag_from_object(obj) -> tuple[str, str, str] | None:
    """Return (uid, href, etag) for a caldav Event object."""
    try:
        instance = obj.icalendar_instance
        uid = ""
        for comp in instance.walk("VEVENT"):
            uid = str(comp.get("uid", ""))
            if uid:
                break
        return uid, str(obj.url), obj.etag or ""
    except Exception:
        return None


def _baseline_calendar(cal) -> dict[str, Any]:
    """Capture the current event-set for a calendar without emitting events."""
    cal_state: dict[str, Any] = {
        "sync_token": "",
        "events": {},
        "use_full_listing": False,
    }
    used_sync_token = False
    try:
        result = cal.objects_by_sync_token(load_objects=True)
        token = getattr(result, "sync_token", "") or ""
        if token:
            cal_state["sync_token"] = token
            used_sync_token = True
            for obj in result:
                meta = _uid_etag_from_object(obj)
                if meta and meta[0]:
                    uid, href, etag = meta
                    cal_state["events"][uid] = {"href": href, "etag": etag}
    except Exception as e:
        log.info("calendar %r: sync-token unsupported (%s); using full listing",
                 _calendar_name(cal), e)

    if not used_sync_token:
        cal_state["use_full_listing"] = True
        try:
            for obj in cal.events():
                meta = _uid_etag_from_object(obj)
                if meta and meta[0]:
                    uid, href, etag = meta
                    cal_state["events"][uid] = {"href": href, "etag": etag}
        except Exception as e:
            log.warning("calendar %r: baseline listing failed: %s",
                        _calendar_name(cal), e)
    return cal_state


def _poll_calendar(cal, cal_state: dict[str, Any]) -> tuple[int, int]:
    """Diff against `cal_state` and write event files. Returns (new_count, updated_count)."""
    cal_name = _calendar_name(cal)
    new_count = 0
    updated_count = 0
    prev_events: dict[str, dict[str, str]] = cal_state.get("events", {}) or {}

    if not cal_state.get("use_full_listing"):
        token = cal_state.get("sync_token") or ""
        try:
            result = cal.objects_by_sync_token(sync_token=token or None, load_objects=True)
            new_token = getattr(result, "sync_token", "") or ""
            for obj in result:
                meta = _uid_etag_from_object(obj)
                if not meta or not meta[0]:
                    continue
                uid, href, etag = meta
                prior = prev_events.get(uid)
                if prior is None:
                    event_dict = _event_dict_from_caldav_object(obj, cal_name)
                    if event_dict:
                        _write_event(config.NEW_EVENT_DIR, event_dict, "new_event")
                        new_count += 1
                elif prior.get("etag") != etag:
                    event_dict = _event_dict_from_caldav_object(obj, cal_name)
                    if event_dict:
                        _write_event(config.UPDATED_EVENT_DIR, event_dict, "updated_event")
                        updated_count += 1
                prev_events[uid] = {"href": href, "etag": etag}
            if new_token:
                cal_state["sync_token"] = new_token
            cal_state["events"] = prev_events
            return new_count, updated_count
        except Exception as e:
            log.info("calendar %r: sync-token delta failed (%s); falling back to full listing for this session",
                     cal_name, e)
            cal_state["use_full_listing"] = True

    # Full-listing fallback.
    seen_uids: set[str] = set()
    try:
        events_iter = list(cal.events())
    except Exception as e:
        raise CalDAVError(f"calendar {cal_name!r}: failed to list events: {e}") from e

    for obj in events_iter:
        meta = _uid_etag_from_object(obj)
        if not meta or not meta[0]:
            continue
        uid, href, etag = meta
        seen_uids.add(uid)
        prior = prev_events.get(uid)
        if prior is None:
            event_dict = _event_dict_from_caldav_object(obj, cal_name)
            if event_dict:
                _write_event(config.NEW_EVENT_DIR, event_dict, "new_event")
                new_count += 1
        elif prior.get("etag") != etag:
            event_dict = _event_dict_from_caldav_object(obj, cal_name)
            if event_dict:
                _write_event(config.UPDATED_EVENT_DIR, event_dict, "updated_event")
                updated_count += 1
        prev_events[uid] = {"href": href, "etag": etag}

    # Drop UIDs that vanished on the server (we don't emit deleted_event in MVP).
    for uid in list(prev_events.keys()):
        if uid not in seen_uids:
            prev_events.pop(uid, None)

    cal_state["events"] = prev_events
    return new_count, updated_count


def _baseline_all(client: CalDAVClient, state: dict[str, Any]) -> None:
    state["caldav_url"] = config.CALDAV_URL
    state["calendars"] = {}
    for cal in client.calendars():
        url = str(cal.url)
        try:
            state["calendars"][url] = _baseline_calendar(cal)
        except Exception as e:
            log.warning("calendar %r: baseline failed: %s", _calendar_name(cal), e)
            state["calendars"][url] = {
                "sync_token": "",
                "events": {},
                "use_full_listing": True,
            }
    _save_state(state)


def _poll_all(client: CalDAVClient, state: dict[str, Any]) -> tuple[int, int]:
    """Poll every calendar once. Returns aggregate (new, updated)."""
    total_new = 0
    total_updated = 0
    any_success = False
    last_error: Exception | None = None

    calendars = client.calendars()
    for cal in calendars:
        if _shutdown:
            break
        url = str(cal.url)
        cal_state = state["calendars"].get(url)
        if cal_state is None:
            cal_state = _baseline_calendar(cal)
            state["calendars"][url] = cal_state
            _save_state(state)
            any_success = True
            continue
        try:
            n, u = _poll_calendar(cal, cal_state)
            total_new += n
            total_updated += u
            any_success = True
            _save_state(state)
        except Exception as e:
            last_error = e
            log.warning("calendar %r: poll failed: %s", _calendar_name(cal), e)

    if not any_success and last_error is not None:
        raise last_error
    return total_new, total_updated


def run() -> None:
    _install_signal_handlers()
    config.NEW_EVENT_DIR.mkdir(parents=True, exist_ok=True)
    config.UPDATED_EVENT_DIR.mkdir(parents=True, exist_ok=True)

    state = _load_state()
    if state.get("caldav_url") and state["caldav_url"] != config.CALDAV_URL:
        log.warning(
            "CALDAV_URL changed (%s -> %s); wiping state to avoid event flood",
            state["caldav_url"], config.CALDAV_URL,
        )
        state = {"caldav_url": "", "calendars": {}}
        try:
            config.STATE_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    backoff = 10

    if not state.get("caldav_url"):
        # Baseline on first run (or after provider migration).
        log.info("baselining calendar state on startup")
        while not _shutdown:
            try:
                with CalDAVClient() as client:
                    _baseline_all(client, state)
                log.info("baseline complete: %d calendar(s) tracked",
                         len(state["calendars"]))
                backoff = 10
                break
            except (CalDAVError, OSError, socket.error) as e:
                log.warning("baseline failed: %s; retrying in %ss", e, backoff)
                _sleep_interruptible(backoff)
                backoff = min(backoff * 2, 60)

    while not _shutdown:
        try:
            with CalDAVClient() as client:
                while not _shutdown:
                    _touch_heartbeat()
                    new_count, updated_count = _poll_all(client, state)
                    if new_count or updated_count:
                        log.info("emitted %d new, %d updated event(s)",
                                 new_count, updated_count)
                    backoff = 10
                    _sleep_interruptible(config.POLL_INTERVAL)
        except (CalDAVError, OSError, socket.error) as e:
            log.warning("poll loop error: %s; reconnecting in %ss", e, backoff)
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
