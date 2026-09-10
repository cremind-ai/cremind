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

Credentials are the CLI's, not the profile's LLM settings. Claude Code
authenticates the way the ``claude`` CLI does - from its own
``CLAUDE_CONFIG_DIR`` (see :mod:`app.config.coding_cli_homes`), the tool's own
API-key or pasted-``setup-token`` variable, or the server environment. It
deliberately does NOT read the profile's Anthropic provider credentials: a user
who signs in to Claude Code
with their Claude subscription and separately configures an Anthropic API key
for Cremind's own reasoning model expects those to stay separate, and the old
chain silently billed coding tasks to the second one.
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import hashlib
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import httpx

from app.agent.agent_activity import AgentActivity
# Imported as a MODULE, not as ``from ... import cpu_features``: the whole point
# of :func:`host_blocker` is that a test can claim to be running on a CPU other
# than the one under the suite, and a name bound at import time could not be
# moved. ``runtime_env`` is stdlib-only at import time (everything from ``app``
# is imported inside its functions), so this costs the runner nothing.
from app.config import runtime_env
from app.config.coding_cli_homes import (
    claude_login_present,
    profile_claude_config_dir,
    read_claude_account_hint,
    resolve_claude_config_dir,
    shared_claude_config_dir,
)
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

# The one sentence every "Claude Code cannot authenticate" answer ends with -
# the failed run's payload, the model listing, the status leaf and the API all
# quote this constant so a user is never told three different things about the
# same problem. It names the CLI's own sign-in (which is where the credential
# actually lives now) before the API-key escape hatch, and deliberately does NOT
# mention Settings -> LLM Providers: the Anthropic provider configured there is
# Cremind's own reasoning credential and has nothing to do with this tool.
_SIGN_IN_REMEDIATION = (
    "Sign in: Settings -> Tools & Skills -> Coding Agents -> Claude Code -> Sign in "
    "(runs `claude auth login` in the built-in terminal), or "
    "`cremind tools coding-agents login claude_code` on the server host; with no "
    "browser reachable from the server, run `claude setup-token` on any machine "
    "that has one and paste the token into the same dialog (the "
    "CLAUDE_CODE_OAUTH_TOKEN tool variable); or set CLAUDE_CODE_API_KEY / "
    "ANTHROPIC_API_KEY."
)

# The name of the one host condition that stops the CLI before it starts. It
# rides every payload as ``code`` so a client can branch on the cause without
# matching on prose, and so a second condition (should one ever be found) is an
# added constant rather than a new shape.
HOST_BLOCKER_CPU = "cpu_features"

# What to do about it when a hypervisor is between us and the silicon, which is
# the case this was written for: the instructions are physically there and the
# virtual CPU model simply does not advertise them, so the fix is one setting on
# the host and costs nothing. The three hypervisors are named because an operator
# who has never had to think about a guest CPU model does not know where the knob
# is, and the wording of each one is the label in that product's own UI.
_VM_CPU_REMEDIATION = (
    "The instructions are almost certainly present on the physical host and only "
    "hidden by the guest CPU model, so the fix is on the hypervisor: in Proxmox, "
    "set the VM's Hardware -> Processors -> Type to 'host' (or to 'x86-64-v2-AES' "
    "if the cluster needs a portable model). In libvirt / virt-manager, use "
    "<cpu mode='host-passthrough'/> - the \"Copy host CPU configuration\" "
    "checkbox. With plain QEMU, pass `-cpu host` (or `-cpu x86-64-v3`) instead of "
    "letting it default to `qemu64`. The node has to be shut down and started "
    "again afterwards - a live reboot keeps the old CPU model."
)

# And when the flags really are absent from the hardware. There is no setting to
# change, so the only honest advice is a different machine - said plainly, with
# the scope of the check attached so nobody reads it as "Cremind needs a newer
# CPU" and moves an install that is otherwise perfectly happy.
_HARDWARE_CPU_REMEDIATION = (
    "No hypervisor is reporting here, so this looks like real hardware that "
    "predates the x86-64-v2 instruction level (roughly 2009 and earlier): there "
    "is no setting to change, and Cremind has to run on a newer host for Claude "
    "Code to work. This check covers the Claude Code CLI only - the rest of "
    "Cremind, including the Codex coding agent, is unaffected by it."
)


class Var:
    """required_config variable keys (also imported by the leaf module)."""

    MODEL = "CLAUDE_CODE_MODEL"
    PERMISSION_MODE = "CLAUDE_CODE_PERMISSION_MODE"
    MAX_TURNS = "CLAUDE_CODE_MAX_TURNS"
    MAX_BUDGET_USD = "CLAUDE_CODE_MAX_BUDGET_USD"
    API_KEY = "CLAUDE_CODE_API_KEY"
    OAUTH_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"
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
    Var.OAUTH_TOKEN: "",
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


class ClaudeCodeHostError(Exception):
    """Raised by :func:`start_task` when this host cannot run the CLI at all.

    Separate from :class:`ClaudeCodeConcurrencyError` because it is not a
    "try again later": no task is registered, no activity feed is opened and no
    SDK client is constructed, since the binary the client would spawn is the
    thing that cannot run. The whole diagnosis travels on ``blocker`` (the dict
    :func:`host_blocker` returns) so the leaf can hand the model the CPU model,
    the missing instructions and the remedy without re-deriving any of it.
    """

    def __init__(self, blocker: Dict[str, Any]):
        super().__init__(
            blocker.get("message") or "This host cannot run the Claude Code CLI."
        )
        self.blocker = blocker


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
    """The credential env a Claude Code child process runs with.

    ``CLAUDE_CONFIG_DIR`` is ALWAYS set, even when an API key wins: it is not
    only where the CLI looks for a login, it is also where it keeps this
    profile's session transcripts, so a run left to inherit the server
    operator's home would resume the wrong profile's sessions and write its own
    into a directory another profile can read. The tool's own API-key and
    long-lived-token variables are layered on top when set; everything else (a
    key in the server environment, an OAuth token, the login inside the resolved
    home) is already visible to the CLI and needs no override from us.

    Both key variables may be exported together, and that is not a conflict to
    resolve here: the CLI itself prefers ``CLAUDE_CODE_OAUTH_TOKEN`` over
    ``ANTHROPIC_API_KEY`` (see the precedence note above
    :data:`_CLI_KEY_AUTH_METHODS`), so blanking one of them would only take a
    decision away from the binary that makes it.

    Never raises: a credential *failure* is reported later, from the SDK result
    or from :func:`auth_status`, where the user can be told what to do about it.
    """
    overrides: Dict[str, str] = {
        "CLAUDE_CONFIG_DIR": resolve_claude_config_dir(profile).path,
    }
    explicit = str((variables or {}).get(Var.API_KEY) or "").strip()
    if explicit:
        overrides["ANTHROPIC_API_KEY"] = explicit
    token = str((variables or {}).get(Var.OAUTH_TOKEN) or "").strip()
    if token:
        overrides["CLAUDE_CODE_OAUTH_TOKEN"] = token
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


