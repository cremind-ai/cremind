"""Quick account switching in the claude-usage-monitor skill: another account becomes the one
your Claude Code uses, with the login saved on this computer — no /login.

Logins move between Claude Code homes (never copied: Anthropic replaces the refresh token at
each renewal, so of two copies only one would keep working), the way /login writes them and
under Claude Code's own locks. Claude Code's homes are temp folders; Anthropic is the local
fake from the live-figures tests.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.skills.sync import BUILTIN_SKILLS_DIR

SCRIPTS_DIR = BUILTIN_SKILLS_DIR / "claude-usage-monitor" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from usage_monitor import common as C  # noqa: E402
from usage_monitor import profiles as P  # noqa: E402
from usage_monitor import switch as S  # noqa: E402
from usage_monitor.monitor import Monitor  # noqa: E402

from tests.skills.test_claude_usage_monitor_e2e import A, B, C3, TIER, Rig, _append, _reply, view  # noqa: E402
from tests.skills.test_claude_usage_monitor_live import FakeAnthropic, anthropic, usage_body  # noqa: E402, F401

MIN = 60_000
HOUR = 60 * MIN
ORG = {A["uuid"]: "org-a", B["uuid"]: "org-b", C3["uuid"]: "org-c"}


def token(who: dict, kind: str = "at", n: int = 1) -> str:
    return f"sk-ant-o{kind}01-test-{who['email'].split('@')[0]}-{n}"


def oauth_account(who: dict) -> dict:
    return {
        "accountUuid": who["uuid"],
        "emailAddress": who["email"],
        "organizationUuid": ORG[who["uuid"]],
        "organizationName": f"{who['email']}'s Organization",
        "displayName": who["email"].split("@")[0],
        "organizationRateLimitTier": TIER,
        "billingType": "stripe_subscription",
    }


def make_home(home: Path, who: dict | None, *, expires_in: float = 4 * HOUR, n: int = 1, own: dict | None = None) -> None:
    """A Claude Code home signed in to ``who`` (None: signed out), with entries of its own
    that must stay with the folder: an MCP server's login, a project's settings."""
    home.mkdir(parents=True, exist_ok=True)
    config: dict[str, Any] = {
        "numStartups": 7,
        "userID": f"device-of-{home.name}",
        "hasCompletedOnboarding": True,
        "lastOnboardingVersion": "2.1.291",
        "projects": {f"C:/work/{home.name}": {"allowedTools": ["Bash"]}},
        "cachedUsageUtilization": {"accountUuid": (who or {}).get("uuid"), "utilization": {}},
        "modelAccessCache": ["claude-opus-5-5"],
        "clientDataCacheSlots": {"slot": {"org": "x"}},
    }
    creds: dict[str, Any] = {"mcpOAuth": {f"server-of-{home.name}": {"accessToken": f"mcp-{home.name}"}}, **(own or {})}
    if who:
        config["oauthAccount"] = oauth_account(who)
        creds["claudeAiOauth"] = {
            "accessToken": token(who, "at", n),
            "refreshToken": token(who, "rt", n),
            "expiresAt": int(time.time() * 1000 + expires_in),
            "scopes": ["user:inference", "user:profile", "user:sessions:claude_code"],
            "subscriptionType": "max",
            "rateLimitTier": TIER,
        }
        creds["organizationUuid"] = ORG[who["uuid"]]
    (home / ".claude.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    (home / ".credentials.json").write_text(json.dumps(creds, separators=(",", ":")), encoding="utf-8")


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def snapshot(root: Path) -> dict[str, str]:
    """Every file under ``root``, for "nothing changed" checks."""
    return {str(p.relative_to(root)): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*")) if p.is_file()}


def logins(root: Path) -> Counter:
    """Refresh tokens on disk: each login must be in exactly one home."""
    out: Counter = Counter()
    for p in root.rglob(".credentials.json"):
        o = read(p).get("claudeAiOauth") or {}
        if o.get("refreshToken"):
            out[o["refreshToken"]] += 1
    return out


def lock_dirs(root: Path) -> list[str]:
    return [str(p) for p in root.rglob("*.lock") if p.is_dir()] + [str(p) for p in root.parent.glob("*.lock") if p.is_dir()]


@pytest.fixture
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, anthropic: FakeAnthropic) -> SimpleNamespace:  # noqa: F811
    for k in list(os.environ):
        if k.startswith("CREMIND_") or k == "CLAUDE_CONFIG_DIR":
            monkeypatch.delenv(k)
    root = tmp_path / "claude"
    main, accounts = root / "main", root / "accounts"
    monkeypatch.setenv("CLAUDE_USAGE_MONITOR_MAIN_HOME", str(main))
    monkeypatch.setenv("CLAUDE_USAGE_MONITOR_ACCOUNTS_DIR", str(accounts))
    monkeypatch.setattr(S, "LOCK_WAITS", {"refresh": (4, 0.05), "storage": (4, 0.05), "config": (4, 0.05)})
    paths = C.Paths(tmp_path / "data")
    for who in (A, B, C3):
        for n in (1, 2):
            anthropic.usage[token(who, "at", n)] = usage_body(20, 30, time.time() * 1000)
            anthropic.orgs[token(who, "at", n)] = ORG[who["uuid"]]
    return SimpleNamespace(root=root, main=main, accounts=accounts, paths=paths, anthropic=anthropic)


def register(paths: C.Paths, *names: str) -> None:
    P.write_registry(paths, [{"name": n, "addedAt": None} for n in names])


# ---------------------------------------------------------------- moving logins


def test_switch_moves_the_login_in_and_keeps_the_one_it_replaces(homes: SimpleNamespace) -> None:
    make_home(homes.main, A)
    make_home(homes.accounts / "spare", C3)
    register(homes.paths, "spare")
    before = logins(homes.root)

    r = S.switch_to(homes.paths, C3["uuid"])

    assert r["verified"] and r["reading"]["session"]["pct"] == 20  # checked with Anthropic first
    assert (r["source"], r["parkedIn"], r["createdProfile"], r["email"], r["fromEmail"]) == ("spare", "first-example-com", True, C3["email"], A["email"])
    # Your Claude Code: the account's login and identity, as /login writes them; its own
    # entries stay, the per-account caches are cleared.
    creds, config = read(homes.main / ".credentials.json"), read(homes.main / ".claude.json")
    assert creds["claudeAiOauth"]["refreshToken"] == token(C3, "rt") and creds["organizationUuid"] == "org-c"
    assert creds["mcpOAuth"] == {"server-of-main": {"accessToken": "mcp-main"}}
    assert config["oauthAccount"] == oauth_account(C3)
    assert config["userID"] == "device-of-main" and config["projects"] == {"C:/work/main": {"allowedTools": ["Bash"]}}
    assert not {"cachedUsageUtilization", "modelAccessCache", "clientDataCacheSlots"} & set(config)
    assert (homes.main / ".claude.json").read_text(encoding="utf-8").startswith('{\n  "')  # indented, as Claude Code writes it
    # The replaced login: a new profile named after its email, ready to switch back to.
    kept = homes.accounts / "first-example-com"
    assert read(kept / ".credentials.json")["claudeAiOauth"]["refreshToken"] == token(A, "rt")
    assert read(kept / ".claude.json")["oauthAccount"] == oauth_account(A)
    assert read(kept / ".claude.json")["hasCompletedOnboarding"] is True
    assert C.is_our_statusline(read(kept / "settings.json").get("statusLine"))
    # The profile it came from: signed out, but its own entries stay.
    spare_creds, spare_config = read(homes.accounts / "spare" / ".credentials.json"), read(homes.accounts / "spare" / ".claude.json")
    assert "claudeAiOauth" not in spare_creds and "organizationUuid" not in spare_creds and spare_creds["mcpOAuth"]
    assert "oauthAccount" not in spare_config and spare_config["userID"] == "device-of-spare"
    # Never copied, never lost; no lock left behind; the registry knows whose place is whose.
    assert logins(homes.root) == before and set(before.values()) == {1}
    assert lock_dirs(homes.root) == []
    registry = {e["name"]: e for e in P.read_registry(homes.paths)}
    assert registry["spare"]["account"] == C3["uuid"]
    assert registry["first-example-com"]["account"] == A["uuid"] and registry["first-example-com"]["createdBy"] == "switch"


def test_switching_back_returns_each_login_to_its_place(homes: SimpleNamespace) -> None:
    make_home(homes.main, A)
    make_home(homes.accounts / "spare", C3)
    register(homes.paths, "spare")
    S.switch_to(homes.paths, C3["uuid"])

    r = S.switch_to(homes.paths, A["uuid"])

    # A comes back from the profile kept for it; C3 returns to the place "spare" kept for it.
    assert (r["source"], r["parkedIn"], r["createdProfile"]) == ("first-example-com", "spare", False)
    assert read(homes.main / ".credentials.json")["claudeAiOauth"]["refreshToken"] == token(A, "rt")
    assert read(homes.accounts / "spare" / ".credentials.json")["claudeAiOauth"]["refreshToken"] == token(C3, "rt")
    assert "claudeAiOauth" not in read(homes.accounts / "first-example-com" / ".credentials.json")
    assert set(logins(homes.root).values()) == {1}
    registry = {e["name"]: e for e in P.read_registry(homes.paths)}
    assert registry["first-example-com"]["account"] == A["uuid"] and registry["spare"]["account"] == C3["uuid"]
    with pytest.raises(S.SwitchError) as e:
        S.switch_to(homes.paths, A["uuid"])
    assert e.value.reason == "already"


def test_an_account_signed_in_twice_keeps_its_other_login(homes: SimpleNamespace) -> None:
    make_home(homes.main, A)
    make_home(homes.accounts / "a-too", A, n=2)  # a second, independent login of A
    make_home(homes.accounts / "spare", C3)
    register(homes.paths, "a-too", "spare")

    r = S.switch_to(homes.paths, C3["uuid"])

    # As /login would, the main home's login of A is let go: A stays signed in through a-too.
    assert r["dropped"] and r["parkedIn"] is None and not (homes.accounts / "first-example-com").exists()
    assert read(homes.accounts / "a-too" / ".credentials.json")["claudeAiOauth"]["refreshToken"] == token(A, "rt", 2)
    assert read(homes.main / ".claude.json")["oauthAccount"]["accountUuid"] == C3["uuid"]


def test_a_signed_out_claude_code_just_takes_the_login(homes: SimpleNamespace) -> None:
    make_home(homes.main, None)
    make_home(homes.accounts / "spare", C3)
    register(homes.paths, "spare")
    r = S.switch_to(homes.paths, C3["uuid"])
    assert r["from"] is None and r["parkedIn"] is None
    assert read(homes.main / ".credentials.json")["claudeAiOauth"]["refreshToken"] == token(C3, "rt")


def test_an_expired_login_is_renewed_before_it_moves(homes: SimpleNamespace) -> None:
    make_home(homes.main, A)
    make_home(homes.accounts / "spare", C3, expires_in=-HOUR)
    register(homes.paths, "spare")
    fresh = {"access_token": token(C3, "at", 2), "refresh_token": token(C3, "rt", 2)}
    homes.anthropic.refresh[token(C3, "rt")] = fresh

    r = S.switch_to(homes.paths, C3["uuid"])

    assert r["verified"] and homes.anthropic.count("token") == 1
    moved = read(homes.main / ".credentials.json")["claudeAiOauth"]
    assert (moved["accessToken"], moved["refreshToken"]) == (fresh["access_token"], fresh["refresh_token"])
    assert moved["expiresAt"] > time.time() * 1000 + 7 * HOUR  # renewed for 8 hours


# ---------------------------------------------------------------- refusing, and changing nothing


@pytest.mark.parametrize(
    "case, reason",
    [
        ("unknown", "not_here"),
        ("in_use", "in_use"),
        ("refused", "signin"),
        ("other_org", "mismatch"),
        ("cremind_only", "cremind_only"),
        ("no_login", "no_login"),
    ],
)
def test_a_refused_switch_changes_nothing(homes: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, case: str, reason: str) -> None:
    make_home(homes.main, A)
    make_home(homes.accounts / "spare", C3)
    register(homes.paths, "spare")
    target = C3["uuid"]
    if case == "unknown":
        target = B["uuid"]
    elif case == "in_use":
        (homes.accounts / "spare" / "projects" / "p").mkdir(parents=True)
        _append(homes.accounts / "spare" / "projects" / "p" / "s.jsonl", _reply("x", 5))
    elif case == "refused":
        del homes.anthropic.usage[token(C3, "at")]  # 401, and no refresh accepted
    elif case == "other_org":
        homes.anthropic.orgs[token(C3, "at")] = "org-someone-else"
    elif case == "cremind_only":
        own = homes.root / "cremind-home"
        make_home(own, B)
        monkeypatch.setenv("CREMIND_PROFILE", "admin")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(own))
        target = B["uuid"]
    elif case == "no_login":
        (homes.accounts / "spare" / ".credentials.json").write_text("{}", encoding="utf-8")
    before = snapshot(homes.root)

    with pytest.raises(S.SwitchError) as e:
        S.switch_to(homes.paths, target)

    assert e.value.reason == reason and e.value.fix
    assert snapshot(homes.root) == before
    assert not (homes.accounts / "first-example-com").exists() and lock_dirs(homes.root) == []
    if case == "in_use":  # unless asked to anyway
        assert S.switch_to(homes.paths, target, force=True)["source"] == "spare"


def test_the_switch_waits_for_claude_codes_locks(homes: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    make_home(homes.main, A)
    make_home(homes.accounts / "spare", C3)
    register(homes.paths, "spare")
    held = homes.main / ".storage-write.lock"
    held.mkdir()  # Claude Code writing your login right now
    threading.Timer(0.12, held.rmdir).start()
    assert S.switch_to(homes.paths, C3["uuid"])["source"] == "spare"

    # A lock Claude Code keeps fresh, past every try: nothing changes.
    config_lock = homes.main / ".claude.json.lock"
    config_lock.mkdir()
    before = snapshot(homes.root)
    with pytest.raises(S.SwitchError) as e:
        S.switch_to(homes.paths, A["uuid"])
    assert e.value.reason == "busy" and snapshot(homes.root) == before
    config_lock.rmdir()
    assert lock_dirs(homes.root) == []


def test_a_failed_write_puts_everything_back(homes: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    make_home(homes.main, A)
    make_home(homes.accounts / "spare", C3)
    register(homes.paths, "spare")
    before = snapshot(homes.root)
    real = S._write_config

    def failing(path: Path, data: dict | None) -> None:
        if path == homes.main / ".claude.json" and data and data.get("oauthAccount", {}).get("accountUuid") == C3["uuid"]:
            raise PermissionError("held open by another process")
        real(path, data)

    monkeypatch.setattr(S, "_write_config", failing)
    with pytest.raises(S.SwitchError) as e:
        S.switch_to(homes.paths, C3["uuid"])
    assert e.value.reason == "write" and e.value.restored
    assert snapshot(homes.root) == before  # the logins written before it were put back
    assert not (homes.accounts / "first-example-com").exists() and lock_dirs(homes.root) == []


# ---------------------------------------------------------------- the monitor, the dashboard API and the CLI


def test_find_account_by_what_people_say(homes: SimpleNamespace) -> None:
    make_home(homes.main, A)
    make_home(homes.accounts / "spare", C3)
    register(homes.paths, "spare")
    m = Monitor(homes.paths, readonly=True)
    m.refresh_readonly()
    for ref, who in (("third@example.com", C3), ("THIRD", C3), ("spare", C3), ("main", A), (C3["uuid"][:8], C3), ("first", A)):
        assert m.find_account(ref)["id"] == who["uuid"], ref
    assert m.find_account("example") is None  # both match: ambiguous
    assert m.find_account("nobody") is None


def test_switch_through_the_dashboard_and_the_cli(tmp_path: Path) -> None:
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
        s = rig.until(lambda s: s["activeId"] == A["uuid"] and view(s, C3["uuid"])["switchable"] and not view(s, A["uuid"])["switchable"])

        # Cross-site pages can't switch, and an unknown account is a 404.
        assert rig.post("api/switch", {"account": "third"}, headers={"Origin": "http://evil.example"})[0] == 403
        assert rig.post("api/switch", {"account": "nobody"})[0] == 404

        code, out = rig.post("api/switch", {"account": "third"})
        assert code == 200 and out["ok"] and out["result"]["parkedIn"] == "first-example-com" and out["result"]["verified"]
        assert "sk-ant" not in json.dumps(out)
        s = rig.until(
            lambda s: s["activeId"] == C3["uuid"]
            and view(s, A["uuid"])["signedIn"] == ["first-example-com"]
            and view(s, A["uuid"])["switchable"]
            and any(p["name"] == "spare" and p["keeps"] == C3["email"] for p in s["profiles"])
        )
        texts = [e["text"] for e in s["events"]]
        assert any(t.startswith(f"Switched Claude Code to {C3['email']}") and "first-example-com" in t for t in texts)
        assert not any("is signed out" in t or "Now tracking profile" in t for t in texts)  # the switch's line says it all
        assert view(s, C3["uuid"])["session"]["pct"] == 10  # Anthropic's answer from the check

        code, out = rig.cli("switch", "first@example.com")
        assert code == 0 and out["switched"] and out["your_claude_code_now_uses"] == A["email"] and out["previous_login_kept_in"] == "profile spare"
        rig.until(lambda s: s["activeId"] == A["uuid"] and view(s, C3["uuid"])["signedIn"] == ["spare"])
        code, out = rig.cli("switch", "first")
        assert code == 0 and out["switched"] is False  # already

        code, status = rig.cli("status")
        row = next(a for a in status["accounts"] if a["account"] == C3["email"])
        assert row["switch_with"] == f"switch {C3['email']}" and not row["your_claude_code_uses_it"]
        assert "sk-ant" not in json.dumps(rig.get())
        assert read(rig.main / ".credentials.json")["claudeAiOauth"]["refreshToken"] == token(A, "rt")
        assert read(rig.spare / ".credentials.json")["claudeAiOauth"]["refreshToken"] == token(C3, "rt")
    finally:
        rig.stop(graceful=False)
        fake.close()


def test_switch_while_the_monitor_is_stopped(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    make_home(rig.main, A)
    make_home(rig.spare, C3)
    (rig.data / "profiles.json").write_text(json.dumps({"profiles": [{"name": "spare"}]}), encoding="utf-8")
    code, out = rig.cli("switch", "spare")  # by the profile its login is in; Anthropic unreachable here
    assert code == 0 and out["switched"] and out["checked_with_anthropic"] is False and out["previous_login_kept_in"] == "profile first-example-com (new)"
    assert read(rig.main / ".claude.json")["oauthAccount"]["accountUuid"] == C3["uuid"]
    code, out = rig.cli("accounts")
    rows = {r["profile"]: r for r in out["profiles"]}
    assert rows["first-example-com"]["switch_with"] == f"switch {A['email']}"
    assert C3["email"] in rows["spare"]["keeps_place_of"]
    code, out = rig.cli("switch", "nobody")
    assert code == 1 and "No account matches" in out["error"]
