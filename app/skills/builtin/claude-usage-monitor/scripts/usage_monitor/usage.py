"""Usage-limit maths shared by the monitor and the status line bridge.

A "window" is ``{"pct": 0-100+, "resetsAt": epoch ms | None}``.
A usage snapshot is ``{"at", "source", "session", "weekly", "scoped", "spend"}``.

Every time is epoch milliseconds and every record a plain dict with camelCase
keys: the dashboard reads these objects verbatim from ``/api/state``, and
``state.json`` / ``ledger.json`` keep the standalone app's format, so its data
can be imported as is.
"""

from __future__ import annotations

import bisect
import math
import re
from datetime import datetime
from typing import Any, Iterable

MIN = 60_000
HOUR = 60 * MIN
SESSION_MS = 5 * HOUR
WEEK_MS = 7 * 24 * HOUR

# Either limit interrupts a session, so each has a heads-up and a switch-now threshold. The
# weekly ones sit higher: 1% of the weekly limit is ~5x the usage of 1% of the 5-hour one.
DEFAULT_SETTINGS: dict[str, Any] = {
    "warnPct": 80,  # 5-hour: heads-up
    "critPct": 90,  # 5-hour: switch now
    "weeklyWarnPct": 90,  # weekly: heads-up
    "weeklyCritPct": 95,  # weekly: switch now
    "etaAlertMin": 15,  # alert when either limit is projected this close
    "events": True,  # send alerts to Cremind events
    "sound": False,  # beep in the dashboard tab
    "live": True,  # ask Anthropic for the official figures (each profile's Claude login)
    # Switching: "manual" (alerts; you or the agent switch) or "auto" (the monitor switches
    # your Claude Code itself when the account in use reaches either threshold).
    "switchMode": "manual",
    "autoSessionPct": 98,
    "autoWeeklyPct": 99,  # ...and the account is set aside until its week resets
    "autoEarly": False,  # also switch early to spend a week that would otherwise expire unused
}

IDLE_PER_HOUR = 1.2  # % of the 5-hour window per hour; slower than this counts as idle


# ---------------------------------------------------------------- JavaScript semantics
#
# The maths was written for JavaScript, and these three helpers keep its results
# identical: Math.round rounds halves up (Python rounds them to even), objects and
# empty containers are truthy, and Number('') is 0.


def js_round(x: float) -> int:
    return math.floor(x + 0.5)


def truthy(v: Any) -> bool:
    if v is None or v is False:
        return False
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v != 0 and not math.isnan(v)
    if isinstance(v, str):
        return v != ""
    return True


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def to_ms(v: Any) -> float | None:
    """Epoch ms from epoch seconds, epoch ms or an ISO 8601 string."""
    if v is None or v == "":
        return None
    if _is_number(v):
        if not math.isfinite(v):
            return None
        return v * 1000 if v < 1e12 else v
    if isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt.timestamp() * 1000  # a naive time is local, as Date.parse reads it
    return None


def to_num(v: Any) -> float | None:
    if isinstance(v, str):
        text = v.strip()
        if not text:
            return 0.0
        try:
            v = float(text)
        except ValueError:
            return None
    return v if _is_number(v) and math.isfinite(v) else None


def window_of(pct: Any, resets_at: Any) -> dict | None:
    p = to_num(pct)
    return None if p is None else {"pct": max(0, p), "resetsAt": to_ms(resets_at)}


def pct_of(w: dict | None) -> float:
    return w["pct"] if w else 0


def _get(o: Any, key: str) -> Any:
    return o.get(key) if isinstance(o, dict) else None


# ---------------------------------------------------------------- parsing


def from_statusline(rl: Any, at: float) -> dict | None:
    """From the ``rate_limits`` object Claude Code passes to status line commands."""
    if not isinstance(rl, dict):
        return None

    def w(o: Any) -> dict | None:
        return window_of(o.get("used_percentage"), o.get("resets_at")) if isinstance(o, dict) else None

    u: dict[str, Any] = {
        "at": at,
        "source": "statusline",
        "session": w(rl.get("five_hour")),
        "weekly": w(rl.get("seven_day")),
        "scoped": None,
        "spend": None,
    }
    s = rl.get("spend_limit")
    spend_win = w(s) if truthy(s) else None
    if spend_win:
        u["spend"] = {
            **spend_win,
            "usedUsd": to_num(_get(s, "used_usd")),
            "limitUsd": to_num(_get(s, "limit_usd")),
            "period": _get(s, "period") or None,
        }
    return u if (u["session"] or u["weekly"] or u["spend"]) else None


