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
import os
import shutil
import stat
import threading
import time
from pathlib import Path
from typing import Callable

from app.config.settings import BaseConfig
from app.events.settings_state_bus import publish_settings_state_changed
from app.skills.env_file import write_skill_env_file
from app.skills.scanner import SkillInfo, scan_skills
from app.skills.tool import SkillTool
from app.skills.watcher import SkillsWatcher
from app.tools import ToolRegistry
from app.utils.logger import logger

BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent / "builtin"


def _force_writable(path: str) -> None:
    """Clear the read-only bit so a stubborn file can be removed (Windows)."""
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass


def _robust_rmtree(path: Path, *, attempts: int = 5, delay: float = 0.3) -> None:
    """``shutil.rmtree`` with retries for transient Windows file locks.

    After a process tree is killed, Windows can take a moment to release the
    file handles it held (e.g. a skill's ``scripts/.listener.lock``). We retry a
    few times with a short backoff and clear read-only attributes on failure.
    The final attempt re-raises so genuine failures still surface.

    Runs synchronously; callers invoke it via ``asyncio.to_thread`` so the brief
    sleeps never block the event loop.
    """
    def _on_error(func, p, _exc):  # rmtree onexc handler (Python 3.12+)
        _force_writable(p)
        func(p)

    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            shutil.rmtree(path, onexc=_on_error)
            return
        except OSError as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(delay)
    if last_exc is not None:
        raise last_exc

_watchers: dict[str, SkillsWatcher] = {}
_watchers_lock = threading.Lock()


def profile_skills_dir(profile: str) -> Path:
    """Return ``<CREMIND_SYSTEM_DIR>/<profile>/skills``."""
    return Path(BaseConfig.CREMIND_SYSTEM_DIR) / profile / "skills"


def _refresh_event_watch(profile: str, registry: ToolRegistry) -> None:
    """Refresh the EventManager's skill index for *profile* (and arm its watch).

    The blanket per-profile event watch never needs re-arming on skill changes,
    but its path→tool_id index must track skills added/edited/removed so
    fan-out keeps resolving the canonical subscription key. ``watch_profile`` is
    idempotent and no-ops the schedule if the loop isn't running yet (boot).
    """
    try:
        from app.events import get_event_manager

        get_event_manager().watch_profile(profile, registry)
    except Exception:  # noqa: BLE001
        logger.exception(f"Failed to refresh event watch for profile '{profile}'")


def builtin_skill_dir_names() -> set[str]:
    """Return the set of directory names shipped under ``app/skills/builtin``."""
    if not BUILTIN_SKILLS_DIR.is_dir():
        return set()
    return {p.name for p in BUILTIN_SKILLS_DIR.iterdir() if p.is_dir()}


def is_builtin_skill_dir(dir_name: str) -> bool:
    """True if *dir_name* corresponds to a shipped built-in skill directory.

    Built-in skills are detected by directory name (the on-disk dir name equals
    the SKILL.md ``name`` for every built-in). This is what distinguishes a
    "Reset to Default" (built-in) from a "Delete" (external) skill.
    """
    if not dir_name:
        return False
    return (BUILTIN_SKILLS_DIR / dir_name).is_dir()


def list_builtin_skill_catalog() -> list[dict]:
    """Catalog rows for the shipped built-in skills — for the first-run wizard.

    During first-run setup no profile exists yet, so the registry holds no skill
    rows and ``GET /api/tools`` returns none (skills are profile-owned). The
    wizard still needs to show built-in skills so the admin can choose which to
    enable. This scans ``app/skills/builtin`` and returns rows shaped like the
    skill entries from :meth:`ToolRegistry.visible_for_profile`. ``tool_id`` is
    the profile-independent ``slugify(name)`` — the *base* of the
    ``<profile>__<slug>`` id the skill gets when it is seeded on Apply — so the
    wizard's chosen enable states map straight onto the seeded skills (applied
    in the first-setup pass in ``app/api/config.py``).
    """
    from app.tools.ids import slugify

    rows: list[dict] = []
    for info in scan_skills(BUILTIN_SKILLS_DIR).values():
        row = {
            "tool_id": slugify(info.name),
            "name": info.name,
            "display_name": info.name,
            "description": info.description,
            "tool_type": "skill",
            # Skills start OFF — the admin must opt each one in. This matches the
            # runtime fallback (skills default disabled absent a ``profile_tools``
            # row — see ToolRegistry._default_enabled), so a fresh profile, the
            # Setup Wizard, and a factory reset all start skills off.
            "enabled": False,
            "default_enabled": False,
            "configured": True,
            "config": {},
            "required_fields": {},
            "is_stub": False,
            "is_builtin": True,
            "toggle_locked": False,
        }
        lra = info.metadata.get("long_running_app") if isinstance(info.metadata, dict) else None
        if isinstance(lra, dict):
            row["long_running_app"] = lra
        rows.append(row)
    return rows


