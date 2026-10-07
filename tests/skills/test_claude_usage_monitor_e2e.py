"""End-to-end test of the claude-usage-monitor skill's listener, dashboard API and CLI.

Ported from the standalone app's end-to-end run (test/run.js), plus what the Cremind
skill adds: alerts written as Cremind event files, the CLI the agent drives, and the
listener contract (single instance, heartbeat, runtime file).

The skill folder is copied to a temp dir and run from there — boot sync copies whatever
sits in the shipped folder into every profile, so a test must never write into it.
Claude Code's homes are fake folders, pointed at with the skill's override variables.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import pytest

from app.skills.sync import BUILTIN_SKILLS_DIR

SKILL_SRC = BUILTIN_SKILLS_DIR / "claude-usage-monitor"
# The scripts need only the standard library, as under `uv run`. The base interpreter also
# keeps a Windows venv's launcher out of the way: it would run the monitor as a child
# process with another pid, and killing the launcher would not be a crash of the monitor.
PY = getattr(sys, "_base_executable", None) or sys.executable
if not Path(PY).exists():
    PY = sys.executable
MIN = 60_000
HOUR = 60 * MIN
TIER = "default_claude_max_20x"
A = {"uuid": "11111111-1111-1111-1111-111111111111", "email": "first@example.com"}
B = {"uuid": "22222222-2222-2222-2222-222222222222", "email": "second@example.com"}
C3 = {"uuid": "33333333-3333-3333-3333-333333333333", "email": "third@example.com"}
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _iso(ms: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000)) + f".{int(ms % 1000):03d}Z"


def _cache(uuid: str, session: float, weekly: float, at: float, now: float, weekly_reset: float | None = None) -> dict:
    return {
        "fetchedAtMs": at,
        "accountUuid": uuid,
        "utilization": {
            "five_hour": {"utilization": session, "resets_at": _iso(now + 2 * HOUR)},
            "seven_day": {"utilization": weekly, "resets_at": _iso(weekly_reset or now + 3 * 24 * HOUR)},
        },
    }


def _reply(rid: str, out_tokens: int) -> str:
    line = {
        "type": "assistant",
        "timestamp": _iso(time.time() * 1000),
        "requestId": f"req_{rid}",
        "message": {"id": f"msg_{rid}", "model": "claude-opus-5-5", "usage": {"output_tokens": out_tokens}},
    }
    return json.dumps(line) + "\n"


def _append(path: Path, text: str) -> None:
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(text)


class Rig:
    """A copy of the skill, fake Claude Code homes, and the listener running against them."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.skill = root / "skill"
        shutil.copytree(SKILL_SRC, self.skill, ignore=shutil.ignore_patterns("__pycache__", ".monitor", ".env", ".listener*"))
        self.scripts = self.skill / "scripts"
        self.main = root / "main"
        self.accounts = root / "accounts"
        self.spare = self.accounts / "spare"
        self.data = self.scripts / ".monitor"
        self.port = _free_port()
        (self.scripts / ".env").write_text(f"DASHBOARD_PORT={self.port}\n", encoding="utf-8")
        for d in (self.main / "projects" / "proj", self.spare / "projects" / "proj", self.data / "inbox"):
            d.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CREMIND_", "CLAUDE_"))}
        env.update(
            {
                "CLAUDE_USAGE_MONITOR_MAIN_HOME": str(self.main),
                "CLAUDE_USAGE_MONITOR_ACCOUNTS_DIR": str(self.accounts),
                "CLAUDE_USAGE_MONITOR_RESCAN_MS": "1000",
                "CLAUDE_USAGE_MONITOR_TICK_MS": "250",
                # Never the real Anthropic: a closed loopback port, unless a test brings a fake.
                "CLAUDE_USAGE_MONITOR_API_URL": "http://127.0.0.1:9",
                "CLAUDE_USAGE_MONITOR_TOKEN_URL": "http://127.0.0.1:9/v1/oauth/token",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
            }
        )
        env.pop("PORT", None)  # Cremind's own port must never be mistaken for the dashboard's
        self.env = env
        self.proc: subprocess.Popen | None = None
        self.log = ""

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self) -> subprocess.Popen:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            [PY, str(self.scripts / "event_listener.py")],
            cwd=str(self.scripts),
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        return self.proc

    def stop(self, graceful: bool = True) -> str:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return ""
        if graceful:
            proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
        else:
            proc.kill()
        try:
            out, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        text = out.decode("utf-8", errors="replace")
        self.log += text
        return text

    def get(self, path: str = "api/state", host: str | None = None) -> Any:
        req = urllib.request.Request(self.url + path, headers={"Host": host} if host else {})
        with _NO_PROXY.open(req, timeout=5) as resp:
            return json.loads(resp.read())

    def post(self, path: str, body: Any, method: str = "POST", headers: dict | None = None) -> tuple[int, Any]:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method=method, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with _NO_PROXY.open(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def until(self, cond: Callable[[dict], bool], timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        last: dict | None = None
        while time.monotonic() < deadline:
            time.sleep(0.2)
            try:
                last = self.get()
            except OSError:
                continue
            try:
                if cond(last):
                    return last
            except (KeyError, TypeError, StopIteration):
                pass
        raise AssertionError(f"condition not met in {timeout}s; last events: {[e['text'] for e in (last or {}).get('events', [])]}")

    def cli(self, *args: str) -> tuple[int, dict]:
        out = subprocess.run([PY, str(self.scripts / "__main__.py"), *args], cwd=str(self.scripts), env=self.env, capture_output=True, timeout=60)
        return out.returncode, json.loads(out.stdout.decode("utf-8"))

    def events(self, event_type: str) -> list[str]:
        folder = self.skill / "events" / event_type
        return sorted(p.read_text(encoding="utf-8") for p in folder.glob("*.md"))


def view(state: dict, uuid: str) -> dict:
    return next(a for a in state["accounts"] if a["id"] == uuid)


def frontmatter(text: str) -> dict:
    block = text.split("---\n")[1]
    return {k: json.loads(v) for k, v in (line.split(": ", 1) for line in block.strip().splitlines())}


@pytest.fixture
def rig(tmp_path: Path):
    r = Rig(tmp_path)
    yield r
    r.stop(graceful=False)


def test_listener_end_to_end(rig: Rig) -> None:
    now = time.time() * 1000
    main_config = rig.main / ".claude.json"
    spare_config = rig.spare / ".claude.json"
    # Main profile: account A, 40% according to Claude Code's cache.
    main_config.write_text(json.dumps({"oauthAccount": {"accountUuid": A["uuid"], "emailAddress": A["email"], "organizationRateLimitTier": TIER}, "cachedUsageUtilization": _cache(A["uuid"], 40, 55, now - 30e3, now)}), encoding="utf-8")
    # Spare profile: account C, 20% of its 5-hour window but 93% of its week.
    spare_config.write_text(json.dumps({"oauthAccount": {"accountUuid": C3["uuid"], "emailAddress": C3["email"], "organizationRateLimitTier": TIER}, "cachedUsageUtilization": _cache(C3["uuid"], 20, 93, now - 60e3, now)}), encoding="utf-8")
    (rig.data / "profiles.json").write_text(json.dumps({"profiles": [{"name": "spare", "dir": "ignored: folders derive from the name"}]}), encoding="utf-8")
    # Account B reported through a terminal status line a minute ago.
    (rig.data / "inbox" / "session-b.json").write_text(
        json.dumps(
            {
                "v": 1,
                "at": now - 60e3,
                "sessionId": "session-b",
                "account": {"id": B["uuid"], "email": B["email"], "tier": TIER},
                "rateLimits": {"five_hour": {"used_percentage": 8, "resets_at": round((now + 4 * HOUR) / 1000)}, "seven_day": {"used_percentage": 12, "resets_at": round((now + 3 * 24 * HOUR) / 1000)}},
            }
        ),
        encoding="utf-8",
    )
    main_transcript = rig.main / "projects" / "proj" / "session.jsonl"
    spare_transcript = rig.spare / "projects" / "proj" / "session.jsonl"
    main_transcript.write_text("", encoding="utf-8")
    spare_transcript.write_text("", encoding="utf-8")

    rig.start()
    s = rig.until(lambda x: x["ready"] and len(x["accounts"]) >= 3)

    # The listener contract: runtime file with the dashboard URL, heartbeat, lock.
    runtime = json.loads((rig.data / "runtime.json").read_text(encoding="utf-8"))
    assert runtime["url"] == rig.url and runtime["port"] == rig.port
    assert (rig.scripts / ".listener_heartbeat").exists()
    second = subprocess.run([PY, str(rig.scripts / "event_listener.py")], cwd=str(rig.scripts), env=rig.env, capture_output=True, timeout=30)
    assert second.returncode == 1
    assert "is already running for this skill" in second.stderr.decode("utf-8", errors="replace")

    # Accounts from the main profile, an extra profile and the status line.
    assert len(s["accounts"]) == 3
    assert view(s, A["uuid"])["signedIn"] == ["main"]
    assert view(s, C3["uuid"])["signedIn"] == ["spare"]
    assert view(s, B["uuid"])["signedIn"] == []
    assert round(view(s, C3["uuid"])["session"]["pct"]) == 20
    assert view(s, B["uuid"])["source"] == "statusline"
    assert [p["name"] for p in s["profiles"]] == ["main", "spare"]
    assert s["profiles"][1]["dir"] == str(rig.spare)
    assert s["heroId"] == A["uuid"]

    # A VS Code session on the main profile spends $296.40 at list price: +52% at $5.70 per 1%.
    _append(main_transcript, _reply("1", 14_820_000))
    s = rig.until(lambda x: view(x, A["uuid"])["session"]["pct"] > 90)
    a = view(s, A["uuid"])
    assert abs(a["session"]["pct"] - 92) < 0.01
    assert a["session"]["estimated"] is True and a["session"]["official"] == 40
    assert a["running"] is True
    assert a["rate"]["session"]["perHour"] > 0
    assert abs(a["rate"]["weekly"]["perHour"] - a["rate"]["session"]["perHour"] * 5.7 / 31) < 1e-6
    assert a["status"]["level"] == "critical"
    assert round(view(s, C3["uuid"])["session"]["pct"]) == 20, "spend on the main profile is not credited to other accounts"

    # The switch alert names the account with the most headroom, in the log and as an event.
    s = rig.until(lambda x: any(e["kind"] == "alert" and "Switch now: first@example.com at ≈92%" in e["text"] for e in x["events"]))
    assert s["recommendation"]["id"] == B["uuid"]
    assert len(view(s, A["uuid"])["series"]) >= 2 and abs(view(s, A["uuid"])["series"][-1][1] - 92) < 0.01
    switch = [frontmatter(t) | {"_body": t} for t in rig.events("switch_now")]
    first = next(e for e in switch if e["account"] == A["email"])
    assert first["alert"] == "switch_now" and first["level"] == "critical" and first["limit"] == "5-hour"
    assert first["used_pct"] == 92 and first["estimated"] is True
    assert first["switch_to"] == B["email"] and first["dashboard"] == rig.url
    assert first["event_type"] == "switch_now" and re.match(r"\d{4}-\d\d-\d\dT", first["received_at"])
    assert "Switch to second@example.com" in first["_body"] and rig.url in first["_body"]
    assert "secret" not in first["_body"]

    # Work on the spare profile spends $93: +16% of C's 5-hour window but +3% of its week,
    # which takes the week to 96% — past the weekly switch-now while 5 hours has plenty left.
    _append(spare_transcript, _reply("c1", 4_650_000))
    s = rig.until(lambda x: view(x, C3["uuid"])["weekly"]["pct"] > 95.5 and sum(e["kind"] == "alert" for e in x["events"]) >= 3)
    c = view(s, C3["uuid"])
    assert abs(c["weekly"]["pct"] - 96) < 0.01 and abs(c["session"]["pct"] - 36.32) < 0.01
    assert c["running"] is True
    assert c["forecast"]["first"]["kind"] == "weekly"
    texts = [e["text"] for e in s["events"] if e["kind"] == "alert"]
    assert any(t.startswith("Switch now: third@example.com at ≈96% of its weekly limit") for t in texts), texts
    assert any(re.match(r"^third@example\.com hits its weekly limit in ~\d+ min", t) for t in texts), texts
    assert not any(re.search(r"third@example\.com.*5-hour", t) for t in texts), "no 5-hour alert at 36%"
    soon = [frontmatter(t) for t in rig.events("limit_soon")]
    assert any(e["account"] == C3["email"] and e["limit"] == "weekly" and e["minutes_left"] >= 1 for e in soon), soon

    # Then it hits its weekly limit.
    week_end = round((now + 2 * 24 * HOUR) / 1000)

    def weekly_hit() -> str:
        return json.dumps({"type": "assistant", "timestamp": _iso(time.time() * 1000), "quotaLimits": {"status": "rejected", "rateLimitType": "seven_day", "resetsAt": week_end}, "message": {"model": "<synthetic>"}}) + "\n"

    _append(spare_transcript, weekly_hit())
    s = rig.until(lambda x: view(x, C3["uuid"])["weekly"]["pct"] >= 100 and any(e["text"].startswith("third@example.com hit its weekly limit") for e in x["events"]))
    c = view(s, C3["uuid"])
    assert c["status"]["label"] == "Weekly limit reached" and c["source"] == "limit"
    reached = [frontmatter(t) for t in rig.events("limit_reached")]
    assert [e["account"] for e in reached] == [C3["email"]] and reached[0]["used_pct"] == 100

    # Claude resets C's weekly limit early: the same reset time, but usage is back to 3%.
    cfg = json.loads(spare_config.read_text(encoding="utf-8"))
    cfg["cachedUsageUtilization"] = _cache(C3["uuid"], 37, 3, time.time() * 1000, now, weekly_reset=week_end * 1000)
    spare_config.write_text(json.dumps(cfg), encoding="utf-8")
    s = rig.until(lambda x: view(x, C3["uuid"])["weekly"]["pct"] < 10)
    assert view(s, C3["uuid"])["weekly"]["official"] == 3
    assert any(re.match(r"^third@example\.com's limit seems to have been reset early: Claude Code now reports weekly usage at 3% \(was 100% at ", e["text"]) for e in s["events"])

    # Used up again in the same window: the limit alert fires a second time.
    _append(spare_transcript, weekly_hit())
    rig.until(lambda x: sum(e["text"].startswith("third@example.com hit its weekly limit") for e in x["events"]) >= 2)
    assert len(rig.events("limit_reached")) == 2

    # The summary file the status line reads.
    summary = json.loads((rig.data / "summary.json").read_text(encoding="utf-8"))
    assert summary["next"]["id"] == B["uuid"]

    # No credential material or conversation content is exposed.
    text = json.dumps(rig.get())
    for bad in ("sk-ant", "accessToken", "refreshToken", "credentials"):
        assert bad not in text

    # Settings round-trip and are clamped.
    code, out = rig.post("api/settings", {"warnPct": 70, "critPct": 50, "etaAlertMin": 999, "weeklyWarnPct": 85, "weeklyCritPct": 80})
    assert code == 200
    assert [out["settings"][k] for k in ("warnPct", "critPct", "etaAlertMin", "weeklyWarnPct", "weeklyCritPct")] == [70, 70, 120, 85, 85]

    # Renaming an account sticks.
    assert rig.post(f"api/accounts/{B['uuid']}", {"label": "spare-b"})[0] == 200
    assert view(rig.get(), B["uuid"])["displayName"] == "spare-b"

    # Foreign hosts and cross-site writes are refused.
    with pytest.raises(urllib.error.HTTPError) as refused:
        rig.get(host="evil.example")
    assert refused.value.code == 403
    assert rig.get(host=f"localhost:{rig.port}")["ready"] is True
    assert rig.post("api/settings", {}, headers={"Origin": "http://evil.example"})[0] == 403
    assert rig.post("api/profiles/nope/open", {})[0] == 404

    # The test alert goes to Cremind events.
    code, out = rig.post("api/test-alert", {"type": "limit_warning"})
    assert code == 200 and out["ok"] is True
    tests = [frontmatter(t) for t in rig.events("limit_warning") if '"test"' in t]
    assert tests and tests[0]["test"] is True and tests[0]["alert"] == "test"
    assert rig.post("api/test-alert", {"type": "nope"})[0] == 400

    # Switching the main profile to another account is noticed, and new spend follows it.
    cfg = json.loads(main_config.read_text(encoding="utf-8"))
    cfg["oauthAccount"] = {"accountUuid": B["uuid"], "emailAddress": B["email"], "organizationRateLimitTier": TIER}
    del cfg["cachedUsageUtilization"]
    main_config.write_text(json.dumps(cfg), encoding="utf-8")
    s = rig.until(lambda x: x["activeId"] == B["uuid"])
    assert any(e["kind"] == "switch" and "switched to spare-b (from first@example.com at 5-hour ≈92%, weekly ≈65%)" in e["text"] for e in s["events"]), [e["text"] for e in s["events"]]
    _append(main_transcript, _reply("2", 2_850_000))  # $57 = 10%
    s = rig.until(lambda x: view(x, B["uuid"])["session"]["pct"] > 15)
    assert abs(view(s, B["uuid"])["session"]["pct"] - 18) < 0.01
    assert abs(view(s, A["uuid"])["session"]["pct"] - 92) < 0.01

    # The CLI the agent drives.
    code, status = rig.cli("status")
    assert code == 0 and status["monitor_running"] is True and status["dashboard"] == rig.url
    assert status["running_account"]["account"] == "spare-b"
    assert {x["account"] for x in status["accounts"]} == {A["email"], "spare-b", C3["email"]}
    code, dash = rig.cli("dashboard", "--no-start")
    assert code == 0 and dash["url"] == rig.url and dash["link"] == f"[Claude Usage Monitor]({rig.url})"
    code, settings = rig.cli("settings", "--eta-minutes", "20", "--alerts", "on")
    assert code == 0 and settings["settings"]["etaAlertMin"] == 20
    assert rig.get()["settings"]["etaAlertMin"] == 20, "the running monitor applied it"
    code, accounts = rig.cli("accounts")
    assert code == 0 and [p["profile"] for p in accounts["profiles"]] == ["main", "spare"]
    code, added = rig.cli("add-account", "spare2", "--no-open")
    assert code == 0 and added["created"] == "spare2" and added["opened_claude_code"] is False
    assert json.loads((rig.accounts / "spare2" / "settings.json").read_text(encoding="utf-8"))["statusLine"]["command"].endswith('statusline.py"')
    s = rig.until(lambda x: [p["name"] for p in x["profiles"]] == ["main", "spare", "spare2"])
    assert rig.cli("add-account", "main")[0] == 1, "reserved name"
    assert rig.cli("add-account", "../evil")[0] == 1, "names can't escape the accounts folder"
    code, removed = rig.cli("remove-account", "spare2")
    assert code == 0 and removed["removed"] == "spare2" and (rig.accounts / "spare2").exists()

    # Spend and estimates survive a crash and restart without double counting.
    crashed_pid = rig.get("api/health")["pid"]
    assert crashed_pid == rig.proc.pid
    rig.stop(graceful=False)
    rig.start()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if rig.get("api/health")["pid"] == rig.proc.pid:
                break
        except OSError:
            pass
        time.sleep(0.2)
    assert rig.get("api/health")["pid"] == rig.proc.pid != crashed_pid, "a new monitor answers"
    s = rig.until(lambda x: x["ready"] and len(x["accounts"]) >= 3)
    s = rig.until(lambda x: abs(view(x, B["uuid"])["session"]["pct"] - 18) < 0.01)
    assert abs(view(s, A["uuid"])["session"]["pct"] - 92) < 0.01
    assert view(s, B["uuid"])["displayName"] == "spare-b", "the custom name was persisted"
    assert len(rig.events("limit_reached")) == 2, "fired alerts are not raised again after a restart"

    # A graceful stop removes the runtime file; the CLI then reports the last known figures.
    rig.stop(graceful=True)
    assert not (rig.data / "runtime.json").exists()
    assert not (rig.scripts / ".listener_heartbeat").exists()
    code, status = rig.cli("status")
    assert code == 0 and status["monitor_running"] is False and "listener-start" in status["note"]
    code, dash = rig.cli("dashboard", "--no-start")
    assert code == 1 and "not running" in dash["error"] and dash["fix"].startswith("cremind skill-events listener-start")


def test_port_taken_moves_to_the_next(rig: Rig) -> None:
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", rig.port))
        blocker.listen()
        rig.start()
        deadline = time.monotonic() + 20
        runtime = None
        while time.monotonic() < deadline and not runtime:
            time.sleep(0.2)
            data = json.loads((rig.data / "runtime.json").read_text(encoding="utf-8")) if (rig.data / "runtime.json").exists() else None
            runtime = data if data and data.get("pid") == rig.proc.pid else None
        assert runtime, rig.stop()
        assert runtime["port"] != rig.port and rig.port < runtime["port"] <= rig.port + 10


def test_cremind_profile_home_is_tracked(rig: Rig, tmp_path: Path) -> None:
    """Under Cremind, the profile's own Claude Code home (CLAUDE_CONFIG_DIR) is a profile too."""
    own = tmp_path / "sys" / "alice" / "coding-cli" / "claude"
    own.mkdir(parents=True)
    (own / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": A["uuid"], "emailAddress": A["email"], "organizationRateLimitTier": TIER}}), encoding="utf-8")
    rig.env.update({"CREMIND_PROFILE": "alice", "CREMIND_SYSTEM_DIR": str(tmp_path / "sys"), "CLAUDE_CONFIG_DIR": str(own)})
    del rig.env["CLAUDE_USAGE_MONITOR_ACCOUNTS_DIR"]
    rig.start()
    s = rig.until(lambda x: x["ready"] and len(x["profiles"]) == 2)
    assert [(p["name"], p["kind"]) for p in s["profiles"]] == [("main", "main"), ("cremind", "cremind")]
    assert view(s, A["uuid"])["signedIn"] == ["cremind"]
    # Extra profiles of a Cremind profile live in its own coding-cli folder.
    code, added = rig.cli("add-account", "spare", "--no-open")
    assert code == 0 and Path(added["folder"]) == tmp_path / "sys" / "alice" / "coding-cli" / "claude-accounts" / "spare"


def test_a_monitor_started_by_hand_finds_the_profiles_cremind_made(rig: Rig, tmp_path: Path) -> None:
    """2026-10-07: the listener started by hand from a Cremind profile's copy of the skill — no
    CREMIND_* variables — looked for the extra profiles in ~/.claude-accounts, reported every
    one signed out and had no account to switch to. The copy's own place names its profile."""
    sysdir = tmp_path / "sys"
    home = sysdir / "alice" / "skills" / "claude-usage-monitor"
    home.parent.mkdir(parents=True)
    shutil.move(str(rig.skill), str(home))
    rig.skill, rig.scripts, rig.data = home, home / "scripts", home / "scripts" / ".monitor"
    (sysdir / "bootstrap.toml").write_text('db_provider = "sqlite"\n', encoding="utf-8")
    spare = sysdir / "alice" / "coding-cli" / "claude-accounts" / "spare"
    spare.mkdir(parents=True)
    (spare / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": C3["uuid"], "emailAddress": C3["email"], "organizationRateLimitTier": TIER}}), encoding="utf-8")
    (rig.data / "profiles.json").write_text(json.dumps({"profiles": [{"name": "spare"}]}), encoding="utf-8")
    del rig.env["CLAUDE_USAGE_MONITOR_ACCOUNTS_DIR"]
    rig.start()
    s = rig.until(lambda x: x["ready"] and view(x, C3["uuid"])["signedIn"] == ["spare"])
    assert Path(next(p for p in s["profiles"] if p["name"] == "spare")["dir"]).resolve() == spare.resolve()
    assert not any("signed out" in e["text"] for e in s["events"])


def test_turning_automatic_switching_on_names_the_events_that_report_it(rig: Rig) -> None:
    """In automatic mode the account in use raises no heads-up alerts: a conversation that only
    has the alert events subscribed hears nothing (2026-10-07), so the agent is told."""
    code, out = rig.cli("settings", "--switching", "auto")
    assert code == 0 and out["settings"]["switchMode"] == "auto"
    assert all(name in out["note"] for name in ("auto_switched", "no_account_available", "auto_switch_failed", "cremind skill-events list"))
    code, out = rig.cli("settings", "--auto-session-pct", "95")
    assert code == 0 and out["settings"]["autoSessionPct"] == 95 and "note" not in out


def test_a_page_that_leaves_before_its_answer_is_no_error(capsys: pytest.CaptureFixture[str]) -> None:
    """2026-10-07: the dashboard closed during a refresh, and the monitor's log on Cremind's
    Processes page showed a ConnectionAbortedError traceback (WinError 10053)."""
    sys.path.insert(0, str(SKILL_SRC / "scripts"))
    try:
        from usage_monitor.server import DashboardServer
    finally:
        sys.path.remove(str(SKILL_SRC / "scripts"))
    server = DashboardServer.__new__(DashboardServer)  # handle_error needs no socket
    for gone in (ConnectionAbortedError(10053, "aborted by the software in your host machine"), BrokenPipeError(), ConnectionResetError()):
        try:
            raise gone
        except ConnectionError:
            server.handle_error(None, ("127.0.0.1", 62265))
    assert capsys.readouterr().err == ""
    try:
        raise ValueError("a real fault")
    except ValueError:
        server.handle_error(None, ("127.0.0.1", 62265))
    assert "ValueError: a real fault" in capsys.readouterr().err


def test_event_writer_contract(tmp_path: Path) -> None:
    sys.path.insert(0, str(SKILL_SRC / "scripts"))
    try:
        from usage_monitor.events import EVENT_TYPES, write_event
    finally:
        sys.path.remove(str(SKILL_SRC / "scripts"))
    a = write_event("switch_now", 'Switch now: a/b:c "x"', {"account": "Lê Nguyễn", "used_pct": 91, "none": None}, "body", tmp_path)
    b = write_event("switch_now", 'Switch now: a/b:c "x"', {}, "body", tmp_path)
    assert a.parent == tmp_path / "switch_now" and a != b and b.name.endswith(" (2).md")
    assert not re.search(r'[<>:"/\\|?*]', a.name)
    text = a.read_bytes().decode("utf-8")
    fm = frontmatter(text)
    assert fm["account"] == "Lê Nguyễn" and fm["used_pct"] == 91 and "none" not in fm
    assert fm["event_type"] == "switch_now" and fm["received_at"]
    with pytest.raises(ValueError):
        write_event("not_declared", "x", {}, "", tmp_path)
    declared = re.findall(r"^      - name: (\w+)$", (SKILL_SRC / "SKILL.md").read_text(encoding="utf-8"), re.M)
    assert sorted(declared) == sorted(EVENT_TYPES)
    assert sorted(p.name for p in (SKILL_SRC / "events").iterdir()) == sorted(EVENT_TYPES)
