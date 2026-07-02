# Skill Templates

Copy-adapt starting points. Every block has `# EDIT:` / `TODO(author)` markers
where you fill in specifics. Adapt them — don't paste verbatim. Read `spec.md`
for the frontmatter contract and `events.md` for the event/listener contract.

**Quote the `description`** in double quotes if it contains a colon-space (`: `)
or other YAML special character — an unquoted `:` makes YAML read a nested key,
the frontmatter fails to parse, and the skill silently fails to load. See
`spec.md` → Parsing rules.

Contents:
- A. Minimal `SKILL.md` (instructions-only skill)
- B. Full `SKILL.md` (env vars + events + listener)
- C. `scripts/event_listener.py` (polling listener with a correct event writer)
- D. `scripts/__main__.py` (CLI skeleton)
- E. `scripts/.gitignore`

---

## A. Minimal SKILL.md (instructions-only)

The smallest useful skill: no scripts, no events. The body IS the capability —
Cremind injects it into the conversation and the agent follows it.

````markdown
---
name: my-skill
description: EDIT — one or two sentences on what this does and when to load it. This is what makes the model choose it, so name the capability and the trigger. (Quote the whole value in double quotes if it contains a colon-space.)
---

# my-skill

**Purpose:** EDIT — one line.

## Instructions
EDIT — the steps the agent should follow when this skill is loaded. Be concrete
and self-contained; the agent has only what's written here.

## Examples
EDIT — 1–3 worked examples of input → what the agent should do.
````

---

## B. Full SKILL.md (config + events + listener)

For a skill with configuration, event types, and a background listener. Keep the
body tight (the whole thing is injected on load); push depth to a `references/`
file if it grows past ~200 lines.

````markdown
---
name: my-skill
description: EDIT — capabilities and when to use, including that it can react to <event> automatically.
metadata:
  environment_variables:
    - name: API_BASE_URL
      description: Base URL of the service
      required: true
      type: string
      default: ""
    - name: API_TOKEN
      description: API token
      required: true
      secret: true
      type: string
      default: ""
  events:
    event_type:
      - name: new_item          # EDIT — lowercase snake_case; also the folder name
        description: A new item appeared upstream
  long_running_app:
    command: uv run scripts/event_listener.py
    description: Persistent listener that emits my-skill events.
---

# my-skill

**Purpose:** EDIT — one or two lines.

## Setup
Configuration is read from `scripts/.env`, populated from **Settings → my-skill**.
Required variables: `API_BASE_URL`, `API_TOKEN`. Do not ask the user to set env
vars in chat — point them at Settings.

## CLI Commands
Run `uv run scripts/__main__.py <subcommand>`. Output is JSON.

| Subcommand | Required | Optional |
|---|---|---|
| `status` | — | — |
| `list` | — | `--limit` |
| EDIT | | |

## Examples
```bash
uv run scripts/__main__.py status
uv run scripts/__main__.py list --limit 5
```

## Event listener
```bash
uv run scripts/event_listener.py
```
Behavior:
- **Baseline on first run:** records the current cursor; emits nothing for
  pre-existing items.
- **Live:** writes new items to `events/new_item/<YYYY-MM-DDTHH-MM-SS> <label>.md`.
- **State:** `scripts/.listener_state.json` (gitignored). Stops on SIGINT/SIGTERM.

### Event markdown schema
```markdown
---
id: "EDIT"
title: "EDIT"
url: "EDIT"
event_type: "new_item"
received_at: "2026-07-02T09:00:05+07:00"
---

EDIT — human-readable body describing the event.
```

## Troubleshooting
- EDIT — common failure → fix.

## Module layout
```
my-skill/
├── SKILL.md
├── events/new_item/          # markdown drop-zone (.gitkeep shipped)
└── scripts/
    ├── .env                  # from Settings (gitignored)
    ├── __main__.py           # CLI entry
    └── event_listener.py     # listener entry
```
````

---

## C. scripts/event_listener.py (polling listener)

A complete, runnable polling listener. The lock, state handling, signal
handling, filename sanitizer, and atomic event writer are correct as-is — **leave
them alone**. Customize only `poll_for_new_items()` and the frontmatter you put
in each event. For a push source (webhook/socket), replace the poll loop with
your connection but keep `write_event()` and the state/lock helpers.

