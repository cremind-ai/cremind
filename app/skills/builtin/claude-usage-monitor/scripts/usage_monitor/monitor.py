"""The monitor: follows Claude Code's own files, keeps every account's usage, raises alerts.

Where the numbers come from (login tokens are never read):

- ``<profile>/.claude.json`` — which account each Claude Code profile is signed in to,
  plus Claude Code's own cached usage: official, refreshed now and then.
- ``.monitor/inbox/*.json`` — status line snapshots after each terminal reply: official.
- ``<profile>/projects/**/*.jsonl`` — session transcripts (VS Code and terminal): what
  each reply cost and any limit hits, which roll the official figure forward in between.

Alerts go to Cremind as skill events (``events/<type>/*.md``) instead of desktop
notifications; the activity log and the dashboard show every one of them too.
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from . import common as C
from . import profiles as P
from . import usage as U
from .events import ALERT_EVENT_TYPES, EVENT_TYPES, write_event
from .transcripts import TranscriptTail

MIN = U.MIN
HOUR = U.HOUR
TICK_MS = float(os.environ.get("CLAUDE_USAGE_MONITOR_TICK_MS") or 2000)
RUNNING_MS = 6 * MIN  # spent or reported this recently => the account is running
KEEP_MS = 8 * 24 * HOUR  # spend, readings and fired alerts older than this are dropped
INBOX_MAX_AGE_MS = 24 * HOUR
PROFILE_CHECK_MS = 10_000  # how often this Cremind profile's own Claude home is looked for
STATIC_FILES = ("index.html", "app.js", "style.css", "icon.svg")


def _session_key(type_: str, id_: str, resets_at: float) -> str:
    return f"{type_}:{id_}:{U.js_round(resets_at / (10 * MIN))}"


def _weekly_key(type_: str, id_: str, resets_at: float) -> str:
    # `weekly:` is what the weekly heads-up was called before the weekly limit had the full set.
    prefix = "weekly" if type_ == "warn" else f"weekly-{type_}"
    return f"{prefix}:{id_}:{U.js_round(resets_at / HOUR)}"


# Both limits interrupt a session when they run out, so both get the same alerts. Each
# alert fires once per window: the key carries the window's reset time.
LIMITS: dict[str, dict[str, Any]] = {
    "session": {"name": "5-hour", "warn": "warnPct", "crit": "critPct", "key": _session_key},
    "weekly": {"name": "weekly", "warn": "weeklyWarnPct", "crit": "weeklyCritPct", "key": _weekly_key},
}

_ALERT_NAMES = {"warn": "heads_up", "crit": "switch_now", "eta": "limit_soon", "limit": "limit_reached", "back": "available_again"}
_UNSET = object()


# ---------------------------------------------------------------- formatting


def _local(ms: float) -> datetime:
    return datetime.fromtimestamp(U.js_round(ms / MIN) * MIN / 1000)  # resets land on xx:59:59.9


def clock(ms: float) -> str:
    return _local(ms).strftime("%H:%M")


def day_clock(ms: float) -> str:
    return f"{_local(ms).strftime('%a')} {clock(ms)}"


def when_clock(ms: float, now: float) -> str:
    return day_clock(ms) if abs(ms - now) > 20 * HOUR else clock(ms)


def pct_text(w: dict | None) -> str:
    return f"{'≈' if w and w.get('estimated') else ''}{U.js_round(w['pct'] if w else 0)}%"


def rate_text(r: dict) -> str:
    per_hour = r["perHour"]
    return f"{per_hour:.1f}%/h" if per_hour < 10 else f"{U.js_round(per_hour)}%/h"


def name_of(a: dict) -> str:
    return a.get("label") or a.get("email") or a.get("name") or str(a.get("id"))[:8]


def iso(ms: float | None) -> str | None:
    if ms is None or not math.isfinite(ms):
        return None
    return datetime.fromtimestamp(ms / 1000).astimezone().isoformat(timespec="minutes")


def _migrate_account(a: dict) -> None:
    """The first version of the standalone app kept one combined reading per account."""
    if not isinstance(a.get("official"), dict):
        u = a.get("usage") if isinstance(a.get("usage"), dict) else {}
        a["official"] = {"session": None, "weekly": None}
        if u.get("session"):
            a["official"]["session"] = {**u["session"], "at": u.get("at"), "source": u.get("source")}
        if u.get("weekly"):
            a["official"]["weekly"] = {**u["weekly"], "at": u.get("at"), "source": u.get("source")}
        if u.get("spend"):
            a["extra"] = {**u["spend"], "at": u.get("at")}
    if not isinstance(a.get("readings"), list):
        a["readings"] = a["samples"] if isinstance(a.get("samples"), list) else []
    a.pop("usage", None)
    a.pop("samples", None)


class _Profile:
    """A Claude Code profile plus what the monitor tracks about it at runtime."""

    def __init__(self, p: P.ClaudeProfile, tail: TranscriptTail) -> None:
        self.p = p
        self.tail = tail
        self.config_mtime: Any = None  # st_mtime_ns, "missing", or None (not read yet)

    @property
    def name(self) -> str:
        return self.p.name


class Monitor:
    """All state and the polling logic. Every public method takes ``self.lock``: the
    dashboard's HTTP threads and the tick loop share one instance."""

    def __init__(self, paths: C.Paths | None = None, *, readonly: bool = False, events_dir: Path = C.EVENTS_DIR) -> None:
        self.paths = paths or C.default_paths()
        self.readonly = readonly
        self.events_dir = events_dir
        self.lock = threading.RLock()
        self.state = self._load_state()
        self.ledger = self._load_ledger()
        self.spend: dict[str, U.SpendLog] = {str(i): U.SpendLog(e) for i, e in self.ledger["spend"].items() if isinstance(e, list)}
        self.seen: dict[str, None] = dict.fromkeys(str(k) for k in self.ledger["seen"])
        self.profiles: list[_Profile] = []
        self._profiles_sig: Any = None
        self._own_home_seen = False
        self._last_profile_check = 0.0
        self.ready = False  # all transcripts read up to date: calibration and alerts may run
        self.dirty = False
        self.ledger_dirty = False
        self.save_now = False  # an account switch: persist before attributing any reply made after it
        self.last_saved_at = 0.0
        self.last_ledger_save = 0.0
        self.last_prune = 0.0
        self.last_summary: tuple[str, float] = ("", 0.0)
        self.last_alert: dict | None = None
        self.statusline_info: dict[str, Any] = {"mtime": None, "checkedAt": 0.0, "installed": False, "other": False}
        self.inbox_seen: dict[str, float] = {}  # inbox file -> mtime already ingested
        self.sessions: dict[str, dict] = {}  # Claude Code session id -> {accountId, rlKey, staleKey, at}
        self.warned_at: dict[str, float] = {}
        self.dashboard_url: str | None = None
        self._build: tuple[float, str] = (0.0, "")

    # ------------------------------------------------------------ persistence

    def _load_state(self) -> dict:
        s = C.read_json(self.paths.state)
        s = s if isinstance(s, dict) else {}
        accounts = s.get("accounts") if isinstance(s.get("accounts"), dict) else {}
        accounts = {str(k): v for k, v in accounts.items() if isinstance(v, dict)}
        for a in accounts.values():
            _migrate_account(a)
        saved = s.get("settings") if isinstance(s.get("settings"), dict) else {}
        settings = {**U.DEFAULT_SETTINGS, **saved}
        if "events" not in saved and "toast" in saved:
            settings["events"] = bool(saved["toast"])  # the standalone app's "Windows notifications"
        settings.pop("toast", None)
        return {
            "settings": settings,
            "accounts": accounts,
            "activeId": s.get("activeId") or None,
            "events": s["events"][-200:] if isinstance(s.get("events"), list) else [],
            "fired": s["fired"] if isinstance(s.get("fired"), dict) else {},
            "lastLinkAt": s.get("lastLinkAt") or 0,
            "history": s["history"] if isinstance(s.get("history"), dict) else {},  # profile -> [[t, accountId|None]]
            "calibration": s["calibration"] if isinstance(s.get("calibration"), dict) else {},  # tier -> {session, weekly}
        }

    def _load_ledger(self) -> dict:
        data = C.read_json(self.paths.ledger)
        data = data if isinstance(data, dict) else {}
        return {
            "tails": data["tails"] if isinstance(data.get("tails"), dict) else {},  # profile -> {since, offsets}
            "seen": data["seen"] if isinstance(data.get("seen"), list) else [],
            "spend": data["spend"] if isinstance(data.get("spend"), dict) else {},  # account -> [[minute, usd]]
        }

    def save_state(self, force: bool = False) -> None:
        now = C.now_ms()
        if self.readonly or (not force and (not self.dirty or now - self.last_saved_at < 5000)):
            return
        try:
            C.write_json_atomic(self.paths.state, self.state)
            self.dirty = False
            self.last_saved_at = now
        except OSError as e:
            self.warn("save state", e)

    def save_ledger(self, force: bool = False) -> None:
        """Spend and transcript offsets are saved together, so a restart never counts a reply twice."""
        now = C.now_ms()
        if self.readonly or (not force and (not self.ledger_dirty or now - self.last_ledger_save < 30_000)):
            return
        try:
            self.ledger["spend"] = {i: log.to_json() for i, log in self.spend.items()}
            self.ledger["seen"] = list(self.seen)[-3000:]
            C.write_json_atomic(self.paths.ledger, self.ledger)
            self.ledger_dirty = False
            self.last_ledger_save = now
        except OSError as e:
            self.warn("save ledger", e)

    def warn(self, where: str, e: Any) -> None:
        msg = f"{where}: {e}"
        now = C.now_ms()
        if now - self.warned_at.get(msg, 0) < 60_000:
            return
        self.warned_at[msg] = now
        if not self.readonly:
            print(f"[warn] {msg}", file=sys.stderr, flush=True)

    def log_event(self, kind: str, text: str, level: str = "info") -> None:
        e = {"t": C.now_ms(), "kind": kind, "level": level, "text": text}
        self.state["events"].append(e)
        del self.state["events"][:-200]
        self.dirty = True
        if not self.readonly:
            print(f"{datetime.now():%H:%M:%S}  {text}", flush=True)

    # ------------------------------------------------------------ accounts, profiles, attribution

    def upsert_account(self, meta: dict | None, now: float) -> dict | None:
        if not meta or not meta.get("id"):
            return None
        accounts = self.state["accounts"]
        a = accounts.get(meta["id"])
        if a is None:
            a = accounts[meta["id"]] = {
                "id": meta["id"],
                "label": "",
                "firstSeenAt": now,
                "lastActiveAt": None,
                "lastRunningAt": None,
                "official": {"session": None, "weekly": None},
                "readings": [],
                "scoped": None,
                "extra": None,
            }
            self.dirty = True
            self.log_event("account", f"New account detected: {meta.get('email') or meta['id']}")
        for k in ("email", "name", "org", "orgType", "tier"):
            if meta.get(k) is not None and a.get(k) != meta[k]:
                a[k] = meta[k]
                self.dirty = True
        return a

    def account_at(self, profile_name: str, t: float) -> str | None:
        """Which account was signed in to a profile at time t (None before tracking began)."""
        found = None
        for ht, hid in self.state["history"].get(profile_name) or []:
            if ht > t:
                break
            found = hid
        return found

    def current_account(self, profile_name: str) -> str | None:
        h = self.state["history"].get(profile_name)
        return h[-1][1] if h else None

    def profiles_of(self, account_id: str) -> list[str]:
        """Profiles an account is signed in to right now."""
        return [p.name for p in self.profiles if self.current_account(p.name) == account_id]

    def active_since(self, account_id: str) -> float:
        """When the account most recently became signed in to a tracked profile."""
        since = 0.0
        for h in self.state["history"].values():
            for i, (t, hid) in enumerate(h):
                if hid == account_id and (i == 0 or h[i - 1][1] != account_id):
                    since = max(since, t)
        return since

    def covered(self, account_id: str, a: float, b: float) -> bool:
        """Whether every reply the account made in [a, b] went through a tracked profile."""
        for h in self.state["history"].values():
            for i, (t, hid) in enumerate(h):
                if hid != account_id:
                    continue
                end = h[i + 1][0] if i + 1 < len(h) else math.inf
                if t <= a + MIN and end >= b - MIN:
                    return True
        return False

    def set_signed_in(self, rt: _Profile, account_id: str | None, now: float) -> None:
        h = self.state["history"].setdefault(rt.name, [])
        cur = h[-1][1] if h else _UNSET
        if cur == account_id:
            return
        h.append([now, account_id])
        del h[:-500]
        self.dirty = True
        self.save_now = True
        a = self.state["accounts"].get(account_id) if account_id else None
        if rt.p.main:
            prev = self.state["accounts"].get(self.state["activeId"]) if self.state["activeId"] else None
            self.state["activeId"] = account_id
            if a:
                a["lastActiveAt"] = now
            if cur is _UNSET:
                if a:
                    self.log_event("switch", f"Claude Code is signed in to {name_of(a)}")
            elif a:
                pv = self.view_of(prev, now) if prev and prev["id"] != account_id else None
                src = f" (from {name_of(prev)} at 5-hour {pct_text(pv['session'])}, weekly {pct_text(pv['weekly'])})" if pv else ""
                self.log_event("switch", f"Claude Code switched to {name_of(a)}{src}")
            else:
                self.log_event("switch", "Claude Code is signed out")
        elif a:
            self.log_event("account", f'Profile "{rt.name}" is signed in to {name_of(a)}')
        elif cur is not _UNSET and cur:
            self.log_event("account", f'Profile "{rt.name}" is signed out')

    # ------------------------------------------------------------ calibration & readings

    def cal_for(self, a: dict) -> dict:
        key = a.get("tier") or "unknown"
        cal = self.state["calibration"].get(key)
        if not isinstance(cal, dict) or "session" not in cal or "weekly" not in cal:
            cal = self.state["calibration"][key] = U.calibration_seed(key)
        return cal

    def calibrate(self, a: dict, kind: str, prev: dict | None, nxt: dict) -> None:
        """When a new official reading lands, compare the rise since the previous one (or
        since its window opened) with what the account spent in between, and refine
        USD-per-percent."""
        if not self.ready or not prev:
            return
        length = U.WEEK_MS if kind == "weekly" else U.SESSION_MS
        start = nxt["resetsAt"] - length if nxt.get("resetsAt") is not None else None
        prev_at = prev.get("at") or 0
        base = None
        if prev.get("resetsAt") is not None and nxt.get("resetsAt") is not None and abs(prev["resetsAt"] - nxt["resetsAt"]) < 15 * MIN:
            base = (prev_at, prev["pct"])  # same window
        elif start is not None and (
            (prev["pct"] == 0 and start >= prev_at - 10 * MIN) if prev.get("resetsAt") is None else prev["resetsAt"] <= start + 15 * MIN
        ):
            base = (max(start, prev_at), 0)  # the window opened after the previous reading
        if not base:
            return
        rise = nxt["pct"] - base[1]
        if rise < (4 if kind == "weekly" else 8) or not self.covered(a["id"], base[0], nxt["at"]):
            return
        usd = (self.spend.get(a["id"]) or U.EMPTY_LOG).between(base[0], nxt["at"])
        cal = self.cal_for(a)
        blended = U.blend_calibration(cal[kind], usd / rise)
        if not blended:
            return
        cal[kind] = blended
        self.dirty = True
        what = "weekly" if kind == "weekly" else "5-hour"
        plural = "" if blended["n"] == 1 else "s"
        self.log_event(
            "calibration",
            f"Calibrated on {name_of(a)}: 1% of the {what} limit ≈ ${blended['usdPerPct']:.2f} of usage at list price ({blended['n']} reading{plural})",
        )

    @staticmethod
    def early_reset(kind: str, prev: dict | None, nxt: dict) -> str | None:
        """Claude occasionally resets an account's limits before they are due: the usage of
        the same window drops, or a new window starts while the previous one still had hours
        to run. Returns a description, or None for an ordinary reading."""
        if not prev or prev.get("resetsAt") is None or nxt.get("resetsAt") is None or nxt["at"] >= prev["resetsAt"]:
            return None
        name = LIMITS[kind]["name"]
        if abs(prev["resetsAt"] - nxt["resetsAt"]) < 15 * MIN:
            if nxt["pct"] < prev["pct"] - 10:
                return f"{name} usage at {U.js_round(nxt['pct'])}% (was {U.js_round(prev['pct'])}% at {when_clock(prev.get('at') or 0, nxt['at'])})"
            return None
        start = nxt["resetsAt"] - (U.WEEK_MS if kind == "weekly" else U.SESSION_MS)
        if nxt["resetsAt"] > prev["resetsAt"] and start < prev["resetsAt"] - 15 * MIN:
            return f"a new {name} window from {when_clock(start, nxt['at'])}"
        return None

    def apply_reading(self, a: dict | None, r: dict | None) -> None:
        """An official reading: ``{at, source, session?, weekly?, scoped?, spend?}``. Older ones are ignored."""
        if not a or not r or not r.get("at"):
            return
        if not isinstance(a.get("official"), dict):
            a["official"] = {"session": None, "weekly": None}
        changed = False
        resets: list[str] = []
        for kind in ("session", "weekly"):
            w = r.get(kind)
            if not w or w.get("pct") is None:
                continue
            cur = a["official"].get(kind)
            if cur and r["at"] <= (cur.get("at") or 0):
                continue
            nxt = {"pct": w["pct"], "resetsAt": w.get("resetsAt"), "at": r["at"], "source": r.get("source")}
            reset = self.early_reset(kind, cur, nxt)
            if reset:
                resets.append(reset)
                # Same reset time, fresh usage: this window's alerts must be able to fire again.
                for type_ in ("warn", "crit", "eta", "limit"):
                    self.state["fired"].pop(LIMITS[kind]["key"](type_, a["id"], nxt["resetsAt"]), None)
            self.calibrate(a, kind, cur, nxt)
            a["official"][kind] = nxt
            changed = True
        if resets:
            what = "limits seem" if len(resets) > 1 else "limit seems"
            self.log_event("reset", f"{name_of(a)}'s {what} to have been reset early: Claude Code now reports {' and '.join(resets)}.")
        if r.get("scoped") and (not a.get("scoped") or r["at"] >= (a["scoped"].get("at") or 0)):
            a["scoped"] = {"at": r["at"], "items": r["scoped"]}
            self.dirty = True
        if r.get("spend") and (not a.get("extra") or r["at"] >= (a["extra"].get("at") or 0)):
            a["extra"] = {**r["spend"], "at": r["at"]}
            self.dirty = True
        if not changed:
            return
        s = r.get("session")
        readings = a["readings"]
        readings.append([r["at"], s["pct"] if s else None, s.get("resetsAt") if s else None, r["weekly"]["pct"] if r.get("weekly") else None])
        if len(readings) > 1 and readings[-2][0] > r["at"]:
            readings.sort(key=lambda x: x[0])
        cutoff = C.now_ms() - KEEP_MS
        while readings and (readings[0][0] < cutoff or len(readings) > 400):
            readings.pop(0)
        self.dirty = True

    # ------------------------------------------------------------ sources

    def sync_profiles(self, now: float) -> None:
        try:
            mtime = os.stat(self.paths.profiles).st_mtime_ns
        except OSError:
            mtime = None
        if now - self._last_profile_check >= PROFILE_CHECK_MS or not self.profiles:
            self._last_profile_check = now
            self._own_home_seen = P.cremind_profile() is not None
        sig = (mtime, self._own_home_seen)
        if self.profiles and sig == self._profiles_sig:
            return
        self._profiles_sig = sig
        old = {p.name: p for p in self.profiles}
        fresh: list[_Profile] = []
        for desc in P.list_profiles(self.paths):
            prev = old.get(desc.name)
            if prev and prev.p.dir == desc.dir:
                fresh.append(prev)
                continue
            if prev:
                prev.tail.close()
            store = self.ledger["tails"].get(desc.name)
            if not isinstance(store, dict):
                store = self.ledger["tails"][desc.name] = {}
            if not store.get("since"):
                h = self.state["history"].get(desc.name)
                store["since"] = h[0][0] if h else now
            rt = _Profile(desc, TranscriptTail(str(desc.projects_dir), store, self.seen))
            if old and not prev:
                self.log_event("account", f'Now tracking profile "{desc.name}"')
            self._import_backups(rt)
            self.ready = False
            fresh.append(rt)
        for p in old.values():
            if p not in fresh:
                p.tail.close()
        self.profiles = fresh

    def _import_backups(self, rt: _Profile) -> None:
        """Claude Code keeps a few backups of its config; they carry earlier usage readings."""
        try:
            names = [n for n in os.listdir(rt.p.backups_dir) if n.startswith(".claude.json.backup")]
        except OSError:
            return
        found = []
        for n in names:
            cfg = C.read_json(rt.p.backups_dir / n)
            if not isinstance(cfg, dict):
                continue
            cache = cfg.get("cachedUsageUtilization")
            found.append((C.pick_account(cfg.get("oauthAccount")), cache, U.from_claude_cache(cache) if cache else None))
        found.sort(key=lambda f: (f[2] or {}).get("at") or 0)
        for meta, cache, reading in found:
            if meta:
                self.upsert_account(meta, C.now_ms())
            if reading and isinstance(cache, dict) and cache.get("accountUuid"):
                self.apply_reading(self.state["accounts"].get(str(cache["accountUuid"])), reading)

    def _check_profile_config(self, rt: _Profile, now: float) -> None:
        """A profile's .claude.json: who is signed in, and Claude Code's cached usage."""
        try:
            st = os.stat(rt.p.config_file)
        except OSError:
            if rt.config_mtime != "missing":
                rt.config_mtime = "missing"
                self.set_signed_in(rt, None, now)
            return
        if st.st_mtime_ns == rt.config_mtime:
            return
        cfg = C.read_json(rt.p.config_file)
        if not isinstance(cfg, dict):
            return  # being rewritten: retry next tick
        rt.config_mtime = st.st_mtime_ns
        meta = C.pick_account(cfg.get("oauthAccount"))
        if meta:
            self.upsert_account(meta, now)
        self.set_signed_in(rt, meta["id"] if meta else None, now)
        cache = cfg.get("cachedUsageUtilization")
        if isinstance(cache, dict) and cache.get("accountUuid"):
            self.apply_reading(self.state["accounts"].get(str(cache["accountUuid"])), U.from_claude_cache(cache))

    def _ingest_transcripts(self, rt: _Profile, now: float) -> None:
        for e in rt.tail.poll(now):
            if e["kind"] == "unpriced":
                self.warn("pricing", f"no price known for {e['model']}; its usage is not counted")
                continue
            account_id = self.account_at(rt.name, e["t"])
            a = self.state["accounts"].get(account_id) if account_id else None
            if not a:
                continue
            if e["kind"] == "reply":
                self.spend.setdefault(account_id, U.SpendLog()).add(e["t"], e["usd"])
                self.ledger_dirty = True
            elif e["kind"] == "limit":
                kind = {"five_hour": "session", "seven_day": "weekly"}.get(e["type"])
                if kind:
                    self.apply_reading(a, {"at": e["t"], "source": "limit", kind: {"pct": 100, "resetsAt": e["resetsAt"]}})
        if rt.tail.changed:
            rt.tail.changed = False
            self.ledger_dirty = True

    def _ingest_snapshot(self, snap: dict) -> None:
        """A snapshot written by the status line bridge (terminal sessions only)."""
        now = C.now_ms()
        a = self.upsert_account(snap.get("account") if isinstance(snap.get("account"), dict) else None, now)
        if not a:
            return
        at = U.to_num(snap.get("at")) or now
        a["lastRunningAt"] = max(a.get("lastRunningAt") or 0, at)
        self.state["lastLinkAt"] = max(self.state.get("lastLinkAt") or 0, at)
        self.dirty = True

        # Right after /login a session may still report the previous account's numbers
        # until its next API response — don't credit those to the new account.
        sid = str(snap.get("sessionId") or "unknown")
        rate_limits = snap.get("rateLimits")
        key = json.dumps(rate_limits) if rate_limits else None
        prev = self.sessions.get(sid)
        stale_key = prev["staleKey"] if prev else None
        if prev and prev["accountId"] != a["id"] and key and key == prev["rlKey"]:
            stale_key = key
        if not (key and key == stale_key):
            stale_key = None
            self.apply_reading(a, U.from_statusline(rate_limits, at))
        self.sessions[sid] = {"accountId": a["id"], "rlKey": key, "staleKey": stale_key, "at": at}

    def _scan_inbox(self) -> None:
        try:
            names = os.listdir(self.paths.inbox)
        except OSError:
            return
        now = C.now_ms()
        fresh = []
        for name in names:
            path = self.paths.inbox / name
            try:
                mtime = os.stat(path).st_mtime_ns / 1e6
            except OSError:
                continue
            if not name.endswith(".json"):
                if name.endswith(".tmp") and now - mtime > HOUR:
                    path.unlink(missing_ok=True)
                continue
            if now - mtime > INBOX_MAX_AGE_MS:
                path.unlink(missing_ok=True)
                self.inbox_seen.pop(name, None)
                continue
            if self.inbox_seen.get(name) != mtime:
                fresh.append((mtime, name, path))
        fresh.sort(key=lambda f: f[0])
        for mtime, name, path in fresh:
            snap = C.read_json(path)
            if not isinstance(snap, dict):
                continue  # mid-write: retry next tick
            self.inbox_seen[name] = mtime
            self._ingest_snapshot(snap)

    def statusline_status(self) -> dict:
        settings_file = P.main_profile().settings_file
        try:
            mtime = os.stat(settings_file).st_mtime_ns
        except OSError:
            mtime = None
        info = self.statusline_info
        if mtime == info["mtime"] and C.now_ms() - info["checkedAt"] < 60_000:
            return info
        settings = C.read_json(settings_file) if mtime is not None else None
        sl = settings.get("statusLine") if isinstance(settings, dict) else None
        installed = C.is_our_statusline(sl)
        self.statusline_info = {"mtime": mtime, "checkedAt": C.now_ms(), "installed": installed, "other": bool(sl) and not installed}
        return self.statusline_info

    # ------------------------------------------------------------ views & alerts

    def view_of(self, a: dict, now: float) -> dict:
        s = self.state["settings"]
        cal = self.cal_for(a)
        log = self.spend.get(a["id"])
        official = a.get("official") or {}
        session = U.estimate_window(official.get("session"), log, cal["session"]["usdPerPct"], "session", now)
        weekly = U.estimate_window(official.get("weekly"), log, cal["weekly"]["usdPerPct"], "weekly", now)
        active = a["id"] == self.state["activeId"]
        last_spend = log.last() if log is not None else None
        last_running = a.get("lastRunningAt")
        running = (last_spend is not None and now - last_spend < RUNNING_MS) or (bool(last_running) and now - last_running < RUNNING_MS)
        # Burn rate of each limit in %/hour: the same spend, through each limit's calibration.
        rate = None
        if active or running:
            if log is not None:
                since = self.active_since(a["id"])
                rate = {
                    "session": U.spend_rate(log, cal["session"]["usdPerPct"], now, since),
                    "weekly": U.spend_rate(log, cal["weekly"]["usdPerPct"], now, since),
                }
            else:
                r = U.burn_rate(a.get("readings"), session, now)  # terminal-only account: from status line readings
                if r:
                    per_hour = r["perHour"] * cal["session"]["usdPerPct"] / cal["weekly"]["usdPerPct"]
                    rate = {"session": r, "weekly": {"perHour": per_hour, "basis": r["basis"]}}
        readings = [w for w in (official.get("session"), official.get("weekly")) if w]
        latest = max(readings, key=lambda w: w.get("at") or 0) if readings else None
        scoped = a.get("scoped") if isinstance(a.get("scoped"), dict) else None
        v = {
            "id": a["id"],
            "label": a.get("label") or "",
            "displayName": name_of(a),
            "email": a.get("email") or None,
            "name": a.get("name") or None,
            "plan": C.plan_label(a),
            "active": active,
            "running": running,
            "signedIn": self.profiles_of(a["id"]),
            "hasData": bool(latest),
            "updatedAt": latest.get("at") if latest else None,
            "source": latest.get("source") if latest else None,
            "lastActiveAt": a.get("lastActiveAt") or None,
            "lastRunningAt": max(last_running or 0, last_spend or 0) or None,
            "session": session,
            "weekly": weekly,
            "scoped": [{"label": i.get("label"), **(U.project(i, now) or {})} for i in (scoped or {}).get("items") or [] if isinstance(i, dict)],
            "spend": {**a["extra"], **(U.project(a["extra"], now) or {})} if isinstance(a.get("extra"), dict) else None,
            "rate": rate,
            "forecast": U.forecast_limits(session, weekly, rate, now, s),
            "calibration": {"session": cal["session"], "weekly": cal["weekly"]},
        }
        v["status"] = U.status_of(v, s)
        return v

    def next_hint(self, views: list[dict], rec: dict | None) -> str:
        if not rec:
            return "No other account is available." if len(views) > 1 else "No other accounts known yet."
        if rec.get("id"):
            v = next(x for x in views if x["id"] == rec["id"])
            return f"Switch to {v['displayName']} (5h {pct_text(v['session'])}, weekly {pct_text(v['weekly'])})."
        v = next((x for x in views if x["id"] == rec.get("freeId")), None)
        who = f" ({v['displayName']})" if v else ""
        return f"No spare account until {when_clock(rec['freeAt'], C.now_ms())}{who}."

    def _fire(self, key: str, level: str, title: str, body: str, alert: str, kind: str, v: dict, views: list[dict], rec: dict | None, extra: dict | None = None) -> None:
        if key in self.state["fired"]:
            return
        now = C.now_ms()
        self.state["fired"][key] = now
        self.dirty = True
        self.log_event("alert", f"{title} — {body}", level)
        self.last_alert = {"id": key, "level": level, "title": title, "body": body, "at": now}
        if self.state["settings"].get("events") and not self.readonly:
            try:
                self._emit(alert, level, title, body, kind, v, views, rec, extra or {})
            except OSError as e:
                self.warn("event", e)

    def _accounts_digest(self, views: list[dict]) -> str:
        lines = []
        for v in views[:8]:
            tag = " (running)" if v["active"] or v["running"] else ""
            plan = f", {v['plan']}" if v.get("plan") else ""
            if v["hasData"]:
                lines.append(f"- {v['displayName']}{plan}{tag}: 5-hour {pct_text(v['session'])}, weekly {pct_text(v['weekly'])} used")
            else:
                lines.append(f"- {v['displayName']}{plan}{tag}: no figures yet")
        return "\n".join(lines)

    def _emit(self, alert: str, level: str, title: str, body: str, kind: str, v: dict, views: list[dict], rec: dict | None, extra: dict) -> Path:
        w = v.get(kind) or {}
        fm: dict[str, Any] = {
            "alert": _ALERT_NAMES[alert],
            "level": level,
            "limit": LIMITS[kind]["name"],
            "account": v["displayName"],
            "email": v.get("email") if v.get("email") and v.get("email") != v["displayName"] else None,
            "plan": v.get("plan") or None,
        }
        if alert != "back":
            fm["used_pct"] = U.js_round(w.get("pct") or 0)
            fm["estimated"] = bool(w.get("estimated"))
            fm["resets_at"] = iso(w.get("resetsAt"))
        fm.update(extra)
        if rec and rec.get("id") and rec["id"] != v["id"]:
            nxt = next((x for x in views if x["id"] == rec["id"]), None)
            if nxt:
                fm["switch_to"] = nxt["displayName"]
                fm["switch_to_used"] = f"5-hour {pct_text(nxt['session'])}, weekly {pct_text(nxt['weekly'])}"
        elif rec and rec.get("freeAt"):
            fm["no_spare_account_until"] = iso(rec["freeAt"])
        fm["dashboard"] = self.dashboard_url
        text = f"**{title}**\n\n{body}\n\nAccounts:\n{self._accounts_digest(views)}"
        if alert != "back":
            text += "\n\nTo switch, run /login in Claude Code and pick the other account."
        if self.dashboard_url:
            text += f"\n\nDashboard (on the computer running Cremind): {self.dashboard_url}"
        return write_event(ALERT_EVENT_TYPES[alert], title, fm, text, self.events_dir)

    def _evaluate_alerts(self, views: list[dict], rec: dict | None, now: float) -> None:
        s = self.state["settings"]
        for v in views:
            name = v["displayName"]
            for kind in ("session", "weekly"):
                L = LIMITS[kind]
                w = v[kind]
                if not w:
                    continue
                if v["active"] or v["running"]:
                    if not w.get("resetsAt"):
                        continue

                    def key(type_: str, _v: dict = v, _w: dict = w, _L: dict = L) -> str:
                        return _L["key"](type_, _v["id"], _w["resetsAt"])

                    f = v["forecast"] and v["forecast"].get(kind)
                    hits = bool(f) and not f["idle"] and not f["resetsFirst"]
                    pace = f"At this pace the limit hits ~{when_clock(f['limitAt'], now)}. " if hits else ""
                    pace_fields = {"runs_out_at": iso(f["limitAt"])} if hits else {}
                    if w["pct"] >= 100:
                        verb = "has probably hit" if w.get("estimated") else "hit"
                        self._fire(key("limit"), "critical", f"{name} {verb} its {L['name']} limit", f"Resets {when_clock(w['resetsAt'], now)}. {self.next_hint(views, rec)}", "limit", kind, v, views, rec)
                    elif w["pct"] >= s[L["crit"]]:
                        self._fire(key("crit"), "critical", f"Switch now: {name} at {pct_text(w)} of its {L['name']} limit", pace + self.next_hint(views, rec), "crit", kind, v, views, rec, pace_fields)
                    elif w["pct"] >= s[L["warn"]]:
                        self._fire(key("warn"), "warning", f"{name} at {pct_text(w)} of its {L['name']} limit", pace + self.next_hint(views, rec), "warn", kind, v, views, rec, pace_fields)
                    if hits and w["pct"] < 100 and s["etaAlertMin"] > 0 and f["limitAt"] - now <= s["etaAlertMin"] * MIN:
                        mins = max(1, U.js_round((f["limitAt"] - now) / MIN))
                        rate = v["rate"][kind]
                        self._fire(
                            key("eta"),
                            "critical",
                            f"{name} hits its {L['name']} limit in ~{mins} min",
                            f"{pct_text(w)} used, {rate_text(rate)}. {self.next_hint(views, rec)}",
                            "eta",
                            kind,
                            v,
                            views,
                            rec,
                            {"runs_out_at": iso(f["limitAt"]), "minutes_left": mins, "pace_pct_per_hour": round(rate["perHour"], 1)},
                        )
                elif w.get("rolledOver") and w.get("lastPct", 0) >= s[L["crit"]] and now - w["endedAt"] < 30 * MIN and U.usable(v, s):
                    # Not while the other limit still blocks it.
                    prefix = "weekly-back" if kind == "weekly" else "back"
                    self._fire(f"{prefix}:{v['id']}:{U.js_round(w['endedAt'] / MIN)}", "info", f"{name} is available again", f"Its {L['name']} limit has reset.", "back", kind, v, views, rec)

    def _write_summary(self, views: list[dict], rec: dict | None, now: float) -> None:
        """Small file the status line reads to show "next →" and the projected limit time."""
        s = self.state["settings"]
        nxt = next((v for v in views if v["id"] == rec["id"]), None) if rec and rec.get("id") else None
        accounts = {}
        for v in views:
            first = v["forecast"] and v["forecast"].get("first")  # whichever limit runs out first
            accounts[v["id"]] = {
                "label": v["displayName"],
                "limitAt": U.js_round(first["limitAt"] / MIN) * MIN if first and math.isfinite(first["limitAt"]) else None,
                "limitKind": first["kind"] if first else None,
            }
        summary = {
            "settings": {k: s[k] for k in ("warnPct", "critPct", "weeklyWarnPct", "weeklyCritPct")},
            "accounts": accounts,
            "next": {
                "id": nxt["id"],
                "label": nxt["displayName"],
                "sessionPct": U.js_round(nxt["session"]["pct"] if nxt["session"] else 0),
                "weeklyPct": U.js_round(nxt["weekly"]["pct"] if nxt["weekly"] else 0),
            }
            if nxt
            else None,
            "freeAt": rec["freeAt"] if rec and not rec.get("id") else None,
        }
        key = json.dumps(summary, sort_keys=True)
        if key == self.last_summary[0] and now - self.last_summary[1] < 60_000:
            return
        try:
            C.write_json_atomic(self.paths.summary, {"at": now, **summary})
            self.last_summary = (key, now)
        except OSError as e:
            self.warn("summary", e)

    def sort_views(self, views: list[dict]) -> list[dict]:
        s = self.state["settings"]

        def rank(v: dict) -> tuple:
            if v["active"]:
                return (0, 0, 0)
            if not v["hasData"]:
                return (3, 0, 0)
            if U.usable(v, s):
                return (1, (1000 if U.pct_of(v["weekly"]) >= s["weeklyWarnPct"] else 0) + U.pct_of(v["session"]), U.pct_of(v["weekly"]))
            return (2, U.blocked_until(v, s), 0)

        return sorted(views, key=lambda v: (*rank(v), v["displayName"].casefold()))

    def _client_build(self) -> str:
        """Changes when a dashboard file does, so an open dashboard reloads itself after an update."""
        now = C.now_ms()
        if now - self._build[0] < 5000:
            return self._build[1]
        parts = []
        for f in STATIC_FILES:
            try:
                parts.append(format(int(os.stat(C.DASHBOARD_DIR / f).st_mtime), "x"))
            except OSError:
                pass
        self._build = (now, "".join(parts))
        return self._build[1]

    def api_state(self) -> dict:
        with self.lock:
            now = C.now_ms()
            s = self.state["settings"]
            views = self.sort_views([self.view_of(a, now) for a in self.state["accounts"].values()])
            rec = U.recommend(views, s)
            hero = next((v for v in views if v["active"]), None)
            if hero is None:
                running = sorted((v for v in views if v["running"]), key=lambda v: v["lastRunningAt"] or 0, reverse=True)
                hero = running[0] if running else None
            if hero and hero["session"] and hero["session"].get("resetsAt"):
                a = self.state["accounts"][hero["id"]]
                start = hero["session"]["resetsAt"] - U.SESSION_MS
                hero["series"] = U.window_series(hero["session"], (a.get("official") or {}).get("session"), self.spend.get(hero["id"]), hero["calibration"]["session"]["usdPerPct"], now)
                hero["marks"] = [
                    [x[0], x[1]]
                    for x in a.get("readings") or []
                    if x[0] >= start and x[1] is not None and (x[2] is None or abs(x[2] - hero["session"]["resetsAt"]) < 15 * MIN)
                ]
            live_sessions = sum(1 for x in self.sessions.values() if now - x["at"] < RUNNING_MS)
            return C.js_safe(
                {
                    "now": now,
                    "build": self._client_build(),
                    "ready": self.ready,
                    "settings": s,
                    "activeId": self.state["activeId"],
                    "heroId": hero["id"] if hero else None,
                    "recommendation": rec,
                    "accounts": views,
                    "profiles": [
                        {
                            "name": p.name,
                            "kind": p.p.kind,
                            "main": p.p.main,
                            "dir": str(p.p.dir),
                            "accountId": self.current_account(p.name),
                            "account": name_of(self.state["accounts"][aid]) if (aid := self.current_account(p.name)) in self.state["accounts"] else None,
                        }
                        for p in self.profiles
                    ],
                    "link": {
                        **self.statusline_status(),
                        "lastAt": self.state["lastLinkAt"] or None,
                        "liveSessions": live_sessions,
                        "command": C.statusline_command(),
                    },
                    "events": list(reversed(self.state["events"][-40:])),
                    "alert": self.last_alert,
                    "eventTypes": list(EVENT_TYPES),
                    "dashboardUrl": self.dashboard_url,
                }
            )

    def _prune(self, now: float) -> None:
        self.last_prune = now
        for account_id, log in list(self.spend.items()):
            log.prune(now - KEEP_MS)
            if not log.m or account_id not in self.state["accounts"]:
                del self.spend[account_id]
        for key, t in list(self.state["fired"].items()):
            if not isinstance(t, (int, float)) or now - t > KEEP_MS:
                del self.state["fired"][key]
        self.ledger_dirty = True

    # ------------------------------------------------------------ the loop

    def tick(self) -> None:
        with self.lock:
            now = C.now_ms()
            try:
                self.sync_profiles(now)
            except Exception as e:  # noqa: BLE001 - one bad source must not stop the monitor
                self.warn("profiles", e)
            for rt in self.profiles:
                try:
                    self._check_profile_config(rt, now)
                except Exception as e:  # noqa: BLE001
                    self.warn(f"config of {rt.name}", e)
                try:
                    self._ingest_transcripts(rt, now)
                except Exception as e:  # noqa: BLE001
                    self.warn(f"transcripts of {rt.name}", e)
            if not self.ready and all(rt.tail.caught_up for rt in self.profiles):
                self.ready = True
                print(f"{datetime.now():%H:%M:%S}  Session transcripts are up to date; live estimates are on.", flush=True)
            try:
                self._scan_inbox()
            except Exception as e:  # noqa: BLE001
                self.warn("inbox", e)
            if self.ready:
                try:
                    views = [self.view_of(a, now) for a in self.state["accounts"].values()]
                    rec = U.recommend(views, self.state["settings"])
                    self._evaluate_alerts(self.sort_views(views), rec, now)
                    self._write_summary(views, rec, now)
                except Exception as e:  # noqa: BLE001
                    self.warn("evaluate", e)
            if now - self.last_prune > 10 * MIN:
                self._prune(now)
            self.save_state(self.save_now)
            self.save_ledger(self.save_now)
            self.save_now = False

    def refresh_readonly(self) -> None:
        """Read who is signed in where without following transcripts: the CLI's view of
        the last known figures while the monitor is not running."""
        with self.lock:
            now = C.now_ms()
            self.sync_profiles(now)
            for rt in self.profiles:
                self._check_profile_config(rt, now)

    def close(self) -> None:
        with self.lock:
            for rt in self.profiles:
                rt.tail.close()
            self.save_state(True)
            self.save_ledger(True)

    # ------------------------------------------------------------ actions from the dashboard and the CLI

    def apply_settings(self, body: dict) -> dict:
        with self.lock:
            s = self.state["settings"]

            def to_int(value: Any, lo: int, hi: int) -> int | None:
                if isinstance(value, bool):
                    return None
                try:
                    n = float(value)
                except (TypeError, ValueError):
                    return None
                return min(hi, max(lo, U.js_round(n))) if math.isfinite(n) else None

            for k, lo, hi in (("warnPct", 1, 100), ("critPct", 1, 100), ("weeklyWarnPct", 1, 100), ("weeklyCritPct", 1, 100), ("etaAlertMin", 0, 120)):
                if k in body:
                    value = to_int(body[k], lo, hi)
                    if value is not None:
                        s[k] = value
            if s["critPct"] < s["warnPct"]:
                s["critPct"] = s["warnPct"]
            if s["weeklyCritPct"] < s["weeklyWarnPct"]:
                s["weeklyCritPct"] = s["weeklyWarnPct"]
            for k in ("events", "sound"):
                if isinstance(body.get(k), bool):
                    s[k] = body[k]
            self.dirty = True
            self.save_state(True)
            return dict(s)

    def find_account(self, ref: str) -> dict | None:
        """An account by id, id prefix, label or email."""
        ref = str(ref).strip().lower()
        accounts = list(self.state["accounts"].values())
        exact = [a for a in accounts if ref in (str(a["id"]).lower(), str(a.get("label") or "").lower(), str(a.get("email") or "").lower())]
        if exact:
            return exact[0]
        prefix = [a for a in accounts if str(a["id"]).lower().startswith(ref)]
        return prefix[0] if len(prefix) == 1 else None

    def set_label(self, account_id: str, label: str) -> bool:
        with self.lock:
            a = self.state["accounts"].get(account_id)
            if not a:
                return False
            a["label"] = str(label).strip()[:40]
            self.dirty = True
            self.save_state(True)
            return True

    def forget_account(self, account_id: str) -> bool:
        with self.lock:
            a = self.state["accounts"].pop(account_id, None)
            if not a:
                return False
            self.spend.pop(account_id, None)
            for rt in self.profiles:
                if self.current_account(rt.name) == account_id:
                    rt.config_mtime = None  # still signed in: re-added
            self.log_event("account", f"Removed {name_of(a)} from the monitor")
            self.ledger_dirty = True
            self.save_state(True)
            return True

    def open_profile(self, name: str) -> bool | None:
        """True when Claude Code was opened, False where that isn't supported, None for an unknown profile."""
        with self.lock:
            rt = next((p for p in self.profiles if p.name == name), None)
        return None if rt is None else P.open_terminal(rt.p)

    def test_alert(self, event_type: str = "limit_warning") -> Path:
        with self.lock:
            path = send_test_alert(event_type, self.dashboard_url, self.events_dir)
            self.log_event("test", f"Sent a test alert to Cremind events ({event_type})")
            self.save_state(True)
            return path


def send_test_alert(event_type: str, dashboard_url: str | None, events_dir: Path = C.EVENTS_DIR) -> Path:
    title = "Test alert from the Claude Usage Monitor"
    body = (
        f"**{title}**\n\nThis is a test of the {event_type} event, sent on request. A real alert "
        "names the account, the limit, how much of it is used, when it resets or runs out, and "
        "which account to switch to."
    )
    if dashboard_url:
        body += f"\n\nDashboard (on the computer running Cremind): {dashboard_url}"
    fm = {"alert": "test", "level": "info", "test": True, "dashboard": dashboard_url}
    return write_event(event_type, title, fm, body, events_dir)
