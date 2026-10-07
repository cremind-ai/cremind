"""Claude Code profiles: separate config directories (``CLAUDE_CONFIG_DIR``), each signed
in to its own account, so several accounts stay signed in at once.

"main" is the Claude Code configuration you work in; "cremind" is this Cremind profile's
own Claude Code home; the rest are extra profiles, which keep the other accounts' logins —
so the monitor can track them, and a switch (``switch.py``) can move any of them into the
main profile in one step. Claude Code's documented way to use multiple accounts
(code.claude.com/docs/en/authentication).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import common as C

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$", re.I)
RESERVED = {"main", "cremind"}


@dataclass(frozen=True)
class ClaudeProfile:
    name: str
    dir: Path
    kind: str  # "main" | "cremind" | "extra"
    config_file: Path

    @property
    def main(self) -> bool:
        return self.kind == "main"

    @property
    def projects_dir(self) -> Path:
        return self.dir / "projects"

    @property
    def settings_file(self) -> Path:
        return self.dir / "settings.json"

    @property
    def backups_dir(self) -> Path:
        return self.dir / "backups"


def main_profile() -> ClaudeProfile:
    home, config = C.main_claude_home()
    return ClaudeProfile("main", home, "main", config)


def cremind_profile() -> ClaudeProfile | None:
    """This Cremind profile's own Claude Code home, once something has been put there."""
    home = C.cremind_claude_home()
    if home is None or not home.is_dir():
        return None
    return ClaudeProfile("cremind", home, "cremind", home / ".claude.json")


def extra_profile(name: str) -> ClaudeProfile:
    home = C.accounts_root() / name
    return ClaudeProfile(name, home, "extra", home / ".claude.json")


def read_registry(paths: C.Paths) -> list[dict]:
    """Extra profiles registered with ``add-account`` (or created by a switch):
    ``[{name, addedAt, account?, createdBy?}]`` — ``account`` is the account whose place the
    profile keeps once a switch has moved its login out.

    Folders are always derived from the name, never read from the file, so the registry
    cannot point the monitor anywhere outside this profile's accounts folder.
    """
    data = C.read_json(paths.profiles)
    entries = data.get("profiles") if isinstance(data, dict) else None
    out: list[dict] = []
    seen: set[str] = set()
    for e in entries if isinstance(entries, list) else []:
        name = e.get("name") if isinstance(e, dict) else None
        if not isinstance(name, str) or not NAME_RE.match(name) or name.lower() in RESERVED or name.lower() in seen:
            continue
        seen.add(name.lower())
        row = {"name": name, "addedAt": e.get("addedAt")}
        for k in ("account", "createdBy"):
            if isinstance(e.get(k), str) and e[k]:
                row[k] = e[k]
        out.append(row)
    return out


def write_registry(paths: C.Paths, entries: list[dict]) -> None:
    rows = []
    for e in entries:
        row = {"name": e["name"], "dir": str(extra_profile(e["name"]).dir), "addedAt": e.get("addedAt")}
        row.update({k: e[k] for k in ("account", "createdBy") if e.get(k)})
        rows.append(row)
    C.write_json_atomic(paths.profiles, {"profiles": rows}, indent=2)


def update_registry(paths: C.Paths, change: Callable[[list[dict]], list[dict]]) -> list[dict]:
    """Read, change and write the registry under a lock, as the monitor and the CLI may both
    write it."""
    lock = paths.profiles.with_name(paths.profiles.name + ".lock")
    paths.data.mkdir(parents=True, exist_ok=True)
    held = False
    for attempt in range(50):
        try:
            os.mkdir(lock)
            held = True
            break
        except FileExistsError:
            try:
                if time.time() - os.stat(lock).st_mtime > 10:
                    os.rmdir(lock)  # its holder died
                    continue
            except OSError:
                pass
            time.sleep(0.05 + 0.01 * attempt)
    try:
        entries = change(read_registry(paths))
        write_registry(paths, entries)
        return entries
    finally:
        if held:
            try:
                os.rmdir(lock)
            except OSError:
                pass


def prepare_profile_dir(profile: ClaudeProfile) -> None:
    """A new extra profile's folder, with this skill's status line in its settings."""
    profile.dir.mkdir(parents=True, exist_ok=True)
    settings = C.read_json(profile.settings_file)
    settings = settings if isinstance(settings, dict) else {}
    if not settings.get("statusLine"):
        settings["statusLine"] = {"type": "command", "command": C.statusline_command(), "padding": 0}
        C.write_json_atomic(profile.settings_file, settings, indent=2)


def list_profiles(paths: C.Paths) -> list[ClaudeProfile]:
    own = cremind_profile()
    return [main_profile(), *([own] if own else []), *(extra_profile(e["name"]) for e in read_registry(paths))]


def find_profile(paths: C.Paths, name: str) -> ClaudeProfile | None:
    return next((p for p in list_profiles(paths) if p.name.lower() == str(name).lower()), None)


def signed_in_account(profile: ClaudeProfile) -> dict | None:
    """Account signed in to a profile, from its config file (no login tokens are read)."""
    cfg = C.read_json(profile.config_file)
    return C.pick_account(cfg.get("oauthAccount")) if isinstance(cfg, dict) else None


def workspace_of(profile: ClaudeProfile) -> Path:
    """A folder for Claude Code sessions opened in a profile, so they don't run in the
    config folder itself."""
    return Path.home() if profile.main else profile.dir / "workspace"


def _env_for(profile: ClaudeProfile) -> dict[str, str]:
    env = dict(os.environ)
    home, _config = C.main_claude_home()
    if profile.main and home == Path.home() / ".claude":
        env.pop("CLAUDE_CONFIG_DIR", None)  # Claude Code's own default location
    else:
        env["CLAUDE_CONFIG_DIR"] = str(profile.dir)
    return env


def launch_hint(profile: ClaudeProfile) -> str:
    """How to start Claude Code in a profile by hand."""
    env = _env_for(profile)
    if "CLAUDE_CONFIG_DIR" not in env:
        return "claude"
    if sys.platform == "win32":
        return f'set "CLAUDE_CONFIG_DIR={profile.dir}" && claude'
    return f'CLAUDE_CONFIG_DIR="{profile.dir}" claude'


def open_terminal(profile: ClaudeProfile) -> bool:
    """Opens a new console window running Claude Code in the profile (Windows, on the
    computer Cremind runs on), so the user can sign in or run /usage. False where that
    isn't supported."""
    if sys.platform != "win32" or not NAME_RE.match(profile.name) or C.in_container():
        return False
    cwd = workspace_of(profile)
    cwd.mkdir(parents=True, exist_ok=True)
    # `start` needs its window title quoted; the name is restricted to [a-z0-9_-].
    subprocess.Popen(
        f'cmd.exe /d /c start "Claude - {profile.name}" cmd.exe /k claude',
        env=_env_for(profile),
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        close_fds=True,
    )
    return True
