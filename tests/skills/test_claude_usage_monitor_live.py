"""Live figures in the claude-usage-monitor skill: every account's official usage from
Anthropic's usage API (what Claude Code's /usage and claude.ai show), and expired logins
renewed the way Claude Code renews them — its refresh lock, a re-read, a compare-and-swap.

Anthropic is a local fake here; the skill lets only loopback addresses override its URLs.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import pytest

from app.skills.sync import BUILTIN_SKILLS_DIR

SCRIPTS_DIR = BUILTIN_SKILLS_DIR / "claude-usage-monitor" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from usage_monitor import live as L  # noqa: E402
from usage_monitor import usage as U  # noqa: E402
from usage_monitor.profiles import ClaudeProfile  # noqa: E402

from tests.skills.test_claude_usage_monitor_e2e import A, C3, TIER, Rig, _append, _reply, view  # noqa: E402

MIN = 60_000
HOUR = 60 * MIN
TOKEN_A = "sk-ant-oat01-test-account-a"
TOKEN_C_OLD = "sk-ant-oat01-test-account-c-expired"
TOKEN_C = "sk-ant-oat01-test-account-c-renewed"
REFRESH_C = "sk-ant-ort01-test-account-c"
REFRESH_C2 = "sk-ant-ort01-test-account-c-next"


def _iso(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def usage_body(session: float, weekly: float, now: float, extra: dict | None = None) -> dict:
    """An answer shaped like Anthropic's (2026-10-07), trimmed of the always-null keys."""
    session_reset, weekly_reset = _iso(now + 2 * HOUR), _iso(now + 3 * 24 * HOUR)
    return {
        "five_hour": {"utilization": session, "resets_at": session_reset, "limit_dollars": None, "used_dollars": None},
        "seven_day": {"utilization": weekly, "resets_at": weekly_reset, "limit_dollars": None, "used_dollars": None},
        "seven_day_opus": None,
        "seven_day_sonnet": None,
        "extra_usage": extra or {"is_enabled": False, "monthly_limit": 2000, "used_credits": 0, "utilization": 0, "currency": "USD", "decimal_places": 2},
        "limits": [
            {"kind": "session", "group": "session", "percent": session, "severity": "normal", "resets_at": session_reset, "scope": None, "is_active": False},
            {"kind": "weekly_all", "group": "weekly", "percent": weekly, "severity": "normal", "resets_at": weekly_reset, "scope": None, "is_active": True},
            {
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": 4,
                "severity": "normal",
                "resets_at": weekly_reset,
                "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
                "is_active": False,
            },
        ],
        "seven_day_breakdown": {"rows": [{"key": "claude_code", "display_name": "Claude Code", "percent": 97}]},
    }


class FakeAnthropic:
    """Anthropic's usage API and OAuth token endpoint, as far as the skill uses them."""

    def __init__(self) -> None:
        self.usage: dict[str, dict] = {}  # access token -> usage answer
        self.orgs: dict[str, str] = {}  # access token -> its account's organization (a response header)
        self.refresh: dict[str, dict] = {}  # refresh token -> new tokens, used up once given
        self.calls: list[tuple[str, dict]] = []
        self.fail_usage: tuple[int, dict, dict] | None = None  # status, body, headers
        self.on_refresh: Callable[[str], None] | None = None  # runs before the token answer
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def count(self, kind: str) -> int:
        with self.lock:
            return sum(1 for c in self.calls if c[0] == kind)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

            def _send(self, code: int, body: Any, headers: dict | None = None) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/api/oauth/usage":
                    return self._send(404, {})
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                with fake.lock:
                    fake.calls.append(("usage", {"token": token, "beta": self.headers.get("anthropic-beta")}))
                    fail, body, org = fake.fail_usage, fake.usage.get(token), fake.orgs.get(token)
                if fail:
                    return self._send(*fail)
                if body is None:
                    return self._send(401, {"type": "error", "error": {"type": "authentication_error", "message": "Invalid bearer token"}})
                return self._send(200, body, {"anthropic-organization-id": org} if org else None)

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/v1/oauth/token":
                    return self._send(404, {})
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                with fake.lock:
                    fake.calls.append(("token", body))
                    hook = fake.on_refresh
                if hook:
                    hook(body.get("refresh_token"))
                with fake.lock:
                    new = fake.refresh.pop(body.get("refresh_token"), None)
                if body.get("grant_type") != "refresh_token" or body.get("client_id") != L.CLIENT_ID or new is None:
                    return self._send(400, {"error": "invalid_grant", "error_description": "Refresh token not found or invalid"})
                return self._send(200, {"token_type": "Bearer", "expires_in": 28800, "scope": body.get("scope", ""), **new})

        return Handler


