"""Official usage straight from Anthropic: the figures claude.ai shows under Settings → Usage.

Claude Code's ``/usage`` asks Anthropic's usage API (``GET /api/oauth/usage``) with the
profile's Claude login, and so does this module, for every tracked account: every 30-60 s
while the account is in use, every 5 minutes otherwise, and at once when the dashboard opens
or the agent asks for the status. The answer is exact, it counts usage made anywhere
(claude.ai, the apps, other computers), and it replaces the estimates.

The login comes from the profile's ``.credentials.json`` (on a Mac, from the Keychain when
Claude Code keeps it there). It is sent to Anthropic only, and never stored, logged or shown.
An access token lasts 8 hours and Claude Code renews it while it runs. When an idle profile's
token has expired, the monitor renews it the way Claude Code does — under Claude Code's own
refresh lock, re-reading the file first, saving with a compare-and-swap — so a Claude Code
session in that profile simply adopts the new token. Keychain logins are used while valid and
never renewed here.
"""

from __future__ import annotations

import email.utils
import hashlib
import json
import os
import random
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import common as C
from . import usage as U
from .profiles import ClaudeProfile


def _loopback_override(name: str, default: str) -> str:
    """Tests point these at a local fake. Anything but a loopback address is ignored, so a
    stray variable can never send a login anywhere else."""
    value = os.environ.get(name, "").strip().rstrip("/")
    if value:
        parts = urlsplit(value)
        if parts.scheme in ("http", "https") and parts.hostname in ("127.0.0.1", "localhost", "::1"):
            return value
    return default


def _ms_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name) or default)
    except ValueError:
        return default
    return value if value > 0 else default


API_URL = _loopback_override("CLAUDE_USAGE_MONITOR_API_URL", "https://api.anthropic.com")
TOKEN_URL = _loopback_override("CLAUDE_USAGE_MONITOR_TOKEN_URL", "https://platform.claude.com/v1/oauth/token")
USAGE_PATH = "/api/oauth/usage"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code's OAuth client
OAUTH_BETA = "oauth-2025-04-20"
USER_AGENT = "claude-usage-monitor (Cremind skill)"
KEYCHAIN_SERVICE = "Claude Code-credentials"

# Anthropic answers "slow down" to a monitor asking every 30-60 s (20 times in a morning,
# 2026-10-07), and each costs 5 minutes without figures. Between readings the figures roll
# forward with what each reply costs, so auto mode doesn't wait for a reading either.
ACTIVE_MS = _ms_env("CLAUDE_USAGE_MONITOR_LIVE_ACTIVE_MS", 2 * 60_000)  # an account in use
WATCHED_MS = _ms_env("CLAUDE_USAGE_MONITOR_LIVE_WATCHED_MS", 5 * 60_000)  # an idle one while the dashboard is open
IDLE_MS = _ms_env("CLAUDE_USAGE_MONITOR_LIVE_IDLE_MS", 10 * 60_000)
MIN_GAP_MS = _ms_env("CLAUDE_USAGE_MONITOR_LIVE_MIN_GAP_MS", 45_000)  # between on-demand refreshes
SLOWER_FOR_MS = 30 * 60_000  # after a "slow down", an account is asked less often for this long
FRESH_MS = 3 * 60_000  # a live reading this recent is shown as it is, the way claude.ai shows it
RENEW_AHEAD_MS = 60_000  # a token this close to expiry is renewed first (Claude Code: 5 min ahead, while it runs)
SLOW_DOWN_MS = 5 * 60_000  # after a 429 or 403 that names no wait (what Claude Code waits)
MAX_WAIT_MS = 60 * 60_000
RETRY_MS = 2_000  # a login or profile caught mid-change
RECHECK_MS = 30_000  # a profile without a usable login: look again this often (a file read)
HTTP_TIMEOUT_S = 10.0
LOCK_STALE_MS = 60_000  # Claude Code's refresh lock counts as abandoned after this (proper-lockfile)
STORAGE_LOCK_STALE_MS = 15_000  # its credential-store write lock
CONFIG_LOCK_STALE_MS = 10_000  # its .claude.json lock (proper-lockfile's default)
LOCK_TOUCH_S = 2.0


