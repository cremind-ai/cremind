"""Quick account switching: make another account the one your Claude Code uses, with the
login already saved on this computer — no /login, no browser.

Claude Code keeps one login per config folder (a "home"). /login replaces a home's login
and drops the old one, so going back to an account costs another sign-in. A switch moves
logins between homes instead: the chosen account's login moves from its extra profile into
your Claude Code (``~/.claude`` — VS Code and terminals), and the login it replaces moves to
an extra profile of its own (created when the account has none). Every account stays
signed in here, so switching back is just as quick.

A switch writes what /login writes, the way Claude Code writes it: the account's entries
in the credential store (``.credentials.json``, or the macOS Keychain) and ``oauthAccount``
in ``.claude.json``, clearing the per-account caches /login clears — under Claude Code's
own locks (refresh, credential store, config), with every login on disk at every step.
Running Claude Code sessions pick the new account up on their next request, as they do
after a /login in another window. Nothing is signed out, and no login is ever copied:
Anthropic replaces the refresh token at each renewal, so of two copies only one would keep
working.
"""

from __future__ import annotations

import binascii
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import common as C
from . import live
from . import profiles as P
from . import usage as U
from .profiles import ClaudeProfile

# The account's entries in the credential store: what /login replaces (Claude Code's
# performLogout drops exactly these). Everything else there — MCP servers' logins, the
# device's identity — belongs to the folder and stays.
LOGIN_KEYS = ("claudeAiOauth", "organizationUuid", "trustedDeviceToken", "designOauth", "enterpriseGateway")
# Per-account caches in .claude.json that /login clears; Claude Code fetches them again.
CACHE_KEYS = (
    "additionalModelOptionsCache",
    "additionalModelOptionsAnsweredAt",
    "additionalModelCostsCache",
    "modelAccessCache",
    "orgModelDefaultCache",
    "cachedArtifactRoster",
    "artifactRosterDenied",
    "lastSeenOrgDefaultUpdatedAt",
    "clientDataCache",
    "clientDataCacheSlots",
    "autoCompactWindowsCache",
    "cachedUsageUtilization",
    "metricsStatusCache",
    "metricsStatusCacheByPrincipal",
    "githubWebConnectionStatusCache",
    "startupPrefetchedAt",
)
IN_USE_S = 6 * 60  # a profile whose transcripts changed this recently has a Claude Code window open
# (tries, seconds between them) for Claude Code's locks: it holds them for a moment, its
# refresh lock for the length of a renewal.
LOCK_WAITS = {"refresh": (12, 0.5), "storage": (25, 0.2), "config": (25, 0.2)}
KEYCHAIN_ACCOUNT = "claude-code-user"  # the Keychain account name Claude Code uses
_NOT_FOUND = 44  # `security` exit code for a missing Keychain item


class SwitchError(Exception):
    """A switch that could not be done. ``reason``: already, busy, changed, in_use, mismatch,
    no_login, not_here, cremind_only, signin, unsupported, unsuitable (auto mode's last check),
    auto_blocked (the monitor: auto mode would switch away at once), write. Only "write" can
    leave something changed, and then ``restored`` says whether it was all put back."""

    def __init__(self, reason: str, message: str, fix: str | None = None, restored: bool = True) -> None:
        super().__init__(message)
        self.reason = reason
        self.fix = fix
        self.restored = restored


def _sign_in_again(name: str) -> str:
    return f"open-account {name}, then /login with that account and /exit"


# ---------------------------------------------------------------- one home's files


