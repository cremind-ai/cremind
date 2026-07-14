"""Task registry + OpenAI Codex SDK bridge for the ``codex`` built-in tool.

The Codex mirror of :mod:`app.tools.builtin.claude_code_runner`. Kept separate
from the leaf module (``codex.py``) so the SDK boundary is unit-testable in
isolation (monkeypatch ``sys.modules['openai_codex']``) and mirrors exec_shell's
runner/registry split.

A Codex coding session can run far longer than ``MCP_TOOL_CALL_TIMEOUT`` bounds a
single ``run()`` call, so each session runs in a background ``asyncio.Task``
tracked here; the leaves start / long-poll / stop it. The SDK never loads at
import time — :func:`load_sdk` imports it lazily so built-in registration works
with the feature uninstalled.

Cremind's LLM only ever sees the final ``TurnResult``-derived payload
(``task.result``); intermediate reasoning / tool activity goes exclusively to the
user-facing :class:`~app.agent.agent_activity.AgentActivity` feed.

Design notes specific to Codex (vs the Claude Agent SDK):

* **Client** — ``openai_codex.AsyncCodex`` spawns ``codex app-server`` over stdio
  and speaks JSON-RPC. One client per task (matching Claude's per-session
  ``ClaudeSDKClient``) so each task's env / ``CODEX_HOME`` / profile auth is
  isolated and cleanup is a context-manager exit.
* **Auth** — the app-server authenticates from ``$CODEX_HOME/auth.json``. A host
  ``codex login`` works out of the box; an explicit/profile/env API key is
  installed via ``login_api_key()`` into a Cremind-managed ``CODEX_HOME`` so we
  never clobber the user's own ``~/.codex``. All of this lives in
  :func:`resolve_auth` — the single auth seam.
* **Approval** — pinned to ``ApprovalMode.deny_all`` (never pauses): the server
  is headless, so an approval-seeking mode would stall. The user-facing knob is
  the filesystem ``Sandbox`` instead.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import inspect
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.agent.agent_activity import AgentActivity
from app.config.settings import BaseConfig
from app.tools.builtin.codex_activity import apply_notification
from app.utils.logger import logger
from app.utils.task_context import current_task_id_var

AGENT_NAME = "codex"

_RUN_GRACE_SECONDS = 60.0
_WAIT_DEFAULT_SECONDS = 120.0
_WAIT_MARGIN_SECONDS = 15.0
_FINISHED_TASK_TTL_SECONDS = 3600.0
_DEFAULT_MAX_CONCURRENT = 2

_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})

_DELEGATION_APPEND = (
    "You are being driven programmatically by another AI assistant on behalf of "
    "its user; there is no human available to answer questions. Work fully "
    "autonomously and never pause for interactive confirmation. When finished, "
    "end with a concise summary of what you did, which files you created or "
    "changed, and how to run or verify the result."
)


class Var:
    """required_config variable keys (also imported by the leaf module)."""

    MODEL = "CODEX_MODEL"
    SANDBOX = "CODEX_SANDBOX"
    REASONING_EFFORT = "CODEX_REASONING_EFFORT"
    API_KEY = "CODEX_API_KEY"
    BIN_PATH = "CODEX_BIN"
    CONFIG_OVERRIDES = "CODEX_CONFIG_OVERRIDES"
    MAX_CONCURRENT_TASKS = "CODEX_MAX_CONCURRENT_TASKS"


VAR_DEFAULTS: Dict[str, Any] = {
    Var.MODEL: "",
    Var.SANDBOX: "full-access",
    Var.REASONING_EFFORT: "",
    Var.API_KEY: "",
    Var.BIN_PATH: "",
    Var.CONFIG_OVERRIDES: "",
    Var.MAX_CONCURRENT_TASKS: _DEFAULT_MAX_CONCURRENT,
}


class CodexConcurrencyError(Exception):
    """Raised by :func:`start_task` when a concurrency limit is hit."""

    def __init__(self, code: str, message: str, running_task_id: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.running_task_id = running_task_id


@dataclass
class CodexAuth:
    """Resolved Codex credentials for one task/probe.

    ``env_overrides`` are merged over the app-server subprocess env (chiefly
    ``CODEX_HOME`` when an explicit key is used); ``api_key``, when set, is
    installed via ``login_api_key()`` into that home; ``source`` is a non-secret
    label reusing the :func:`credential_source` vocabulary.
    """

    env_overrides: Dict[str, str] = field(default_factory=dict)
    api_key: Optional[str] = None
    source: Optional[str] = None


@dataclass
class CodexTask:
    task_id: str
    context_id: str
    profile: str
    prompt: str
    cwd: str
    status: str = "running"  # running | completed | failed | interrupted
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    session_id: Optional[str] = None  # the Codex thread id
    resumed_from: Optional[str] = None
    model: Optional[str] = None
    runner: Optional[asyncio.Task] = None
    client: Any = None
    turn_handle: Any = None
    activity: Optional[AgentActivity] = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    result: Optional[Dict[str, Any]] = None
    token_usage: Optional[Dict[str, int]] = None
    token_usage_reported: bool = False
    expire_time: float = 0.0
    agent_task_id: Optional[str] = None

    def elapsed_seconds(self) -> int:
        end = self.finished_at or time.time()
        return int(end - self.created_at)


_task_registry: Dict[str, CodexTask] = {}


# ── variable / option helpers ─────────────────────────────────────────────────
def merge_variables(variables: Optional[dict]) -> Dict[str, Any]:
    merged = dict(VAR_DEFAULTS)
    for key, value in (variables or {}).items():
        if value is not None:
            merged[key] = value
    return merged


def _csv_pairs(value: Any) -> Tuple[str, ...]:
    """Parse ``"k1=v1, k2=v2"`` into a tuple of ``--config`` override strings."""
    if not value:
        return ()
    items = [part.strip() for part in str(value).split(",")]
    return tuple(p for p in items if p and "=" in p)


def _as_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def load_sdk():
    """Lazily import ``openai_codex``. Returns ``(module, error_str)``."""
    try:
        import openai_codex

        return openai_codex, None
    except ImportError as exc:
        return None, str(exc)


# ── SDK-item duck-typing helpers ───────────────────────────────────────────────
def _unwrap(item: Any) -> Any:
    return getattr(item, "root", item)


def _item_type(item: Any) -> str:
    return str(getattr(item, "type", "") or "")


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip()


# ── auth ───────────────────────────────────────────────────────────────────────
def _managed_codex_home() -> Path:
    """Cremind-owned ``CODEX_HOME`` used when an explicit API key is installed via
    ``login_api_key()``, so the user's own ``~/.codex`` is never touched."""
    home = Path(BaseConfig.CREMIND_SYSTEM_DIR) / "codex-home"
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.debug("codex: could not create managed CODEX_HOME", exc_info=True)
    return home


