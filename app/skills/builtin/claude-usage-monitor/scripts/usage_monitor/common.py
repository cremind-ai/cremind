"""Paths, configuration and small file helpers shared by the monitor, the CLI and the
status line bridge.

Which Claude Code homes are read:

- **main** — the server's shared Claude Code home, the same one Cremind's Claude Code
  tool falls back to: ``~/.claude`` on a native install, the System Directory's
  ``coding-cli/claude`` in a container. Outside Cremind, ``$CLAUDE_CONFIG_DIR``.
- **cremind** — this Cremind profile's own Claude Code home (the ``CLAUDE_CONFIG_DIR``
  Cremind gives its shells), when the profile has signed in there.
- **extra profiles** — one folder per additional account, under this Cremind profile's
  own ``coding-cli/claude-accounts`` (deleted with the profile, like its other logins).

None of these is a path a user types in, so one Cremind profile can never point the
monitor at another profile's folders.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
SKILL_DIR = SCRIPTS_DIR.parent
SKILL_NAME = "claude-usage-monitor"
EVENTS_DIR = SKILL_DIR / "events"
DASHBOARD_DIR = SCRIPTS_DIR / "dashboard"
ENV_FILE = SCRIPTS_DIR / ".env"
LOCK_FILE = SCRIPTS_DIR / ".listener.lock"
HEARTBEAT_FILE = SCRIPTS_DIR / ".listener_heartbeat"
STATUSLINE_SCRIPT = SCRIPTS_DIR / "statusline.py"

DEFAULT_PORT = 7337
PORT_TRIES = 11  # the configured port, then the next ten


class Paths:
    """Everything the monitor stores, under one data folder (safe to delete)."""

    def __init__(self, data: Path) -> None:
        self.data = data
        self.inbox = data / "inbox"  # one status line snapshot per Claude Code session
        self.state = data / "state.json"
        self.ledger = data / "ledger.json"  # per-account spend + how far each transcript was read
        self.profiles = data / "profiles.json"  # extra Claude Code profiles (add-account)
        self.summary = data / "summary.json"  # read by the status line for "next account"
        self.backups = data / "backups"
        self.runtime = data / "runtime.json"  # pid + dashboard URL of the running monitor


def default_paths() -> Paths:
    override = os.environ.get("CLAUDE_USAGE_MONITOR_DATA", "").strip()
    return Paths(Path(override) if override else SCRIPTS_DIR / ".monitor")


def owner_label(paths: Paths) -> str:
    """Who runs a monitor, for people: its Cremind profile, or its data folder."""
    profile = os.environ.get("CREMIND_PROFILE", "").strip()
    return f'Cremind profile "{profile}"' if profile else str(paths.data)


def now_ms() -> float:
    return time.time() * 1000


# ---------------------------------------------------------------- configuration


def load_env(env_file: Path = ENV_FILE) -> dict[str, str]:
    """``scripts/.env``, which Cremind writes from Settings → claude-usage-monitor."""
    env: dict[str, str] = {}
    try:
        text = env_file.read_text(encoding="utf-8-sig")
    except OSError:
        return env
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def dashboard_port(env: dict[str, str]) -> int:
    # Never $PORT: a listener inherits the Cremind server's environment, where PORT is
    # the server's own loopback port.
    try:
        port = int(float(env.get("DASHBOARD_PORT") or DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT
    return port if 1 <= port <= 65535 - PORT_TRIES else DEFAULT_PORT


def in_container() -> bool:
    """Docker or Kubernetes, decided the way Cremind decides it."""
    if os.environ.get("INSTALL_MODE", "").strip().lower() in ("docker", "kubernetes"):
        return True
    return Path("/.dockerenv").exists() or bool(os.environ.get("KUBERNETES_SERVICE_HOST"))


def under_cremind() -> bool:
    return bool(os.environ.get("CREMIND_PROFILE", "").strip())


def main_claude_home() -> tuple[Path, Path]:
    """(home, ``.claude.json``) of the main profile — see the module docstring.

    With ``CLAUDE_CONFIG_DIR`` unset Claude Code keeps its config beside the home, as
    ``~/.claude.json``; with it set, inside.
    """
    override = os.environ.get("CLAUDE_USAGE_MONITOR_MAIN_HOME", "").strip()
    if override:
        home = Path(override)
        return home, home / ".claude.json"
    stated = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if stated and not under_cremind():
        home = Path(stated).resolve()
        return home, home / ".claude.json"
    sysdir = os.environ.get("CREMIND_SYSTEM_DIR", "").strip()
    if sysdir and in_container():
        home = Path(sysdir) / "coding-cli" / "claude"
        return home, home / ".claude.json"
    return Path.home() / ".claude", Path.home() / ".claude.json"


def cremind_claude_home() -> Path | None:
    """This Cremind profile's own Claude Code home, or None outside Cremind."""
    stated = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if not stated or not under_cremind():
        return None
    home = Path(stated)
    return None if _same_path(home, main_claude_home()[0]) else home