@pytest.fixture
def anthropic(monkeypatch: pytest.MonkeyPatch):
    fake = FakeAnthropic()
    monkeypatch.setattr(L, "API_URL", fake.url)
    monkeypatch.setattr(L, "TOKEN_URL", fake.url + "/v1/oauth/token")
    yield fake
    fake.close()


def write_login(home: Path, access: str, refresh: str, expires_at: float) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / ".credentials.json"
    login = {"accessToken": access, "refreshToken": refresh, "expiresAt": int(expires_at), "scopes": ["user:inference", "user:profile"], "subscriptionType": "max", "rateLimitTier": TIER}
    path.write_text(json.dumps({"claudeAiOauth": login, "organizationUuid": "org-1"}, separators=(",", ":")), encoding="utf-8")
    return path


def make_profile(home: Path, name: str = "spare", account: dict = C3) -> ClaudeProfile:
    home.mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": account["uuid"], "emailAddress": account["email"]}}), encoding="utf-8")
    return ClaudeProfile(name, home, "main" if name == "main" else "extra", home / ".claude.json")


def stamp(profile: ClaudeProfile) -> int:
    return os.stat(profile.config_file).st_mtime_ns


class FakeMonitor:
    """The monitor as the poller sees it."""

    def __init__(self, targets: list[dict]) -> None:
        self.targets = targets
        self.readings: list[tuple[str, dict]] = []
        self.statuses: dict[str, dict] = {}
        self.events: list[str] = []

    def live_targets(self, now: float) -> list[dict]:
        return self.targets

    def apply_live(self, account_id: str, reading: dict) -> None:
        self.readings.append((account_id, reading))

    def note_live(self, statuses: dict[str, dict]) -> None:
        self.statuses = statuses

    def note_live_event(self, text: str, level: str = "info") -> None:
        self.events.append(text)

    def warn(self, where: str, e: Any) -> None:
        raise AssertionError(f"{where}: {e}")


def target(profile: ClaudeProfile, account: dict = C3, busy: bool = True) -> dict:
    return {"account": account["uuid"], "profiles": [(profile, stamp(profile))], "busy": busy, "watched": False}


# ---------------------------------------------- reading Anthropic's answer


def test_usage_answer_becomes_an_official_reading() -> None:
    now = time.time() * 1000
    u = U.from_usage_api(usage_body(25, 78, now), now)
    assert u["source"] == "api" and u["at"] == now
    assert u["session"]["pct"] == 25 and abs(u["session"]["resetsAt"] - (now + 2 * HOUR)) < 1000
    assert u["weekly"]["pct"] == 78
    assert u["scoped"] == [{"label": "Weekly · Fable", "pct": 4, "resetsAt": u["weekly"]["resetsAt"]}]
    assert u["spend"] is None, "extra usage turned off"

    paid = {"is_enabled": True, "monthly_limit": 2000, "used_credits": 350, "utilization": 17.5, "currency": "USD", "decimal_places": 2}
    spend = U.from_usage_api(usage_body(25, 78, now, extra=paid), now)["spend"]
    assert spend == {"pct": 17.5, "resetsAt": None, "usedUsd": 3.5, "limitUsd": 20.0, "period": "monthly"}


def test_usage_answer_without_a_window() -> None:
    now = time.time() * 1000
    u = U.from_usage_api({"five_hour": None, "seven_day": {"utilization": 3, "resets_at": _iso(now + HOUR)}}, now)
    assert u["session"] == {"pct": 0, "resetsAt": None}, "null: no usage in progress"
    assert U.from_usage_api({"seven_day": {"utilization": 3, "resets_at": None}}, now)["session"] is None, "absent: not known"
    assert U.from_usage_api({}, now) is None and U.from_usage_api("nope", now) is None