def from_claude_cache(c: Any) -> dict | None:
    """From Claude Code's own cache (``cachedUsageUtilization`` in ``.claude.json``)."""
    if not isinstance(c, dict) or not truthy(c.get("utilization")) or not truthy(to_num(c.get("fetchedAtMs"))):
        return None
    x = c["utilization"] if isinstance(c["utilization"], dict) else {}

    def w(o: Any) -> dict | None:
        return window_of(o.get("utilization"), o.get("resets_at")) if isinstance(o, dict) else None

    u: dict[str, Any] = {
        "at": to_num(c.get("fetchedAtMs")),
        "source": "claude-cache",
        "session": w(x.get("five_hour")),
        "weekly": w(x.get("seven_day")),
        "scoped": [],
        "spend": None,
    }
    limits = x.get("limits")
    for item in limits if isinstance(limits, list) else []:
        win = window_of(item.get("percent"), item.get("resets_at")) if isinstance(item, dict) else None
        if not win:
            continue
        kind = item.get("kind")
        if kind == "session":
            u["session"] = u["session"] or win
        elif kind == "weekly_all":
            u["weekly"] = u["weekly"] or win
        else:
            scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
            model = scope.get("model") if isinstance(scope.get("model"), dict) else None
            surface = scope.get("surface") if isinstance(scope.get("surface"), dict) else None
            name = (model and model.get("display_name")) or (surface and (surface.get("display_name") or surface.get("name")))
            group = "Weekly" if item.get("group") == "weekly" else str(kind or "Limit")
            u["scoped"].append({"label": f"{group} · {name or kind or 'limit'}", **win})
    # Older response shape
    for key, label in (("seven_day_opus", "Weekly · Opus"), ("seven_day_sonnet", "Weekly · Sonnet")):
        win = w(x.get(key))
        if win and not any(s["label"] == label for s in u["scoped"]):
            u["scoped"].append({"label": label, **win})
    return u if (u["session"] or u["weekly"]) else None


def from_usage_api(body: Any, at: float) -> dict | None:
    """From Anthropic's usage API — what Claude Code's ``/usage`` and claude.ai show. The same
    shape Claude Code caches; a limit the answer names as null has no usage in progress."""
    if not isinstance(body, dict):
        return None
    limits = body.get("limits") if isinstance(body.get("limits"), list) else []
    named = {item.get("kind") for item in limits if isinstance(item, dict)}
    filled = dict(body)
    for key, kind in (("five_hour", "session"), ("seven_day", "weekly_all")):
        if key in body and body[key] is None and kind not in named:
            filled[key] = {"utilization": 0, "resets_at": None}
    u = from_claude_cache({"utilization": filled, "fetchedAtMs": at})
    if not u:
        return None
    u["source"] = "api"
    u["spend"] = _extra_usage(body)
    return u


def _extra_usage(body: dict) -> dict | None:
    """Extra usage (paid past the plan's limits) in the status line's ``spend`` shape, when it
    is turned on and counted in US dollars."""
    x = body.get("extra_usage")
    if not isinstance(x, dict) or x.get("is_enabled") is not True:
        return None
    s = body.get("spend") if isinstance(body.get("spend"), dict) else {}

    def money(o: Any) -> float | None:
        minor = to_num(_get(o, "amount_minor"))
        exp = to_num(_get(o, "exponent"))
        return None if minor is None or exp is None else minor / 10**exp

    used, limit = money(s.get("used")), money(s.get("limit"))
    if used is None or limit is None:
        places = to_num(x.get("decimal_places"))
        scale = 10 ** (2 if places is None else places)
        raw_used, raw_limit = to_num(x.get("used_credits")), to_num(x.get("monthly_limit"))
        used = None if raw_used is None else raw_used / scale
        limit = None if raw_limit is None else raw_limit / scale
    if (_get(s.get("used"), "currency") or x.get("currency") or "USD") != "USD":
        return None
    pct = to_num(x.get("utilization"))
    pct = to_num(s.get("percent")) if pct is None else pct
    return {"pct": max(0, pct or 0), "resetsAt": None, "usedUsd": used, "limitUsd": limit, "period": "monthly"}


def project(win: dict | None, now: float) -> dict | None:
    """The window as of ``now``: once its reset time has passed, usage is back to 0."""
    if not win:
        return None
    if win.get("resetsAt") is not None and now >= win["resetsAt"]:
        return {"pct": 0, "resetsAt": None, "rolledOver": True, "endedAt": win["resetsAt"], "lastPct": win["pct"]}
    return {"pct": win["pct"], "resetsAt": win.get("resetsAt"), "rolledOver": False}


