"""Who is in a platform group.

Two sources, because no platform gives both halves:

**The roster** — the platform's own member list, where the adapter can read one
at all. Slack, WhatsApp, the Telegram userbot and Discord can; a Telegram *bot*
sees only administrators, and the Zalo bot sees nothing. This is the only source
that carries display names and admin flags, and the only one that knows about
somebody who has never spoken.

**Seen** — anybody who posts. All a bot-only platform can ever know, and it is
enough for the member policy, which is the feature that actually needs the list.

Refreshing is best-effort throughout: a roster the platform declined to hand over
costs the settings page a list, never a message.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Sequence

from app.channels.groups.constants import MEMBER_SOURCE_SEEN
from app.utils.logger import logger


async def refresh_roster(adapter: Any, group: Dict[str, Any]) -> Optional[int]:
    """Re-read a group's member list from the platform. Returns how many, or ``None``.

    ``None`` means this platform cannot list members — which is a fact about the
    platform, not a failure, and the API reports it as such. The timestamp is
    written either way so a platform that cannot answer is not asked again on
    every single message.
    """
    group_id = group.get("id")
    chat_id = str(group.get("platform_chat_id") or "")
    if not group_id or not chat_id:
        return None

    members: Optional[Sequence[Dict[str, Any]]] = None
    try:
        members = await adapter.fetch_group_roster(chat_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            f"[channel_group] could not read the member list for "
            f"{adapter.channel_type}:{chat_id}",
            exc_info=True,
        )
        members = None

    from app.storage import get_channel_group_storage

    storage = get_channel_group_storage()
    written: Optional[int] = None
    if members is not None:
        try:
            written = await storage.replace_roster(group_id, members)
        except Exception:  # noqa: BLE001
            logger.exception(
                f"[channel_group] could not store the roster for {group_id}"
            )
            written = None

    try:
        await storage.update_group(group_id, roster_refreshed_at=time.time() * 1000)
    except Exception:  # noqa: BLE001
        logger.debug(
            "[channel_group] could not stamp the roster refresh", exc_info=True,
        )
    return written


async def note_seen_member(
    group_id: str,
    *,
    member_id: str,
    alt_ids: Sequence[str] = (),
    display_name: Optional[str] = None,
    username: Optional[str] = None,
    is_bot: bool = False,
) -> None:
    """Record that somebody posted. Never raises — this is bookkeeping."""
    try:
        from app.storage import get_channel_group_storage

        await get_channel_group_storage().upsert_member(
            group_id,
            member_id=member_id,
            alt_ids=alt_ids,
            display_name=display_name,
            username=username,
            is_bot=is_bot,
            source=MEMBER_SOURCE_SEEN,
            count_message=True,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            f"[channel_group] could not record {member_id} as seen", exc_info=True,
        )
