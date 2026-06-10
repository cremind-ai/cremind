"""Jira Cloud issue listener.

Architecture (token-less relay):
  1. Register a Jira dynamic webhook pointing at the org ingress
     (`${webhookUrl}?rk=<accountKey>`), using THIS user's own token. Jira expires
     dynamic webhooks after 30 days, so refresh well inside that window.
  2. Connect a WebSocket to the cremind-connect relay and prove account control
     with a short-lived relay-session (Atlassian issues no id_token; the backend
     mints the session from the access token via /me).
  3. On each `resync` nudge (and once on startup), pull issues updated since the
     cursor via JQL and drop changed issues as markdown into events/issue_changed/.
"""
from __future__ import annotations

import errno
import json
import os
import re
import secrets
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, formatter, jira_api
from .atlassian import auth
from .atlassian.discovery import Discovery
from .atlassian.relay_client import RelayClient

log = config.setup_logging()

_shutdown = threading.Event()
_sync_lock = threading.Lock()

DEFAULT_EVENTS = ["jira:issue_created", "jira:issue_updated", "jira:issue_deleted", "comment_created"]
PULL_FIELDS = ["summary", "status", "issuetype", "assignee", "reporter", "priority", "updated", "created", "description"]
_EMITTED_CAP = 500


def _install_signal_handlers() -> None:
    def handler(_signum, _frame):
        _shutdown.set()

    try:
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError):
        pass


_instance_lock = None  # held open for the process lifetime to enforce single-instance


def _acquire_single_instance() -> bool:
    global _instance_lock
    try:
        f = open(config.LOCK_FILE, "a+")
    except OSError:
        return True
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


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _parse_jira_dt_ms(value: str) -> int | None:
    """Parse a Jira datetime ('2026-06-08T10:20:30.123-0700') to epoch ms."""
    if not value:
        return None
    s = value.strip()
    # Normalize a trailing '-0700'/'+0000' offset to '-07:00' for fromisoformat.
    m = re.search(r"([+-]\d{2})(\d{2})$", s)
    if m:
        s = s[: m.start()] + f"{m.group(1)}:{m.group(2)}"
    try:
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except ValueError:
        return None


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


def _write_event(issue: dict[str, Any], site_url: str) -> Path:
    config.ISSUE_CHANGED_DIR.mkdir(parents=True, exist_ok=True)
    content = formatter.format_issue_markdown(issue, event_type="issue_changed", site_url=site_url)
    key = issue.get("key", "")
    summary = (issue.get("fields", {}) or {}).get("summary", "")
    base = f"{datetime.now().strftime('%Y-%m-%dT%H-%M-%S')} {key} {_sanitize(summary)}"
    attempt = 0
    while True:
        name = f"{base}.md" if attempt == 0 else f"{base} ({attempt + 1}).md"
        path = config.ISSUE_CHANGED_DIR / name
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


# --- jira client ---

def _svc() -> jira_api.JiraClient:
    access_token, data = auth.get_access_token(config.TOKEN_PATH, config.CREMIND_CONNECT_URL)
    return jira_api.JiraClient(access_token, data["cloud_id"])


# --- sync ---