# ---------------------------------------------------------------- logins


@dataclass(frozen=True)
class Login:
    """A profile's Claude login. Only ever sent to Anthropic."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float | None
    scopes: tuple[str, ...]

    def usable(self, now: float) -> bool:
        return self.expires_at is None or self.expires_at - now > RENEW_AHEAD_MS


def parse_login(data: Any) -> Login | None:
    o = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(o, dict):
        return None
    token = o.get("accessToken")
    if not isinstance(token, str) or not token:
        return None
    exp = o.get("expiresAt")
    expires_at = float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) and exp > 0 else None
    refresh = o.get("refreshToken") if isinstance(o.get("refreshToken"), str) else ""
    scopes = tuple(s for s in o["scopes"] if isinstance(s, str)) if isinstance(o.get("scopes"), list) else ()
    return Login(token, refresh, expires_at, scopes)


class LoginStore:
    """Where Claude Code keeps one profile's login: ``.credentials.json`` in the profile's
    folder, or on a Mac the Keychain (read only here)."""

    def __init__(self, profile: ClaudeProfile) -> None:
        self.profile = profile
        self.file = profile.dir / ".credentials.json"

    def stamp(self) -> Any:
        try:
            st = os.stat(self.file)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def read(self) -> tuple[Login | None, str]:
        """``(login, kind)``; kind is "file", "keychain", "unreadable" (being rewritten) or "none".
        On a Mac, Claude Code reads the Keychain first and a file only when it holds nothing."""
        if sys.platform == "darwin":
            login = parse_login(_keychain_read(self.keychain_service()))
            if login:
                return login, "keychain"
        if self.file.exists():
            data = _read_retrying(self.file)
            return (parse_login(data), "file") if data is not None else (None, "unreadable")
        return None, "none"

    def keychain_service(self) -> str:
        """Claude Code names the Keychain item after the config folder unless it is the default one."""
        default = Path.home() / ".claude"
        if os.path.normcase(os.path.abspath(self.profile.dir)) == os.path.normcase(os.path.abspath(default)):
            return KEYCHAIN_SERVICE
        digest = hashlib.sha256(unicodedata.normalize("NFC", str(self.profile.dir)).encode("utf-8")).hexdigest()[:8]
        return f"{KEYCHAIN_SERVICE}-{digest}"


def _read_retrying(path: Path) -> Any:
    """The parsed file, retried briefly while another process rewrites it; None when missing."""
    for attempt in range(5):
        data = C.read_json(path)
        if data is not None or not path.exists():
            return data
        time.sleep(0.05 * (attempt + 1))
    return None


def _keychain_read(service: str) -> Any:
    for account in (["-a", "claude-code-user"], []):
        try:
            out = subprocess.run(["security", "find-generic-password", *account, "-w", "-s", service], capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode == 0:
            try:
                return json.loads(out.stdout.decode("utf-8").strip())
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------- Anthropic's API


class LiveError(Exception):
    """kind: auth (401) | denied (403) | slow (429) | server | network | bad"""

    def __init__(self, kind: str, message: str, retry_after_ms: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after_ms = retry_after_ms


class DeadLogin(Exception):
    """Anthropic refused the refresh token: only /login in Claude Code fixes it."""


def _retry_after_ms(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value)) * 1000
    except ValueError:
        pass
    try:
        return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() * 1000 - C.now_ms())
    except (TypeError, ValueError):
        return None


def _error_of(e: urllib.error.HTTPError) -> tuple[str | None, str | None]:
    """(OAuth error code, short message) from an error answer; never the request, which
    carries the login."""
    try:
        body = json.loads(e.read(4000).decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return None, None
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return err.get("type") if isinstance(err.get("type"), str) else None, err.get("message") or err.get("type")
    if isinstance(err, str):
        return err, body.get("error_description") or err
    return None, None


def _short(e: BaseException) -> str:
    return (str(e) or type(e).__name__)[:200]


_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open(req: urllib.request.Request, timeout: float) -> Any:
    """Anthropic through the system's proxy settings; a loopback fake (tests) directly."""
    if urlsplit(req.full_url).hostname in ("127.0.0.1", "localhost", "::1"):
        return _DIRECT.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_usage(token: str, timeout: float = HTTP_TIMEOUT_S) -> tuple[dict, str | None]:
    """Anthropic's usage answer for the account the token belongs to, and that account's
    organization as Anthropic names it."""
    req = urllib.request.Request(
        API_URL + USAGE_PATH,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with _open(req, timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            org = resp.headers.get("anthropic-organization-id") or None
    except urllib.error.HTTPError as e:
        _code, message = _error_of(e)
        kind = {401: "auth", 403: "denied", 429: "slow"}.get(e.code, "server")
        text = f"HTTP {e.code}" + (f": {message}" if message else "")
        raise LiveError(kind, text[:200], _retry_after_ms(e.headers.get("Retry-After"))) from None
    except (OSError, ValueError) as e:  # URLError and timeouts are OSErrors
        raise LiveError("network", _short(e)) from None
    if not isinstance(body, dict):
        raise LiveError("bad", "unexpected answer")
    return body, org


def _post_refresh(refresh_token: str, scopes: tuple[str, ...]) -> dict:
    """Claude Code's refresh request, to the same endpoint with the same client."""
    payload: dict[str, Any] = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID}
    if scopes:
        payload["scope"] = " ".join(scopes)
    while True:
        req = urllib.request.Request(
            TOKEN_URL,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with _open(req, 30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            code, message = _error_of(e)
            if code == "invalid_scope" and "scope" in payload:
                del payload["scope"]
                continue
            if code == "invalid_grant":
                raise DeadLogin(message or code) from None
            kind = "slow" if e.code == 429 else "server" if e.code >= 500 else "denied"
            text = f"renewing the login: HTTP {e.code}" + (f": {message}" if message else "")
            raise LiveError(kind, text[:200], _retry_after_ms(e.headers.get("Retry-After"))) from None
        except (OSError, ValueError) as e:
            raise LiveError("network", f"renewing the login: {_short(e)}") from None
        ok = isinstance(body, dict) and isinstance(body.get("access_token"), str) and body["access_token"]
        if not ok or not isinstance(body.get("expires_in"), (int, float)) or isinstance(body.get("expires_in"), bool):
            raise LiveError("bad", "renewing the login: unexpected answer")
        return body


# ---------------------------------------------------------------- renewing a login


def _take_dir(d: Path, stale_ms: float = LOCK_STALE_MS) -> bool:
    """mkdir-based lock, as proper-lockfile takes it; an abandoned one is taken over."""
    for _ in range(2):
        try:
            os.mkdir(d)
            return True
        except FileExistsError:
            pass
        except OSError:
            return False
        try:
            age_ms = (time.time() - os.stat(d).st_mtime) * 1000
        except FileNotFoundError:
            continue  # released meanwhile
        except OSError:
            return False
        if age_ms < stale_ms:
            return False
        try:
            os.rmdir(d)  # its holder died: take it over
        except OSError:
            return False
    return False


class DirLock:
    """A lock Claude Code takes with proper-lockfile: one or more directories, created
    together, abandoned once older than ``stale_ms``. Ours are touched every 2 s while
    held, so a Claude Code process that wants one waits instead of taking it over."""

    def __init__(self, dirs: tuple[Path, ...], stale_ms: float, name: str = "lock") -> None:
        self.dirs = dirs
        self.stale_ms = stale_ms
        self.name = name
        self.held: list[Path] = []
        self._stop = threading.Event()
        self._toucher: threading.Thread | None = None

    def acquire(self, attempts: int = 3, wait_s: float = 1.0) -> bool:
        """``attempts`` tries ``wait_s`` (plus up to as much again at random) apart."""
        for i in range(attempts):
            if i:
                time.sleep(wait_s * (1 + random.random()))
            if self._try():
                self._toucher = threading.Thread(target=self._touch, name=self.name, daemon=True)
                self._toucher.start()
                return True
        return False

    def _try(self) -> bool:
        for d in self.dirs:
            if not _take_dir(d, self.stale_ms):
                self.release()
                return False
            self.held.append(d)
        return True

    def _touch(self) -> None:
        while not self._stop.wait(LOCK_TOUCH_S):
            for d in self.held:
                try:
                    os.utime(d)
                except OSError:
                    pass

    def release(self) -> None:
        self._stop.set()
        if self._toucher is not None:
            self._toucher.join(5)
            self._toucher = None
        for d in reversed(self.held):
            try:
                os.rmdir(d)
            except OSError:
                pass
        self.held = []
        self._stop = threading.Event()


def RefreshLock(home: Path) -> DirLock:  # noqa: N802 - a lock, named like the others
    """Claude Code's OAuth refresh lock: ``<home>/.oauth_refresh.lock`` and, for older
    versions, ``<home>.lock``. Held around a whole renewal."""
    return DirLock((home / ".oauth_refresh.lock", Path(os.path.realpath(home) + ".lock")), LOCK_STALE_MS, "refresh-lock")


def StorageLock(home: Path) -> DirLock:  # noqa: N802
    """Claude Code's credential-store lock, ``<home>/.storage-write.lock``: every read-modify-
    write of a home's login (a renewal's save, /login, a device token) runs under it."""
    return DirLock((home / ".storage-write.lock",), STORAGE_LOCK_STALE_MS, "storage-lock")


def ConfigLock(config_file: Path) -> DirLock:  # noqa: N802
    """Claude Code's lock on its config file, ``<config>.lock`` (``~/.claude.json.lock``):
    each save re-reads the file under it and changes only what it set out to change."""
    return DirLock((config_file.with_name(config_file.name + ".lock"),), CONFIG_LOCK_STALE_MS, "config-lock")


def _write_private_json(path: Path, data: Any) -> None:
    """Atomic like ``C.write_json_atomic``, compact like Claude Code writes it, and readable
    by its owner only."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # Windows: another process may have the file open for a moment.
                time.sleep(0.02 * (attempt + 1))
        raise PermissionError(f"could not replace {path.name}")
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def renew(store: LoginStore, seen: Login) -> tuple[str, Login | None]:
    """Renew a profile's expired login the way Claude Code does.

    Returns ``("renewed" | "adopted", login)`` — adopted when another process renewed it or
    signed in meanwhile — or ``("busy", None)`` while Claude Code holds the lock, or
    ``("dead", None)`` when only /login can fix it. Raises LiveError when Anthropic can't
    be asked or refuses for another reason.
    """
    lock = RefreshLock(store.profile.dir)
    if not lock.acquire():
        return "busy", None
    try:
        data = _read_retrying(store.file)
        current = parse_login(data)
        if current is None:
            return "adopted", None  # signed out meanwhile
        if current.access_token != seen.access_token and current.usable(C.now_ms()):
            return "adopted", current
        if not current.refresh_token:
            return "dead", None
        try:
            body = _post_refresh(current.refresh_token, current.scopes)
        except DeadLogin:
            return "dead", None
        return _save(store, current.refresh_token, body)
    finally:
        lock.release()


def _save(store: LoginStore, posted: str, body: dict) -> tuple[str, Login | None]:
    """Compare-and-swap, as Claude Code saves: only over the refresh token that was used, and
    under its credential-store lock. Anthropic has already replaced the refresh token by now,
    so a lock that can't be had within its stale time is no reason to drop the new one."""
    lock = StorageLock(store.profile.dir)
    held = lock.acquire(attempts=20, wait_s=0.5)
    try:
        return _save_locked(store, posted, body)
    finally:
        if held:
            lock.release()


def _save_locked(store: LoginStore, posted: str, body: dict) -> tuple[str, Login | None]:
    now = C.now_ms()
    data = _read_retrying(store.file)
    o = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(o, dict):
        return "adopted", None  # signed out meanwhile: don't bring the login back
    if o.get("refreshToken") not in ("", posted):
        return "adopted", parse_login(data)  # a newer login was saved meanwhile: keep it
    fresh = {
        **o,
        "accessToken": body["access_token"],
        "refreshToken": body.get("refresh_token") or posted,
        "expiresAt": int(now + float(body["expires_in"]) * 1000),
    }
    later = body.get("refresh_token_expires_in")
    if isinstance(later, (int, float)) and not isinstance(later, bool):
        fresh["refreshTokenExpiresAt"] = int(now + float(later) * 1000)
    if isinstance(body.get("scope"), str) and body["scope"].split():
        fresh["scopes"] = body["scope"].split()
    data["claudeAiOauth"] = fresh
    try:
        _write_private_json(store.file, data)
    except OSError as e:
        raise LiveError("bad", f"could not save the renewed login: {_short(e)}") from None
    return "renewed", parse_login(data)


# ---------------------------------------------------------------- the poller


@dataclass
class _Status:
    """What the poller knows about one account."""

    state: str = "pending"  # ok | pending | no_login | expired | signin | slow | denied | error | off
    detail: str | None = None
    profile: str | None = None
    checked_at: float = 0.0  # last request to Anthropic
    ok_at: float | None = None
    wait_until: float = 0.0
    failures: int = 0
    slow_level: int = 0  # each "slow down" doubles the interval (up to 4x) for SLOWER_FOR_MS
    slow_at: float = 0.0
    dead_stamp: Any = None  # login file stamp when Anthropic refused it: retried once it changes

    def view(self) -> dict:
        return {
            "state": self.state,
            "detail": self.detail,
            "profile": self.profile,
            "checkedAt": self.checked_at or None,
            "okAt": self.ok_at,
            "nextAt": self.wait_until or None,
        }


@dataclass
class _Candidate:
    profile: ClaudeProfile
    known_config: Any
    store: LoginStore
    login: Login
    kind: str


def _config_stamp(profile: ClaudeProfile) -> Any:
    try:
        return os.stat(profile.config_file).st_mtime_ns
    except OSError:
        return "missing"


def _clock(ms: float) -> str:
    return time.strftime("%H:%M", time.localtime(ms / 1000))


class LivePoller:
    """Asks Anthropic for every tracked account's usage, in its own thread — network calls
    never hold the monitor's lock — and hands the readings to the monitor.

    The monitor provides ``live_targets(now)``, ``apply_live(account_id, reading)``,
    ``note_live(statuses)``, ``note_live_event(text, level)`` and ``warn(where, e)``.
    """

    def __init__(self, monitor: Any, *, renew_logins: bool = True) -> None:
        self.monitor = monitor
        self.renew_logins = renew_logins
        self.status: dict[str, _Status] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._cv = threading.Condition()
        self._forced_at = 0.0
        self._done = 0.0  # start time of the last finished round
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ thread

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="live-usage", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(15)

    def _run(self) -> None:
        while not self._stop.is_set():
            started = C.now_ms()
            try:
                self.round(started)
            except Exception:  # noqa: BLE001 - one bad round must not stop the polling
                self.monitor.warn("live figures", traceback.format_exc(limit=6))
            with self._cv:
                self._done = started
                self._cv.notify_all()
            self._wake.wait(1.0)
            self._wake.clear()

    def refresh(self, wait_s: float) -> bool:
        """Ask every account now — no sooner than MIN_GAP_MS after its last request, and never
        during a back-off Anthropic asked for. Waits up to ``wait_s`` for the answers."""
        asked = C.now_ms()
        with self._cv:
            self._forced_at = asked
        self._wake.set()
        if wait_s <= 0:
            return False
        deadline = time.monotonic() + wait_s
        with self._cv:
            while self._done < asked:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(left)
        return True

    # ------------------------------------------------------------ one round

    def round(self, now: float) -> None:
        if getattr(self.monitor, "switching", False):
            return  # logins are moving between profiles; an answer caught half-way would be misread
        targets = self.monitor.live_targets(now)
        if targets is None:  # turned off in the settings
            for st in self.status.values():
                st.state, st.detail = "off", None
        else:
            forced = self._forced_at
            for t in targets:
                st = self.status.setdefault(t["account"], _Status())
                if st.state == "off":
                    st.state = "pending"
                if self._due(st, t, now, forced):
                    self.poll(t, st)
            tracked = {t["account"] for t in targets}
            for account_id in [a for a in self.status if a not in tracked]:
                del self.status[account_id]
        self.monitor.note_live({a: st.view() for a, st in self.status.items()})

    @staticmethod
    def _due(st: _Status, t: dict, now: float, forced: float) -> bool:
        if now < st.wait_until:
            return False
        if forced > st.checked_at and now - st.checked_at >= MIN_GAP_MS:
            return True
        if st.slow_level and now - st.slow_at >= SLOWER_FOR_MS:
            st.slow_level -= 1
            st.slow_at = now
        if t["busy"]:
            interval = ACTIVE_MS
        else:
            interval = WATCHED_MS if t["watched"] else IDLE_MS
        interval = min(interval * 2**st.slow_level, max(interval, IDLE_MS))
        return now - st.checked_at >= interval

    def poll(self, t: dict, st: _Status) -> None:
        """One account: find a usable login among its profiles, renew it if it expired, ask
        Anthropic, hand over the reading."""
        now = C.now_ms()
        found: list[_Candidate] = []
        for profile, known_config in t["profiles"]:
            store = LoginStore(profile)
            login, kind = store.read()
            if kind == "unreadable":
                st.wait_until = now + RETRY_MS
                return
            if login:
                found.append(_Candidate(profile, known_config, store, login, kind))
        names = ", ".join(p.name for p, _ in t["profiles"])
        if not found:
            self._set(st, "no_login", f"No Claude login readable in profile {names}; figures are estimates", now + RECHECK_MS)
            return

        pick = next((c for c in found if c.login.usable(now)), None)
        renewed = False
        if pick is None:
            renewable = sorted((c for c in found if c.kind == "file" and c.login.refresh_token), key=lambda c: c.profile.main)
            if not renewable:
                self._set(st, "expired", f"Its login has expired; live figures resume when Claude Code next runs in profile {names}", now + RECHECK_MS)
                return
            if not self.renew_logins:
                self._set(st, "expired", "Its login has expired; the monitor renews it once it runs", now + RECHECK_MS)
                return
            pick = self._renew(renewable[0], st, now)
            if pick is None:
                return
            renewed = True

        # The profile's account as the monitor last read it must still be the one signed in.
        if _config_stamp(pick.profile) != pick.known_config:
            st.wait_until = now + RETRY_MS
            return
        st.checked_at = now
        st.profile = pick.profile.name
        before = pick.store.stamp()
        try:
            body, org = fetch_usage(pick.login.access_token)
        except LiveError as e:
            if e.kind != "auth" or renewed or pick.kind != "file" or not pick.login.refresh_token or not self.renew_logins:
                self._failed(st, e, now)
                return
            # Revoked early (another sign-in, or a renewal elsewhere): renew it as Claude Code would.
            pick = self._renew(pick, st, now)
            if pick is None:
                return
            before = pick.store.stamp()
            try:
                body, org = fetch_usage(pick.login.access_token)
            except LiveError as e2:
                self._failed(st, e2, now)
                return
        switched = getattr(self.monitor, "recently_switched", lambda _now: False)(now)
        if pick.store.stamp() != before or (switched and _config_stamp(pick.profile) != pick.known_config):
            st.wait_until = now + RETRY_MS  # signed in again meanwhile: the answer may be another account's
            return
        if org and t.get("orgId") and org != t["orgId"]:
            # The login and the profile's account disagree: a sign-in, or an account switch, caught
            # between its two files.
            if switched:
                st.wait_until = now + RETRY_MS
                return
            self._failed(st, LiveError("bad", "the profile's login belongs to another account than its config names"), now)
            return
        reading = U.from_usage_api(body, C.now_ms())
        if not reading:
            self._failed(st, LiveError("bad", "Anthropic's answer had no usage figures"), now)
            return
        self.monitor.apply_live(t["account"], reading)
        st.state, st.detail, st.ok_at, st.failures, st.wait_until = "ok", None, reading["at"], 0, 0.0

    def _renew(self, c: _Candidate, st: _Status, now: float) -> _Candidate | None:
        if st.dead_stamp is not None and st.dead_stamp == c.store.stamp():
            self._set(st, "signin", self._signin_text(c), now + RECHECK_MS)
            return None
        try:
            outcome, login = renew(c.store, c.login)
        except LiveError as e:
            self._failed(st, e, now)
            return None
        if outcome == "busy":
            st.wait_until = now + 5_000  # Claude Code is renewing it right now
            return None
        if outcome == "dead":
            st.dead_stamp = c.store.stamp()
            self._set(st, "signin", self._signin_text(c), now + RECHECK_MS)
            self.monitor.note_live_event(f"Anthropic no longer accepts the login of profile {c.profile.name}: {self._signin_text(c)}", "warning")
            return None
        if login is None or not login.usable(C.now_ms()):
            self._set(st, "no_login", f"No Claude login readable in profile {c.profile.name}; figures are estimates", now + RECHECK_MS)
            return None
        st.dead_stamp = None
        if outcome == "renewed":
            self.monitor.note_live_event(f"Renewed the expired Claude login of profile {c.profile.name}, as Claude Code does", "info")
        return _Candidate(c.profile, c.known_config, c.store, login, c.kind)

    @staticmethod
    def _signin_text(c: _Candidate) -> str:
        where = "your main Claude Code" if c.profile.main else f"Claude Code's {c.profile.name} profile"
        return f"sign in again with /login in {where}"

    @staticmethod
    def _set(st: _Status, state: str, detail: str | None, wait_until: float) -> None:
        st.state, st.detail, st.wait_until = state, detail, wait_until

    def _failed(self, st: _Status, e: LiveError, now: float) -> None:
        if e.kind in ("slow", "denied"):
            if e.kind == "slow":
                st.slow_level, st.slow_at = min(st.slow_level + 1, 2), now
            wait = min(MAX_WAIT_MS, max(MIN_GAP_MS, e.retry_after_ms or SLOW_DOWN_MS))
            state = "slow" if e.kind == "slow" else "denied"
            what = "Anthropic asked to slow down" if e.kind == "slow" else f"Anthropic refused ({e})"
            self._set(st, state, f"{what}; next try at {_clock(now + wait)}", now + wait)
            return
        st.failures += 1
        wait = min(15 * 60_000, 30_000 * 2 ** min(st.failures - 1, 5))
        self._set(st, "error", f"{e}; next try at {_clock(now + wait)}", now + wait)


def poll_once(monitor: Any, wait_s: float = 8.0) -> None:
    """One immediate request per account with the logins as they are (none is renewed): the
    CLI's figures while the monitor isn't running."""
    poller = LivePoller(monitor, renew_logins=False)
    targets = monitor.live_targets(C.now_ms())
    if not targets:
        return
    threads = []
    for t in targets:
        st = poller.status.setdefault(t["account"], _Status())
        threads.append(threading.Thread(target=poller.poll, args=(t, st), daemon=True))
    for th in threads:
        th.start()
    deadline = time.monotonic() + wait_s
    for th in threads:
        th.join(max(0.0, deadline - time.monotonic()))
    monitor.note_live({a: st.view() for a, st in poller.status.items()})