class _Creds:
    """A home's credential store, as Claude Code keeps it: ``.credentials.json``, or on a Mac
    the Keychain, which Claude Code reads first (the file only as a fallback)."""

    def __init__(self, profile: ClaudeProfile) -> None:
        self.profile = profile
        self.file = profile.dir / ".credentials.json"
        self.service = live.LoginStore(profile).keychain_service() if sys.platform == "darwin" else None
        self.kind = "file"
        self.existed = False

    def load(self) -> dict:
        """The store's content ({} when empty). Raises SwitchError when it can't be read."""
        if self.service:
            state, data = _keychain_get(self.service)
            if state == "error":
                raise SwitchError("unsupported", "The macOS Keychain did not answer; unlock it and try again")
            if state == "found" and data is not None:
                self.kind, self.existed = "keychain", True
                return data
        self.existed = self.file.exists()
        doc = _read_doc(self.file)
        if self.service and not live.parse_login(doc):
            self.kind, self.existed = "keychain", False  # nothing in either: Claude Code saves to the Keychain
        return doc

    def save(self, data: dict | None) -> None:
        """Replace the store's content; None: it had none, so remove it."""
        if self.kind == "keychain":
            _keychain_put(self.service or "", data)
        elif data is None:
            self.file.unlink(missing_ok=True)
        else:
            live._write_private_json(self.file, data)

    def __str__(self) -> str:
        return f"the Keychain item of {self.profile.name}" if self.kind == "keychain" else str(self.file)


def _read_doc(path: Path) -> dict:
    """A JSON object file: {} when missing; retried while another process rewrites it."""
    for attempt in range(6):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return {}
        except OSError:
            text = None
        if text is not None:
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            if isinstance(data, dict):
                return data
        time.sleep(0.05 * (attempt + 1))
    raise SwitchError("busy", f"{path} could not be read; try again in a moment")


def _write_config(path: Path, data: dict | None) -> None:
    """Atomic, owner-only, indented as Claude Code writes ``.claude.json``; None removes it."""
    if data is None:
        path.unlink(missing_ok=True)
        return
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.02 * (attempt + 1))  # Windows: open in another process for a moment
        raise PermissionError(f"could not replace {path}")
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _keychain_get(service: str) -> tuple[str, dict | None]:
    """("found", data) | ("absent", None) | ("error", None)"""
    try:
        out = subprocess.run(["security", "find-generic-password", "-a", KEYCHAIN_ACCOUNT, "-w", "-s", service], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return "error", None
    if out.returncode == _NOT_FOUND:
        return "absent", None
    if out.returncode != 0:
        return "error", None
    try:
        data = json.loads(out.stdout.decode("utf-8").strip())
    except ValueError:
        return "error", None
    return ("found", data) if isinstance(data, dict) else ("error", None)


def _keychain_put(service: str, data: dict | None) -> None:
    """As Claude Code saves: ``security -i`` reading the command from stdin, the login
    hex-encoded, so it never shows on a command line."""
    if data is None:
        line = f'delete-generic-password -a "{KEYCHAIN_ACCOUNT}" -s "{service}"\n'
    else:
        payload = binascii.hexlify(json.dumps(data, separators=(",", ":")).encode("utf-8")).decode("ascii")
        line = f'add-generic-password -U -a "{KEYCHAIN_ACCOUNT}" -s "{service}" -X "{payload}"\n'
    try:
        out = subprocess.run(["security", "-i"], input=line.encode("ascii"), capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise OSError(f"the macOS Keychain did not answer: {e}") from None
    if out.returncode != 0:
        raise OSError(f"the macOS Keychain refused the change (security exited {out.returncode})")


@dataclass
class _Home:
    """What a switch reads of one home: its login store and config, as they were."""

    profile: ClaudeProfile
    creds: _Creds = field(init=False)
    data: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)
    had_config: bool = False

    def __post_init__(self) -> None:
        self.creds = _Creds(self.profile)

    def load(self) -> _Home:
        self.data = self.creds.load()
        self.had_config = self.profile.config_file.exists()
        self.config = _read_doc(self.profile.config_file)
        return self

    @property
    def account(self) -> str | None:
        o = self.config.get("oauthAccount")
        return str(o["accountUuid"]) if isinstance(o, dict) and o.get("accountUuid") else None

    @property
    def login(self) -> live.Login | None:
        return live.parse_login(self.data)

    @property
    def email(self) -> str | None:
        o = self.config.get("oauthAccount")
        return o.get("emailAddress") if isinstance(o, dict) and isinstance(o.get("emailAddress"), str) else None


def _without_login(data: dict) -> dict:
    return {k: v for k, v in data.items() if k not in LOGIN_KEYS}


def _login_entries(data: dict) -> dict:
    return {k: data[k] for k in LOGIN_KEYS if k in data}


def _with_account(config: dict, account: Any) -> dict:
    """``config`` signed in to ``account`` (None: signed out), per-account caches cleared."""
    out = {k: v for k, v in config.items() if k not in CACHE_KEYS}
    if isinstance(account, dict):
        out["oauthAccount"] = account
    else:
        out.pop("oauthAccount", None)
    return out


def recently_used(profile: ClaudeProfile, within_s: float = IN_USE_S) -> bool:
    """A Claude Code session wrote one of the profile's transcripts this recently."""
    now = time.time()
    try:
        projects = list(os.scandir(profile.projects_dir))
    except OSError:
        return False
    for proj in projects:
        try:
            if not proj.is_dir():
                continue
            with os.scandir(proj.path) as files:
                if any(f.name.endswith(".jsonl") and now - f.stat().st_mtime < within_s for f in files):
                    return True
        except OSError:
            continue
    return False


# ---------------------------------------------------------------- where every login goes


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:32].rstrip("-") or "saved-login"


