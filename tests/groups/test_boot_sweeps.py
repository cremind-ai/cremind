"""Repairing what a crash interrupted, at boot.

Both sweeps exist because the two halves of "an agent answered" are not one
write: the agent's message is persisted by the runner, and the post is made by
the turn-end hook afterwards. A process that dies in between leaves a turn the
room never heard, and a fan-out that dies half-way leaves members who never got
the message.

Both must be safe to run on every boot, so what they DON'T touch is as
load-bearing as what they do — a sweep that re-posted an already-posted turn, or
re-delivered to a member who has it, would be worse than no sweep at all.
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

import app.groups.boot as boot  # noqa: E402
import app.groups.fanout as fanout  # noqa: E402
import app.storage as storage_module  # noqa: E402
import app.utils.agent_name as agent_name_module  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.groups.shadow import shadow_context_id  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402
from app.storage.group_chat_storage import GroupChatStorage  # noqa: E402

_TABLES = (
    "profiles", "channels", "conversations", "messages",
    "group_chats", "group_chat_members", "group_chat_messages",
)

_MEMBERS = ("dog", "cat", "chicken")


def _env(tmp_path: Path, monkeypatch):
    provider = SqliteDatabaseProvider(str(tmp_path / "boot.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        for profile in _MEMBERS:
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
    monkeypatch.setattr(boot, "_storages", lambda: (storage, conversation_storage))
    monkeypatch.setattr(
        storage_module, "get_group_chat_storage", lambda *a, **k: storage,
    )
    monkeypatch.setattr(agent_name_module, "read_agent_name", lambda profile: profile.title())

    posts: list[dict] = []
    deliveries: list[dict] = []
    counter = {"n": 0}

    async def fake_post(**kwargs):
        posts.append(kwargs)
        counter["n"] += 1
        return {"id": f"post-{counter['n']}", **kwargs}

    async def fake_deliver(**kwargs):
        deliveries.append(kwargs)

    monkeypatch.setattr(fanout, "post_message", fake_post)
    monkeypatch.setattr(fanout, "_deliver_to_member", fake_deliver)

    return SimpleNamespace(
        storage=storage,
        conversation_storage=conversation_storage,
        posts=posts,
        deliveries=deliveries,
    )


async def _seated_group(env, profile="dog", members=_MEMBERS) -> tuple[dict, dict]:
    """A group where ``profile`` already has a seat conversation."""
    group = await env.storage.create_group(name="Ops", members=list(members))
    conv = await env.conversation_storage.create_conversation(
        profile=profile,
        context_id=shadow_context_id(group["id"], profile),
        title="Group: Ops",
        kind="group_chat",
    )
    await env.storage.set_shadow_conversation(group["id"], profile, conv["id"])
    return group, conv


# ── stranded turns ──────────────────────────────────────────────────────────


def test_a_turn_the_process_died_before_posting_is_posted_once(
    tmp_path, monkeypatch,
) -> None:
    """No ``metadata.group`` on the newest agent row means the hook never ran.
    Re-running it is safe: the unique ``(source_message_id, segment)`` refuses a
    second copy, and the stamp it leaves stops the next boot repeating this."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, conv = await _seated_group(env)
        row = await env.conversation_storage.add_message(
            conv["id"], "agent", content="It is 14:20.",
            token_usage={"input_tokens": 10, "output_tokens": 4},
        )

        assert await boot.sweep_unposted_agent_rows() == 1

        assert len(env.posts) == 1
        assert env.posts[0]["group_id"] == group["id"]
        assert env.posts[0]["content"] == "It is 14:20."
        assert env.posts[0]["sender_profile"] == "dog"
        assert env.posts[0]["source_message_id"] == row["id"]

        stamped = await env.conversation_storage.get_message(row["id"])
        assert stamped["metadata"]["group"]["kind"] == "posted"
        assert stamped["metadata"]["group"]["run_id"] == "boot-sweep"

        # The second boot finds it accounted for.
        assert await boot.sweep_unposted_agent_rows() == 0
        assert len(env.posts) == 1

    asyncio.run(run())