def test_a_fresh_live_reading_is_shown_as_it_is() -> None:
    now = 1_800_000_000_000
    reading = {"pct": 25, "resetsAt": now + 2 * HOUR, "at": now - 30_000}
    log = U.SpendLog([[now - 20_000, 57.0]])  # +10% at $5.70 per 1%
    exact = U.estimate_window(reading, log, 5.7, "session", now, exact_ms=L.FRESH_MS)
    assert exact["pct"] == 25 and exact["estimated"] is False and exact["windowStart"] == now - 3 * HOUR
    rolled = U.estimate_window(reading, log, 5.7, "session", now)
    assert abs(rolled["pct"] - 35) < 1e-9 and rolled["estimated"] is True
    stale = U.estimate_window({**reading, "at": now - L.FRESH_MS - 1}, log, 5.7, "session", now, exact_ms=L.FRESH_MS)
    assert stale["estimated"] is True, "an old reading is rolled forward as before"
    over = U.estimate_window({**reading, "resetsAt": now - 1}, log, 5.7, "session", now, exact_ms=L.FRESH_MS)
    assert over["rolledOver"] is True, "a window that ended is over, however recent the reading"


def test_chart_line_through_official_readings() -> None:
    start = 1_800_000_000_000
    now = start + 2 * HOUR
    series = U.official_series([[start + 30 * MIN, 10], [start + HOUR, 20]], start, now, 31)
    assert series[0] == [start, 0]
    assert dict((t, p) for t, p in series)[start + 46 * MIN] == pytest.approx(10 + 10 * 16 / 30)
    assert series[-1] == [now, 31]


# ---------------------------------------------- talking to Anthropic


def test_fetch_usage_and_its_errors(anthropic: FakeAnthropic) -> None:
    now = time.time() * 1000
    anthropic.usage[TOKEN_A] = usage_body(25, 78, now)
    anthropic.orgs[TOKEN_A] = "org-a"
    body, org = L.fetch_usage(TOKEN_A)
    assert body["five_hour"]["utilization"] == 25 and org == "org-a"
    assert anthropic.calls[-1][1] == {"token": TOKEN_A, "beta": "oauth-2025-04-20"}

    with pytest.raises(L.LiveError) as e:
        L.fetch_usage("sk-ant-oat01-unknown")
    assert e.value.kind == "auth" and "Invalid bearer token" in str(e.value) and "sk-ant" not in str(e.value)

    anthropic.fail_usage = (429, {"type": "error", "error": {"type": "rate_limit_error", "message": "Slow down"}}, {"Retry-After": "120"})
    with pytest.raises(L.LiveError) as e:
        L.fetch_usage(TOKEN_A)
    assert e.value.kind == "slow" and e.value.retry_after_ms == 120_000


def test_only_loopback_may_replace_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_URL", "https://evil.example/api")
    assert L._loopback_override("X_URL", "https://api.anthropic.com") == "https://api.anthropic.com"
    monkeypatch.setenv("X_URL", "http://127.0.0.1:5555/")
    assert L._loopback_override("X_URL", "https://api.anthropic.com") == "http://127.0.0.1:5555"


# ---------------------------------------------- renewing an expired login


