"""A seat that answers an interruption while its turn is still running.

Before this, everything a seat said reached the room at turn end. That is fine
for an answer — the answer IS the end of the turn — but not for a reply to
somebody who interrupted: "have you finished installing?" asked during a ten
minute install was answered ten minutes later, after the install, which is
exactly the wait the interruption was trying to skip.

So each flow break posts the segment it just closed. Two properties carry the
whole design and both are tested here:

* **the open tail stays put** — the agent is still writing it, and posting a
  half-finished sentence would be worse than waiting;
* **nothing is said twice** — the same segments are re-derived at turn end (and
  again by the boot sweep after a crash), so the live rows must be recognisable.
  They are, by the run they were posted under: the turn's message does not exist
  yet, so the run is their provisional owner until it does.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402
from sqlalchemy import text  # noqa: E402

import app.groups.fanout as fanout  # noqa: E402
import app.storage as storage_module  # noqa: E402
import app.utils.agent_name as agent_name_module  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.groups.hooks import (  # noqa: E402
    live_source_key, on_shadow_turn_complete, on_shadow_turn_segment,
)
from app.groups.shadow import shadow_context_id  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402
from app.storage.group_chat_storage import GroupChatStorage  # noqa: E402

_TABLES = (
    "profiles", "channels", "conversations", "messages",
    "group_chats", "group_chat_members", "group_chat_messages",
)

RUN = "group:conv-1:246b2086-02e7-49ac-9f40-78672ef8e0ca"


def _env(tmp_path: Path, monkeypatch):
    """Like ``test_hooks._env``, but ``post_message`` really writes.

    The dedupe this file is about is enforced by the UNIQUE constraint on
    ``(source_message_id, segment)`` and read back through ``find_by_source``,
    so a fake that only records its arguments would let every one of these
    tests pass while the room filled up with duplicates.
    """
    provider = SqliteDatabaseProvider(str(tmp_path / "live.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO profiles (id, name, created_at, updated_at) "
                "VALUES ('pid-dog', 'dog', 0, 0)"
            )
        )

    conversation_storage = ConversationStorage(provider)
    conversation_storage._initialized = True
    storage = GroupChatStorage(provider)
    monkeypatch.setattr(
        storage_module, "get_group_chat_storage", lambda *a, **k: storage,
    )
    monkeypatch.setattr(
        agent_name_module, "read_agent_name", lambda profile: "Rex",
    )

    async def real_post(**kwargs):
        # The fan-out itself (routing, hops, mirroring) is tested elsewhere;
        # what matters here is that the timeline row is real.
        return await storage.add_message(
            group_id=kwargs["group_id"],
            sender_kind=kwargs["sender_kind"],
            sender_name=kwargs["sender_name"],
            content=kwargs["content"],
            sender_profile=kwargs.get("sender_profile"),
            source_conversation_id=kwargs.get("source_conversation_id"),
            source_message_id=kwargs.get("source_message_id"),
            segment=kwargs.get("segment", 0),
        )

    monkeypatch.setattr(fanout, "post_message", real_post)
    return SimpleNamespace(storage=storage, conversation_storage=conversation_storage)


async def _seat(env):
    group = await env.storage.create_group(name="Ops", members=["dog", "cat"])
    conv = await env.conversation_storage.create_conversation(
        profile="dog",
        context_id=shadow_context_id(group["id"], "dog"),
        title="Group: Ops",
        kind="group_chat",
    )
    await env.storage.set_shadow_conversation(group["id"], "dog", conv["id"])
    return group, conv


async def _timeline(env, group_id) -> list[str]:
    return [
        r["content"]
        for r in await env.storage.list_messages(group_id)
        if r["sender_kind"] == "agent"
    ]


async def _agent_row(env, conv, content, metadata=None):
    row = await env.conversation_storage.add_message(
        conv["id"], "agent", content=content, metadata=metadata,
    )
    return row["id"]


async def _marker(env, message_id: str) -> dict:
    row = await env.conversation_storage.get_message(message_id)
    return (row.get("metadata") or {}).get("group") or {}


# ── posting while the turn runs ─────────────────────────────────────────────


def test_a_closed_segment_reaches_the_room_before_the_turn_ends(
    tmp_path, monkeypatch,
) -> None:
    """The whole point: the reply is in the room while the work carries on."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, conv = await _seat(env)
        raw = "Not yet — still installing Node."

        posted = await on_shadow_turn_segment(
            conversation_id=conv["id"], profile="dog", run_id=RUN,
            raw_text=raw,
            mid_turn_breaks=[{"content_offset": len(raw)}],
            context_id=conv["context_id"],
        )

        assert len(posted) == 1
        assert await _timeline(env, group["id"]) == [
            "Not yet — still installing Node.",
        ]

    asyncio.run(run())


