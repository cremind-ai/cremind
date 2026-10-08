"""Disabling an account in the claude-usage-monitor skill: it is hidden, never suggested nor
switched to, raises no alerts and isn't asked about, until it is enabled again. Disabling the
account your Claude Code uses asks first, then hands your Claude Code to the best other account.

Fake Claude Code homes and a fake Anthropic, as in the switch and auto tests; the monitor runs
in this process, then end to end through the listener, the dashboard API and the CLI.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.skills.test_claude_usage_monitor_auto import answer, auto, events, figures, settle, start, texts  # noqa: F401 - fixture
from tests.skills.test_claude_usage_monitor_e2e import A, B, C3, Rig, view
from tests.skills.test_claude_usage_monitor_live import FakeAnthropic, anthropic, usage_body  # noqa: F401 - fixture
from tests.skills.test_claude_usage_monitor_switch import ORG, homes, make_home, read, snapshot, token  # noqa: F401 - fixture
from usage_monitor import common as C  # noqa: E402
from usage_monitor.switch import SwitchError  # noqa: E402

DISABLED = "hidden, never suggested nor switched to until it is enabled again"


def ids(rows: list[dict]) -> list[str]:
    return [r["id"] for r in rows]


def test_a_disabled_account_is_left_out_of_everything(auto: SimpleNamespace, tmp_path: Path) -> None:
    now = C.now_ms()
    m = start(auto, tmp_path, ("spare-b", B), ("spare-c", C3))
    figures(m, A, 50, 40, now)
    figures(m, B, 0, 10, now)  # the best account by far
    figures(m, C3, 30, 50, now)
    m.tick()
    assert m.api_state()["plan"]["next"]["id"] == B["uuid"]

    assert m.set_disabled(B["uuid"], True) == {"account": B["email"], "disabled": True}, "not the account in use: nothing to ask"
    assert texts(m)[-1] == f"Disabled {B['email']}: {DISABLED}"
    s = m.api_state()
    assert B["uuid"] not in ids(s["accounts"]) and ids(s["disabledAccounts"]) == [B["uuid"]]
    assert s["disabledAccounts"][0]["signedIn"] == ["spare-b"], "its login stays where it is"
    assert B["uuid"] not in ids(s["plan"]["candidates"]) and s["plan"]["next"]["id"] == C3["uuid"]
    assert B["uuid"] not in [t["account"] for t in m.live_targets(C.now_ms())], "its figures aren't fetched"
    with pytest.raises(SwitchError) as e:
        m.switch_account(B["uuid"])
    assert e.value.reason == "disabled" and e.value.fix == f"enable {B['email']}, then switch to it"

    # Automatic switching passes it by, though it has the most room...
    figures(m, A, 98, 40, now + 1000)
    answer(auto, C3, 30, 50, now)
    m.tick()
    settle(m)
    assert m.state["activeId"] == C3["uuid"]
    # ...and a limit hit while it is in use elsewhere (claude.ai, say) raises no alert.
    figures(m, B, 100, 10, now + 2000)
    m.tick()
    assert m.view_of(m.state["accounts"][B["uuid"]], C.now_ms())["running"]
    assert not any(B["email"] in t for t in events(tmp_path, "limit_reached"))

    assert m.set_disabled(B["uuid"], False) == {"account": B["email"], "disabled": False}
    assert texts(m)[-1].startswith(f"{B['email']} is enabled again")
    s = m.api_state()
    assert B["uuid"] in ids(s["accounts"]) and not s["disabledAccounts"]
    assert B["uuid"] in [t["account"] for t in m.live_targets(C.now_ms())]
    m.tick()
    assert any(B["email"] in t for t in events(tmp_path, "limit_reached")), "enabled, its alerts are back"


def test_disabling_the_account_in_use_asks_first_then_hands_over_to_the_best_other(auto: SimpleNamespace, tmp_path: Path) -> None:
    now = C.now_ms()
    m = start(auto, tmp_path, ("spare-b", B), ("spare-c", C3), mode="manual")
    figures(m, A, 40, 50, now)
    figures(m, B, 10, 30, now)
    figures(m, C3, 70, 30, now)
    answer(auto, B, 10, 30, now)
    m.tick()
    before = snapshot(auto.root)

    with pytest.raises(SwitchError) as e:
        m.set_disabled(A["uuid"], True)
    assert e.value.reason == "confirm"
    assert str(e.value).startswith(
        f"{A['email']} is the account your Claude Code uses now. Disabling it first switches your Claude Code to {B['email']} (5-hour 10%, weekly 30%: plenty of room"
    )
    assert snapshot(auto.root) == before and not m.state["accounts"][A["uuid"]].get("disabledAt"), "nothing changes before the user says yes"

    r = m.set_disabled(A["uuid"], True, confirm=True)
    assert r["switchedTo"] == B["email"] and r["switch"]["parkedIn"] == "first-example-com" and "reading" not in r["switch"]
    assert read(auto.main / ".credentials.json")["claudeAiOauth"]["refreshToken"] == token(B, "rt")
    assert read(auto.accounts / "first-example-com" / ".credentials.json")["claudeAiOauth"]["refreshToken"] == token(A, "rt"), "kept, not signed out"
    s = m.api_state()
    assert s["activeId"] == B["uuid"] and ids(s["disabledAccounts"]) == [A["uuid"]] and A["uuid"] not in ids(s["accounts"])
    lines = texts(m)
    [switched] = [t for t in lines if t.startswith(f"Switched Claude Code to {B['email']} — {A['email']} is being disabled; {B['email']}: 5-hour 10%")]
    assert 'kept in profile "first-example-com"' in switched and "ready to switch back" not in switched
    assert lines[-1] == f"Disabled {A['email']}: {DISABLED}"


def test_with_every_other_account_at_a_limit_it_still_hands_over(auto: SimpleNamespace, tmp_path: Path) -> None:
    """Leaving the account is the user's call: an account at its limit takes over when no other
    can, and the question says so. Automatic switching never brings the disabled one back."""
    now = C.now_ms()
    m = start(auto, tmp_path, ("spare-b", B))
    figures(m, A, 40, 50, now)
    figures(m, B, 100, 30, now)
    answer(auto, B, 100, 30, now)
    m.tick()
    settle(m)
    with pytest.raises(SwitchError) as e:
        m.set_disabled(A["uuid"], True)
    assert e.value.reason == "confirm" and f"to {B['email']} (5-hour limit full (100%), resets" in str(e.value)

    assert m.set_disabled(A["uuid"], True, confirm=True)["switchedTo"] == B["email"], "not refused as auto mode would leave it at once"
    m.tick()
    settle(m)
    assert m.state["activeId"] == B["uuid"]
    assert events(tmp_path, "no_account_available"), "auto mode has nowhere to go but says so"


def test_the_account_in_use_stays_when_no_other_can_take_over(auto: SimpleNamespace, tmp_path: Path) -> None:
    m = start(auto, tmp_path, ("spare-b", B), mode="manual")
    m.set_rotation(B["uuid"], False)  # the only other account is left out of switching
    before = snapshot(auto.root)
    with pytest.raises(SwitchError) as e:
        m.set_disabled(A["uuid"], True, confirm=True)
    assert e.value.reason == "no_replacement" and "add-account" in (e.value.fix or "")
    assert snapshot(auto.root) == before and not m.state["accounts"][A["uuid"]].get("disabledAt")


def test_a_failed_switch_leaves_the_account_in_use_enabled(auto: SimpleNamespace, tmp_path: Path) -> None:
    now = C.now_ms()
    m = start(auto, tmp_path, ("spare-b", B), mode="manual")
    figures(m, A, 40, 50, now)
    figures(m, B, 10, 30, now)
    m.tick()
    (auto.accounts / "spare-b" / ".oauth_refresh.lock").mkdir()  # Claude Code is renewing its login, and stays at it
    with pytest.raises(SwitchError) as e:
        m.set_disabled(A["uuid"], True, confirm=True)
    assert e.value.reason == "busy"
    assert str(e.value).startswith(f"{A['email']} was not disabled: your Claude Code couldn't be switched to another account ({B['email']}: ")
    assert not m.state["accounts"][A["uuid"]].get("disabledAt") and m.state["activeId"] == A["uuid"]
    assert read(auto.main / ".credentials.json")["claudeAiOauth"]["refreshToken"] == token(A, "rt")


def test_a_disabled_account_signed_in_with_login_shows_until_claude_code_moves_off_it(auto: SimpleNamespace, tmp_path: Path) -> None:
    m = start(auto, tmp_path, ("spare-b", B), ("spare-c", C3), mode="manual")
    m.set_disabled(B["uuid"], True)
    make_home(auto.main, B, n=2)  # /login with it in VS Code: the monitor can't stop that
    m.tick()
    s = m.api_state()
    b = view(s, B["uuid"])
    assert s["activeId"] == B["uuid"] and b["active"] and b["disabled"] and not s["disabledAccounts"], "shown while in use, still disabled"

    m.switch_account(C3["uuid"])
    s = m.api_state()
    assert s["activeId"] == C3["uuid"] and ids(s["disabledAccounts"]) == [B["uuid"]], "hidden again"


def test_disable_through_the_dashboard_and_the_cli(tmp_path: Path) -> None:
    fake = FakeAnthropic()
    rig = Rig(tmp_path)
    try:
        now = time.time() * 1000
        for who in (A, C3):
            fake.usage[token(who, "at")] = usage_body(40 if who is A else 10, 60 if who is A else 20, now)
            fake.orgs[token(who, "at")] = ORG[who["uuid"]]
        make_home(rig.main, A)
        make_home(rig.spare, C3)
        (rig.data / "profiles.json").write_text(json.dumps({"profiles": [{"name": "spare"}]}), encoding="utf-8")
        rig.env.update({"CLAUDE_USAGE_MONITOR_API_URL": fake.url, "CLAUDE_USAGE_MONITOR_TOKEN_URL": fake.url + "/v1/oauth/token"})
        rig.start()
        rig.until(lambda s: s["activeId"] == A["uuid"] and view(s, C3["uuid"])["switchable"] and view(s, C3["uuid"])["hasData"])

        # The account in use: the agent learns who would take over, and must ask the user.
        code, out = rig.cli("disable", "first")
        assert code == 1 and out["reason"] == "confirm" and f"switches your Claude Code to {C3['email']}" in out["error"]
        assert out["fix"] == f"Tell the user and ask them to confirm. Only if they do: disable {A['email']} --confirm"
        assert read(rig.main / ".claude.json")["oauthAccount"]["accountUuid"] == A["uuid"]

        code, out = rig.cli("disable", "first", "--confirm")
        assert code == 0 and out["disabled"] == A["email"] and out["your_claude_code_now_uses"] == C3["email"]
        assert out["its_login_kept_in"] == "profile first-example-com (new)" and out["checked_with_anthropic"] is True
        s = rig.until(lambda s: s["activeId"] == C3["uuid"] and ids(s["disabledAccounts"]) == [A["uuid"]])
        assert A["uuid"] not in ids(s["accounts"]) and s["disabledAccounts"][0]["signedIn"] == ["first-example-com"]

        code, out = rig.cli("switch", "first")
        assert code == 1 and out["reason"] == "disabled" and out["fix"] == f"enable {A['email']}, then switch to it"
        code, status = rig.cli("status")
        [d] = status["disabled_accounts"]
        assert d["account"] == A["email"] and d["enable_with"] == f"enable {A['email']}" and d["disabled_since"]
        assert A["email"] not in [a["account"] for a in status["accounts"]] and "Every other account is disabled" in status["recommendation"]
        code, plan = rig.cli("plan")
        assert [d["account"] for d in plan["disabled_accounts"]] == [A["email"]] and plan["ranking"] == []

        # The dashboard's switch: with the only other account disabled, nothing can take over.
        code, out = rig.post(f"api/accounts/{C3['uuid']}", {"disabled": True, "confirm": True})
        assert code == 409 and out["reason"] == "no_replacement" and out["state"]["activeId"] == C3["uuid"]

        code, out = rig.cli("enable", "first")
        assert code == 0 and out["enabled"] == A["email"] and out["switch_with"] == f"switch {A['email']}"
        rig.until(lambda s: not s["disabledAccounts"] and view(s, A["uuid"])["switchable"])
        code, out = rig.post(f"api/accounts/{C3['uuid']}", {"disabled": True})
        assert code == 409 and out["reason"] == "confirm" and A["email"] in out["error"]
        code, out = rig.post(f"api/accounts/{C3['uuid']}", {"disabled": True, "confirm": True})
        assert code == 200 and out["ok"] and out["result"]["switchedTo"] == A["email"]
        assert out["state"]["activeId"] == A["uuid"] and ids(out["state"]["disabledAccounts"]) == [C3["uuid"]]
        assert read(rig.main / ".credentials.json")["claudeAiOauth"]["refreshToken"] == token(A, "rt")
        assert read(rig.spare / ".credentials.json")["claudeAiOauth"]["refreshToken"] == token(C3, "rt")
        assert rig.post(f"api/accounts/{C3['uuid']}", {"disabled": "yes"})[0] == 400
        assert "sk-ant" not in json.dumps(rig.get())
    finally:
        rig.stop(graceful=False)
        fake.close()


def test_disable_while_the_monitor_is_stopped(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    make_home(rig.main, A)
    make_home(rig.spare, C3)
    (rig.data / "profiles.json").write_text(json.dumps({"profiles": [{"name": "spare"}]}), encoding="utf-8")
    code, out = rig.cli("disable", "first")
    assert code == 1 and out["reason"] == "confirm"
    code, out = rig.cli("disable", "first", "--confirm")  # Anthropic unreachable here: the switch goes ahead unchecked
    assert code == 0 and out["your_claude_code_now_uses"] == C3["email"] and out["checked_with_anthropic"] is False
    assert read(rig.main / ".claude.json")["oauthAccount"]["accountUuid"] == C3["uuid"]
    code, status = rig.cli("status")
    assert [d["account"] for d in status["disabled_accounts"]] == [A["email"]]
    rows = {r["profile"]: r for r in rig.cli("accounts")[1]["profiles"]}
    assert "switch_with" not in rows["first-example-com"] and rows["first-example-com"]["disabled"].startswith("yes")
    code, out = rig.cli("switch", "first")
    assert code == 1 and out["reason"] == "disabled"
    code, out = rig.cli("enable", "first")
    assert code == 0 and out["enabled"] == A["email"]
    code, out = rig.cli("switch", "first")
    assert code == 0 and out["switched"] and out["your_claude_code_now_uses"] == A["email"]