def test_the_sweep_reuses_the_run_a_stranded_turn_already_posted_under(
    tmp_path, monkeypatch,
) -> None:
    """A turn that answered an interruption posted before its message existed,
    so those rows are owned by the run until the completion hook hands them
    over. If the process died in between, the sweep has to claim them under the
    SAME run — passing "boot-sweep" instead would leave them orphaned and say
    the interim reply a second time."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        _, conv = await _seated_group(env)
        row = await env.conversation_storage.add_message(
            conv["id"], "agent", content="Not yet.\n\nDone.",
            token_usage={"input_tokens": 10, "output_tokens": 4},
            metadata={
                "mid_turn_breaks": [{"content_offset": 8}],
                "run_id": "group:conv-1:246b2086-02e7-49ac-9f40-78672ef8e0ca",
            },
        )

        assert await boot.sweep_unposted_agent_rows() == 1
        stamped = await env.conversation_storage.get_message(row["id"])
        assert stamped["metadata"]["group"]["run_id"] == (
            "group:conv-1:246b2086-02e7-49ac-9f40-78672ef8e0ca"
        )

    asyncio.run(run())


def test_a_turn_that_already_had_its_say_is_left_alone(tmp_path, monkeypatch) -> None:
    """Including a deliberate silence — which is exactly why the hook stamps
    the silent and skipped outcomes too."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        _, conv = await _seated_group(env)
        for marker in ({"kind": "posted", "posted_message_ids": ["x"]},
                       {"kind": "silent", "posted_message_ids": []},
                       {"kind": "skipped", "reason": "cancelled"}):
            await env.conversation_storage.add_message(
                conv["id"], "agent", content="whatever",
                token_usage={"input_tokens": 1}, metadata={"group": marker},
            )
            assert await boot.sweep_unposted_agent_rows() == 0

        assert env.posts == []

    asyncio.run(run())


def test_a_row_no_llm_call_produced_is_not_a_turn(tmp_path, monkeypatch) -> None:
    """An event-trigger bubble is written as an ``agent`` row with no usage.
    Posting it would put a system notice into the room under the agent's name."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        _, conv = await _seated_group(env)
        await env.conversation_storage.add_message(
            conv["id"], "agent", content="Event: file changed", token_usage=None,
        )

        assert await boot.sweep_unposted_agent_rows() == 0
        assert env.posts == []

    asyncio.run(run())


def test_a_seat_with_nothing_in_it_is_skipped(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)

    async def run():
        await _seated_group(env)
        # cat and chicken have no seat pointer at all, dog's seat is empty.
        assert await boot.sweep_unposted_agent_rows() == 0
        assert env.posts == []

    asyncio.run(run())


def test_one_unreadable_seat_does_not_abort_the_sweep(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, conv = await _seated_group(env)
        await env.conversation_storage.add_message(
            conv["id"], "agent", content="It is 14:20.",
            token_usage={"input_tokens": 1},
        )
        # A second member whose seat pointer names a conversation that is gone.
        await env.storage.set_shadow_conversation(group["id"], "cat", "ghost-conv")

        original = env.conversation_storage.get_latest_agent_message

        async def flaky(conversation_id):
            if conversation_id == "ghost-conv":
                raise RuntimeError("conversation vanished")
            return await original(conversation_id)

        monkeypatch.setattr(
            env.conversation_storage, "get_latest_agent_message", flaky,
        )

        assert await boot.sweep_unposted_agent_rows() == 1
        assert len(env.posts) == 1

    asyncio.run(run())


# ── unfinished fan-outs ─────────────────────────────────────────────────────


def test_only_the_members_who_missed_it_are_delivered_to(tmp_path, monkeypatch) -> None:
    """``delivered_to`` is written member by member as the fan-out goes, so a
    crash halfway leaves an accurate record to finish from — delivering to
    everybody again would answer the same message twice."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, _ = await _seated_group(env)
        row = await env.storage.add_message(
            group_id=group["id"], sender_kind="agent", sender_profile="dog",
            sender_name="Dog", content="Twelve eggs today.", hop=1,
            delivered_to=["dog", "cat"],
        )

        assert await boot.sweep_undelivered_group_messages() == 1

        assert [d["member"] for d in env.deliveries] == ["chicken"]
        assert env.deliveries[0]["rendered"] == "Dog (agent): Twelve eggs today."
        assert env.deliveries[0]["capped"] is False
        assert env.deliveries[0]["row"]["id"] == row["id"]

        persisted = await env.storage.get_message(row["id"])
        assert set(persisted["delivered_to"]) == set(_MEMBERS)

        # Nothing left to do on the next boot.
        assert await boot.sweep_undelivered_group_messages() == 0

    asyncio.run(run())


