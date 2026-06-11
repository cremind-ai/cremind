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
import re
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from app.config.settings import BaseConfig, get_user_working_directory
from app.utils.logger import logger

# Backend OAuth callback path for the Google skills (served by
# app/api/oauth_callback.py). The browser-facing redirect they advertise is
# APP_URL + this. (Atlassian uses a single FIXED URL — see settings.py — not an
# APP_URL-derived one, because its 3LO Web client allows only one registered,
# exact-match callback per app.)
_GOOGLE_CALLBACK_PATH = "/api/oauth/google/callback"

# APP_URL origins a Google "Desktop" client will accept as a redirect: loopback
# only (localhost / 127.0.0.1, any port). A real hostname (Ingress/domain/LAN
# server) is rejected by Google, so the Google redirect is left unset there and
# the skill falls back to the manual ``complete-link`` paste.
_LOOPBACK_APP_URL_RE = re.compile(r"^https?://(127\.0\.0\.1|localhost)(:[0-9]+)?(/|$)")


def _app_url_base() -> Optional[str]:
    """APP_URL trimmed of any trailing slash, or ``None`` when it is the
    unusable ``http://0.0.0.0:<port>`` listen-all default (not a browser origin)."""
    url = (BaseConfig.APP_URL or "").strip().rstrip("/")
    if not url or "://0.0.0.0" in url:
        return None
    return url


def _resolve_google_redirect_uri(_profile: Optional[str]) -> Optional[str]:
    """Browser-facing Google OAuth redirect for the gmail/gcalendar skills:
    ``<APP_URL>/api/oauth/google/callback``. Emitted only for a loopback APP_URL
    (Desktop client constraint); otherwise omitted so the skill uses the manual
    ``complete-link`` paste."""
    base = _app_url_base()
    if not base or not _LOOPBACK_APP_URL_RE.match(base):
        return None
    return base + _GOOGLE_CALLBACK_PATH


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


@dataclass(frozen=True)
class SystemVarSpec:
    name: str
    resolve: Callable[[Optional[str]], Optional[str]]
    description: str = ""


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
        name="CREMIND_TOKEN",
        resolve=_load_cremind_token,
        description="Per-profile Cremind token; omitted when missing.",
    ),
    SystemVarSpec(
        name="CREMIND_OAUTH_REDIRECT_URI",
        resolve=_resolve_google_redirect_uri,
        description=(
            "Browser-facing Google OAuth redirect for the gmail/gcalendar skills "
            "(<APP_URL>/api/oauth/google/callback). The skill advertises it and the "
            "backend captures the consent redirect; omitted for non-loopback APP_URL "
            "(Desktop clients only accept loopback) so the skill uses manual paste."
        ),
    ),
    SystemVarSpec(
        name="CREMIND_ATLASSIAN_REDIRECT_URI",
        resolve=lambda _profile: BaseConfig.CREMIND_ATLASSIAN_REDIRECT_URI or None,
        description=(
            "Atlassian 3LO redirect for the jira/confluence skills — a SINGLE FIXED "
            "URL (not APP_URL-derived) registered exact-match in the Atlassian "
            "developer console (one callback per app). Set via the same env var or "
            "the chart's cremind.atlassianRedirectUri; defaults to the single "
            "public origin (http://localhost:1515/api/oauth/atlassian/callback)."
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
