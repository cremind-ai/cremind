"""The turn-end hook: an agent's answer becoming its post.

An agent in a group does not "send a message" — it answers, and the answer is
what the room sees. So this runs from the one place every turn passes through on
its way out, and three things have to be right:

* silence is judged per SEGMENT, because a turn interrupted mid-flight speaks
  twice ("Got it, checking" then ``[silent]``) and testing the concatenation
  would post the sentinel out loud;
* ``metadata.group`` is stamped whatever happens, because an UNSTAMPED agent row
  is precisely what the boot sweep treats as "this turn never got to post" —
  leaving one off would make the sweep re-post a deliberate silence;
* a failed turn is announced rather than silently absent, or an agent that
  errors mid-room just stops existing from everyone else's point of view.
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
from app.groups.bus import get_group_stream_bus  # noqa: E402
from app.groups.hooks import (  # noqa: E402
    on_shadow_turn_complete, publish_agent_status,
)
from app.groups.shadow import shadow_context_id  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402
from app.storage.group_chat_storage import GroupChatStorage  # noqa: E402

_TABLES = (
    "profiles", "channels", "conversations", "messages",
    "group_chats", "group_chat_members", "group_chat_messages",
)

_NAMES = {"dog": "Rex", "cat": "Mia", "duck": "Quack"}


def _env(tmp_path: Path, monkeypatch):
    provider = SqliteDatabaseProvider(str(tmp_path / "hooks.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        for profile in _NAMES:
            c.execute(
                text(
                    "INSERT INTO profiles (id, name, created_at, updated_at) "
                    "VALUES (:id, :name, 0, 0)"
                ),
                {"id": f"pid-{profile}", "name": profile},
            )

    conversation_storage = ConversationStorage(provider)
    conversation_storage._initialized = True
    storage = GroupChatStorage(provider)
    monkeypatch.setattr(
        storage_module, "get_group_chat_storage", lambda *a, **k: storage,
    )
    monkeypatch.setattr(
        agent_name_module, "read_agent_name", lambda profile: _NAMES.get(profile, profile),
    )

    posts: list[dict] = []
    counter = {"n": 0}

    async def fake_post(**kwargs):
        posts.append(kwargs)
        counter["n"] += 1
        return {"id": f"post-{counter['n']}", **kwargs}

    monkeypatch.setattr(fanout, "post_message", fake_post)

    return SimpleNamespace(
        storage=storage,
        conversation_storage=conversation_storage,
        posts=posts,
    )


async def _seat(env, profiles=("dog", "cat")) -> tuple[dict, dict, str]:
    """A group, a seat conversation for ``profiles[0]``, and an agent row id."""
    group = await env.storage.create_group(name="Ops", members=list(profiles))
    profile = profiles[0]
    conv = await env.conversation_storage.create_conversation(
        profile=profile,
        context_id=shadow_context_id(group["id"], profile),
        title="Group: Ops",
        kind="group_chat",
    )
    await env.storage.set_shadow_conversation(group["id"], profile, conv["id"])
    row = await env.conversation_storage.add_message(
        conv["id"], "agent", content="It is 14:20.",
    )
    return group, conv, row["id"]


async def _marker(env, message_id: str) -> dict:
    row = await env.conversation_storage.get_message(message_id)
    return (row.get("metadata") or {}).get("group") or {}


# ── the ordinary case ───────────────────────────────────────────────────────


def test_a_finished_turn_becomes_a_post(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, conv, msg_id = await _seat(env)

        posted = await on_shadow_turn_complete(
            conversation_storage=env.conversation_storage,
            conversation_id=conv["id"],
            profile="dog",
            run_id="run-1",
            assistant_msg_id=msg_id,
            raw_text="It is 14:20.",
            final_text="It is 14:20.",
            context_id=conv["context_id"],
        )

        assert posted == ["post-1"]
        call = env.posts[0]
        assert call["group_id"] == group["id"]
        assert call["sender_kind"] == "agent"
        assert call["sender_profile"] == "dog"
        assert call["sender_name"] == "Rex"
        assert call["content"] == "It is 14:20."
        assert call["segment"] == 0
        assert call["source_conversation_id"] == conv["id"]
        assert call["source_message_id"] == msg_id
        # The seat's own turn: fan-out must not put it back into its own history.
        assert call["originated_from_shadow_turn"] is True

        marker = await _marker(env, msg_id)
        assert marker["kind"] == "posted"
        assert marker["posted_message_ids"] == ["post-1"]
        assert marker["run_id"] == "run-1"
        assert marker["group_id"] == group["id"]

    asyncio.run(run())


def test_the_group_is_found_from_the_conversation_when_no_context_is_passed(
    tmp_path, monkeypatch,
) -> None:
    """The boot sweep calls this without the runner's in-memory context id."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, conv, msg_id = await _seat(env)

        await on_shadow_turn_complete(
            conversation_storage=env.conversation_storage,
            conversation_id=conv["id"], profile="dog", run_id="boot-sweep",
            assistant_msg_id=msg_id, raw_text="hello", final_text="hello",
        )

        assert env.posts[0]["group_id"] == group["id"]

    asyncio.run(run())


