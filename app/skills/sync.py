"""Per-profile skill orchestration.

Every profile gets its own skills directory at
``<CREMIND_SYSTEM_DIR>/<profile>/skills/``. The built-in skills shipped in
``app/skills/builtin/`` are copied into each profile's directory on profile
creation and re-synced on every server boot so that deleted built-ins are
restored (user-authored skills are left untouched).

This module is the single entry point used by both ``server.py`` (boot) and
``app/api/profiles.py`` (profile create/delete) to:

- ensure the profile's skills directory exists and is populated with builtins,
- synchronise the on-disk skill set with the :class:`ToolRegistry`,
- start/stop a per-profile :class:`SkillsWatcher` for hot-reload,
- drop the per-profile tool-embedding collection on profile removal.
"""

from __future__ import annotations

import asyncio
import shutil
import threading
from pathlib import Path
from typing import Callable

from app.config.settings import BaseConfig
from app.skills.scanner import SkillInfo, scan_skills
from app.skills.tool import SkillTool
from app.skills.watcher import SkillsWatcher
from app.tools import ToolRegistry
from app.utils.logger import logger

BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent / "builtin"

_watchers: dict[str, SkillsWatcher] = {}
_watchers_lock = threading.Lock()


def profile_skills_dir(profile: str) -> Path:
    """Return ``<CREMIND_SYSTEM_DIR>/<profile>/skills``."""
    return Path(BaseConfig.CREMIND_SYSTEM_DIR) / profile / "skills"


def sync_builtin_skills_into_profile(profile: str) -> list[str]:
    """Copy every builtin skill directory into the profile's skills dir.

    Built-in-named skill directories are overwritten on every call so that
    accidental deletions and tampering are repaired. Directories in the
    profile's skills folder that do NOT correspond to a builtin are left
    untouched (those are user-authored skills).

    Returns the list of skill directory names that did not exist in the
    profile before this call (i.e. first-time installs for this profile).
    """
    target_root = profile_skills_dir(profile)
    target_root.mkdir(parents=True, exist_ok=True)

    if not BUILTIN_SKILLS_DIR.is_dir():
        logger.warning(f"Builtin skills dir missing: {BUILTIN_SKILLS_DIR}")
        return []

    newly_added: list[str] = []
    for src in BUILTIN_SKILLS_DIR.iterdir():
        if not src.is_dir():
            continue
        dst = target_root / src.name
        if not dst.exists():
            newly_added.append(src.name)
        shutil.copytree(src, dst, dirs_exist_ok=True)

    logger.info(f"Synced builtin skills into {target_root}")
    return newly_added


async def initialize_profile_skills(
    profile: str,
    registry: ToolRegistry,
    *,
    loop: asyncio.AbstractEventLoop | None = None,
) -> SkillsWatcher | None:
    """Bring a profile's skills up to date and attach a watcher.

    Steps:
    1. Ensure the profile's skills dir exists and is seeded with builtins.
    2. Scan the dir and sync the registry for this profile.
    3. Start a :class:`SkillsWatcher` scoped to the profile (idempotent).

    Returns the watcher handle (``None`` if the directory could not be set up).
    """
    newly_added_dirs = sync_builtin_skills_into_profile(profile)
    skills_dir = profile_skills_dir(profile)
    if not skills_dir.is_dir():
        logger.warning(f"Skills dir not ready for '{profile}': {skills_dir}")
        return None

    skills = scan_skills(skills_dir)
    _notify_first_add_long_running(profile, skills, newly_added_dirs)
    await registry.sync_skills(
        profile=profile,
        current=skills,
        skill_factory=lambda info: SkillTool(info),
    )
    logger.info(f"Synced {len(skills)} skill(s) for profile '{profile}'")

    return _start_watcher(profile, skills_dir, registry, loop=loop)


async def teardown_profile_skills(
    profile: str,
    registry: ToolRegistry,
    *,
    drop_embeddings: Callable[[str], None] | None = None,
) -> None:
    """Stop the watcher, drop registry rows, and optionally drop embeddings."""
    _stop_watcher(profile)

    await registry.sync_skills(
        profile=profile,
        current={},
        skill_factory=lambda info: SkillTool(info),
    )

    if drop_embeddings is not None:
        try:
            drop_embeddings(profile)
        except Exception:  # noqa: BLE001
            logger.exception(f"Failed to drop embeddings for profile '{profile}'")


def stop_all_watchers() -> None:
    """Stop every live profile watcher -- used during server shutdown."""
    with _watchers_lock:
        profiles = list(_watchers.keys())
    for profile in profiles:
        _stop_watcher(profile)


# ── internals ─────────────────────────────────────────────────────────────


def _start_watcher(
    profile: str,
    skills_dir: Path,
    registry: ToolRegistry,
    *,
    loop: asyncio.AbstractEventLoop | None,
) -> SkillsWatcher:
    with _watchers_lock:
        if profile in _watchers:
            return _watchers[profile]

    target_loop = loop or asyncio.get_event_loop()

    def _on_change(new_skills: dict[str, SkillInfo]) -> None:
        future = asyncio.run_coroutine_threadsafe(
            registry.sync_skills(
                profile=profile,
                current=new_skills,
                skill_factory=lambda info: SkillTool(info),
            ),
            target_loop,
        )
        try:
            future.result(timeout=30)
        except Exception:  # noqa: BLE001
            logger.exception(f"Skill re-sync failed for profile '{profile}'")
        # Notify the Settings page that the skills list / config schema may
        # have changed (a SKILL.md was added, edited, or removed on disk).
        try:
            from app.events.settings_state_bus import publish_settings_state_changed
            publish_settings_state_changed(profile)
        except Exception:  # noqa: BLE001
            logger.debug("settings-state publish failed", exc_info=True)

    watcher = SkillsWatcher(skills_dir, on_change=_on_change)
    watcher.start()

    with _watchers_lock:
        _watchers[profile] = watcher

    logger.info(f"Skills watcher started for profile '{profile}'")
    return watcher


def _stop_watcher(profile: str) -> None:
    with _watchers_lock:
        watcher = _watchers.pop(profile, None)
    if watcher is None:
        return
    try:
        watcher.stop()
    except Exception:  # noqa: BLE001
        logger.exception(f"Error stopping watcher for '{profile}'")


def _notify_first_add_long_running(
    profile: str,
    skills: dict[str, SkillInfo],
    newly_added_dirs: list[str],
) -> None:
    """Push a 'register required' notification for newly-added long-running skills.

    Fires once per skill, only the first time it lands in this profile's skills
    directory. Reboots that re-validate already-present skills do not re-fire.
    """
    if not newly_added_dirs:
        return
    try:
        from app.events.notifications_buffer import get_event_notifications
    except ImportError:
        return

    by_dir = {info.dir_path.name: info for info in skills.values()}
    buffer = get_event_notifications()

    for dir_name in newly_added_dirs:
        info = by_dir.get(dir_name)
        if info is None:
            continue
        lra = info.metadata.get("long_running_app") if isinstance(info.metadata, dict) else None
        if not isinstance(lra, dict):
            continue
        command = lra.get("command")
        if not isinstance(command, str) or not command.strip():
            continue
        try:
            buffer.push(
                profile=profile,
                conversation_id="",
                conversation_title=f"Set up {info.name}",
                message_preview=f"Click to register the {info.name} background process.",
                kind="skill_register_required",
                priority="high",
                extra={"skill_id": info.name, "skill_name": info.name},
            )
            logger.info(
                f"Notified profile '{profile}' to register long-running skill '{info.name}'"
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                f"Failed to emit skill_register_required notification for '{info.name}'"
            )