# ---------------------------------------------------------------- rates & forecasts


def burn_rate(samples: Iterable[list] | None, session: dict | None, now: float) -> dict | None:
    """Burn rate of the current 5-hour window in %/hour.

    samples: ``[[t, sessionPct, sessionResetsAt, weeklyPct], ...]`` oldest first.
    Prefers the slope of the last 20 minutes; falls back to the whole-window average.
    """
    if not session or session.get("resetsAt") is None:
        return None
    reset = session["resetsAt"]
    in_window = [s for s in samples or [] if s[1] is not None and s[2] is not None and abs(s[2] - reset) < 2 * MIN]

    recent = [s for s in in_window if now - s[0] <= 20 * MIN]
    if len(recent) >= 3 and recent[-1][0] - recent[0][0] >= 8 * MIN:
        n = len(recent)
        mt = sum(s[0] for s in recent) / n
        mp = sum(s[1] for s in recent) / n
        num = sum((s[0] - mt) * (s[1] - mp) for s in recent)
        den = sum((s[0] - mt) ** 2 for s in recent)
        if den > 0:
            return {"perHour": max(0, (num / den) * HOUR), "basis": "last 20 min"}

    elapsed = min(now, reset) - (reset - SESSION_MS)
    if elapsed >= 15 * MIN and session["pct"] > 0:
        return {"perHour": session["pct"] / (elapsed / HOUR), "basis": "window average"}
    return None


def forecast(win: dict | None, rate: dict | None, now: float, crit_pct: float, idle: bool | None = None) -> dict | None:
    """When a window's limit is hit at the current pace, and when it crosses ``crit_pct``.

    ``idle`` defaults to the 5-hour idle pace; pass it explicitly for the weekly window.
    """
    if idle is None:
        idle = not rate or rate["perHour"] < IDLE_PER_HOUR
    if not win or not rate or win.get("resetsAt") is None:
        return None
    if idle:
        return {"idle": True, "resetsFirst": True, "resetsAt": win["resetsAt"]}
    per_min = rate["perHour"] / 60
    # JavaScript divides by zero into Infinity; the JSON encoder sends that as null.
    if win["pct"] >= 100:
        limit_at = now
    else:
        limit_at = now + ((100 - win["pct"]) / per_min) * MIN if per_min > 0 else math.inf
    if win["pct"] >= crit_pct:
        switch_at = now
    else:
        switch_at = now + ((crit_pct - win["pct"]) / per_min) * MIN if per_min > 0 else math.inf
    return {"idle": False, "limitAt": limit_at, "switchAt": switch_at, "resetsFirst": limit_at >= win["resetsAt"], "resetsAt": win["resetsAt"]}


def forecast_limits(session: dict | None, weekly: dict | None, rate: dict | None, now: float, s: dict) -> dict | None:
    """Forecasts for both limits, and ``first``: the one that interrupts the account first.

    A 5-hour reset doesn't pause the weekly count, so the weekly limit can come first.
    ``rate`` is ``{"session": {"perHour"}, "weekly": {"perHour"}}`` — % of each limit per hour.
    """
    if not rate or not rate.get("session"):
        return None
    idle = rate["session"]["perHour"] < IDLE_PER_HOUR  # the same spend drives both
    out: dict[str, Any] = {
        "idle": idle,
        "session": forecast(session, rate["session"], now, s["critPct"], idle),
        "weekly": forecast(weekly, rate["weekly"], now, s["weeklyCritPct"], idle) if rate.get("weekly") else None,
        "first": None,
    }
    for kind in ("session", "weekly"):
        f = out[kind]
        if f and not f["idle"] and not f["resetsFirst"] and (not out["first"] or f["limitAt"] < out["first"]["limitAt"]):
            out["first"] = {"kind": kind, **f}
    return out


def usable(v: dict, s: dict) -> bool:
    """Whether an account can take over now: both limits below their switch-now thresholds."""
    return bool(v.get("hasData")) and pct_of(v.get("session")) < s["critPct"] and pct_of(v.get("weekly")) < s["weeklyCritPct"]


def blocked_until(v: dict, s: dict) -> float:
    """When a used-up account can take over again: the latest reset among the limits blocking it."""
    weekly, session = v.get("weekly"), v.get("session")
    return max(
        weekly["resetsAt"] if pct_of(weekly) >= s["weeklyCritPct"] and truthy(weekly and weekly.get("resetsAt")) else 0,
        session["resetsAt"] if pct_of(session) >= s["critPct"] and truthy(session and session.get("resetsAt")) else 0,
    )


