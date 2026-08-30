"""What a group conversation tells the agent about the room it is speaking in.

Feeds the ``message_origin`` slot the reasoning agent already renders into its
system prompt (see
:func:`app.agent.reasoning_agent._format_message_origin_block`). Derived from the
conversation row rather than from the turn, so it changes only when the group
does — not once per message.

That last part is a hard requirement, not a nicety. The origin block is rendered
into the SYSTEM message, ahead of the whole conversation, and OpenAI's automatic
prompt cache matches on an exact prefix: one reordered name near the top costs
the entire cached prefix, on every turn, forever. The roster is therefore
**selected** by recency (the ≤40 members worth showing) and then **rendered in a
stable order** — an earlier version sorted the rendered list by ``last_seen_at``,
which permuted whenever a different person spoke and measurably zeroed the cache.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.channels.groups.constants import CONTEXT_PREFIX
from app.utils.logger import logger

# How much of a big group's roster reaches the prompt. A 200-person Slack channel
# would otherwise spend the prompt on a list nobody reads; the members who have
# spoken recently are the ones the agent needs to recognise.
_ROSTER_LIMIT = 40


def channel_group_context_id(group_id: str) -> str:
    return f"{CONTEXT_PREFIX}{group_id}"


def is_channel_group_context(context_id: Optional[str]) -> bool:
    return bool(context_id) and str(context_id).startswith(CONTEXT_PREFIX)


def group_id_from_context(context_id: Optional[str]) -> Optional[str]:
    if not is_channel_group_context(context_id):
        return None
    return str(context_id)[len(CONTEXT_PREFIX):] or None


def visible_members(
    group: Dict[str, Any], identity: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """The group's other members, capped and in a STABLE order.

    Two steps, and the order of them is the point:

    1. *select* the ≤40 members worth showing by recency, so a 200-person room
       spends the prompt on the people actually talking rather than on a list
       nobody reads;
    2. *render* that selection sorted by ``member_id``, which never changes.

    Doing the sort the other way round — rendering by ``last_seen_at`` — makes
    the system prompt permute every time a different person speaks, and with it
    the whole cached prefix. See the module docstring.

    Our own account is excluded: the agent is not a member of its own roster,
    and a model told it is tends to answer as though addressed.
    """
    identity = identity or {}
    self_ids = {str(identity.get("user_id") or "")} | {
        str(v) for v in (identity.get("alt_ids") or ())
    }
    self_ids.discard("")

    ranked = sorted(
        group.get("members") or [],
        key=lambda m: float(m.get("last_seen_at") or 0),
        reverse=True,
    )
    chosen: List[Dict[str, Any]] = []
    for member in ranked:
        if str(member.get("member_id") or "") in self_ids:
            continue
        name = (
            member.get("display_name")
            or member.get("username")
            or member.get("member_id")
        )
        if not name:
            continue
        chosen.append({
            "name": str(name),
            "handle": str(member.get("username") or ""),
            "is_bot": bool(member.get("is_bot")),
            "role": member.get("role"),
            "member_id": str(member.get("member_id") or ""),
        })
        if len(chosen) >= _ROSTER_LIMIT:
            break
    return sorted(chosen, key=lambda m: m["member_id"])


async def resolve_channel_group_origin(
    conversation_storage: Any, conv: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Describe the platform group behind a conversation, or ``None``.

    ``None`` on any failure: the prompt loses a section, the run continues.
    """
    try:
        group_id = group_id_from_context(conv.get("context_id"))
        if not group_id:
            return None

        from app.storage import get_channel_group_storage
        from app.utils.agent_name import read_agent_name

        group = await get_channel_group_storage().get_group(group_id)
        if group is None:
            return None

        profile = group.get("profile") or conv.get("profile") or ""
        channel = None
        try:
            channel = await conversation_storage.get_channel(group.get("channel_id"))
        except Exception:  # noqa: BLE001
            logger.debug(
                "[channel_group] could not read the channel for the origin",
                exc_info=True,
            )
        channel_type = (channel or {}).get("channel_type") or ""
        identity = ((channel or {}).get("state") or {}).get("self_identity") or {}
        self_handle = identity.get("mention") or (
            f"@{identity['username']}" if identity.get("username") else ""
        )

        return {
            "source": "channel_group",
            "channel_id": group.get("channel_id"),
            "channel_type": channel_type,
            "channel_name": _channel_display_name(channel_type),
            "group_id": group_id,
            "group_title": group.get("title") or group.get("platform_chat_id") or "",
            "platform_chat_id": group.get("platform_chat_id"),
            "self_profile": profile,
            "self_name": read_agent_name(profile),
            "self_handle": self_handle,
            # The name the OTHER members see above this agent's messages. On
            # Zalo and WhatsApp it is the only handle they can address it by.
            "self_account_name": str(identity.get("display_name") or ""),
            "members": visible_members(group, identity),
            "member_count": len(group.get("members") or []),
        }
    except Exception:  # noqa: BLE001
        logger.exception(
            f"[channel_group] failed to resolve the origin for {conv.get('id')}"
        )
        return None


def _channel_display_name(channel_type: str) -> str:
    """The platform's own name ("Telegram"), or the raw type as a fallback."""
    if not channel_type:
        return "a messaging platform"
    try:
        from app.config import load_channel_catalog

        catalog = load_channel_catalog(channel_type) or {}
        return (catalog.get("channel") or {}).get("display_name") or channel_type
    except Exception:  # noqa: BLE001
        return channel_type
