"""Per-adapter volatile state for the groups one channel is in.

Everything here is a cache or a counter, held on the adapter instance and thrown
away when it restarts. That is deliberate: the durable facts are the
``channel_groups`` rows, and anything that survived a restart in memory would be
a second source of truth for them.

Owned by one adapter, so it needs no locking of its own beyond the per-group
:class:`asyncio.Lock` it hands out — the event loop is single-threaded and there
is no ``await`` inside any of these methods.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from typing import Any, Deque, Dict, Optional

from app.channels.groups.constants import (
    DEDUPE_RING_SIZE,
    INBOUND_DEDUPE_WINDOW_SECONDS,
    ROSTER_MAX_AGE_SECONDS,
    SEEN_WRITE_INTERVAL_SECONDS,
)

# How long an agent post counts towards the per-minute cap.
_RATE_WINDOW_SECONDS = 60.0


class ChannelGroupRuntime:
    """The in-memory half of one adapter's group handling."""

    def __init__(self) -> None:
        self._locks: Dict[str, asyncio.Lock] = {}
        # dedupe key -> first-seen timestamp
        self._seen: "OrderedDict[str, float]" = OrderedDict()
        # group_id -> timestamps of our own recent posts
        self._posts: Dict[str, Deque[float]] = {}
        # group_id -> consecutive bot-authored messages since the last human
        self._bot_streak: Dict[str, int] = {}
        # group_ids whose brake we have already announced this episode
        self._brake_notified: set[str] = set()
        # (group_id, member_id) -> when we last wrote a "seen" row
        self._seen_writes: Dict[str, float] = {}

    # ── ordering ──────────────────────────────────────────────────────────

    def lock(self, platform_chat_id: str) -> asyncio.Lock:
        """The per-group inbound lock (created on first use).

        Held across the whole decision pipeline, so two messages from one room
        are parked or enqueued in arrival order even though the relevance judge
        in the middle takes a provider round trip. Keyed by the platform chat id
        rather than the group row id because the lock is taken before the row is
        looked up.

        Safe without synchronization for the same reason as
        ``BaseChannelAdapter._inbound_lock``: the loop is single-threaded and
        there is no ``await`` between the lookup and the assignment.
        """
        lock = self._locks.get(platform_chat_id)
        if lock is None:
            lock = self._locks[platform_chat_id] = asyncio.Lock()
        return lock

    # ── dedupe ────────────────────────────────────────────────────────────

    def seen_recently(self, key: str, *, now: Optional[float] = None) -> bool:
        """Whether this exact platform message already came through.

        Records the key as a side effect, so the first caller gets ``False`` and
        every repeat inside the window gets ``True``.
        """
        if not key:
            return False
        stamp = time.time() if now is None else now
        cutoff = stamp - INBOUND_DEDUPE_WINDOW_SECONDS
        for stale in [k for k, ts in self._seen.items() if ts < cutoff]:
            self._seen.pop(stale, None)
        if key in self._seen:
            return True
        self._seen[key] = stamp
        while len(self._seen) > DEDUPE_RING_SIZE:
            self._seen.popitem(last=False)
        return False

    # ── brakes ────────────────────────────────────────────────────────────

    def note_agent_post(self, group_id: str, *, now: Optional[float] = None) -> None:
        """Record that we just posted into a group.

        Also counts towards the bot streak: our own posts are bot-authored, and
        a room where the only recent speakers are assistants is exactly what the
        streak brake is for.
        """
        stamp = time.time() if now is None else now
        posts = self._posts.setdefault(group_id, deque())
        posts.append(stamp)
        self._trim_posts(posts, stamp)
        self._bot_streak[group_id] = self._bot_streak.get(group_id, 0) + 1

    def agent_posts_last_minute(
        self, group_id: str, *, now: Optional[float] = None,
    ) -> int:
        stamp = time.time() if now is None else now
        posts = self._posts.get(group_id)
        if not posts:
            return 0
        self._trim_posts(posts, stamp)
        return len(posts)

    def note_inbound_author(self, group_id: str, is_bot: bool) -> None:
        """Count an incoming message towards (or out of) the bot streak.

        A human message resets it to zero — that is the whole design: the brake
        stops assistants talking to each other, and a person joining in means
        the conversation is real again.
        """
        if is_bot:
            self._bot_streak[group_id] = self._bot_streak.get(group_id, 0) + 1
        else:
            self._bot_streak.pop(group_id, None)
            self._brake_notified.discard(group_id)

    def bot_streak(self, group_id: str) -> int:
        return self._bot_streak.get(group_id, 0)

    def note_brake_engaged(self, group_id: str) -> bool:
        """Whether this is the FIRST time a brake engaged this episode.

        ``True`` exactly once per episode, so the operator gets one notification
        rather than one per suppressed message. Reset when a human speaks.
        """
        if group_id in self._brake_notified:
            return False
        self._brake_notified.add(group_id)
        return True

    @staticmethod
    def _trim_posts(posts: Deque[float], now: float) -> None:
        cutoff = now - _RATE_WINDOW_SECONDS
        while posts and posts[0] < cutoff:
            posts.popleft()

    # ── write throttles ───────────────────────────────────────────────────

    def should_write_seen(
        self, group_id: str, member_id: str, *, now: Optional[float] = None,
    ) -> bool:
        """Whether to refresh a member's "last seen" row.

        Throttled because the alternative is one UPDATE per message per member
        for a column nobody reads in real time, and a busy group is a lot of
        messages.
        """
        stamp = time.time() if now is None else now
        key = f"{group_id}\x00{member_id}"
        last = self._seen_writes.get(key)
        if last is not None and stamp - last < SEEN_WRITE_INTERVAL_SECONDS:
            return False
        self._seen_writes[key] = stamp
        return True

    @staticmethod
    def roster_stale(group: Dict[str, Any], *, now: Optional[float] = None) -> bool:
        """Whether this group's member list is old enough to re-fetch.

        Static because it reads only the row. A group that has never had a
        roster is stale by definition — a platform that cannot list members
        simply returns nothing and the timestamp is written anyway, so this does
        not loop.
        """
        refreshed = group.get("roster_refreshed_at")
        if not refreshed:
            return True
        stamp = (time.time() if now is None else now) * 1000
        return (stamp - float(refreshed)) > ROSTER_MAX_AGE_SECONDS * 1000

    # ── teardown ──────────────────────────────────────────────────────────

    def forget_group(self, group_id: str, platform_chat_id: str = "") -> None:
        """Drop everything remembered about one group (blocked, or forgotten)."""
        self._posts.pop(group_id, None)
        self._bot_streak.pop(group_id, None)
        self._brake_notified.discard(group_id)
        prefix = f"{group_id}\x00"
        for key in [k for k in self._seen_writes if k.startswith(prefix)]:
            self._seen_writes.pop(key, None)
        if platform_chat_id:
            self._locks.pop(platform_chat_id, None)