def test_an_ordinary_conversation_is_left_alone(tmp_path, monkeypatch) -> None:
    """The hook runs on every turn in the process, so the non-group path has to
    be free and silent."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        conv = await env.conversation_storage.create_conversation(
            profile="dog", title="Chat",
        )
        row = await env.conversation_storage.add_message(conv["id"], "agent", content="hi")

        assert await on_shadow_turn_complete(
            conversation_storage=env.conversation_storage,
            conversation_id=conv["id"], profile="dog", run_id="run-1",
            assistant_msg_id=row["id"], raw_text="hi", final_text="hi",
        ) == []

        assert env.posts == []
        assert await _marker(env, row["id"]) == {}

    asyncio.run(run())


# ── a turn that spoke more than once ────────────────────────────────────────


def test_an_interrupted_turn_posts_the_ack_and_then_the_answer(
    tmp_path, monkeypatch,
) -> None:
    """Two bubbles the room watched arrive, in the order they arrived, each its
    own post so replies can attach to the right one."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        _, conv, msg_id = await _seat(env)
        raw = "Got it, checking.\n\nIt is 14:20."

        posted = await on_shadow_turn_complete(
            conversation_storage=env.conversation_storage,
            conversation_id=conv["id"], profile="dog", run_id="run-1",
            assistant_msg_id=msg_id, raw_text=raw, final_text="It is 14:20.",
            mid_turn_breaks=[{"content_offset": 17}],
            context_id=conv["context_id"],
        )

        assert posted == ["post-1", "post-2"]
        assert [p["content"] for p in env.posts] == [
            "Got it, checking.", "It is 14:20.",
        ]
        assert [p["segment"] for p in env.posts] == [0, 1]
        # Each bubble is its own post, so a reply can attach to the right one.
        assert [p["source_message_id"] for p in env.posts] == [msg_id, msg_id]

    asyncio.run(run())


def test_an_ack_followed_by_silence_posts_only_the_ack(tmp_path, monkeypatch) -> None:
    """The exact case per-segment judging exists for: as one string this has no
    sentinel at the end of it and the whole thing goes out."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        _, conv, msg_id = await _seat(env)

        posted = await on_shadow_turn_complete(
            conversation_storage=env.conversation_storage,
            conversation_id=conv["id"], profile="dog", run_id="run-1",
            assistant_msg_id=msg_id, raw_text="ack\n\n[silent]", final_text="[silent]",
            mid_turn_breaks=[{"content_offset": 5}],
            context_id=conv["context_id"],
        )

        assert posted == ["post-1"]
        assert [p["content"] for p in env.posts] == ["ack"]
        assert (await _marker(env, msg_id))["kind"] == "posted"

    asyncio.run(run())


# ── the silences ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("final", ["[silent]", "**[silent]**", "  [SILENT] ", "[silent]."])
def test_a_dressed_up_sentinel_posts_nothing_and_says_so(
    tmp_path, monkeypatch, final,
) -> None:
    """Most turns in a busy room should produce nothing at all — every member
    answers every message, and this is how they decline."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        _, conv, msg_id = await _seat(env)

        posted = await on_shadow_turn_complete(
            conversation_storage=env.conversation_storage,
            conversation_id=conv["id"], profile="dog", run_id="run-1",
            assistant_msg_id=msg_id, raw_text=final, final_text=final,
            context_id=conv["context_id"],
        )

        assert posted == []
        assert env.posts == []
        marker = await _marker(env, msg_id)
        assert marker["kind"] == "silent"
        assert marker["posted_message_ids"] == []

    asyncio.run(run())


