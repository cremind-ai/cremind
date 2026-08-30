"""Shared fakes for the channel-group pipeline.

Everything the pipeline touches is faked at the seam it imports from, not
mocked wholesale: a dict-backed storage that honours the real unique key, a
concrete adapter subclass, and stubs for the two delivery calls. What is NOT
faked is the decision logic itself — the point of these tests is which branch
runs, so the branches have to be real.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

import pytest

import app.storage as storage_mod
from app.channels.base import BaseChannelAdapter


class FakeChannelGroupStorage:
    """A dict-backed ``ChannelGroupStorage``, honouring the same unique key.

    Real enough for the pipeline: ``create_group`` is idempotent on
    ``(channel_id, platform_chat_id)`` the way the DB's unique constraint makes
    it, because a test that let two pending rows exist would pass while
    production raised.
    """

    def __init__(self) -> None:
        self.groups: Dict[str, Dict[str, Any]] = {}
        self.members: Dict[str, List[Dict[str, Any]]] = {}
        self.roster_calls: List[str] = []

    async def create_group(self, **kw: Any) -> Dict[str, Any]:
        existing = await self.get_group_by_chat(
            kw["channel_id"], kw["platform_chat_id"],
        )
        if existing is not None:
            return existing
        now = time.time() * 1000
        gid = str(uuid.uuid4())
        self.groups[gid] = {
            "id": gid,
            "channel_id": kw["channel_id"],
            "profile": kw["profile"],
            "platform_chat_id": str(kw["platform_chat_id"]),
            "chat_type": kw.get("chat_type"),
            "title": kw.get("title") or "",
            "status": kw.get("status", "pending"),
            "discovered_via": kw.get("discovered_via", "message"),
            "conversation_id": None,
            "settings": kw.get("settings") or {},
            "roster_refreshed_at": None,
            "last_message_at": None,
            "created_at": now,
            "updated_at": now,
        }
        self.members[gid] = []
        return await self.get_group(gid)

    async def get_group(self, group_id: str) -> Optional[Dict[str, Any]]:
        row = self.groups.get(group_id)
        if row is None:
            return None
        return {**row, "members": list(self.members.get(group_id, []))}

    async def get_group_by_chat(
        self, channel_id: str, platform_chat_id: str,
    ) -> Optional[Dict[str, Any]]:
        for gid, row in self.groups.items():
            if (
                row["channel_id"] == channel_id
                and row["platform_chat_id"] == str(platform_chat_id)
            ):
                return await self.get_group(gid)
        return None

    async def get_group_by_conversation(
        self, conversation_id: str,
    ) -> Optional[Dict[str, Any]]:
        for gid, row in self.groups.items():
            if row.get("conversation_id") == conversation_id:
                return await self.get_group(gid)
        return None

    async def list_groups(
        self, channel_id: str, *, status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        out = []
        for gid, row in self.groups.items():
            if row["channel_id"] != channel_id:
                continue
            if status and row["status"] != status:
                continue
            out.append(await self.get_group(gid))
        return out

    async def update_group(self, group_id: str, **fields: Any):
        row = self.groups.get(group_id)
        if row is None:
            return None
        for key, value in fields.items():
            if value is not None:
                row[key] = value
        return await self.get_group(group_id)

    async def delete_group(self, group_id: str) -> bool:
        self.members.pop(group_id, None)
        return self.groups.pop(group_id, None) is not None

    async def upsert_member(self, group_id: str, *, member_id: str, **kw: Any):
        rows = self.members.setdefault(group_id, [])
        for row in rows:
            if row["member_id"] == member_id:
                row.update({k: v for k, v in kw.items() if v})
                return row
        row = {
            "member_id": member_id,
            "alt_ids": list(kw.get("alt_ids") or []),
            "display_name": kw.get("display_name") or "",
            "username": kw.get("username") or "",
            "is_bot": bool(kw.get("is_bot")),
            "role": kw.get("role"),
            "source": kw.get("source", "seen"),
            "last_seen_at": time.time() * 1000,
            "message_count": 1,
        }
        rows.append(row)
        return row

    async def list_members(self, group_id: str) -> List[Dict[str, Any]]:
        return list(self.members.get(group_id, []))

    async def replace_roster(self, group_id: str, members) -> int:
        self.roster_calls.append(group_id)
        written = 0
        for entry in members or ():
            await self.upsert_member(
                group_id, member_id=entry["member_id"], source="roster",
                **{k: v for k, v in entry.items() if k != "member_id"},
            )
            written += 1
        return written


class FakeConversationStorage:
    """Just enough of ``ConversationStorage`` for the group pipeline."""

    def __init__(self) -> None:
        self.conversations: Dict[str, Dict[str, Any]] = {}
        self.messages: List[Dict[str, Any]] = []
        self.metadata_patches: List[tuple] = []
        self.channels_updated: List[Dict[str, Any]] = []

    async def update_channel(self, channel_id: str, **fields: Any):
        """Record a channel-row write (the group baseline lives there)."""
        self.channels_updated.append({"id": channel_id, **fields})
        return None

    async def get_conversation(self, conversation_id: str):
        return self.conversations.get(conversation_id)

    async def get_conversation_by_context(self, profile: str, context_id: str):
        for row in self.conversations.values():
            if row["profile"] == profile and row.get("context_id") == context_id:
                return row
        return None

    async def create_conversation(self, **kw: Any) -> Dict[str, Any]:
        cid = f"conv-{len(self.conversations) + 1}"
        row = {"id": cid, **kw}
        self.conversations[cid] = row
        return row

    async def update_conversation(self, conversation_id: str, **fields: Any):
        row = self.conversations.get(conversation_id)
        if row:
            row.update(fields)
        return row

    async def add_message(self, **kw: Any) -> Dict[str, Any]:
        row = {"id": f"m{len(self.messages) + 1}", **kw}
        self.messages.append(row)
        return row

    async def get_messages_after(self, conversation_id, after, **kw: Any):
        return [
            m for m in self.messages if m.get("conversation_id") == conversation_id
        ]

    async def update_message_metadata(self, message_id, patch):
        self.metadata_patches.append((message_id, patch))

    async def list_senders(self, channel_id):
        return []

    async def get_channel(self, channel_id):
        return None


class RoomAdapter(BaseChannelAdapter):
    """A concrete adapter that records what it would have sent."""

    supports_group_chats = True
    supports_group_roster = True
    reports_sender_is_bot = True

    def __init__(self, channel, storage, roster=None):
        super().__init__(channel, storage)
        self.sent: List[tuple] = []
        self.chat_sends: List[tuple] = []
        self.forwarded: List[tuple] = []
        self._roster = roster

    async def _run(self):  # abstract in base
        return None

    async def _send_text(self, sender_id, text):
        self.sent.append((sender_id, text))

    async def send_to_chat(self, chat_id, text):
        self.chat_sends.append((chat_id, text))

    async def fetch_group_roster(self, chat_id):
        return self._roster

    async def _forward_reply(self, conversation_id, target):
        """Return at once instead of tailing the bus.

        These tests are about which branch the pipeline takes, not about the
        reply path (``test_dispatch_and_forwarder.py`` owns that). A real
        forwarder waits on the stream bus forever, so leaving one running past
        the end of a test surfaces as a "loop is closed" traceback attributed to
        whichever test happens to run next.
        """
        self.forwarded.append((conversation_id, target))


def make_adapter(
    *,
    enabled: bool = True,
    self_id: str = "bot-1",
    storage: Optional[FakeConversationStorage] = None,
    roster=None,
) -> RoomAdapter:
    channel = {
        "id": "ch-1",
        "profile": "admin",
        "channel_type": "telegram",
        "mode": "bot",
        "config": {"group_chats_enabled": enabled},
        "state": {
            "self_identity": {
                "user_id": self_id, "username": "opsbot", "is_bot": True,
                "mention": "@opsbot",
            },
        },
    }
    return RoomAdapter(channel, storage or FakeConversationStorage(), roster=roster)


@pytest.fixture
def group_storage(monkeypatch) -> FakeChannelGroupStorage:
    """Swap the storage singleton the pipeline resolves lazily."""
    fake = FakeChannelGroupStorage()
    monkeypatch.setattr(
        storage_mod, "get_channel_group_storage", lambda *a, **k: fake,
    )
    return fake


@pytest.fixture
def notifications(monkeypatch) -> List[Dict[str, Any]]:
    """Capture what reaches the profile's notification bus."""
    pushed: List[Dict[str, Any]] = []

    class _Buffer:
        def push(self, **kw):
            pushed.append(kw)

    import app.events as events_mod

    monkeypatch.setattr(events_mod, "get_event_notifications", lambda: _Buffer())
    return pushed


@pytest.fixture
def delivery(monkeypatch):
    """Stub the two delivery calls; record what the pipeline asked for.

    ``parked`` lets a test say the message was folded into a running turn.
    """
    state: Dict[str, Any] = {"enqueued": [], "parked": None, "park_calls": []}

    async def _try_park(**kw):
        state["park_calls"].append(kw)
        return state["parked"]

    async def _enqueue(**kw):
        state["enqueued"].append(kw)

    import app.events.queue as queue_mod
    import app.events.user_message_delivery as umd

    monkeypatch.setattr(umd, "try_park_user_message", _try_park)
    monkeypatch.setattr(queue_mod, "enqueue_user_message", _enqueue)
    return state
