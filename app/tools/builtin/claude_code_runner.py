"""Task registry + Claude Agent SDK bridge for the ``claude_code`` built-in tool.

Kept separate from the leaf module (``claude_code.py``) so the SDK boundary is
unit-testable in isolation (monkeypatch ``sys.modules['claude_agent_sdk']``) and
mirrors exec_shell's runner/registry split.

A Claude Code coding session can run far longer than ``MCP_TOOL_CALL_TIMEOUT``
bounds a single ``run()`` call, so each session runs in a background
``asyncio.Task`` tracked here; the leaves start / long-poll / stop it. The SDK
never loads at import time — :func:`load_sdk` imports it lazily so built-in
registration works with the feature uninstalled.

Cremind's LLM only ever sees the final ``ResultMessage``-derived payload
(``task.result``); intermediate thinking / tool activity goes exclusively to the
user-facing :class:`~app.agent.agent_activity.AgentActivity` feed.
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import httpx

from app.agent.agent_activity import AgentActivity
from app.config.settings import BaseConfig, get_dynamic
from app.tools.builtin.claude_code_activity import apply_sdk_message
from app.utils.logger import logger
from app.utils.task_context import current_task_id_var

AGENT_NAME = "claude_code"

_RUN_GRACE_SECONDS = 60.0
_WAIT_DEFAULT_SECONDS = 120.0
_WAIT_MARGIN_SECONDS = 15.0
_FINISHED_TASK_TTL_SECONDS = 3600.0
_DEFAULT_MAX_CONCURRENT = 2
_STDERR_TAIL = 40

_DELEGATION_APPEND = (
    "You are being driven programmatically by another AI assistant on behalf of "
    "its user; there is no human available to answer questions. Work fully "
    "autonomously and never pause for interactive confirmation. When finished, "
    "end with a concise summary of what you did, which files you created or "
    "changed, and how to run or verify the result."
)


class Var:
    """required_config variable keys (also imported by the leaf module)."""

    MODEL = "CLAUDE_CODE_MODEL"
    PERMISSION_MODE = "CLAUDE_CODE_PERMISSION_MODE"
    MAX_TURNS = "CLAUDE_CODE_MAX_TURNS"
    MAX_BUDGET_USD = "CLAUDE_CODE_MAX_BUDGET_USD"
    API_KEY = "CLAUDE_CODE_API_KEY"
    CLI_PATH = "CLAUDE_CODE_CLI_PATH"
    ALLOWED_TOOLS = "CLAUDE_CODE_ALLOWED_TOOLS"
    DISALLOWED_TOOLS = "CLAUDE_CODE_DISALLOWED_TOOLS"
    MAX_CONCURRENT_TASKS = "CLAUDE_CODE_MAX_CONCURRENT_TASKS"


VAR_DEFAULTS: Dict[str, Any] = {
    Var.MODEL: "",
    Var.PERMISSION_MODE: "bypassPermissions",
    Var.MAX_TURNS: 0,
    Var.MAX_BUDGET_USD: 0,
    Var.API_KEY: "",
    Var.CLI_PATH: "",
    Var.ALLOWED_TOOLS: "",
    Var.DISALLOWED_TOOLS: "",
    Var.MAX_CONCURRENT_TASKS: _DEFAULT_MAX_CONCURRENT,
}


class ClaudeCodeConcurrencyError(Exception):
    """Raised by :func:`start_task` when a concurrency limit is hit."""

    def __init__(self, code: str, message: str, running_task_id: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.running_task_id = running_task_id


@dataclass
class ClaudeCodeTask:
    task_id: str
    context_id: str
    profile: str
    prompt: str
    cwd: str
    status: str = "running"  # running | completed | failed | interrupted
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    session_id: Optional[str] = None
    resumed_from: Optional[str] = None
    model: Optional[str] = None
    permission_mode: Optional[str] = None
    runner: Optional[asyncio.Task] = None
    client: Any = None
    activity: Optional[AgentActivity] = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    result: Optional[Dict[str, Any]] = None
    token_usage: Optional[Dict[str, int]] = None
    token_usage_reported: bool = False
    stderr_tail: Deque[str] = field(default_factory=lambda: collections.deque(maxlen=_STDERR_TAIL))
    expire_time: float = 0.0
    agent_task_id: Optional[str] = None

    def elapsed_seconds(self) -> int:
        end = self.finished_at or time.time()
        return int(end - self.created_at)


_task_registry: Dict[str, ClaudeCodeTask] = {}


# ── variable / option helpers ─────────────────────────────────────────────────
def merge_variables(variables: Optional[dict]) -> Dict[str, Any]:
    merged = dict(VAR_DEFAULTS)
    for key, value in (variables or {}).items():
        if value is not None:
            merged[key] = value
    return merged


def _csv(value: Any) -> Optional[List[str]]:
    if not value:
        return None
    items = [part.strip() for part in str(value).split(",")]
    items = [p for p in items if p]
    return items or None


def _as_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def load_sdk():
    """Lazily import ``claude_agent_sdk``. Returns ``(module, error_str)``."""
    try:
        import claude_agent_sdk

        return claude_agent_sdk, None
    except ImportError as exc:
        return None, str(exc)


def resolve_auth_env(variables: dict, profile: str) -> Dict[str, str]:
    """Resolve Anthropic credentials into subprocess env overrides.

    Order: explicit tool variable → profile's Anthropic LLM credentials
    (setup-token or api key) → nothing (let the CLI's own login / the server
    environment authenticate). Auth *failures* are surfaced later from the
    SDK result, so this never raises.
    """
    overrides: Dict[str, str] = {}
    explicit = str(variables.get(Var.API_KEY) or "").strip()
    if explicit:
        overrides["ANTHROPIC_API_KEY"] = explicit
        return overrides
    try:
        auth_method = get_dynamic("llm_config", "anthropic.auth_method", profile=profile)
        if auth_method == "setup_token":
            token = get_dynamic("llm_config", "anthropic.setup_token", profile=profile)
            if token:
                overrides["CLAUDE_CODE_OAUTH_TOKEN"] = str(token)
                return overrides
        prof_key = BaseConfig.get_provider_api_key("anthropic", profile=profile)
        if prof_key:
            overrides["ANTHROPIC_API_KEY"] = prof_key
    except Exception:  # noqa: BLE001
        logger.debug("claude_code: profile auth resolution failed", exc_info=True)
    return overrides


def build_options(
    sdk,
    *,
    cwd: str,
    variables: dict,
    model: Optional[str],
    session_id: Optional[str],
    env_overrides: Dict[str, str],
    on_stderr,
):
    """Build a ``ClaudeAgentOptions``, dropping fields an older SDK lacks."""
    kwargs: Dict[str, Any] = {
        "cwd": cwd,
        "permission_mode": variables.get(Var.PERMISSION_MODE) or "bypassPermissions",
        "model": (model or variables.get(Var.MODEL) or None),
        "resume": session_id or None,
        "max_turns": _as_int(variables.get(Var.MAX_TURNS)) or None,
        "max_budget_usd": _as_float(variables.get(Var.MAX_BUDGET_USD)) or None,
        "allowed_tools": _csv(variables.get(Var.ALLOWED_TOOLS)),
        "disallowed_tools": _csv(variables.get(Var.DISALLOWED_TOOLS)),
        "cli_path": (variables.get(Var.CLI_PATH) or None),
        # Merge over the inherited process env so the CLI keeps PATH/node/etc.
        # whether the SDK treats ``env`` as additive or as a full replacement.
        "env": {**os.environ, **env_overrides},
        "system_prompt": {"type": "preset", "preset": "claude_code", "append": _DELEGATION_APPEND},
        "setting_sources": ["project"],
        "include_partial_messages": False,
        "stderr": on_stderr,
    }
    return _construct_options(sdk, kwargs)


def _construct_options(sdk, kwargs: Dict[str, Any]):
    """Instantiate ``ClaudeAgentOptions`` from ``kwargs``, dropping any field an
    older SDK version lacks (and None values)."""
    opt_cls = sdk.ClaudeAgentOptions
    try:
        known = {f.name for f in dataclasses.fields(opt_cls)}
        filtered = {k: v for k, v in kwargs.items() if k in known and v is not None}
    except TypeError:
        filtered = {k: v for k, v in kwargs.items() if v is not None}
    try:
        return opt_cls(**filtered)
    except TypeError:
        # Last-resort: keep only the fields every SDK version has.
        minimal = {
            k: filtered[k]
            for k in ("cwd", "permission_mode", "model", "resume", "env")
            if k in filtered
        }
        return opt_cls(**minimal)


def credential_source(variables: dict, profile: str) -> Optional[str]:
    """Return a non-secret label for the credential Claude Code would use for
    ``profile`` (or None when nothing is visible at all).

    Covers the same chain the model listing resolves — explicit tool variable →
    profile Anthropic creds → server env → host ``claude login`` store. The last
    tier is readable on Windows/Linux (``~/.claude/.credentials.json``); a macOS
    host stores it in the Keychain, which is not visible here, so ``None`` there
    does not necessarily mean logged out (``probe=true`` settles it)."""
    if str(variables.get(Var.API_KEY) or "").strip():
        return "tool_variable_api_key"
    try:
        auth_method = get_dynamic("llm_config", "anthropic.auth_method", profile=profile)
        if auth_method == "setup_token" and get_dynamic("llm_config", "anthropic.setup_token", profile=profile):
            return "profile_setup_token"
        if BaseConfig.get_provider_api_key("anthropic", profile=profile):
            return "profile_api_key"
    except Exception:  # noqa: BLE001
        pass
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "env_anthropic_api_key"
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "env_oauth_token"
    if _read_host_claude_credentials():
        return "host_claude_login"
    return None


# --- Account model listing -------------------------------------------------
#
# Neither claude_agent_sdk nor the bundled ``claude`` CLI exposes a way to
# enumerate the models available to the logged-in account, so we query the
# Anthropic REST ``/v1/models`` endpoint directly with whatever credential the
# coding task would use. This lets the Settings dropdown and the ``cremind
# tools options`` CLI present a live, account-scoped model list.

_MODELS_URL = "https://api.anthropic.com/v1/models"
_ANTHROPIC_VERSION = "2023-06-01"
_OAUTH_BETA = "oauth-2025-04-20"
_MODELS_TIMEOUT = 10.0
_MODELS_CACHE_TTL = 300.0  # seconds
_MODEL_ALIASES = ("sonnet", "opus", "haiku", "opusplan")
# Host ``claude login`` credential store (Windows/Linux; macOS uses the
# Keychain, which is not readable here — the fetch simply degrades). Module-level
# so tests can monkeypatch it to a temp file.
_CLAUDE_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"

# credential fingerprint -> (fetched_at, models). Errors are never cached.
_models_cache: Dict[str, Tuple[float, List[dict]]] = {}


def _read_host_claude_credentials() -> Optional[str]:
    """Best-effort read of ``claudeAiOauth.accessToken`` from the host
    ``claude login`` credential store. Returns None on any problem."""
    try:
        path = _CLAUDE_CREDENTIALS_PATH
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        token = (data.get("claudeAiOauth") or {}).get("accessToken")
        return str(token) if token else None
    except Exception:  # noqa: BLE001
        logger.debug("claude_code: reading host claude credentials failed", exc_info=True)
        return None


def _build_models_headers(variables: dict, profile: str) -> Tuple[Dict[str, str], Optional[str]]:
    """Resolve credentials into request headers for ``GET /v1/models``.

    Returns ``(headers, source_label)`` where ``source_label`` reuses the
    :func:`credential_source` vocabulary (plus ``host_claude_login``), or
    ``({}, None)`` when no credential can be found.

    Resolution order mirrors :func:`resolve_auth_env` / :func:`credential_source`:
    explicit tool variable → profile Anthropic creds → server env → host
    ``claude login`` store.
    """
    def _api_key_headers(key: str) -> Dict[str, str]:
        return {"x-api-key": key, "anthropic-version": _ANTHROPIC_VERSION}

    def _oauth_headers(token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "anthropic-version": _ANTHROPIC_VERSION,
            "anthropic-beta": _OAUTH_BETA,
        }

    explicit = str(variables.get(Var.API_KEY) or "").strip()
    if explicit:
        return _api_key_headers(explicit), "tool_variable_api_key"
    try:
        auth_method = get_dynamic("llm_config", "anthropic.auth_method", profile=profile)
        if auth_method == "setup_token":
            token = get_dynamic("llm_config", "anthropic.setup_token", profile=profile)
            if token:
                return _oauth_headers(str(token)), "profile_setup_token"
        prof_key = BaseConfig.get_provider_api_key("anthropic", profile=profile)
        if prof_key:
            return _api_key_headers(prof_key), "profile_api_key"
    except Exception:  # noqa: BLE001
        logger.debug("claude_code: profile auth resolution failed", exc_info=True)
    env_key = os.environ.get("ANTHROPIC_API_KEY")
    if env_key:
        return _api_key_headers(env_key), "env_anthropic_api_key"
    env_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env_token:
        return _oauth_headers(env_token), "env_oauth_token"
    host_token = _read_host_claude_credentials()
    if host_token:
        return _oauth_headers(host_token), "host_claude_login"
    return {}, None


async def _fetch_models(headers: Dict[str, str]) -> List[dict]:
    """One ``GET /v1/models`` call. Returns the raw ``data`` rows. Raises on
    HTTP or transport failure so :func:`list_models` can surface the detail."""
    async with httpx.AsyncClient(timeout=_MODELS_TIMEOUT) as client:
        resp = await client.get(_MODELS_URL, params={"limit": 1000}, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, list) else []


def _cache_key(headers: Dict[str, str]) -> str:
    """Fingerprint the credential material so cached lists never cross accounts.
    Never uses the raw secret as a dict key."""
    material = headers.get("x-api-key") or headers.get("Authorization") or ""
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def list_models(
    variables: dict, profile: str, *, force_refresh: bool = False
) -> Dict[str, Any]:
    """List the Claude models available to the resolved account. Never raises.

    Returns ``{"models": [{"id", "display_name", "created_at"}...], "source":
    label, "cached": bool}`` on success, or ``{"models": [], "error": "<detail>",
    "source": label|None}`` when no credential is available or the fetch fails.
    """
    headers, source = _build_models_headers(variables, profile)
    if not headers:
        return {
            "models": [],
            "error": (
                "No Anthropic credential available. Set CLAUDE_CODE_API_KEY, "
                "configure Anthropic under Settings -> LLM, or run `claude login` "
                "on the host."
            ),
            "source": None,
        }

    key = _cache_key(headers)
    if not force_refresh:
        entry = _models_cache.get(key)
        if entry is not None:
            fetched_at, cached_models = entry
            if (time.monotonic() - fetched_at) < _MODELS_CACHE_TTL:
                return {"models": cached_models, "source": source, "cached": True}

    try:
        rows = await _fetch_models(headers)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        detail = f"Anthropic API returned HTTP {status}"
        if status in (401, 403):
            detail += " (credential rejected)"
        return {"models": [], "error": detail, "source": source}
    except Exception as exc:  # noqa: BLE001
        logger.debug("claude_code: model listing failed", exc_info=True)
        return {"models": [], "error": f"Failed to list models: {exc}", "source": source}

    models = [
        {
            "id": row["id"],
            "display_name": row.get("display_name") or row["id"],
            "created_at": row.get("created_at"),
        }
        for row in rows
        if isinstance(row, dict) and row.get("id")
    ]
    _models_cache[key] = (time.monotonic(), models)
    return {"models": models, "source": source, "cached": False}


# --- Permission-mode listing -----------------------------------------------
#
# The Claude Agent SDK is the source of truth for which permission modes exist:
# it exports them as the ``PermissionMode`` Literal (the same set the Claude Code
# CLI cycles through with Shift+Tab), so a newer SDK with new modes is picked up
# automatically. This is pure in-process introspection — no network, no cache —
# so ``refresh`` is meaningless here (a failed ``import claude_agent_sdk`` is not
# cached in ``sys.modules``, so installing the feature mid-process is seen on the
# next call; an in-place SDK *upgrade* needs a restart, as with everything else).

_PERMISSION_MODE_LABELS: Dict[str, str] = {
    "default": "default (ask before privileged actions)",
    "acceptEdits": "acceptEdits (auto-approve file edits)",
    "plan": "plan (read-only planning, no changes)",
    "bypassPermissions": "bypassPermissions (fully autonomous)",
    "dontAsk": "dontAsk (never prompts; denies anything not pre-approved)",
    "auto": "auto (no prompts; a safety classifier blocks destructive actions)",
}

# Which permission modes let Claude Code actually make changes when running
# headless (no human present to approve). Kept next to _PERMISSION_MODE_LABELS so
# a new SDK mode gets a label AND a classification review in the same place.
#   autonomous — writes files AND runs commands with no approval stall
#   edits_only — writes files but non-edit actions may stall (acceptEdits)
#   blocked    — cannot make changes headless (plan / default / dontAsk)
#   unknown    — a mode this build doesn't recognise: surface it, never claim it
#                is blocked (a future SDK mode could be fully autonomous)
_AUTONOMOUS_MODES = frozenset({"bypassPermissions", "auto"})
_EDITS_ONLY_MODES = frozenset({"acceptEdits"})
_BLOCKED_MODES = frozenset({"plan", "default", "dontAsk"})

_WRITE_CAPABLE_TARGET = "bypassPermissions"


def permission_autonomy(mode: Optional[str]) -> str:
    """Classify a permission mode as autonomous / edits_only / blocked / unknown.

    Structural (mode is known at task start) — never parses result text."""
    if mode in _AUTONOMOUS_MODES:
        return "autonomous"
    if mode in _EDITS_ONLY_MODES:
        return "edits_only"
    if mode in _BLOCKED_MODES:
        return "blocked"
    return "unknown"


def _permission_advisory(mode: Optional[str]) -> Optional[Dict[str, Any]]:
    """A machine-readable 'confirm once, then fix' advisory for a mode that may
    prevent headless changes, or ``None`` when the mode is fully autonomous.

    The remediation names the ONE real lever — Cremind's
    ``CLAUDE_CODE_PERMISSION_MODE`` tool variable — and the exact
    ``cremind tools set-var`` command, and forbids inventing a Claude Code UI /
    plan-mode step. The orchestrator must ask the user once before running it."""
    autonomy = permission_autonomy(mode)
    if autonomy == "autonomous":
        return None
    command = (
        f"cremind tools set-var claude_code "
        f"CLAUDE_CODE_PERMISSION_MODE={_WRITE_CAPABLE_TARGET}"
    )
    if autonomy == "edits_only":
        warning = (
            f"Claude Code ran in permission mode '{mode}', which auto-approves file "
            "edits but may block other actions (e.g. running commands) because no "
            "human is present to approve them. If this task needed more than file "
            "edits and some step was denied, this is why."
        )
    elif autonomy == "blocked":
        warning = (
            f"Claude Code ran in permission mode '{mode}', which does NOT let it "
            "create or modify files when running headless (there is no human to "
            "approve actions, and the ExitPlanMode tool is not available here). If "
            "this task was meant to make changes and none were made, this is why."
        )
    else:  # unknown
        warning = (
            f"Claude Code ran in permission mode '{mode}', which Cremind does not "
            "recognise as a fully-autonomous mode. If the task was meant to make "
            "changes but none were made, this permission mode may be the reason."
        )
    return {
        "autonomy": autonomy,
        "warning": warning,
        "cause": (
            "This mode is set by Cremind's CLAUDE_CODE_PERMISSION_MODE tool "
            "variable — NOT by Claude Code itself. There is no Claude Code "
            "'plan mode' UI for the user to exit and no `claude` CLI command that "
            "changes it. Do NOT tell the user to exit plan mode or run any claude "
            "plan command."
        ),
        "remediation": (
            "Tell the user Claude Code is in this mode and ask ONCE whether to "
            f"switch it to {_WRITE_CAPABLE_TARGET} (fully autonomous). Only after "
            "they agree, run the command below with the Shell Executor tool, then "
            "re-run the task with claude_code__run (reuse the session_id to "
            "continue). Do NOT change the mode without asking first."
        ),
        "command": command,
    }


def _permission_mode_ids(sdk) -> List[str]:
    """Extract the ``PermissionMode`` Literal members from the installed SDK.

    Tries the exported ``PermissionMode`` alias first, then the
    ``ClaudeAgentOptions.permission_mode`` type hint (unwrapping
    ``Optional[Literal[...]]``). Returns ``[]`` if neither shape is present.
    Never raises.
    """
    import typing as _t

    pm = getattr(sdk, "PermissionMode", None)
    args = _t.get_args(pm) if pm is not None else ()
    if args and all(isinstance(a, str) for a in args):
        return list(args)
    try:
        hint = _t.get_type_hints(sdk.ClaudeAgentOptions).get("permission_mode")
    except Exception:  # noqa: BLE001 — forward refs on some SDK versions
        logger.debug("claude_code: permission-mode hint resolution failed", exc_info=True)
        return []
    for candidate in (hint, *_t.get_args(hint)):  # bare Literal or Optional[Literal]
        inner = _t.get_args(candidate)
        if inner and all(isinstance(a, str) for a in inner):
            return list(inner)
    return []


def list_permission_modes() -> Dict[str, Any]:
    """List the permission modes the installed Claude Agent SDK accepts.

    Never raises. Mirrors :func:`list_models`'s envelope:
    ``{"modes": [...], "source": "claude_agent_sdk"|None, "error": str|None}``.
    When the SDK is not installed the list is empty (the tool cannot run without
    it anyway) with an install hint; the API write-check treats an empty list as
    "can't validate" and accepts any value, exactly like the model field offline.
    """
    sdk, err = load_sdk()
    if sdk is None:
        return {
            "modes": [],
            "error": (
                "claude_agent_sdk is not installed — install it with "
                "`cremind features install claude_code`. " + (err or "")
            ).strip(),
            "source": None,
        }
    modes = _permission_mode_ids(sdk)
    if not modes:
        return {
            "modes": [],
            "error": "Installed claude_agent_sdk does not expose a permission-mode list.",
            "source": "claude_agent_sdk",
        }
    return {"modes": modes, "source": "claude_agent_sdk", "error": None}


async def probe_auth(sdk, *, cwd: str, variables: dict, profile: str, timeout: float = 30.0) -> Dict[str, Any]:
    """Run a minimal 1-turn query to confirm the credential actually
    authenticates. Cheap (one tiny API call), no tools, no file edits.

    Returns ``{"logged_in": bool|None, "detail": str}``. ``logged_in`` is None
    when the probe could not run (e.g. CLI missing / timeout).
    """
    env_overrides = resolve_auth_env(variables, profile)
    options = _construct_options(sdk, {
        "cwd": cwd,
        "permission_mode": "plan",  # never edits files
        "max_turns": 1,
        "allowed_tools": [],
        "cli_path": variables.get(Var.CLI_PATH) or None,
        "env": {**os.environ, **env_overrides},
        "include_partial_messages": False,
    })
    cli_not_found = getattr(sdk, "CLINotFoundError", ())
    text_parts: List[str] = []
    saw_error: Optional[bool] = None

    async def _run() -> None:
        nonlocal saw_error
        async with sdk.ClaudeSDKClient(options=options) as client:
            await client.query("Reply with the single word: OK")
            async for message in client.receive_response():
                cls = type(message).__name__
                if cls == "ResultMessage":
                    saw_error = bool(getattr(message, "is_error", False))
                    for attr in ("result", "subtype"):
                        val = getattr(message, attr, None)
                        if val:
                            text_parts.append(str(val))

    try:
        await asyncio.wait_for(_run(), timeout=timeout)
    except cli_not_found as exc:  # type: ignore[misc]
        return {"logged_in": None, "detail": f"Claude Code CLI not found: {exc}"}
    except asyncio.TimeoutError:
        return {"logged_in": None, "detail": f"Auth probe timed out after {int(timeout)}s."}
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if _looks_like_auth_error(text):
            return {"logged_in": False, "detail": text}
        return {"logged_in": None, "detail": f"Probe error: {text}"}

    blob = " ".join(text_parts)
    if saw_error:
        return {"logged_in": False, "detail": blob or "Claude Code reported an error."}
    return {"logged_in": True, "detail": "Claude Code authenticated successfully."}


# ── registry lifecycle ────────────────────────────────────────────────────────
def get_task(task_id: str) -> Optional[ClaudeCodeTask]:
    return _task_registry.get(task_id)


def find_running_for_context(context_id: str) -> Optional[ClaudeCodeTask]:
    for task in _task_registry.values():
        if task.context_id == context_id and not task.done.is_set():
            return task
    return None


def known_task_ids() -> List[str]:
    return list(_task_registry.keys())


def _cleanup_stale_tasks() -> None:
    now = time.monotonic()
    stale = [
        tid
        for tid, task in _task_registry.items()
        if task.done.is_set() and task.expire_time and task.expire_time < now
    ]
    for tid in stale:
        _task_registry.pop(tid, None)


async def start_task(
    *,
    prompt: str,
    cwd: str,
    profile: str,
    context_id: str,
    variables: dict,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
) -> ClaudeCodeTask:
    """Register + spawn a background Claude Code session. May raise
    :class:`ClaudeCodeConcurrencyError`."""
    _cleanup_stale_tasks()

    existing = find_running_for_context(context_id)
    if existing is not None:
        raise ClaudeCodeConcurrencyError(
            "TaskAlreadyRunning",
            "A Claude Code task is already running in this conversation. Wait for "
            "it (claude_code__wait) or stop it (claude_code__stop) before starting "
            "another.",
            running_task_id=existing.task_id,
        )

    max_concurrent = _as_int(variables.get(Var.MAX_CONCURRENT_TASKS)) or _DEFAULT_MAX_CONCURRENT
    running = sum(1 for t in _task_registry.values() if not t.done.is_set())
    if running >= max_concurrent:
        raise ClaudeCodeConcurrencyError(
            "TooManyTasks",
            f"The Claude Code concurrent-task limit ({max_concurrent}) is reached. "
            "Wait for a running task to finish before starting another.",
        )

    sdk, err = load_sdk()
    if sdk is None:  # pragma: no cover — leaf pre-checks this; defensive.
        raise RuntimeError(f"claude_agent_sdk not importable: {err}")

    task = ClaudeCodeTask(
        task_id=uuid.uuid4().hex[:8],
        context_id=context_id,
        profile=profile,
        prompt=prompt,
        cwd=cwd,
        session_id=session_id,
        resumed_from=session_id,
        model=model,
        permission_mode=(variables.get(Var.PERMISSION_MODE) or "bypassPermissions"),
        agent_task_id=current_task_id_var.get(),
    )
    _task_registry[task.task_id] = task

    try:
        task.activity = await AgentActivity.start(
            context_id=context_id,
            profile=profile,
            agent=AGENT_NAME,
            task_id=task.task_id,
            title=prompt,
        )
    except Exception:  # noqa: BLE001 — activity is best-effort
        logger.exception("claude_code: failed to start activity feed")
        task.activity = None

    def _on_stderr(line: str) -> None:
        try:
            task.stderr_tail.append(line)
        except Exception:  # noqa: BLE001
            pass

    env_overrides = resolve_auth_env(variables, profile)
    options = build_options(
        sdk,
        cwd=cwd,
        variables=variables,
        model=model,
        session_id=session_id,
        env_overrides=env_overrides,
        on_stderr=_on_stderr,
    )
    task.runner = asyncio.create_task(_run_session(task, sdk, options, prompt))
    return task


async def wait_for_task(task: ClaudeCodeTask, timeout: float) -> bool:
    if task.done.is_set():
        return True
    try:
        await asyncio.wait_for(task.done.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def stop_task(task: ClaudeCodeTask, grace: float = 10.0) -> str:
    """Interrupt a running task gracefully (session stays resumable), force-
    cancelling if it does not stop promptly."""
    if task.done.is_set():
        return task.status

    client = task.client
    if client is not None:
        try:
            await client.interrupt()
        except Exception:  # noqa: BLE001
            logger.debug("claude_code: interrupt() failed", exc_info=True)
    try:
        await asyncio.wait_for(task.done.wait(), timeout=grace)
        return task.status
    except asyncio.TimeoutError:
        pass

    if task.runner is not None and not task.runner.done():
        task.runner.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task.done.wait()), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    if not task.done.is_set():
        task.status = "interrupted"
        task.result = _interrupted_payload(task)
        task.finished_at = time.time()
        task.expire_time = time.monotonic() + _FINISHED_TASK_TTL_SECONDS
        task.done.set()
    return task.status


# ── session runner ────────────────────────────────────────────────────────────
async def _run_session(task: ClaudeCodeTask, sdk, options, prompt: str) -> None:
    cli_not_found = getattr(sdk, "CLINotFoundError", ())
    process_error = getattr(sdk, "ProcessError", ())
    try:
        async with sdk.ClaudeSDKClient(options=options) as client:
            task.client = client
            await client.query(prompt)
            async for message in client.receive_response():
                await _handle_message(task, message)
                if task.activity is not None:
                    await apply_sdk_message(task.activity, message)
    except asyncio.CancelledError:
        task.status = "interrupted"
        task.result = _interrupted_payload(task)
    except cli_not_found as exc:  # type: ignore[misc]
        task.status = "failed"
        task.result = _failure_payload(
            task,
            error="CLINotFound",
            message=(
                "The Claude Code CLI binary could not be found. "
                f"{exc}"
            ),
            remediation=(
                "Reinstall the claude_code feature (cremind features install "
                "claude_code) or set the CLAUDE_CODE_CLI_PATH tool variable to an "
                "existing Claude Code CLI binary."
            ),
        )
    except process_error as exc:  # type: ignore[misc]
        task.status = "failed"
        stderr_tail = "\n".join(task.stderr_tail)
        if _looks_like_auth_error(f"{exc}\n{stderr_tail}"):
            task.result = _auth_failure_payload(task, detail=str(exc), stderr_tail=stderr_tail)
        else:
            task.result = _failure_payload(
                task,
                error="ClaudeCodeProcessError",
                message=f"The Claude Code process failed: {exc}",
                stderr_tail=stderr_tail,
                exit_code=getattr(exc, "exit_code", None),
            )
    except Exception as exc:  # noqa: BLE001
        task.status = "failed"
        stderr_tail = "\n".join(task.stderr_tail)
        if _looks_like_auth_error(f"{exc}\n{stderr_tail}"):
            task.result = _auth_failure_payload(task, detail=str(exc), stderr_tail=stderr_tail)
        else:
            task.result = _failure_payload(
                task,
                error="ClaudeCodeError",
                message=f"Claude Code failed: {exc}",
                stderr_tail=stderr_tail,
            )
    finally:
        task.client = None
        task.finished_at = time.time()
        task.expire_time = time.monotonic() + _FINISHED_TASK_TTL_SECONDS
        if task.status == "running":
            task.status = "failed"
            if task.result is None:
                task.result = _failure_payload(
                    task,
                    error="ClaudeCodeError",
                    message="Claude Code ended without producing a result.",
                )
        task.done.set()
        if task.activity is not None:
            try:
                await task.activity.finish(
                    status=task.status,
                    stats=_activity_stats(task),
                    error=(task.result or {}).get("message") if task.status == "failed" else None,
                )
            except Exception:  # noqa: BLE001
                logger.debug("claude_code: activity.finish failed", exc_info=True)


async def _handle_message(task: ClaudeCodeTask, message: Any) -> None:
    cls = type(message).__name__
    if cls == "SystemMessage":
        if getattr(message, "subtype", None) == "init":
            data = getattr(message, "data", None) or {}
            sid = data.get("session_id")
            if sid:
                task.session_id = sid
        return
    if cls == "ResultMessage":
        is_error = bool(getattr(message, "is_error", False))
        subtype = getattr(message, "subtype", None)
        sid = getattr(message, "session_id", None)
        if sid:
            task.session_id = sid
        task.token_usage = _map_usage(getattr(message, "usage", None))
        if is_error:
            task.status = "failed"
            stats = {
                "subtype": subtype,
                "num_turns": getattr(message, "num_turns", None),
                "duration_ms": getattr(message, "duration_ms", None),
                "total_cost_usd": getattr(message, "total_cost_usd", None),
            }
            # An unauthenticated / not-logged-in run surfaces here as an
            # is_error ResultMessage (not a raised ProcessError), so the auth
            # classification MUST run on this path too — otherwise the common
            # "Claude isn't logged in" case is mislabelled a generic error with
            # no actionable remediation.
            blob = _result_error_text(message, task)
            if _looks_like_auth_error(blob):
                task.result = _auth_failure_payload(
                    task,
                    detail=(getattr(message, "result", None) or subtype or "").strip(),
                    stderr_tail="\n".join(task.stderr_tail),
                    **stats,
                )
            else:
                # A non-auth failure may still stem from a restrictive permission
                # mode; attach the advisory here (but never on the auth path, which
                # carries its own credential remediation). Autonomous modes yield
                # None, which _failure_payload filters out.
                task.result = _failure_payload(
                    task,
                    error="ClaudeCodeError",
                    message=f"Claude Code did not complete successfully (subtype: {subtype}).",
                    permission_advisory=_permission_advisory(task.permission_mode),
                    **stats,
                )
        else:
            task.status = "completed"
            task.result = _success_payload(task, message)
        if task.activity is not None:
            await task.activity.add_step(
                kind="result",
                label=("Completed" if not is_error else f"Failed: {subtype}"),
                detail=(getattr(message, "result", None) or ""),
            )
        return


# ── payload builders (the only Claude Code content Cremind sees) ──────────────
def _map_usage(usage: Any) -> Optional[Dict[str, int]]:
    if not isinstance(usage, dict):
        return None
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
    }


def _success_payload(task: ClaudeCodeTask, message: Any) -> Dict[str, Any]:
    result_text = getattr(message, "result", None) or ""
    num_turns = getattr(message, "num_turns", None)
    payload: Dict[str, Any] = {
        "status": "completed",
        "task_id": task.task_id,
        "session_id": task.session_id,
        "result": result_text,
        "num_turns": num_turns,
        "duration_ms": getattr(message, "duration_ms", None),
        "total_cost_usd": getattr(message, "total_cost_usd", None),
        "usage": task.token_usage,
        "working_directory": task.cwd,
        "effective_permission_mode": task.permission_mode,
        "note": (
            "To continue this coding session with a follow-up instruction, call "
            "claude_code__run with this session_id."
        ),
    }

    # Empty-resume guard: a resumed session that cannot be continued (expired /
    # evicted) comes back as an is_error=False ResultMessage with an empty result
    # and no turns. Replace the bare empty string with an actionable note so the
    # orchestrator never mistakes it for "the task produced no output". Gate
    # tightly (empty AND no turns) so a legitimate empty final message with real
    # turns still passes through unchanged.
    if not result_text.strip() and (num_turns or 0) == 0:
        payload["resume_produced_no_work"] = True
        if task.resumed_from:
            payload["result"] = (
                "(no output) Resuming this coding session produced no work — the "
                "session may have expired or is no longer resumable. Do NOT treat "
                "this as 'the task produced no output'. Start a fresh task with "
                "claude_code__run WITHOUT a session_id, repeating the full task "
                "brief."
            )
        else:
            payload["result"] = (
                "(no output) Claude Code returned no result and took no turns. "
                "This is not a normal completion. Re-run the task with a clearer, "
                "more complete brief; if it recurs, check Claude Code's setup with "
                "claude_code__status (probe=true)."
            )

    advisory = _permission_advisory(task.permission_mode)
    if advisory is not None:
        payload["permission_advisory"] = advisory
    return payload


def _failure_payload(task: ClaudeCodeTask, *, error: str, message: str, **extra: Any) -> Dict[str, Any]:
    payload = {
        "status": "failed",
        "task_id": task.task_id,
        "session_id": task.session_id,
        "error": error,
        "message": message,
        "working_directory": task.cwd,
        "effective_permission_mode": task.permission_mode,
    }
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


def _auth_failure_payload(
    task: ClaudeCodeTask, *, detail: str, stderr_tail: str = "", **extra: Any
) -> Dict[str, Any]:
    return _failure_payload(
        task,
        error="AuthenticationError",
        message=(
            "Claude Code could not authenticate with Anthropic. "
            + (detail or "")
        ).strip(),
        remediation=(
            "Provide credentials one of three ways: set the CLAUDE_CODE_API_KEY "
            "tool variable; configure the Anthropic provider under Settings → LLM "
            "for this profile; or run `claude login` on the server host."
        ),
        stderr_tail=stderr_tail or None,
        **extra,
    )


def _result_error_text(message: Any, task: ClaudeCodeTask) -> str:
    """Concatenate the text a failed ResultMessage carries (+ stderr tail) so it
    can be pattern-matched for an auth failure."""
    parts: List[str] = []
    for attr in ("subtype", "result"):
        val = getattr(message, attr, None)
        if val:
            parts.append(str(val))
    errors = getattr(message, "errors", None)
    if isinstance(errors, (list, tuple)):
        parts.extend(str(e) for e in errors)
    elif errors:
        parts.append(str(errors))
    if task.stderr_tail:
        parts.append("\n".join(task.stderr_tail))
    return " ".join(parts)


def _interrupted_payload(task: ClaudeCodeTask) -> Dict[str, Any]:
    return {
        "status": "interrupted",
        "task_id": task.task_id,
        "session_id": task.session_id,
        "working_directory": task.cwd,
        "message": (
            "The Claude Code task was stopped before completion. If it captured a "
            "session_id, the coding session can be resumed by calling "
            "claude_code__run with that session_id."
        ),
    }


def _activity_stats(task: ClaudeCodeTask) -> Dict[str, Any]:
    result = task.result or {}
    return {
        "num_turns": result.get("num_turns"),
        "total_cost_usd": result.get("total_cost_usd"),
        "duration_ms": result.get("duration_ms"),
        "session_id": task.session_id,
    }


_AUTH_HINTS = (
    "authentication",
    "unauthorized",
    "401",
    "403",
    "forbidden",
    "invalid api key",
    "invalid x-api-key",
    "missing api key",
    "no api key",
    "anthropic_api_key",
    "credit balance",
    "could not resolve authentication",
    "expected one of apikey",
    "please run /login",
    "claude login",
    "not logged in",
    "not authenticated",
    "log in",
    "sign in",
    "oauth token",
    "invalid bearer token",
    "token has expired",
    "expired",
    "revoked",
)


def _looks_like_auth_error(text: str) -> bool:
    low = (text or "").lower()
    return any(hint in low for hint in _AUTH_HINTS)
