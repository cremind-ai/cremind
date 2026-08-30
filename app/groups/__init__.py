"""Group chats — one room, several profiles' agents, one human.

A group is the only resource in Cremind that several profiles share. The room
and its timeline are system-wide; each member's *participation* is an ordinary
hidden conversation owned by that profile (its "seat"), so an agent in a group
still runs with its own persona, tools, LLM and memory, and the only thing that
crosses the tenant boundary is the text of a posted message.

This is Cremind's OWN multi-agent room and has nothing to do with the messaging
platforms. An agent taking part in a real Telegram or Slack group is a different
feature living in :mod:`app.channels.groups`; the two never mix.

The flow, once:

1. Someone posts — a human from the web UI or the CLI; an agent by finishing a
   turn in its seat; an agent elsewhere via the ``send_group_message`` tool.
2. :func:`app.groups.fanout.post_message` writes the timeline row, tells every
   open client, and hands the message to every OTHER member's seat — folding it
   into a turn already running when there is one.
3. Each member's agent decides for itself whether it was addressed. If not, it
   answers with ``[silent]`` and nothing is posted.
4. When a seat turn ends, :func:`app.groups.hooks.on_shadow_turn_complete` posts
   whatever it said — which starts the cycle again, bounded by the hop counter.
   A turn that answered an interruption part-way through has already posted that
   much, from :func:`app.groups.hooks.on_shadow_turn_segment`.

Sub-modules: ``settings`` (the group's configuration blob), ``render`` (message
attribution and the silence sentinel), ``index`` (the in-memory membership
cache), ``bus`` (live updates), ``shadow`` (seats), ``fanout``, ``hooks``,
``routing`` (who starts a turn), ``origin`` (what the agent is told about the
room), ``boot`` (start-up and crash repair).
"""

from app.groups.bus import get_group_stream_bus
from app.groups.fanout import post_message
from app.groups.hooks import (
    on_shadow_turn_complete,
    on_shadow_turn_segment,
    publish_agent_status,
)
from app.groups.index import get_group_index, has_group_membership
from app.groups.shadow import (
    ensure_shadow_conversation,
    group_id_from_context,
    shadow_context_id,
)

__all__ = [
    "ensure_shadow_conversation",
    "get_group_index",
    "get_group_stream_bus",
    "group_id_from_context",
    "has_group_membership",
    "on_shadow_turn_complete",
    "on_shadow_turn_segment",
    "post_message",
    "publish_agent_status",
    "shadow_context_id",
]