def test_renewal_follows_claude_codes_protocol(anthropic: FakeAnthropic, tmp_path: Path) -> None:
    now = time.time() * 1000
    profile = make_profile(tmp_path / "spare")
    path = write_login(profile.dir, TOKEN_C_OLD, REFRESH_C, now - HOUR)
    anthropic.refresh[REFRESH_C] = {"access_token": TOKEN_C, "refresh_token": REFRESH_C2}
    store = L.LoginStore(profile)
    login, kind = store.read()
    assert kind == "file" and not login.usable(now) and "sk-ant" not in repr(login)

    outcome, fresh = L.renew(store, login)
    assert outcome == "renewed" and fresh.access_token == TOKEN_C and fresh.usable(now)
    token_call = next(c[1] for c in anthropic.calls if c[0] == "token")
    assert token_call == {"grant_type": "refresh_token", "refresh_token": REFRESH_C, "client_id": L.CLIENT_ID, "scope": "user:inference user:profile"}
    raw = path.read_text(encoding="utf-8")
    saved = json.loads(raw)
    assert "\n" not in raw, "compact, as Claude Code writes it"
    assert saved["organizationUuid"] == "org-1", "the rest of the file is kept"
    oauth = saved["claudeAiOauth"]
    assert oauth["accessToken"] == TOKEN_C and oauth["refreshToken"] == REFRESH_C2
    assert abs(oauth["expiresAt"] - (now + 8 * HOUR)) < 60_000
    assert oauth["subscriptionType"] == "max" and oauth["rateLimitTier"] == TIER
    assert not (profile.dir / ".oauth_refresh.lock").exists() and not Path(str(profile.dir) + ".lock").exists(), "locks released"
    if os.name != "nt":
        assert (path.stat().st_mode & 0o777) == 0o600


def test_renewal_keeps_a_login_saved_meanwhile(anthropic: FakeAnthropic, tmp_path: Path) -> None:
    """Someone signs in again while the request is out: their login wins (compare-and-swap)."""
    now = time.time() * 1000
    profile = make_profile(tmp_path / "spare")
    write_login(profile.dir, TOKEN_C_OLD, REFRESH_C, now - HOUR)
    anthropic.refresh[REFRESH_C] = {"access_token": TOKEN_C, "refresh_token": REFRESH_C2}
    anthropic.on_refresh = lambda _old: write_login(profile.dir, "sk-ant-oat01-signed-in-again", "sk-ant-ort01-signed-in-again", now + 8 * HOUR)
    store = L.LoginStore(profile)
    outcome, login = L.renew(store, store.read()[0])
    assert outcome == "adopted" and login.access_token == "sk-ant-oat01-signed-in-again"
    assert json.loads((profile.dir / ".credentials.json").read_text(encoding="utf-8"))["claudeAiOauth"]["refreshToken"] == "sk-ant-ort01-signed-in-again"


