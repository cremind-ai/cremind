"""Telling one platform message from another, and one account from its aliases.

Both problems come from the same place: a platform does not hand every receiver
the same view of a message. Two Cremind profiles with accounts in one group each
receive their own copy, and some platforms number those copies differently; one
account can arrive under more than one id depending on the device it posted from.
"""

from __future__ import annotations

import hashlib
from typing import List, Optional, Sequence

# ``(channel_type, chat_type)`` pairs whose message ids are numbered per ACCOUNT
# rather than per chat. A legacy Telegram group is the only known one: each
# receiving account numbers the same message from its own sequence, so the ids
# disagree and cannot identify it. Matched on the pair rather than on the word
# "group" alone because Slack calls a private channel a "group" too, and reading
# that as per-account would silently downgrade every Slack message to the weaker
# content-and-time fingerprint.
_PER_ACCOUNT_MESSAGE_IDS = {("telegram", "group")}


def platform_key(
    *,
    channel_type: str,
    chat_id: str,
    sender_id: str,
    platform_message_id: Optional[str],
    chat_type: Optional[str],
    text: str,
    platform_message_date: Optional[float] = None,
) -> str:
    """A stable id for "this platform message", however it reaches us.

    In a supergroup (and in every other room that numbers messages per chat) the
    message id is shared by every account that receives it, so it is the key.

    Where the ids are per account (``_PER_ACCOUNT_MESSAGE_IDS``) the key is a
    fingerprint of who said what and WHEN instead — the send time is the
    platform's own, identical on every account that received it. Without the
    time, "status?" would key the same forever and the second time anybody asked
    it the message would be silently swallowed as a duplicate.
    """
    per_account = (
        (channel_type or "").lower(), (chat_type or "").lower(),
    ) in _PER_ACCOUNT_MESSAGE_IDS
    if platform_message_id and not per_account:
        return f"{channel_type}:{chat_id}:{platform_message_id}"
    digest = hashlib.sha1(
        f"{sender_id}|{text}".encode("utf-8", "replace")
    ).hexdigest()[:16]
    return (
        f"{channel_type}:{chat_id}:{sender_id}:"
        f"{int(platform_message_date or 0)}:{digest}"
    )


def candidate_ids(
    sender_id: str, alt_ids: Optional[Sequence[str]] = None,
) -> List[str]:
    """Every id this one sender may be recognised by, primary first.

    One account is not one id: WhatsApp reports the same participant as
    ``<digits>@s.whatsapp.net`` on one device and ``<opaque>@lid`` on another.
    Matching on whichever form the transport happened to hand us would let our
    own post back in as somebody else's on one run, and turn an allowed member
    into a denied one on the next.
    """
    out: List[str] = []
    for value in [sender_id, *(alt_ids or ())]:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def ids_overlap(
    left: Sequence[str], right: Sequence[str],
) -> bool:
    """Whether two id sets name the same account, ignoring blanks and case.

    Case-insensitive because WhatsApp's ``@lid`` forms and Slack's user ids are
    handed back with inconsistent casing by different endpoints.
    """
    wanted = {str(value or "").strip().lower() for value in left}
    wanted.discard("")
    if not wanted:
        return False
    for value in right:
        text = str(value or "").strip().lower()
        if text and text in wanted:
            return True
    return False
