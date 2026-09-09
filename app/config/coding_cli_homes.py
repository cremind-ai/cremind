"""Where the ``claude`` and ``codex`` CLIs keep their logins - per profile.

Both coding-agent tools authenticate the way their CLI does: from a directory on
disk (``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME``) that the CLI itself writes when
the user runs ``claude auth login`` / ``codex login``. Cremind is multi-profile,
and one shared directory is the wrong home for that: a member profile signing in
from a Cremind terminal would overwrite the server operator's login, and every
profile would then be running as whichever account signed in last. So each
profile gets its own pair of homes under
``<CREMIND_SYSTEM_DIR>/<profile>/coding-cli/{claude,codex}``, and the server's
own login - the one an operator made on the host, or inside the container - is
kept as a read-only fallback for profiles that never signed in.

This module owns those paths and the "is there a login in here?" question, and
nothing else: no subprocess, no SDK, no network. The two runners, the
``/api/coding-agents`` endpoints, the CLI and profile deletion all resolve
through here so they can never disagree about which home a run authenticated
from - and so a credential's on-disk location is stated once rather than
re-derived in five places.

Environment variables are read LIVE on every call, and that is deliberate. The
code this replaces derived ``_CLAUDE_CREDENTIALS_PATH`` / ``_CODEX_AUTH_PATH``
at import time, so the only way to keep a test off the developer's own
``~/.claude`` was to monkeypatch those private module constants - the tests
pinned private names, and anything that computed a path for itself silently used
the real home anyway. The isolation story here is the ordinary one instead: set
``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME`` (or ``BaseConfig.CREMIND_SYSTEM_DIR``)
and every function in this module follows.

Import discipline mirrors :mod:`app.config.runtime_env`: stdlib at module level,
``app.config.settings`` / ``app.config.runtime_env`` imported inside the
functions. The CLI and the installer both want these answers and neither can
afford to drag the settings stack in as a side effect of asking where a login
lives.
"""

from __future__ import annotations

import base64
import json
import os
import platform
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# One directory name on both sides - ``<SYSDIR>/<profile>/coding-cli/...`` for a
# profile, ``<SYSDIR>/coding-cli/...`` for the install-wide shared login. Using
# the same word for both is what makes the one-line support answer ("your CLI
# logins are under coding-cli") true wherever the user looks.
_CLI_DIRNAME = "coding-cli"


@dataclass(frozen=True)
class ResolvedHome:
    """Which CLI home a run will authenticate from, and whether it holds a login.

    ``scope`` is ``"profile"`` when the login belongs to this profile alone and
    ``"shared"`` when it is the server's - the UI, the CLI table and the tool's
    status leaf all label the credential with it, because "signed in" means
    something different to a member profile borrowing the operator's account
    than to a profile that signed in itself (only the latter can sign out).

    ``path`` is a plain ``str`` and not a ``Path`` because every consumer puts it
    straight into a subprocess environment (``{"CODEX_HOME": home.path}``), and a
    ``Path`` value there raises ``TypeError`` out of ``subprocess`` / ``asyncio``
    on Windows.
    """

    path: str
    scope: str  # "profile" | "shared"
    has_login: bool


def _system_dir() -> Path:
    from app.config.settings import BaseConfig

    return Path(BaseConfig.CREMIND_SYSTEM_DIR)


def _in_container() -> bool:
    """Docker/Kubernetes, as :mod:`app.config.runtime_env` decides it.

    Looked up through the module rather than imported by name so that a test
    patching ``runtime_env.is_container`` patches it for this module too - the
    container answer is what flips the shared homes between the System Directory
    and ``~``, and every path in here has to move with it.
    """
    from app.config import runtime_env

    return runtime_env.is_container()


# -- Per-profile homes ---------------------------------------------------------


def profile_cli_root(profile: str) -> Path:
    """``<CREMIND_SYSTEM_DIR>/<profile>/coding-cli`` - one profile's CLI logins.

    Under the profile directory (beside ``skills``, ``agent_name.txt``, ...) and
    not under a shared ``coding-cli/<profile>`` tree, so that everything a
    profile owns stays in the one place a backup, a copy or an eyeball finds it.
    """
    return _system_dir() / profile / _CLI_DIRNAME


