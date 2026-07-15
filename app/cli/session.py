"""Per-terminal profile selection & on-disk token resolution for the CLI.

The `cremind` CLI runs on the same host as the server, so each profile's JWT
already lives on disk at ``<CREMIND_SYSTEM_DIR>/tokens/<profile>.token`` (written
by the setup wizard and backup-restore). This module lets the CLI resolve a
usable token without the user exporting ``CREMIND_TOKEN``:

- list the profiles that have a token file (:func:`list_profiles`),
- read a profile's token (:func:`read_token`),
- remember the chosen profile *per terminal* so later commands don't re-prompt
  (:func:`get_session_profile` / :func:`set_session_profile` /
  :func:`clear_session_profile`),
- orchestrate the whole thing (:func:`resolve_profile`), prompting via the TUI
  picker when nothing is remembered and we're on an interactive terminal.

Import discipline (see ``app/cli/main.py``): this module uses only the stdlib
plus the picker in ``app/cli/tui`` — no ``app.server`` / ``app.config`` imports —
so the slim ``pip install cremind`` stays free of server deps. The system-dir
default is duplicated here (the CLI already duplicates ``DEFAULT_SERVER``)
rather than importing ``app.config.settings``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional


# Entries in the per-terminal session map older than this are pruned on write,
# so a long-lived box doesn't accumulate stale terminal keys forever.
_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days

_TOKEN_SUFFIX = ".token"
_SESSIONS_FILENAME = "cli-sessions.json"


def system_dir() -> Path:
    """Resolve ``CREMIND_SYSTEM_DIR`` (default ``~/.cremind``).

    Duplicated from ``app.config.settings._default_system_dir`` on purpose to
    keep the slim CLI install free of server config.
    """
    raw = os.environ.get("CREMIND_SYSTEM_DIR")
    if raw:
        return Path(raw)
    return Path(os.path.expanduser("~")) / ".cremind"


def tokens_dir() -> Path:
    return system_dir() / "tokens"


def list_profiles() -> list[str]:
    """Profiles that have a usable token file, sorted.

    Lists ``<system_dir>/tokens/*.token`` stems, excluding hidden profiles
    (names starting with ``__``, matching the server convention). A profile
    with no token file can't be authenticated as from the CLI anyway, so this
    is exactly the set the picker should offer. Returns ``[]`` if the directory
    is absent or unreadable.
    """
    try:
        entries = list(tokens_dir().iterdir())
    except (FileNotFoundError, NotADirectoryError, OSError):
        return []
    names = [
        entry.name[: -len(_TOKEN_SUFFIX)]
        for entry in entries
        if entry.is_file() and entry.name.endswith(_TOKEN_SUFFIX)
    ]
    return sorted(name for name in names if name and not name.startswith("__"))


def read_token(profile: str) -> Optional[str]:
    """Read ``<system_dir>/tokens/<profile>.token``; ``None`` if missing/empty."""
    if not profile:
        return None
    path = tokens_dir() / f"{profile}{_TOKEN_SUFFIX}"
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    return token or None


def has_token(profile: str) -> bool:
    return read_token(profile) is not None


# ── per-terminal session state ─────────────────────────────────────────────


# Ancestor process names treated as "the terminal's shell" when walking the
# Windows process tree (lower-cased, with extension). The nearest such ancestor
# is a stable per-terminal anchor even when an intermediate wrapper (uv, npx,
# a launcher) sits between it and this process.
_WINDOWS_SHELL_NAMES = {
    "powershell.exe",
    "pwsh.exe",
    "cmd.exe",
    "bash.exe",
    "sh.exe",
    "zsh.exe",
    "fish.exe",
    "nu.exe",
    "wt.exe",
    "windowsterminal.exe",
    "conemu.exe",
    "conemu64.exe",
}


def session_key() -> str:
    """Best-effort stable identifier for *this terminal session*.

    Layered so it survives a wrapper process (e.g. ``uv run``) that changes the
    immediate parent PID:

    1. ``WT_SESSION``      — Windows Terminal, one GUID per tab.
    2. ``TERM_SESSION_ID`` — macOS Terminal.app / iTerm2.
    3. ``os.getsid(0)``    — POSIX session id (the controlling shell), shared by
       every process in the terminal regardless of intermediate wrappers.
    4. Windows: the PID of the nearest ancestor process that is a shell
       (PowerShell / cmd / bash …), found by walking the process tree — stable
       across ``uv run`` invocations that would otherwise change the parent PID.
    5. ``os.getppid()``    — last-resort fallback.

    When none of (1)–(4) apply, the parent-PID fallback can drift between
    ``uv run`` invocations, which just means the picker shows again — safe,
    never wrong.
    """
    for env_var in ("WT_SESSION", "TERM_SESSION_ID"):
        val = os.environ.get(env_var)
        if val:
            return f"{env_var}:{val}"
    getsid = getattr(os, "getsid", None)
    if getsid is not None:
        try:
            return f"sid:{getsid(0)}"
        except OSError:
            pass
    if sys.platform == "win32":
        win_key = _windows_shell_ancestor_key()
        if win_key:
            return win_key
    return f"ppid:{os.getppid()}"


def _windows_shell_ancestor_key() -> Optional[str]:
    """`win:<shellname>:<pid>` for the nearest shell ancestor, or ``None``.

    Snapshots the process table via the Toolhelp API and walks parent links from
    this process upward. Entirely best-effort — any failure returns ``None`` and
    the caller falls back to the parent PID.
    """
    try:
        import ctypes
        from ctypes import wintypes

        class _ProcessEntry32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # SNAPPROCESS
        entry = _ProcessEntry32()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32)
        procs: dict[int, tuple[int, str]] = {}
        try:
            if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
                return None
            while True:
                name = entry.szExeFile.decode("ascii", "replace").lower()
                procs[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), name)
                if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    break
        finally:
            kernel32.CloseHandle(snapshot)

        seen: set[int] = set()
        pid = os.getpid()
        while pid and pid not in seen and len(seen) < 64:
            seen.add(pid)
            info = procs.get(pid)
            if not info:
                break
            ppid = info[0]
            parent = procs.get(ppid)
            if parent and parent[1] in _WINDOWS_SHELL_NAMES:
                return f"win:{parent[1]}:{ppid}"
            pid = ppid
        return None
    except Exception:
        return None


def _sessions_file() -> Path:
    return system_dir() / _SESSIONS_FILENAME


def _load_sessions() -> dict:
    try:
        raw = _sessions_file().read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_sessions(sessions: dict) -> None:
    path = _sessions_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sessions, indent=2), encoding="utf-8")
    except OSError:
        # Best-effort: an unwritable system dir just means no stickiness, not a
        # hard failure of the command the user actually ran.
        pass


def get_session_profile() -> Optional[str]:
    """The profile remembered for this terminal, or ``None``.

    Drops the entry (and returns ``None``) if the profile no longer has a token
    file, so a deleted profile transparently re-prompts on the next command.
    """
    entry = _load_sessions().get(session_key())
    profile = entry.get("profile") if isinstance(entry, dict) else None
    if not profile:
        return None
    if not has_token(profile):
        clear_session_profile()
        return None
    return profile


def set_session_profile(profile: str) -> None:
    """Remember ``profile`` for this terminal (prunes expired entries)."""
    now = time.time()
    sessions = _load_sessions()
    pruned = {
        key: val
        for key, val in sessions.items()
        if isinstance(val, dict)
        and (now - float(val.get("updated_at") or 0)) < _SESSION_TTL_SECONDS
    }
    pruned[session_key()] = {"profile": profile, "updated_at": now}
    _save_sessions(pruned)


def clear_session_profile() -> None:
    """Forget this terminal's remembered profile."""
    sessions = _load_sessions()
    if sessions.pop(session_key(), None) is not None:
        _save_sessions(sessions)


# ── resolution ─────────────────────────────────────────────────────────────


def resolve_profile(explicit: Optional[str], *, interactive: bool) -> Optional[str]:
    """Resolve which profile the CLI should act as, and persist the choice.

    Precedence:
      1. ``explicit`` (the ``--profile`` flag / ``CREMIND_PROFILE`` env) — sticky
         when it has a token file.
      2. the terminal's remembered profile.
      3. the sole profile, if there's exactly one on disk (unambiguous, so this
         applies even non-interactively).
      4. interactive TTY with several profiles: the TUI picker.

    Returns ``None`` when nothing can be resolved (no profiles on disk, several
    profiles but no TTY, or the picker was cancelled). Never raises for a missing
    token dir — callers surface the "no token" error themselves.
    """
    if explicit:
        if has_token(explicit):
            set_session_profile(explicit)
        return explicit

    remembered = get_session_profile()
    if remembered:
        return remembered

    profiles = list_profiles()
    if len(profiles) == 1:
        set_session_profile(profiles[0])
        return profiles[0]
    if not profiles or not interactive:
        return None

    from app.cli.tui.profile_picker import pick_profile

    chosen = pick_profile(profiles)
    if chosen:
        set_session_profile(chosen)
    return chosen