def test_the_sender_is_never_delivered_its_own_message(tmp_path, monkeypatch) -> None:
    """It said it. Handing it back would read as somebody else speaking, and
    would start a turn answering itself."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, _ = await _seated_group(env)
        await env.storage.add_message(
            group_id=group["id"], sender_kind="agent", sender_profile="dog",
            sender_name="Dog", content="on it", delivered_to=[],
        )

        await boot.sweep_undelivered_group_messages()

        assert {d["member"] for d in env.deliveries} == {"cat", "chicken"}

    asyncio.run(run())


def test_a_quiet_message_is_re_delivered_without_starting_a_turn(
    tmp_path, monkeypatch,
) -> None:
    """A message that was capped when it was posted must not be un-capped by
    the sweep — that would start exactly the turn the cap prevented."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, _ = await _seated_group(env)
        await env.storage.add_message(
            group_id=group["id"], sender_kind="agent", sender_profile="dog",
            sender_name="Dog", content="still talking", hop=9,
            delivered_to=["dog"], metadata={"quiet": True, "quiet_reason": "hop_limit"},
        )

        await boot.sweep_undelivered_group_messages()

        assert {d["member"] for d in env.deliveries} == {"cat", "chicken"}
        assert all(d["capped"] is True for d in env.deliveries)

    asyncio.run(run())


def test_a_message_beyond_the_hop_cap_is_capped_on_re_delivery_too(
    tmp_path, monkeypatch,
) -> None:
    """Re-derived from the settings rather than trusted from the row, so a cap
    lowered since the post still holds."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await env.storage.create_group(
            name="Ops", settings={"max_agent_hops": 2}, members=list(_MEMBERS),
        )
        await env.storage.add_message(
            group_id=group["id"], sender_kind="agent", sender_profile="dog",
            sender_name="Dog", content="still talking", hop=2, delivered_to=["dog"],
        )

        await boot.sweep_undelivered_group_messages()

        assert all(d["capped"] is True for d in env.deliveries)

    asyncio.run(run())


def test_the_sweep_does_not_start_the_turns_the_router_declined(
    tmp_path, monkeypatch,
) -> None:
    """The decision lives on the row, not in memory, so a restart finishes the
    fan-out the way it began. Re-classifying would cost a call per swept message
    and could answer differently — waking agents the room already passed over."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, _ = await _seated_group(env)
        await env.storage.add_message(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="Mia, what did we spend?", delivered_to=[],
            metadata={"routing": {"targets": ["cat"], "everyone": False}},
        )

        await boot.sweep_undelivered_group_messages()

        by_member = {d["member"]: d for d in env.deliveries}
        assert set(by_member) == {"dog", "cat", "chicken"}
        assert by_member["cat"]["capped"] is False
        assert by_member["cat"]["routed_away"] is False
        for passed_over in ("dog", "chicken"):
            assert by_member[passed_over]["capped"] is True
            assert by_member[passed_over]["routed_away"] is True

    asyncio.run(run())