def test_renewal_waits_for_claude_codes_lock(anthropic: FakeAnthropic, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(L.time, "sleep", lambda s: None)
    now = time.time() * 1000
    profile = make_profile(tmp_path / "spare")
    write_login(profile.dir, TOKEN_C_OLD, REFRESH_C, now - HOUR)
    anthropic.refresh[REFRESH_C] = {"access_token": TOKEN_C, "refresh_token": REFRESH_C2}
    held = profile.dir / ".oauth_refresh.lock"
    held.mkdir()  # Claude Code is renewing it right now
    store = L.LoginStore(profile)
    assert L.renew(store, store.read()[0]) == ("busy", None)
    assert anthropic.count("token") == 0 and held.exists(), "its lock is left alone"

    # Claude Code renewed it meanwhile: the lock's new holder finds a valid token and uses it.
    held.rmdir()
    write_login(profile.dir, TOKEN_C, REFRESH_C2, now + 8 * HOUR)
    assert L.renew(store, L.Login(TOKEN_C_OLD, REFRESH_C, now - HOUR, ()))[0] == "adopted"
    assert anthropic.count("token") == 0

    # A lock nobody has touched for over a minute is abandoned and taken over, like proper-lockfile does.
    write_login(profile.dir, TOKEN_C_OLD, REFRESH_C, now - HOUR)
    held.mkdir()
    old = time.time() - 120
    os.utime(held, (old, old))
    assert L.renew(store, store.read()[0])[0] == "renewed"
    assert not held.exists()


def test_the_lock_is_kept_fresh_while_held(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(L, "LOCK_TOUCH_S", 0.05)
    home = tmp_path / "home"
    home.mkdir()
    lock = L.RefreshLock(home)
    assert lock.acquire(attempts=1)
    assert not L.RefreshLock(home).acquire(attempts=1), "one holder at a time"
    old = time.time() - 120
    for d in lock.dirs:
        os.utime(d, (old, old))
    time.sleep(0.3)
    assert all(time.time() - os.stat(d).st_mtime < 5 for d in lock.dirs), "touched, so nobody takes it over"
    lock.release()
    assert not any(d.exists() for d in lock.dirs)


def test_a_refused_login_needs_sign_in(anthropic: FakeAnthropic, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(L, "MIN_GAP_MS", 0)
    now = time.time() * 1000
    profile = make_profile(tmp_path / "spare")
    path = write_login(profile.dir, TOKEN_C_OLD, "sk-ant-ort01-revoked", now - HOUR)
    before = path.read_bytes()
    monitor = FakeMonitor([target(profile)])
    poller = L.LivePoller(monitor)
    poller.round(L.C.now_ms())
    st = monitor.statuses[C3["uuid"]]
    assert st["state"] == "signin" and "/login" in st["detail"] and "spare" in st["detail"]
    assert path.read_bytes() == before, "the login file is left as it was"
    assert any("no longer accepts the login of profile spare" in e for e in monitor.events)

    # Not asked again while the login is unchanged, even when forced past the re-check time.
    poller.status[C3["uuid"]].wait_until = 0
    poller._forced_at = L.C.now_ms() + 1
    poller.round(L.C.now_ms())
    assert anthropic.count("token") == 1 and monitor.statuses[C3["uuid"]]["state"] == "signin"

    # Signed in again: figures resume.
    write_login(profile.dir, TOKEN_C, REFRESH_C2, now + 8 * HOUR)
    anthropic.usage[TOKEN_C] = usage_body(12, 33, now)
    poller.status[C3["uuid"]].wait_until = 0
    poller.round(L.C.now_ms())
    assert monitor.statuses[C3["uuid"]]["state"] == "ok" and monitor.readings[-1][1]["session"]["pct"] == 12


# ---------------------------------------------- the poller


def test_poller_asks_on_schedule(anthropic: FakeAnthropic, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = time.time() * 1000
    profile = make_profile(tmp_path / "main", "main", A)
    write_login(profile.dir, TOKEN_A, "sk-ant-ort01-a", now + 4 * HOUR)
    anthropic.usage[TOKEN_A] = usage_body(25, 78, now)
    monitor = FakeMonitor([target(profile, A)])
    poller = L.LivePoller(monitor)
    poller.round(L.C.now_ms())
    (account_id, reading), = monitor.readings
    assert account_id == A["uuid"] and reading["source"] == "api" and reading["session"]["pct"] == 25 and reading["weekly"]["pct"] == 78
    assert monitor.statuses[A["uuid"]]["state"] == "ok" and monitor.statuses[A["uuid"]]["profile"] == "main"

    poller.round(L.C.now_ms())
    assert anthropic.count("usage") == 1, "not again before the interval"
    monkeypatch.setattr(L, "MIN_GAP_MS", 0)
    poller._forced_at = L.C.now_ms() + 1
    poller.round(L.C.now_ms() + 2)
    assert anthropic.count("usage") == 2, "an on-demand refresh asks at once"

    st = L._Status(checked_at=1_000_000)
    busy, idle = {"busy": True, "watched": False}, {"busy": False, "watched": False}
    assert not L.LivePoller._due(st, busy, 1_000_000 + L.ACTIVE_MS - 1, 0) and L.LivePoller._due(st, busy, 1_000_000 + L.ACTIVE_MS, 0)
    assert not L.LivePoller._due(st, {"busy": True, "watched": True}, 1_000_000 + L.ACTIVE_MS - 1, 0), "no faster while watched: Anthropic slows a faster poller down"
    assert not L.LivePoller._due(st, idle, 1_000_000 + L.WATCHED_MS, 0) and L.LivePoller._due(st, idle, 1_000_000 + L.IDLE_MS, 0)
    assert L.LivePoller._due(st, {"busy": False, "watched": True}, 1_000_000 + L.WATCHED_MS, 0), "the dashboard is open"

    # After a "slow down": half as often, back to normal once it hasn't happened for a while.
    slow = L._Status(checked_at=1_000_000, slow_level=1, slow_at=1_000_000)
    assert not L.LivePoller._due(slow, busy, 1_000_000 + 2 * L.ACTIVE_MS - 1, 0)
    assert L.LivePoller._due(slow, busy, 1_000_000 + 2 * L.ACTIVE_MS, 0)
    assert L.LivePoller._due(slow, busy, 1_000_000 + L.SLOWER_FOR_MS, 0) and slow.slow_level == 0


def test_poller_backs_off_when_asked_to(anthropic: FakeAnthropic, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(L, "MIN_GAP_MS", 0)
    now = time.time() * 1000
    profile = make_profile(tmp_path / "main", "main", A)
    write_login(profile.dir, TOKEN_A, "sk-ant-ort01-a", now + 4 * HOUR)
    anthropic.fail_usage = (429, {"type": "error", "error": {"type": "rate_limit_error", "message": "Slow down"}}, {"Retry-After": "120"})
    monitor = FakeMonitor([target(profile, A)])
    poller = L.LivePoller(monitor)
    poller.round(L.C.now_ms())
    st = monitor.statuses[A["uuid"]]
    assert st["state"] == "slow" and "slow down" in st["detail"] and abs(st["nextAt"] - (L.C.now_ms() + 120_000)) < 5_000
    poller._forced_at = L.C.now_ms() + 1
    poller.round(L.C.now_ms() + 2)
    assert anthropic.count("usage") == 1, "not even on demand during the wait Anthropic asked for"
    assert not monitor.readings


def test_poller_skips_a_profile_being_changed(anthropic: FakeAnthropic, tmp_path: Path) -> None:
    """The monitor hasn't read the profile's latest config yet: it may be another account now."""
    now = time.time() * 1000
    profile = make_profile(tmp_path / "main", "main", A)
    write_login(profile.dir, TOKEN_A, "sk-ant-ort01-a", now + 4 * HOUR)
    anthropic.usage[TOKEN_A] = usage_body(25, 78, now)
    monitor = FakeMonitor([{"account": A["uuid"], "profiles": [(profile, stamp(profile) - 1)], "busy": True, "watched": False}])
    L.LivePoller(monitor).round(L.C.now_ms())
    assert anthropic.count("usage") == 0 and not monitor.readings
    assert monitor.statuses[A["uuid"]]["state"] == "pending"


def test_poller_drops_an_answer_for_another_account(anthropic: FakeAnthropic, tmp_path: Path) -> None:
    """A sign-in caught between its two files: the login is already another account's."""
    now = time.time() * 1000
    profile = make_profile(tmp_path / "main", "main", A)
    write_login(profile.dir, TOKEN_A, "sk-ant-ort01-a", now + 4 * HOUR)
    anthropic.usage[TOKEN_A] = usage_body(25, 78, now)
    anthropic.orgs[TOKEN_A] = "org-of-another-account"
    monitor = FakeMonitor([{**target(profile, A), "orgId": "org-a"}])
    poller = L.LivePoller(monitor)
    poller.round(L.C.now_ms())
    assert not monitor.readings and monitor.statuses[A["uuid"]]["state"] == "error"
    assert poller.status[A["uuid"]].wait_until >= L.C.now_ms() + 20_000, "backs off rather than asking again at once"

    anthropic.orgs[TOKEN_A] = "org-a"
    poller.status[A["uuid"]].wait_until = 0
    poller.status[A["uuid"]].checked_at = 0
    poller.round(L.C.now_ms())
    assert monitor.readings and monitor.statuses[A["uuid"]]["state"] == "ok"


def test_poller_without_a_login(tmp_path: Path) -> None:
    profile = make_profile(tmp_path / "spare")
    monitor = FakeMonitor([target(profile)])
    L.LivePoller(monitor).round(L.C.now_ms())
    st = monitor.statuses[C3["uuid"]]
    assert st["state"] == "no_login" and "spare" in st["detail"]


# ---------------------------------------------- the monitor's side


def test_a_rise_means_in_use_only_between_recent_readings(tmp_path: Path) -> None:
    from usage_monitor import common as C
    from usage_monitor.monitor import Monitor

    m = Monitor(C.Paths(tmp_path / "data"), readonly=True)
    now = time.time() * 1000
    a = m.upsert_account({"id": C3["uuid"], "email": C3["email"]}, now)
    # Claude Code's backups carry a reading from hours ago: the rise since says nothing about now.
    m.apply_reading(a, {"at": now - 3 * HOUR, "source": "claude-cache", "session": {"pct": 40, "resetsAt": now + HOUR}})
    m.apply_live(C3["uuid"], {"at": now, "source": "api", "session": {"pct": 85, "resetsAt": now + HOUR}, "weekly": None})
    assert not a.get("lastRunningAt") and not m.view_of(a, now)["running"]
    # A minute later it rose again: in use right now, wherever that is.
    m.apply_live(C3["uuid"], {"at": now + MIN, "source": "api", "session": {"pct": 86, "resetsAt": now + HOUR}, "weekly": None})
    assert a["lastRunningAt"] == now + MIN and m.view_of(a, now + MIN)["running"]


def test_pace_comes_from_anthropics_figures(tmp_path: Path) -> None:
    from usage_monitor import common as C
    from usage_monitor.monitor import Monitor

    m = Monitor(C.Paths(tmp_path / "data"), readonly=True)
    now = time.time() * 1000
    a = m.upsert_account({"id": A["uuid"], "email": A["email"], "tier": TIER}, now - 20 * MIN)
    m.state["activeId"] = A["uuid"]
    reset, week = now + 3 * HOUR, now + 3 * 24 * HOUR
    for i, pct in enumerate([20, 22, 24, 26]):  # +2% every 3 minutes: 40%/h
        m.apply_live(A["uuid"], {"at": now - (9 - 3 * i) * MIN, "source": "api", "session": {"pct": pct, "resetsAt": reset}, "weekly": {"pct": 50, "resetsAt": week}})
    v = m.view_of(a, now)
    assert v["session"]["pct"] == 26 and v["session"]["estimated"] is False and v["running"]
    assert v["rate"]["session"]["basis"] == "Anthropic's figures, last 20 min"
    assert v["rate"]["session"]["perHour"] == pytest.approx(40, rel=1e-6)
    assert v["rate"]["weekly"]["perHour"] == pytest.approx(40 * 5.7 / 31, rel=1e-6), "the same usage, through each limit's calibration"
    assert v["forecast"]["session"]["limitAt"] == pytest.approx(now + 74 / 40 * HOUR, abs=1000)


def test_an_alert_never_suggests_its_own_account(tmp_path: Path) -> None:
    from usage_monitor import planner as PL
    from usage_monitor.monitor import Monitor

    def v(id_: str, session: float, active: bool) -> dict:
        return {
            "id": id_,
            "displayName": id_,
            "active": active,
            "switchable": not active,
            "hasData": True,
            "session": {"pct": session, "resetsAt": None},
            "weekly": {"pct": 10, "resetsAt": None},
            "lastActiveAt": 0,
        }

    monitor = Monitor(L.C.Paths(tmp_path / "data"), readonly=True)
    now = time.time() * 1000
    views = [v("main", 30, True), v("elsewhere", 85, False)]
    monitor.state["activeId"] = "main"
    rec = PL.recommendation(monitor.plan_for(views, now))
    assert rec["id"] == "elsewhere", "the best spare for the main account"
    assert monitor._rec_for(views[1], views, rec, now)["id"] == "main", "an alert about it suggests another one"
    assert monitor._rec_for(views[0], views, rec, now) is rec


# ---------------------------------------------- end to end


def test_listener_with_live_figures(tmp_path: Path) -> None:
    fake = FakeAnthropic()
    rig = Rig(tmp_path)
    try:
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
        now = time.time() * 1000
        # Main profile: account A. Claude Code's own cache is half an hour old.
        cache = {"fetchedAtMs": now - 30 * MIN, "accountUuid": A["uuid"], "utilization": usage_body(40, 55, now)}
        account_a = {"accountUuid": A["uuid"], "emailAddress": A["email"], "organizationUuid": "org-a", "organizationRateLimitTier": TIER}
        (rig.main / ".claude.json").write_text(json.dumps({"oauthAccount": account_a, "cachedUsageUtilization": cache}), encoding="utf-8")
        write_login(rig.main, TOKEN_A, "sk-ant-ort01-a", now + 4 * HOUR)
        fake.usage[TOKEN_A] = usage_body(61, 70, now)
        fake.orgs[TOKEN_A] = "org-a"
        # Spare profile: account C, idle; its login expired an hour ago.
        (rig.spare / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": C3["uuid"], "emailAddress": C3["email"], "organizationRateLimitTier": TIER}}), encoding="utf-8")
        write_login(rig.spare, TOKEN_C_OLD, REFRESH_C, now - HOUR)
        fake.refresh[REFRESH_C] = {"access_token": TOKEN_C, "refresh_token": REFRESH_C2}
        fake.usage[TOKEN_C] = usage_body(12, 33, now)
        (rig.data / "profiles.json").write_text(json.dumps({"profiles": [{"name": "spare"}]}), encoding="utf-8")
        main_transcript = rig.main / "projects" / "proj" / "session.jsonl"
        main_transcript.write_text("", encoding="utf-8")

        rig.start()
        s = rig.until(lambda x: x["ready"] and view(x, A["uuid"])["source"] == "api" and view(x, C3["uuid"])["source"] == "api")
        a, c = view(s, A["uuid"]), view(s, C3["uuid"])
        assert (a["session"]["pct"], a["weekly"]["pct"], a["session"]["estimated"]) == (61, 70, False)
        assert (c["session"]["pct"], c["weekly"]["pct"]) == (12, 33)
        assert a["live"]["state"] == "ok" and c["live"]["state"] == "ok" and c["live"]["profile"] == "spare"
        assert {"label": "Weekly · Fable", "pct": 4} == {k: a["scoped"][0][k] for k in ("label", "pct")}

        # The spare profile's expired login was renewed and saved for Claude Code to use.
        saved = json.loads((rig.spare / ".credentials.json").read_text(encoding="utf-8"))["claudeAiOauth"]
        assert saved["accessToken"] == TOKEN_C and saved["refreshToken"] == REFRESH_C2 and saved["expiresAt"] > now + 7 * HOUR
        texts = [e["text"] for e in s["events"]]
        assert any("Renewed the expired Claude login of profile spare" in t for t in texts), texts
        assert any("Official figures from Anthropic for first@example.com: 5-hour 61%, weekly 70%" in t for t in texts), texts

        # Spend in a transcript doesn't move an exact figure; Anthropic's next answer does.
        _append(main_transcript, _reply("1", 2_850_000))  # $57: +10% by the estimate
        rig.until(lambda x: view(x, A["uuid"])["running"])
        assert view(rig.get(), A["uuid"])["session"]["pct"] == 61
        fake.usage[TOKEN_A] = usage_body(64, 71, now)
        s = rig.until(lambda x: view(x, A["uuid"])["session"]["pct"] == 64)
        a = view(s, A["uuid"])
        assert s["heroId"] == A["uuid"] and a["seriesKind"] == "official" and a["marks"][-1][1] == 64 and a["series"][-1][1] == 64

        # No login material ever reaches the dashboard's API.
        text = json.dumps(rig.get())
        for bad in ("sk-ant", "accessToken", "refreshToken", "credentials"):
            assert bad not in text

        # The agent's status asks Anthropic first.
        fake.usage[TOKEN_A] = usage_body(66, 72, now)
        code, status = rig.cli("status")
        row = next(x for x in status["accounts"] if x["account"] == A["email"])
        assert code == 0 and row["five_hour"]["used"] == "66%" and row["figures"].startswith("official") and status["live_figures"] == "on"

        # Turned off: no more requests.
        code, out = rig.cli("settings", "--live", "off")
        assert code == 0 and out["settings"]["live"] is False
        time.sleep(0.6)  # a request already out when it was switched off
        asked = fake.count("usage")
        time.sleep(1.5)
        assert fake.count("usage") == asked and rig.get()["live"]["enabled"] is False
    finally:
        rig.stop(graceful=False)
        fake.close()