def test_a_sentinel_with_an_echoed_routing_note_still_means_silence(
    tmp_path, monkeypatch,
) -> None:
    """Every line a seat receives now ends with a routing note, so an agent
    copying the shape onto its own answer is a matter of time. Stuck to the
    sentinel it defeats the reduction, and the room would be shown the word
    "[silent]"."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        _, conv, msg_id = await _seat(env)

        posted = await on_shadow_turn_complete(
            conversation_storage=env.conversation_storage,
            conversation_id=conv["id"], profile="dog", run_id="run-1",
            assistant_msg_id=msg_id,
            raw_text="[silent]\n[to: you]", final_text="[silent]\n[to: you]",
            context_id=conv["context_id"],
        )

        assert posted == []
        assert env.posts == []
        assert (await _marker(env, msg_id))["kind"] == "silent"

    asyncio.run(run())


def test_an_echoed_note_is_stripped_from_a_real_answer(tmp_path, monkeypatch) -> None:
    """The room is shown what the agent said, not the annotation it copied."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        _, conv, msg_id = await _seat(env)

        await on_shadow_turn_complete(
            conversation_storage=env.conversation_storage,
            conversation_id=conv["id"], profile="dog", run_id="run-1",
            assistant_msg_id=msg_id,
            raw_text="It is 14:20.\n[to: everyone in the room]",
            final_text="It is 14:20.\n[to: everyone in the room]",
            context_id=conv["context_id"],
        )

        assert [p["content"] for p in env.posts] == ["It is 14:20."]

    asyncio.run(run())


@pytest.mark.parametrize("final", ["(no response)", "(stopped)"])
def test_a_turn_with_no_text_of_its_own_posts_nothing(
    tmp_path, monkeypatch, final,
) -> None:
    env = _env(tmp_path, monkeypatch)

    async def run():
        _, conv, msg_id = await _seat(env)

        assert await on_shadow_turn_complete(
            conversation_storage=env.conversation_storage,
            conversation_id=conv["id"], profile="dog", run_id="run-1",
            assistant_msg_id=msg_id, raw_text=final, final_text=final,
            context_id=conv["context_id"],
        ) == []

        assert env.posts == []
        marker = await _marker(env, msg_id)
        assert marker["kind"] == "skipped"
        assert marker["reason"] == "empty"

    asyncio.run(run())


def test_a_stopped_turn_posts_nothing_and_says_nothing_to_the_room(
    tmp_path, monkeypatch,
) -> None:
    """Someone pressed Stop. Half an answer is not an answer, and the room does
    not need telling that a person changed their mind."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        _, conv, msg_id = await _seat(env)

        assert await on_shadow_turn_complete(
            conversation_storage=env.conversation_storage,
            conversation_id=conv["id"], profile="dog", run_id="run-1",
            assistant_msg_id=msg_id, raw_text="I was saying", final_text="I was saying",
            cancelled=True, context_id=conv["context_id"],
        ) == []

        assert env.posts == []
        marker = await _marker(env, msg_id)
        assert marker["kind"] == "skipped"
        assert marker["reason"] == "cancelled"

    asyncio.run(run())


def test_a_failed_turn_is_announced_as_a_system_notice(tmp_path, monkeypatch) -> None:
    """Otherwise the agent just stops existing from everyone else's point of
    view. ``deliver_only`` because nobody should answer a crash report."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, conv, msg_id = await _seat(env)

        assert await on_shadow_turn_complete(
            conversation_storage=env.conversation_storage,
            conversation_id=conv["id"], profile="dog", run_id="run-1",
            assistant_msg_id=msg_id, raw_text="", final_text="",
            errored=True, context_id=conv["context_id"],
        ) == []

        assert len(env.posts) == 1
        notice = env.posts[0]
        assert notice["group_id"] == group["id"]
        assert notice["sender_kind"] == "system"
        assert notice["deliver_only"] is True
        assert "Rex" in notice["content"]
        assert notice.get("sender_profile") is None

        marker = await _marker(env, msg_id)
        assert marker["kind"] == "skipped"
        assert marker["reason"] == "errored"

    asyncio.run(run())