# The credential tiers, in the order CREMIND ranks them. A key is put above a
# login because an operator who sets CLAUDE_CODE_API_KEY is stating which account
# should be billed for coding work; the CLI logins are what a user without a key
# signs in with, per profile first and the server's shared login as the inherited
# fallback. The pasted `claude setup-token` token leads the whole list because
# the CLI itself prefers it over an API key when both are exported (measured -
# see the precedence note above :data:`_CLI_KEY_AUTH_METHODS`), and a tier
# ranking that named the key while the binary ran on the token would describe a
# credential no run uses.
#
# This is a statement of intent, not an observation of the binary, and it must
# never be used to decide WHICH CREDENTIAL TO CHECK: an ANTHROPIC_API_KEY the
# host exports for some other service outranks a profile's own `claude auth
# login` here, while the CLI carries on using the login. :func:`probe_auth` asks
# the CLI instead (``authMethod``); this ranking answers the different, local
# question "what can Cremind see, and what would it hand a run?".
_KEY_CREDENTIAL_SOURCES = frozenset({
    "tool_variable_oauth_token",
    "tool_variable_api_key",
    "env_anthropic_api_key",
    "env_oauth_token",
})
_LOGIN_CREDENTIAL_SOURCES = frozenset({"profile_claude_login", "host_claude_login"})


def _source_for_home(variables: dict, home) -> Optional[str]:
    """The credential tier Cremind ranks first, given an already-resolved home."""
    if str((variables or {}).get(Var.OAUTH_TOKEN) or "").strip():
        return "tool_variable_oauth_token"
    if str((variables or {}).get(Var.API_KEY) or "").strip():
        return "tool_variable_api_key"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "env_anthropic_api_key"
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "env_oauth_token"
    if home.has_login:
        return "profile_claude_login" if home.scope == "profile" else "host_claude_login"
    return None


def credential_info(variables: dict, profile: str) -> Dict[str, Any]:
    """What Claude Code would authenticate as for ``profile``, without secrets.

    ``{"source", "scope", "cli_home", "account_hint"}``:

    - ``source`` names the winning tier (see :data:`_KEY_CREDENTIAL_SOURCES` /
      :data:`_LOGIN_CREDENTIAL_SOURCES`), or None when nothing is visible.
    - ``scope`` is ``"profile"`` / ``"shared"`` for a *login* tier only, and
      None for the key tiers and for "no credential". It is what the card gates
      its Sign-out button on, and there is nothing to sign out of an API key or
      of a home that never signed in.
    - ``cli_home`` is always the resolved ``CLAUDE_CONFIG_DIR`` - the answer to
      "where did this run authenticate from, and where do its sessions live?",
      which is true regardless of which tier won.
    - ``account_hint`` is the account recorded in that home (email / org), again
      only for a login tier.

    On macOS the login itself lives in the Keychain rather than in the home, so
    a Mac host can report ``None`` while ``claude`` is in fact signed in - the
    probe (``claude auth status``) is what settles it there.

    This is what Cremind can see WITHOUT running anything, so it is the answer
    for a listing. It is not a promise about the binary: when a key and a login
    are both present the CLI may well use the login, and only :func:`probe_auth`
    - which asks it - can say. Prefer that call's ``credential_source`` wherever
    a payload also carries a probe verdict, or the two halves of the same card
    end up describing different credentials.
    """
    home = resolve_claude_config_dir(profile)
    source = _source_for_home(variables, home)
    is_login = source in _LOGIN_CREDENTIAL_SOURCES
    return {
        "source": source,
        "scope": home.scope if is_login else None,
        "cli_home": home.path,
        "account_hint": read_claude_account_hint(home.path) if is_login else None,
    }


def credential_source(variables: dict, profile: str) -> Optional[str]:
    """The winning credential tier for ``profile`` (see :func:`credential_info`)."""
    return credential_info(variables, profile)["source"]


# --- Account model listing -------------------------------------------------
#
# Neither claude_agent_sdk nor the bundled ``claude`` CLI exposes a way to
# enumerate the models available to the logged-in account, so we query the
# Anthropic REST ``/v1/models`` endpoint directly with whatever credential the
# coding task would use. This lets the Settings dropdown and the ``cremind
# tools options`` CLI present a live, account-scoped model list.

_MODELS_URL = "https://api.anthropic.com/v1/models"
_MODELS_PATH = "/v1/models"
_BASE_URL_VAR = "ANTHROPIC_BASE_URL"
_ANTHROPIC_VERSION = "2023-06-01"
_OAUTH_BETA = "oauth-2025-04-20"
_MODELS_TIMEOUT = 10.0
_MODELS_CACHE_TTL = 300.0  # seconds
_MODEL_ALIASES = ("sonnet", "opus", "haiku", "opusplan")

# credential fingerprint -> (fetched_at, models). Errors are never cached.
_models_cache: Dict[str, Tuple[float, List[dict]]] = {}


def _models_url() -> str:
    """``/v1/models`` on the host the ``claude`` CLI itself would call.

    The CLI honours ``ANTHROPIC_BASE_URL`` (that is how a gateway / proxy install
    is wired), and a child process inherits it from us - so the credential this
    install holds is scoped to that gateway, not to Anthropic. Hardcoding
    api.anthropic.com sends a gateway-scoped key to a host that has never heard
    of it, gets a 401 back, and reports a working install as signed out. The
    public endpoint stays the default because that is what an install with
    nothing stated uses.
    """
    base = (os.environ.get(_BASE_URL_VAR) or "").strip().rstrip("/")
    return (base + _MODELS_PATH) if base else _MODELS_URL