def resolve_auth(variables: dict, profile: str) -> CodexAuth:
    """Resolve OpenAI credentials for a Codex run/probe. Never raises.

    Order: explicit tool variable → profile's OpenAI LLM key → server env
    (``CODEX_API_KEY`` / ``OPENAI_API_KEY``) → host ``codex login`` store. Any
    resolved key is installed into a Cremind-managed ``CODEX_HOME``; when no key
    is found the app-server authenticates from the ambient ``$CODEX_HOME`` /
    ``~/.codex/auth.json`` (host ``codex login``). Auth *failures* are surfaced
    later from the SDK result.
    """
    managed = {"CODEX_HOME": str(_managed_codex_home())}

    explicit = str(variables.get(Var.API_KEY) or "").strip()
    if explicit:
        return CodexAuth(env_overrides=managed, api_key=explicit, source="tool_variable_api_key")
    try:
        prof_key = BaseConfig.get_provider_api_key("openai", profile=profile)
        if prof_key:
            return CodexAuth(env_overrides=managed, api_key=prof_key, source="profile_openai_api_key")
    except Exception:  # noqa: BLE001
        logger.debug("codex: profile auth resolution failed", exc_info=True)
    env_codex = os.environ.get("CODEX_API_KEY")
    if env_codex:
        return CodexAuth(env_overrides=managed, api_key=env_codex, source="env_codex_api_key")
    env_openai = os.environ.get("OPENAI_API_KEY")
    if env_openai:
        return CodexAuth(env_overrides=managed, api_key=env_openai, source="env_openai_api_key")
    source = "host_codex_login" if _read_host_codex_auth() else None
    return CodexAuth(env_overrides={}, api_key=None, source=source)


