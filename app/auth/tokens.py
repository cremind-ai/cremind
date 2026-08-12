"""Session-token verification, rotation, and on-disk token files.

The JWT-facing half of the revocation feature; :mod:`app.auth.serial` owns the
counter this checks against.

:func:`verify_token` is the one function every decode site should call — the
HTTP middleware, the A2A call-context builder, the WebSocket handshake, and the
handlers that re-read their own bearer. Anything that calls ``jwt.decode``
directly is a hole through which a revoked token still works.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import jwt

from app.auth.serial import bump_serial, current_serial, serial_matches
from app.config.settings import BaseConfig
from app.utils.logger import logger


#: Duplicated from ``app.api.config`` rather than imported: this module is
#: reached from the CLI's ``--local`` path, which must not drag in the API
#: layer, and an unvalidated name here would be a path traversal into
#: ``<system_dir>/tokens/``.
_PROFILE_NAME_PATTERN = re.compile(r"^[a-z0-9_-]+$")

_TOKEN_SUFFIX = ".token"


def _validate_profile_name(profile: str) -> str:
    if not profile:
        raise ValueError("profile is required")
    if len(profile) > 64 or not _PROFILE_NAME_PATTERN.match(profile):
        raise ValueError(f"invalid profile name: {profile!r}")
    return profile


# ── verification ───────────────────────────────────────────────────────────


def verify_token(token: str, *, secret: str | None = None) -> dict[str, Any] | None:
    """Decode and fully validate a session JWT; ``None`` if it isn't usable.

    Checks the signature, ``exp``, **and** the profile's token serial, so a
    token that was revoked by ``cremind auth regenerate`` fails here even
    though it is still perfectly well-signed and unexpired.

    ``secret`` lets a caller pass a secret it already resolved (the auth
    middleware does, once per request). When it resolves to empty — setup mode,
    before any secret exists — this returns ``None`` so requests stay anonymous
    rather than erroring.
    """
    key = secret if secret is not None else BaseConfig.get_jwt_secret()
    if not key or not token:
        return None
    try:
        payload = jwt.decode(token, key, algorithms=["HS256"])
    except jwt.InvalidTokenError as e:
        logger.warning(f"[auth] JWT decode failed: {e}")
        return None
    if not serial_matches(payload):
        profile = payload.get("profile") or payload.get("sub") or ""
        logger.warning(
            f"[auth] rejected a revoked token for profile {profile!r} "
            f"(token serial {payload.get('tsr', 0)}, "
            f"current {current_serial(str(profile))})"
        )
        return None
    return payload


# ── token files ────────────────────────────────────────────────────────────


def tokens_dir() -> Path:
    return Path(BaseConfig.CREMIND_SYSTEM_DIR) / "tokens"


def token_file_path(profile: str) -> Path:
    return tokens_dir() / f"{_validate_profile_name(profile)}{_TOKEN_SUFFIX}"


def write_token_file(profile: str, token: str) -> Path:
    """Write ``<system_dir>/tokens/<profile>.token`` atomically, 0600 on POSIX.

    Atomic because this file is read live by other processes —
    ``app/config/system_vars.py`` reads it on every ``exec_shell`` spawn, and
    the CLI reads it on every command. A plain truncating write lets a reader
    observe an empty or half-written token during a rotation.

    The temp file deliberately does not end in ``.token``, so a concurrent
    directory scan can never surface a half-written profile.
    """
    _validate_profile_name(profile)
    if not token:
        raise ValueError("token is required")

    directory = tokens_dir()
    directory.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(directory, 0o700)
        except OSError:  # best-effort; a shared dir isn't fatal
            pass

    path = directory / f"{profile}{_TOKEN_SUFFIX}"
    tmp = directory / f".{profile}{_TOKEN_SUFFIX}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
        if os.name != "nt":
            # O_CREAT's mode only applies when the file is created, so an
            # existing temp file would keep its old mode. chmod before the
            # rename so the token is never briefly readable under its real name.
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


def delete_token_file(profile: str) -> bool:
    """Remove a profile's token file. ``True`` if one was there."""
    try:
        path = token_file_path(profile)
    except ValueError:
        return False
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.warning(f"[auth] could not delete token file for {profile!r}: {e}")
        return False


# ── rotation ───────────────────────────────────────────────────────────────


def rotate_profile_token(
    profile: str,
    *,
    hours: int | None = None,
    write_file: bool = True,
) -> dict[str, Any]:
    """Revoke every token issued to ``profile`` and mint a replacement.

    Synchronous (one UPDATE plus a file write) so the CLI's ``--local`` path can
    call it without an event loop. Returns
    ``{profile, token, expires_at, serial, token_file}``.
    """
    _validate_profile_name(profile)

    secret = BaseConfig.get_jwt_secret()
    if not secret:
        # Almost always the CLI --local path having skipped
        # ensure_local_config_storage(): jwt.encode would happily sign with ""
        # and hand back a token no server will ever accept.
        raise RuntimeError(
            "no JWT secret is configured for this installation — refusing to "
            "mint a token that could never authenticate."
        )

    # Bump *then* mint. The other order hands out a token that the bump
    # immediately invalidates.
    serial = bump_serial(profile)
    token, expires_at = BaseConfig.mint_token(
        profile, secret=secret, hours=hours, serial=serial
    )

    token_file = ""
    if write_file:
        token_file = str(write_token_file(profile, token))

    return {
        "profile": profile,
        "token": token,
        "expires_at": expires_at,
        "serial": serial,
        "token_file": token_file,
    }


def ensure_local_config_storage() -> None:
    """Make ``BaseConfig.get_jwt_secret()`` work in a process that never booted.

    ``set_dynamic_config_storage`` is normally called from the server's
    ``boot_storage_and_post_storage``. Without it ``get_dynamic`` short-circuits
    to the Dynaconf fallback, ``general.jwt_secret`` is empty on a normal
    install, and every token minted here would be signed with ``""``.

    Idempotent — a no-op inside the running server.
    """
    import app.config.settings as settings_module

    if settings_module._dynamic_config_storage is None:
        from app.storage import get_dynamic_config_storage

        settings_module.set_dynamic_config_storage(get_dynamic_config_storage())
