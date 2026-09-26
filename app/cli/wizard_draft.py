"""The `cremind profile wizard` draft — answers on disk between steps.

The wizard exists because the agent runs one command per conversational turn:
it asks the user for a step's inputs, ends its turn, and comes back with the
answers in the next one. Nothing survives in the process between those turns, so
the answers live in a file until ``finish`` posts them as a single
``/api/config/setup`` body.

Where the file lives matters twice over. It is keyed by the *acting* profile —
``<system dir>/<acting>/cli-wizards/<target>.json`` — because it is that
profile's work in progress, not the target's (which may not exist yet), and
Cremind's rule is that per-profile state is keyed by whoever is acting. And it
holds plaintext API keys and bot tokens until ``finish``, so it is written 0600
into a directory ``/api/files`` refuses to serve at all
(``_CREDENTIAL_DIR_NAMES``), and ``status`` masks every secret-shaped value it
prints.

Deliberately stdlib-only apart from :mod:`app.cli.session`: everything under
``app/cli`` has to keep working in the slim ``pip install cremind``, which
ships no server code.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from app.cli import session

#: The steps, in the order the wizard asks them. Mirrors the per-profile steps
#: of the web Setup Wizard (LLM Providers → Tools → Memory → Channels).
STEPS: tuple[str, ...] = ("llm", "tools", "memory", "channels")

STATUS_PENDING = "pending"
STATUS_SET = "set"
STATUS_SKIPPED = "skipped"

#: Draft schema version. Bumped only if the on-disk shape changes
#: incompatibly; ``load`` treats anything it does not recognise as unusable
#: rather than guessing.
DRAFT_VERSION = 1

#: A draft this old is treated as absent. Someone who abandoned a wizard a week
#: ago is not coming back to it, and the file holds an API key.
DRAFT_TTL_SECONDS = 7 * 24 * 60 * 60

_DRAFTS_DIRNAME = "cli-wizards"
_DRAFT_SUFFIX = ".json"
_RESULT_SUFFIX = ".result.json"

# Same rule the server applies (app/api/config.py). Duplicated rather than
# imported because ``app.cli`` must not import ``app.api``; the wizard is
# refusing a name the server would refuse anyway, one round trip earlier.
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9_-]+$")

# Names a NEW profile may not take — mirrors ``RESERVED_PROFILE_NAMES`` in
# app/cremind_documents/paths.py (the manual's ``shared`` / ``cli`` scopes, and
# ``workspaces``, the folder holding every profile's working directory).
# Spelled out for the same reason as the pattern above; a test pins the two
# equal.
_RESERVED_PROFILE_NAMES = frozenset({"shared", "cli", "workspaces"})

#: Values whose key looks like a credential are never printed by ``status``.
_SECRET_KEY_RE = re.compile(
    r"api_key|setup_token|oauth_token|bearer_token|service_account"
    r"|secret|password|passcode|token|hash",
    re.IGNORECASE,
)

MASK = "***"


class DraftError(Exception):
    """A draft exists but cannot be used."""


def validate_profile_name(name: str) -> str:
    """Return ``name`` if it is a legal profile name, else raise ``ValueError``.

    Also the path guard: the name becomes a filename, so anything with a
    separator or a ``..`` in it must never get that far.
    """
    if not name:
        raise ValueError("a profile name is required")
    if not _PROFILE_NAME_RE.match(name):
        raise ValueError(
            f"invalid profile name {name!r}: use lowercase letters, numbers, "
            "hyphens and underscores only"
        )
    if len(name) > 64:
        raise ValueError("profile name must be 64 characters or less")
    # The server refuses these too (``valid_profile_dirname`` in
    # app/config/working_dirs.py): ``__…`` is Cremind's own pseudo profiles.
    if name.startswith("__"):
        raise ValueError(
            f"invalid profile name {name!r}: names starting with '__' are reserved "
            "for Cremind's internal profiles"
        )
    return name


def reserved_profile_name_error(name: str) -> Optional[str]:
    """The server's refusal for a name a new profile may not take, or None.

    Only for creating: a profile that already carries one (made before the
    name was reserved) may still be adopted, as the server allows."""
    if name not in _RESERVED_PROFILE_NAMES:
        return None
    if name == "workspaces":
        return (
            f"the profile name '{name}' is reserved (Cremind keeps every profile's "
            "working directory in a folder of that name); choose another name"
        )
    return (
        f"the profile name '{name}' is reserved (Cremind's manual uses it "
        "internally); choose another name"
    )


def drafts_dir(acting_profile: str) -> Path:
    return session.system_dir() / acting_profile / _DRAFTS_DIRNAME


def draft_path(acting_profile: str, target: str) -> Path:
    return drafts_dir(acting_profile) / f"{validate_profile_name(target)}{_DRAFT_SUFFIX}"


def result_path(acting_profile: str, target: str) -> Path:
    return drafts_dir(acting_profile) / f"{validate_profile_name(target)}{_RESULT_SUFFIX}"


@dataclass
class Draft:
    """One in-progress profile setup."""

    profile: str
    adopt: bool = False
    created_at: str = ""
    updated_at: str = ""
    steps: dict[str, dict[str, Any]] = field(default_factory=dict)

    def step(self, name: str) -> dict[str, Any]:
        return self.steps.setdefault(name, {"status": STATUS_PENDING, "payload": {}})

    def status(self, name: str) -> str:
        return str(self.step(name).get("status") or STATUS_PENDING)

    def payload(self, name: str) -> Any:
        return self.step(name).get("payload")

    def set_payload(self, name: str, payload: Any) -> None:
        self.steps[name] = {"status": STATUS_SET, "payload": payload}

    def skip(self, name: str) -> None:
        self.steps[name] = {"status": STATUS_SKIPPED, "payload": _empty_payload(name)}

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": DRAFT_VERSION,
            "profile": self.profile,
            "adopt": self.adopt,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "steps": self.steps,
        }


def _empty_payload(step: str) -> Any:
    """The zero value for a step, so callers never branch on ``None``."""
    if step == "channels":
        return []
    if step == "tools":
        return {"tool_configs": {}, "agent_configs": {}}
    return {}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_draft(target: str, adopt: bool = False) -> Draft:
    validate_profile_name(target)
    stamp = _now()
    return Draft(
        profile=target,
        adopt=adopt,
        created_at=stamp,
        updated_at=stamp,
        steps={s: {"status": STATUS_PENDING, "payload": _empty_payload(s)} for s in STEPS},
    )


def load(acting_profile: str, target: str) -> Optional[Draft]:
    """The draft for ``target``, or ``None`` if there is none (or it expired).

    An expired draft is deleted on the way past: it is unusable by definition
    and holds credentials, so leaving it would be the worst of both.
    """
    path = draft_path(acting_profile, target)
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        raise DraftError(f"cannot read the wizard draft at {path}: {e}") from e

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise DraftError(
            f"the wizard draft at {path} is not readable ({e}). "
            f"Start over: cremind profile wizard cancel {target}"
        ) from e
    if not isinstance(data, dict) or data.get("version") != DRAFT_VERSION:
        raise DraftError(
            f"the wizard draft at {path} was written by a different version. "
            f"Start over: cremind profile wizard cancel {target}"
        )

    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        age = 0
    if age > DRAFT_TTL_SECONDS:
        delete(acting_profile, target)
        return None

    steps = data.get("steps")
    draft = Draft(
        profile=str(data.get("profile") or target),
        adopt=bool(data.get("adopt")),
        created_at=str(data.get("created_at") or ""),
        updated_at=str(data.get("updated_at") or ""),
        steps=steps if isinstance(steps, dict) else {},
    )
    for step in STEPS:
        draft.step(step)  # fill in anything a shorter draft omitted
    return draft


def save(acting_profile: str, draft: Draft) -> Path:
    """Write the draft atomically, 0600, creating the directory 0700.

    Failures propagate: the caller has just collected an API key from a human,
    and silently losing it would send them round the same questions again.
    """
    draft.updated_at = _now()
    return write_private_file(
        draft_path(acting_profile, draft.profile),
        (json.dumps(draft.to_dict(), indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )


def delete(acting_profile: str, target: str, *, include_result: bool = True) -> bool:
    """Remove the draft, and by default any finished-result record too.

    ``finish`` passes ``include_result=False``: it is replacing the draft with
    the result, not throwing both away.
    """
    paths = [draft_path(acting_profile, target)]
    if include_result:
        paths.append(result_path(acting_profile, target))
    removed = False
    for path in paths:
        try:
            path.unlink()
            removed = True
        except (FileNotFoundError, NotADirectoryError):
            pass
        except OSError:
            pass
    return removed


def save_result(acting_profile: str, target: str, result: dict[str, Any]) -> Path:
    """Persist what ``finish`` printed.

    ``finish`` can run for minutes (the setup POST installs features), which is
    long enough for the agent's shell capture to come back as a long-running
    process rather than a completed command. The token, the login URL and the
    path to the configuration file would be lost with it, so they are written
    down and ``status`` prints them back.
    """
    return write_private_file(
        result_path(acting_profile, target),
        (json.dumps(result, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )


def load_result(acting_profile: str, target: str) -> Optional[dict[str, Any]]:
    try:
        raw = result_path(acting_profile, target).read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError, OSError):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def write_private_file(path: Path, data: bytes) -> Path:
    """Atomic 0600 write, with a 0700 parent. The token-file pattern.

    The temp file is chmod'ed before the rename, so the content is never
    briefly world-readable under its final name, and the temp name cannot be
    mistaken for a finished draft.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass  # best-effort; a shared dir is not fatal
    tmp = directory / f".{path.name}.tmp"
    try:
        tmp.write_bytes(data)
        if os.name == "posix":
            # On Windows os.chmod only toggles the read-only bit, which would
            # be misleading — skip it there, as session.write_token does.
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