def accounts_root() -> Path:
    """Where ``add-account`` creates the extra profiles."""
    override = os.environ.get("CLAUDE_USAGE_MONITOR_ACCOUNTS_DIR", "").strip()
    if override:
        return Path(override)
    sysdir = os.environ.get("CREMIND_SYSTEM_DIR", "").strip()
    profile = os.environ.get("CREMIND_PROFILE", "").strip()
    if sysdir and profile:
        return Path(sysdir) / profile / "coding-cli" / "claude-accounts"
    return Path.home() / ".claude-accounts"


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# ---------------------------------------------------------------- files


def read_json(path: Path) -> Any:
    """Parsed JSON, or None when the file is missing, unreadable or mid-write."""
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def write_json_atomic(path: Path, data: Any, indent: int | None = None) -> None:
    """Write through a temp file + rename so readers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    text = json.dumps(data, indent=indent, ensure_ascii=False, separators=None if indent else (",", ":"))
    tmp.write_text(text + ("\n" if indent else ""), encoding="utf-8")
    for attempt in range(9):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # On Windows the target can be briefly locked by another process.
            if attempt == 8:
                break
            time.sleep(0.015 * (attempt + 1))
    try:
        tmp.unlink()
    except OSError:
        pass
    raise PermissionError(f"could not replace {path}")


def js_safe(value: Any) -> Any:
    """JSON the way JavaScript writes it: a non-finite number becomes null."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: js_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [js_safe(v) for v in value]
    return value


# ---------------------------------------------------------------- Claude Code accounts


def pick_account(o: Any) -> dict | None:
    """The non-secret account metadata Claude Code keeps in its config. Login tokens live
    elsewhere (``.credentials.json``); only ``live.py`` reads them, to ask Anthropic."""
    if not isinstance(o, dict) or not o.get("accountUuid"):
        return None
    return {
        "id": str(o["accountUuid"]),
        "email": o.get("emailAddress") or None,
        "name": o.get("displayName") or o.get("fullName") or None,
        "org": o.get("organizationName") or None,
        "orgId": o.get("organizationUuid") or None,
        "orgType": o.get("organizationType") or None,
        "tier": o.get("organizationRateLimitTier") or o.get("userRateLimitTier") or None,
    }


def config_file_for(env: dict[str, str] | None = None) -> Path:
    """``.claude.json`` of the Claude Code session that runs the status line."""
    env = os.environ if env is None else env
    stated = (env.get("CLAUDE_CONFIG_DIR") or "").strip()
    return Path(stated) / ".claude.json" if stated else Path.home() / ".claude.json"


def read_active_account(env: dict[str, str] | None = None) -> dict | None:
    cfg = read_json(config_file_for(env))
    return pick_account(cfg.get("oauthAccount")) if isinstance(cfg, dict) else None


def plan_label(a: dict | None) -> str:
    """``default_claude_max_20x`` -> ``Max 20x``"""
    import re

    tier = str((a or {}).get("tier") or "")
    m = re.search(r"max_(\d+)x", tier, re.I)
    if m:
        return f"Max {m.group(1)}x"
    org_type = (a or {}).get("orgType")
    if re.search("max", tier, re.I) or org_type == "claude_max":
        return "Max"
    if re.search("pro", tier, re.I) or org_type == "claude_pro":
        return "Pro"
    return str(org_type).removeprefix("claude_").replace("_", " ") if org_type else ""


# ---------------------------------------------------------------- the status line


def _python_for_statusline() -> str:
    """The interpreter Claude Code runs the bridge with. The base interpreter rather than
    ``uv run``'s cached environment, which ``uv cache prune`` may delete."""
    base = getattr(sys, "_base_executable", None)
    return base if base and Path(base).exists() else sys.executable


def statusline_command() -> str:
    """Command line for Claude Code's ``statusLine`` (forward slashes: on Windows it runs
    in Git Bash). ``-I -S``: the bridge needs only the standard library, and starts faster."""
    py = _python_for_statusline().replace("\\", "/")
    script = str(STATUSLINE_SCRIPT).replace("\\", "/")
    return f'"{py}" -I -S "{script}"'


def is_our_statusline(sl: Any) -> bool:
    """Whether a ``statusLine`` settings entry points at this skill's bridge."""

    def norm(p: str) -> str:
        return p.replace("\\", "/").lower()

    return isinstance(sl, dict) and isinstance(sl.get("command"), str) and norm(str(STATUSLINE_SCRIPT)) in norm(sl["command"])
