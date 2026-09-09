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
* **Auth** - the app-server reads ``$CODEX_HOME/auth.json`` ONCE, at startup, so
  ``CODEX_HOME`` is always pointed at a home BEFORE the client is constructed:
  either a Cremind-managed per-key home (an API key is then installed into the
  running server via ``login_api_key()``, an RPC, so that half comes after the
  spawn) or the profile's / the server's own ``codex login`` home resolved by
  :mod:`app.config.coding_cli_homes`. :func:`resolve_auth` +
  :func:`prepare_auth` are the single auth seam; :mod:`app.tools.builtin.
  codex_login` is the sign-in that fills a login home in the first place.
* **Approval** — pinned to ``ApprovalMode.deny_all`` (never pauses): the server
  is headless, so an approval-seeking mode would stall. The user-facing knob is
  the filesystem ``Sandbox`` instead.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import inspect
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.agent.agent_activity import AgentActivity
from app.config.coding_cli_homes import (
    codex_login_present,
    profile_codex_home,
    read_codex_account_hint,
    resolve_codex_home,
    shared_codex_home,
)
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

    ``env_overrides`` are merged over the app-server subprocess env and ALWAYS
    carry ``CODEX_HOME`` now: either a Cremind-managed per-key home or the CLI
    login home :mod:`app.config.coding_cli_homes` resolved for this profile.
    Leaving it unset used to mean "authenticate from the ambient ``~/.codex``",
    which on a multi-profile server is the one thing that must never happen -
    every profile would have run as whichever account signed in last.

    ``api_key``, when set, is installed via ``login_api_key()`` into that home;
    ``source`` is a non-secret label reusing the :func:`credential_source`
    vocabulary; ``scope`` is ``"profile"`` / ``"shared"`` for the two login
    tiers and None for the key tiers (a key is not a login anyone can sign out
    of, so labelling it with a scope would invite a "Sign out" button that
    could not work).
    """

    env_overrides: Dict[str, str] = field(default_factory=dict)
    api_key: Optional[str] = None
    source: Optional[str] = None
    scope: Optional[str] = None


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
    sandbox: Optional[str] = None  # the EFFECTIVE sandbox the run actually uses
    sandbox_note: Optional[str] = None  # set when a configured value fell open
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
#
# Four tiers, no LLM-provider lookups. A Codex credential is either a key the
# operator/profile typed for THIS tool (or exported to the server), or a
# ``codex login`` the user made in the CLI - and the CLI login is owned by
# :mod:`app.config.coding_cli_homes`, not by this module. The bridge that used
# to materialise a profile's "Sign in with ChatGPT" LLM tokens as a Codex
# ``auth.json`` is gone: it made one refresh-token chain shared state between
# the LLM transport and a subprocess that rotates it behind our back, and a
# task killed between the CLI's refresh and our read-back left the profile's
# OpenAI provider holding a spent single-use token. Users of it sign in again
# through :mod:`app.tools.builtin.codex_login` (device code) or the CLI.
def _managed_codex_home(auth: "CodexAuth") -> Path:
    """Cremind-owned ``CODEX_HOME`` for an installed API key, so the user's own
    ``~/.codex`` is never touched.

    Isolated PER CREDENTIAL (a subdir keyed by the credential fingerprint) so that
    concurrent tasks under different keys/profiles never share one ``auth.json``
    (no cross-account bleed), and a fresh key always lands in an empty dir (a
    failed login can't silently reuse a previous key's stored credential)."""
    home = Path(BaseConfig.CREMIND_SYSTEM_DIR) / "codex-home" / _cache_key(auth)[:16]
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.debug("codex: could not create managed CODEX_HOME", exc_info=True)
    return home


# The Codex CLI's own credential file, inside whichever ``CODEX_HOME`` is in
# play. Named once because three places have to agree on it: the login markers
# in :mod:`app.config.coding_cli_homes`, :func:`logout`, and the app-server
# itself, which reads it exactly once at spawn.
_AUTH_JSON_NAME = "auth.json"

_LOGOUT_TIMEOUT = 15.0

# Said once, in the two places a user meets a missing/refused credential (the
# failed-run payload and the model listing). It names the three real doors -
# the card, the CLI, a key - and no longer points at Settings -> LLM, which
# stopped being a Codex credential when the provider tiers were removed and
# would now send the user to change a setting that cannot help.
_SIGN_IN_REMEDIATION = (
    "Sign in from Settings -> Tools & Skills -> Coding Agents -> Codex -> Sign in "
    "(a device code you confirm in a browser), or run `cremind tools coding-agents "
    "login codex` on the server host. An OpenAI API key also works: set the "
    "CODEX_API_KEY tool variable, or CODEX_API_KEY / OPENAI_API_KEY in the server "
    "environment."
)
_NO_CREDENTIAL_REMEDIATION = (
    "No Codex credential is available for this profile. " + _SIGN_IN_REMEDIATION
)


def resolve_auth(variables: dict, profile: str) -> CodexAuth:
    """Resolve Codex credentials for a run/probe. Never raises.

    Order: the ``CODEX_API_KEY`` tool variable -> ``CODEX_API_KEY`` in the
    server environment -> ``OPENAI_API_KEY`` in the server environment -> the
    CLI login home for this profile (its own ``codex login`` first, the
    server's shared one as the fallback). A key beats a login because it is the
    credential someone typed *for this tool*; the profile's own login beats the
    server's because it is the account that profile chose.

    The profile's LLM-provider credentials are deliberately NOT a tier any
    more. There used to be two of them - the OpenAI API key and the "Sign in
    with ChatGPT" login - and both were surprises: configuring an OpenAI model
    for chatting silently also handed the Codex tool a paid coding agent on
    that key, and the ChatGPT bridge made one single-use refresh-token chain
    shared state between the LLM transport and a subprocess that rotates it
    behind our back. Codex now authenticates the way the Codex CLI does.

    ``CODEX_HOME`` is always set on the way out (see :class:`CodexAuth`); auth
    *failures* are surfaced later, from the SDK result.
    """
    api_key: Optional[str] = None
    source: Optional[str] = None

    explicit = str(variables.get(Var.API_KEY) or "").strip()
    if explicit:
        api_key, source = explicit, "tool_variable_api_key"
    if api_key is None:
        env_codex = os.environ.get("CODEX_API_KEY")
        if env_codex:
            api_key, source = env_codex, "env_codex_api_key"
    if api_key is None:
        env_openai = os.environ.get("OPENAI_API_KEY")
        if env_openai:
            api_key, source = env_openai, "env_openai_api_key"

    if api_key is not None:
        auth = CodexAuth(api_key=api_key, source=source)
        auth.env_overrides = {"CODEX_HOME": str(_managed_codex_home(auth))}
        return auth

    # No key: authenticate from a CLI login home. Pointing CODEX_HOME at it
    # even when it holds nothing is deliberate - it is the home a sign-in would
    # fill, so the run fails against the same directory the card is talking
    # about instead of against the server user's ambient ``~/.codex``.
    home = resolve_codex_home(profile)
    if not home.has_login:
        # No scope either: the scope labels a credential ("this profile's
        # login" / "the shared server login"), and with nothing signed in
        # there is no credential to label - a "shared" scope here would put a
        # Sign out button on a card that has nothing to sign out.
        return CodexAuth(env_overrides={"CODEX_HOME": home.path})
    return CodexAuth(
        env_overrides={"CODEX_HOME": home.path},
        source=("profile_codex_login" if home.scope == "profile" else "host_codex_login"),
        scope=home.scope,
    )


def credential_source(variables: dict, profile: str) -> Optional[str]:
    """Return a non-secret label for the credential Codex would use for
    ``profile`` (or None when nothing is visible at all)."""
    return resolve_auth(variables, profile).source


def credential_info(variables: dict, profile: str) -> Dict[str, Any]:
    """``{"source", "scope", "cli_home", "account_hint"}`` for ``profile``.

    The full non-secret description of the credential a run would use - what
    the Coding Agents card, the ``cremind tools coding-agents`` table and the
    status leaf print. Never raises and never returns a secret: ``cli_home`` is
    a directory path and ``account_hint`` is the account label
    :mod:`app.config.coding_cli_homes` reads out of that home's ``auth.json``.

    The hint is read for the login tiers only. An API-key home holds a key we
    installed there ourselves, so its hint would be the tautology
    ``{"type": "api_key"}`` that ``source`` already states - and producing it
    would mean opening a file containing the key on every status call, for
    nothing.
    """
    auth = resolve_auth(variables, profile)
    home = auth.env_overrides.get("CODEX_HOME")
    hint: Optional[dict] = None
    if home and auth.source in ("profile_codex_login", "host_codex_login"):
        try:
            hint = read_codex_account_hint(home)
        except Exception:  # noqa: BLE001 - a status call must never fail on a hint
            logger.debug("codex: reading the Codex account hint failed", exc_info=True)
    return {
        "source": auth.source,
        "scope": auth.scope,
        "cli_home": home,
        "account_hint": hint,
    }


# -- the codex CLI binary (sign-in / sign-out live here, not in the SDK) ------─
_CLI_NAME = "codex"


def _locate_cli(variables: Optional[dict]) -> Tuple[Optional[str], Optional[str]]:
    """``(binary, source)`` for the ``codex`` CLI, or ``(None, None)``.

    Same order the SDK itself resolves a binary in, so the CLI a user signs in
    with is the CLI the app-server later authenticates as: the ``CODEX_BIN``
    tool variable (an operator pointing at their own build), then the binary the
    ``codex`` feature ships inside its wheel, then whatever ``codex`` is on
    PATH. ``source`` is the non-secret label the ``/cli`` route publishes -
    ``tool_variable`` / ``bundled`` / ``path``.

    ``codex_cli_bin`` is imported inside the function because it arrives with
    the ``codex`` feature's extras: importing it at module scope would make this
    module unimportable whenever the feature is not installed, and the built-in
    has to register (and report itself uninstalled) in exactly that state. Its
    accessor raises when the wheel is present but the binary is not, hence the
    broad catch. Never raises.
    """
    explicit = str((variables or {}).get(Var.BIN_PATH) or "").strip()
    if explicit:
        return explicit, "tool_variable"
    try:
        from codex_cli_bin import bundled_codex_path

        return str(bundled_codex_path()), "bundled"
    except Exception:  # noqa: BLE001
        logger.debug("codex: no bundled codex binary available", exc_info=True)
    found = shutil.which(_CLI_NAME)
    if found:
        return found, "path"
    return None, None


def find_cli(variables: Optional[dict] = None) -> Optional[str]:
    """The ``codex`` binary this install would run, or None if there is none.

    Deliberately the same signature and return shape as
    :func:`app.tools.builtin.claude_code_runner.find_cli`, so the sign-in
    routes and the ``cremind tools coding-agents`` commands drive both coding
    agents through one code path instead of two.
    """
    return _locate_cli(variables)[0]


def cli_binary_source(variables: Optional[dict] = None) -> Optional[str]:
    """Where :func:`find_cli`'s answer came from: ``tool_variable`` /
    ``bundled`` / ``path``, or None when there is no binary."""
    return _locate_cli(variables)[1]


def login_argv(binary: str) -> List[str]:
    """``codex login --device-auth``.

    The device-code flow, never the plain ``codex login``: the plain one opens
    a browser and waits on a loopback redirect, which is nothing on a headless
    server (and on the desktop image would open a browser on the VNC display,
    not on the user's). ``--device-auth`` prints a URL and a code the user can
    carry to whatever machine they do have a browser on.
    """
    return [binary, "login", "--device-auth"]


def logout_argv(binary: str) -> List[str]:
    return [binary, "logout"]


def status_argv(binary: str) -> List[str]:
    return [binary, "login", "status"]


async def logout(variables: dict, profile: str, *, scope: str = "profile") -> Dict[str, Any]:
    """Sign one CLI home out of Codex. Never raises.

    Returns ``{"ok", "scope", "home", "detail"}``. ``scope="profile"`` targets
    ``<SYSDIR>/<profile>/coding-cli/codex`` and ``scope="shared"`` the server's
    own home. The RESOLVED home is deliberately not used: a profile with no
    login of its own resolves to the shared one, so "Sign out" on its card
    would have signed the whole server out. The caller states which of the two
    it means (and the API refuses ``shared`` for a non-admin).

    ``codex logout`` is an RPC on a live app-server, so this spawns one against
    the target home - which is also why ``CODEX_HOME`` goes into the config env
    before the client is constructed: the server reads ``auth.json`` once, at
    spawn, and a home named afterwards would sign out the wrong account.

    ``auth.json`` is unlinked afterwards whatever the RPC did, including when
    the SDK is missing entirely. The user asked to be signed out; leaving a
    live OAuth refresh token on disk because an RPC failed is the wrong
    failure mode. ``ok`` reports the only thing that matters - whether the
    home still holds a login - and ``detail`` carries what went wrong on the
    way.
    """
    home = Path(shared_codex_home() if scope == "shared" else profile_codex_home(profile))
    problems: List[str] = []

    sdk, err = load_sdk()
    if sdk is None:
        problems.append(f"the Codex SDK is not installed ({err})")
    else:
        auth = CodexAuth(env_overrides={"CODEX_HOME": str(home)}, scope=scope)

        async def _run():
            config = build_config(sdk, variables=variables, auth=auth, cwd=None)
            async with sdk.AsyncCodex(config) as codex:
                await codex.logout()

        try:
            await asyncio.wait_for(_run(), timeout=_LOGOUT_TIMEOUT)
        except asyncio.TimeoutError:
            problems.append(f"`codex logout` timed out after {int(_LOGOUT_TIMEOUT)}s")
        except Exception as exc:  # noqa: BLE001
            logger.debug("codex: the logout RPC failed", exc_info=True)
            problems.append(f"`codex logout` failed: {exc}")

    try:
        auth_file = home / _AUTH_JSON_NAME
        if auth_file.exists():
            auth_file.unlink()
    except OSError as exc:
        problems.append(f"could not remove {home / _AUTH_JSON_NAME}: {exc}")

    _forget_models_cache(str(home))
    if not codex_login_present(home):
        return {
            "ok": True,
            "scope": scope,
            "home": str(home),
            "detail": (
                f"Signed out of Codex ({'the shared server login' if scope == 'shared' else 'this profile'})."
            ),
        }
    return {
        "ok": False,
        "scope": scope,
        "home": str(home),
        "detail": (
            "The Codex login is still present after signing out"
            + (": " + "; ".join(problems) if problems else ".")
        ),
    }


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


def _config_override_sandbox(variables: dict) -> Optional[str]:
    """A ``sandbox_mode=`` entry in ``CODEX_CONFIG_OVERRIDES`` is a second lever
    on the filesystem sandbox; return its value (or None)."""
    for pair in _csv_pairs(variables.get(Var.CONFIG_OVERRIDES)):
        key, _, val = pair.partition("=")
        if key.strip() == "sandbox_mode":
            return val.strip() or None
    return None


def resolve_sandbox(sdk, variables: dict) -> Tuple[str, Optional[str]]:
    """Return ``(effective_sandbox, coercion_note)``.

    ``effective_sandbox`` is the sandbox the run will ACTUALLY use, so the
    reported ``effective_sandbox``/``sandbox_advisory`` can never contradict the
    run: a ``sandbox_mode`` override in ``CODEX_CONFIG_OVERRIDES`` wins; otherwise
    the ``CODEX_SANDBOX`` value coerced exactly as :func:`_coerce_sandbox` does
    at run time (an unrecognized value silently falls open to ``full-access``).

    ``coercion_note`` is set only when a non-empty configured ``CODEX_SANDBOX``
    was unrecognized and fell open to ``full-access`` — so that silent fail-open
    is surfaced honestly instead of being reported as an intended sandbox.
    """
    override = _config_override_sandbox(variables)
    if override:
        return override, None
    raw = str(variables.get(Var.SANDBOX) or "").strip()
    coerced = _coerce_sandbox(sdk, raw)
    effective = _enum_value(coerced) if coerced is not None else (raw or "full-access")
    note: Optional[str] = None
    if raw and effective != raw:
        note = (
            f"The configured CODEX_SANDBOX '{raw}' is not a recognized sandbox, so "
            f"Codex ran with '{effective}' (full filesystem access). Set a valid "
            "sandbox (read-only, workspace-write, or full-access) with "
            "`cremind tools set-var codex CODEX_SANDBOX=<value>`."
        )
    return effective, note


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


async def prepare_auth(codex, auth: CodexAuth) -> None:
    """Install an API-key credential into the running app-server, before any call.

    Only the API-key tier lands here, because ``login_api_key`` is an RPC the
    already-running server handles. The login tiers are on disk before the
    server exists - the CLI put them there - and are reached purely through
    ``CODEX_HOME``, which :func:`build_config` places in the subprocess env
    BEFORE the client is constructed (the app-server reads ``auth.json`` once,
    at startup, so a home named afterwards is invisible to it).

    A failure is NOT swallowed - it propagates so the caller's exception
    handling surfaces it (classified as an auth failure where recognizable),
    instead of silently proceeding against whatever the store already held."""
    if auth.api_key:
        await codex.login_api_key(auth.api_key)


# ── account model listing ──────────────────────────────────────────────────────
_MODELS_CACHE_TTL = 300.0  # seconds
_MODELS_TIMEOUT = 15.0  # cap the app-server spawn + models() call
# credential fingerprint -> (fetched_at, models). Errors are never cached.
_models_cache: Dict[str, Tuple[float, List[dict]]] = {}


def _cache_key(auth: CodexAuth) -> str:
    """Fingerprint the credential material so cached lists never cross accounts.
    Never uses the raw secret as a dict key.

    A login tier is fingerprinted by its ``CODEX_HOME``, which is the only
    thing that distinguishes one login from another from here: the source label
    alone (what this used to hash) is shared by every profile signed in on its
    own account, so two profiles with their own logins would have read each
    other's model list - a cross-profile leak of which models, and therefore
    which plan, another tenant's account has.
    """
    if auth.api_key:
        material = auth.api_key
    else:
        material = f"home:{auth.env_overrides.get('CODEX_HOME') or ''}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _forget_models_cache(home: str) -> None:
    """Drop the cached model list for one ``CODEX_HOME``.

    Called after a sign-in or a sign-out: the listing is cached per credential
    for five minutes, so without this the card would keep offering the previous
    account's models (or a signed-out home's) for the rest of the window.
    """
    _models_cache.pop(_cache_key(CodexAuth(env_overrides={"CODEX_HOME": home})), None)


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

    async def _fetch():
        # CODEX_HOME rides in the config env, so it is set before the client
        # spawns the app-server that reads auth.json.
        config = build_config(sdk, variables=variables, auth=auth, cwd=None)
        async with sdk.AsyncCodex(config) as codex:
            await prepare_auth(codex, auth)
            return await codex.models()

    try:
        # Bound the app-server spawn + query so a hung/slow launch can't stall the
        # Settings variable-options hook / `cremind tools options codex` (parity
        # with probe_auth, which already has a timeout).
        resp = await asyncio.wait_for(_fetch(), timeout=_MODELS_TIMEOUT)
        rows = getattr(resp, "data", None) or []
    except asyncio.TimeoutError:
        return {
            "models": [],
            "error": f"Listing Codex models timed out after {int(_MODELS_TIMEOUT)}s.",
            "source": auth.source,
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("codex: model listing failed", exc_info=True)
        detail = f"Failed to list Codex models: {exc}"
        if not auth.source:
            detail = _NO_CREDENTIAL_REMEDIATION
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

# Sandbox autonomy classification — the Codex analog of Claude Code's permission
# mode. Kept next to _SANDBOX_LABELS so a new SDK sandbox gets a label AND a
# classification review together.
#   autonomous — full-access: writes anywhere, runs any command
#   edits_only — workspace-write: writes/runs INSIDE the working directory only
#   blocked    — read-only: explore/answer only, cannot make changes
#   unknown    — an unrecognised sandbox value: surface it, never claim blocked
_SANDBOX_AUTONOMOUS = frozenset({"full-access"})
_SANDBOX_EDITS_ONLY = frozenset({"workspace-write"})
_SANDBOX_BLOCKED = frozenset({"read-only"})

_SANDBOX_WRITE_TARGET = "full-access"


def sandbox_autonomy(mode: Optional[str]) -> str:
    """Classify a Codex sandbox as autonomous / edits_only / blocked / unknown."""
    if mode in _SANDBOX_AUTONOMOUS:
        return "autonomous"
    if mode in _SANDBOX_EDITS_ONLY:
        return "edits_only"
    if mode in _SANDBOX_BLOCKED:
        return "blocked"
    return "unknown"


def _sandbox_advisory(mode: Optional[str]) -> Optional[Dict[str, Any]]:
    """A 'confirm once, then fix' advisory for a Codex sandbox that may prevent
    headless changes, or ``None`` when the sandbox is fully autonomous.

    Mirrors the Claude Code permission advisory: names the real lever (Cremind's
    ``CODEX_SANDBOX`` tool variable) and the exact ``cremind tools set-var``
    command, and requires asking the user once before running it."""
    autonomy = sandbox_autonomy(mode)
    if autonomy == "autonomous":
        return None
    command = f"cremind tools set-var codex CODEX_SANDBOX={_SANDBOX_WRITE_TARGET}"
    if autonomy == "edits_only":
        warning = (
            f"Codex ran in sandbox '{mode}', which lets it edit files and run "
            "commands only INSIDE the working directory. If the task needed to "
            "touch files outside the working directory and was blocked, this is why."
        )
    elif autonomy == "blocked":
        warning = (
            f"Codex ran in sandbox '{mode}', which is read-only — it can explore "
            "and answer but CANNOT create or modify files. If the task was meant "
            "to make changes and none were made, this is why."
        )
    else:  # unknown
        warning = (
            f"Codex ran in sandbox '{mode}', which Cremind does not recognise as a "
            "fully-autonomous sandbox. If the task was meant to make changes but "
            "none were made, this sandbox may be the reason."
        )
    return {
        "autonomy": autonomy,
        "warning": warning,
        "cause": (
            "This sandbox is set by Cremind's CODEX_SANDBOX tool variable — NOT by "
            "Codex itself. There is no UI sandbox toggle for the user to flip and "
            "no `codex` CLI command that changes it here."
        ),
        "remediation": (
            "Tell the user Codex is in this sandbox and ask ONCE whether to switch "
            f"it to {_SANDBOX_WRITE_TARGET} (fully autonomous; workspace-write is a "
            "narrower option that confines changes to the working directory). Only "
            "after they agree, run the command below with the Shell Executor tool, "
            "then re-run the task with codex__run (reuse the session_id to "
            "continue). Do NOT change the sandbox without asking first."
        ),
        "command": command,
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


# The Codex account kinds the SDK models, spelled the way Cremind spells them
# everywhere else (``read_codex_account_hint``, the card's label map). The SDK
# uses camelCase discriminators; translating here rather than at three display
# sites keeps one vocabulary for "what kind of account is this?".
_ACCOUNT_TYPES = {
    "apiKey": "api_key",
    "chatgpt": "chatgpt",
    "amazonBedrock": "amazon_bedrock",
}


def account_summary(resp: Any) -> Optional[Dict[str, Any]]:
    """``{"type", "email", "plan_type"}`` from a ``GetAccountResponse``, or None.

    ``resp.account`` is a pydantic ``RootModel`` union (API key / ChatGPT /
    Bedrock), so the real fields live under ``.root`` - reading ``email`` off
    the wrapper silently yields None and the card would show a signed-in
    account with no name on it. Only the non-secret label is taken; nothing
    here may carry a token or a key, because this travels to the UI and into
    the CLI's output.
    """
    account = getattr(resp, "account", None)
    if account is None:
        return None
    inner = _unwrap(account)
    kind = _enum_value(getattr(inner, "type", None))
    return {
        "type": _ACCOUNT_TYPES.get(kind, kind or None),
        "email": str(getattr(inner, "email", "") or "").strip() or None,
        "plan_type": _enum_value(getattr(inner, "plan_type", None)) or None,
    }


async def probe_auth(
    sdk, *, cwd: str, variables: dict, profile: str, timeout: float = 30.0
) -> Dict[str, Any]:
    """Confirm Codex can authenticate by reading the active account (cheap - no
    coding turn, no token spend).

    Returns ``{"logged_in": bool|None, "detail": str, "account": dict|None}``.
    ``logged_in`` is None when the probe could not run (binary missing /
    timeout); ``account`` is the non-secret summary of the credential that
    answered, so the card can say WHICH account is signed in rather than only
    that one is.
    """
    auth = resolve_auth(variables, profile)

    async def _run():
        # CODEX_HOME rides in the config env, so the app-server reads the
        # intended home's auth.json at spawn.
        config = build_config(sdk, variables=variables, auth=auth, cwd=cwd)
        async with sdk.AsyncCodex(config) as codex:
            await prepare_auth(codex, auth)
            return await codex.account()

    try:
        resp = await asyncio.wait_for(_run(), timeout=timeout)
    except FileNotFoundError as exc:
        return {"logged_in": None, "detail": f"Codex binary not found: {exc}", "account": None}
    except asyncio.TimeoutError:
        return {
            "logged_in": None,
            "detail": f"Auth probe timed out after {int(timeout)}s.",
            "account": None,
        }
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if _looks_like_auth_error(text):
            return {"logged_in": False, "detail": text, "account": None}
        return {"logged_in": None, "detail": f"Probe error: {text}", "account": None}

    account = getattr(resp, "account", None)
    # ``account`` alone decides it. ``requires_openai_auth`` reads like "this
    # credential is unusable" and was gated on for exactly that reason, but the
    # bundled binary returns True in EVERY reachable state - an empty home, an
    # API-key home, a ChatGPT home, even immediately after the SDK's own
    # ``login_api_key`` succeeded - so gating on it made ``logged_in`` always
    # False and told users to re-credential a credential Codex was actively
    # using. The SDK ships no description for the field; treat it as a property
    # of the build, not of the account.
    if account is not None:
        return {
            "logged_in": True,
            "detail": "Codex has an active account credential.",
            "account": account_summary(resp),
        }
    return {
        "logged_in": False,
        "detail": "No Codex account credential is active.",
        "account": None,
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

    effective_sandbox, sandbox_note = resolve_sandbox(sdk, variables)
    task = CodexTask(
        task_id=uuid.uuid4().hex[:8],
        context_id=context_id,
        profile=profile,
        prompt=prompt,
        cwd=cwd,
        session_id=session_id,
        resumed_from=session_id,
        model=model,
        sandbox=effective_sandbox,
        sandbox_note=sandbox_note,
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
        # CODEX_HOME rides in the config env, so it is in place before the
        # client spawns the app-server that reads auth.json once.
        config = build_config(sdk, variables=variables, auth=auth, cwd=task.cwd)
        async with sdk.AsyncCodex(config) as codex:
            task.client = codex
            await prepare_auth(codex, auth)
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
        # Classify from more than .message — the SDK may carry the auth reason in
        # .code/.type — so an auth failure isn't mislabeled CodexError (and handed
        # a misleading sandbox_advisory).
        blob = " ".join(
            str(x) for x in (
                detail,
                getattr(error, "code", None),
                getattr(error, "type", None),
                error,
            ) if x
        )
        if _looks_like_auth_error(blob):
            task.result = _auth_failure_payload(task, detail=detail)
        else:
            # A non-auth failure may stem from a restrictive sandbox; attach the
            # advisory here (never on the auth path). Autonomous sandboxes yield
            # None, which _failure_payload filters out.
            task.result = _failure_payload(
                task,
                error="CodexError",
                message=f"Codex did not complete successfully. {detail}".strip(),
                duration_ms=getattr(turn, "duration_ms", None),
                sandbox_advisory=_sandbox_advisory(task.sandbox),
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
    result_text = _final_response_from_items(getattr(turn, "items", None))
    payload: Dict[str, Any] = {
        "status": "completed",
        "task_id": task.task_id,
        "session_id": task.session_id,
        "result": result_text,
        "duration_ms": getattr(turn, "duration_ms", None),
        "usage": task.token_usage,
        "working_directory": task.cwd,
        "effective_sandbox": task.sandbox,
        "note": (
            "To continue this coding session with a follow-up instruction, call "
            "codex__run with this session_id (the Codex thread)."
        ),
    }

    # Empty-resume guard: a resumed thread that cannot be continued comes back
    # with no final message. Codex reports no turn count, so gate on empty result
    # AND a resume attempt so a legitimately-empty fresh completion is untouched.
    if not result_text.strip() and task.resumed_from:
        payload["resume_produced_no_work"] = True
        payload["result"] = (
            "(no output) Resuming this Codex session produced no work — the thread "
            "may have expired or is no longer resumable. Do NOT treat this as 'the "
            "task produced no output'. Start a fresh task with codex__run WITHOUT a "
            "session_id, repeating the full task brief."
        )

    advisory = _sandbox_advisory(task.sandbox)
    if advisory is not None:
        payload["sandbox_advisory"] = advisory
    if task.sandbox_note:
        payload["sandbox_coercion_note"] = task.sandbox_note
    return payload


def _failure_payload(task: CodexTask, *, error: str, message: str, **extra: Any) -> Dict[str, Any]:
    payload = {
        "status": "failed",
        "task_id": task.task_id,
        "session_id": task.session_id,
        "error": error,
        "message": message,
        "working_directory": task.cwd,
        "effective_sandbox": task.sandbox,
    }
    if task.sandbox_note:
        payload["sandbox_coercion_note"] = task.sandbox_note
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


def _auth_failure_payload(task: CodexTask, *, detail: str, **extra: Any) -> Dict[str, Any]:
    return _failure_payload(
        task,
        error="AuthenticationError",
        message=("Codex could not authenticate with OpenAI. " + (detail or "")).strip(),
        remediation=_SIGN_IN_REMEDIATION,
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
