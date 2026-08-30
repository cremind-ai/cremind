"""Who is in which room, in memory.

Two questions get asked often enough to be worth caching: does this profile sit
in any group (the ``send_group_message`` tool gate, on every prompt build), and
which conversation is this profile's seat in that group (every fan-out). Both
would otherwise be a database round trip on a hot path.

Rebuilt from the database by :meth:`GroupIndex.refresh` at boot and whenever
membership changes, so it is a cache with a single writer, never a source of
truth.
"""

from __future__ import annotations

from typing import Dict, Optional, Set, Tuple

from app.utils.logger import logger


class GroupIndex:
    def __init__(self) -> None:
        # group_id -> {profile}
        self._members: Dict[str, Set[str]] = {}
        # profile -> {group_id}
        self._groups_by_profile: Dict[str, Set[str]] = {}
        # (group_id, profile) -> conversation_id
        self._shadow: Dict[Tuple[str, str], str] = {}
        self._loaded = False

    # ── load ──────────────────────────────────────────────────────────────

    async def refresh(self) -> None:
        """Rebuild from the database. Safe to call repeatedly."""
        from app.storage import get_group_chat_storage

        storage = get_group_chat_storage()
        try:
            memberships = await storage.list_memberships()
        except Exception:  # noqa: BLE001
            logger.exception("[group] index refresh failed to read the database")
            return

        members: Dict[str, Set[str]] = {}
        by_profile: Dict[str, Set[str]] = {}
        shadow: Dict[Tuple[str, str], str] = {}
        for row in memberships:
            gid, profile = row["group_id"], row["profile"]
            members.setdefault(gid, set()).add(profile)
            by_profile.setdefault(profile, set()).add(gid)
            if row.get("shadow_conversation_id"):
                shadow[(gid, profile)] = row["shadow_conversation_id"]
        self._members = members
        self._groups_by_profile = by_profile
        self._shadow = shadow
        self._loaded = True

        logger.info(
            f"[group] index loaded: {len(members)} group(s), "
            f"{len(by_profile)} member profile(s)"
        )

    # ── lookups ───────────────────────────────────────────────────────────

    def groups_for_profile(self, profile: str) -> Set[str]:
        return set(self._groups_by_profile.get(profile, ()))

    def members_of(self, group_id: str) -> Set[str]:
        return set(self._members.get(group_id, ()))

    def shadow_conversation(self, group_id: str, profile: str) -> Optional[str]:
        return self._shadow.get((group_id, profile))

    def note_shadow_conversation(
        self, group_id: str, profile: str, conversation_id: Optional[str],
    ) -> None:
        if conversation_id:
            self._shadow[(group_id, profile)] = conversation_id
        else:
            self._shadow.pop((group_id, profile), None)

    # ── state ─────────────────────────────────────────────────────────────

    @property
    def loaded(self) -> bool:
        return self._loaded

    def clear(self) -> None:
        """Tests only."""
        self.__init__()  # type: ignore[misc]


_instance: Optional[GroupIndex] = None


def get_group_index() -> GroupIndex:
    global _instance
    if _instance is None:
        _instance = GroupIndex()
    return _instance


def has_group_membership(profile: str) -> bool:
    """Whether ``profile`` sits in any group.

    Gates the ``send_group_message`` tool. Returns ``False`` before the index is
    loaded (CLI, tests, early boot) rather than raising — the same contract as
    :func:`app.channels.registry.has_any_channel`, and for the same reason: a
    tool-availability check must never be able to fail a run.
    """
    try:
        return bool(get_group_index().groups_for_profile(profile))
    except Exception:  # noqa: BLE001
        return False