def _assert_inside_profile_skills(profile: str, dir_name: str) -> Path:
    """Resolve ``<profile skills>/<dir_name>`` and guard against traversal.

    Returns the resolved target path. Raises :class:`ValueError` if *dir_name*
    escapes the profile's skills directory (e.g. contains ``..`` or separators).
    """
    skills_root = profile_skills_dir(profile)
    target = (skills_root / dir_name)
    resolved = target.resolve()
    root_resolved = skills_root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError(f"Skill path '{dir_name}' escapes the skills directory")
    if resolved == root_resolved:
        raise ValueError("Refusing to operate on the skills directory itself")
    return target


def delete_profile_skill(profile: str, dir_name: str) -> bool:
    """Remove a skill directory from the profile's skills folder.

    Only touches ``<CREMIND_SYSTEM_DIR>/<profile>/skills/<dir_name>`` — never the
    shipped source under ``app/skills/builtin``. Returns True if a directory was
    removed, False if it did not exist.
    """
    target = _assert_inside_profile_skills(profile, dir_name)
    if not target.exists():
        return False
    _robust_rmtree(target)
    logger.info(f"Deleted skill '{dir_name}' from profile '{profile}'")
    return True


def reset_builtin_skill(profile: str, dir_name: str) -> None:
    """Restore a single built-in skill to its shipped default for *profile*.

    Deletes the profile's copy (if any) and re-copies the pristine directory
    from ``BUILTIN_SKILLS_DIR``. Raises :class:`ValueError` if *dir_name* is not
    a built-in.
    """
    src = BUILTIN_SKILLS_DIR / dir_name
    if not src.is_dir():
        raise ValueError(f"Skill '{dir_name}' is not a built-in")
    target = _assert_inside_profile_skills(profile, dir_name)
    if target.exists():
        _robust_rmtree(target)
    shutil.copytree(src, target, dirs_exist_ok=True)
    logger.info(f"Reset built-in skill '{dir_name}' for profile '{profile}'")


async def resync_profile_skills(profile: str, registry: ToolRegistry) -> dict[str, SkillInfo]:
    """Re-scan the profile's skills dir and reconcile the registry immediately.

    Used after a programmatic add/delete so the UI reflects the change at once
    rather than after the watcher's debounce window. Also pings the Settings
    SSE stream. Returns the freshly scanned skill set.
    """
    skills = scan_skills(profile_skills_dir(profile))
    await registry.sync_skills(
        profile=profile,
        current=skills,
        skill_factory=lambda info: SkillTool(info),
    )
    _materialize_skill_env_files(profile, registry)
    _refresh_event_watch(profile, registry)
    publish_settings_state_changed(profile)
    return skills


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

    # The copytree above restores shipped skill files (including any stale
    # .env); re-apply persisted variables so user overrides survive the boot.
    _materialize_skill_env_files(profile, registry)

    # Keep the EventManager's skill index in sync. At boot this runs before
    # EventManager.start(), so it only refreshes the index; the watch itself is
    # armed by server boot (block 7d). On runtime profile creation the loop is
    # already running, so this also arms the watch.
    _refresh_event_watch(profile, registry)

    return _start_watcher(profile, skills_dir, registry, loop=loop)


def _materialize_skill_env_files(profile: str, registry: ToolRegistry) -> None:
    """Re-write each profile skill's ``scripts/.env`` from its persisted vars.

    Skills get their config solely through ``scripts/.env`` (the agent has no
    per-skill env hook for the generic exec_shell tool), and that file is
    otherwise written only when the user saves variables. The boot-time
    copytree restores the shipped ``.env``, so without this the user's saved
    overrides would be lost on every restart. Writing from SQLite also yields an
    empty ``.env`` when there are no overrides — clearing any credentials that
    older builds shipped on disk.
    """
    # Use ``owned_skills`` (not ``tools_for_profile``) so a skill that's
    # disabled in Settings still gets its ``.env`` rewritten/cleared — env
    # materialization is credential hygiene over on-disk assets, independent of
    # whether the skill is currently exposed to the agent.
    for tool in registry.owned_skills(profile):
        declared = getattr(tool, "environment_variable_names", []) or []
        if not declared:
            continue
        try:
            variables = registry.config.get_variables(
                tool.tool_id, profile, include_secrets=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception(f"Failed to read variables for skill '{tool.tool_id}'")
            variables = {}
        write_skill_env_file(tool.info.dir_path / "scripts", declared, variables)


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
        # Track skills added/edited/removed on disk in the EventManager index so
        # fan-out keeps resolving the canonical skill key after a hot-reload.
        _refresh_event_watch(profile, registry)
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