def profile_claude_config_dir(profile: str) -> Path:
    """``CLAUDE_CONFIG_DIR`` for one profile's own ``claude auth login``."""
    return profile_cli_root(profile) / "claude"


def profile_codex_home(profile: str) -> Path:
    """``CODEX_HOME`` for one profile's own ``codex login``."""
    return profile_cli_root(profile) / "codex"


# -- The server's shared login -------------------------------------------------


def _shared_home(env_var: str, dirname: str, native_name: str) -> Path:
    """The install-wide CLI home: env var, else container default, else ``~``.

    The env var wins because it is how the operator (and the Dockerfile and the
    Helm chart) *state* the home: the containers export
    ``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME`` so that the app, a ``docker exec``
    shell and the VNC desktop's terminal all sign in to the same directory
    instead of three that look identical from inside their own session.

    The container default exists for the case where nothing states it: in a
    container ``~`` is ``/root``, which is not a volume - only ``~/.cremind``
    and the venv survive - so a login written to ``/root/.codex`` disappears on
    the next ``docker compose down``, image upgrade or pod replacement, and the
    user is signed out by an upgrade with no explanation. Under the System
    Directory it persists. This default is the reason a Kubernetes pod running
    newer venv code on an *older* image (the venv PVC upgrades in place, the
    image does not) still keeps its logins.

    On a native install ``~`` is the right answer and the only one the user
    expects: it is where their own ``claude``/``codex`` already signed in.
    """
    stated = (os.environ.get(env_var) or "").strip()
    if stated:
        return Path(stated)
    if _in_container():
        return _system_dir() / _CLI_DIRNAME / dirname
    return Path.home() / native_name


def shared_claude_config_dir() -> Path:
    """The server's own ``CLAUDE_CONFIG_DIR`` (see :func:`_shared_home`)."""
    return _shared_home("CLAUDE_CONFIG_DIR", "claude", ".claude")


def shared_codex_home() -> Path:
    """The server's own ``CODEX_HOME`` (see :func:`_shared_home`)."""
    return _shared_home("CODEX_HOME", "codex", ".codex")


# -- "Is there a login in this home?" ------------------------------------------