def test_the_tail_the_agent_is_still_writing_is_not_posted(
    tmp_path, monkeypatch,
) -> None:
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, conv = await _seat(env)

        await on_shadow_turn_segment(
            conversation_id=conv["id"], profile="dog", run_id=RUN,
            raw_text="Not yet.\n\nInstalled — and here is what it found so f",
            mid_turn_breaks=[{"content_offset": 8}],
            context_id=conv["context_id"],
        )

        assert await _timeline(env, group["id"]) == ["Not yet."]

    asyncio.run(run())


def test_nothing_is_posted_before_the_first_break(tmp_path, monkeypatch) -> None:
    """No break means nothing is closed — not even a whole-looking sentence."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, conv = await _seat(env)

        posted = await on_shadow_turn_segment(
            conversation_id=conv["id"], profile="dog", run_id=RUN,
            raw_text="Working on it.", mid_turn_breaks=[],
            context_id=conv["context_id"],
        )

        assert posted == []
        assert await _timeline(env, group["id"]) == []

    asyncio.run(run())


def test_a_silent_interim_posts_nothing(tmp_path, monkeypatch) -> None:
    """It decided the interruption was not for it. Same sentinel, same rules."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, conv = await _seat(env)

        posted = await on_shadow_turn_segment(
            conversation_id=conv["id"], profile="dog", run_id=RUN,
            raw_text="[silent]", mid_turn_breaks=[{"content_offset": 8}],
            context_id=conv["context_id"],
        )

        assert posted == []
        assert await _timeline(env, group["id"]) == []

    asyncio.run(run())


def test_a_second_break_posts_only_what_is_new(tmp_path, monkeypatch) -> None:
    """Two interruptions, two replies — and the first is not repeated."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, conv = await _seat(env)
        first = "Not yet."
        both = "Not yet.\n\nStill going, about half way."

        await on_shadow_turn_segment(
            conversation_id=conv["id"], profile="dog", run_id=RUN,
            raw_text=first, mid_turn_breaks=[{"content_offset": len(first)}],
            context_id=conv["context_id"],
        )
        posted = await on_shadow_turn_segment(
            conversation_id=conv["id"], profile="dog", run_id=RUN,
            raw_text=both,
            mid_turn_breaks=[
                {"content_offset": len(first)}, {"content_offset": len(both)},
            ],
            context_id=conv["context_id"],
        )

        assert len(posted) == 1
        assert await _timeline(env, group["id"]) == [
            "Not yet.", "Still going, about half way.",
        ]

    asyncio.run(run())


def test_a_repeated_break_says_nothing_twice(tmp_path, monkeypatch) -> None:
    """Idempotent on its own, not just against the completion hook."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, conv = await _seat(env)
        raw = "Not yet."
        for _ in range(3):
            await on_shadow_turn_segment(
                conversation_id=conv["id"], profile="dog", run_id=RUN,
                raw_text=raw, mid_turn_breaks=[{"content_offset": len(raw)}],
                context_id=conv["context_id"],
            )

        assert await _timeline(env, group["id"]) == ["Not yet."]

    asyncio.run(run())