def credential_source(variables: dict, profile: str) -> Optional[str]:
    """Return a non-secret label for the credential Codex would use for
    ``profile`` (or None when nothing is visible at all)."""
    return resolve_auth(variables, profile).source


# Host ``codex login`` credential store. ``CODEX_HOME`` defaults to ``~/.codex``
# (``%USERPROFILE%\\.codex`` on Windows). Module-level so tests can monkeypatch
# it to a temp file.
_CODEX_AUTH_PATH = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "auth.json"


def _read_host_codex_auth() -> bool:
    """Best-effort check that the host ``codex login`` store holds a credential.
    Returns False on any problem (missing file, unreadable, empty)."""
    try:
        path = _CODEX_AUTH_PATH
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.debug("codex: reading host codex credentials failed", exc_info=True)
        return False
    if not isinstance(data, dict):
        return False
    # auth.json carries either an API key or ChatGPT OAuth tokens.
    return bool(data.get("OPENAI_API_KEY") or data.get("tokens") or data.get("openai_api_key"))


# ── SDK config / kwargs construction ───────────────────────────────────────────
def _construct_config(sdk, kwargs: Dict[str, Any]):
    """Instantiate ``CodexConfig`` from ``kwargs``, dropping any field an older
    SDK version lacks (and None values)."""
    cfg_cls = sdk.CodexConfig
    try:
        known = {f.name for f in dataclasses.fields(cfg_cls)}
        filtered = {k: v for k, v in kwargs.items() if k in known and v is not None}
    except TypeError:
        filtered = {k: v for k, v in kwargs.items() if v is not None}
    return cfg_cls(**filtered)


def build_config(sdk, *, variables: dict, auth: CodexAuth, cwd: Optional[str]):
    """Build a ``CodexConfig`` for the app-server subprocess."""
    return _construct_config(sdk, {
        # ``or ""`` so a missing/None variable stays None (an unquoted
        # ``str(None)`` would become the literal path "None" and the SDK would
        # fail to find a binary there — breaking model listing / probe_auth,
        # which may pass an unmerged variables dict).
        "codex_bin": (str(variables.get(Var.BIN_PATH) or "").strip() or None),
        "config_overrides": _csv_pairs(variables.get(Var.CONFIG_OVERRIDES)),
        "cwd": cwd,
        # Merge over the inherited env so the app-server keeps PATH/node/etc.;
        # auth.env_overrides may point CODEX_HOME at the managed store.
        "env": {**os.environ, **auth.env_overrides},
    })