def _new_profile_name(base: str, taken: set[str]) -> str:
    name, n = _slug(base), 1
    while name.lower() in taken or name.lower() in P.RESERVED or P.extra_profile(name).dir.exists():
        n += 1
        name = f"{_slug(base)[: 31 - len(str(n))].rstrip('-')}-{n}"
    return name


@dataclass
class Plan:
    account: str
    main: ClaudeProfile
    source: ClaudeProfile  # the extra profile the account's login comes from
    outgoing: str | None  # the account leaving your Claude Code (None: unknown, or no login)
    park: ClaudeProfile | None = None  # where your Claude Code's login goes
    park_is_new: bool = False
    drop_outgoing: bool = False  # that account is signed in to a profile already: this login is let go, as /login does


def plan_switch(paths: C.Paths, account_id: str, force: bool = False) -> Plan:
    """Where every login goes, decided from the files as they are (checked again under the
    locks). Raises SwitchError when the switch can't be done."""
    profiles = P.list_profiles(paths)
    main_profile = profiles[0]
    main = _Home(main_profile).load()
    homes: list[_Home] = []
    for p in profiles[1:]:
        try:
            homes.append(_Home(p).load())
        except SwitchError:
            continue  # unreadable now: not a candidate
    if main.account == account_id and main.login:
        raise SwitchError("already", "Your Claude Code already uses this account")
    if main.account and not main.login and not main.creds.existed:
        raise SwitchError("unsupported", "Claude Code keeps your login where the monitor can't move it (e.g. the Windows Credential Manager), so it can't switch here")

    holders = [h for h in homes if h.profile.kind == "extra" and h.account == account_id]
    sources = [h for h in holders if h.login]
    if not sources:
        if any(h.profile.kind == "cremind" and h.account == account_id and h.login for h in homes):
            raise SwitchError(
                "cremind_only",
                "This account's only login here is Cremind's own Claude Code login, which Cremind's Claude Code tool needs",
                "add-account <name>, then /login with this account in the window that opens; after that it switches in one step",
            )
        if holders:
            name = holders[0].profile.name
            raise SwitchError("no_login", f'Profile "{name}" names this account but holds no readable login', _sign_in_again(name))
        raise SwitchError("not_here", "No login for this account is saved on this computer", "add-account <name>, then /login with this account in the window that opens; after that it switches in one step")
    free = [h for h in sources if not recently_used(h.profile)]
    if not free and not force:
        name = sources[0].profile.name
        raise SwitchError(
            "in_use",
            f'This account is in use in a Claude Code window of profile "{name}" right now',
            "close that window (or /exit in it) and switch again; --force switches anyway, and that window then continues with the account it replaces",
        )
    source = (free or sources)[0].profile

    outgoing = main.account if main.login else None
    plan = Plan(account_id, main_profile, source, outgoing)
    if not main.login:
        return plan  # signed out: nothing to keep
    if outgoing and any(h.profile.kind == "extra" and h.account == outgoing and h.login for h in homes):
        plan.drop_outgoing = True
        return plan
    # A profile an earlier switch emptied for this account; else a new one, named after it.
    by_name = {h.profile.name: h for h in homes}
    for entry in P.read_registry(paths):
        h = by_name.get(entry["name"])
        if h and outgoing and entry.get("account") == outgoing and not h.login and h.profile.name != source.name:
            plan.park = h.profile
            return plan
    taken = {e["name"].lower() for e in P.read_registry(paths)} | {p.name.lower() for p in profiles}
    plan.park = P.extra_profile(_new_profile_name(main.email or "previous-login", taken))
    plan.park_is_new = True
    return plan