def test_a_non_member_posts_nothing(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, conv = await _seat(env)
        await env.storage.set_members(group["id"], ["cat"])

        posted = await on_shadow_turn_segment(
            conversation_id=conv["id"], profile="dog", run_id=RUN,
            raw_text="hello", mid_turn_breaks=[{"content_offset": 5}],
            context_id=conv["context_id"],
        )

        assert posted == []
        assert await _timeline(env, group["id"]) == []

    asyncio.run(run())


def test_a_broken_room_never_fails_the_turn(tmp_path, monkeypatch) -> None:
    """The room is a side effect of the turn, never a reason to fail it."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        _, conv = await _seat(env)

        async def boom(**_kw):
            raise RuntimeError("timeline is down")

        monkeypatch.setattr(fanout, "post_message", boom)

        assert await on_shadow_turn_segment(
            conversation_id=conv["id"], profile="dog", run_id=RUN,
            raw_text="hi", mid_turn_breaks=[{"content_offset": 2}],
            context_id=conv["context_id"],
        ) == []

    asyncio.run(run())


# ── and then the turn ends ──────────────────────────────────────────────────


def test_the_end_of_the_turn_posts_only_the_rest(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, conv = await _seat(env)
        raw = "Not yet.\n\nDone — Node 24 and OpenClaw are installed."
        await on_shadow_turn_segment(
            conversation_id=conv["id"], profile="dog", run_id=RUN,
            raw_text=raw, mid_turn_breaks=[{"content_offset": 8}],
            context_id=conv["context_id"],
        )
        msg_id = await _agent_row(env, conv, raw)

        posted = await on_shadow_turn_complete(
            conversation_storage=env.conversation_storage,
            conversation_id=conv["id"], profile="dog", run_id=RUN,
            assistant_msg_id=msg_id, raw_text=raw,
            final_text="Done — Node 24 and OpenClaw are installed.",
            mid_turn_breaks=[{"content_offset": 8}],
            context_id=conv["context_id"],
        )

        assert await _timeline(env, group["id"]) == [
            "Not yet.", "Done — Node 24 and OpenClaw are installed.",
        ]
        # Both count as this turn's: the interim one was re-pointed at the
        # message that has now persisted, so the stamp tells the truth.
        assert len(posted) == 2
        assert (await _marker(env, msg_id))["kind"] == "posted"

    asyncio.run(run())


def test_the_interim_post_is_handed_to_the_message_that_persisted(
    tmp_path, monkeypatch,
) -> None:
    """Which is what makes the trace behind an interim post findable — the
    Thinking-Process panel looks a room row up by its source message."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, conv = await _seat(env)
        raw = "Not yet."
        await on_shadow_turn_segment(
            conversation_id=conv["id"], profile="dog", run_id=RUN,
            raw_text=raw, mid_turn_breaks=[{"content_offset": 8}],
            context_id=conv["context_id"],
        )
        rows = await env.storage.list_messages(group["id"])
        assert rows[0]["source_message_id"] == live_source_key(RUN)

        msg_id = await _agent_row(env, conv, raw)
        await on_shadow_turn_complete(
            conversation_storage=env.conversation_storage,
            conversation_id=conv["id"], profile="dog", run_id=RUN,
            assistant_msg_id=msg_id, raw_text=raw, final_text="[silent]",
            mid_turn_breaks=[{"content_offset": 8}],
            context_id=conv["context_id"],
        )

        rows = await env.storage.list_messages(group["id"])
        assert [r["source_message_id"] for r in rows] == [msg_id]
        assert [r["content"] for r in rows] == ["Not yet."]

    asyncio.run(run())


def test_a_turn_that_only_spoke_mid_turn_is_not_stamped_silent(
    tmp_path, monkeypatch,
) -> None:
    """The interim reply lives in this turn's trace. Stamping the row silent
    drops it from the seat's own history, so the agent forgets it answered."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        _, conv = await _seat(env)
        raw = "Not yet.\n\n[silent]"
        await on_shadow_turn_segment(
            conversation_id=conv["id"], profile="dog", run_id=RUN,
            raw_text=raw, mid_turn_breaks=[{"content_offset": 8}],
            context_id=conv["context_id"],
        )
        msg_id = await _agent_row(env, conv, raw)

        await on_shadow_turn_complete(
            conversation_storage=env.conversation_storage,
            conversation_id=conv["id"], profile="dog", run_id=RUN,
            assistant_msg_id=msg_id, raw_text=raw, final_text="[silent]",
            mid_turn_breaks=[{"content_offset": 8}],
            context_id=conv["context_id"],
        )

        assert (await _marker(env, msg_id))["kind"] == "posted"

    asyncio.run(run())


def test_a_turn_that_died_after_speaking_keeps_what_it_said(
    tmp_path, monkeypatch,
) -> None:
    """Cancelled is not silent. The reply is already in the room and the row it
    belongs to has to own it."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, conv = await _seat(env)
        raw = "Not yet."
        await on_shadow_turn_segment(
            conversation_id=conv["id"], profile="dog", run_id=RUN,
            raw_text=raw, mid_turn_breaks=[{"content_offset": 8}],
            context_id=conv["context_id"],
        )
        msg_id = await _agent_row(env, conv, raw)

        posted = await on_shadow_turn_complete(
            conversation_storage=env.conversation_storage,
            conversation_id=conv["id"], profile="dog", run_id=RUN,
            assistant_msg_id=msg_id, raw_text=raw, final_text=raw,
            mid_turn_breaks=[{"content_offset": 8}],
            cancelled=True, context_id=conv["context_id"],
        )

        assert len(posted) == 1
        marker = await _marker(env, msg_id)
        assert marker["kind"] == "posted"
        assert marker["reason"] == "cancelled"
        rows = await env.storage.list_messages(group["id"])
        assert [r["source_message_id"] for r in rows] == [msg_id]

    asyncio.run(run())


def test_running_the_completion_hook_twice_says_nothing_twice(
    tmp_path, monkeypatch,
) -> None:
    """The boot sweep's case, and the one the dedupe key exists for."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, conv = await _seat(env)
        raw = "Not yet.\n\nDone."
        await on_shadow_turn_segment(
            conversation_id=conv["id"], profile="dog", run_id=RUN,
            raw_text=raw, mid_turn_breaks=[{"content_offset": 8}],
            context_id=conv["context_id"],
        )
        msg_id = await _agent_row(env, conv, raw)

        for _ in range(2):
            await on_shadow_turn_complete(
                conversation_storage=env.conversation_storage,
                conversation_id=conv["id"], profile="dog", run_id=RUN,
                assistant_msg_id=msg_id, raw_text=raw, final_text="Done.",
                mid_turn_breaks=[{"content_offset": 8}],
                context_id=conv["context_id"],
            )

        assert await _timeline(env, group["id"]) == ["Not yet.", "Done."]
        # Everything it had to say was already said — which is not silence, and
        # calling it silence would drop this turn from the seat's history.
        marker = await _marker(env, msg_id)
        assert marker["kind"] == "skipped"
        assert marker["reason"] == "duplicate"

    asyncio.run(run())