def _sync(state: dict[str, Any], site_url: str) -> int:
    with _sync_lock:
        svc = _svc()
        since_ms = int(state.get("since_ms") or _now_ms())
        # Jira JQL date comparisons are minute-precision; floor to the minute and
        # rely on the emitted-set to suppress duplicates.
        floor_ms = (since_ms // 60000) * 60000
        # Scope the pull to the SAME filter as the webhook so emitted issues match
        # what we subscribed to — the webhook jqlFilter only gates the nudge, not
        # which issues we then fetch. The pull is a normal search, so date clauses
        # (updated) are fine here even though the webhook filter rejects them.
        scope = config.JIRA_WEBHOOK_JQL.strip()
        jql = (f"({scope}) AND " if scope else "") + f"updated >= {floor_ms} ORDER BY updated ASC"
        resp = svc.search(jql, fields=PULL_FIELDS, max_results=config.CATCHUP_MAX)
        issues = resp.get("issues", []) or []

        emitted = list(state.get("emitted", []))
        emitted_set = set(emitted)
        max_ms = since_ms
        count = 0
        for issue in issues:
            key = issue.get("key", "")
            updated = (issue.get("fields", {}) or {}).get("updated", "")
            marker = f"{key}:{updated}"
            if marker in emitted_set:
                continue
            try:
                _write_event(issue, site_url)
                count += 1
            except OSError as e:
                log.warning("failed to write issue %s: %s", key, e)
                continue
            emitted.append(marker)
            emitted_set.add(marker)
            ms = _parse_jira_dt_ms(updated)
            if ms and ms > max_ms:
                max_ms = ms

        state["since_ms"] = max_ms
        state["emitted"] = emitted[-_EMITTED_CAP:]
        _save_state(state)
        if count:
            log.info("emitted %d changed issue(s)", count)
        return count


def _safe_sync(state: dict[str, Any], site_url: str) -> None:
    try:
        _sync(state, site_url)
    except Exception as e:
        log.warning("sync failed: %s", e)


# --- webhook lifecycle ---

def _webhook_url(webhook_base: str, account_key: str) -> str:
    return f"{webhook_base}?rk={account_key}&n={secrets.token_urlsafe(8)}"


def _ensure_webhook(state: dict[str, Any], webhook_base: str, account_key: str, account_id: str = "") -> None:
    svc = _svc()
    old_ids = state.get("webhook_ids") or []
    if old_ids:
        try:
            svc.delete_webhooks(old_ids)
        except Exception as e:
            log.debug("delete old webhooks failed (ignored): %s", e)

    # currentUser() can't resolve in the webhook matcher (no user context); resolve
    # it to the literal accountId for the webhook filter. The pull keeps the raw
    # filter, where normal search resolves currentUser().
    if not account_id:
        try:
            account_id = svc.myself().get("accountId", "")
        except Exception as e:
            log.warning("could not resolve accountId for webhook filter: %s", e)
    wjql = jira_api.webhook_jql(config.JIRA_WEBHOOK_JQL, account_id)

    url = _webhook_url(webhook_base, account_key)
    res = svc.register_webhook(url=url, events=DEFAULT_EVENTS, jql=wjql)
    results = res.get("webhookRegistrationResult", []) or []
    ids = [r.get("createdWebhookId") for r in results if r.get("createdWebhookId")]
    errors = [r.get("errors") for r in results if r.get("errors")]
    state["webhook_ids"] = ids
    _save_state(state)
    if ids:
        log.info("jira webhook registered (ids=%s, webhook_jql=%r)", ids, wjql)
    else:
        # Loud, not silent: a bad jqlFilter/events list means NO events will ever
        # arrive, and that must not look like normal operation.
        log.error(
            "jira webhook registration FAILED — no webhook created, so NO events "
            "will be delivered. webhook_jql=%r errors=%s",
            wjql,
            errors,
        )


def _renew_loop(state: dict[str, Any], webhook_base: str, account_key: str, account_id: str = "") -> None:
    while not _shutdown.is_set():
        if _shutdown.wait(timeout=config.WEBHOOK_RENEW_INTERVAL):
            return
        ids = state.get("webhook_ids") or []
        try:
            if ids:
                svc = _svc()
                svc.refresh_webhooks(ids)
                log.info("refreshed jira webhook(s) %s", ids)
            else:
                _ensure_webhook(state, webhook_base, account_key, account_id)
        except Exception as e:
            log.warning("webhook refresh failed (%s); re-registering", e)
            try:
                _ensure_webhook(state, webhook_base, account_key, account_id)
            except Exception as e2:
                log.warning("webhook re-registration failed: %s", e2)


def run() -> None:
    _install_signal_handlers()
    if not _acquire_single_instance():
        log.error(
            "another jira listener is already running for this skill (lock: %s); "
            "exiting to avoid duplicate event files", config.LOCK_FILE
        )
        raise SystemExit(1)
    config.ISSUE_CHANGED_DIR.mkdir(parents=True, exist_ok=True)

    # Wait for the account to be linked instead of exiting (starts automatically once linked).
    data = None
    announced = False
    while not _shutdown.is_set():
        try:
            _, data = auth.get_access_token(config.TOKEN_PATH, config.CREMIND_CONNECT_URL)
            break
        except auth.AuthError as e:
            if not announced:
                log.warning("%s — waiting; will start automatically once linked", e)
                announced = True
            _shutdown.wait(timeout=5)
    if data is None:
        return

    account_key = data["account_key"]
    account_id = data.get("account_id", "")
    site_url = data.get("site_url", "")
    disc = Discovery(config.CREMIND_CONNECT_URL)
    try:
        webhook_base = disc.webhook_url("jira")
        ws_url = disc.ws_url()
    except Exception as e:
        log.error("discovery failed: %s", e)
        raise SystemExit(1)

    state = _load_state()
    state["account_key"] = account_key
    if not state.get("since_ms"):
        state["since_ms"] = _now_ms()  # baseline: emit nothing for pre-existing issues
        _save_state(state)

    _ensure_webhook(state, webhook_base, account_key, account_id)
    threading.Thread(target=_renew_loop, args=(state, webhook_base, account_key, account_id), daemon=True).start()

    _safe_sync(state, site_url)  # catch-up on startup

    relay = RelayClient(
        ws_url=ws_url,
        account_key=account_key,
        resources=["jira"],
        session_provider=lambda: auth.fresh_relay_session(config.TOKEN_PATH, config.CREMIND_CONNECT_URL),
        on_resync=lambda _source: _safe_sync(state, site_url),
        logger=log,
    )
    relay_thread = threading.Thread(target=relay.run_forever, name="relay", daemon=True)
    relay_thread.start()
    log.info("listening for jira issue events (account_key=%s)", account_key)
    try:
        while relay_thread.is_alive() and not _shutdown.is_set():
            relay_thread.join(timeout=0.5)
    except KeyboardInterrupt:
        pass
    _shutdown.set()
    relay.stop()
    relay_thread.join(timeout=5)
    log.info("jira listener shut down")