# ---------------------------------------------------------------- the switch


def _verify(source: ClaudeProfile, expected_org: str | None) -> tuple[dict | None, bool]:
    """The source's login checked with Anthropic — renewed first if it expired, as Claude Code
    would. Returns Anthropic's usage answer as a reading, and whether the check was made."""
    store = live.LoginStore(source)
    login, kind = store.read()
    if login is None:
        raise SwitchError("no_login", f'Profile "{source.name}" holds no readable login', _sign_in_again(source.name))
    renewable = kind == "file" and bool(login.refresh_token)
    for attempt in range(2):
        if not login.usable(C.now_ms()):
            if not renewable:
                return None, False  # Claude Code renews it on first use
            outcome, fresh = live.renew(store, login)
            if outcome == "dead":
                raise SwitchError("signin", f'Anthropic no longer accepts the login saved in profile "{source.name}"', _sign_in_again(source.name))
            if outcome == "busy" or fresh is None:
                raise SwitchError("busy", "Claude Code is renewing that login right now; try again in a moment")
            login = fresh
        try:
            body, org = live.fetch_usage(login.access_token)
        except live.LiveError as e:
            if e.kind == "auth" and attempt == 0 and renewable:
                login = live.Login(login.access_token, login.refresh_token, 0, login.scopes)  # revoked early: renew it
                continue
            if e.kind == "auth":
                raise SwitchError("signin", f'Anthropic no longer accepts the login saved in profile "{source.name}"', _sign_in_again(source.name)) from None
            return None, False  # no network, or Anthropic busy: the login itself may well be fine
        if org and expected_org and org != expected_org:
            raise SwitchError("mismatch", f'Profile "{source.name}" holds the login of another account than its config names', _sign_in_again(source.name))
        return U.from_usage_api(body, C.now_ms()), True
    return None, False


def switch_to(paths: C.Paths, account_id: str, *, force: bool = False, verify: bool = True, accept: Callable[[dict | None], str | None] | None = None) -> dict:
    """Make ``account_id`` the account of your Claude Code. Returns what moved where, and
    Anthropic's usage answer for the account when it was checked. Raises SwitchError.

    ``accept`` sees that answer (None when it couldn't be had) before anything moves, and
    returns why the account can't take over after all, or None — auto mode's last check."""
    plan = plan_switch(paths, account_id, force)
    expected_org = (_read_doc(plan.source.config_file).get("oauthAccount") or {}).get("organizationUuid")
    reading, verified = _verify(plan.source, expected_org) if verify else (None, False)
    if accept is not None:
        unsuitable = accept(reading)
        if unsuitable:
            raise SwitchError("unsuitable", f"It can't take over after all: {unsuitable}")

    if plan.park is not None and plan.park_is_new:
        plan.park.dir.mkdir(parents=True, exist_ok=False)
    homes = [plan.source, plan.main, *([plan.park] if plan.park else [])]
    # One order for every switch: two at once can never wait on each other.
    ordered = sorted(homes, key=lambda p: os.path.normcase(os.path.abspath(p.dir)))
    locks: list[live.DirLock] = []
    keep_new_folder = True
    try:
        for kind, make in (
            ("refresh", lambda p: live.RefreshLock(p.dir)),
            ("storage", lambda p: live.StorageLock(p.dir)),
            ("config", lambda p: live.ConfigLock(p.config_file)),
        ):
            attempts, wait_s = LOCK_WAITS[kind]
            for p in ordered:
                lock = make(p)
                if not lock.acquire(attempts, wait_s):
                    raise SwitchError("busy", "Claude Code is busy with its login files right now; try again in a moment")
                locks.append(lock)
        moved = _move(plan)
    except SwitchError as e:
        keep_new_folder = e.reason == "write" and not e.restored  # it may hold a login now
        raise
    finally:
        for lock in reversed(locks):
            lock.release()
        if plan.park is not None and plan.park_is_new and not keep_new_folder:
            shutil.rmtree(plan.park.dir, ignore_errors=True)
    warning = None
    try:
        _remember(paths, plan)
    except OSError as e:
        where = plan.park.dir if plan.park else plan.source.dir
        warning = f"The switch is done, but the profile list could not be updated ({e}); the moved login is in {where}"
    return {
        "warning": warning,
        "account": account_id,
        "email": moved["email"],
        "from": plan.outgoing,
        "fromEmail": moved["fromEmail"],
        "source": plan.source.name,
        "parkedIn": plan.park.name if plan.park else None,
        "createdProfile": plan.park_is_new,
        "dropped": plan.drop_outgoing,
        "verified": verified,
        "reading": reading,
        "touched": [p.name for p in homes],
    }