def test_an_everyone_decision_sweeps_like_an_unrouted_message(
    tmp_path, monkeypatch,
) -> None:
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, _ = await _seated_group(env)
        await env.storage.add_message(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="status?", delivered_to=[],
            metadata={"routing": {"targets": [], "everyone": True}},
        )

        await boot.sweep_undelivered_group_messages()

        assert all(d["capped"] is False for d in env.deliveries)
        assert all(d["routed_away"] is False for d in env.deliveries)

    asyncio.run(run())


def test_a_nobody_decision_sweeps_quiet_for_every_member(
    tmp_path, monkeypatch,
) -> None:
    """A reply that asked nothing of anyone is still delivered to everyone, and
    still starts no turn — a restart must not undo that by waking the room to
    read a remark that was finished before the crash."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, _ = await _seated_group(env)
        await env.storage.add_message(
            group_id=group["id"], sender_kind="agent", sender_profile="dog",
            sender_name="Rex", content="It is 14:20.", delivered_to=[],
            metadata={"routing": {"targets": [], "everyone": False, "nobody": True}},
        )

        await boot.sweep_undelivered_group_messages()

        by_member = {d["member"]: d for d in env.deliveries}
        assert set(by_member) == {"cat", "chicken"}  # the sender is not swept
        assert all(d["capped"] is True for d in env.deliveries)
        assert all(d["routed_away"] is True for d in env.deliveries)

    asyncio.run(run())


def test_a_stamp_from_before_the_nobody_outcome_still_wakes_its_targets(
    tmp_path, monkeypatch,
) -> None:
    """Rows written by an older server carry no ``nobody`` key, and false is
    what they meant: somebody was woken."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, _ = await _seated_group(env)
        await env.storage.add_message(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="Mia?", delivered_to=[],
            metadata={"routing": {"targets": ["cat"], "everyone": False}},
        )

        await boot.sweep_undelivered_group_messages()

        by_member = {d["member"]: d for d in env.deliveries}
        assert by_member["cat"]["routed_away"] is False

    asyncio.run(run())


def test_an_unreadable_routing_stamp_wakes_everyone(tmp_path, monkeypatch) -> None:
    """A row written before routing existed, or a truncated stamp, falls open to
    the sweep's old behaviour rather than silencing the room on a bad read."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, _ = await _seated_group(env)
        for stamp in ("nonsense", {"targets": "cat"}, {}):
            await env.storage.add_message(
                group_id=group["id"], sender_kind="user", sender_name="Alexa",
                content=f"say something {stamp!r}", delivered_to=[],
                metadata={"routing": stamp},
            )

        await boot.sweep_undelivered_group_messages()

        assert env.deliveries
        assert all(d["capped"] is False for d in env.deliveries)
        assert all(d["routed_away"] is False for d in env.deliveries)

    asyncio.run(run())


def test_a_capped_row_stays_capped_whatever_the_router_said(
    tmp_path, monkeypatch,
) -> None:
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, _ = await _seated_group(env)
        await env.storage.add_message(
            group_id=group["id"], sender_kind="agent", sender_profile="dog",
            sender_name="Dog", content="still talking", hop=9, delivered_to=["dog"],
            metadata={
                "quiet": True, "quiet_reason": "hop_limit",
                "routing": {"targets": ["cat"], "everyone": False},
            },
        )

        await boot.sweep_undelivered_group_messages()

        assert {d["member"] for d in env.deliveries} == {"cat", "chicken"}
        assert all(d["capped"] is True for d in env.deliveries)
        # The cap is the reason, not the router: they stay tellable apart.
        assert all(d["routed_away"] is False for d in env.deliveries)

    asyncio.run(run())


def test_a_fully_delivered_room_costs_nothing(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, _ = await _seated_group(env)
        await env.storage.add_message(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="status?", delivered_to=list(_MEMBERS),
        )

        assert await boot.sweep_undelivered_group_messages() == 0
        assert env.deliveries == []

    asyncio.run(run())


def test_a_delivery_that_fails_leaves_the_rest_to_the_next_boot(
    tmp_path, monkeypatch,
) -> None:
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, _ = await _seated_group(env)
        row = await env.storage.add_message(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="status?", delivered_to=[],
        )

        async def flaky(**kwargs):
            if kwargs["member"] == "cat":
                raise RuntimeError("seat unavailable")
            env.deliveries.append(kwargs)

        monkeypatch.setattr(fanout, "_deliver_to_member", flaky)

        assert await boot.sweep_undelivered_group_messages() == 2

        persisted = await env.storage.get_message(row["id"])
        assert set(persisted["delivered_to"]) == {"dog", "chicken"}

    asyncio.run(run())


# ── seats ───────────────────────────────────────────────────────────────────


def test_missing_seats_are_created_before_the_adapters_start(
    tmp_path, monkeypatch,
) -> None:
    """A member with no seat has nowhere for a message to land, and the seat is
    created lazily during a fan-out — which is too late if the first message
    arrives from a platform adapter."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, conv = await _seated_group(env)

        created = await boot.ensure_all_shadow_conversations()

        assert created == 2  # cat and chicken; dog already had one
        for profile in _MEMBERS:
            member = await env.storage.get_member(group["id"], profile)
            assert member["shadow_conversation_id"]
        # Dog's existing seat is reused, not replaced.
        assert (await env.storage.get_member(group["id"], "dog"))[
            "shadow_conversation_id"
        ] == conv["id"]

        # And a second boot creates nothing.
        assert await boot.ensure_all_shadow_conversations() == 0

    asyncio.run(run())