def status_of(v: dict, s: dict) -> dict:
    """Status of an account view. level: good | warning | serious | critical | none
    (always rendered with an icon + label, never color alone)."""
    sp = v["session"]["pct"] if v.get("session") else None
    wp = v["weekly"]["pct"] if v.get("weekly") else None
    if sp is None and wp is None:
        return {"level": "none", "icon": "…", "label": "No data yet"}
    if wp is not None and wp >= 100:
        return {"level": "critical", "icon": "⛔", "label": "Weekly limit reached", "until": v["weekly"].get("resetsAt")}
    if sp is not None and sp >= 100:
        return {"level": "critical", "icon": "⛔", "label": "5-hour limit reached", "until": v["session"].get("resetsAt")}
    s_crit = sp is not None and sp >= s["critPct"]
    w_crit = wp is not None and wp >= s["weeklyCritPct"]
    s_warn = sp is not None and sp >= s["warnPct"]
    w_warn = wp is not None and wp >= s["weeklyWarnPct"]
    if v.get("active"):
        if s.get("switchMode") == "auto" and (s_warn or w_warn or s_crit or w_crit):
            # Automatic switching moves it at its own thresholds: nothing to do but know.
            near_s, near_w = s_warn or s_crit, w_warn or w_crit
            label = "Near both limits" if near_s and near_w else "Near 5-hour limit" if near_s else "Near weekly limit"
            return {"level": "warning", "icon": "▲", "label": label}
        if s_crit or w_crit:
            label = "Switch now" if s_crit and w_crit else "Switch now (5-hour)" if s_crit else "Switch now (weekly)"
            return {"level": "critical", "icon": "⚠", "label": label}
        if s_warn or w_warn:
            label = "Near both limits" if s_warn and w_warn else "Near 5-hour limit" if s_warn else "Near weekly limit"
            return {"level": "warning", "icon": "▲", "label": label}
        return {"level": "good", "icon": "●", "label": "In use"}
    if w_crit:
        return {"level": "serious", "icon": "◔", "label": "Weekly almost used up", "until": v["weekly"].get("resetsAt")}
    if s_crit:
        return {"level": "serious", "icon": "◔", "label": "Almost used up", "until": v["session"].get("resetsAt")}
    if w_warn:
        return {"level": "warning", "icon": "▲", "label": "Weekly running low", "until": v["weekly"].get("resetsAt")}
    return {"level": "good", "icon": "✓", "label": "Ready"}


def recommend(views: list[dict], s: dict) -> dict | None:
    """The best account to switch to: below both switch-now thresholds, most 5-hour headroom,
    avoiding accounts past the weekly heads-up; least recently used breaks ties."""
    candidates = [v for v in views if not v.get("active") and usable(v, s)]
    candidates.sort(
        key=lambda v: (
            pct_of(v.get("weekly")) >= s["weeklyWarnPct"],
            pct_of(v.get("session")),
            pct_of(v.get("weekly")),
            v.get("lastActiveAt") or 0,
        )
    )
    if candidates:
        return {"id": candidates[0]["id"]}

    # Nothing usable right now: when does the first account free up?
    soonest = None
    for v in views:
        if v.get("active") or not v.get("hasData"):
            continue
        until = blocked_until(v, s)
        if until and (not soonest or until < soonest["at"]):
            soonest = {"id": v["id"], "at": until}
    return {"id": None, "freeId": soonest["id"], "freeAt": soonest["at"]} if soonest else None


# ---------------------------------------------------------------- estimates from spend
#
# Official readings come from Anthropic's usage API every minute or so while live figures
# work, and otherwise only now and then (a status line reply, Claude Code refreshing its
# cache, a limit being hit). In between, the monitor rolls the last reading forward with
# what the account's sessions have spent since, priced at API list rates and converted
# with a per-plan calibration: list-price USD per 1% of each window.