def _move(plan: Plan) -> dict:
    """Under the locks: read everything again, then write in an order that keeps every login on
    disk at every step — the outgoing one into its new place, the incoming one into your Claude
    Code, the incoming one out of its old place — and the configs after the logins."""
    src = _Home(plan.source).load()
    main = _Home(plan.main).load()
    park = _Home(plan.park).load() if plan.park else None
    if src.account != plan.account or not src.login:
        raise SwitchError("changed", f'Profile "{plan.source.name}" changed meanwhile; try again')
    if (main.account if main.login else None) != plan.outgoing or (plan.outgoing is None and main.login and not plan.park):
        raise SwitchError("changed", "Your Claude Code's account changed meanwhile; try again")
    if park is not None and park.login:
        raise SwitchError("changed", f'Profile "{park.profile.name}" was signed in meanwhile; try again')

    incoming = src.config.get("oauthAccount")
    outgoing = main.config.get("oauthAccount") if main.login else None
    steps: list[tuple[Any, dict, dict | None]] = []  # (store or config file, new, old)
    if park is not None:
        seed = park.config if park.had_config else {k: main.config[k] for k in ("hasCompletedOnboarding", "lastOnboardingVersion") if k in main.config}
        steps.append((park.creds, {**_without_login(park.data), **_login_entries(main.data)}, park.data if park.creds.existed else None))
    steps.append((main.creds, {**_without_login(main.data), **_login_entries(src.data)}, main.data if main.creds.existed else None))
    steps.append((src.creds, _without_login(src.data), src.data))
    if park is not None:
        steps.append((park.profile.config_file, _with_account(seed, outgoing), park.config if park.had_config else None))
    steps.append((plan.main.config_file, _with_account(main.config, incoming), main.config if main.had_config else None))
    steps.append((plan.source.config_file, _with_account(src.config, None), src.config if src.had_config else None))

    done: list[tuple[Any, dict, dict | None]] = []
    for target, new, old in steps:
        try:
            _put(target, new)
        except OSError as e:
            failed = []
            for t, _n, o in reversed(done):
                try:
                    _put(t, o)
                except OSError:
                    failed.append(str(t))
            if failed:
                raise SwitchError("write", f"Could not save the switch ({e}), and could not put back {', '.join(failed)}: every login is still on disk, but check both accounts", restored=False) from None
            raise SwitchError("write", f"Could not save the switch ({e}); nothing was changed") from None
        done.append((target, new, old))
    return {"email": src.email, "fromEmail": main.email if main.login else None}


def _put(target: Any, data: dict | None) -> None:
    if isinstance(target, _Creds):
        target.save(data)
    else:
        _write_config(target, data)


def _remember(paths: C.Paths, plan: Plan) -> None:
    """The registry learns which account each touched profile keeps: an emptied profile keeps
    the place of the account that left it, so that account returns there."""

    def change(entries: list[dict]) -> list[dict]:
        by_name = {e["name"].lower(): e for e in entries}
        if plan.source.name.lower() in by_name:
            by_name[plan.source.name.lower()]["account"] = plan.account
        if plan.park is not None:
            park = by_name.get(plan.park.name.lower())
            if park is None:
                park = {"name": plan.park.name, "addedAt": datetime.now().astimezone().isoformat(timespec="seconds"), "createdBy": "switch"}
                entries.append(park)
            if plan.outgoing:
                park["account"] = plan.outgoing
        return entries

    P.update_registry(paths, change)
    if plan.park is not None and plan.park_is_new:
        P.prepare_profile_dir(plan.park)
