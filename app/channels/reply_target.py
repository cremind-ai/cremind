"""Where an adapter's reply is going: a person, or a room.

The reply machinery in :mod:`app.channels.base` — the in-flight forwarder table,
the pending-run counter, the per-target lock — was built when a reply only ever
went to one sender, so it keyed everything on ``sender_id`` and sent through
``_send_text``. A group reply needs the same machinery pointed somewhere else: a
platform CHAT id, delivered with ``send_to_chat``.

Rather than duplicate the machinery, this is the one value it is keyed and
dispatched on. A DM target's ``key`` is the bare sender id, unchanged, so
nothing about the 1:1 path moves.

What a target does NOT decide is "answer with steps": a room used to be pinned
to final-answer-only here, on the theory that a room full of people has not
asked to read the agent's tool calls. But the operator who ticked "Answer with
steps" on the channel has asked, and both the CLI doc and the settings dialog
promised it applied. The channel's ``response_mode`` now governs both
destinations — which does mean it is channel-wide, so steps cannot be had in
DMs but withheld from rooms. A per-room override would go on
``channel_groups.settings`` (JSON, so no migration) if that is ever wanted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Union

# Prefix for a group's registry key. Namespaced so a platform whose chat ids and
# user ids come from one space cannot have a room collide with a person.
_GROUP_KEY_PREFIX = "cg:"


@dataclass(frozen=True)
class ReplyTarget:
    """One destination for an agent's answer."""

    # "sender" (a 1:1 conversation) | "group" (a platform room)
    kind: str
    # What the platform call addresses: a sender id, or a chat id.
    address: str
    # What the adapter's forwarder tables are keyed on.
    key: str
    # The ``channel_groups`` row id, for a group target.
    group_id: Optional[str] = None

    @property
    def is_group(self) -> bool:
        return self.kind == "group"


def sender_target(sender_id: str) -> ReplyTarget:
    """A 1:1 reply. The key stays the bare sender id."""
    address = str(sender_id)
    return ReplyTarget(kind="sender", address=address, key=address)


def group_target(group: Dict[str, Any]) -> ReplyTarget:
    """A reply into a platform group, from its ``channel_groups`` row."""
    group_id = str(group.get("id") or "")
    return ReplyTarget(
        kind="group",
        address=str(group.get("platform_chat_id") or ""),
        key=group_key(group_id),
        group_id=group_id,
    )


def group_key(group_id: str) -> str:
    return f"{_GROUP_KEY_PREFIX}{group_id}"


def coerce_target(value: Union[ReplyTarget, str]) -> ReplyTarget:
    """Accept either a target or a bare sender id.

    The 1:1 call sites (and their tests) pass a sender id string and should not
    have to care that rooms exist.
    """
    if isinstance(value, ReplyTarget):
        return value
    return sender_target(str(value))
