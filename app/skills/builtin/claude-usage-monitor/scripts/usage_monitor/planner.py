"""Which account should take over from the one in use, and when: the rules of automatic
switching, shared by auto mode, the alerts' suggestions, the dashboard and the CLI's ``plan``.

Pure functions over the monitor's account views (``Monitor.view_of``).

Why these rules: one full 5-hour window costs about a quarter of a week on a Max plan
(measured 2026-10-07), so the week is the scarce part and the 5-hour window the throttle.
Accounts are compared by how much work they can still carry — the room left in their 5-hour
window, capped by what their week allows — in coarse classes, and within a class by how soon
their unused week would be lost at its reset. In a simulation of three accounts this beat
both "most 5-hour room first" and "use up one week before the next": when the weeks reset
together it keeps several accounts alive to the end, and when they reset on different days
it spends the week that is about to expire first.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Callable

from . import usage as U

MIN = U.MIN
HOUR = U.HOUR
DAY = 24 * HOUR

# An account needs this much room (points of a 5-hour window, capped by its week) to be worth
# a switch. In points, not minutes: one big reply makes the measured pace spike, and a gate
# in minutes would then refuse every account just when a switch is due.
GATE_POINTS = 10
PLENTY, SOME = 60, 30  # room classes, in points of a 5-hour window
WEEKLY_STEP = 3  # weekly room per day is compared in steps of this many points
DEFAULT_PACE = 30.0  # % of a 5-hour window per hour, when the account in use isn't running
EARLY_WITHIN_MS = 24 * HOUR  # the early switch: an unused week this close to its reset
EARLY_MIN = 60  # ...in an account that can carry you this long
EARLY_FACTOR = 1.25  # ...and that has this much more room per day than the account in use


# ---------------------------------------------------------------- formatting


def _local(ms: float) -> datetime:
    return datetime.fromtimestamp(U.js_round(ms / MIN) * MIN / 1000)  # resets land on xx:59:59.9


def clock(ms: float) -> str:
    return _local(ms).strftime("%H:%M")


def day_clock(ms: float) -> str:
    return f"{_local(ms).strftime('%a')} {clock(ms)}"


def when_clock(ms: float, now: float) -> str:
    return day_clock(ms) if abs(ms - now) > 20 * HOUR else clock(ms)


def duration(minutes: float) -> str:
    if not math.isfinite(minutes):
        return "for hours"
    m = max(0, U.js_round(minutes))
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60:02d} min" if m < 600 else f"{U.js_round(m / 60)} h"


def _pct(w: dict | None) -> float:
    return float(w["pct"]) if w and w.get("pct") is not None else 0.0


def _p(w: dict | None) -> str:
    return f"{'≈' if w and w.get('estimated') else ''}{U.js_round(_pct(w))}%"


# ---------------------------------------------------------------- figures


def thresholds(s: dict) -> tuple[float, float]:
    """Auto mode's switch thresholds: (5-hour %, weekly %)."""
    return float(s.get("autoSessionPct", 98)), float(s.get("autoWeeklyPct", 99))


def usd_per_point(v: dict | None) -> tuple[float, float]:
    """List-price USD per 1% of the account's 5-hour window and of its week (its calibration)."""
    cal = (v or {}).get("calibration") or {}
    seed = U.calibration_seed(None)
    return (
        (cal.get("session") or {}).get("usdPerPct") or seed["session"]["usdPerPct"],
        (cal.get("weekly") or {}).get("usdPerPct") or seed["weekly"]["usdPerPct"],
    )


def pace(cur: dict | None) -> tuple[float, bool]:
    """List-price USD per hour the account in use spends now, and whether that was measured.
    USD, not percent: accounts on other plans fill at other rates for the same work."""
    usd5 = usd_per_point(cur)[0]
    rate = ((cur or {}).get("rate") or {}).get("session")
    if rate and rate.get("perHour", 0) >= U.IDLE_PER_HOUR:
        return rate["perHour"] * usd5, True
    return DEFAULT_PACE * usd5, False


def _minutes_to(pct: float, reset: float | None, length_ms: float, per_min: float, threshold: float, now: float) -> float:
    """Minutes until a window reaches ``threshold`` at ``per_min`` points a minute. A reset that
    comes first starts a fresh window (in use, it opens at once). inf: it never gets there."""
    if per_min <= 0:
        return math.inf
    if reset is not None and reset <= now:
        pct, reset = 0.0, None
    need = max(0.0, threshold - pct) / per_min
    if reset is None:  # no window running: it opens on first use
        return need if need * MIN < length_ms else math.inf
    left = (reset - now) / MIN
    if need <= left:
        return need
    fresh = threshold / per_min
    return left + (fresh if fresh * MIN < length_ms else math.inf)


