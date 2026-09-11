"""System variables registry.

Single source of truth for the env-var block injected into subprocesses
spawned by built-in tools (currently only ``exec_shell``).

Each entry in :data:`SYSTEM_VARS` pairs a canonical env-var name with a
resolver callable. Resolvers receive the active profile (may be ``None``)
and return the value as a string, or ``None`` to omit the variable from
the spawned env. Values are computed lazily on every call to
:func:`build_system_env` so that runtime changes to the underlying
config (port, working dir, profile token) are picked up immediately.

To add a new variable: append one :class:`SystemVarSpec` to
:data:`SYSTEM_VARS`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from app.config.coding_cli_homes import profile_claude_config_dir, profile_codex_home
from app.config.oauth_loopback import google_redirect_uri
from app.config.settings import BaseConfig, get_user_working_directory
from app.utils.logger import logger

# Shared backend OAuth callback path for the built-in skills (served by
# app/api/oauth_callback.py). The Google skills advertise a loopback origin +
# this; the Atlassian skills advertise a single FIXED URL ending in the same path
# (defaulted in the jira/confluence skill), because its 3LO Web client allows only
# one registered, exact-match callback per app. One route suffices because the
# per-flow ``state`` (not the path) disambiguates the provider/flow.
_OAUTH_CALLBACK_PATH = "/api/oauth/callback"

# The Google skills' redirect follows the rule every Google flow shares
# (app/config/oauth_loopback.py). Their "Desktop" OAuth client accepts only an
# ``http://`` loopback redirect — a real hostname (Ingress/domain/LAN) is refused
# before consent, and so is ``https://localhost`` (the installed-app loopback flow,
# RFC 8252 §7.3, is specified over plain HTTP).
#
# An ``https`` loopback APP_URL — what the installers now default local installs
# to — still yields a redirect: ``http`` on the SAME host and port. The same-port
# TLS listener answers that plaintext request by redirecting exactly the Google
# callback paths on to their HTTPS handler, so the consent redirect lands without
# Google ever seeing an https URI. Advertising ``https://localhost:1515/...``
# instead fails mid-flow with redirect_uri_mismatch; omitting the variable, which
# is what this used to do, pushed every TLS install onto the manual paste.
#
# An operator's loopback pin in the server's own environment is honoured next (a
# port-forward in front of an Ingress install). With neither, the variable is
# omitted and the backend-managed skills fall back to their built-in
# ``http://localhost:1515/api/oauth/callback`` — captured when a port-forward
# makes that address reach this server, otherwise finished with ``complete-link``.


def _resolve_google_redirect_uri(_profile: Optional[str]) -> Optional[str]:
    """Browser-facing Google OAuth redirect for the Google skills, or ``None``
    to let each skill apply its own default (see the comment above)."""
    return google_redirect_uri(_OAUTH_CALLBACK_PATH, fallback=False)


def _load_cremind_token(profile: Optional[str]) -> Optional[str]:
    """Read the per-profile CREMIND_TOKEN from ``<CREMIND_SYSTEM_DIR>/tokens/<profile>.token``.

    Returns the stripped token string, or ``None`` if the profile is unset,
    the file is missing, or it cannot be read. Failure is non-fatal —
    callers should simply omit CREMIND_TOKEN from the spawned env.
    """
    if not profile:
        return None
    token_path = os.path.join(BaseConfig.CREMIND_SYSTEM_DIR, "tokens", f"{profile}.token")
    try:
        with open(token_path, "r", encoding="utf-8") as f:
            token = f.read().strip()
        return token or None
    except FileNotFoundError:
        logger.warning(f"Cremind token file missing for profile '{profile}': {token_path}")
        return None
    except OSError as e:
        logger.warning(f"Could not read Cremind token for profile '{profile}' ({token_path}): {e}")
        return None


def _resolve_skill_dir(profile: Optional[str]) -> Optional[str]:
    if not profile:
        return None
    # Lazy import: app.skills.sync pulls in the watcher / tool registry chain.
    from app.skills.sync import profile_skills_dir
    return str(profile_skills_dir(profile))


def _resolve_agent_name(profile: Optional[str]) -> Optional[str]:
    if not profile:
        return None
    # Lazy import to keep this module's import graph light.
    from app.utils.agent_name import read_agent_name
    return read_agent_name(profile)


def _resolve_claude_config_dir(profile: Optional[str]) -> Optional[str]:
    """The profile's own Claude Code CLI home - never the shared one.

    Deliberately the PROFILE directory rather than what
    ``coding_cli_homes.resolve_claude_config_dir`` would pick: this block goes
    into every shell Cremind spawns (exec_shell, autostart scripts, the built-in
    terminal), so a member profile that runs ``claude auth login`` in one of
    them must land in its own home. Resolving here would hand it the server's
    shared home whenever the profile has no login yet - and the first sign-in
    from any profile would overwrite the operator's account for everybody. The
    fallback still exists where it belongs: the *tool* reads the shared login
    when the profile has none.
    """
    if not profile:
        return None
    return str(profile_claude_config_dir(profile))


def _resolve_codex_home(profile: Optional[str]) -> Optional[str]:
    """The profile's own Codex CLI home; see :func:`_resolve_claude_config_dir`."""
    if not profile:
        return None
    return str(profile_codex_home(profile))