def _read_claude_credentials(config_dir: Any) -> Optional[str]:
    """Best-effort read of ``claudeAiOauth.accessToken`` out of one CLI home.

    Takes the directory rather than reading a module-level path so that each
    profile's own home is read, and so tests isolate by pointing
    ``CLAUDE_CONFIG_DIR`` / the System Directory somewhere else instead of
    monkeypatching a private constant. Returns None on any problem - including
    macOS, where the token is in the Keychain and no such file exists (the model
    fetch then degrades to "no credential", which is what it always did there).
    """
    try:
        path = Path(config_dir) / ".credentials.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        token = (data.get("claudeAiOauth") or {}).get("accessToken")
        return str(token) if token else None
    except Exception:  # noqa: BLE001
        logger.debug("claude_code: reading the claude credential store failed", exc_info=True)
        return None


def _api_key_headers(key: str) -> Dict[str, str]:
    return {"x-api-key": key, "anthropic-version": _ANTHROPIC_VERSION}


def _oauth_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-version": _ANTHROPIC_VERSION,
        "anthropic-beta": _OAUTH_BETA,
    }


def _login_source_label(home) -> str:
    """``profile_claude_login`` / ``host_claude_login`` for a resolved home."""
    return "profile_claude_login" if home.scope == "profile" else "host_claude_login"


def _headers_for_source(
    variables: dict, profile: str, source: Optional[str]
) -> Dict[str, str]:
    """Request headers for ONE named credential tier, or ``{}``.

    Asking for a tier BY NAME is the point: ranking the tiers again here is how a
    check ends up sending an ``ANTHROPIC_API_KEY`` while the CLI is running on
    the profile's own login (see :func:`probe_auth`). ``{}`` means "that tier's
    credential cannot be read from here" - an empty variable, or a login whose
    token lives in the macOS Keychain - which is a reason not to make the call at
    all, never a reason to fall back to a different credential.
    """
    if source == "tool_variable_oauth_token":
        token = str((variables or {}).get(Var.OAUTH_TOKEN) or "").strip()
        return _oauth_headers(token) if token else {}
    if source == "tool_variable_api_key":
        key = str((variables or {}).get(Var.API_KEY) or "").strip()
        return _api_key_headers(key) if key else {}
    if source == "env_anthropic_api_key":
        key = os.environ.get("ANTHROPIC_API_KEY") or ""
        return _api_key_headers(key) if key else {}
    if source == "env_oauth_token":
        token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or ""
        return _oauth_headers(token) if token else {}
    if source in _LOGIN_CREDENTIAL_SOURCES:
        token = _read_claude_credentials(resolve_claude_config_dir(profile).path)
        return _oauth_headers(token) if token else {}
    return {}


def _build_models_headers(variables: dict, profile: str) -> Tuple[Dict[str, str], Optional[str]]:
    """Resolve credentials into request headers for ``GET /v1/models``.

    Returns ``(headers, source_label)`` where ``source_label`` reuses the
    :func:`credential_source` vocabulary, or ``({}, None)`` when no credential
    can be found.

    The tiers are the same ones :func:`credential_info` reports, in the same
    order, and the OAuth token is read from the RESOLVED home - so the model
    list a profile sees is the one its own login can reach, not the server
    operator's. The two functions can still disagree in one direction on macOS,
    where a login is detectable (``.claude.json``) but its token is not
    (Keychain): the listing then degrades to "no credential" while the status
    still says signed in, which is the honest reading of both.

    This ranking answers "which credential can Cremind list models with?", which
    is the right question for the model dropdown and the WRONG one for the
    sign-in probe - only the CLI knows which credential a run would use.
    """
    for source in (
        "tool_variable_oauth_token",
        "tool_variable_api_key",
        "env_anthropic_api_key",
        "env_oauth_token",
    ):
        headers = _headers_for_source(variables, profile, source)
        if headers:
            return headers, source
    label = _login_source_label(resolve_claude_config_dir(profile))
    headers = _headers_for_source(variables, profile, label)
    return (headers, label) if headers else ({}, None)


async def _fetch_models(headers: Dict[str, str]) -> List[dict]:
    """One ``GET /v1/models`` call. Returns the raw ``data`` rows. Raises on
    HTTP or transport failure so :func:`list_models` can surface the detail."""
    async with httpx.AsyncClient(timeout=_MODELS_TIMEOUT) as client:
        resp = await client.get(_models_url(), params={"limit": 1000}, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, list) else []


def _cache_key(headers: Dict[str, str]) -> str:
    """Fingerprint the credential material so cached lists never cross accounts.
    Never uses the raw secret as a dict key.

    The endpoint is part of the fingerprint: the same key can front two different
    gateways, and a list fetched from one is not the other's answer.
    """
    material = (headers.get("x-api-key") or headers.get("Authorization") or "")
    return hashlib.sha256(f"{_models_url()}\n{material}".encode("utf-8")).hexdigest()


async def list_models(
    variables: dict, profile: str, *, force_refresh: bool = False
) -> Dict[str, Any]:
    """List the Claude models available to the resolved account. Never raises.

    Returns ``{"models": [{"id", "display_name", "created_at"}...], "source":
    label, "cached": bool}`` on success, or ``{"models": [], "error": "<detail>",
    "source": label|None, "rejected": bool}`` when no credential is available or
    the fetch fails.

    The credential is chosen by :func:`_build_models_headers`'s ranking, which is
    what a model dropdown wants. The sign-in probe does NOT come through here -
    it names the tier the CLI reported and calls :func:`_models_listing` with it.
    """
    headers, source = _build_models_headers(variables, profile)
    if not headers:
        return {
            "models": [],
            "error": "No Anthropic credential available. " + _SIGN_IN_REMEDIATION,
            "source": None,
            "rejected": False,
        }
    return await _models_listing(headers, source, force_refresh=force_refresh)