class SpendLog:
    """List-price USD spent per minute by one account."""

    def __init__(self, entries: Iterable[Any] | None = None) -> None:
        self.m: list[int] = []  # minute start, ascending
        self.v: list[float] = []  # USD in that minute
        for e in entries or []:
            if isinstance(e, (list, tuple)) and len(e) >= 2 and _is_number(e[0]) and _is_number(e[1]) and e[1] > 0:
                self.add(e[0], e[1])

    def bisect(self, t: float) -> int:
        return bisect.bisect_left(self.m, t)

    def add(self, t: float, usd: float) -> None:
        minute = int(math.floor(t / MIN) * MIN)
        i = self.bisect(minute)
        if i < len(self.m) and self.m[i] == minute:
            self.v[i] += usd
        else:
            self.m.insert(i, minute)
            self.v.insert(i, usd)

    def between(self, a: float, b: float) -> float:
        """USD spent in [a, b). A minute counts from ``a`` when its midpoint is at or after ``a``,
        and up to ``b`` once it has begun — so ``between(x, now)`` always includes the minute
        in progress."""
        total = 0.0
        i = self.bisect(a - MIN / 2)
        while i < len(self.m) and self.m[i] < b:
            total += self.v[i]
            i += 1
        return total

    def first_after(self, t: float) -> int | None:
        """Start of the first minute with spend at or after t, or None."""
        i = self.bisect(t - MIN / 2)
        return self.m[i] if i < len(self.m) else None

    def last(self) -> int | None:
        return self.m[-1] if self.m else None

    def prune(self, before: float) -> None:
        i = self.bisect(before)
        if i:
            del self.m[:i]
            del self.v[:i]

    def to_json(self) -> list[list[float]]:
        return [[m, js_round(self.v[i] * 1e4) / 1e4] for i, m in enumerate(self.m)]


EMPTY_LOG = SpendLog()


def calibration_seed(tier: Any) -> dict:
    """List-price USD per 1% of each window, by plan. Measured on Max 20x (2026-10-06):
    5-hour windows of $590 and $550; the weekly figure is rougher (one 5-point move).
    Other plans scale by their advertised multiple of Pro; calibration takes over from there."""
    t = str(tier or "")
    if re.search(r"max_20x", t, re.I):
        scale = 1.0
    elif re.search(r"max_5x", t, re.I):
        scale = 0.25
    elif re.search(r"pro", t, re.I):
        scale = 0.05
    else:
        scale = 1.0
    return {"session": {"usdPerPct": 5.7 * scale, "n": 0}, "weekly": {"usdPerPct": 31 * scale, "n": 0}}


def blend_calibration(cal: dict, sample: float) -> dict | None:
    """Blend a measured sample into a calibration; implausible samples (mis-attributed spend) are dropped."""
    if not (sample > 0) or not (cal["usdPerPct"] > 0):
        return None
    lo, hi = (0.25, 4) if cal["n"] < 2 else (0.4, 2.5)
    if sample < cal["usdPerPct"] * lo or sample > cal["usdPerPct"] * hi:
        return None
    w = 0.6 if cal["n"] == 0 else 0.3
    return {"usdPerPct": cal["usdPerPct"] * (1 - w) + sample * w, "n": cal["n"] + 1}


def estimate_window(reading: dict | None, log: SpendLog | None, usd_per_pct: float, kind: str, now: float, exact_ms: float | None = None) -> dict | None:
    """A usage window as of ``now``: its last official reading plus the spend since, rolled
    through resets. A 5-hour window opens at the first use after the previous one ends
    (aligned to 10 minutes); a weekly one likewise, aligned to the hour.

    reading   ``{"pct", "resetsAt", "at"}`` — official figure, or None when never seen
    kind      ``"session"`` | ``"weekly"``
    exact_ms  a reading younger than this is shown as it is — Anthropic's own figure, the
              one claude.ai shows — rather than rolled forward with estimated spend
    """
    if not reading or reading.get("pct") is None:
        return None
    length = WEEK_MS if kind == "weekly" else SESSION_MS
    align = HOUR if kind == "weekly" else 10 * MIN
    lg = log if log is not None else EMPTY_LOG

    def spent(a: float, b: float) -> float:
        return lg.between(a, b) / usd_per_pct if usd_per_pct > 0 and b > a else 0

    def result(pct: float, resets_at: float | None, extra: dict) -> dict:
        return {
            "pct": min(100, max(0, pct)),
            "resetsAt": resets_at,
            "official": reading["pct"],
            "officialAt": reading.get("at"),
            "rolledOver": False,
            "approxReset": False,
            **extra,
        }

    base = reading["pct"]
    base_at = reading.get("at")
    resets_at = reading.get("resetsAt")
    approx_reset = False
    rolled = None

    if exact_ms is not None and base_at is not None and now - base_at <= exact_ms and (resets_at is None or now < resets_at):
        return result(base, resets_at, {"estimated": False, **({"windowStart": resets_at - length} if resets_at is not None else {})})

    # Spend can't take a used-up window past 100%, so an official 100% stays official (a reply
    # sharing its minute with the limit notice would otherwise mark it as an estimate).
    def moved(added: float) -> bool:
        return added >= 0.5 and base < 100

    if resets_at is None:
        first = lg.first_after(base_at) if base <= 0 else None
        if first is None or first > now:
            added = spent(base_at, now)
            return result(base + added, None, {"estimated": moved(added)})
        base_at = math.floor(first / align) * align
        resets_at = base_at + length
        base = 0
        approx_reset = True

    guard = 0
    while now >= resets_at and guard < 500:
        guard += 1
        rolled = {"endedAt": resets_at, "lastPct": min(100, base + spent(base_at, resets_at))}
        first = lg.first_after(resets_at)
        if first is None or first > now:
            return result(0, None, {"estimated": True, "rolledOver": True, "endedAt": rolled["endedAt"], "lastPct": rolled["lastPct"]})
        base_at = max(resets_at, math.floor(first / align) * align)
        resets_at = base_at + length
        base = 0
        approx_reset = True

    added = spent(base_at, now)
    extra: dict[str, Any] = {"estimated": approx_reset or moved(added), "approxReset": approx_reset, "windowStart": resets_at - length}
    if rolled:
        extra.update({"rolledOver": True, "endedAt": rolled["endedAt"], "lastPct": rolled["lastPct"]})
    return result(base + added, resets_at, extra)