def _filter_kwargs(fn, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only kwargs the callable accepts (dropping None), so a beta SDK that
    renamed/removed a parameter degrades instead of raising ``TypeError``."""
    cleaned = {k: v for k, v in kwargs.items() if v is not None}
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return cleaned
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return cleaned
    return {k: v for k, v in cleaned.items() if k in params}


def _coerce_sandbox(sdk, value: Any):
    """Map a ``CODEX_SANDBOX`` string to the SDK ``Sandbox`` enum (default
    full-access, matching the configured default)."""
    sandbox_cls = getattr(sdk, "Sandbox", None)
    if sandbox_cls is None:
        return None
    for candidate in (str(value or "").strip(), "full-access"):
        try:
            return sandbox_cls(candidate)
        except (ValueError, TypeError):
            continue
    return None


def _coerce_effort(value: Any) -> Optional[str]:
    effort = str(value or "").strip().lower()
    return effort if effort in _REASONING_EFFORTS else None


def build_thread_kwargs(
    sdk, *, cwd: str, variables: dict, model: Optional[str], resume: bool
):
    """Build the kwargs for ``thread_start`` / ``thread_resume``."""
    approval = getattr(getattr(sdk, "ApprovalMode", None), "deny_all", None)
    kwargs = {
        "cwd": cwd,
        "model": (model or str(variables.get(Var.MODEL) or "").strip() or None),
        "sandbox": _coerce_sandbox(sdk, variables.get(Var.SANDBOX)),
        "approval_mode": approval,
        "developer_instructions": _DELEGATION_APPEND,
    }
    fn = sdk.AsyncCodex.thread_resume if resume else sdk.AsyncCodex.thread_start
    return _filter_kwargs(fn, kwargs)


def build_turn_kwargs(sdk, *, variables: dict) -> Dict[str, Any]:
    kwargs = {"effort": _coerce_effort(variables.get(Var.REASONING_EFFORT))}
    return _filter_kwargs(sdk.AsyncThread.turn, kwargs)


async def _login_if_needed(codex, auth: CodexAuth) -> None:
    """Install an explicit/profile/env API key into the (managed) CODEX_HOME.
    A no-op when authenticating from a host ``codex login`` store."""
    if not auth.api_key:
        return
    try:
        await codex.login_api_key(auth.api_key)
    except Exception:  # noqa: BLE001 — surfaced later as an auth failure
        logger.debug("codex: login_api_key failed", exc_info=True)


# ── account model listing ──────────────────────────────────────────────────────
_MODELS_CACHE_TTL = 300.0  # seconds
# credential fingerprint -> (fetched_at, models). Errors are never cached.
_models_cache: Dict[str, Tuple[float, List[dict]]] = {}


def _cache_key(auth: CodexAuth) -> str:
    """Fingerprint the credential material so cached lists never cross accounts.
    Never uses the raw secret as a dict key."""
    material = auth.api_key or f"host:{auth.source or ''}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


async def list_models(
    variables: dict, profile: str, *, force_refresh: bool = False
) -> Dict[str, Any]:
    """List the Codex models available to the resolved account. Never raises.

    Returns ``{"models": [{"id", "display_name"}...], "source": label, "cached":
    bool}`` on success, or ``{"models": [], "error": "<detail>", "source":
    label|None}`` when the SDK is missing, no credential is available, or the
    query fails. Uses the SDK's ``codex.models()`` (spawns a short-lived
    app-server), cached 300s per credential fingerprint.
    """
    sdk, err = load_sdk()
    if sdk is None:
        return {
            "models": [],
            "error": (
                "openai_codex is not installed — install it with "
                "`cremind features install codex`. " + (err or "")
            ).strip(),
            "source": None,
        }

    auth = resolve_auth(variables, profile)
    key = _cache_key(auth)
    if not force_refresh:
        entry = _models_cache.get(key)
        if entry is not None:
            fetched_at, cached_models = entry
            if (time.monotonic() - fetched_at) < _MODELS_CACHE_TTL:
                return {"models": cached_models, "source": auth.source, "cached": True}

    try:
        config = build_config(sdk, variables=variables, auth=auth, cwd=None)
        async with sdk.AsyncCodex(config) as codex:
            await _login_if_needed(codex, auth)
            resp = await codex.models()
        rows = getattr(resp, "data", None) or []
    except Exception as exc:  # noqa: BLE001
        logger.debug("codex: model listing failed", exc_info=True)
        detail = f"Failed to list Codex models: {exc}"
        if not auth.source:
            detail = (
                "No OpenAI credential available. Set CODEX_API_KEY, configure "
                "OpenAI under Settings -> LLM, or run `codex login` on the host."
            )
        return {"models": [], "error": detail, "source": auth.source}

    models = [
        {"id": getattr(m, "id", None), "display_name": getattr(m, "display_name", None) or getattr(m, "id", None)}
        for m in rows
        if getattr(m, "id", None) and not getattr(m, "hidden", False)
    ]
    _models_cache[key] = (time.monotonic(), models)
    return {"models": models, "source": auth.source, "cached": False}


# ── sandbox-mode listing ───────────────────────────────────────────────────────
#
# The Codex SDK is the source of truth for the filesystem sandbox levels: it
# exports them as the ``Sandbox`` enum, so a newer SDK with new levels is picked
# up automatically. Pure in-process introspection — no network, no cache.

_SANDBOX_LABELS: Dict[str, str] = {
    "read-only": "read-only (explore/answer only, no changes)",
    "workspace-write": "workspace-write (edit files and run commands inside the working directory)",
    "full-access": (
        "full-access (no sandbox; fully autonomous — same trust level as the "
        "Shell Executor tool)"
    ),
}


def list_sandbox_modes() -> Dict[str, Any]:
    """List the filesystem sandbox modes the installed Codex SDK accepts.

    Never raises. Mirrors :func:`list_models`'s envelope:
    ``{"modes": [...], "source": "openai_codex"|None, "error": str|None}``.
    """
    sdk, err = load_sdk()
    if sdk is None:
        return {
            "modes": [],
            "error": (
                "openai_codex is not installed — install it with "
                "`cremind features install codex`. " + (err or "")
            ).strip(),
            "source": None,
        }
    sandbox_cls = getattr(sdk, "Sandbox", None)
    try:
        modes = [m.value for m in sandbox_cls] if sandbox_cls is not None else []
    except TypeError:
        modes = []
    if not modes:
        return {
            "modes": [],
            "error": "Installed openai_codex does not expose a sandbox-mode list.",
            "source": "openai_codex",
        }
    return {"modes": modes, "source": "openai_codex", "error": None}


async def probe_auth(
    sdk, *, cwd: str, variables: dict, profile: str, timeout: float = 30.0
) -> Dict[str, Any]:
    """Confirm Codex can authenticate by reading the active account (cheap — no
    coding turn, no token spend).

    Returns ``{"logged_in": bool|None, "detail": str}``. ``logged_in`` is None
    when the probe could not run (binary missing / timeout).
    """
    auth = resolve_auth(variables, profile)

    async def _run():
        config = build_config(sdk, variables=variables, auth=auth, cwd=cwd)
        async with sdk.AsyncCodex(config) as codex:
            await _login_if_needed(codex, auth)
            return await codex.account()

    try:
        resp = await asyncio.wait_for(_run(), timeout=timeout)
    except FileNotFoundError as exc:
        return {"logged_in": None, "detail": f"Codex binary not found: {exc}"}
    except asyncio.TimeoutError:
        return {"logged_in": None, "detail": f"Auth probe timed out after {int(timeout)}s."}
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if _looks_like_auth_error(text):
            return {"logged_in": False, "detail": text}
        return {"logged_in": None, "detail": f"Probe error: {text}"}

    requires_auth = bool(getattr(resp, "requires_openai_auth", False))
    account = getattr(resp, "account", None)
    if account is not None and not requires_auth:
        return {"logged_in": True, "detail": "Codex has an active account credential."}
    return {
        "logged_in": False,
        "detail": "No Codex account credential is active.",
    }


# ── registry lifecycle ────────────────────────────────────────────────────────
def get_task(task_id: str) -> Optional[CodexTask]:
    return _task_registry.get(task_id)


def find_running_for_context(context_id: str) -> Optional[CodexTask]:
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
) -> CodexTask:
    """Register + spawn a background Codex session. May raise
    :class:`CodexConcurrencyError`."""
    _cleanup_stale_tasks()

    existing = find_running_for_context(context_id)
    if existing is not None:
        raise CodexConcurrencyError(
            "TaskAlreadyRunning",
            "A Codex task is already running in this conversation. Wait for it "
            "(codex__wait) or stop it (codex__stop) before starting another.",
            running_task_id=existing.task_id,
        )

    max_concurrent = _as_int(variables.get(Var.MAX_CONCURRENT_TASKS)) or _DEFAULT_MAX_CONCURRENT
    running = sum(1 for t in _task_registry.values() if not t.done.is_set())
    if running >= max_concurrent:
        raise CodexConcurrencyError(
            "TooManyTasks",
            f"The Codex concurrent-task limit ({max_concurrent}) is reached. "
            "Wait for a running task to finish before starting another.",
        )

    sdk, err = load_sdk()
    if sdk is None:  # pragma: no cover — leaf pre-checks this; defensive.
        raise RuntimeError(f"openai_codex not importable: {err}")

    task = CodexTask(
        task_id=uuid.uuid4().hex[:8],
        context_id=context_id,
        profile=profile,
        prompt=prompt,
        cwd=cwd,
        session_id=session_id,
        resumed_from=session_id,
        model=model,
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
        logger.exception("codex: failed to start activity feed")
        task.activity = None

    auth = resolve_auth(variables, profile)
    task.runner = asyncio.create_task(
        _run_session(
            task, sdk, prompt,
            variables=variables, model=model, session_id=session_id, auth=auth,
        )
    )
    return task


async def wait_for_task(task: CodexTask, timeout: float) -> bool:
    if task.done.is_set():
        return True
    try:
        await asyncio.wait_for(task.done.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def stop_task(task: CodexTask, grace: float = 10.0) -> str:
    """Interrupt a running task gracefully (the thread stays resumable), force-
    cancelling if it does not stop promptly."""
    if task.done.is_set():
        return task.status

    handle = task.turn_handle
    if handle is not None:
        try:
            await handle.interrupt()
        except Exception:  # noqa: BLE001
            logger.debug("codex: interrupt() failed", exc_info=True)
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
async def _run_session(
    task: CodexTask,
    sdk,
    prompt: str,
    *,
    variables: dict,
    model: Optional[str],
    session_id: Optional[str],
    auth: CodexAuth,
) -> None:
    try:
        config = build_config(sdk, variables=variables, auth=auth, cwd=task.cwd)
        async with sdk.AsyncCodex(config) as codex:
            task.client = codex
            await _login_if_needed(codex, auth)
            thread_kwargs = build_thread_kwargs(
                sdk, cwd=task.cwd, variables=variables, model=model, resume=bool(session_id),
            )
            if session_id:
                thread = await codex.thread_resume(session_id, **thread_kwargs)
            else:
                thread = await codex.thread_start(**thread_kwargs)
            task.session_id = getattr(thread, "id", None) or session_id

            turn_kwargs = build_turn_kwargs(sdk, variables=variables)
            handle = await thread.turn(prompt, **turn_kwargs)
            task.turn_handle = handle
            async for notification in handle.stream():
                _handle_notification(task, notification)
                if task.activity is not None:
                    await apply_notification(task.activity, notification)
        if task.activity is not None and task.status in ("completed", "failed", "interrupted"):
            await task.activity.add_step(
                kind="result",
                label=("Completed" if task.status == "completed" else f"{task.status.title()}"),
                detail=(task.result or {}).get("result") or (task.result or {}).get("message") or "",
            )
    except asyncio.CancelledError:
        task.status = "interrupted"
        task.result = _interrupted_payload(task)
    except FileNotFoundError as exc:
        task.status = "failed"
        task.result = _failure_payload(
            task,
            error="CodexBinaryNotFound",
            message=f"The Codex binary could not be found. {exc}",
            remediation=(
                "Reinstall the codex feature (cremind features install codex) or "
                "set the CODEX_BIN tool variable to an existing codex binary."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        task.status = "failed"
        if _looks_like_auth_error(str(exc)):
            task.result = _auth_failure_payload(task, detail=str(exc))
        else:
            task.result = _failure_payload(
                task,
                error="CodexError",
                message=f"Codex failed: {exc}",
            )
    finally:
        task.client = None
        task.turn_handle = None
        task.finished_at = time.time()
        task.expire_time = time.monotonic() + _FINISHED_TASK_TTL_SECONDS
        if task.status == "running":
            task.status = "failed"
            if task.result is None:
                task.result = _failure_payload(
                    task,
                    error="CodexError",
                    message="Codex ended without producing a result.",
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
                logger.debug("codex: activity.finish failed", exc_info=True)


def _handle_notification(task: CodexTask, notification: Any) -> None:
    """Capture task-owned state from a Codex notification: token usage and the
    terminal ``turn/completed`` payload. Activity/UI translation is separate."""
    method = str(getattr(notification, "method", "") or "")
    payload = getattr(notification, "payload", None)
    if payload is None:
        return
    if method == "thread/started":
        thread = getattr(payload, "thread", None)
        sid = getattr(thread, "id", None)
        if sid:
            task.session_id = sid
        return
    if method == "thread/tokenUsage/updated":
        usage = _map_usage(getattr(payload, "token_usage", None))
        if usage is not None:
            task.token_usage = usage
        return
    if method == "turn/completed":
        _finalize_from_turn(task, getattr(payload, "turn", None))


def _finalize_from_turn(task: CodexTask, turn: Any) -> None:
    if turn is None:
        return
    status = _enum_value(getattr(turn, "status", None))
    if status == "interrupted":
        task.status = "interrupted"
        task.result = _interrupted_payload(task)
        return
    if status == "failed":
        task.status = "failed"
        error = getattr(turn, "error", None)
        detail = getattr(error, "message", None) or ""
        if _looks_like_auth_error(detail):
            task.result = _auth_failure_payload(task, detail=detail)
        else:
            task.result = _failure_payload(
                task,
                error="CodexError",
                message=f"Codex did not complete successfully. {detail}".strip(),
                duration_ms=getattr(turn, "duration_ms", None),
            )
        return
    task.status = "completed"
    task.result = _success_payload(task, turn)


def _final_response_from_items(items: Any) -> str:
    """Return the turn's final assistant message (prefer phase=final_answer)."""
    if not isinstance(items, (list, tuple)):
        return ""
    last_any: Optional[str] = None
    for raw in reversed(items):
        item = _unwrap(raw)
        if _item_type(item) != "agentMessage":
            continue
        text = getattr(item, "text", None)
        if _enum_value(getattr(item, "phase", None)) == "final_answer":
            return text or ""
        if last_any is None:
            last_any = text
    return last_any or ""


