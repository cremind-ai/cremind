"""Where Cremind's manual lives on disk — the one place that spells it out.

Two trees, both under ``<CREMIND_SYSTEM_DIR>/storage/cremind_documents``:

- ``shared/``           — the bundled manual, mirrored from
  ``app/cremind_documents/bundled`` on every boot. Nothing else writes here.
- ``profiles/<uuid>/``  — the manual pages a profile authored itself, keyed by
  the profile's immutable uuid (``profiles.id``), never by its name.

Why not the old ``<SYS>/documents`` and ``<SYS>/<profile>/documents``: a
profile's tree starts at ``<SYS>/<name>``, so a profile *named* ``documents``
owned the shared mirror's directory (and the boot mirror deleted "extras" out
of it), and a profile named ``storage`` had its manual at
``<SYS>/storage/documents`` — which is exactly where Documentation search keeps
every profile's index. A uuid cannot collide with ``shared`` or with anything
another subsystem owns, and a profile deleted and re-created under the same
name starts with an empty directory instead of inheriting the old one's pages.

:mod:`app.documents.relocate` moves installs from the old layout at boot.

Kept free of server imports at module level so the offline CLI (backup
restore, reset) can use the path helpers without the storage stack.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from app.utils.logger import logger

# Relative to the system dir. Spelled out as tuples so the backup rules (pure,
# no app imports) and the file-access rules can mirror them without importing.
ROOT_PARTS: tuple[str, ...] = ("storage", "cremind_documents")
SHARED_PARTS: tuple[str, ...] = ROOT_PARTS + ("shared",)
PROFILES_PARTS: tuple[str, ...] = ROOT_PARTS + ("profiles",)

# Scope names the manual keeps for itself. A scope is otherwise a profile NAME
# (it is what every vector point's payload carries and what searches filter
# on), so a profile named after one of these would share it: a profile named
# ``shared`` would have its pages merged into the bundled manual, its watcher
# would replace the shared one, and deleting it would stop that watcher and
# prune every shared point for every profile until the next restart; ``cli``
# is the retired CLI-reference scope, pruned wholesale at every boot. So
# neither may be a profile name (profile creation, the setup wizard and a
# blueprint import all refuse them), and the delete path never touches the
# shared scope.
SHARED_SCOPE = "shared"
LEGACY_CLI_SCOPE = "cli"
# ``<SYS>/workspaces`` holds every profile's working directory
# (app/config/working_dirs.py), so a profile of that name would have its own
# system folder — persona, skills, exports — inside the other profiles' space.
WORKSPACES_NAME = "workspaces"
RESERVED_PROFILE_NAMES: frozenset[str] = frozenset({SHARED_SCOPE, LEGACY_CLI_SCOPE, WORKSPACES_NAME})


def reserved_profile_name_error(name: object) -> Optional[str]:
    """The refusal for a profile name Cremind reserves, or None."""
    clean = str(name or "").strip()
    if clean == WORKSPACES_NAME:
        return (
            f"The profile name '{clean}' is reserved (Cremind keeps every profile's working "
            "directory in a folder of that name); choose another name."
        )
    if clean in RESERVED_PROFILE_NAMES:
        return (
            f"The profile name '{clean}' is reserved (Cremind's manual uses it "
            "internally); choose another name."
        )
    return None


def system_dir() -> Path:
    from app.config.settings import BaseConfig

    return Path(BaseConfig.CREMIND_SYSTEM_DIR)


def root(base: Path | str | None = None) -> Path:
    return Path(base if base is not None else system_dir()).joinpath(*ROOT_PARTS)


def shared_dir(base: Path | str | None = None) -> Path:
    """The bundled manual's mirror. Seeded (and pruned to the bundle) at boot."""
    return Path(base if base is not None else system_dir()).joinpath(*SHARED_PARTS)


def profiles_root(base: Path | str | None = None) -> Path:
    return Path(base if base is not None else system_dir()).joinpath(*PROFILES_PARTS)


def valid_uid(uid: object) -> bool:
    """A uuid this module is willing to turn into a directory name.

    The directory is removed wholesale when its profile is deleted, so
    anything that could climb out of ``profiles/`` (a separator, ``..``) or
    name the root itself is refused — that would be a bug upstream, never a
    real profile id."""
    s = str(uid or "")
    if not s or s in (".", "..") or len(s) > 128:
        return False
    return not any(sep in s for sep in ("/", "\\", os.sep, "\0"))


def profile_dir_for_uid(uid: str, base: Path | str | None = None) -> Path:
    """``<SYS>/storage/cremind_documents/profiles/<uid>``. Raises ValueError for
    a uid that is not a plain directory name (see :func:`valid_uid`)."""
    if not valid_uid(uid):
        raise ValueError(f"invalid profile uid for a manual directory: {uid!r}")
    return profiles_root(base) / str(uid)


def resolve_profile_uid(profile: str) -> Optional[str]:
    """The profile's uuid from the ``profiles`` table, or None.

    None covers "no such profile" and "storage is not up" alike: every caller
    treats both as "this profile has no manual directory right now" and skips
    it, which is the only safe reading — guessing a name-keyed directory is
    exactly the collision this layout exists to remove. Never cached here:
    callers that ask often (the sync service) keep their own cache and drop it
    when a profile is deleted.
    """
    if not profile:
        return None
    try:
        from app.storage.documents_storage import get_documents_storage

        uid = get_documents_storage().profile_uid(profile)
    except Exception as exc:  # noqa: BLE001 — no storage yet (setup mode, offline CLI)
        logger.debug(f"[cremind_documents] could not resolve the uuid of profile {profile!r}: {exc}")
        return None
    return uid if uid and valid_uid(uid) else None


def profile_dir(profile: str, base: Path | str | None = None) -> Optional[Path]:
    """The profile's authored-manual directory, or None when its uuid cannot be
    resolved (see :func:`resolve_profile_uid`)."""
    uid = resolve_profile_uid(profile)
    return profile_dir_for_uid(uid, base) if uid else None


__all__ = [
    "LEGACY_CLI_SCOPE",
    "PROFILES_PARTS",
    "RESERVED_PROFILE_NAMES",
    "ROOT_PARTS",
    "SHARED_PARTS",
    "SHARED_SCOPE",
    "profile_dir",
    "profile_dir_for_uid",
    "profiles_root",
    "reserved_profile_name_error",
    "resolve_profile_uid",
    "root",
    "shared_dir",
    "system_dir",
    "valid_uid",
]