def test_a_profile_that_left_the_group_mid_turn_posts_nothing(
    tmp_path, monkeypatch,
) -> None:
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, conv, msg_id = await _seat(env)
        await env.storage.set_members(group["id"], ["cat"])

        assert await on_shadow_turn_complete(
            conversation_storage=env.conversation_storage,
            conversation_id=conv["id"], profile="dog", run_id="run-1",
            assistant_msg_id=msg_id, raw_text="hello", final_text="hello",
            context_id=conv["context_id"],
        ) == []

        assert env.posts == []
        marker = await _marker(env, msg_id)
        assert marker["kind"] == "skipped"
        assert marker["reason"] == "not_a_member"

    asyncio.run(run())


# ── the marker, always ──────────────────────────────────────────────────────


def test_every_outcome_leaves_a_marker_on_the_agent_row(tmp_path, monkeypatch) -> None:
    """An unstamped row is what the boot sweep reads as "this turn never got to
    post", so a missing stamp means a deliberate silence gets re-posted on the
    next restart."""
    env = _env(tmp_path, monkeypatch)

    cases = [
        ({"raw_text": "hello", "final_text": "hello"}, "posted"),
        ({"raw_text": "[silent]", "final_text": "[silent]"}, "silent"),
        ({"raw_text": "x", "final_text": "(no response)"}, "skipped"),
        ({"raw_text": "x", "final_text": "x", "cancelled": True}, "skipped"),
        ({"raw_text": "x", "final_text": "x", "errored": True}, "skipped"),
    ]

    async def run():
        _, conv, _ = await _seat(env)
        for kwargs, expected in cases:
            row = await env.conversation_storage.add_message(
                conv["id"], "agent", content="turn",
            )
            await on_shadow_turn_complete(
                conversation_storage=env.conversation_storage,
                conversation_id=conv["id"], profile="dog", run_id="run-1",
                assistant_msg_id=row["id"], context_id=conv["context_id"], **kwargs,
            )
            marker = await _marker(env, row["id"])
            assert marker.get("kind") == expected, kwargs
            assert "posted_message_ids" in marker

    asyncio.run(run())


def test_a_post_that_fails_never_fails_the_turn(tmp_path, monkeypatch) -> None:
    """A room that cannot be posted to must not turn a completed turn into a
    failed one — the agent's answer is already persisted and shown."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        _, conv, msg_id = await _seat(env)

        async def boom(**kwargs):
            raise RuntimeError("timeline unavailable")

        monkeypatch.setattr(fanout, "post_message", boom)

        assert await on_shadow_turn_complete(
            conversation_storage=env.conversation_storage,
            conversation_id=conv["id"], profile="dog", run_id="run-1",
            assistant_msg_id=msg_id, raw_text="hello", final_text="hello",
            context_id=conv["context_id"],
        ) == []

        marker = await _marker(env, msg_id)
        assert marker["kind"] == "skipped"
        assert marker["reason"] == "error"

    asyncio.run(run())


def test_a_post_refused_as_a_duplicate_is_recorded_as_such(
    tmp_path, monkeypatch,
) -> None:
    """A crash between posting and stamping: the sweep re-runs the hook, the
    unique ``(source_message_id, segment)`` refuses it, and the marker says so
    rather than claiming a post that was already there."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        _, conv, msg_id = await _seat(env)

        async def duplicate(**kwargs):
            return None

        monkeypatch.setattr(fanout, "post_message", duplicate)

        assert await on_shadow_turn_complete(
            conversation_storage=env.conversation_storage,
            conversation_id=conv["id"], profile="dog", run_id="run-1",
            assistant_msg_id=msg_id, raw_text="hello", final_text="hello",
            context_id=conv["context_id"],
        ) == []

        marker = await _marker(env, msg_id)
        assert marker["kind"] == "skipped"
        assert marker["reason"] == "duplicate"

    asyncio.run(run())