# ── payload builders (the only Codex content Cremind sees) ────────────────────
def _map_usage(usage: Any) -> Optional[Dict[str, int]]:
    """Map a Codex ``ThreadTokenUsage`` onto the 4-key dict the tool adapter
    folds. Codex's ``input_tokens`` INCLUDES cached tokens (OpenAI convention),
    so we subtract to match the Anthropic split the adapter assumes (input =
    non-cached input, cache_read = cached), avoiding a cost double-count."""
    breakdown = getattr(usage, "last", None) or getattr(usage, "total", None)
    if breakdown is None:
        return None
    input_total = int(getattr(breakdown, "input_tokens", 0) or 0)
    cached = int(getattr(breakdown, "cached_input_tokens", 0) or 0)
    return {
        "input_tokens": max(0, input_total - cached),
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": 0,
        "output_tokens": int(getattr(breakdown, "output_tokens", 0) or 0),
    }


def _success_payload(task: CodexTask, turn: Any) -> Dict[str, Any]:
    return {
        "status": "completed",
        "task_id": task.task_id,
        "session_id": task.session_id,
        "result": _final_response_from_items(getattr(turn, "items", None)),
        "duration_ms": getattr(turn, "duration_ms", None),
        "usage": task.token_usage,
        "working_directory": task.cwd,
        "note": (
            "To continue this coding session with a follow-up instruction, call "
            "codex__run with this session_id (the Codex thread)."
        ),
    }


