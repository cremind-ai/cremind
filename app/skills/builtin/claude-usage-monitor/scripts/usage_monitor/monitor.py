"""The monitor: follows Claude Code's own files, keeps every account's usage, raises alerts.

Where the numbers come from:

- Anthropic's usage API (``live.py``) — every account's official figures, the ones
  claude.ai shows, fetched with the account's Claude login every minute or so.
- ``<profile>/.claude.json`` — which account each Claude Code profile is signed in to,
  plus Claude Code's own cached usage: official, refreshed now and then.
- ``.monitor/inbox/*.json`` — status line snapshots after each terminal reply: official.
- ``<profile>/projects/**/*.jsonl`` — session transcripts (VS Code and terminal): what
  each reply cost and any limit hits, which roll the official figure forward when no
  recent live reading is at hand.

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
from . import live
from . import planner as PL
from . import profiles as P
from . import switch as switching
from . import usage as U
from .events import ALERT_EVENT_TYPES, EVENT_TYPES, write_event
from .planner import clock, day_clock, when_clock  # noqa: F401 - the CLI imports them from here
from .transcripts import TranscriptTail

MIN = U.MIN
HOUR = U.HOUR
TICK_MS = float(os.environ.get("CLAUDE_USAGE_MONITOR_TICK_MS") or 2000)
RUNNING_MS = 6 * MIN  # spent or reported this recently => the account is running
KEEP_MS = 8 * 24 * HOUR  # spend, readings and fired alerts older than this are dropped
READING_EVERY_MS = 5 * MIN  # an unchanged reading is kept in the history this often
READINGS_MAX = 1000
VIEWED_MS = 10_000  # the dashboard asked this recently => someone is watching
INBOX_MAX_AGE_MS = 24 * HOUR
PROFILE_CHECK_MS = 10_000  # how often this Cremind profile's own Claude home is looked for
STATIC_FILES = ("index.html", "app.js", "style.css", "icon.svg")
AUTO_COOLDOWN_MS = 2 * MIN  # between automatic switches, unless a limit is hit or a week is full
AUTO_EARLY_GAP_MS = 30 * MIN  # an early switch never comes sooner than this after another switch
AUTO_RETRY_MS = 60_000  # after a failed automatic switch
AUTO_GIVE_UP = 3  # failed rounds before auto_switch_failed is sent; then a try every 10 minutes
CLAIM_FILE = ".claude-usage-monitor-auto.json"  # in the main Claude home: the monitor running auto mode
CLAIM_STALE_MS = 2 * MIN
IN_USE_CHECK_MS = 30_000


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
        self.poller: live.LivePoller | None = None  # set by the listener; the CLI polls once instead
        self.live_status: dict[str, dict] = {}  # account id -> the poller's view of it
        self.viewed_at = 0.0
        self.switch_lock = threading.Lock()  # one account switch at a time; the listener waits for it on exit
        self.switching = False  # files are being moved: don't read them half-way, nor poll Anthropic
        self.switched_at = 0.0
        self.quiet: dict[str, float] = {}  # profile -> until when a switch's own log line covers its changes
        self.kept: dict[str, str] = {}  # extra profile -> the account whose place it keeps (registry)
        self.auto_thread: threading.Thread | None = None  # an automatic switch on its way
        self.auto_retry_at = 0.0
        self.auto_failures = 0
        self.last_switch_at = 0.0  # the last switch that happened (a refused one doesn't count)
        self.left_main: dict[str, float] = {}  # account -> when your Claude Code stopped using it
        self.auto_owner: str | None = None  # another Cremind profile runs auto mode for this Claude Code
        self._claim_at = 0.0
        self._in_use: dict[str, tuple[float, bool]] = {}  # extra profile -> (checked at, used lately)

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
            # account -> {until, at}: its weekly limit is full; not switched to until its week resets
            "setAside": {str(k): v for k, v in s["setAside"].items() if isinstance(v, dict) and U.to_num(v.get("until"))}
            if isinstance(s.get("setAside"), dict)
            else {},
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
        for k in ("email", "name", "org", "orgId", "orgType", "tier"):
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
        quiet = now < self.quiet.get(rt.name, 0)  # a switch from here: its own log line says it all
        if rt.p.main:
            prev = self.state["accounts"].get(self.state["activeId"]) if self.state["activeId"] else None
            self.state["activeId"] = account_id
            if prev and prev["id"] != account_id:
                self.left_main[prev["id"]] = now  # still "running" for a while: its usage was here
            if a:
                a["lastActiveAt"] = now
            if quiet:
                pass
            elif cur is _UNSET:
                if a:
                    self.log_event("switch", f"Claude Code is signed in to {name_of(a)}")
            elif a:
                pv = self.view_of(prev, now) if prev and prev["id"] != account_id else None
                src = f" (from {name_of(prev)} at 5-hour {pct_text(pv['session'])}, weekly {pct_text(pv['weekly'])})" if pv else ""
                self.log_event("switch", f"Claude Code switched to {name_of(a)}{src}")
            else:
                self.log_event("switch", "Claude Code is signed out")
        elif quiet:
            pass
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

    def calibrate(self, a: dict, kind: str, prev: dict | None, nxt: dict) -> bool:
        """When a new official reading lands, compare the rise since ``prev`` (or since its
        window opened) with what the account spent in between, and refine USD-per-percent.

        Returns whether ``prev`` stays the base for a later sample: live readings arrive every
        minute or so, far too close together for one step to measure anything, so the base
        holds until the rise is large enough."""
        if not prev:
            return False
        if not self.ready:
            return True
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
            return False
        rise = nxt["pct"] - base[1]
        if rise < 0:
            return False
        if rise < (4 if kind == "weekly" else 8):
            return True
        if not self.covered(a["id"], base[0], nxt["at"]):
            return False
        usd = (self.spend.get(a["id"]) or U.EMPTY_LOG).between(base[0], nxt["at"])
        cal = self.cal_for(a)
        blended = U.blend_calibration(cal[kind], usd / rise)
        if not blended:
            return False
        cal[kind] = blended
        self.dirty = True
        what = "weekly" if kind == "weekly" else "5-hour"
        plural = "" if blended["n"] == 1 else "s"
        self.log_event(
            "calibration",
            f"Calibrated on {name_of(a)}: 1% of the {what} limit ≈ ${blended['usdPerPct']:.2f} of usage at list price ({blended['n']} reading{plural})",
        )
        return False

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
            anchors = a["calAnchor"] if isinstance(a.get("calAnchor"), dict) else {}
            anchor = anchors.get(kind) or cur
            anchors[kind] = anchor if self.calibrate(a, kind, anchor, nxt) else nxt
            a["calAnchor"] = anchors
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
        self.dirty = True
        s = r.get("session")
        row = [r["at"], s["pct"] if s else None, s.get("resetsAt") if s else None, r["weekly"]["pct"] if r.get("weekly") else None]
        readings = a["readings"]
        # Live readings arrive every minute or so: keep the changes, and an unchanged figure
        # every few minutes.
        last = readings[-1] if readings else None
        if (
            last is not None
            and 0 <= r["at"] - last[0] < READING_EVERY_MS
            and last[1] == row[1]
            and last[3] == row[3]
            and (last[2] == row[2] or (last[2] is not None and row[2] is not None and abs(last[2] - row[2]) < 2 * MIN))
        ):
            return
        readings.append(row)
        if len(readings) > 1 and readings[-2][0] > r["at"]:
            readings.sort(key=lambda x: x[0])
        cutoff = C.now_ms() - KEEP_MS
        while readings and (readings[0][0] < cutoff or len(readings) > READINGS_MAX):
            readings.pop(0)

    # ------------------------------------------------------------ live figures (live.py)

    def running_now(self, a: dict, now: float) -> bool:
        """Spent, reported or seen rising this recently: the account is in use."""
        log = self.spend.get(a["id"])
        last_spend = log.last() if log is not None else None
        last_running = a.get("lastRunningAt")
        return (last_spend is not None and now - last_spend < RUNNING_MS) or (bool(last_running) and now - last_running < RUNNING_MS)

    def live_targets(self, now: float) -> list[dict] | None:
        """What the poller asks Anthropic about: every account signed in to a tracked profile,
        with its organization, those profiles (and the config stamp this monitor last read in
        each), whether it is in use, and whether the dashboard is open. None when live figures
        are off."""
        with self.lock:
            if not self.state["settings"].get("live", True):
                return None
            watched = now - self.viewed_at < VIEWED_MS
            groups: dict[str, dict] = {}
            for rt in self.profiles:
                account_id = self.current_account(rt.name)
                if not account_id or account_id not in self.state["accounts"]:
                    continue
                org_id = self.state["accounts"][account_id].get("orgId")
                g = groups.setdefault(account_id, {"account": account_id, "orgId": org_id, "profiles": [], "busy": False, "watched": watched})
                g["profiles"].append((rt.p, rt.config_mtime))
            for account_id, g in groups.items():
                g["busy"] = self.running_now(self.state["accounts"][account_id], now)
            return list(groups.values())

    def apply_live(self, account_id: str, reading: dict) -> None:
        """A reading from Anthropic. Usage that rose since a reading of a few minutes ago means
        the account is in use right now — on this computer or anywhere else."""
        with self.lock:
            a = self.state["accounts"].get(account_id)
            if not a:
                return
            official = a.get("official") or {}
            for kind in ("session", "weekly"):
                prev, w = official.get(kind), reading.get(kind)
                if not prev or not w or w["pct"] <= prev["pct"] or reading["at"] - (prev.get("at") or 0) > RUNNING_MS:
                    continue
                if prev.get("resetsAt") is None or w.get("resetsAt") is None or abs(prev["resetsAt"] - w["resetsAt"]) < 15 * MIN:
                    a["lastRunningAt"] = max(a.get("lastRunningAt") or 0, reading["at"])
                    break
            if not reading.get("spend") and a.get("extra"):
                a["extra"] = None  # extra usage was turned off
            self.apply_reading(a, reading)

    def note_live(self, statuses: dict[str, dict]) -> None:
        """The poller's view of every account after each round; changes worth knowing go to
        the activity log."""
        with self.lock:
            for account_id, st in statuses.items():
                old = (self.live_status.get(account_id) or {}).get("state")
                new = st["state"]
                a = self.state["accounts"].get(account_id)
                if not a or new == old or new in ("pending", "off"):
                    continue
                if new == "ok":
                    if old in (None, "pending", "off"):
                        v = self.view_of(a, C.now_ms())
                        self.log_event("live", f"Official figures from Anthropic for {name_of(a)}: 5-hour {pct_text(v['session'])}, weekly {pct_text(v['weekly'])}")
                    else:
                        self.log_event("live", f"Official figures for {name_of(a)} are back")
                else:
                    self.log_event("live", f"Can't get official figures for {name_of(a)} right now: {st['detail']}", "warning")
            self.live_status = statuses

    def recently_switched(self, now: float) -> bool:
        """A switch is moving logins, or just did: a login and a config that disagree are
        half-way through it, not a problem."""
        return self.switching or now - self.switched_at < 15_000

    def note_live_event(self, text: str, level: str = "info") -> None:
        with self.lock:
            self.log_event("live", text, level)

    def refresh_live(self, wait_s: float) -> bool:
        """Ask Anthropic for every account now (the dashboard opening, the agent asking for the
        status). Never holds the lock while waiting: the answers need it."""
        poller = self.poller
        return poller.refresh(wait_s) if poller is not None else False

    # ------------------------------------------------------------ automatic switching (planner.py)

    def _in_own_window(self, v: dict) -> bool:
        """A Claude Code window opened on the account's own profile ran lately: its login can't
        be moved out from under it."""
        now = C.now_ms()
        for rt in self.profiles:
            if rt.p.kind != "extra" or rt.name not in (v.get("signedIn") or []):
                continue
            seen = self._in_use.get(rt.name)
            if not seen or now - seen[0] > IN_USE_CHECK_MS:
                seen = self._in_use[rt.name] = (now, switching.recently_used(rt.p))
            if seen[1]:
                return True
        return False

    def plan_for(self, views: list[dict], now: float, current_id: Any = _UNSET) -> dict:
        """What should happen to the account your Claude Code uses (or ``current_id``) now."""
        cid = self.state["activeId"] if current_id is _UNSET else current_id
        return PL.plan(views, self.state["settings"], now, current_id=cid, aside=self.state["setAside"], in_use=self._in_own_window)

    def _plan_view(self, p: dict, views: list[dict], now: float) -> dict:
        s = self.state["settings"]
        accounts = self.state["accounts"]
        return {
            **p,
            "mode": s.get("switchMode", "manual"),
            "early": bool(s.get("autoEarly")),
            "owner": self.auto_owner,  # another Cremind profile runs auto mode for this Claude Code
            "setAside": [{"id": k, "account": name_of(accounts[k]), "until": e["until"]} for k, e in self.state["setAside"].items() if k in accounts],
            "hoursLeft": PL.hours_left(views, s, now, p["pace"]["usdPerHour"]),
        }

    def _update_set_aside(self, views: list[dict], now: float) -> None:
        """An account whose week is full is set aside until its week resets, or sooner when
        Anthropic's figures show the week was reset early."""
        _, t7 = PL.thresholds(self.state["settings"])
        aside = self.state["setAside"]
        for v in views:
            w = v.get("weekly") or {}
            entry = aside.get(v["id"])
            if entry:
                later_week = bool(w.get("resetsAt")) and w["resetsAt"] > entry["until"] + HOUR
                if now >= entry["until"] or later_week or (w and (w.get("pct") or 0) < t7 - 10):
                    del aside[v["id"]]
                    self.dirty = True
                    how = "has reset" if now >= entry["until"] - MIN else "was reset early"
                    self.log_event("auto", f"{v['displayName']} can take over again: its week {how}")
            elif (w.get("pct") or 0) >= t7 and w.get("resetsAt") and w["resetsAt"] > now:
                aside[v["id"]] = {"until": w["resetsAt"], "at": now}
                self.dirty = True
                self.log_event("auto", f"{v['displayName']} used {pct_text(w)} of its weekly limit: set aside until its week resets, {when_clock(w['resetsAt'], now)}")

    def _claim(self, now: float) -> bool:
        """Only one monitor may switch this computer's Claude Code by itself: Cremind profiles
        are independent, but they share ~/.claude. The claim lives beside the logins it moves."""
        home = P.main_profile().dir
        if not home.is_dir():
            return False
        path = home / CLAIM_FILE
        me = os.path.normcase(os.path.abspath(self.paths.data))
        data = C.read_json(path)
        mine = isinstance(data, dict) and data.get("owner") == me
        if isinstance(data, dict) and not mine and now - (U.to_num(data.get("at")) or 0) < CLAIM_STALE_MS:
            self.auto_owner = str(data.get("label") or data.get("owner") or "another monitor")
            return False
        if not mine or now - self._claim_at >= 30_000:
            body = {"owner": me, "label": C.owner_label(self.paths), "pid": os.getpid(), "at": now}
            try:
                if data is None and not path.exists():
                    with open(path, "x", encoding="utf-8") as f:  # two monitors claiming at once: one wins
                        json.dump(body, f)
                else:
                    C.write_json_atomic(path, body)
            except FileExistsError:
                return False  # claimed just now by another monitor; look again next tick
            except OSError as e:
                self.warn("auto mode", e)
                return False
            self._claim_at = now
        self.auto_owner = None
        return True

    def _release_claim(self) -> None:
        if not self._claim_at:
            return
        self._claim_at = 0.0
        path = P.main_profile().dir / CLAIM_FILE
        data = C.read_json(path)
        if isinstance(data, dict) and data.get("owner") == os.path.normcase(os.path.abspath(self.paths.data)):
            try:
                path.unlink()
            except OSError:
                pass

    def _auto_tick(self, p: dict, now: float) -> None:
        """Auto mode: switch when the plan says so — in a thread, as the switch asks Anthropic."""
        s = self.state["settings"]
        if s.get("switchMode") != "auto" or self.readonly:
            self._release_claim()
            self.auto_owner = None
            return
        if self.switching or (self.auto_thread is not None and self.auto_thread.is_alive()):
            return
        if not self._claim(now):
            return
        act = p["action"]
        if act["do"] == "wait":
            self._auto_stuck(p, now)
            return
        if act["do"] != "switch" or now < self.auto_retry_at:
            return
        since = now - self.last_switch_at
        if act["reason"] in ("session", "early") and since < AUTO_COOLDOWN_MS:
            return
        if act["reason"] == "early" and since < AUTO_EARLY_GAP_MS:
            return
        targets = [act["to"]]
        if act["reason"] != "early":
            targets += [c["id"] for c in p["candidates"] if c["ok"] and c["id"] != act["to"]]
        self.auto_thread = threading.Thread(target=self._auto_switch, args=(targets[:3], act, p), name="auto-switch", daemon=True)
        self.auto_thread.start()

    def _still_suitable(self, account_id: str, reading: dict | None) -> str | None:
        """The fresh figures the switch got for the account, before anything moves: None when
        it can still take over, else why not."""
        if not reading:
            return None
        self.apply_live(account_id, reading)
        t5, t7 = PL.thresholds(self.state["settings"])
        se, w = reading.get("session") or {}, reading.get("weekly") or {}
        if (w.get("pct") or 0) >= t7 - 1:
            return f"its week is at {U.js_round(w['pct'])}% now"
        if (se.get("pct") or 0) >= t5 - 5:
            return f"its 5-hour window is at {U.js_round(se['pct'])}% now"
        return None

    def _auto_switch(self, targets: list[str], act: dict, p: dict) -> None:
        failures: list[tuple[str, Exception]] = []
        for target in targets:
            pick = next((c["why"] for c in p["candidates"] if c["id"] == target), None)
            try:
                result = self.switch_account(target, why={**act, "pick": pick}, accept=lambda reading, t=target: self._still_suitable(t, reading))
            except switching.SwitchError as e:
                if e.reason == "already":
                    return  # switched meanwhile, by you or the agent
                failures.append((target, e))
                if e.reason in ("unsuitable", "in_use", "busy", "changed", "no_login", "signin", "mismatch", "not_here", "cremind_only"):
                    continue  # this account can't; the next may
                break
            except Exception as e:  # noqa: BLE001 - a failed switch must not end the listener
                failures.append((target, e))
                break
            else:
                with self.lock:
                    self.auto_failures = 0
                    self.auto_retry_at = 0.0
                    self._auto_switched(result, act, p)
                    self.save_state(True)
                return
        with self.lock:
            self._auto_failed(failures, act, p)
            self.save_state(True)

    def _auto_event(self, event_type: str, title: str, fm: dict, body: str) -> None:
        if not self.state["settings"].get("events") or self.readonly:
            return
        fm = {**fm, "dashboard": self.dashboard_url}
        text = f"**{title}**\n\n{body}"
        if self.dashboard_url:
            text += f"\n\nDashboard (on the computer running Cremind): {self.dashboard_url}"
        try:
            write_event(event_type, title, fm, text, self.events_dir)
        except OSError as e:
            self.warn("event", e)

    def _auto_switched(self, result: dict, act: dict, p: dict) -> None:
        now = C.now_ms()
        a = self.state["accounts"].get(result["account"])
        to = name_of(a) if a else result.get("email") or str(result["account"])[:8]
        cur = p.get("current") or {}
        was = cur.get("account") or result.get("fromEmail") or "the previous account"
        v = self.view_of(a, now) if a else None
        cand = next((c for c in p["candidates"] if c["id"] == result["account"]), None)
        nxt = next((c for c in p["candidates"] if c["ok"] and c["id"] != result["account"]), None)
        body = f"{act['because']}. Claude Code now uses {to}"
        body += f" ({cand['why']})." if cand else "."
        body += " Running sessions carry on from their next message, with no sign-in."
        aside = self.state["setAside"].get(cur.get("id") or "")
        if act["reason"] == "weekly" and aside:
            body += f" {was} is set aside until its week resets, {when_clock(aside['until'], now)}."
        if nxt:
            body += f" Next in line: {nxt['account']}."
        fm = {
            "alert": "auto_switched",
            "level": "info",
            "reason": {"session": "5-hour limit", "limit": "5-hour limit hit", "weekly": "weekly limit", "early": "early switch"}[act["reason"]],
            "from": was,
            "to": to,
            "from_used": f"5-hour {cur.get('session')}%, weekly {cur.get('weekly')}%" if cur else None,
            "to_used": f"5-hour {pct_text(v['session'])}, weekly {pct_text(v['weekly'])}" if v else None,
            "set_aside_until": iso(aside["until"]) if act["reason"] == "weekly" and aside else None,
            "next_in_line": nxt["account"] if nxt else None,
        }
        self._auto_event("auto_switched", f"Switched Claude Code to {to}", fm, body)

    def _auto_failed(self, failures: list[tuple[str, Exception]], act: dict, p: dict) -> None:
        now = C.now_ms()
        self.auto_failures += 1
        self.auto_retry_at = now + (10 * MIN if self.auto_failures >= AUTO_GIVE_UP else AUTO_RETRY_MS)
        accounts = self.state["accounts"]
        detail = "; ".join(f"{name_of(accounts[t]) if t in accounts else t[:8]}: {e}" for t, e in failures) or "no account to try"
        self.log_event("auto", f"Automatic switch didn't happen ({act['because']}): {detail}", "warning")
        if self.auto_failures != AUTO_GIVE_UP:
            return
        fix = next((e.fix for _, e in failures if isinstance(e, switching.SwitchError) and e.fix), None)
        cur = p.get("current") or {}
        body = f"{act['because']}, but Claude Code couldn't be switched: {detail}."
        if fix:
            body += f" To fix: {fix}."
        body += " Auto mode tries again every 10 minutes; you can also switch from the dashboard or ask Cremind."
        fm = {"alert": "auto_switch_failed", "level": "warning", "account": cur.get("account"), "error": detail, "fix": fix}
        self._auto_event("auto_switch_failed", "Couldn't switch Claude Code automatically", fm, body)

    def _auto_stuck(self, p: dict, now: float) -> None:
        """Nothing can take over: say so once per window, and when the first account frees up."""
        act, cur = p["action"], p["current"] or {}
        key = f"auto-none:{cur.get('id')}:{act['kind']}:{U.js_round((act.get('resetsAt') or 0) / (10 * MIN))}"
        if key in self.state["fired"]:
            return
        self.state["fired"][key] = now
        self.dirty = True
        self.log_event("auto", act["text"], "warning")
        free = self.state["accounts"].get(p.get("freeId") or "")
        fm = {
            "alert": "no_account_available",
            "level": "critical",
            "account": cur.get("account"),
            "limit": "weekly" if act["kind"] == "weekly" else "5-hour",
            "used_pct": U.js_round(act.get("pct") or 0),
            "resets_at": iso(act.get("resetsAt")),
            "first_free": name_of(free) if free else None,
            "first_free_at": iso(p.get("freeAt")),
        }
        body = act["text"] + " Claude Code stops for it once the limit runs out; auto mode switches as soon as an account frees up."
        self._auto_event("no_account_available", f"No account can take over from {cur.get('account')}", fm, body)

    def _auto_would_leave(self, account_id: str, now: float) -> str | None:
        """Why auto mode would switch away from this account at once, if it would."""
        a = self.state["accounts"].get(account_id)
        if not a:
            return None
        entry = self.state["setAside"].get(account_id)
        if entry:
            return f"its weekly limit is full until {when_clock(entry['until'], now)}"
        trig = PL.trigger(self.view_of(a, now), self.state["settings"])
        if trig:
            return f"it is at {U.js_round(trig['pct'])}% of its {'weekly' if trig['kind'] == 'weekly' else '5-hour'} limit"
        return None

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
            if old and not prev and now >= self.quiet.get(desc.name, 0):
                self.log_event("account", f'Now tracking profile "{desc.name}"')
            self._import_backups(rt)
            self.ready = False
            fresh.append(rt)
        for p in old.values():
            if p not in fresh:
                p.tail.close()
        self.profiles = fresh
        self.kept = {e["name"]: e["account"] for e in P.read_registry(self.paths) if e.get("account")}

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

    @staticmethod
    def _official_rate(a: dict, session: dict | None, now: float) -> dict | None:
        """Pace of the 5-hour window from Anthropic's own readings of the last 20 minutes."""
        o = (a.get("official") or {}).get("session")
        samples = list(a.get("readings") or [])
        if o and (not samples or o["at"] > samples[-1][0]):
            samples.append([o["at"], o["pct"], o.get("resetsAt"), None])  # the history keeps changes only
        r = U.burn_rate(samples, session, now)
        return {**r, "basis": "Anthropic's figures, last 20 min"} if r and r["basis"] == "last 20 min" else None

    def view_of(self, a: dict, now: float) -> dict:
        s = self.state["settings"]
        cal = self.cal_for(a)
        log = self.spend.get(a["id"])
        official = a.get("official") or {}

        def window(kind: str) -> dict | None:
            # A recent reading from Anthropic is shown as it is — what claude.ai shows.
            r = official.get(kind)
            exact = live.FRESH_MS if r and r.get("source") == "api" else None
            return U.estimate_window(r, log, cal[kind]["usdPerPct"], kind, now, exact)

        session = window("session")
        weekly = window("weekly")
        active = a["id"] == self.state["activeId"]
        last_spend = log.last() if log is not None else None
        last_running = a.get("lastRunningAt")
        running = self.running_now(a, now)
        # Burn rate of each limit in %/hour: the same usage, through each limit's calibration.
        rate = None
        if active or running:
            measured = self._official_rate(a, session, now) if (official.get("session") or {}).get("source") == "api" else None
            if measured:
                per_hour = measured["perHour"] * cal["session"]["usdPerPct"] / cal["weekly"]["usdPerPct"]
                rate = {"session": measured, "weekly": {"perHour": per_hour, "basis": measured["basis"]}}
            elif log is not None:
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
        signed_in = self.profiles_of(a["id"])
        extra = {p.name for p in self.profiles if p.p.kind == "extra"}
        v = {
            "id": a["id"],
            "label": a.get("label") or "",
            "displayName": name_of(a),
            "email": a.get("email") or None,
            "name": a.get("name") or None,
            "plan": C.plan_label(a),
            "active": active,
            "running": running,
            "signedIn": signed_in,
            # Its login is saved in an extra profile: one step makes it your Claude Code's account.
            "switchable": not active and any(n in extra for n in signed_in),
            "rotation": a.get("rotation", True) is not False,  # may be suggested and switched to
            "setAsideUntil": (self.state["setAside"].get(a["id"]) or {}).get("until"),  # its week is full
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
            "live": self.live_status.get(a["id"]),
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
                if rec.get("why"):
                    fm["switch_why"] = rec["why"]
        elif rec and rec.get("freeAt"):
            fm["no_spare_account_until"] = iso(rec["freeAt"])
        fm["dashboard"] = self.dashboard_url
        text = f"**{title}**\n\n{body}\n\nAccounts:\n{self._accounts_digest(views)}"
        if alert != "back" and fm.get("switch_to"):
            nxt = next((x for x in views if x["id"] == rec["id"]), None) if rec else None
            if nxt and nxt.get("switchable"):
                fm["switch_command"] = f"switch {nxt.get('email') or nxt['id']}"
                text += f"\n\nTo switch, ask Cremind to switch Claude Code to {nxt['displayName']} (no sign-in needed: its login is saved here), or press Switch on the dashboard."
                if rec.get("why"):
                    text += f" Why {nxt['displayName']}: {rec['why']}."
            else:
                text += "\n\nTo switch, sign that account in to a profile once (add-account), then switch from the dashboard or ask Cremind."
        if self.dashboard_url:
            text += f"\n\nDashboard (on the computer running Cremind): {self.dashboard_url}"
        return write_event(ALERT_EVENT_TYPES[alert], title, fm, text, self.events_dir)

    def _rec_for(self, v: dict, views: list[dict], rec: dict | None, now: float) -> dict | None:
        """The account to suggest in an alert about ``v``: never ``v`` itself. An account in use
        elsewhere (an extra profile, claude.ai) may be the best spare; then the others,
        the one signed in to the main profile included, are weighed instead."""
        if not rec or rec.get("id") != v["id"]:
            return rec
        return PL.recommendation(self.plan_for(views, now, current_id=v["id"]))

    def _evaluate_alerts(self, views: list[dict], rec: dict | None, now: float) -> None:
        s = self.state["settings"]
        # Auto mode switches your Claude Code before these matter: only a limit actually hit
        # is still reported for it (auto mode sends its own events).
        auto = s.get("switchMode") == "auto"
        all_rec = rec
        for v in views:
            name = v["displayName"]
            rec = self._rec_for(v, views, all_rec, now)
            # In automatic mode, the account your Claude Code uses — or used until moments ago,
            # whose usage still counts as running — is switched, not warned about.
            muted = auto and (v["active"] or now - self.left_main.get(v["id"], 0) < RUNNING_MS)
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
                    elif muted:
                        pass
                    elif w["pct"] >= s[L["crit"]]:
                        self._fire(key("crit"), "critical", f"Switch now: {name} at {pct_text(w)} of its {L['name']} limit", pace + self.next_hint(views, rec), "crit", kind, v, views, rec, pace_fields)
                    elif w["pct"] >= s[L["warn"]]:
                        self._fire(key("warn"), "warning", f"{name} at {pct_text(w)} of its {L['name']} limit", pace + self.next_hint(views, rec), "warn", kind, v, views, rec, pace_fields)
                    if hits and not muted and w["pct"] < 100 and s["etaAlertMin"] > 0 and f["limitAt"] - now <= s["etaAlertMin"] * MIN:
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
                elif not auto and w.get("rolledOver") and w.get("lastPct", 0) >= s[L["crit"]] and now - w["endedAt"] < 30 * MIN and U.usable(v, s):
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
            self.viewed_at = now  # the poller asks more often while someone is watching
            s = self.state["settings"]
            views = self.sort_views([self.view_of(a, now) for a in self.state["accounts"].values()])
            p = self.plan_for(views, now)
            rec = PL.recommendation(p)
            hero = next((v for v in views if v["active"]), None)
            if hero is None:
                running = sorted((v for v in views if v["running"]), key=lambda v: v["lastRunningAt"] or 0, reverse=True)
                hero = running[0] if running else None
            if hero and hero["session"] and hero["session"].get("resetsAt"):
                a = self.state["accounts"][hero["id"]]
                start = hero["session"]["resetsAt"] - U.SESSION_MS
                official = (a.get("official") or {}).get("session")
                hero["marks"] = [
                    [x[0], x[1]]
                    for x in a.get("readings") or []
                    if x[0] >= start and x[1] is not None and (x[2] is None or abs(x[2] - hero["session"]["resetsAt"]) < 15 * MIN)
                ]
                if official and official.get("source") == "api" and not hero["session"].get("estimated"):
                    # Live readings: the line runs through Anthropic's own figures.
                    hero["series"] = U.official_series(hero["marks"], start, now, hero["session"]["pct"])
                    hero["seriesKind"] = "official"
                else:
                    hero["series"] = U.window_series(hero["session"], official, self.spend.get(hero["id"]), hero["calibration"]["session"]["usdPerPct"], now)
                    hero["seriesKind"] = "estimate"
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
                    "plan": self._plan_view(p, views, now),
                    "accounts": views,
                    "profiles": [
                        {
                            "name": p.name,
                            "kind": p.p.kind,
                            "main": p.p.main,
                            "dir": str(p.p.dir),
                            "accountId": self.current_account(p.name),
                            "account": name_of(self.state["accounts"][aid]) if (aid := self.current_account(p.name)) in self.state["accounts"] else None,
                            # Emptied by a switch: the account whose place it keeps.
                            "keeps": name_of(self.state["accounts"][kid]) if (kid := self.kept.get(p.name)) in self.state["accounts"] and not aid else None,
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
                    "live": {
                        "enabled": bool(s.get("live", True)),
                        "everySec": {"inUse": U.js_round(live.ACTIVE_MS / 1000), "watched": U.js_round(live.WATCHED_MS / 1000), "idle": U.js_round(live.IDLE_MS / 1000)},
                    },
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
            if not self.switching:  # a switch moves logins between profiles: read them once it's done
                try:
                    self.sync_profiles(now)
                except Exception as e:  # noqa: BLE001 - one bad source must not stop the monitor
                    self.warn("profiles", e)
            for rt in self.profiles:
                try:
                    if not self.switching:
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
                    self._update_set_aside(views, now)
                    p = self.plan_for(views, now)
                    rec = PL.recommendation(p)
                    self._evaluate_alerts(self.sort_views(views), rec, now)
                    self._write_summary(views, rec, now)
                    self._auto_tick(p, now)
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
            self._release_claim()
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
            was_live = bool(s.get("live", True))
            for k in ("events", "sound", "live"):
                if isinstance(body.get(k), bool):
                    s[k] = body[k]
            if bool(s["live"]) != was_live:
                self.log_event("live", "Official figures from Anthropic turned on" if s["live"] else "Official figures from Anthropic turned off: figures are estimates from now on")
                if self.poller is not None:
                    self.poller.refresh(0)
            # Auto mode needs a threshold below 100%: at 100% Claude Code has already stopped.
            auto_was = (s.get("autoSessionPct"), s.get("autoWeeklyPct"), bool(s.get("autoEarly")))
            for k, lo, hi in (("autoSessionPct", 50, 99), ("autoWeeklyPct", 50, 100)):
                if k in body:
                    value = to_int(body[k], lo, hi)
                    if value is not None:
                        s[k] = value
            if isinstance(body.get("autoEarly"), bool):
                s["autoEarly"] = body["autoEarly"]
            mode = body.get("switchMode")
            if s.get("switchMode") == "auto" and mode != "manual" and auto_was != (s.get("autoSessionPct"), s.get("autoWeeklyPct"), bool(s.get("autoEarly"))):
                # They decide when your Claude Code moves: a switch is explained by the values of the moment.
                self.log_event(
                    "auto",
                    f"Automatic switching now at {s['autoSessionPct']}% of the 5-hour limit or {s['autoWeeklyPct']}% of the weekly one"
                    + (", and early to use up a week that resets soon" if s.get("autoEarly") else ""),
                )
            if mode in ("manual", "auto") and mode != s.get("switchMode"):
                s["switchMode"] = mode
                if mode == "auto":
                    self.auto_failures = 0
                    self.auto_retry_at = 0.0
                    self.log_event(
                        "auto",
                        f"Automatic switching on: your Claude Code moves to the best account at {s['autoSessionPct']}% of the 5-hour limit or {s['autoWeeklyPct']}% of the weekly one",
                    )
                else:
                    self.log_event("auto", "Automatic switching off: alerts only; you or the agent switch")
            self.dirty = True
            self.save_state(True)
            return dict(s)

    def find_account(self, ref: str) -> dict | None:
        """An account by id, label, email or the profile it is signed in to; else by a part of
        its email or name that only it has, or an id prefix."""
        ref = str(ref).strip().lower()
        if not ref:
            return None
        accounts = list(self.state["accounts"].values())
        exact = [a for a in accounts if ref in (str(a["id"]).lower(), str(a.get("label") or "").lower(), str(a.get("email") or "").lower())]
        if exact:
            return exact[0]
        profile = next((p for p in self.profiles if p.name.lower() == ref), None)
        if profile is not None:
            account_id = self.current_account(profile.name) or self.kept.get(profile.name)
            if account_id in self.state["accounts"]:
                return self.state["accounts"][account_id]
        for match in (
            lambda a: str(a["id"]).lower().startswith(ref),
            lambda a: str(a.get("email") or "").lower().split("@")[0] == ref,
            lambda a: any(ref in str(a.get(k) or "").lower() for k in ("email", "label", "name")),
        ):
            found = [a for a in accounts if match(a)]
            if len(found) == 1:
                return found[0]
        return None

    def set_label(self, account_id: str, label: str) -> bool:
        with self.lock:
            a = self.state["accounts"].get(account_id)
            if not a:
                return False
            a["label"] = str(label).strip()[:40]
            self.dirty = True
            self.save_state(True)
            return True

    def set_rotation(self, account_id: str, on: bool) -> bool:
        """Whether an account may be suggested and switched to."""
        with self.lock:
            a = self.state["accounts"].get(account_id)
            if not a:
                return False
            if on:
                a.pop("rotation", None)
            else:
                a["rotation"] = False
            self.log_event("auto", f"{name_of(a)} {'is back in' if on else 'is left out of'} switching")
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

    def switch_account(self, account_id: str, force: bool = False, *, why: dict | None = None, accept: Any = None) -> dict:
        """Make an account the one your Claude Code uses, with the login saved for it here
        (``switch.py``). Raises ``switching.SwitchError``. Claude Code's locks and Anthropic
        are waited on without holding the monitor's lock.

        ``why``: auto mode's action (planner.py) when auto mode switches; ``accept``: checks the
        account's fresh figures before anything moves."""
        with self.switch_lock:
            with self.lock:
                now = C.now_ms()
                if why is None and self.state["settings"].get("switchMode") == "auto" and account_id != self.state["activeId"]:
                    blocked = self._auto_would_leave(account_id, now)
                    if blocked:
                        a = self.state["accounts"].get(account_id)
                        raise switching.SwitchError(
                            "auto_blocked",
                            f"Automatic switching would move away from {name_of(a) if a else account_id[:8]} at once: {blocked}",
                            "turn automatic switching off first (settings --switching manual), or pick another account",
                        )
                prev_id = self.state["activeId"]
                prev = self.state["accounts"].get(prev_id) if prev_id and prev_id != account_id else None
                pv = self.view_of(prev, now) if prev else None
                self.switching = True
            try:
                result = switching.switch_to(self.paths, account_id, force=force, accept=accept)
            finally:
                with self.lock:
                    self.switching = False
                    self.switched_at = C.now_ms()
            with self.lock:
                now = C.now_ms()
                self.last_switch_at = now
                self.quiet = {name: now + 15_000 for name in result["touched"]}
                for rt in self.profiles:
                    if rt.name in result["touched"]:
                        rt.config_mtime = None  # read again at once
                self._profiles_sig = None  # the registry changed; a profile may be new
                a = self.state["accounts"].get(account_id)
                who = name_of(a) if a else result.get("email") or account_id[:8]
                if why:
                    # Why this account, too: "why not that one?" is the question an automatic switch raises.
                    picked = f"; {who}: {why['pick']}" if why.get("pick") else ""
                    text = f"Auto mode switched Claude Code to {who} — {why['because']}{picked} — with its login saved here, no sign-in"
                else:
                    text = f"Switched Claude Code to {who}, with its login saved here — no sign-in"
                if pv:
                    text += f" (from {name_of(prev)} at 5-hour {pct_text(pv['session'])}, weekly {pct_text(pv['weekly'])})"
                gone = name_of(prev) if prev else result.get("fromEmail") or "The previous account"
                if result.get("parkedIn"):
                    text += f'. {gone}\'s login is kept in profile "{result["parkedIn"]}"{" (new)" if result.get("createdProfile") else ""}, ready to switch back'
                elif result.get("dropped"):
                    text += f". {gone} stays signed in through its own profile"
                if not result.get("verified"):
                    text += ". Its login could not be checked with Anthropic just now; Claude Code renews it on first use"
                self.log_event("switch", text)
                if result.get("warning"):
                    self.log_event("switch", result["warning"], "warning")
                if result.get("reading") and a:
                    self.apply_live(account_id, result["reading"])
                self.save_state(True)
        self.tick()
        if self.poller is not None:
            self.poller.refresh(0)
        return result

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