async def _models_listing(
    headers: Dict[str, str], source: Optional[str], *, force_refresh: bool = False
) -> Dict[str, Any]:
    """One model listing for an ALREADY-CHOSEN credential. Never raises.

    ``rejected`` is True only when the API itself REFUSED the credential
    (401/403). A transport failure, a proxy or a 500 leaves the credential
    unjudged, and the distinction is load-bearing: :func:`probe_auth` uses this
    call to validate a key, and reporting "not signed in" for a network blip
    would send a user whose key is fine off to sign in again.
    """
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
        rejected = status in (401, 403)
        detail = f"Anthropic API returned HTTP {status}"
        if rejected:
            detail += " (credential rejected)"
        return {"models": [], "error": detail, "source": source, "rejected": rejected}
    except Exception as exc:  # noqa: BLE001
        logger.debug("claude_code: model listing failed", exc_info=True)
        return {
            "models": [],
            "error": f"Failed to list models: {exc}",
            "source": source,
            "rejected": False,
        }

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


# --- The `claude` CLI itself: sign in, sign out, and ask who is signed in -----
#
# The Python SDK exposes no auth API at all, so every question about the login
# is answered by the same binary the user would type: the SDK's bundled
# ``claude``. ``claude auth status --json`` is the important one - it is local,
# unbilled and non-interactive, and it replaced a probe that ran a real one-turn
# query (a live, billed API call every time a user pressed "Check sign-in").

_CLI_NAME = "claude"
_LOGIN_BINARY_NAME = f"{_CLI_NAME}.exe" if os.name == "nt" else _CLI_NAME
_STATUS_TIMEOUT = 15.0
_LOGOUT_TIMEOUT = 15.0


def _locate_cli(variables: Optional[dict]) -> Tuple[Optional[str], Optional[str]]:
    """``(binary, source)`` for the ``claude`` CLI, or ``(None, None)``.

    Order: the ``CLAUDE_CODE_CLI_PATH`` tool variable (an operator pointing at
    their own build) -> the one bundled inside the installed ``claude_agent_sdk``
    wheel -> whatever ``claude`` is on PATH. The bundled copy comes before PATH
    because it is the binary the SDK itself drives: signing in with a different
    ``claude`` from PATH would write a login the coding tasks never read.

    ``source`` is the non-secret label the ``/cli`` route publishes -
    ``tool_variable`` / ``bundled`` / ``path``. Never raises: a namespace
    package with no ``__file__``, or an SDK wheel built without the binary,
    just falls through to the next tier.
    """
    explicit = str((variables or {}).get(Var.CLI_PATH) or "").strip()
    if explicit:
        return explicit, "tool_variable"
    sdk, _err = load_sdk()
    if sdk is not None:
        try:
            bundled = Path(sdk.__file__).parent / "_bundled" / _LOGIN_BINARY_NAME
            if bundled.is_file():
                return str(bundled), "bundled"
        except Exception:  # noqa: BLE001 - a namespace package has no __file__
            logger.debug("claude_code: locating the bundled CLI failed", exc_info=True)
    found = shutil.which(_CLI_NAME)
    if found:
        return found, "path"
    return None, None


def find_cli(variables: Optional[dict] = None) -> Optional[str]:
    """The ``claude`` binary this install would run, or None if there is none.

    Deliberately the same signature and return shape as
    :func:`app.tools.builtin.codex_runner.find_cli`, so the sign-in routes and
    the ``cremind tools coding-agents`` commands drive both coding agents
    through one code path instead of two.
    """
    return _locate_cli(variables)[0]


def cli_binary_source(variables: Optional[dict] = None) -> Optional[str]:
    """Where :func:`find_cli`'s answer came from: ``tool_variable`` /
    ``bundled`` / ``path``, or None when there is no binary."""
    return _locate_cli(variables)[1]


def host_blocker(variables: Optional[dict] = None) -> Optional[Dict[str, Any]]:
    """Why this host cannot run the ``claude`` binary at all, or ``None``.

    There is exactly one such reason today, and it is not a Cremind bug we can
    fix: the CLI inside the claude-agent-sdk wheel is a Bun single-file
    executable built for the x86-64-v2 instruction level, and on a virtual CPU
    that advertises none of those instructions - the ``qemu64`` model a
    default-configured Proxmox / libvirt / QEMU guest gets - every subcommand
    except ``--version`` spins at 100% CPU forever. Not fails: spins, in
    userspace, before the CLI writes its first log line, because under KVM the
    instructions do execute while Bun's CPUID-based dispatch reports them absent
    and takes a fallback path that never terminates. Measured on such a node:
    user CPU equal to wall time on one thread, an empty ``/proc/<pid>/syscall``,
    not a single socket opened, and no ``--debug`` log written at all.

    So every surface asks this first and refuses in milliseconds with the CPU
    model, the missing instructions and the hypervisor setting that fixes them -
    where before, the sign-in probe burned its 15s timeout to say "could not
    tell", the login terminal was a black box, and a coding task hung until the
    SDK's own timeout.

    ``{"code", "cpu_model", "missing", "hypervisor", "message", "remedy"}``, or
    ``None`` when nothing is known to be wrong. ``message`` is the one sentence
    every surface shows and ``remedy`` the fix for the kind of host this is; the
    other fields are there so a client can render the diagnosis its own way.

    Only x86-64-**v2** is required, deliberately. The wheel ships the baseline
    build precisely so that v2 hosts work, and a modest-but-real CPU that has
    all of v2 and none of v3 runs the binary perfectly well - requiring v3 here
    would block hosts where nothing is wrong. That is also why
    :func:`app.config.runtime_env.cpu_features` reports the v3 flags without
    ever acting on them.

    The binary itself is not inspected for the level it was built at, even
    though the answer is in there somewhere: it is a ~200MB file that would have
    to be scanned on every coding-agents listing, and what a scan could find -
    an instruction-set name in a string table - is a heuristic about a build we
    do not control, not a fact. The CPU's own flag list is cheap (the
    /proc/cpuinfo read is cached for the life of the process, which matters
    because the listing calls this once per row) and it is checkable.

    Never raises, and never guesses: ``flags_known`` False - macOS, Windows, an
    ARM pod, a kernel whose /proc looks different - means every caller carries
    on and finds out the slow way, which is the right trade. A false "unknown"
    costs one broken node a slow failure; a false "blocked" takes the tool away
    from every host where it works.
    """
    try:
        cpu = runtime_env.cpu_features()
        if not cpu.get("flags_known"):
            return None
        absent = set(cpu.get("missing") or ())
        missing = [flag for flag in runtime_env.X86_64_V2_FLAGS if flag in absent]
        if not missing:
            return None

        hypervisor = bool(cpu.get("hypervisor"))
        model = cpu.get("model") or None
        # A quoted model name is what a support conversation and a hypervisor
        # config screen both key on ("QEMU Virtual CPU version 2.5+" is the
        # whole diagnosis on the node this was written for), so it leads - and
        # says so plainly when /proc/cpuinfo had no ``model name`` line.
        model_label = f'"{model}"' if model else "(model unknown)"
        # "this virtual CPU" is not a flourish: it is the difference between a
        # setting to change and a machine to replace, and the remedy below picks
        # the same fork.
        cpu_label = "this virtual CPU" if hypervisor else "this CPU"
        # Both failure shapes are named whichever host this is. An operator
        # reading the message has usually already run `claude` by hand, and the
        # thing they need to recognise is the symptom they actually saw.
        pointed_elsewhere = cli_binary_source(variables) == "tool_variable"
        cli_path_clause = (
            "pointing CLAUDE_CODE_CLI_PATH at another copy does not help, and "
            "the copy it points at now is affected in exactly the same way"
            if pointed_elsewhere
            else "pointing CLAUDE_CODE_CLI_PATH at another copy does not help"
        )
        message = (
            "The Claude Code CLI cannot run on this server's CPU: it is a Bun "
            "single-file executable built for the x86-64-v2 instruction level, "
            f"and {cpu_label} {model_label} advertises none of "
            f"{', '.join(missing)}; under a hypervisor that makes every "
            "subcommand spin at 100% CPU forever instead of failing, with only "
            "`claude --version` still answering, and on real hardware without "
            'those instructions it dies with "Illegal instruction"; every '
            f"Claude Code build is the same kind of executable, so {cli_path_clause}."
        )
        return {
            "code": HOST_BLOCKER_CPU,
            "cpu_model": model,
            "missing": missing,
            "hypervisor": hypervisor,
            "message": message,
            "remedy": _VM_CPU_REMEDIATION if hypervisor else _HARDWARE_CPU_REMEDIATION,
        }
    except Exception:  # noqa: BLE001
        # This answer gates a listing endpoint, a tool catalogue and every
        # sign-in surface, so an unexpected shape from the probe must degrade to
        # "nothing known to be wrong" rather than turn any of them into a 500.
        logger.debug("claude_code: the host CPU check failed", exc_info=True)
        return None