# ── progress ─────────────────────────────────────────────────────────────


def pending_steps(draft: Draft) -> list[str]:
    return [s for s in STEPS if draft.status(s) == STATUS_PENDING]


def next_step(draft: Draft) -> Optional[str]:
    remaining = pending_steps(draft)
    return remaining[0] if remaining else None


def is_ready(draft: Draft) -> bool:
    """Every step answered or skipped — ``finish`` may run."""
    return not pending_steps(draft)


def skipped_steps(draft: Draft) -> list[str]:
    return [s for s in STEPS if draft.status(s) == STATUS_SKIPPED]


def summarize_step(step: str, draft: Draft) -> str:
    """One line describing what a step holds, with no secrets in it."""
    status = draft.status(step)
    if status != STATUS_SET:
        return status
    payload = draft.payload(step)
    if step == "llm" and isinstance(payload, dict):
        bits = []
        if payload.get("default_provider"):
            bits.append(f"provider={payload['default_provider']}")
        if payload.get("model_group.high"):
            bits.append(f"model={payload['model_group.high']}")
        creds = sorted(
            k.split(".", 1)[1] for k in payload
            if "." in k and _SECRET_KEY_RE.search(k.split(".", 1)[1])
        )
        if creds:
            bits.append(f"credentials={','.join(creds)}")
        return f"set ({', '.join(bits)})" if bits else "set"
    if step == "tools" and isinstance(payload, dict):
        tools = payload.get("tool_configs") or {}
        enabled = sum(1 for c in tools.values() if str(c.get("_enabled", "")).lower() == "true")
        return f"set ({len(tools)} tool(s), {enabled} enabled)"
    if step == "memory" and isinstance(payload, dict):
        enabled = str(payload.get("memory.enabled", "")).lower() == "true"
        return f"set (memory {'on' if enabled else 'off'})"
    if step == "channels" and isinstance(payload, list):
        return "set (" + ", ".join(
            f"{c.get('channel_type')}/{c.get('mode')}" for c in payload
        ) + ")" if payload else "set (none)"
    return "set"