@dataclass(frozen=True)
class SystemVarSpec:
    name: str
    resolve: Callable[[Optional[str]], Optional[str]]
    description: str = ""
    secret: bool = False


SYSTEM_VARS: list[SystemVarSpec] = [
    SystemVarSpec(
        name="CREMIND_SYSTEM_DIR",
        resolve=lambda _profile: BaseConfig.CREMIND_SYSTEM_DIR,
        description="Cremind System Directory (~/.cremind) — runtime state + user content root.",
    ),
    SystemVarSpec(
        name="CREMIND_INSTALL_DIR",
        resolve=lambda _profile: BaseConfig.CREMIND_INSTALL_DIR,
        description="Cremind Install Directory — install-time scratch (compose bundle, install.log, caches).",
    ),
    SystemVarSpec(
        name="CREMIND_USER_WORKING_DIR",
        resolve=lambda _profile: get_user_working_directory(),
        description="User-facing default working directory.",
    ),
    SystemVarSpec(
        name="CREMIND_SKILL_DIR",
        resolve=_resolve_skill_dir,
        description="Per-profile skills directory; omitted when no profile.",
    ),
    SystemVarSpec(
        name="CREMIND_SERVER",
        resolve=lambda _profile: f"http://127.0.0.1:{BaseConfig.PORT}",
        description="Loopback URL of this server for the `cremind` CLI.",
    ),
    SystemVarSpec(
        name="CREMIND_PROFILE",
        resolve=lambda profile: profile or None,
        description="Active profile name; omitted when no profile is set.",
    ),
    SystemVarSpec(
        name="CREMIND_AGENT_NAME",
        resolve=_resolve_agent_name,
        description="The agent's display name for this profile; omitted when no profile.",
    ),
    SystemVarSpec(
        name="CREMIND_TOKEN",
        resolve=_load_cremind_token,
        description="Per-profile Cremind token; omitted when missing.",
        secret=True,
    ),
    SystemVarSpec(
        name="CREMIND_OAUTH_REDIRECT_URI",
        resolve=_resolve_google_redirect_uri,
        description=(
            "Browser-facing Google OAuth redirect for the Google skills: "
            "http://<loopback host>:<port>/api/oauth/callback, derived from a "
            "loopback APP_URL (an https one maps to http on the same port, which "
            "this server redirects to its HTTPS handler — only when this process "
            "serves that port itself, not behind edge TLS termination or with "
            "CREMIND_UI_PORT=0) or a loopback operator pin. The skill "
            "advertises it and the backend captures the consent "
            "redirect into the oauth_inbox. Omitted otherwise (Google Desktop "
            "clients accept only http loopback redirects); the skill then uses "
            "http://localhost:1515/api/oauth/callback, finished by complete-link "
            "when that address does not reach this server."
        ),
    ),
    SystemVarSpec(
        name="CLAUDE_CONFIG_DIR",
        resolve=_resolve_claude_config_dir,
        description=(
            "Per-profile Claude Code CLI home, so `claude` in a Cremind shell "
            "signs this profile in instead of overwriting the server's shared "
            "login; the tool still falls back to that shared login when the "
            "profile has none. Omitted when no profile."
        ),
    ),
    SystemVarSpec(
        name="CODEX_HOME",
        resolve=_resolve_codex_home,
        description=(
            "Per-profile Codex CLI home, so `codex` in a Cremind shell signs "
            "this profile in instead of overwriting the server's shared login; "
            "the tool still falls back to that shared login when the profile "
            "has none. Omitted when no profile."
        ),
    ),
]


def build_system_env(profile: Optional[str]) -> Dict[str, str]:
    """Resolve every entry in :data:`SYSTEM_VARS` for ``profile``.

    Returns a dict suitable for merging into a subprocess env. Variables
    whose resolver returns ``None`` are omitted (matches the historical
    "skip CREMIND_TOKEN when missing" behavior).
    """
    out: Dict[str, str] = {}
    for spec in SYSTEM_VARS:
        value = spec.resolve(profile)
        if value is None:
            continue
        out[spec.name] = str(value)
    return out