def login_argv(binary: str) -> List[str]:
    """``claude auth login`` - interactive; must run under a PTY, not here."""
    return [binary, "auth", "login"]


def logout_argv(binary: str) -> List[str]:
    return [binary, "auth", "logout"]


def status_argv(binary: str) -> List[str]:
    return [binary, "auth", "status", "--json"]


def _auth_status_unknown(detail: str) -> Dict[str, Any]:
    """A status result meaning "cannot tell", which is NOT "signed out".

    The distinction is load-bearing: a card that says "Not signed in" because
    the CLI is missing or timed out sends the user off to fix a login that is
    already fine. ``logged_in`` None must therefore stay None all the way to the
    UI, with the reason in ``detail``.
    """
    return {
        "logged_in": None,
        "auth_method": None,
        "api_provider": None,
        "email": None,
        "org_name": None,
        "subscription_type": None,
        "detail": detail,
    }


def _first_json_object(text: str) -> Optional[dict]:
    """The first JSON object embedded in ``text``, or None.

    ``claude auth status --json`` can print a banner / update notice before its
    payload, so scanning for the first decodable ``{...}`` is the only reliable
    read - ``json.loads`` on the whole stream would fail on exactly the installs
    that print the banner.
    """
    decoder = json.JSONDecoder()
    index = text.find("{")
    while index >= 0:
        try:
            value, _end = decoder.raw_decode(text[index:])
        except ValueError:
            value = None
        if isinstance(value, dict):
            return value
        index = text.find("{", index + 1)
    return None


def _text_field(value: Any) -> Optional[str]:
    if value is None or isinstance(value, (dict, list)):
        return None
    return str(value).strip() or None


async def _terminate(proc: Any) -> None:
    """Kill a child that outstayed its timeout. Best-effort, never raises."""
    try:
        proc.kill()
    except Exception:  # noqa: BLE001 - already gone / not a real process
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except Exception:  # noqa: BLE001
        pass