def mask_secrets(payload: Any) -> Any:
    """``payload`` with every credential-shaped value replaced by ``***``.

    ``status`` output reaches the agent's transcript, and from there the user's
    screen; the values the user typed into chat do not need echoing back.
    """
    if isinstance(payload, dict):
        masked: dict[str, Any] = {}
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                masked[key] = mask_secrets(value)
            elif _SECRET_KEY_RE.search(str(key)):
                masked[key] = MASK
            else:
                masked[key] = value
        return masked
    if isinstance(payload, list):
        return [mask_secrets(v) for v in payload]
    return payload


# ── the setup body ───────────────────────────────────────────────────────


def build_setup_body(draft: Draft) -> dict[str, Any]:
    """The ``POST /api/config/setup`` body this draft describes.

    A skipped step contributes nothing at all rather than an empty object: the
    server treats "no ``tool_configs`` key" as "leave the defaults alone",
    while ``{}`` would say the same thing less clearly and an empty
    ``channel_configs`` would emit a pointless progress line.
    """
    body: dict[str, Any] = {"profile": draft.profile}
    if draft.adopt:
        body["adopt_existing"] = True

    if draft.status("llm") == STATUS_SET:
        llm = draft.payload("llm")
        if isinstance(llm, dict) and llm:
            body["llm_config"] = llm

    if draft.status("tools") == STATUS_SET:
        tools = draft.payload("tools")
        if isinstance(tools, dict):
            tool_configs = tools.get("tool_configs") or {}
            agent_configs = tools.get("agent_configs") or {}
            if tool_configs:
                body["tool_configs"] = tool_configs
            if agent_configs:
                body["agent_configs"] = agent_configs

    if draft.status("memory") == STATUS_SET:
        user_config = draft.payload("memory")
        if isinstance(user_config, dict) and user_config:
            body["user_config"] = user_config

    if draft.status("channels") == STATUS_SET:
        channels = draft.payload("channels")
        if isinstance(channels, list) and channels:
            body["channel_configs"] = channels

    return body


def load_json_payload(path: str, allow: Iterable[type] = (dict,)) -> Any:
    """Read a step payload from a JSON file.

    Not shared with ``cremind setup complete``'s reader, which is dict-only by
    contract; the channels step is a list. Reading from stdin is deliberately
    not offered: the agent's shell auto-closes an idle stdin, which would turn
    "I forgot the file" into a silently empty payload.
    """
    allowed = tuple(allow)
    try:
        raw = Path(path).read_bytes()
    except OSError as e:
        raise ValueError(f"read {path}: {e}") from e
    if not raw.strip():
        raise ValueError(f"{path} is empty")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"parse {path}: {e}") from e
    if not isinstance(body, allowed):
        names = " or ".join("a JSON object" if t is dict else "a JSON array" for t in allowed)
        raise ValueError(f"{path} must contain {names}")
    return body
