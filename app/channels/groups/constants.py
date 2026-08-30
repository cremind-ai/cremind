"""Names and defaults shared across channel group chats."""

from __future__ import annotations

# ``conversations.context_id`` prefix for a channel group's conversation. The
# group row id is appended. The conversation itself is an ordinary ``kind="chat"``
# row bound to the channel — it shows in the sidebar like a DM sender's thread —
# so this prefix is the only thing that identifies it as a group afterwards.
CONTEXT_PREFIX = "channel_group:"

# ``channel_groups.status``. A group is invisible to the agent until a human
# approves it: a bot can be added to any group by anyone, and answering in one
# nobody vetted is how an agent ends up talking to strangers.
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_BLOCKED = "blocked"
STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_BLOCKED)

# How the group was first seen. Only Telegram (bot and userbot), Slack and the
# two sidecar platforms report a join; elsewhere a group is discovered by its
# first message. ``picked`` is the operator choosing a group the account was
# already in (there was no join to notice), and ``sweep`` is the reconciler
# noticing one that was joined while Cremind was not running.
DISCOVERED_VIA_JOIN = "join"
DISCOVERED_VIA_MESSAGE = "message"
DISCOVERED_VIA_PICKED = "picked"
DISCOVERED_VIA_SWEEP = "sweep"
DISCOVERED_VIAS = (
    DISCOVERED_VIA_JOIN,
    DISCOVERED_VIA_MESSAGE,
    DISCOVERED_VIA_PICKED,
    DISCOVERED_VIA_SWEEP,
)

# ``channel_group_members.source``. A roster row came from the platform's member
# list; a seen row came from somebody posting. Roster wins when both know a
# member, because it carries the display name and the admin flag.
MEMBER_SOURCE_ROSTER = "roster"
MEMBER_SOURCE_SEEN = "seen"

# Whether the agent may answer a message that does not mention it. ``mention_only``
# skips the relevance judge entirely — cheaper, and the right setting for a quiet
# assistant in a busy room.
RESPOND_MENTION_OR_RELEVANT = "mention_or_relevant"
RESPOND_MENTION_ONLY = "mention_only"
RESPOND_MODES = (RESPOND_MENTION_OR_RELEVANT, RESPOND_MENTION_ONLY)

# ``member_policy.mode``. ``everyone`` answers anyone not in ``deny``;
# ``selected`` answers only the ids in ``allow``.
POLICY_EVERYONE = "everyone"
POLICY_SELECTED = "selected"
POLICY_MODES = (POLICY_EVERYONE, POLICY_SELECTED)

# Agent posts per minute, per group, before the agent stops starting turns.
# A human exchange rarely draws more than one reply every few seconds; twenty a
# minute is a rate only two agents answering each other reach.
DEFAULT_MAX_AGENT_POSTS_PER_MINUTE = 20

# Consecutive bot-authored messages since the last human before the agent goes
# quiet. A legitimate hand-off (ask, answer, thanks, follow-up, answer) is five,
# so eight leaves room for one more exchange while a two-bot ping-pong hits it
# within seconds. Only reachable on platforms that flag bot authorship.
DEFAULT_MAX_CONSECUTIVE_BOT_MESSAGES = 8

# How long two copies of one platform message count as the same message.
INBOUND_DEDUPE_WINDOW_SECONDS = 90.0

# Entries in the per-adapter dedupe ring.
DEDUPE_RING_SIZE = 400

# How often one member's "seen" row is rewritten. Without a throttle a chatty
# group is one UPDATE per message per member for a column nobody reads in real
# time.
SEEN_WRITE_INTERVAL_SECONDS = 60.0

# How old a roster snapshot may get before the next inbound message refreshes it.
ROSTER_MAX_AGE_SECONDS = 24 * 60 * 60.0

# The reconcile sweep: how long after an adapter goes live before it compares
# the platform's group list against what we track, and how often afterwards.
# Its job is to catch joins that happened while Cremind was down, so it is a
# safety net rather than a hot path.
SWEEP_INITIAL_DELAY_SECONDS = 5.0
SWEEP_INTERVAL_SECONDS = 15 * 60.0

# ``channels.state`` key holding the groups an account was ALREADY in when the
# feature was first switched on. They get no notification — nobody just added
# the account to them — and the operator picks the ones to enable instead.
STATE_GROUP_BASELINE = "group_baseline"

# Timeline rows handed to the relevance judge.
JUDGE_HISTORY_ROWS = 12

# Notification kinds pushed to the profile's notification bus.
NOTIFY_GROUP_REQUEST = "channel_group_request"
NOTIFY_GROUP_BRAKE = "channel_group_brake"

# ``usage_records.source_kind`` for a relevance judgement. The column is
# ``String(16)`` — anything longer is silently truncated on SQLite and rejected
# on Postgres.
JUDGE_SOURCE_KIND = "group_judge"

# ``metadata.channel_group.decision`` values, recorded on every stored message so
# the log and the UI can explain why the agent did or did not answer.
DECISION_MENTIONED = "mentioned"
DECISION_NOT_MENTIONED = "not_mentioned"
DECISION_JUDGE_RELEVANT = "judge:relevant"
DECISION_JUDGE_IRRELEVANT = "judge:irrelevant"
DECISION_JUDGE_ERROR = "judge:error"
DECISION_BRAKE_RATE = "brake:rate"
DECISION_BRAKE_BOTS = "brake:bots"