# ── "X is thinking…" ────────────────────────────────────────────────────────


def test_agent_status_is_a_no_op_outside_a_seat(tmp_path, monkeypatch) -> None:
    """It is called unconditionally on the hot path of every turn in the
    process, so a normal chat must cost nothing and publish nothing."""
    _env(tmp_path, monkeypatch)
    bus = get_group_stream_bus()

    async def run():
        before = len(bus.snapshot("g-none"))
        await publish_agent_status(
            conv={"id": "c1", "context_id": None}, profile="dog", state="thinking",
        )
        await publish_agent_status(conv=None, profile="dog", state="thinking")
        await publish_agent_status(
            conv={"context_id": "event:123"}, profile="dog", state="thinking",
        )
        assert len(bus.snapshot("g-none")) == before

    asyncio.run(run())


def test_agent_status_reaches_the_room_for_a_seat(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    bus = get_group_stream_bus()

    async def run():
        group, _conv, _msg_id = await _seat(env)
        gid = group["id"]
        bus.discard(gid)
        await publish_agent_status(
            conv={"context_id": shadow_context_id(gid, "dog")},
            profile="dog", state="thinking",
        )
        await publish_agent_status(
            conv={"context_id": shadow_context_id(gid, "dog")},
            profile="dog", state="idle",
        )

        frames = bus.snapshot(gid)
        assert [f["type"] for f in frames] == ["agent_status", "agent_status"]
        assert frames[0]["data"] == {
            "profile": "dog", "agent_name": "Rex", "state": "thinking",
        }
        assert frames[1]["data"]["state"] == "idle"
        bus.discard(gid)

    asyncio.run(run())


def test_a_deleted_room_is_not_resurrected_by_a_turn_still_finishing(
    tmp_path, monkeypatch,
) -> None:
    """Deleting a group does not stop the turns already running in its seats.

    The delete pops the room's replay ring and its sequence counter; a seat
    reaching its ``finally`` a moment later used to put both back — for a room
    nobody can subscribe to and nothing will ever discard again. So the status
    hook reads the room first, exactly as the turn-end hook does before it
    posts.
    """
    env = _env(tmp_path, monkeypatch)
    bus = get_group_stream_bus()

    async def run():
        group, _conv, _msg_id = await _seat(env)
        gid = group["id"]
        # The order the delete endpoint uses: the row goes, then the bus.
        await env.storage.delete_group(gid)
        bus.discard(gid)

        await publish_agent_status(
            conv={"context_id": shadow_context_id(gid, "dog")},
            profile="dog", state="idle",
        )

        assert bus.snapshot(gid) == []
        assert gid not in bus._ring
        assert gid not in bus._seq

    asyncio.run(run())


def test_a_sentinel_and_note_on_one_line_still_means_silence(
    tmp_path, monkeypatch,
) -> None:
    """Whole-line stripping alone leaves this looking like an answer, and the
    room is shown the word the sentinel exists to hide. Nothing is published
    either way, so the judgement is the tolerant one."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        _, conv, msg_id = await _seat(env)

        posted = await on_shadow_turn_complete(
            conversation_storage=env.conversation_storage,
            conversation_id=conv["id"], profile="dog", run_id="run-1",
            assistant_msg_id=msg_id,
            raw_text="[silent] [to: you]", final_text="[silent] [to: you]",
            context_id=conv["context_id"],
        )

        assert posted == []
        assert env.posts == []
        assert (await _marker(env, msg_id))["kind"] == "silent"

    asyncio.run(run())