def test_a_deleted_profile_releases_its_seats_runtime_state(
    tmp_path, monkeypatch,
) -> None:
    """The membership rows CASCADE away in the database, but the queue workers,
    stream-bus entries and run bindings for that profile's seats do not — which
    is the whole reason this function exists.

    Written the way it should behave: ``ConversationStreamBus.discard`` is a
    coroutine (every other caller awaits it), so calling it bare leaves the
    seq counter and replay ring in memory forever and raises a RuntimeWarning.
    """
    import app.groups.index as index_module
    from app.events.stream_bus import get_event_stream_bus
    from app.groups.index import GroupIndex

    env = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(index_module, "_instance", GroupIndex())
    bus = get_event_stream_bus()
    seats: list[str] = []

    async def run():
        _, conv = await _seated_group(env)
        seats.append(conv["id"])
        await index_module.get_group_index().refresh()

        await bus.publish(conv["id"], "text", {"token": "hello"})
        assert conv["id"] in bus._seq

        await boot.on_profile_deleted("dog")

        assert conv["id"] not in bus._seq

    try:
        asyncio.run(run())
    finally:
        # The bus is a process-wide singleton; whichever way this went, do not
        # leave this test's conversation in it.
        for seat_id in seats:
            bus._seq.pop(seat_id, None)
            bus._ring.pop(seat_id, None)


def test_every_seat_gets_its_own_context_id(tmp_path, monkeypatch) -> None:
    """``context_id`` keys the per-conversation tool state — working directory,
    loaded skills, current query — so a shared one would let Dog's
    ``change_working_directory`` silently move Cat's, across tenants."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group, _ = await _seated_group(env)
        await boot.ensure_all_shadow_conversations()

        context_ids = set()
        for profile in _MEMBERS:
            member = await env.storage.get_member(group["id"], profile)
            conv = await env.conversation_storage.get_conversation(
                member["shadow_conversation_id"]
            )
            assert conv["kind"] == "group_chat"
            assert conv["profile"] == profile
            context_ids.add(conv["context_id"])

        assert context_ids == {
            shadow_context_id(group["id"], p) for p in _MEMBERS
        }

    asyncio.run(run())