def spend_rate(log: SpendLog | None, usd_per_pct: float, now: float, active_since: float | None) -> dict | None:
    """Burn rate in % of a window per hour (the window's own USD per %), from recent spend."""
    if log is None or not (usd_per_pct > 0):
        return None
    span = max(5 * MIN, min(30 * MIN, now - (active_since or 0)))
    usd = log.between(now - span, now)
    return {"perHour": usd / usd_per_pct / (span / HOUR), "basis": f"last {js_round(span / MIN)} min"}


def window_series(win: dict | None, reading: dict | None, log: SpendLog | None, usd_per_pct: float, now: float, step: float = 2 * MIN) -> list[list[float]]:
    """Points for the window chart: ``[t, pct]`` every ``step`` from the window start to now,
    passing through the official reading when one falls inside the window."""
    if not win or win.get("resetsAt") is None or not (usd_per_pct > 0):
        return []
    start = win["resetsAt"] - SESSION_MS
    lg = log if log is not None else EMPTY_LOG
    anchor_inside = bool(
        reading
        and reading.get("at") is not None
        and start <= reading["at"] <= now
        and reading.get("resetsAt") is not None
        and abs(reading["resetsAt"] - win["resetsAt"]) < 15 * MIN
    )
    anchor_at = reading["at"] if anchor_inside else start
    anchor_pct = reading["pct"] if anchor_inside else 0

    # Counted out from the anchor exactly as estimate_window counts, so the line ends on the
    # headline figure even when a reply shares its minute with the official reading.
    def at(t: float) -> float:
        usd = lg.between(anchor_at, t) if t >= anchor_at else -lg.between(t, anchor_at)
        return min(100, max(0, anchor_pct + usd / usd_per_pct))

    out: list[list[float]] = []
    first = lg.first_after(start)
    begin = min(anchor_at, now if first is None else first)
    t = max(start, math.floor(begin / step) * step)
    while t < now:
        out.append([t, at(t)])
        t += step
    out.append([now, at(now)])
    return out


def official_series(marks: Iterable[list], start: float, now: float, pct_now: float, step: float = 2 * MIN) -> list[list[float]]:
    """Points for the window chart from official readings alone (``marks``: ``[t, pct]``):
    straight lines between them, from 0% when the window opened to the figure now."""
    pts = [(start, 0.0), *sorted((m[0], m[1]) for m in marks if start <= m[0] <= now), (now, pct_now)]
    j = 0

    def at(t: float) -> float:
        nonlocal j
        while j + 1 < len(pts) and pts[j + 1][0] <= t:
            j += 1
        if j + 1 >= len(pts):
            return pts[-1][1]
        (t0, p0), (t1, p1) = pts[j], pts[j + 1]
        return p0 + (p1 - p0) * (t - t0) / (t1 - t0)

    out: list[list[float]] = []
    t = start
    while t < now:
        out.append([t, min(100, max(0, at(t)))])
        t += step
    out.append([now, min(100, max(0, pct_now))])
    return out
