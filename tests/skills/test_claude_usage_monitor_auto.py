"""Automatic switching in the claude-usage-monitor skill: when the account your Claude Code uses
reaches a threshold, the monitor moves your Claude Code to the best account by itself.

Fake Claude Code homes and a fake Anthropic, as in the switch tests; the monitor runs in this
process (its ticks driven by the test), and once end to end through the listener and the CLI.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.skills.test_claude_usage_monitor_e2e import A, B, C3, Rig, frontmatter, view
from tests.skills.test_claude_usage_monitor_live import FakeAnthropic, anthropic, usage_body  # noqa: F401 - fixture
from tests.skills.test_claude_usage_monitor_switch import ORG, homes, make_home, read, register, token  # noqa: F401 - fixture
from usage_monitor import common as C  # noqa: E402
from usage_monitor import monitor as M  # noqa: E402
from usage_monitor import usage as U  # noqa: E402
from usage_monitor.monitor import Monitor  # noqa: E402
from usage_monitor.switch import SwitchError  # noqa: E402

MIN = 60_000
HOUR = 60 * MIN
DAY = 24 * HOUR


def body(session: float, weekly: float, now: float, *, session_in: float = 2 * HOUR, weekly_in: float = 3 * DAY) -> dict:
    """Anthropic's usage answer with the windows resetting when given."""
    b = usage_body(session, weekly, now)
    s_iso, w_iso = (time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime((now + d) / 1000)) for d in (session_in, weekly_in))
    b["five_hour"]["resets_at"], b["seven_day"]["resets_at"] = s_iso, w_iso
    for item in b["limits"]:
        item["resets_at"] = s_iso if item["kind"] == "session" else w_iso
    return b


def figures(m: Monitor, who: dict, session: float, weekly: float, at: float, **kw) -> None:
    """Anthropic's figures for an account, as the poller would apply them."""
    m.apply_live(who["uuid"], U.from_usage_api(body(session, weekly, at, **kw), at))


def answer(homes: SimpleNamespace, who: dict, session: float, weekly: float, now: float, **kw) -> None:
    """What Anthropic answers for the account's login: the switch's last check reads it."""
    homes.anthropic.usage[token(who, "at")] = body(session, weekly, now, **kw)


def events(tmp_path: Path, event_type: str) -> list[str]:
    folder = tmp_path / "events" / event_type
    return sorted(p.read_text(encoding="utf-8") for p in folder.glob("*.md")) if folder.exists() else []


def start(homes: SimpleNamespace, tmp_path: Path, *spares: tuple[str, dict], mode: str = "auto", paths: C.Paths | None = None) -> Monitor:
    for name, who in spares:
        make_home(homes.accounts / name, who)
    register(paths or homes.paths, *(name for name, _ in spares))
    m = Monitor(paths or homes.paths, events_dir=tmp_path / "events")
    m.apply_settings({"switchMode": mode})
    for _ in range(3):
        m.tick()
    assert m.ready and m.state["activeId"] == A["uuid"]
    return m


def settle(m: Monitor) -> None:
    """Let a switch the last tick started finish."""
    if m.auto_thread is not None:
        m.auto_thread.join(15)
        assert not m.auto_thread.is_alive()


def texts(m: Monitor) -> list[str]:
    return [e["text"] for e in m.state["events"]]


@pytest.fixture
def auto(homes: SimpleNamespace, tmp_path: Path) -> SimpleNamespace:
    make_home(homes.main, A)
    return homes


def test_at_the_threshold_it_switches_to_the_best_account_and_says_so(auto: SimpleNamespace, tmp_path: Path) -> None:
    now = C.now_ms()
    m = start(auto, tmp_path, ("spare-b", B), ("spare-c", C3))
    figures(m, A, 97, 40, now)
    figures(m, B, 10, 30, now)
    figures(m, C3, 70, 30, now)
    answer(auto, B, 10, 30, now)
    m.tick()
    settle(m)
    assert read(auto.main / ".credentials.json")["claudeAiOauth"]["refreshToken"] == token(A, "rt"), "97% is below the threshold"

    figures(m, A, 98, 40, now + 1000)
    m.tick()
    settle(m)
    assert read(auto.main / ".credentials.json")["claudeAiOauth"]["refreshToken"] == token(B, "rt")
    assert read(auto.accounts / "first-example-com" / ".credentials.json")["claudeAiOauth"]["refreshToken"] == token(A, "rt")
    assert m.state["activeId"] == B["uuid"]
    [line] = [t for t in texts(m) if t.startswith(f"Auto mode switched Claude Code to {B['email']} — {A['email']} reached 98% of its 5-hour limit")]
    assert f"; {B['email']}: 5-hour 10%, weekly 30%: plenty of room" in line, "why this account, not only why the switch"

    [event] = events(tmp_path, "auto_switched")
    fm = frontmatter(event)
    assert fm["from"] == A["email"] and fm["to"] == B["email"] and fm["reason"] == "5-hour limit" and fm["next_in_line"] == C3["email"]
    assert fm["from_used"] == "5-hour 98%, weekly 40%" and fm["to_used"] == "5-hour 10%, weekly 30%"
    assert "carry on from their next message" in event and "sk-ant" not in event
    assert not events(tmp_path, "switch_now") and not events(tmp_path, "limit_warning"), "no heads-up alerts in automatic mode"

    m.tick()
    settle(m)
    assert len(events(tmp_path, "auto_switched")) == 1, "the new account has room: nothing more to do"


