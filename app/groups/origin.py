"""What a seat conversation tells the agent about the room it sits in.

Feeds the ``message_origin`` slot the reasoning agent already renders into its
system prompt (see :func:`app.agent.reasoning_agent._format_message_origin_block`).
Deriving it from the conversation row rather than from the turn keeps it constant
for the whole seat, which is what keeps the cached prompt prefix byte-stable —
the roster only changes when the membership does.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.groups import settings as group_settings
from app.groups.shadow import group_id_from_context
from app.utils.logger import logger


async def resolve_group_origin(
    conversation_storage: Any, conv: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Describe the group behind a seat conversation, or ``None``.

    ``None`` on any failure: the prompt loses a section, the run continues.
    """
    try:
        group_id = group_id_from_context(conv.get("context_id"))
        if not group_id:
            return None
        self_profile = conv.get("profile") or ""

        from app.storage import get_group_chat_storage
        from app.utils.agent_name import read_agent_name

        storage = get_group_chat_storage()
        group = await storage.get_group(group_id)
        if group is None:
            return None
        settings = group_settings.normalize_settings(group.get("settings"))

        members: List[Dict[str, Any]] = [
            {"profile": profile, "agent_name": read_agent_name(profile)}
            for profile in group.get("members") or []
        ]

        return {
            "source": "group_chat",
            "group_id": group_id,
            "group_name": group.get("name") or group_id,
            "self_profile": self_profile,
            "self_name": read_agent_name(self_profile),
            "members": members,
            "max_agent_hops": group_settings.max_agent_hops(settings),
        }
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[group] failed to resolve the group origin for {conv.get('id')}"
        )
        return None
