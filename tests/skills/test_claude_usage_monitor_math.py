"""Unit tests for the claude-usage-monitor skill: the usage maths, pricing and transcript
reader, ported from the standalone app's test suite (test/run.js).

The skill's ``usage_monitor`` package is imported from its ``scripts`` folder; the name is
unique, so it cannot collide with Cremind's own ``app`` package.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from app.skills.sync import BUILTIN_SKILLS_DIR

SCRIPTS_DIR = BUILTIN_SKILLS_DIR / "claude-usage-monitor" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from usage_monitor import usage as U  # noqa: E402
from usage_monitor.pricing import reply_cost  # noqa: E402
from usage_monitor.transcripts import TranscriptTail  # noqa: E402

MIN = 60_000
HOUR = 60 * MIN
S = U.DEFAULT_SETTINGS


def iso_ms(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000


def near(a, b, tol):
    assert abs(a - b) <= tol, f"expected ~{b}, got {a}"


# ---------------------------------------------- parsing


def test_statusline_percent_and_epoch_seconds():
    now = 1_700_000_000_000
    u = U.from_statusline(
        {
            "five_hour": {"used_percentage": 47, "resets_at": 1_700_003_600},
            "seven_day": {"used_percentage": 68, "resets_at": 1_700_600_000},
            "spend_limit": {"used_percentage": 25, "resets_at": 1_700_600_000, "used_usd": 5, "limit_usd": 20, "period": "monthly"},
        },
        now,
    )
    assert u["source"] == "statusline"
    assert u["session"]["pct"] == 47
    assert u["session"]["resetsAt"] == 1_700_003_600_000
    assert u["weekly"]["pct"] == 68
    assert [u["spend"]["usedUsd"], u["spend"]["limitUsd"], u["spend"]["period"]] == [5, 20, "monthly"]


def test_statusline_absent_or_empty():
    assert U.from_statusline(None, 1) is None
    assert U.from_statusline({}, 1) is None
    assert U.from_statusline({"five_hour": {}}, 1) is None


def test_claude_cache_iso_and_scoped():
    u = U.from_claude_cache(
        {
            "fetchedAtMs": 1_700_000_000_000,
            "utilization": {
                "five_hour": {"utilization": 39, "resets_at": "2026-10-06T12:40:00.353924+00:00"},
                "seven_day": {"utilization": 22, "resets_at": "2026-10-12T01:00:00+00:00"},
                "limits": [
                    {"kind": "session", "group": "session", "percent": 39, "resets_at": "2026-10-06T12:40:00.353924+00:00"},
                    {"kind": "weekly_all", "group": "weekly", "percent": 22, "resets_at": "2026-10-12T01:00:00+00:00"},
                    {"kind": "weekly_scoped", "group": "weekly", "percent": 7, "resets_at": "2026-10-12T01:00:00+00:00", "scope": {"model": {"display_name": "Opus"}}},
                ],
            },
        }
    )
    assert u["source"] == "claude-cache"
    assert u["session"]["pct"] == 39
    near(u["session"]["resetsAt"], iso_ms("2026-10-06T12:40:00.353924+00:00"), 1)
    assert u["weekly"]["pct"] == 22
    assert len(u["scoped"]) == 1
    assert "Opus" in u["scoped"][0]["label"]


def test_claude_cache_rubbish():
    assert U.from_claude_cache(None) is None
    assert U.from_claude_cache({"utilization": {}}) is None
    assert U.from_claude_cache({"fetchedAtMs": 1, "utilization": {"limits": []}}) is None


# ---------------------------------------------- pricing


def test_reply_cost_list_prices():
    usd = reply_cost(
        "claude-opus-5-5",
        {
            "input_tokens": 1000,
            "output_tokens": 1000,
            "cache_read_input_tokens": 1_000_000,
            "cache_creation_input_tokens": 3000,
            "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 2000},
        },
    )
    near(usd, 0.004 + 0.02 + 0.2 + 0.005 + 0.016, 1e-9)


def test_reply_cost_models_fast_unknown():
    near(reply_cost("claude-haiku-4-5-20251001", {"input_tokens": 914, "output_tokens": 11}), 0.000969, 1e-9)
    near(reply_cost("claude-fable-5-1", {"cache_read_input_tokens": 1e6}), 0.25, 1e-9)
    near(reply_cost("claude-fable-5", {"cache_read_input_tokens": 1e6}), 1, 1e-9)
    near(reply_cost("claude-opus-5", {"output_tokens": 1e6}), 25, 1e-9)
    near(reply_cost("claude-opus-5-5", {"output_tokens": 1e6, "speed": "fast"}), 40, 1e-9)
    assert reply_cost("gpt-9", {"output_tokens": 5}) is None
    near(reply_cost("claude-sonnet-5-5", {"cache_creation_input_tokens": 1e6}), 2.5, 1e-9)


# ---------------------------------------------- spend log


def test_spendlog():
    t0 = iso_ms("2026-10-06T12:00:00Z")
    log = U.SpendLog()
    log.add(t0 + 10e3, 1)
    log.add(t0 + 50e3, 2)
    log.add(t0 + 5 * MIN, 4)
    log.add(t0 + 2 * MIN, 8)
    assert log.between(t0, t0 + 10 * MIN) == 15
    assert log.between(t0 + MIN, t0 + 3 * MIN) == 8
    assert log.first_after(t0 + MIN) == t0 + 2 * MIN
    assert log.first_after(t0 + 6 * MIN) is None
    assert log.last() == t0 + 5 * MIN
    copy = U.SpendLog(json.loads(json.dumps(log.to_json())))
    assert copy.between(t0, t0 + 10 * MIN) == 15
    log.prune(t0 + 3 * MIN)
    assert log.between(0, float("inf")) == 4


# ---------------------------------------------- estimates

T = iso_ms("2026-10-06T12:14:37Z")


def test_estimate_rolls_forward():
    log = U.SpendLog([[T + 5 * MIN, 57]])
    w = U.estimate_window({"pct": 31, "resetsAt": T + 4 * HOUR, "at": T}, log, 5.7, "session", T + 30 * MIN)
    near(w["pct"], 41, 1e-9)
    assert w["resetsAt"] == T + 4 * HOUR
    assert w["estimated"] is True
    assert w["official"] == 31
    assert w["approxReset"] is False


def test_estimate_no_spend_official_stands():
    w = U.estimate_window({"pct": 31, "resetsAt": T + 4 * HOUR, "at": T}, U.SpendLog(), 5.7, "session", T + HOUR)
    assert w["pct"] == 31
    assert w["estimated"] is False


def test_estimate_zero_reading_opens_window_aligned():
    first_use = iso_ms("2026-10-06T11:27:40Z")
    log = U.SpendLog([[first_use, 28.5]])
    w = U.estimate_window({"pct": 0, "resetsAt": None, "at": iso_ms("2026-10-06T11:26:04Z")}, log, 5.7, "session", T)
    assert w["resetsAt"] == iso_ms("2026-10-06T16:20:00Z")
    near(w["pct"], 5, 1e-9)
    assert w["approxReset"] is True


def test_estimate_after_reset_new_window():
    reset = T + HOUR
    log = U.SpendLog([[T + 10 * MIN, 28.5], [reset + 25 * MIN, 57]])
    w = U.estimate_window({"pct": 80, "resetsAt": reset, "at": T}, log, 5.7, "session", reset + HOUR)
    near(w["pct"], 10, 1e-9)
    assert w["rolledOver"] is True
    near(w["lastPct"], 85, 1e-9)
    assert w["endedAt"] == reset
    assert w["resetsAt"] == iso_ms("2026-10-06T18:30:00Z")


def test_estimate_after_reset_fresh():
    reset = T + HOUR
    w = U.estimate_window({"pct": 97, "resetsAt": reset, "at": T}, U.SpendLog(), 5.7, "session", reset + 5 * MIN)
    assert [w["pct"], w["resetsAt"], w["rolledOver"], w["lastPct"]] == [0, None, True, 97]


def test_estimate_weekly_and_caps():
    reset = iso_ms("2026-10-12T01:00:00Z")
    log = U.SpendLog([[reset + 90 * MIN, 31]])
    w = U.estimate_window({"pct": 22, "resetsAt": reset, "at": reset - 2 * 24 * HOUR}, log, 31, "weekly", reset + 3 * HOUR)
    near(w["pct"], 1, 1e-9)
    assert w["resetsAt"] == reset + HOUR + 7 * 24 * HOUR
    capped = U.estimate_window({"pct": 95, "resetsAt": T + HOUR, "at": T}, U.SpendLog([[T + MIN, 570]]), 5.7, "session", T + 2 * MIN)
    assert capped["pct"] == 100
    assert capped["estimated"] is True
    notice = iso_ms("2026-10-06T12:00:20Z")
    hit = U.estimate_window({"pct": 100, "resetsAt": notice + HOUR, "at": notice}, U.SpendLog([[notice - 10e3, 93]]), 5.7, "session", notice + 2 * MIN)
    assert [hit["pct"], hit["estimated"]] == [100, False]
    assert U.estimate_window(None, None, 5.7, "session", T) is None


def test_calibration():
    assert U.calibration_seed("default_claude_max_20x")["session"]["usdPerPct"] == 5.7
    near(U.calibration_seed("default_claude_max_5x")["session"]["usdPerPct"], 1.425, 1e-9)
    cal = {"usdPerPct": 5.7, "n": 0}
    b = U.blend_calibration(cal, 5.5)
    assert 5.5 < b["usdPerPct"] < 5.7 and b["n"] == 1
    assert U.blend_calibration(cal, 50) is None
    assert U.blend_calibration(cal, 0) is None


def test_spend_rate():
    log = U.SpendLog()
    for i in range(30):
        log.add(T - i * MIN - 1, 95 / 30)
    near(U.spend_rate(log, 5.7, T, 0)["perHour"], 33.33, 0.2)
    assert U.spend_rate(log, 5.7, T, T - 10 * MIN)["basis"] == "last 10 min"


def test_window_series_passes_reading():
    resets_at = T + 4 * HOUR
    log = U.SpendLog([[T - 30 * MIN, 57], [T + 10 * MIN, 57]])
    win = U.estimate_window({"pct": 31, "resetsAt": resets_at, "at": T}, log, 5.7, "session", T + 20 * MIN)
    pts = U.window_series(win, {"pct": 31, "resetsAt": resets_at, "at": T}, log, 5.7, T + 20 * MIN)
    at = lambda t: next(p for p in pts if p[0] >= t)[1]  # noqa: E731
    near(at(T), 31, 1e-9)
    near(pts[-1][1], 41, 1e-9)
    assert pts[0][1] < 31


def test_window_series_shared_minute():
    at = iso_ms("2026-10-06T12:00:20Z")
    reading = {"pct": 40, "resetsAt": at + 2 * HOUR, "at": at}
    log = U.SpendLog([[at + 25e3, 57]])
    now = at + 5 * MIN
    win = U.estimate_window(reading, log, 5.7, "session", now)
    pts = U.window_series(win, reading, log, 5.7, now)
    near(win["pct"], 50, 1e-9)
    near(pts[-1][1], win["pct"], 1e-9)


# ---------------------------------------------- transcripts


def _iso(t_ms: float) -> str:
    return datetime.fromtimestamp(t_ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def test_transcript_tail(tmp_path: Path):
    root = tmp_path / "projects"
    (root / "proj" / "sess" / "subagents").mkdir(parents=True)
    file = root / "proj" / "sess" / "subagents" / "agent-1.jsonl"
    since = iso_ms("2026-10-06T12:00:00Z")

    def reply(i, t, out):
        return json.dumps({"type": "assistant", "timestamp": _iso(t), "requestId": f"req_{i}", "message": {"id": f"msg_{i}", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "secret"}], "usage": {"output_tokens": out}}})

    file.write_text(
        "\n".join(
            [
                reply("old", since - MIN, 1e6),
                reply("a", since + MIN, 1e6),
                reply("a", since + MIN, 1e6),
                json.dumps({"type": "assistant", "timestamp": _iso(since + 2 * MIN), "message": {"model": "<synthetic>", "usage": {"output_tokens": 9}}}),
                json.dumps({"type": "assistant", "timestamp": _iso(since + 3 * MIN), "quotaLimits": {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": 1791257400}, "message": {"model": "<synthetic>"}}),
                '{"type":"user","timestamp":"broken',
                "",
            ]
        ),
        encoding="utf-8",
        newline="\n",
    )
    tail = TranscriptTail(str(root), {"since": since}, {})
    ev = tail.poll(since + 10 * MIN)
    replies = [e for e in ev if e["kind"] == "reply"]
    assert len(replies) == 1, ev
    near(replies[0]["usd"], 20, 1e-9)
    assert "secret" not in json.dumps(ev)
    limit = next(e for e in ev if e["kind"] == "limit")
    assert [limit["type"], limit["resetsAt"]] == ["five_hour", 1791257400 * 1000]
    assert tail.caught_up is True

    # A half-written line is held back until it is complete.
    line = reply("b", since + 4 * MIN, 5e5)
    with open(file, "a", encoding="utf-8", newline="\n") as f:
        f.write(line[:40])
    assert tail.poll(since + 11 * MIN) == []
    with open(file, "a", encoding="utf-8", newline="\n") as f:
        f.write(line[40:] + "\n")
    ev = tail.poll(since + 12 * MIN)  # the hot check picks it up without a rescan
    assert len(ev) == 1
    near(ev[0]["usd"], 10, 1e-9)

    # A new reader resuming from the saved offsets doesn't count anything twice.
    again = TranscriptTail(str(root), json.loads(json.dumps(tail.store)), {})
    assert again.poll(since + 13 * MIN) == []


def test_transcript_tail_discovers_new_files_between_rescans(tmp_path: Path):
    root = tmp_path / "projects"
    (root / "proj").mkdir(parents=True)
    since = 1_000_000.0
    tail = TranscriptTail(str(root), {"since": since}, {})
    now = 2_000_000_000_000.0
    assert tail.poll(now) == []  # full scan: nothing yet
    sub = root / "proj" / "sess" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-2.jsonl").write_text(
        json.dumps({"type": "assistant", "timestamp": "2033-05-18T03:33:20Z", "requestId": "r", "message": {"id": "m", "model": "claude-haiku-4-5", "usage": {"output_tokens": 1e6}}}) + "\n",
        encoding="utf-8",
    )
    # Not yet time for a full rescan, but the changed folder is listed.
    ev = tail.poll(now + 6_000)
    assert [e["kind"] for e in ev] == ["reply"], ev


# ---------------------------------------------- status & recommendation


def test_status_of_levels():
    now = 1_000
    cases = [
        ({"active": True, "session": {"pct": 10}, "weekly": {"pct": 5}}, "good"),
        ({"active": True, "session": {"pct": 85}, "weekly": {"pct": 5}}, "warning"),
        ({"active": True, "session": {"pct": 95}, "weekly": {"pct": 5}}, "critical"),
        ({"active": True, "session": {"pct": 100, "resetsAt": now}, "weekly": {"pct": 5}}, "critical"),
        ({"active": True, "session": {"pct": 10}, "weekly": {"pct": 92}}, "warning"),
        ({"active": True, "session": {"pct": 10}, "weekly": {"pct": 96}}, "critical"),
        ({"active": False, "session": {"pct": 95, "resetsAt": now}, "weekly": {"pct": 5}}, "serious"),
        ({"active": False, "session": {"pct": 10}, "weekly": {"pct": 92, "resetsAt": now}}, "warning"),
        ({"active": False, "session": {"pct": 10}, "weekly": {"pct": 96, "resetsAt": now}}, "serious"),
        ({"active": False, "session": {"pct": 10}, "weekly": {"pct": 10}}, "good"),
        ({"active": False, "session": None, "weekly": None}, "none"),
    ]
    for v, want in cases:
        st = U.status_of(v, S)
        assert st["level"] == want, (v, st)
        assert st["icon"] and st["label"]


def test_status_weekly_labels():
    st = U.status_of({"active": True, "session": {"pct": 2}, "weekly": {"pct": 100, "resetsAt": 5}}, S)
    assert st["level"] == "critical" and "eekly" in st["label"]
    assert U.status_of({"active": True, "session": {"pct": 40}, "weekly": {"pct": 96}}, S)["label"] == "Switch now (weekly)"
    assert U.status_of({"active": True, "session": {"pct": 91}, "weekly": {"pct": 40}}, S)["label"] == "Switch now (5-hour)"
    assert U.status_of({"active": True, "session": {"pct": 40}, "weekly": {"pct": 92}}, S)["label"] == "Near weekly limit"


def test_forecast():
    now = 2_000_000_000
    f = U.forecast({"pct": 60, "resetsAt": now + 3 * HOUR}, {"perHour": 20}, now, 90)
    near(f["limitAt"], now + 2 * HOUR, MIN)
    near(f["switchAt"], now + 1.5 * HOUR, MIN)
    assert f["resetsFirst"] is False
    assert U.forecast({"pct": 20, "resetsAt": now + HOUR}, {"perHour": 5}, now, 90)["resetsFirst"] is True
    assert U.forecast({"pct": 20, "resetsAt": now + HOUR}, {"perHour": 0}, now, 90)["idle"] is True


def test_forecast_limits_weekly_first():
    now = 2_000_000_000
    session = {"pct": 58, "resetsAt": now + HOUR}
    weekly = {"pct": 98, "resetsAt": now + 4 * 24 * HOUR}
    rate = {"session": {"perHour": 53}, "weekly": {"perHour": 9.6}}
    f = U.forecast_limits(session, weekly, rate, now, S)
    assert f["first"]["kind"] == "weekly"
    near(f["first"]["limitAt"], now + 12.5 * MIN, MIN)
    assert f["first"]["switchAt"] <= now
    near(f["session"]["limitAt"], now + 47.5 * MIN, MIN)
    roomy = U.forecast_limits(session, {"pct": 30, "resetsAt": weekly["resetsAt"]}, rate, now, S)
    assert roomy["first"]["kind"] == "session"
    slow = U.forecast_limits(session, weekly, {"session": {"perHour": 0.5}, "weekly": {"perHour": 0.09}}, now, S)
    assert slow["idle"] and slow["weekly"]["idle"] and not slow["first"]
    assert U.forecast_limits(session, weekly, None, now, S) is None


def test_burn_rate_fallback():
    now = 2_000_000_000
    reset = now + 2 * HOUR
    samples = [[now - i * MIN, 50 - i * 0.5, reset, 20] for i in range(15, -1, -1)]
    near(U.burn_rate(samples, {"pct": 50, "resetsAt": reset}, now)["perHour"], 30, 0.6)


def acct(i, sp, wp, **extra):

    nowms = time.time() * 1000
    v = {"id": i, "hasData": True, "active": False, "session": {"pct": sp, "resetsAt": nowms + HOUR}, "weekly": {"pct": wp, "resetsAt": nowms + 3 * 24 * HOUR}}
    v.update(extra)
    return v


def test_recommend():
    assert U.recommend([acct("a", 95, 10, active=True), acct("b", 40, 10), acct("c", 5, 10)], S)["id"] == "c"
    assert U.recommend([acct("a", 10, 10, active=True), acct("b", 95, 10), acct("c", 30, 10)], S)["id"] == "c"
    assert U.recommend([acct("a", 50, 10, active=True), acct("b", 5, 92), acct("c", 60, 20)], S)["id"] == "c"


def test_recommend_weekly_blocked():

    week_reset = time.time() * 1000 + 2 * 24 * HOUR
    views = [acct("a", 50, 99, active=True), acct("b", 0, 96, weekly={"pct": 96, "resetsAt": week_reset})]
    assert U.recommend(views, S) == {"id": None, "freeId": "b", "freeAt": week_reset}
    assert U.usable(views[1], {**S, "weeklyCritPct": 97}) is True


def test_recommend_none_usable():

    nowms = time.time() * 1000
    soon = nowms + 20 * MIN
    rec = U.recommend(
        [acct("a", 99, 10, active=True), acct("b", 99, 10, session={"pct": 99, "resetsAt": nowms + 2 * HOUR}), acct("c", 99, 10, session={"pct": 99, "resetsAt": soon})],
        S,
    )
    assert [rec["id"], rec["freeId"], rec["freeAt"]] == [None, "c", soon]
    assert U.recommend([acct("a", 95, 10, active=True), {"id": "b", "hasData": False, "session": None, "weekly": None}], S) is None


def test_js_round_halves_up():
    assert U.js_round(2.5) == 3 and U.js_round(-2.5) == -2 and U.js_round(0.49) == 0