def test_a_full_week_is_set_aside_until_it_resets(auto: SimpleNamespace, tmp_path: Path) -> None:
    now = C.now_ms()
    m = start(auto, tmp_path, ("spare-b", B))
    figures(m, A, 10, 99, now)
    figures(m, B, 10, 30, now)
    answer(auto, B, 10, 30, now)
    m.tick()
    settle(m)
    assert m.state["activeId"] == B["uuid"], "a full week switches whatever the 5-hour window says"
    until = m.state["setAside"][A["uuid"]]["until"]
    assert abs(until - (now + 3 * DAY)) < 2 * MIN, "until its week resets"
    fm = frontmatter(events(tmp_path, "auto_switched")[0])
    assert fm["reason"] == "weekly limit" and fm["set_aside_until"]
    plan = m.api_state()["plan"]
    assert [x["account"] for x in plan["setAside"]] == [A["email"]]
    assert next(c for c in plan["candidates"] if c["id"] == A["uuid"])["why"].startswith("weekly limit full: back")

    with pytest.raises(SwitchError) as e:
        m.switch_account(A["uuid"])
    assert e.value.reason == "auto_blocked" and "manual" in (e.value.fix or "")

    # Anthropic resets the week early: the account can take over again at once.
    figures(m, A, 0, 2, now + 60_000)
    m.tick()
    assert A["uuid"] not in m.state["setAside"]
    assert any("can take over again: its week was reset early" in t for t in texts(m))
    answer(auto, A, 0, 2, now)
    assert m.switch_account(A["uuid"])["account"] == A["uuid"], "a manual switch to it works again"


def test_with_nothing_to_switch_to_it_says_so_once_then_switches_when_an_account_frees_up(auto: SimpleNamespace, tmp_path: Path) -> None:
    now = C.now_ms()
    m = start(auto, tmp_path, ("spare-b", B), ("spare-c", C3))
    figures(m, A, 99, 40, now)
    figures(m, B, 99, 30, now, session_in=HOUR)  # its window is full for another hour
    figures(m, C3, 10, 99.5, now)  # its week is full
    for _ in range(3):
        m.tick()
        settle(m)
    [stuck] = events(tmp_path, "no_account_available")
    fm = frontmatter(stuck)
    assert fm["account"] == A["email"] and fm["first_free"] == B["email"] and fm["limit"] == "5-hour"
    assert read(auto.main / ".credentials.json")["claudeAiOauth"]["refreshToken"] == token(A, "rt")

    figures(m, B, 0, 30, now + 60_000)  # its window reset
    answer(auto, B, 0, 30, now)
    m.tick()
    settle(m)
    assert m.state["activeId"] == B["uuid"]
    assert frontmatter(events(tmp_path, "auto_switched")[0])["to"] == B["email"]


def test_an_account_whose_fresh_figures_say_full_is_skipped_for_the_next(auto: SimpleNamespace, tmp_path: Path) -> None:
    now = C.now_ms()
    m = start(auto, tmp_path, ("spare-b", B), ("spare-c", C3))
    figures(m, A, 98, 40, now)
    figures(m, B, 0, 20, now)  # the last figures say empty...
    figures(m, C3, 30, 30, now)
    answer(auto, B, 97, 20, now)  # ...but it was used on claude.ai meanwhile
    answer(auto, C3, 30, 30, now)
    m.tick()
    settle(m)
    assert m.state["activeId"] == C3["uuid"]
    assert read(auto.accounts / "spare-b" / ".credentials.json")["claudeAiOauth"]["refreshToken"] == token(B, "rt"), "nothing moved for it"
    assert m.view_of(m.state["accounts"][B["uuid"]], C.now_ms())["session"]["pct"] == 97, "its fresh figures are kept"