def _read_json(path: Path) -> dict:
    """Parse ``path`` as a JSON object, or return ``{}``.

    Every caller here is answering "is the user signed in?", and the honest
    answer for a half-written, truncated or hand-edited credential file is "no"
    - never an exception. These files are written by another process (the CLI,
    possibly mid-refresh), so a malformed read is an expected state, not a bug
    worth propagating into a status endpoint.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _claude_config_json_paths(config_dir: Path) -> list[Path]:
    """Where the Claude CLI keeps ``.claude.json`` for ``config_dir``.

    With ``CLAUDE_CONFIG_DIR`` set the CLI writes it INSIDE that directory; with
    the variable unset it writes it beside the directory, as ``~/.claude.json``.
    Both are checked because the unset case is exactly the home this module
    still has to read: a host login made before Cremind ever set the variable,
    and the legacy ``~/.claude`` inside an older container image.
    """
    inside = config_dir / ".claude.json"
    beside = config_dir.parent / f"{config_dir.name}.json"
    return [inside] if beside == inside else [inside, beside]


def claude_login_present(config_dir: Path | str) -> bool:
    """Does ``config_dir`` hold a completed ``claude auth login``? Never raises.

    ``.credentials.json`` with a ``claudeAiOauth.accessToken`` is the marker on
    Linux and Windows. macOS has no such file at all - the CLI puts the
    credential in the login Keychain, which is per OS user and not readable
    from here - so there we fall back to a non-empty ``oauthAccount`` in
    ``.claude.json``, which the CLI does write next to the Keychain entry. That
    fallback is why per-profile isolation of Claude logins is best-effort on
    macOS: the directories differ, but the credential behind them is one
    Keychain entry shared by the OS user, and the doc says so.
    """
    home = Path(config_dir)
    oauth = _read_json(home / ".credentials.json").get("claudeAiOauth")
    if isinstance(oauth, dict) and str(oauth.get("accessToken") or "").strip():
        return True
    if platform.system() != "Darwin":
        return False
    for path in _claude_config_json_paths(home):
        account = _read_json(path).get("oauthAccount")
        if isinstance(account, dict) and account:
            return True
    return False


def codex_login_present(home: Path | str) -> bool:
    """Does ``home`` hold a completed ``codex login``? Never raises.

    ``auth.json`` carries either an API key or the ChatGPT OAuth tokens, and the
    ChatGPT flow writes ``"OPENAI_API_KEY": null`` alongside its ``tokens``
    block - so this is a truthiness check on three keys, not a membership test.
    Both spellings of the key are accepted because the CLI has used both.
    """
    data = _read_json(Path(home) / "auth.json")
    return bool(data.get("OPENAI_API_KEY") or data.get("tokens") or data.get("openai_api_key"))


# -- Resolution: profile login, else the server's ------------------------------


def _resolve(
    profile_dir: Path | None,
    shared_dir: Path,
    legacy_dir: Path,
    has_login: Callable[[Path], bool],
) -> ResolvedHome:
    """Shared body of :func:`resolve_claude_config_dir` / :func:`resolve_codex_home`.

    Order: the profile's own login -> the server's shared login -> (containers
    only) the legacy ``~`` home -> the shared directory as the write target.

    The legacy tier is not tidiness. Inside a container ``~`` stops being the
    shared home the moment the image (or the chart) starts exporting
    ``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME``, and a Kubernetes pod picks up new
    venv code before it picks up a new image - so an operator who signed in
    yesterday has their credential in ``/root/.claude`` and would be silently
    signed out by an upgrade. It is scoped ``"shared"``, because it is the
    server's login and not this profile's, and it is skipped on native installs
    where ``~`` either *is* the shared home already or was deliberately
    overridden by the operator's own env var.

    When nothing holds a login the answer is still the shared home, never an
    empty per-profile one: that is where a login made outside Cremind (``claude
    auth login`` on the host, or in the container's VNC terminal) lands, so a
    run pointed there starts working the moment the operator signs the server
    in, while a run pointed at an empty profile directory would keep reporting
    "not signed in" afterwards.
    """
    if profile_dir is not None and has_login(profile_dir):
        return ResolvedHome(str(profile_dir), "profile", True)
    if has_login(shared_dir):
        return ResolvedHome(str(shared_dir), "shared", True)
    if _in_container() and legacy_dir != shared_dir and has_login(legacy_dir):
        return ResolvedHome(str(legacy_dir), "shared", True)
    return ResolvedHome(str(shared_dir), "shared", False)


def resolve_claude_config_dir(profile: str | None) -> ResolvedHome:
    """The ``CLAUDE_CONFIG_DIR`` a Claude Code run for ``profile`` should use."""
    return _resolve(
        profile_claude_config_dir(profile) if profile else None,
        shared_claude_config_dir(),
        Path.home() / ".claude",
        claude_login_present,
    )


def resolve_codex_home(profile: str | None) -> ResolvedHome:
    """The ``CODEX_HOME`` a Codex run for ``profile`` should use."""
    return _resolve(
        profile_codex_home(profile) if profile else None,
        shared_codex_home(),
        Path.home() / ".codex",
        codex_login_present,
    )


def coding_cli_env(profile: str | None, *, force_profile: bool = False) -> dict[str, str]:
    """The ``{CLAUDE_CONFIG_DIR, CODEX_HOME}`` block for a child process.

    The default is the *resolved* pair, so a run inherits the server's login
    when the profile has none of its own - that is what makes a fresh profile
    able to use the tools at all.

    ``force_profile=True`` returns the profile's own homes whether or not they
    hold anything, and is what a sign-in must use: a login terminal spawned with
    the resolved homes would write the new credential into the *shared* home and
    overwrite the operator's login - precisely the accident this module exists
    to prevent. With no profile there is nothing to force, so both modes give
    the shared homes.
    """
    if force_profile and profile:
        return {
            "CLAUDE_CONFIG_DIR": str(profile_claude_config_dir(profile)),
            "CODEX_HOME": str(profile_codex_home(profile)),
        }
    return {
        "CLAUDE_CONFIG_DIR": resolve_claude_config_dir(profile).path,
        "CODEX_HOME": resolve_codex_home(profile).path,
    }


def remove_profile_cli_homes(profile: str) -> bool:
    """Delete ``profile``'s CLI homes. Never raises. True when a tree is gone.

    Deleting a profile does not remove ``<SYSDIR>/<profile>`` wholesale (its
    skills are deliberately kept), so these homes have to be taken explicitly:
    ``auth.json`` and ``.credentials.json`` hold long-lived OAuth refresh tokens
    that nothing revokes upstream, and a user who deleted a profile must not
    keep carrying its live credential into every later backup or ``~/.cremind``
    copy.

    Never raises because it runs inside the profile-delete path: a file another
    process has open (Windows) or a permission the server does not have must
    leave the profile deletable, not wedge the request.
    """
    root = profile_cli_root(profile)
    try:
        if not root.exists():
            return False
        shutil.rmtree(root, ignore_errors=True)
        return not root.exists()
    except Exception:  # noqa: BLE001
        from app.utils.logger import logger

        logger.debug(f"coding-cli: could not remove the CLI homes for '{profile}'", exc_info=True)
        return False


# -- Non-secret account hints --------------------------------------------------
#
# "Signed in" on its own is not enough for the card or the CLI table: a user with
# two accounts needs to see WHICH one this home is signed in as before they trust
# a run to it. These read the account label out of the same files the login
# markers check - and only the label. A token or an API key must never leave this
# module: the hints travel to the UI over the API and into the CLI's output.


def read_claude_account_hint(config_dir: Path | str) -> dict | None:
    """``{"type": "oauth", "email", "org_name"}`` for a Claude home, or None.

    ``oauthAccount`` in ``.claude.json`` is what the CLI records about the
    account it signed in as; anything else in that file (project history, MCP
    servers) is none of our business. ``None`` means "no account recorded
    here", which is also what a missing or malformed file reads as.
    """
    for path in _claude_config_json_paths(Path(config_dir)):
        account = _read_json(path).get("oauthAccount")
        if isinstance(account, dict) and account:
            email = str(account.get("emailAddress") or "").strip() or None
            org = str(account.get("organizationName") or "").strip() or None
            return {"type": "oauth", "email": email, "org_name": org}
    return None


def _decode_jwt_payload(token: str) -> dict:
    """The claims of a JWT, WITHOUT verifying it. ``{}`` on anything unexpected.

    This is a local read of a credential we already hold, purely to show the
    user which account a home belongs to - there is no security decision behind
    it, so there is nothing to verify and no key to verify against. Doing it by
    hand (base64url + json) rather than through PyJWT keeps this module free of
    dependencies and makes it obvious at the call site that no network call and
    no signature check is implied.
    """
    parts = str(token or "").split(".")
    if len(parts) < 2 or not parts[1]:
        return {}
    raw = parts[1]
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        claims = json.loads(decoded.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return claims if isinstance(claims, dict) else {}


def read_codex_account_hint(home: Path | str) -> dict | None:
    """``{"type": "api_key"}`` or ``{"type": "chatgpt", "email", "plan_type"}``.

    The API key is checked first and matches :func:`codex_login_present`'s
    order, which is safe because the ChatGPT flow writes ``"OPENAI_API_KEY":
    null`` - a home signed in with ChatGPT never trips the key branch. The key
    itself is never returned; that a key is installed is the whole hint.

    The ChatGPT branch reads the account label out of the ``id_token`` the CLI
    stored, which is the only place ``auth.json`` records who signed in. A token
    we cannot decode still says ``chatgpt`` with empty fields - the login is
    real either way, and claiming "not signed in" over an unreadable label would
    be the worse lie.
    """
    data = _read_json(Path(home) / "auth.json")
    if not data:
        return None
    if data.get("OPENAI_API_KEY") or data.get("openai_api_key"):
        return {"type": "api_key"}
    tokens = data.get("tokens")
    if not isinstance(tokens, dict) or not tokens:
        return None
    claims = _decode_jwt_payload(str(tokens.get("id_token") or ""))
    auth_claim = claims.get("https://api.openai.com/auth")
    auth_claim = auth_claim if isinstance(auth_claim, dict) else {}
    profile_claim = claims.get("https://api.openai.com/profile")
    profile_claim = profile_claim if isinstance(profile_claim, dict) else {}
    email = str(claims.get("email") or profile_claim.get("email") or "").strip() or None
    plan = str(auth_claim.get("chatgpt_plan_type") or "").strip() or None
    return {"type": "chatgpt", "email": email, "plan_type": plan}
