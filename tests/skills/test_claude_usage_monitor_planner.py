"""Automatic switching's rules (claude-usage-monitor ``planner.py``): when the account your
Claude Code uses should hand over, and which account takes over."""

from __future__ import annotations

import sys
from datetime import datetime

import pytest

from app.skills.sync import BUILTIN_SKILLS_DIR

SCRIPTS_DIR = BUILTIN_SKILLS_DIR / "claude-usage-monitor" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from usage_monitor import planner as PL  # noqa: E402
from usage_monitor import usage as U  # noqa: E402

MIN = 60_000
HOUR = 60 * MIN
S = U.DEFAULT_SETTINGS
CAL = {"session": {"usdPerPct": 6.21, "n": 3}, "weekly": {"usdPerPct": 25.46, "n": 2}}  # Max 20x, measured 2026-10-07


def at(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp() * 1000


NOW = at("2026-10-07 11:23")


def view(id_: str, session: float, weekly: float, *, s_reset: str | None = None, w_reset: str | None = None, active: bool = False, switchable: bool = True, rate: float | None = None, **extra) -> dict:
    return {
        "id": id_,
        "displayName": f"{id_}@",
        "email": f"{id_}@example.com",
        "hasData": True,
        "active": active,
        "switchable": switchable and not active,
        "signedIn": [],
        "session": {"pct": session, "resetsAt": at(s_reset) if s_reset else None},
        "weekly": {"pct": weekly, "resetsAt": at(w_reset) if w_reset else None},
        "calibration": CAL,
        "rate": {"session": {"perHour": rate}} if rate else None,
        "lastActiveAt": 0,
        **extra,
    }


def by_id(p: dict) -> dict[str, dict]:
    return {c["id"]: c for c in p["candidates"]}


def test_a_week_it_would_use_up_hours_before_its_reset_is_kept_for_later() -> None:
    """Real figures of 2026-10-07 11:23: the account in use reached 98% of its 5-hour window.
    One account's 16% of a week ends tomorrow 06:00, another's 51% lasts until Saturday; both
    have an empty window. The first would be out of its week from mid-afternoon until 06:00 —
    no cover when another account's 5-hour window fills — so the second goes first."""
    views = [
        view("cur", 98, 97, s_reset="2026-10-07 13:30", w_reset="2026-10-12 08:00", active=True, rate=23),
        view("late", 0, 48, w_reset="2026-10-10 18:59"),
        view("soon", 0, 83, s_reset="2026-10-07 15:00", w_reset="2026-10-08 06:00"),
    ]
    p = PL.plan(views, S, NOW, current_id="cur")
    assert p["action"]["do"] == "switch" and p["action"]["to"] == "late" and p["action"]["reason"] == "session"
    c = by_id(p)
    soon = c["soon"]
    assert PL.room_class(soon["room"]) == 0, "16% of a week is still ~66 points of a 5-hour window: plenty"
    assert soon["perDay"] == pytest.approx(16 / (18 * 60 + 37) * 24 * 60, rel=1e-3)
    assert soon["stretchMin"] == pytest.approx(16 / (23 * 6.21 / 25.46) * 60, rel=1e-3), "its week, not its window, ends the stretch"
    assert soon["drainsUntil"] == at("2026-10-08 06:00") and soon["why"].endswith("then its week is used up until 06:00")
    assert c["late"]["drainsUntil"] is None, "51% of a week outlasts a full window (~25%)"
    assert U.recommend(views, S) == {"id": "late"}, "the earlier rule agrees here: the lowest weekly figure"


def test_the_switch_of_15_51_goes_to_the_empty_account() -> None:
    """Real figures of 2026-10-07 15:51: the account in use filled its 5-hour window. One account
    has 17% of its week left until tomorrow 05:59, another a whole week until Monday."""
    now = at("2026-10-07 15:51")
    views = [
        view("cur", 98, 73, s_reset="2026-10-07 16:29", w_reset="2026-10-10 18:59", active=True, rate=27),
        view("nearly", 0, 83, w_reset="2026-10-08 05:59"),
        view("empty", 0, 0, w_reset="2026-10-12 08:00"),
    ]
    p = PL.plan(views, S, now, current_id="cur")
    assert p["action"]["to"] == "empty"
    assert by_id(p)["nearly"]["drainsUntil"] == at("2026-10-08 05:59")

    # The same week coming back at 21:00, ~2.5 h after it would run out: use it before it resets.
    soon = [views[0], view("nearly", 0, 83, w_reset="2026-10-07 21:00"), views[2]]
    p = PL.plan(soon, S, now, current_id="cur")
    assert p["action"]["to"] == "nearly" and by_id(p)["nearly"]["drainsUntil"] is None


def test_when_every_week_would_run_out_the_one_back_soonest_goes_first() -> None:
    views = [
        view("cur", 98, 50, s_reset="2026-10-07 13:00", active=True, rate=25),
        view("sat", 0, 80, w_reset="2026-10-10 18:59"),  # more room, but out of its week until Saturday
        view("thu", 0, 85, w_reset="2026-10-08 06:00"),
        view("crumbs", 85, 10, s_reset="2026-10-07 14:30"),  # its week lasts, but 13 points carry ~30 min
    ]
    p = PL.plan(views, S, NOW, current_id="cur")
    c = by_id(p)
    assert c["sat"]["drainsUntil"] and c["thu"]["drainsUntil"] and c["crumbs"]["drainsUntil"] is None
    assert PL.room_class(c["sat"]["room"]) == 0 and PL.room_class(c["thu"]["room"]) == 1 and PL.room_class(c["crumbs"]["room"]) == 2
    assert [x["id"] for x in p["candidates"] if x["usable"]] == ["thu", "sat", "crumbs"]


def test_more_room_beats_a_more_urgent_week() -> None:
    """The 5-hour window comes first: an account with some room left doesn't beat one with
    plenty, however soon its week resets."""
    views = [
        view("cur", 98, 50, s_reset="2026-10-07 14:00", w_reset="2026-10-12 08:00", active=True, rate=30),
        view("half", 55, 40, s_reset="2026-10-07 15:30", w_reset="2026-10-08 08:00"),  # week ends tomorrow
        view("fresh", 0, 20, w_reset="2026-10-13 08:00"),
    ]
    p = PL.plan(views, S, NOW, current_id="cur")
    assert p["action"]["to"] == "fresh"
    assert [PL.room_class(c["room"]) for c in p["candidates"]] == [0, 1]


def test_a_window_that_resets_before_it_fills_counts_as_empty() -> None:
    views = [
        view("cur", 98, 30, s_reset="2026-10-07 12:00", active=True, rate=30),
        view("soon", 70, 30, s_reset="2026-10-07 11:40", w_reset="2026-10-09 08:00"),  # 28 points need 56 min, it resets in 17
        view("later", 70, 30, s_reset="2026-10-07 15:00", w_reset="2026-10-09 08:00"),
    ]
    c = by_id(PL.plan(views, S, NOW, current_id="cur"))
    assert c["soon"]["room"] == pytest.approx(98) and c["later"]["room"] == pytest.approx(28)
    assert PL.plan(views, S, NOW, current_id="cur")["action"]["to"] == "soon"


def test_the_week_caps_the_room_so_a_week_isnt_drained_days_early() -> None:
    """An empty window on an account with 7% of its week left (resetting in days) is little
    room: picking it would leave that account unusable for days."""
    views = [
        view("cur", 98, 50, active=True, rate=30),
        view("lastbit", 5, 92, w_reset="2026-10-12 08:00"),
        view("some", 60, 20, s_reset="2026-10-07 15:00", w_reset="2026-10-12 08:00"),
    ]
    p = PL.plan(views, S, NOW, current_id="cur")
    c = by_id(p)
    assert c["lastbit"]["room"] == pytest.approx(7 * 25.46 / 6.21, rel=1e-6)
    assert p["action"]["to"] == "some"


def test_accounts_that_cant_take_over_and_why() -> None:
    views = [
        view("cur", 98, 50, active=True, rate=30),
        view("full5h", 98.5, 10, s_reset="2026-10-07 13:00"),
        view("fullweek", 0, 99, w_reset="2026-10-09 06:00"),
        view("aside", 0, 40, w_reset="2026-10-09 06:00"),
        view("nearly", 92, 10, s_reset="2026-10-07 15:00"),
        view("out", 0, 10, rotation=False),
        view("nosave", 0, 10, switchable=False),
        view("busy", 0, 10),
        {**view("nodata", 0, 0), "hasData": False},
    ]
    aside = {"aside": {"until": at("2026-10-09 06:00"), "at": NOW}}
    p = PL.plan(views, S, NOW, current_id="cur", aside=aside, in_use=lambda v: v["id"] == "busy")
    c = by_id(p)
    assert "5-hour limit full" in c["full5h"]["why"] and not c["full5h"]["usable"]
    assert "weekly limit full" in c["fullweek"]["why"] and not c["fullweek"]["usable"]
    assert c["aside"]["why"].startswith("weekly limit full: back") and c["aside"]["setAsideUntil"]
    assert "too little room" in c["nearly"]["why"] and not c["nearly"]["usable"]
    assert "rotation off" in c["out"]["why"] and not c["out"]["usable"]
    assert c["nosave"]["usable"] and not c["nosave"]["ok"] and "add-account" in c["nosave"]["why"]
    assert c["busy"]["usable"] and not c["busy"]["ok"] and "own Claude Code window" in c["busy"]["why"]
    assert c["nodata"]["why"] == "no figures yet"
    assert p["action"]["do"] == "wait", "only accounts whose login is saved here and idle can be switched to"
    assert "nosave@ could take over" in p["action"]["text"] or "busy@ could take over" in p["action"]["text"]
    assert p["freeId"] == "full5h" and p["freeAt"] == at("2026-10-07 13:00"), "the first account free again"
    assert PL.recommendation(p)["id"] in ("nosave", "busy"), "the suggestion may still need a sign-in or a closed window"


def test_triggers() -> None:
    def trig(session: float, weekly: float, **s) -> dict | None:
        return PL.trigger(view("cur", session, weekly, active=True), {**S, **s})

    assert trig(97.9, 98.9) is None
    assert trig(98, 10)["reason"] == "session"
    assert trig(100, 10)["reason"] == "limit"
    assert trig(10, 99)["reason"] == "weekly"
    assert trig(100, 99.5)["reason"] == "weekly", "a full week also sets the account aside"
    assert trig(90, 10, autoSessionPct=90)["reason"] == "session"
    assert trig(97, 10) is None


def test_stay_says_when_the_account_reaches_its_threshold() -> None:
    views = [view("cur", 50, 10, s_reset="2026-10-07 15:00", active=True, rate=60), view("other", 0, 10)]
    p = PL.plan(views, S, NOW, current_id="cur")
    assert p["action"]["do"] == "stay"
    assert p["current"]["switchAt"] == pytest.approx(NOW + 48 / 60 * HOUR, abs=1000)
    assert "11:" in p["action"]["text"] or "12:" in p["action"]["text"]

    slow = PL.plan([view("cur", 50, 10, s_reset="2026-10-07 15:00", active=True, rate=5), view("other", 0, 10)], S, NOW, current_id="cur")
    assert slow["action"] == {"do": "stay", "text": "cur@ has room for now."}, "at 5%/h the window resets first"


def test_early_switch_uses_a_week_that_would_otherwise_expire() -> None:
    """Real figures of 2026-10-07 12:22: the account in use has a week ending Saturday, while
    another's 16% ends tomorrow 06:00."""
    now = at("2026-10-07 12:22")
    views = [
        view("cur", 15, 52, s_reset="2026-10-07 16:29", w_reset="2026-10-10 18:59", active=True, rate=19),
        view("soon", 0, 83, s_reset="2026-10-07 15:00", w_reset="2026-10-08 06:00"),
        view("fresh", 0, 0, s_reset="2026-10-07 17:10", w_reset="2026-10-12 08:00"),
    ]
    assert PL.plan(views, S, now, current_id="cur")["action"]["do"] == "stay", "off by default"
    p = PL.plan(views, {**S, "autoEarly": True}, now, current_id="cur")
    assert p["action"]["do"] == "switch" and p["action"]["to"] == "soon" and p["action"]["reason"] == "early"
    assert "06:00" in p["action"]["because"]

    # Not for a week that ends in two days, nor one with too little left to carry an hour.
    later = [views[0], view("soon", 0, 83, w_reset="2026-10-09 18:00"), views[2]]
    assert PL.plan(later, {**S, "autoEarly": True}, now, current_id="cur")["action"]["do"] == "stay"
    crumbs = [views[0], view("soon", 0, 98, w_reset="2026-10-08 06:00"), views[2]]
    assert PL.plan(crumbs, {**S, "autoEarly": True}, now, current_id="cur")["action"]["do"] == "stay"


def test_the_pace_is_in_dollars_so_other_plans_fill_at_their_own_rate() -> None:
    max5 = {"session": {"usdPerPct": 6.21 / 4, "n": 0}, "weekly": {"usdPerPct": 25.46 / 4, "n": 0}}
    views = [view("cur", 98, 50, active=True, rate=20), {**view("small", 0, 10, w_reset="2026-10-12 08:00"), "calibration": max5}]
    small = by_id(PL.plan(views, S, NOW, current_id="cur"))["small"]
    assert small["stretchMin"] == pytest.approx(98 / 80 * 60, rel=1e-6), "20%/h of a Max 20x window is 80%/h of a Max 5x one"


def test_no_account_in_use_and_hours_left() -> None:
    views = [view("a", 10, 50, w_reset="2026-10-09 08:00"), view("b", 0, 0)]
    p = PL.plan(views, S, NOW, current_id=None)
    assert p["action"]["do"] == "none" and p["next"]["id"] in ("a", "b")
    hours = PL.hours_left(views, S, NOW, 30 * 6.21)
    assert hours == pytest.approx((49 + 99) * 25.46 / (30 * 6.21), rel=1e-6)
    assert PL.duration(65) == "1 h 05 min" and PL.duration(12) == "12 min" and PL.duration(float("inf")) == "for hours"