def test_when_every_try_fails_it_retries_and_then_says_why(auto: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = C.now_ms()
    m = start(auto, tmp_path, ("spare-b", B))
    figures(m, A, 98, 40, now)
    figures(m, B, 0, 20, now)
    answer(auto, B, 0, 20, now)
    (auto.accounts / "spare-b" / ".oauth_refresh.lock").mkdir()  # Claude Code is renewing its login, and stays at it
    monkeypatch.setattr(M, "AUTO_RETRY_MS", 0)
    for _ in range(M.AUTO_GIVE_UP):
        m.tick()
        settle(m)
    [failed] = events(tmp_path, "auto_switch_failed")
    assert "busy with its login files" in failed and frontmatter(failed)["account"] == A["email"]
    assert sum("Automatic switch didn't happen" in t for t in texts(m)) == M.AUTO_GIVE_UP
    assert m.auto_retry_at > C.now_ms() + 9 * MIN, "then every 10 minutes"
    assert m.state["activeId"] == A["uuid"]
    assert read(auto.accounts / "spare-b" / ".credentials.json")["claudeAiOauth"]["refreshToken"] == token(B, "rt")


def test_an_account_refused_on_its_fresh_figures_is_not_tried_again(auto: SimpleNamespace, tmp_path: Path) -> None:
    now = C.now_ms()
    m = start(auto, tmp_path, ("spare-b", B))
    figures(m, A, 98, 40, now)
    figures(m, B, 0, 20, now)
    answer(auto, B, 96, 20, now)  # used elsewhere meanwhile: too little room left
    m.tick()
    settle(m)
    m.tick()
    settle(m)
    assert len(events(tmp_path, "no_account_available")) == 1, "its fresh figures show nothing can take over"
    assert sum("Automatic switch didn't happen" in t for t in texts(m)) == 1


def test_manual_mode_never_switches_and_its_alerts_name_the_best_account(auto: SimpleNamespace, tmp_path: Path) -> None:
    now = C.now_ms()
    m = start(auto, tmp_path, ("spare-b", B), ("spare-c", C3), mode="manual")
    figures(m, A, 98, 40, now)
    figures(m, B, 0, 48, now)  # its week lasts three more days
    figures(m, C3, 0, 83, now, weekly_in=18 * HOUR)  # 16% of a week that ends tomorrow morning: used up in ~3 h
    m.tick()
    settle(m)
    assert m.state["activeId"] == A["uuid"] and m.auto_thread is None
    fm = frontmatter(events(tmp_path, "switch_now")[0])
    assert fm["switch_to"] == B["email"] and fm["switch_command"] == f"switch {B['email']}"
    assert "plenty of room" in fm["switch_why"] and "51% of its week left" in fm["switch_why"]
    assert not (auto.main / M.CLAIM_FILE).exists(), "manual mode claims nothing"


def test_a_threshold_change_in_automatic_mode_is_logged(auto: SimpleNamespace, tmp_path: Path) -> None:
    """A switch is explained by the thresholds of its moment: changing them leaves a line."""
    m = start(auto, tmp_path, ("spare-b", B))
    m.apply_settings({"autoSessionPct": 95})
    assert texts(m)[-1] == "Automatic switching now at 95% of the 5-hour limit or 99% of the weekly one"
    before = len(texts(m))
    m.apply_settings({"autoSessionPct": 95, "autoWeeklyPct": 99, "autoEarly": False, "warnPct": 70})  # the dashboard sends every field
    m.apply_settings({"switchMode": "manual", "autoSessionPct": 90})
    m.apply_settings({"autoSessionPct": 92})
    assert texts(m)[before:] == ["Automatic switching off: alerts only; you or the agent switch"]


def test_heads_up_alerts_wait_for_manual_mode(auto: SimpleNamespace, tmp_path: Path) -> None:
    now = C.now_ms()
    m = start(auto, tmp_path, ("spare-b", B))
    figures(m, A, 85, 40, now)
    figures(m, B, 0, 20, now)
    m.tick()
    assert not events(tmp_path, "limit_warning") and m.state["activeId"] == A["uuid"]
    m.apply_settings({"switchMode": "manual"})
    m.tick()
    assert len(events(tmp_path, "limit_warning")) == 1


def test_only_one_monitor_switches_this_computers_claude_code(auto: SimpleNamespace, tmp_path: Path) -> None:
    now = C.now_ms()
    m1 = start(auto, tmp_path, ("spare-b", B))
    claim = auto.main / M.CLAIM_FILE
    assert read(claim)["owner"].endswith("data")

    other = C.Paths(tmp_path / "data2")
    m2 = start(auto, tmp_path, ("spare-b", B), paths=other)
    figures(m2, A, 98, 40, now)
    figures(m2, B, 0, 20, now)
    answer(auto, B, 0, 20, now)
    m2.tick()
    settle(m2)
    assert m2.state["activeId"] == A["uuid"] and m2.auto_thread is None
    assert m2.api_state()["plan"]["owner"] == str(auto.paths.data), "the dashboard says who switches instead"

    m1.apply_settings({"switchMode": "manual"})
    m1.tick()
    assert not claim.exists(), "given up when switched off"
    m2.tick()
    settle(m2)
    assert m2.state["activeId"] == B["uuid"] and read(claim)["owner"].endswith("data2")
    m2.close()
    assert not claim.exists(), "given up when the monitor stops"


def test_settings_are_kept_in_range() -> None:
    m = Monitor(C.Paths(Path(__file__).parent / "does-not-exist"), readonly=True)
    s = m.apply_settings({"switchMode": "auto", "autoSessionPct": 120, "autoWeeklyPct": 20, "autoEarly": True})
    assert s["switchMode"] == "auto" and s["autoSessionPct"] == 99 and s["autoWeeklyPct"] == 50 and s["autoEarly"] is True
    assert m.apply_settings({"switchMode": "sometimes"})["switchMode"] == "auto"
    assert U.DEFAULT_SETTINGS["switchMode"] == "manual" and U.DEFAULT_SETTINGS["autoSessionPct"] == 98 and U.DEFAULT_SETTINGS["autoWeeklyPct"] == 99


def test_automatic_switching_through_the_dashboard_and_the_cli(tmp_path: Path) -> None:
    fake = FakeAnthropic()
    rig = Rig(tmp_path)
    try:
        now = time.time() * 1000
        for who, (s5, s7) in ((A, (50, 40)), (C3, (10, 20))):
            fake.usage[token(who, "at")] = body(s5, s7, now)
            fake.orgs[token(who, "at")] = ORG[who["uuid"]]
        make_home(rig.main, A)
        make_home(rig.spare, C3)
        (rig.data / "profiles.json").write_text(json.dumps({"profiles": [{"name": "spare"}]}), encoding="utf-8")
        rig.env.update(
            {
                "CLAUDE_USAGE_MONITOR_API_URL": fake.url,
                "CLAUDE_USAGE_MONITOR_TOKEN_URL": fake.url + "/v1/oauth/token",
                "CLAUDE_USAGE_MONITOR_LIVE_ACTIVE_MS": "400",
                "CLAUDE_USAGE_MONITOR_LIVE_WATCHED_MS": "300",
                "CLAUDE_USAGE_MONITOR_LIVE_IDLE_MS": "800",
                "CLAUDE_USAGE_MONITOR_LIVE_MIN_GAP_MS": "100",
            }
        )
        rig.start()
        s = rig.until(lambda s: s["activeId"] == A["uuid"] and view(s, C3["uuid"])["switchable"] and view(s, A["uuid"])["session"]["pct"] == 50)
        assert s["plan"]["mode"] == "manual" and s["plan"]["next"]["id"] == C3["uuid"] and s["plan"]["action"]["do"] == "stay"

        code, out = rig.cli("settings", "--switching", "auto", "--early", "off")
        assert code == 0 and out["settings"]["switchMode"] == "auto"
        code, plan = rig.cli("plan")
        assert code == 0 and plan["do"] == "stay" and plan["switching"]["mode"] == "automatic"
        assert plan["ranking"][0]["account"] == C3["email"] and plan["ranking"][0]["can_switch_to_it_now"]
        rig.until(lambda s: (rig.main / M.CLAIM_FILE).exists())

        fake.usage[token(A, "at")] = body(98, 41, now)  # the account in use reaches 98%
        s = rig.until(lambda s: s["activeId"] == C3["uuid"])
        assert read(rig.main / ".credentials.json")["claudeAiOauth"]["refreshToken"] == token(C3, "rt")
        [event] = rig.events("auto_switched")
        assert frontmatter(event)["to"] == C3["email"] and frontmatter(event)["from"] == A["email"]
        assert any(e["text"].startswith(f"Auto mode switched Claude Code to {C3['email']}") for e in s["events"])

        code, status = rig.cli("status")
        assert status["switching"]["mode"] == "automatic" and "98%" in status["switching"]["switches_at"]
        code, out = rig.cli("rotation", "first", "off")
        assert code == 0 and out["in_rotation"] is False
        s = rig.until(lambda s: view(s, A["uuid"])["rotation"] is False)
        assert "rotation off" in next(c for c in s["plan"]["candidates"] if c["id"] == A["uuid"])["why"]

        code, out = rig.post("api/settings", {"switchMode": "manual"})
        assert code == 200 and out["settings"]["switchMode"] == "manual"
        rig.until(lambda s: s["plan"]["mode"] == "manual" and not (rig.main / M.CLAIM_FILE).exists())
        assert "sk-ant" not in json.dumps(rig.get())
    finally:
        rig.stop(graceful=False)
        fake.close()