def stretch(v: dict, usd_per_hour: float, now: float, t5: float, t7: float) -> float:
    """Minutes the account can carry this pace before reaching either switch threshold."""
    usd5, usd7 = usd_per_point(v)
    s, w = v.get("session") or {}, v.get("weekly") or {}
    m5 = _minutes_to(_pct(s), s.get("resetsAt"), U.SESSION_MS, usd_per_hour / usd5 / 60, t5, now)
    m7 = _minutes_to(_pct(w), w.get("resetsAt"), U.WEEK_MS, usd_per_hour / usd7 / 60, t7, now)
    return min(m5, m7)


def room(v: dict, usd_per_hour: float, now: float, t5: float, t7: float) -> float:
    """The work the account can still take, in points of its 5-hour window: the room left in
    the window (one that resets before it would fill counts as empty), capped by its week."""
    usd5, usd7 = usd_per_point(v)
    s, w = v.get("session") or {}, v.get("weekly") or {}
    reset5 = s.get("resetsAt")
    room5 = t5 if reset5 is None or reset5 <= now else max(0.0, t5 - _pct(s))
    per_min = usd_per_hour / usd5 / 60
    if reset5 is not None and reset5 > now and per_min > 0 and room5 / per_min * MIN > reset5 - now:
        room5 = t5
    reset7 = w.get("resetsAt")
    left7 = t7 if reset7 is not None and reset7 <= now else max(0.0, t7 - _pct(w))
    return min(room5, left7 * usd7 / usd5)


def weekly_per_day(v: dict, now: float, t7: float) -> float:
    """The week's room per day left until its reset: how much would be lost unless used."""
    w = v.get("weekly") or {}
    reset = w.get("resetsAt")
    if reset is None or reset <= now:
        return t7 / 7
    return max(0.0, t7 - _pct(w)) / max((reset - now) / DAY, 1 / 48)


def room_class(points: float) -> int:
    return 0 if points >= PLENTY else 1 if points >= SOME else 2


_CLASS_TEXT = ("plenty of room", "some room", "little room")


# ---------------------------------------------------------------- candidates


def assess(v: dict, *, now: float, s: dict, usd_per_hour: float, aside: dict, in_use: Callable[[dict], bool]) -> dict:
    """One account as a candidate to take over. ``usable``: it could carry on; ``ok``: auto mode
    may switch to it (usable, and its login is saved here); ``why``: one line for people."""
    t5, t7 = thresholds(s)
    session, weekly = v.get("session") or {}, v.get("weekly") or {}
    c: dict[str, Any] = {
        "id": v["id"],
        "account": v["displayName"],
        "email": v.get("email"),
        "switchable": bool(v.get("switchable")),
        "inRotation": v.get("rotation", True) is not False,
        "session": U.js_round(_pct(session)),
        "weekly": U.js_round(_pct(weekly)),
        "sessionResetsAt": session.get("resetsAt"),
        "weeklyResetsAt": weekly.get("resetsAt"),
        "lastActiveAt": v.get("lastActiveAt") or 0,
        "usable": False,
        "ok": False,
    }
    entry = aside.get(v["id"])
    if not v.get("hasData"):
        c["why"] = "no figures yet"
        return c
    if not c["inRotation"]:
        c["why"] = f"left out of switching (rotation off): 5-hour {_p(session)}, weekly {_p(weekly)}"
        return c
    if entry:
        c["why"] = f"weekly limit full: back {when_clock(entry['until'], now)}"
        c["setAsideUntil"] = entry["until"]
        return c
    if _pct(weekly) >= t7:
        c["why"] = f"weekly limit full ({_p(weekly)}), resets {when_clock(weekly['resetsAt'], now)}" if weekly.get("resetsAt") else f"weekly limit full ({_p(weekly)})"
        return c
    if _pct(session) >= t5:
        c["why"] = f"5-hour limit full ({_p(session)}), resets {when_clock(session['resetsAt'], now)}" if session.get("resetsAt") else f"5-hour limit full ({_p(session)})"
        return c
    c["stretchMin"] = st = stretch(v, usd_per_hour, now, t5, t7)
    c["room"] = pts = room(v, usd_per_hour, now, t5, t7)
    c["perDay"] = per_day = weekly_per_day(v, now, t7)
    if pts < GATE_POINTS:
        c["why"] = f"too little room left: 5-hour {_p(session)}, weekly {_p(weekly)}"
        return c
    week = (
        f"{U.js_round(max(0, t7 - _pct(weekly)))}% of its week left until {when_clock(weekly['resetsAt'], now)} (≈{U.js_round(per_day)}%/day)"
        if weekly.get("resetsAt") and weekly["resetsAt"] > now
        else "a fresh week"
    )
    c["why"] = f"5-hour {_p(session)}, weekly {_p(weekly)}: {_CLASS_TEXT[room_class(pts)]}, {week}; carries you ~{duration(st)}"
    c["usable"] = True
    if v.get("active"):
        c["why"] = "your Claude Code uses it already; " + c["why"]
    elif not c["switchable"]:
        c["why"] = "its login isn't saved here (sign it in once with add-account); " + c["why"]
    elif in_use(v):
        c["why"] = "in use in its own Claude Code window right now; " + c["why"]
    else:
        c["ok"] = True
    return c


