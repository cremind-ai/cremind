"""Names and defaults shared across the group-chat feature."""

from __future__ import annotations

# A group message lands in every member's seat, so every member answers — which
# would be unbearable if the only way to say "not my turn" were to say something.
# This is how an agent declines: its ENTIRE final answer is this word, and the
# turn produces no post at all. Recognised tolerantly (see ``render.is_silent``)
# because models dress sentinels up in bold and full stops.
SILENT_SENTINEL = "[silent]"

# What ``stream_runner`` writes when a turn produced no text of its own. Neither
# is something to say out loud in a room.
EMPTY_FINALS = frozenset({"(no response)", "(stopped)"})

# ``conversations.kind`` for a member's seat in a group.
GROUP_CONVERSATION_KIND = "group_chat"

# Where a seat's ``context_id`` starts. The profile is appended, because the
# context id keys per-conversation tool state (working directory, loaded skills,
# current query) — sharing one across members would leak that state between
# tenants.
CONTEXT_PREFIX = "group:"

SENDER_KINDS = ("user", "agent", "system")

# How far a chain of agents answering each other may run before it stops
# starting new turns. Deliberately generous: a real hand-off (Dog asks Chicken,
# Chicken answers, Dog thanks and asks Cat, Cat answers, Dog summarises) is
# already five hops, and the cap is only the backstop against two agents being
# endlessly polite at each other. A human message resets it to zero.
DEFAULT_MAX_AGENT_HOPS = 6

# Second backstop, for the case the hop counter cannot see: many agents each
# answering different messages at once. Beyond this many agent posts per minute
# the group keeps delivering but stops starting turns until it quiets down.
DEFAULT_MAX_AGENT_POSTS_PER_MINUTE = 30

DEFAULT_WEB_SENDER_NAME = "Operator"

# The settings key for the routing classifier (``app.groups.routing``), which
# names the members worth starting a turn for instead of waking all of them.
# Spelled here as well as there, rather than imported from there, so the
# settings blob and the API can be read without dragging the LLM stack in for
# one string; ``tests/groups/test_settings.py`` pins the two spellings together.
ROUTING_SETTING_KEY = "smart_routing"

# On by default. Routing can only narrow who STARTS a turn — the chosen agents
# still answer or go ``[silent]`` on their own judgement, and every uncertain
# path in the classifier resolves to "everyone" — so the worst it does is behave
# exactly like a room without it, one cheap call later. (An agent's own reply
# may also be routed to nobody, which is a confident narrowing rather than an
# uncertain one: see the ``routing`` module docstring.)
DEFAULT_ROUTING_ENABLED = True