def _failure_payload(task: CodexTask, *, error: str, message: str, **extra: Any) -> Dict[str, Any]:
    payload = {
        "status": "failed",
        "task_id": task.task_id,
        "session_id": task.session_id,
        "error": error,
        "message": message,
        "working_directory": task.cwd,
    }
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


def _auth_failure_payload(task: CodexTask, *, detail: str, **extra: Any) -> Dict[str, Any]:
    return _failure_payload(
        task,
        error="AuthenticationError",
        message=("Codex could not authenticate with OpenAI. " + (detail or "")).strip(),
        remediation=(
            "Provide credentials one of three ways: set the CODEX_API_KEY tool "
            "variable; configure the OpenAI provider under Settings → LLM for this "
            "profile; or run `codex login` on the server host."
        ),
        **extra,
    )


def _interrupted_payload(task: CodexTask) -> Dict[str, Any]:
    return {
        "status": "interrupted",
        "task_id": task.task_id,
        "session_id": task.session_id,
        "working_directory": task.cwd,
        "message": (
            "The Codex task was stopped before completion. If it captured a "
            "session_id, the coding session can be resumed by calling codex__run "
            "with that session_id."
        ),
    }


def _activity_stats(task: CodexTask) -> Dict[str, Any]:
    result = task.result or {}
    return {
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
    "incorrect api key",
    "invalid_api_key",
    "missing api key",
    "no api key",
    "openai_api_key",
    "insufficient_quota",
    "usage limit",
    "quota",
    "could not resolve authentication",
    "codex login",
    "not logged in",
    "not authenticated",
    "log in",
    "sign in",
    "no auth",
    "auth.json",
    "token has expired",
    "expired",
    "revoked",
)


def _looks_like_auth_error(text: str) -> bool:
    low = (text or "").lower()
    return any(hint in low for hint in _AUTH_HINTS)