def _rank(c: dict) -> tuple:
    reset5 = c.get("sessionResetsAt")
    return (
        not c["ok"],  # one click (or none) away first
        room_class(c["room"]),
        -math.floor(c["perDay"] / WEEKLY_STEP),
        -U.js_round(c["room"]),
        reset5 if reset5 else math.inf,  # a window already running before a fresh one
        c["lastActiveAt"],
    )


def trigger(cur: dict, s: dict) -> dict | None:
    """Whether the account in use has reached a switch threshold: ``{reason, kind, pct, resetsAt}``."""
    t5, t7 = thresholds(s)
    w, se = cur.get("weekly") or {}, cur.get("session") or {}
    if _pct(w) >= t7:
        return {"reason": "weekly", "kind": "weekly", "pct": _pct(w), "resetsAt": w.get("resetsAt"), "estimated": bool(w.get("estimated"))}
    if _pct(se) >= t5:
        reason = "limit" if _pct(se) >= 100 else "session"
        return {"reason": reason, "kind": "session", "pct": _pct(se), "resetsAt": se.get("resetsAt"), "estimated": bool(se.get("estimated"))}
    return None


def _free_at(views: list[dict], cur_id: str | None, s: dict, aside: dict, now: float) -> tuple[str | None, float | None]:
    """The first account here to free up again, and when — for "nothing can take over"."""
    t5, t7 = thresholds(s)
    best: tuple[str | None, float | None] = (None, None)
    for v in views:
        if v["id"] == cur_id or not v.get("hasData") or v.get("rotation", True) is False:
            continue
        until = 0.0
        if v["id"] in aside:
            until = aside[v["id"]]["until"]
        w, se = v.get("weekly") or {}, v.get("session") or {}
        if _pct(w) >= t7 and w.get("resetsAt"):
            until = max(until, w["resetsAt"])
        if _pct(se) >= t5 and se.get("resetsAt"):
            until = max(until, se["resetsAt"])
        if until > now and (best[1] is None or until < best[1]):
            best = (v["id"], until)
    return best