async def auth_status(
    variables: dict,
    profile: str,
    *,
    config_dir: Optional[Any] = None,
    timeout: float = _STATUS_TIMEOUT,
) -> Dict[str, Any]:
    """Ask ``claude auth status --json`` who is signed in. Local and unbilled.

    Returns ``{"logged_in": bool|None, "auth_method", "api_provider", "email",
    "org_name", "subscription_type", "detail"}``. ``config_dir`` overrides the
    home to inspect (the API uses it to report a specific scope); by default the
    resolved home for ``profile`` is used.

    The JSON is parsed regardless of exit code, because a signed-out CLI is
    entitled to exit non-zero while still telling us ``{"loggedIn": false}`` -
    and that answer is more useful than "the command failed". Anything that
    leaves us unable to read an answer (no binary, a timeout, no JSON at all)
    returns ``logged_in`` None with the reason.
    """
    # A host the binary cannot run on is asked nothing at all. This is the
    # lowest point every login question passes through, and the spawn below is
    # exactly the call that would sit at 100% CPU until the timeout killed it -
    # after which the user was told "could not tell" with no idea why.
    blocked = host_blocker(variables)
    if blocked is not None:
        return _auth_status_unknown(blocked["message"])

    binary = find_cli(variables)
    if not binary:
        return _auth_status_unknown(
            "The Claude Code CLI binary was not found, so the login could not be "
            "checked. Install the claude_code feature (`cremind features install "
            "claude_code`) or set the CLAUDE_CODE_CLI_PATH tool variable."
        )

    env = {**os.environ, **resolve_auth_env(variables, profile)}
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    home = env["CLAUDE_CONFIG_DIR"]

    try:
        proc = await asyncio.create_subprocess_exec(
            *status_argv(binary),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Left unset, the child inherits the SERVER's stdin. A credential
            # probe that stops to read a terminal nobody is typing into would
            # burn the whole timeout and then be killed, and the user is told
            # "could not tell" where an answer was available all along.
            stdin=asyncio.subprocess.DEVNULL,
            env=env,
        )
    except FileNotFoundError as exc:
        return _auth_status_unknown(
            f"The Claude Code CLI could not be started ({binary}): {exc}"
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("claude_code: auth status could not be spawned", exc_info=True)
        return _auth_status_unknown(f"`claude auth status` could not be run: {exc}")

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _terminate(proc)
        return _auth_status_unknown(
            f"`claude auth status` did not answer within {int(timeout)}s."
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("claude_code: auth status failed", exc_info=True)
        return _auth_status_unknown(f"`claude auth status` failed: {exc}")

    out = (stdout or b"").decode("utf-8", "replace")
    data = _first_json_object(out)
    if data is None:
        noise = ((stderr or b"").decode("utf-8", "replace").strip() or out.strip())[:300]
        return _auth_status_unknown(
            "`claude auth status --json` printed no JSON object"
            + (f": {noise}" if noise else ".")
        )

    raw_logged_in = data.get("loggedIn")
    logged_in = bool(raw_logged_in) if isinstance(raw_logged_in, bool) else None
    result: Dict[str, Any] = {
        "logged_in": logged_in,
        "auth_method": _text_field(data.get("authMethod")),
        "api_provider": _text_field(data.get("apiProvider")),
        "email": _text_field(data.get("email")),
        "org_name": _text_field(data.get("orgName")),
        "subscription_type": _text_field(data.get("subscriptionType")),
    }
    if logged_in is True:
        who = result["email"] or result["org_name"] or "an account"
        extra = ", ".join(
            part for part in (result["auth_method"], result["subscription_type"]) if part
        )
        result["detail"] = (
            f"Signed in as {who}" + (f" ({extra})" if extra else "") + f" in {home}."
        )
    elif logged_in is False:
        result["detail"] = f"The Claude CLI reports no login in {home}."
    else:
        result["detail"] = (
            "`claude auth status --json` did not report a login state for " + home + "."
        )
    return result


# ``claude auth status`` can only answer for a LOGIN. It reports ``loggedIn``
# true for ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN the moment the variable is
# merely PRESENT - it never asks Anthropic whether the value works. Observed
# against the bundled binary (claude_agent_sdk 0.2.116) with an empty
# ``CLAUDE_CONFIG_DIR``: a bogus key exits 0 printing ``{"loggedIn": true,
# "authMethod": "api_key", "apiKeySource": "ANTHROPIC_API_KEY"}``, and a bogus
# ``CLAUDE_CODE_OAUTH_TOKEN`` exits 0 printing ``"authMethod": "oauth_token"``;
# only with neither set does it print ``{"loggedIn": false}``. On top of that,
# :func:`resolve_auth_env` hands the CLAUDE_CODE_API_KEY tool variable to that
# same process, so all three key tiers get the same free pass. A probe that
# stopped at the first check answering "yes" therefore called every stale,
# revoked or mistyped key "Signed in" - green chip, LOGGED_IN yes in the CLI,
# and every actual run failing. So the credential's TIER, not the order the
# checks happen to run in, decides which check is authoritative.
#
# The same binary also settles which of two present key credentials it uses, and
# the answer is the TOKEN. Observed against the bundled CLI (2.1.207) with a
# scratch ``CLAUDE_CONFIG_DIR``: ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN
# both set prints ``{"loggedIn": true, "authMethod": "oauth_token",
# "apiKeySource": "ANTHROPIC_API_KEY"}`` - it names the key it can see and then
# authenticates with the token anyway - while either one alone prints that one's
# own ``authMethod``. That is why the ``tool_variable_oauth_token`` tier is
# ranked above ``tool_variable_api_key`` rather than below it: a user who pastes
# a setup-token while a key is still configured must be told about the token,
# because that is what the run will use. The pre-existing
# ``env_anthropic_api_key``-before-``env_oauth_token`` order is left as it is -
# it is a statement of billing intent like the rest of the ranking, and the
# ``prefer_oauth_token`` branch of :func:`_visible_key_source` already corrects
# it wherever an actual verdict is being formed.
#
# The tier itself comes from the CLI's own ``authMethod`` and NOT from
# :func:`credential_source`. Cremind's ranking puts a key in the server
# environment above the CLI's own login, which is a statement about billing
# intent, not about what the binary does: on a host that exports
# ANTHROPIC_API_KEY for some other service, every profile with its own ``claude
# auth login`` had that unrelated key validated in its login's place - a working
# install reported as signed out, and (with a live key over a dead login) a
# broken one reported as fine. ``claude auth status`` runs with the exact
# environment :func:`resolve_auth_env` gives a real run, so its answer to "which
# credential am I using?" is the only one that can be checked.
_CLI_KEY_AUTH_METHODS = frozenset({"api_key", "apikey", "api_key_helper", "oauth_token"})
_CLI_NO_CREDENTIAL_AUTH_METHODS = frozenset({"none", "not_logged_in", "logged_out"})

# What is added to the CLI's own words when nothing was actually verified. The
# card and the `cremind tools coding-agents` table read this line, and both would
# otherwise render "the CLI holds a credential" as "the credential works".
_UNVERIFIED_LOGIN_NOTE = (
    "The `claude` CLI holds this login locally and Cremind did not verify it with "
    "Anthropic, so a login that has since expired or been revoked still reads as "
    "present here - only a real run can settle that."
)
_UNREADABLE_KEY_NOTE = (
    "The `claude` CLI reports a key credential that Cremind cannot read (it is "
    "not the CLAUDE_CODE_API_KEY tool variable and not a key in the server "
    "environment), so it was not verified - only a real run can settle that."
)


def _cli_auth_tier(status: Dict[str, Any]) -> Optional[str]:
    """``"key"`` / ``"login"`` / ``"none"`` - what the CLI said it would use.

    ``None`` means the CLI never told us (no binary, a timeout, output with no
    JSON in it), which is NOT "no credential": the caller has to stay in "cannot
    tell". An ``authMethod`` this build has never heard of is treated as a login
    - i.e. settled locally - because the alternative is to make a network claim
    about a credential we do not understand. That fallback is what keeps a
    Bedrock / Vertex install honest: its credential is AWS's or Google's, and
    asking api.anthropic.com about it would only ever produce a wrong answer.
    """
    method = str(status.get("auth_method") or "").strip().lower()
    if not method:
        return None
    if method in _CLI_NO_CREDENTIAL_AUTH_METHODS:
        return "none"
    if method in _CLI_KEY_AUTH_METHODS:
        return "key"
    return "login"


def _visible_key_source(variables: dict, *, prefer_oauth_token: bool = False) -> Optional[str]:
    """The key tier Cremind can actually produce a secret for, or None.

    ``prefer_oauth_token`` is set when the CLI said ``oauth_token``: with both
    variables present the default ranking would hand back the API key, and the
    probe would then judge a credential the CLI is not using. Within that branch
    the tool variable comes before the environment for the same reason - when a
    user has pasted a ``claude setup-token`` token, that is the token
    :func:`resolve_auth_env` exported into the process the CLI just answered
    for, so it is the one its ``oauth_token`` verdict is about.
    """
    if prefer_oauth_token:
        pasted = str((variables or {}).get(Var.OAUTH_TOKEN) or "").strip()
        if pasted:
            return "tool_variable_oauth_token"
        if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            return "env_oauth_token"
    if str((variables or {}).get(Var.OAUTH_TOKEN) or "").strip():
        return "tool_variable_oauth_token"
    if str((variables or {}).get(Var.API_KEY) or "").strip():
        return "tool_variable_api_key"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "env_anthropic_api_key"
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "env_oauth_token"
    return None


def _probe_credential(
    variables: dict, profile: str, status: Dict[str, Any]
) -> Tuple[str, Optional[str]]:
    """``(tier, source)`` for the credential a run would really authenticate with.

    ``tier`` is ``"key"`` (validate it), ``"login"`` (settled locally, never
    verified), ``"none"`` (nothing to check) or ``"unknown"`` (the CLI could not
    be asked and Cremind cannot tell which of two credentials would win).

    The one place a guess is still allowed is the ``unknown`` tier with a key and
    NO login in sight: there is only one credential a run could possibly use, so
    validating it cannot be about the wrong one.

    A login is labelled by the scope of the home the CLI was pointed at, which is
    the home it just read. The one blurry case is macOS, where the credential is
    in the Keychain: if resolution could not see a login at all it falls back to
    the shared home, so a profile's own Keychain login can be reported as the
    server's. The scope is cosmetic here - the verdict is the CLI's either way.
    """
    tier = _cli_auth_tier(status)
    method = str(status.get("auth_method") or "").strip().lower()
    home = resolve_claude_config_dir(profile)
    if tier == "key":
        return "key", _visible_key_source(
            variables, prefer_oauth_token=(method == "oauth_token")
        )
    if tier == "login":
        return "login", _login_source_label(home)
    if tier == "none":
        return "none", None
    key_source = _visible_key_source(variables)
    if key_source and not home.has_login:
        return "key", key_source
    return "unknown", credential_source(variables, profile)


async def _probe_key_credential(
    headers: Dict[str, str], source: Optional[str]
) -> Tuple[Optional[bool], str]:
    """Validate a *key* credential against the API. ``(logged_in, detail)``.

    ``GET /v1/models`` is a listing, not an inference call: it proves the key is
    accepted without spending anything, which is the whole reason the probe may
    make this call at all. ``headers`` are built for ONE named tier, so the
    verdict can only ever be about the credential the caller reports.

    The answer is deliberately tri-state. Only the API refusing the credential
    (401/403) means "not signed in"; anything that stopped the call from being
    answered - no network, a proxy, a 500 - leaves the key unjudged, and must
    stay None so a user whose key is fine is never sent off to sign in again.
    """
    listing = await _models_listing(headers, source, force_refresh=True)
    error = listing.get("error")
    if not error:
        return True, f"The {source} credential authenticates against the Anthropic API."
    if listing.get("rejected"):
        return False, f"The {source} credential was rejected by the Anthropic API: {error}"
    return None, (
        f"The {source} credential could not be checked against the Anthropic API: "
        f"{error}. That is not the same as being signed out."
    )


def _with_note(detail: Any, note: str) -> str:
    """The CLI's own detail with a caveat appended (never instead of it)."""
    text = str(detail or "").strip()
    return f"{text} {note}" if text else note


async def probe_auth(sdk, *, cwd: str, variables: dict, profile: str, timeout: float = 30.0) -> Dict[str, Any]:
    """Confirm Claude Code can authenticate - WITHOUT spending anything.

    Keeps the old signature (``sdk`` / ``cwd`` are now unused) because the leaf
    and the API both call it positionally, but the implementation no longer runs
    a one-turn coding query: that was a real, billed API call fired every time
    someone clicked "Check sign-in".

    ``claude auth status`` runs first, with the environment a real run gets, and
    its ``authMethod`` names the credential the CLI would use (see the note above
    :func:`_cli_auth_tier`). That, not Cremind's own tier ranking, decides which
    check is authoritative:

    - a **key** tier (the tool variable, or a key / OAuth token in the server
      environment) is settled by the unbilled ``GET /v1/models`` listing - on the
      same ``ANTHROPIC_BASE_URL`` the CLI would call - because ``claude auth
      status`` reports a key it has never validated as logged in;
    - a **login** tier is settled by ``claude auth status``, which reads the CLI's
      own login store - the only thing that knows about it - and makes no network
      call at all;
    - with **no** credential, or with the CLI unable to answer while both a key
      and a login are in sight, nothing is validated: there is either nothing to
      check or no way to know which credential the answer would be about.

    Returns :func:`auth_status`'s shape plus:

    - ``credential_source`` - the tier the verdict is ABOUT, so a caller never
      reports one credential while a different one was checked;
    - ``credential_verified`` - True / False only when the API itself answered
      for that credential, and None whenever nothing was proved. This is the
      field that separates "the CLI holds a credential" from "the credential
      works": a local status check cannot tell a live OAuth login from an expired
      or revoked one, so ``logged_in`` True on a login tier means *held*, and
      only ``credential_verified`` True means *verified*;
    - ``account`` when signed in, kept from the CLI's own report even when the
      models call is what settled the verdict.

    ``logged_in`` None keeps meaning "cannot tell", never "signed out".
    """
    # Checked here as well as inside auth_status, and not for the 15s: the key
    # tier below is settled by an Anthropic API call, which would answer "the
    # credential works" on a host where nothing can use it. A visible API key
    # would then earn a green "signed in" chip for an install whose every coding
    # task hangs - the most misleading answer this function can produce.
    blocked = host_blocker(variables)
    if blocked is not None:
        result = _auth_status_unknown(blocked["message"])
        result["credential_source"] = credential_source(variables, profile)
        result["credential_verified"] = None
        return result

    result = dict(await auth_status(variables, profile, timeout=timeout))

    tier, source = _probe_credential(variables, profile, result)
    result["credential_source"] = source
    result["credential_verified"] = None

    if tier == "key":
        headers = _headers_for_source(variables, profile, source)
        if headers:
            verdict, detail = await _probe_key_credential(headers, source)
            result["logged_in"] = verdict
            result["detail"] = detail
            result["credential_verified"] = verdict
        else:
            # An apiKeyHelper, or a variable that emptied out between the CLI's
            # read and ours: there is nothing to send, and sending some OTHER
            # credential would answer a question nobody asked.
            result["detail"] = _with_note(result.get("detail"), _UNREADABLE_KEY_NOTE)
    elif tier == "login" and result.get("logged_in") is True:
        # Deliberately not validated. The access token on disk is short-lived and
        # refreshed by the CLI, so a 401 on it would fail a login that works; on
        # macOS it is in the Keychain and unreadable from here at all. Presence is
        # all a local check can honestly report - so say so.
        result["detail"] = _with_note(result.get("detail"), _UNVERIFIED_LOGIN_NOTE)

    if result.get("logged_in") is True:
        result["account"] = {
            "auth_method": result.get("auth_method"),
            "email": result.get("email"),
            "org_name": result.get("org_name"),
            "subscription_type": result.get("subscription_type"),
        }
    return result


async def logout(variables: dict, profile: str, *, scope: str = "profile") -> Dict[str, Any]:
    """Sign one CLI home out of Claude Code. Never raises.

    Returns ``{"ok", "scope", "home", "detail"}`` (the Codex runner's shape, so
    one API handler and one card can drive both). ``scope="profile"`` targets
    ``<SYSDIR>/<profile>/coding-cli/claude`` and ``scope="shared"`` the server's
    own home. The RESOLVED home is deliberately not used: a profile with no
    login of its own resolves to the shared one, so "Sign out" on its card would
    have signed the whole server out. The caller states which of the two it
    means (and the API refuses ``shared`` for a non-admin).

    ``claude auth logout`` runs first so the CLI's own bookkeeping happens (the
    macOS Keychain entry, the ``.claude.json`` account block), and
    ``.credentials.json`` is then unlinked whatever the command did - including
    when there is no binary at all, and including on a host whose CPU cannot run
    it. That last case used to spend 15s at 100% CPU, get killed, and unlink the
    file anyway; skipping the spawn changes nothing about the outcome and the
    reason joins ``problems`` so the user is told why the CLI's own bookkeeping
    did not happen. The user asked to be signed out; leaving a live OAuth
    refresh token on disk because a command failed is the wrong failure mode.
    ``ok`` reports the only thing that matters - whether the home still reads as
    signed in - and ``detail`` carries what went wrong on the way.

    The model cache is cleared unconditionally: its entries are keyed by
    credential fingerprint, so a stale entry would keep answering with the
    signed-out account's model list.
    """
    target = Path(
        shared_claude_config_dir() if scope == "shared" else profile_claude_config_dir(profile)
    )
    problems: List[str] = []

    blocked = host_blocker(variables)
    binary = find_cli(variables)
    if blocked is not None:
        problems.append(
            "the Claude Code CLI cannot run on this server's CPU, so `claude auth "
            "logout` was skipped and only the stored credential was removed"
        )
    elif not binary:
        problems.append("the Claude Code CLI was not found, so only the stored credential was removed")
    else:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *logout_argv(binary),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # As with the status probe: an inherited server stdin lets a
                # confirmation prompt hold the command open until the timeout
                # kills it, which reads to the user as a sign-out that failed.
                stdin=asyncio.subprocess.DEVNULL,
                env={**os.environ, "CLAUDE_CONFIG_DIR": str(target)},
            )
            await asyncio.wait_for(proc.communicate(), timeout=_LOGOUT_TIMEOUT)
            code = getattr(proc, "returncode", 0)
            if code:
                problems.append(f"`claude auth logout` exited {code}")
        except asyncio.TimeoutError:
            if proc is not None:
                await _terminate(proc)
            problems.append(f"`claude auth logout` timed out after {int(_LOGOUT_TIMEOUT)}s")
        except Exception as exc:  # noqa: BLE001
            logger.debug("claude_code: the logout command failed", exc_info=True)
            problems.append(f"`claude auth logout` failed: {exc}")

    credentials = target / ".credentials.json"
    try:
        if credentials.exists():
            credentials.unlink()
    except OSError as exc:
        problems.append(f"could not remove {credentials}: {exc}")

    _models_cache.clear()

    if not claude_login_present(target):
        return {
            "ok": True,
            "scope": scope,
            "home": str(target),
            "detail": (
                "Signed out of Claude Code ("
                + ("the shared server login" if scope == "shared" else "this profile")
                + ")."
                + (" Note: " + "; ".join(problems) + "." if problems else "")
            ),
        }
    return {
        "ok": False,
        "scope": scope,
        "home": str(target),
        "detail": (
            "The Claude Code login is still present after signing out"
            + (": " + "; ".join(problems) if problems else ".")
        ),
    }


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
    :class:`ClaudeCodeConcurrencyError` or :class:`ClaudeCodeHostError`.

    The host check comes before everything else, registry included: a task that
    cannot start must leave nothing behind that a later call would find and
    report as "running", and on the node this was written for it would have sat
    at 100% CPU until the SDK's own timeout with an activity feed open in front
    of the user the whole time.
    """
    blocker = host_blocker(variables)
    if blocker is not None:
        raise ClaudeCodeHostError(blocker)

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
        remediation=_SIGN_IN_REMEDIATION,
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
