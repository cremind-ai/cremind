"""Skill source-directory lookup helpers.

Resolves a skill row's on-disk ``source`` directory from its ``tool_id``,
scoped to a profile.  Shared by the exec_shell tool (sticky working
directory on skill load) and the register_skill_event tool (resolving an
event subscription target).
"""

from typing import Optional

from app.utils.logger import logger


def lookup_skill_source(skill_id: str, profile: Optional[str]) -> Optional[str]:
    """Return the ``source`` directory of a skill row, or None on any miss.

    DB errors are logged and swallowed so callers never break on a DB issue
    — they fall back to whatever default makes sense in their flow.
    """
    if not skill_id or not profile:
        return None
    try:
        # Lazy import keeps this utility free of circular-import risk during
        # module initialisation (storage pulls in models which can pull in
        # other utils).
        from app.storage.tool_storage import get_tool_storage
        row = get_tool_storage().get_tool(skill_id)
    except Exception as exc:
        logger.error(f"lookup_skill_source({skill_id!r}, {profile!r}): {exc}")
        return None
    if not row:
        return None
    if row.get("tool_type") != "skill":
        return None
    if row.get("owner_profile") != profile:
        return None
    source = row.get("source")
    return source if source else None