def plan(views: list[dict], s: dict, now: float, *, current_id: str | None, aside: dict | None = None, in_use: Callable[[dict], bool] | None = None) -> dict:
    """What should happen now: ``action`` (switch | wait | stay | none), the account in use,
    and every other account ranked best first with the reason."""
    aside = aside or {}
    in_use = in_use or (lambda _v: False)
    t5, t7 = thresholds(s)
    cur = next((v for v in views if v["id"] == current_id), None) if current_id else None
    usd_h, measured = pace(cur)
    ranked = [assess(v, now=now, s=s, usd_per_hour=usd_h, aside=aside, in_use=in_use) for v in views if v["id"] != current_id]
    usable = sorted((c for c in ranked if c["usable"]), key=_rank)
    others = [c for c in ranked if not c["usable"]]
    ok = [c for c in usable if c["ok"]]
    out: dict[str, Any] = {
        "at": now,
        "thresholds": {"session": t5, "weekly": t7},
        "pace": {"usdPerHour": round(usd_h, 2), "measured": measured, "pctPerHour": round(usd_h / usd_per_point(cur)[0], 1)},
        "current": None,
        "next": usable[0] if usable else None,
        "candidates": usable + others,
    }
    if not ok:
        out["freeId"], out["freeAt"] = _free_at(views, current_id, s, aside, now)
    if cur is None:
        out["action"] = {"do": "none", "text": "Your Claude Code isn't signed in to an account this monitor knows."}
        return out
    cur_stretch = stretch(cur, usd_h, now, t5, t7)
    out["current"] = {
        "id": cur["id"],
        "account": cur["displayName"],
        "session": U.js_round(_pct(cur.get("session"))),
        "weekly": U.js_round(_pct(cur.get("weekly"))),
        "perDay": weekly_per_day(cur, now, t7),
        "switchAt": now + cur_stretch * MIN if math.isfinite(cur_stretch) else None,
    }
    trig = trigger(cur, s)
    name = cur["displayName"]
    if trig:
        what = "weekly" if trig["kind"] == "weekly" else "5-hour"
        hit = f"{name} {'hit' if trig['reason'] == 'limit' else 'reached'} {'≈' if trig['estimated'] else ''}{U.js_round(trig['pct'])}% of its {what} limit"
        base = {"reason": trig["reason"], "kind": trig["kind"], "pct": trig["pct"], "resetsAt": trig["resetsAt"], "because": hit}
        if ok:
            out["action"] = {"do": "switch", "to": ok[0]["id"], **base, "text": f"{hit}: switch to {ok[0]['account']}."}
        else:
            first = next((v for v in views if v["id"] == out["freeId"]), None)
            later = f" The first account free again: {first['displayName']} at {when_clock(out['freeAt'], now)}." if first and out["freeAt"] else ""
            if usable:
                later += f" {usable[0]['account']} could take over: {usable[0]['why']}."
            out["action"] = {"do": "wait", **base, "text": f"{hit}, and no account can be switched to automatically.{later}"}
        return out
    early = _early(cur, ok, now, t7) if s.get("autoEarly") else None
    if early:
        out["action"] = {
            "do": "switch",
            "to": early["id"],
            "reason": "early",
            "kind": "weekly",
            "because": f"{early['account']}'s week resets {when_clock(early['weeklyResetsAt'], now)} with {U.js_round(max(0, t7 - early['weekly']))}% left",
            "text": f"{early['account']}'s week resets {when_clock(early['weeklyResetsAt'], now)} with room left: use it first.",
        }
        return out
    if out["current"]["switchAt"] and cur_stretch < 5 * 60:
        text = f"{name} reaches a switch threshold at ~{when_clock(out['current']['switchAt'], now)} at this pace."
    else:
        text = f"{name} has room for now."
    out["action"] = {"do": "stay", "text": text}
    return out


def _early(cur: dict, ok: list[dict], now: float, t7: float) -> dict | None:
    """The early switch: an account whose unused week would otherwise be lost at a reset soon."""
    mine = weekly_per_day(cur, now, t7)
    best = None
    for c in ok:
        reset = c.get("weeklyResetsAt")
        if not reset or reset <= now or reset - now > EARLY_WITHIN_MS or c["stretchMin"] < EARLY_MIN or c["perDay"] < mine * EARLY_FACTOR:
            continue
        if best is None or c["perDay"] > best["perDay"]:
            best = c
    return best


def hours_left(views: list[dict], s: dict, now: float, usd_per_hour: float) -> float | None:
    """Hours of work at this pace still in the weeks of the accounts you can use here (your
    Claude Code's and those whose login is saved here), before any of them resets."""
    _, t7 = thresholds(s)
    if usd_per_hour <= 0:
        return None
    usd = 0.0
    for v in views:
        if not v.get("hasData") or v.get("rotation", True) is False or not (v.get("active") or v.get("switchable")):
            continue
        w = v.get("weekly") or {}
        reset = w.get("resetsAt")
        left = t7 if reset is not None and reset <= now else max(0.0, t7 - _pct(w))
        usd += left * usd_per_point(v)[1]
    return usd / usd_per_hour


def recommendation(p: dict) -> dict | None:
    """The plan in the shape the alerts and the status line take: ``{id}`` or ``{id: None, freeId, freeAt}``."""
    nxt = p.get("next")
    if nxt:
        return {"id": nxt["id"], "why": nxt["why"], "switchable": nxt["switchable"]}
    if p.get("freeAt"):
        return {"id": None, "freeId": p["freeId"], "freeAt": p["freeAt"]}
    return None