```python
# /// script
# requires-python = ">=3.11"
# dependencies = []
# EDIT: add libraries your source needs, e.g. "httpx"
# ///
"""Polling event listener for my-skill.

Reads config from scripts/.env, polls the upstream source on an interval, and
writes new items as markdown into events/<event_type>/. Baselines on first run
(emits nothing for pre-existing items), dedupes, single-instance, atomic writes.
"""

from __future__ import annotations

import errno
import json
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
EVENTS_DIR = SKILL_DIR / "events"
STATE_PATH = SCRIPTS_DIR / ".listener_state.json"
LOCK_PATH = SCRIPTS_DIR / ".listener.lock"

POLL_INTERVAL = 60  # seconds; EDIT or read from .env

_shutdown = False


# --- config -----------------------------------------------------------------

def load_env() -> dict[str, str]:
    """Minimal .env reader (KEY=VALUE per line). Cremind writes this file."""
    env: dict[str, str] = {}
    if not (SCRIPTS_DIR / ".env").exists():
        return env
    for line in (SCRIPTS_DIR / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip().strip('"').strip("'")
    return env


# --- single instance --------------------------------------------------------

def acquire_lock() -> None:
    try:
        fd = os.open(str(LOCK_PATH), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except OSError as e:
        if e.errno == errno.EEXIST:
            # Stale lock? If the recorded pid is gone, take it over.
            try:
                old = int(LOCK_PATH.read_text() or "0")
            except (OSError, ValueError):
                old = 0
            if old and not _pid_alive(old):
                LOCK_PATH.unlink(missing_ok=True)
                return acquire_lock()
            print("another listener instance is running; exiting", file=sys.stderr)
            raise SystemExit(0)
        raise
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))


def release_lock() -> None:
    LOCK_PATH.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)          # POSIX: signal 0 probes existence
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True              # exists but not ours, or Windows quirk
    return True


# --- state ------------------------------------------------------------------

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, STATE_PATH)   # atomic


# --- event writing (correct; do not change) ---------------------------------

_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _sanitize(label: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", label or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".")[:100].rstrip()
    if not cleaned:
        cleaned = "event"
    if cleaned.lower() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned


def write_event(event_type: str, label: str, frontmatter: dict, body: str) -> Path:
    """Atomically write one event file into events/<event_type>/.

    Forces event_type/received_at into the frontmatter so the file always
    satisfies the contract.
    """
    folder = EVENTS_DIR / event_type
    folder.mkdir(parents=True, exist_ok=True)
    fm = dict(frontmatter)
    fm["event_type"] = event_type
    fm.setdefault("received_at", datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"))
    fm_lines = "\n".join(f'{k}: {json.dumps(v)}' for k, v in fm.items())
    content = f"---\n{fm_lines}\n---\n\n{body.strip()}\n"

    base = f"{datetime.now().strftime('%Y-%m-%dT%H-%M-%S')} {_sanitize(label)}"
    attempt = 0
    while True:
        name = f"{base}.md" if attempt == 0 else f"{base} ({attempt + 1}).md"
        path = folder / name
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except OSError as e:
            if e.errno == errno.EEXIST:
                attempt += 1
                continue
            raise
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        return path


# --- the part you customize -------------------------------------------------

def poll_for_new_items(env: dict[str, str], cursor):
    """Return (items, new_cursor). EDIT this for your source.

    `items` is a list of dicts you turn into events. `cursor` is whatever you
    stored last run (an id, timestamp, page token, ...). On the very first run
    `cursor` is None — return ([], <current cursor>) to BASELINE (emit nothing
    for pre-existing items).
    """
    # EDIT: call your API/source here.
    # Example shape:
    #   resp = httpx.get(env["API_BASE_URL"] + "/items", headers=...)
    #   items = [i for i in resp.json()["items"] if i["id"] > (cursor or 0)]
    #   new_cursor = max([i["id"] for i in items], default=cursor)
    #   return items, new_cursor
    return [], cursor


def item_to_event(item: dict) -> tuple[str, str, dict, str]:
    """Map one source item to (event_type, label, frontmatter, body). EDIT."""
    return (
        "new_item",                       # EDIT: one of your declared event types
        item.get("title", "item"),        # label -> filename
        {"id": str(item.get("id", "")), "title": item.get("title", ""),
         "url": item.get("url", "")},     # EDIT: domain frontmatter fields
        item.get("body", ""),             # EDIT: human-readable body
    )


# --- main loop --------------------------------------------------------------

def _handle_signal(signum, frame) -> None:
    global _shutdown
    _shutdown = True


def main() -> int:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass

    acquire_lock()
    try:
        env = load_env()
        state = load_state()
        first_run = "cursor" not in state
        cursor = state.get("cursor")

        while not _shutdown:
            try:
                items, cursor = poll_for_new_items(env, cursor)
                if not first_run:
                    for item in items:
                        write_event(*item_to_event(item))
                # First run baselines: advance the cursor, emit nothing.
                first_run = False
                state["cursor"] = cursor
                save_state(state)
            except Exception as exc:  # keep the daemon alive across transient errors
                print(f"poll error: {exc}", file=sys.stderr)

            # Sleep in small steps so shutdown is responsive.
            for _ in range(POLL_INTERVAL):
                if _shutdown:
                    break
                time.sleep(1)
        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
```

---

## D. scripts/__main__.py (CLI skeleton)

The on-demand actions the agent runs while a skill is loaded. Output JSON so the
agent can parse it. Config comes from `scripts/.env`.

```python
# /// script
# requires-python = ">=3.11"
# dependencies = []
# EDIT: add libraries you need
# ///
"""CLI for my-skill. Run: uv run scripts/__main__.py <subcommand>."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    envf = SCRIPTS_DIR / ".env"
    if envf.exists():
        for line in envf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def cmd_status(args, env) -> dict:
    return {"ok": True, "configured": bool(env.get("API_BASE_URL"))}  # EDIT


def cmd_list(args, env) -> dict:
    # EDIT: call your source, return JSON-serializable data.
    return {"items": [], "limit": args.limit}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="my-skill")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    p_list = sub.add_parser("list")
    p_list.add_argument("--limit", type=int, default=10)
    # EDIT: add more subcommands.

    args = parser.parse_args(argv[1:])
    env = load_env()
    handlers = {"status": cmd_status, "list": cmd_list}
    try:
        result = handlers[args.command](args, env)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

---

## E. scripts/.gitignore

Keep secrets and runtime state out of git (and out of any shared package).

```gitignore
.env
*token*.json
.listener_state.json*
.listener.lock
.listener_heartbeat
__pycache__/
*.pyc
```
