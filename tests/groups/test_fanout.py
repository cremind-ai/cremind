"""Posting to a group: one timeline row, then a turn in every other seat.

This is the whole feature in one function, so it is tested against the real
storage — the hop count and the ``ordering`` are both derived from rows already
in the timeline, and a fake would let them drift from what the queries actually
return.

What IS faked is everything downstream of the decision: parking and enqueueing.
They are imported inside :func:`app.groups.fanout._deliver_to_member` (late, to
keep ``app.groups`` free of a channels/agent import cycle), so the patches land
on the DEFINING modules — ``app.events.user_message_delivery``,
``app.events.queue``, ``app.agent.stream_runner`` — rather than on names in
``fanout``'s own namespace, which do not exist until the call runs.

The behaviour under test, in one line each: everyone but the sender gets it, a
busy member gets it folded into the turn it is already running, at the hop cap
the room keeps delivering but stops answering, the router narrows who starts a
turn without ever narrowing who receives the message, and posts reach both the
timeline and the seats in the order they arrived — never in the order their
classifications happened to come back.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402
from sqlalchemy import text  # noqa: E402

import app.agent.stream_runner as stream_runner  # noqa: E402
import app.config.user_config as user_config  # noqa: E402
import app.utils.agent_name as agent_name_module  # noqa: E402
import app.events.queue as event_queue  # noqa: E402
import app.events.runner as event_runner  # noqa: E402
import app.events.user_message_delivery as user_message_delivery  # noqa: E402
import app.groups.fanout as fanout  # noqa: E402
import app.groups.routing as routing_module  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.events.user_message_delivery import ParkOutcome  # noqa: E402
from app.groups.bus import get_group_stream_bus  # noqa: E402
from app.groups.routing import ROUTING_SETTING_KEY, RoutingDecision  # noqa: E402
from app.groups.shadow import shadow_context_id  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402
from app.storage.group_chat_storage import GroupChatStorage  # noqa: E402

_TABLES = (
    "profiles", "channels", "conversations", "messages",
    "group_chats", "group_chat_members", "group_chat_messages",
)

_MEMBERS = ("dog", "cat", "chicken")


def _env(tmp_path: Path, monkeypatch):
    """Real storage, faked delivery. Returns the handles the tests assert on."""
    provider = SqliteDatabaseProvider(str(tmp_path / "fanout.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        for profile in (*_MEMBERS, "duck"):
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
    monkeypatch.setattr(fanout, "_storages", lambda: (storage, conversation_storage))

    seats: dict[tuple, dict] = {}

    async def fake_ensure(conv_storage, profile, group):
        key = (group["id"], profile)
        if key not in seats:
            seats[key] = await conv_storage.create_conversation(
                profile=profile,
                context_id=shadow_context_id(group["id"], profile),
                title=f"Group: {group.get('name')}",
                kind="group_chat",
            )
        return seats[key]

    monkeypatch.setattr(fanout, "ensure_shadow_conversation", fake_ensure)

    parks: list[dict] = []
    enqueues: list[dict] = []
    outcomes: dict[str, ParkOutcome] = {}

    async def fake_park(**kwargs):
        parks.append(kwargs)
        return outcomes.get(kwargs.get("profile"))

    async def fake_enqueue(**kwargs):
        enqueues.append(kwargs)

    monkeypatch.setattr(user_message_delivery, "try_park_user_message", fake_park)
    monkeypatch.setattr(event_queue, "enqueue_user_message", fake_enqueue)

    # The routing note names the agents the way the room does. Patched on the
    # defining module (``_routing_note`` imports it inside the call) and to the
    # same names the test content uses, so a note reads as the room reads and a
    # profile id leaking into one is visible rather than plausible.
    monkeypatch.setattr(
        agent_name_module,
        "read_agent_name",
        lambda profile: {"dog": "Rex", "cat": "Mia", "chicken": "Nugget"}.get(
            profile, profile,
        ),
    )

    # Routing is ON by default, so every post here would otherwise reach for a
    # real classifier. Patched on the DEFINING module for the same reason as the
    # rest: ``fanout`` imports it inside the call.
    routes: list[dict] = []
    routed: dict = {"decision": RoutingDecision(reason="everyone")}
    llm = SimpleNamespace(provider_name="fake", model_name="fake-mini")
    routing_profiles: list[str] = []

    class _Agent:
        def low_performance_llm(self, profile):
            routing_profiles.append(profile)
            return llm

    monkeypatch.setattr(event_runner, "get_cremind_agent", lambda: _Agent())

    async def fake_route(**kwargs):
        routes.append(kwargs)
        outcome = routed["decision"]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(routing_module, "route_message", fake_route)
    monkeypatch.setattr(
        stream_runner, "make_run_id",
        lambda conversation_id, kind="msg": f"{kind}:{conversation_id}:fixed",
    )
    # Reading it would reach for a user-config store this harness has no rows in.
    monkeypatch.setattr(user_config, "replay_reasoning_enabled", lambda profile: False)

    return SimpleNamespace(
        storage=storage,
        conversation_storage=conversation_storage,
        seats=seats,
        parks=parks,
        enqueues=enqueues,
        outcomes=outcomes,
        routes=routes,
        routed=routed,
        llm=llm,
        routing_profiles=routing_profiles,
    )


async def _make_group(env, *, settings=None, members=_MEMBERS) -> dict:
    return await env.storage.create_group(
        name="Ops", settings=settings, members=list(members),
    )


async def _post(**kwargs):
    """Post, and give anything watching the room's bus a turn to drain it."""
    row = await fanout.post_message(**kwargs)
    await asyncio.sleep(0)
    return row


async def _seat_messages(env, group_id: str, profile: str) -> list[dict]:
    seat = env.seats.get((group_id, profile))
    if seat is None:
        return []
    return await env.conversation_storage.get_messages(seat["id"])


def _enqueued_profiles(env) -> set[str]:
    return {k["profile"] for k in env.enqueues}


# ── the ordinary case ───────────────────────────────────────────────────────


def test_a_user_post_reaches_every_member_and_the_room(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        bus = get_group_stream_bus()
        queue, _ = await bus.subscribe(group["id"])

        row = await _post(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="what time is it?",
            sender_identity={"channel_type": "web", "sender_id": "admin"},
        )

        assert row["hop"] == 0
        assert row["ordering"] == 0
        assert row["sender_kind"] == "user"

        # Every member is asked, because each one decides for itself whether it
        # was addressed.
        assert _enqueued_profiles(env) == set(_MEMBERS)
        assert set(row["delivered_to"]) == set(_MEMBERS)
        persisted = await env.storage.get_message(row["id"])
        assert set(persisted["delivered_to"]) == set(_MEMBERS)

        frame = await queue.get()
        assert frame["type"] == "message"
        assert frame["data"]["id"] == row["id"]

        await bus.unsubscribe(group["id"], queue)
        bus.discard(group["id"])

    asyncio.run(run())


def test_the_enqueue_says_exactly_what_a_seat_turn_needs(tmp_path, monkeypatch) -> None:
    """The attribution has to live in the CONTENT: history hands the model only
    role and content, so a speaker recorded in metadata is a speaker it never
    learns about. The rest of the kwargs keep a group turn from behaving like a
    normal chat turn (no title rewrite, no second persist)."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env, members=["dog"])
        await _post(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="what time is it?",
        )

        call = env.enqueues[0]
        assert call["query"] == "Alexa (user): what time is it?"
        assert call["profile"] == "dog"
        assert call["conversation_id"] == env.seats[(group["id"], "dog")]["id"]
        assert call["update_title_from_query"] is False
        assert call["push_user_message"] is True
        assert call["existing_user_message_id"] is None
        assert call["reasoning"] is True
        assert call["run_id"].startswith("group:")

        marker = call["user_message_metadata"]
        assert marker["source"] == "group_chat"
        assert marker["group"]["group_id"] == group["id"]
        assert marker["group"]["group_name"] == "Ops"
        assert marker["group"]["sender_name"] == "Alexa"
        assert marker["group"]["sender_kind"] == "user"
        assert marker["group"]["hop"] == 0
        assert marker["group"]["quiet"] is False

        # The park is offered first, with the same text and marker.
        assert env.parks[0]["query"] == call["query"]
        assert env.parks[0]["user_message_metadata"] == marker

    asyncio.run(run())


def test_an_agent_post_skips_its_own_seat(tmp_path, monkeypatch) -> None:
    """It already said it — handing it back would read as somebody else."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        row = await _post(
            group_id=group["id"], sender_kind="agent", sender_profile="dog",
            sender_name="Rex", content="It is 14:20.",
            originated_from_shadow_turn=True,
        )

        assert _enqueued_profiles(env) == {"cat", "chicken"}
        assert env.enqueues[0]["query"] == (
            "Rex (agent): It is 14:20.\n[to: everyone in the room]"
        )
        assert "dog" in row["delivered_to"]  # the sender counts as delivered
        assert await _seat_messages(env, group["id"], "dog") == []

    asyncio.run(run())


# ── busy members ────────────────────────────────────────────────────────────


def test_a_busy_member_folds_the_message_into_the_turn_it_is_running(
    tmp_path, monkeypatch,
) -> None:
    """A group is a place where several agents are busy at once, so this is the
    normal case rather than the exception — and one reply then covers everything
    that arrived instead of answering a stale question twice."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        env.outcomes["cat"] = ParkOutcome(injected=True, message_id="cat-msg")

        await _post(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="status?",
        )

        assert {p["profile"] for p in env.parks} == set(_MEMBERS)
        # Cat's running turn absorbed it; no second turn is started for it.
        assert _enqueued_profiles(env) == {"dog", "chicken"}

    asyncio.run(run())


def test_a_park_that_lost_the_race_runs_without_persisting_twice(
    tmp_path, monkeypatch,
) -> None:
    """The turn ended between the park and the fold-in. The row is already on
    the record, so the follow-up turn must adopt it rather than write another."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env, members=["dog"])
        env.outcomes["dog"] = ParkOutcome(injected=False, message_id="row-9")

        await _post(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="status?",
        )

        call = env.enqueues[0]
        assert call["existing_user_message_id"] == "row-9"
        assert call["push_user_message"] is False

    asyncio.run(run())


# ── hops ────────────────────────────────────────────────────────────────────


def test_each_agent_answering_another_is_one_hop_further_from_the_human(
    tmp_path, monkeypatch,
) -> None:
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        gid = group["id"]

        human = await _post(
            group_id=gid, sender_kind="user", sender_name="Alexa", content="status?",
        )
        first = await _post(
            group_id=gid, sender_kind="agent", sender_profile="dog",
            sender_name="Rex", content="Chicken, how are the eggs?",
            originated_from_shadow_turn=True,
        )
        second = await _post(
            group_id=gid, sender_kind="agent", sender_profile="chicken",
            sender_name="Coco", content="Twelve today.",
            originated_from_shadow_turn=True,
        )

        assert [human["hop"], first["hop"], second["hop"]] == [0, 1, 2]

    asyncio.run(run())


def test_an_agent_speaking_unprompted_is_one_hop_never_zero(
    tmp_path, monkeypatch,
) -> None:
    """Otherwise a scheduled 08:00 post would give the whole room a fresh
    budget every morning without a human ever being involved."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        row = await _post(
            group_id=group["id"], sender_kind="agent", sender_profile="dog",
            sender_name="Rex", content="Morning, all.",
            originated_from_shadow_turn=True,
        )
        assert row["hop"] == 1

    asyncio.run(run())


def test_at_the_cap_the_room_keeps_delivering_but_starts_no_turns(
    tmp_path, monkeypatch,
) -> None:
    """The agents still SEE the conversation, so their history stays coherent —
    they just stop replying. Written straight to the seat rather than parked:
    parking would hand it to the turn-end flush, which starts exactly the turn
    the cap exists to prevent."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env, settings={"max_agent_hops": 2})
        gid = group["id"]

        await _post(group_id=gid, sender_kind="user", sender_name="Alexa",
                    content="status?")
        await _post(group_id=gid, sender_kind="agent", sender_profile="dog",
                    sender_name="Rex", content="asking around",
                    originated_from_shadow_turn=True)
        env.parks.clear()
        env.enqueues.clear()

        capped = await _post(
            group_id=gid, sender_kind="agent", sender_profile="chicken",
            sender_name="Coco", content="Twelve today.",
            originated_from_shadow_turn=True,
        )

        assert capped["hop"] == 2
        assert capped["metadata"]["quiet"] is True
        assert capped["metadata"]["quiet_reason"] == "hop_limit"
        # Nothing was offered to a turn and nothing was queued.
        assert env.parks == []
        assert env.enqueues == []

        # But it IS in the other members' history, attributed, and flagged.
        rows = await _seat_messages(env, gid, "dog")
        assert [r["content"] for r in rows] == ["Coco (agent): Twelve today."]
        assert rows[0]["role"] == "user"
        assert rows[0]["metadata"]["group"]["quiet"] is True
        assert rows[0]["metadata"]["source"] == "group_chat"
        assert set(capped["delivered_to"]) == {"chicken", "dog", "cat"}

    asyncio.run(run())


def test_a_human_message_starts_the_count_over(tmp_path, monkeypatch) -> None:
    """The cap only ever silences agents talking to each other; a person
    speaking is always answered."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env, settings={"max_agent_hops": 1})
        gid = group["id"]
        await _post(group_id=gid, sender_kind="agent", sender_profile="dog",
                    sender_name="Rex", content="anyone?",
                    originated_from_shadow_turn=True)
        env.enqueues.clear()

        human = await _post(
            group_id=gid, sender_kind="user", sender_name="Alexa", content="I am here",
        )

        assert human["hop"] == 0
        assert "quiet" not in human["metadata"]  # the routing stamp is not a cap
        assert _enqueued_profiles(env) == set(_MEMBERS)

    asyncio.run(run())


def test_a_room_that_hit_the_cap_answers_the_next_person_who_asks(
    tmp_path, monkeypatch,
) -> None:
    """The cap is a pause, not an off switch — and this is the case that makes
    the hop floor the last HUMAN post rather than each agent's own last post.
    Measured per agent, an agent's own last post still sits below the
    higher-hop ones that followed it, so it would inherit them and stay capped
    forever however often it was asked."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env, settings={"max_agent_hops": 2})
        gid = group["id"]

        await _post(group_id=gid, sender_kind="user", sender_name="Alexa",
                    content="status?")
        await _post(group_id=gid, sender_kind="agent", sender_profile="dog",
                    sender_name="Rex", content="asking around",
                    originated_from_shadow_turn=True)
        capped = await _post(group_id=gid, sender_kind="agent", sender_profile="cat",
                             sender_name="Mia", content="and again",
                             originated_from_shadow_turn=True)
        assert capped["metadata"]["quiet_reason"] == "hop_limit"

        await _post(group_id=gid, sender_kind="user", sender_name="Alexa",
                    content="never mind — what time is it?")
        env.enqueues.clear()

        revived = await _post(
            group_id=gid, sender_kind="agent", sender_profile="dog",
            sender_name="Rex", content="It is 14:20.",
            originated_from_shadow_turn=True,
        )

        assert revived["hop"] == 1
        assert "quiet" not in revived["metadata"]
        assert _enqueued_profiles(env) == {"cat", "chicken"}

    asyncio.run(run())


def test_too_many_agent_posts_a_minute_also_goes_quiet(tmp_path, monkeypatch) -> None:
    """The second backstop, for what the hop counter cannot see: many agents
    each answering a different message at the same time."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env, settings={"max_agent_posts_per_minute": 1})
        gid = group["id"]
        await _post(group_id=gid, sender_kind="agent", sender_profile="dog",
                    sender_name="Rex", content="one",
                    originated_from_shadow_turn=True)
        env.enqueues.clear()

        flooded = await _post(
            group_id=gid, sender_kind="agent", sender_profile="cat",
            sender_name="Mia", content="two", originated_from_shadow_turn=True,
        )

        assert flooded["metadata"]["quiet_reason"] == "flood"
        assert env.enqueues == []

    asyncio.run(run())


# ── routing ─────────────────────────────────────────────────────────────────


def test_only_the_agents_the_router_named_start_a_turn(tmp_path, monkeypatch) -> None:
    """The point of the feature: one answer costs one turn, not one per member."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        env.routed["decision"] = RoutingDecision(
            targets={"cat"}, everyone=False, reason="addressed Mia by name",
            model="fake-mini",
        )

        row = await _post(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="Mia, what did we spend?",
        )

        assert _enqueued_profiles(env) == {"cat"}
        # A member that is not woken is never even offered to a running turn:
        # folding it in IS starting a turn on it.
        assert {p["profile"] for p in env.parks} == {"cat"}
        stamp = {
            "targets": ["cat"],
            "everyone": False,
            "nobody": False,
            "reason": "addressed Mia by name",
            "errored": False,
            "model": "fake-mini",
        }
        assert row["metadata"]["routing"] == stamp
        # Written to the row, not just to the copy the caller got back: the room
        # renders the stamp on reload, and the boot sweep reads it to finish an
        # interrupted fan-out without asking the model a second question.
        persisted = await env.storage.get_message(row["id"])
        assert persisted["metadata"]["routing"] == stamp
        # The stamp is a second write, so the rest of the metadata has to survive it.
        assert persisted["metadata"].get("quiet") is None

    asyncio.run(run())


def test_a_member_the_router_passed_over_still_receives_the_message(
    tmp_path, monkeypatch,
) -> None:
    """Routing may only take a TURN away, never the message. An agent whose
    history had holes in it would answer the next question wrongly and have no
    way of knowing why."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        env.routed["decision"] = RoutingDecision(
            targets={"cat"}, everyone=False, reason="Mia's department",
        )

        row = await _post(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="Mia, what did we spend?",
        )

        for passed_over in ("dog", "chicken"):
            rows = await _seat_messages(env, group["id"], passed_over)
            # Delivered, and told whose it was — so a later turn reading this
            # back can see why it was not this agent's to answer.
            assert [r["content"] for r in rows] == [
                "Alexa (user): Mia, what did we spend?\n[to: Mia]"
            ]
            assert rows[0]["role"] == "user"
            assert rows[0]["metadata"]["group"]["routed_away"] is True
            assert rows[0]["metadata"]["group"]["quiet"] is True
        # And the room still records everyone as delivered.
        assert set(row["delivered_to"]) == set(_MEMBERS)

    asyncio.run(run())


def test_the_woken_agent_is_not_marked_routed_away(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        env.routed["decision"] = RoutingDecision(targets={"cat"}, everyone=False)

        await _post(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="Mia?",
        )

        marker = env.enqueues[0]["user_message_metadata"]["group"]
        assert marker["routed_away"] is False
        assert marker["quiet"] is False

    asyncio.run(run())


# ── the routing note ────────────────────────────────────────────────────────
#
# What the router worked out is worth more to the agent than to the reader: a
# member woken by "hello everyone" used to read the same bare line as one merely
# copied in, and decided for itself — wrongly — that a greeting to the room was
# not its business. The note is baked into the text because the model is handed
# ``role`` and ``content`` and nothing else.


def test_a_woken_agent_is_told_the_message_is_for_it(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        env.routed["decision"] = RoutingDecision(targets={"cat"}, everyone=False)

        await _post(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="Mia, what did we spend?",
        )

        assert env.enqueues[0]["query"] == (
            "Alexa (user): Mia, what did we spend?\n[to: you]"
        )
        # The parked copy is the same string, or a seat's history stops being a
        # verbatim prefix of itself and every turn re-pays for the prompt cache.
        assert env.parks[0]["query"] == env.enqueues[0]["query"]

    asyncio.run(run())


def test_a_woken_agent_is_told_who_else_was_asked(tmp_path, monkeypatch) -> None:
    """A bare "[to: you]" on a two-target decision would have each of them
    answer as though it were alone."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        env.routed["decision"] = RoutingDecision(
            targets={"cat", "chicken"}, everyone=False,
        )

        await _post(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="Mia and Nugget, thoughts?",
        )

        by_profile = {c["profile"]: c["query"] for c in env.enqueues}
        assert by_profile["cat"].endswith("\n[to: you, Nugget]")
        assert by_profile["chicken"].endswith("\n[to: you, Mia]")

    asyncio.run(run())


def test_everyone_is_told_it_is_everyone(tmp_path, monkeypatch) -> None:
    """The second half of the report: "hello everyone" woke both agents and one
    declined anyway, because nothing told it the router had decided the greeting
    was for the room."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        env.routed["decision"] = RoutingDecision(reason="a greeting to the room")

        await _post(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="hello everyone",
        )

        assert {c["query"] for c in env.enqueues} == {
            "Alexa (user): hello everyone\n[to: everyone in the room]"
        }

    asyncio.run(run())


def test_a_router_that_could_not_run_annotates_nothing(tmp_path, monkeypatch) -> None:
    """Everyone is woken, but nobody decided that — and a note is a statement
    about who is expected to answer."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        env.routed["decision"] = RoutingDecision(
            reason="routing failed; defaulted to everyone", errored=True,
        )

        await _post(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="status?",
        )

        assert {c["query"] for c in env.enqueues} == {"Alexa (user): status?"}

    asyncio.run(run())


def test_a_capped_post_carries_no_note(tmp_path, monkeypatch) -> None:
    """Nobody started a turn on it at all, so there is nobody to name."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        await _post(
            group_id=group["id"], sender_kind="system", sender_name="Cremind",
            content="Mia hit an internal error and could not answer.",
            deliver_only=True,
        )

        rows = await _seat_messages(env, group["id"], "cat")
        assert [r["content"] for r in rows] == [
            "Cremind (system): Mia hit an internal error and could not answer."
        ]

    asyncio.run(run())


def test_a_room_with_routing_off_reads_exactly_as_it_did_before(
    tmp_path, monkeypatch,
) -> None:
    """No classification, no stamp, no note — byte for byte the old string."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env, settings={ROUTING_SETTING_KEY: False})
        await _post(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="what time is it?",
        )

        assert {c["query"] for c in env.enqueues} == {
            "Alexa (user): what time is it?"
        }

    asyncio.run(run())


def test_the_room_is_told_who_the_router_woke_while_it_watches(
    tmp_path, monkeypatch,
) -> None:
    """The chip has to reach a viewer who was already watching, not only one who
    reloads.

    The row is published from inside the lock, which is a post's whole latency
    win — the room sees it the moment it is recorded rather than a provider round
    trip later — but that is necessarily BEFORE the classification it will be
    stamped with exists. So the stamp needs a frame of its own, or the one thing
    the chip is for (explaining, in the room, why only some agents answered) is
    missing from the only view where the silence is actually visible.

    Serialised as it is dequeued, exactly as the SSE endpoint does: the row is a
    live object the stamp mutates, so a test that read the queue afterwards would
    see a stamp that never went out on the wire.
    """
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        env.routed["decision"] = RoutingDecision(
            targets={"cat"}, everyone=False, reason="addressed Mia by name",
            model="fake-mini",
        )
        bus = get_group_stream_bus()
        queue, _ = await bus.subscribe(group["id"])

        seen: list[dict] = []

        async def viewer():
            while True:
                seen.append(json.loads(json.dumps(await queue.get())))

        watching = asyncio.create_task(viewer())
        try:
            row = await _post(
                group_id=group["id"], sender_kind="user", sender_name="Alexa",
                content="Mia, what did we spend?",
            )
            await asyncio.sleep(0)
        finally:
            watching.cancel()

        types = [f["type"] for f in seen]
        assert "message" in types, types
        routing_frames = [f for f in seen if f["type"] == "message_routing"]
        assert len(routing_frames) == 1, types
        assert routing_frames[0]["data"] == {
            "message_id": row["id"],
            "routing": row["metadata"]["routing"],
        }
        # After the message it decorates — a viewer cannot stamp a row it has
        # not been given yet.
        assert types.index("message") < types.index("message_routing")

        # The frame above is live-only, which is only safe because the REPLAY
        # copy needs no such help: the ring holds the row object itself, so the
        # stamp written onto it lands in the ring too and a reconnecting client
        # gets the chip from the message frame alone. Publishing a snapshot
        # instead would silently cost that client the chip, so it is pinned here
        # rather than left as an implementation detail two modules apart.
        ringed = [f for f in bus.snapshot(group["id"]) if f["type"] == "message"]
        assert [f["data"]["metadata"]["routing"] for f in ringed] == [
            row["metadata"]["routing"],
        ]
        assert not [f for f in bus.snapshot(group["id"]) if f["type"] == "message_routing"]

        await bus.unsubscribe(group["id"], queue)
        bus.discard(group["id"])

    asyncio.run(run())


def test_a_post_that_woke_nobody_broadcasts_no_routing_frame(
    tmp_path, monkeypatch,
) -> None:
    """A capped post is never classified, so there is nothing to broadcast — and
    a chip on the very row where nobody answered would read as the opposite of
    the truth."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        bus = get_group_stream_bus()
        queue, _ = await bus.subscribe(group["id"])

        seen: list[dict] = []

        async def viewer():
            while True:
                seen.append(json.loads(json.dumps(await queue.get())))

        watching = asyncio.create_task(viewer())
        try:
            await _post(
                group_id=group["id"], sender_kind="system", sender_name="Cremind",
                content="Rex hit an internal error.", deliver_only=True,
            )
            await asyncio.sleep(0)
        finally:
            watching.cancel()

        assert [f["type"] for f in seen] == ["message"]
        assert env.routes == []

        await bus.unsubscribe(group["id"], queue)
        bus.discard(group["id"])

    asyncio.run(run())


def test_everyone_is_the_same_room_as_before_routing(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        env.routed["decision"] = RoutingDecision(reason="aimed at the whole room")

        row = await _post(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="status, everyone?",
        )

        assert _enqueued_profiles(env) == set(_MEMBERS)
        assert row["metadata"]["routing"]["everyone"] is True
        assert row["metadata"]["routing"]["targets"] == []

    asyncio.run(run())


def test_a_router_that_blows_up_wakes_every_member(tmp_path, monkeypatch) -> None:
    """The one direction the classifier is allowed to be wrong in. A room that
    stopped answering because a provider was down would be a far worse failure
    than one that woke everybody."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        env.routed["decision"] = RuntimeError("provider exploded")

        row = await _post(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="status?",
        )

        assert _enqueued_profiles(env) == set(_MEMBERS)
        assert row["metadata"]["routing"]["errored"] is True
        assert row["metadata"]["routing"]["everyone"] is True

    asyncio.run(run())


def test_a_missing_agent_does_not_stop_the_post(tmp_path, monkeypatch) -> None:
    """Resolving the routing model is best-effort: the classifier is handed
    ``llm=None`` and answers "everyone" for itself."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)

        def boom():
            raise RuntimeError("no agent yet")

        monkeypatch.setattr(event_runner, "get_cremind_agent", boom)

        await _post(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="status?",
        )

        assert env.routes[0]["llm"] is None
        assert _enqueued_profiles(env) == set(_MEMBERS)

    asyncio.run(run())


def test_a_capped_post_is_never_classified_and_carries_no_routing_stamp(
    tmp_path, monkeypatch,
) -> None:
    """A cap silences everyone, so a decision could only ever be discarded.

    Two reasons not to ask for one anyway. The stamp: the chip renders it as
    "only Mia started a turn; everyone else still received the message" on a row
    where NOBODY started a turn — the exact rows a reader is looking at to work
    out why nobody answered. And the call: a hop-capped or flooding room is
    posting fast, so paying a classification (and up to ten seconds) per post
    would spend the most during the failure the caps exist to contain.
    """
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env, settings={"max_agent_hops": 1})
        gid = group["id"]
        await _post(group_id=gid, sender_kind="user", sender_name="Alexa",
                    content="status?")
        env.enqueues.clear()
        env.parks.clear()
        env.routes.clear()
        env.routed["decision"] = RoutingDecision(targets={"cat"}, everyone=False)

        capped = await _post(
            group_id=gid, sender_kind="agent", sender_profile="dog",
            sender_name="Rex", content="Mia, your turn", hop=1,
            originated_from_shadow_turn=True,
        )

        assert capped["metadata"]["quiet_reason"] == "hop_limit"
        assert env.routes == []
        assert "routing" not in capped["metadata"]
        persisted = await env.storage.get_message(capped["id"])
        assert "routing" not in persisted["metadata"]
        # Routing may only take turns away, never hand one back to a room that
        # is already looping: not even the member it would have named answers.
        assert env.enqueues == []
        assert env.parks == []
        rows = await _seat_messages(env, gid, "cat")
        # Capped, but not because of the router — the two reasons stay apart.
        assert rows[-1]["metadata"]["group"]["quiet"] is True
        assert rows[-1]["metadata"]["group"]["routed_away"] is False

    asyncio.run(run())


def test_routing_turned_off_never_calls_the_classifier(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env, settings={ROUTING_SETTING_KEY: False})
        env.routed["decision"] = RoutingDecision(targets={"cat"}, everyone=False)

        row = await _post(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="status?",
        )

        assert env.routes == []
        assert _enqueued_profiles(env) == set(_MEMBERS)
        assert "routing" not in row["metadata"]

    asyncio.run(run())


def test_a_system_notice_is_never_routed(tmp_path, monkeypatch) -> None:
    """Nobody is meant to answer it, so there is nothing to narrow and no reason
    to spend a call finding that out."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        row = await _post(
            group_id=group["id"], sender_kind="system", sender_name="Cremind",
            content="Mia hit an internal error and could not answer.",
            deliver_only=True,
        )

        assert env.routes == []
        assert "routing" not in row["metadata"]

    asyncio.run(run())


def test_one_possible_answerer_is_not_worth_classifying(tmp_path, monkeypatch) -> None:
    """With one member there is one candidate, and for a PERSON's post the only
    answers are "wake it" and "wake it". Nothing to narrow, so nothing is spent.

    (A person's post in a two-MEMBER room still has two candidates — the sender
    is not one of them — so that room is classified as usual.)"""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env, members=["dog"])
        row = await _post(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="anyone about?",
        )

        assert env.routes == []
        assert "routing" not in (row["metadata"] or {})
        assert _enqueued_profiles(env) == {"dog"}

    asyncio.run(run())


def test_a_seat_reply_is_classified_even_with_one_candidate(
    tmp_path, monkeypatch,
) -> None:
    """The same room, the other direction. A reply has a second possible answer
    — "nobody" — so the call buys something a person's post could not: the
    turn every other member would otherwise spend to say ``[silent]``.

    This is the two-member room from the bug report: Cremind answers the person,
    and Andy is woken to read it, think, and decline."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env, members=["dog", "cat"])
        env.routed["decision"] = RoutingDecision(
            targets=set(), everyone=False, nobody=True,
            reason="Rex answered Alexa; nothing asked of Mia",
        )

        row = await _post(
            group_id=group["id"], sender_kind="agent", sender_profile="dog",
            sender_name="Rex", content="It is 14:20.",
            originated_from_shadow_turn=True,
        )

        assert len(env.routes) == 1
        assert row["metadata"]["routing"]["nobody"] is True
        assert env.enqueues == []
        assert env.parks == []
        # Still delivered — routing only ever takes the turn.
        rows = await _seat_messages(env, group["id"], "cat")
        assert [r["content"] for r in rows] == ["Rex (agent): It is 14:20."]
        assert rows[0]["metadata"]["group"]["routed_away"] is True

    asyncio.run(run())


def test_a_tool_post_is_never_routed_to_nobody(tmp_path, monkeypatch) -> None:
    """``send_group_message`` and ``as_profile`` posts are ``agent`` rows too,
    but they are somebody deliberately addressing the room — the tool's own
    contract is that a reply comes back. Only a seat's own finished turn may be
    routed to nobody, so with one candidate these are not classified at all."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env, members=["dog", "cat"])
        await _post(
            group_id=group["id"], sender_kind="agent", sender_profile="dog",
            sender_name="Rex", content="status, Mia?",
            originated_from_shadow_turn=False,
        )

        assert env.routes == []
        assert _enqueued_profiles(env) == {"cat"}

    asyncio.run(run())


def test_only_a_seats_own_turn_reaches_the_router_as_nobody_eligible(
    tmp_path, monkeypatch,
) -> None:
    """The flag itself, in a room big enough to be classified either way — so
    this pins the threading rather than the candidate gate. The row cannot tell
    these apart: all three are ``agent`` rows."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)  # three members: always classified

        await _post(
            group_id=group["id"], sender_kind="agent", sender_profile="dog",
            sender_name="Rex", content="It is 14:20.",
            originated_from_shadow_turn=True,
        )
        await _post(
            group_id=group["id"], sender_kind="agent", sender_profile="dog",
            sender_name="Rex", content="status, everyone?",
            originated_from_shadow_turn=False,      # the tool / --as-profile shape
        )
        await _post(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="anyone about?",
        )

        assert [r.get("nobody_eligible") for r in env.routes] == [True, False, False]

    asyncio.run(run())


def test_the_classification_runs_outside_the_group_lock(tmp_path, monkeypatch) -> None:
    """One post's provider round-trip must not hold every other post in the room
    behind it — the lock only exists to keep ``ordering`` and the hop count
    derivable from the timeline, and both are settled before the router runs."""
    env = _env(tmp_path, monkeypatch)
    held: list[bool] = []

    async def run():
        group = await _make_group(env)
        original = routing_module.route_message

        async def watching(**kwargs):
            held.append(fanout._lock_for(group["id"]).locked())
            return await original(**kwargs)

        monkeypatch.setattr(routing_module, "route_message", watching)

        await _post(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="status?",
        )

        assert held == [False]

    asyncio.run(run())


def test_the_router_sees_the_post_it_is_classifying_and_the_room_before_it(
    tmp_path, monkeypatch,
) -> None:
    """The row is written first, so the slice read back for the classification
    holds the post being classified AND everything said ahead of it.

    Read before the insert — which is what running in front of the lock forced —
    a burst of messages was each classified against a history missing the ones
    just ahead of it, and a router shown half a conversation does not hedge: it
    answers confidently and narrowly, and that answer is then applied.
    """
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        gid = group["id"]
        await _post(group_id=gid, sender_kind="user", sender_name="Alexa",
                    content="morning all")
        env.routes.clear()
        env.routing_profiles.clear()

        row = await _post(group_id=gid, sender_kind="user", sender_name="Alexa",
                          content="Mia, what did we spend?")

        call = env.routes[0]
        assert call["row"]["content"] == "Mia, what did we spend?"
        assert call["row"]["sender_name"] == "Alexa"
        assert call["row"]["sender_kind"] == "user"
        # The real row, so the router can tell it apart from the copy of itself
        # that now sits at the end of the history.
        assert call["row"]["id"] == row["id"]
        assert [r["content"] for r in call["recent_rows"]] == [
            "morning all", "Mia, what did we spend?",
        ]
        assert call["group"]["id"] == gid
        assert call["llm"] is env.llm
        # The room's creator pays for the call and picks the model.
        assert env.routing_profiles == ["admin"]

    asyncio.run(run())


def test_two_posts_keep_arrival_order_however_long_each_takes_to_classify(
    tmp_path, monkeypatch,
) -> None:
    """A slow classification must not let a later message overtake an earlier one.

    Group inbound is concurrent on purpose — every channel adapter starts a task
    per update — so several posts sit in ``post_message`` at once, each one a
    provider round trip long. Classified before the insert, "book us a table"
    (4s) and "actually make it 8pm" (0.4s, sent a second later) took the lock in
    the wrong order: the room, and every seat history, showed the correction
    above the thing it corrected, and each agent answered it first.

    Both halves are pinned here — ``ordering`` from the insert, and the order
    the seats are handed the messages, which is decided after the lock is
    already released.
    """
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        gid = group["id"]

        classifying = asyncio.Event()  # the first post has reached the router
        answer = asyncio.Event()  # ...and may now be classified

        async def slow_for_the_first(**kwargs):
            env.routes.append(kwargs)
            if kwargs["row"]["content"] == "book us a table":
                classifying.set()
                await answer.wait()
            return RoutingDecision(reason="everyone")

        monkeypatch.setattr(routing_module, "route_message", slow_for_the_first)

        first = asyncio.create_task(_post(
            group_id=gid, sender_kind="user", sender_name="Alexa",
            content="book us a table",
        ))
        # Only set once the first post is in the timeline and being classified,
        # so the second one genuinely arrives second.
        await classifying.wait()
        second = asyncio.create_task(_post(
            group_id=gid, sender_kind="user", sender_name="Alexa",
            content="actually make it 8pm",
        ))
        # Ample time for the second post to be recorded and classified — its own
        # classification returns at once — and to overtake if nothing held it.
        await asyncio.sleep(0.2)
        assert env.enqueues == []

        answer.set()
        rows = await asyncio.gather(first, second)

        assert [r["ordering"] for r in rows] == [0, 1]
        timeline = await env.storage.list_messages(gid)
        assert [r["content"] for r in timeline] == [
            "book us a table", "actually make it 8pm",
        ]
        # And each seat was handed them in that order, not in the order the
        # classifications happened to come back.
        assert [c["query"] for c in env.enqueues if c["profile"] == "dog"] == [
            "Alexa (user): book us a table\n[to: everyone in the room]",
            "Alexa (user): actually make it 8pm\n[to: everyone in the room]",
        ]

    asyncio.run(run())


def test_the_routing_call_is_billed_to_the_room(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    import app.storage as storage_module

    recorded: list[dict] = []

    class _Usage:
        async def add_usage_records(self, **kwargs):
            recorded.append(kwargs)

    async def run():
        group = await _make_group(env)
        monkeypatch.setattr(storage_module, "get_usage_storage", lambda: _Usage())
        env.routed["decision"] = RoutingDecision(
            targets={"cat"}, everyone=False, reason="Mia",
            tokens={"input_tokens": 120, "output_tokens": 8},
        )

        await _post(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="Mia?",
        )

        assert len(recorded) == 1
        assert recorded[0]["conversation_id"] is None  # it belongs to the room
        assert recorded[0]["profile"] == "admin"
        record = recorded[0]["records"][0]
        assert record["source_kind"] == "group_routing"
        assert record["input_tokens"] == 120
        assert record["model"] == "fake-mini"

    asyncio.run(run())


def test_a_broken_usage_store_does_not_lose_the_post(tmp_path, monkeypatch) -> None:
    """Accounting is the last thing that should be able to stop a room talking."""
    env = _env(tmp_path, monkeypatch)
    import app.storage as storage_module

    async def run():
        group = await _make_group(env)

        def boom():
            raise RuntimeError("usage storage down")

        monkeypatch.setattr(storage_module, "get_usage_storage", boom)
        env.routed["decision"] = RoutingDecision(
            tokens={"input_tokens": 5, "output_tokens": 1},
        )

        row = await _post(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="status?",
        )

        assert row is not None
        assert _enqueued_profiles(env) == set(_MEMBERS)

    asyncio.run(run())


# ── posts made from outside a seat ──────────────────────────────────────────


def test_a_tool_post_is_also_recorded_in_the_sender_s_own_seat(
    tmp_path, monkeypatch,
) -> None:
    """``send_group_message`` speaks from an ordinary chat or an event run, so
    the words never passed through the agent's seat — without this it would
    later read replies to something it has no memory of saying."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        row = await _post(
            group_id=group["id"], sender_kind="agent", sender_profile="dog",
            sender_name="Rex", content="Cat, today's status please?", hop=1,
            originated_from_shadow_turn=False,
        )

        own = await _seat_messages(env, group["id"], "dog")
        assert [r["role"] for r in own] == ["agent"]
        assert own[0]["content"] == "Cat, today's status please?"
        assert own[0]["metadata"]["group"]["kind"] == "materialised"
        assert own[0]["metadata"]["group"]["posted_message_ids"] == [row["id"]]
        # The peers are asked as usual.
        assert _enqueued_profiles(env) == {"cat", "chicken"}

    asyncio.run(run())


def test_a_seat_turn_does_not_duplicate_itself_into_its_own_history(
    tmp_path, monkeypatch,
) -> None:
    """It is already the agent's own message in that conversation."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        await _post(
            group_id=group["id"], sender_kind="agent", sender_profile="dog",
            sender_name="Rex", content="It is 14:20.",
            originated_from_shadow_turn=True,
        )
        assert await _seat_messages(env, group["id"], "dog") == []

    asyncio.run(run())


# ── the refusals ────────────────────────────────────────────────────────────


def test_an_agent_that_is_not_a_member_cannot_post(tmp_path, monkeypatch) -> None:
    """Raised rather than returned: the tool has to tell the agent why, and a
    silent no-op would read as a delivered message."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        with pytest.raises(ValueError, match="not_a_member"):
            await fanout.post_message(
                group_id=group["id"], sender_kind="agent", sender_profile="duck",
                sender_name="Quack", content="hello?",
            )
        assert await env.storage.list_messages(group["id"]) == []

    asyncio.run(run())


def test_nothing_to_say_and_nowhere_to_say_it(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        assert await _post(group_id=group["id"], sender_kind="user",
                           sender_name="Alexa", content="   ") is None
        assert await _post(group_id="", sender_kind="user", sender_name="Alexa",
                           content="hi") is None
        assert await _post(group_id="ghost", sender_kind="user",
                           sender_name="Alexa", content="hi") is None
        assert env.enqueues == []

    asyncio.run(run())


def test_deliver_only_forces_the_quiet_path(tmp_path, monkeypatch) -> None:
    """System notices: everyone should see them and nobody should answer them."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        row = await _post(
            group_id=group["id"], sender_kind="system", sender_name="Cremind",
            content="Mia hit an internal error and could not answer.",
            deliver_only=True,
        )

        assert row["hop"] == 0
        assert row["metadata"]["quiet"] is True
        assert row["metadata"]["quiet_reason"] == "system"
        assert env.parks == []
        assert env.enqueues == []
        rows = await _seat_messages(env, group["id"], "dog")
        assert rows[0]["content"].startswith("Cremind (system): ")

    asyncio.run(run())


def test_one_member_failing_does_not_stop_the_others(tmp_path, monkeypatch) -> None:
    """Delivery is per member and recorded as it goes, so the boot sweep can
    finish the rest rather than delivering to everybody twice."""
    env = _env(tmp_path, monkeypatch)

    async def run():
        group = await _make_group(env)
        original = fanout.ensure_shadow_conversation

        async def flaky(conv_storage, profile, group_dict):
            if profile == "cat":
                raise RuntimeError("seat unavailable")
            return await original(conv_storage, profile, group_dict)

        monkeypatch.setattr(fanout, "ensure_shadow_conversation", flaky)

        row = await _post(
            group_id=group["id"], sender_kind="user", sender_name="Alexa",
            content="status?",
        )

        assert _enqueued_profiles(env) == {"dog", "chicken"}
        assert "cat" not in row["delivered_to"]
        persisted = await env.storage.get_message(row["id"])
        assert set(persisted["delivered_to"]) == {"dog", "chicken"}

    asyncio.run(run())


def test_the_note_is_derived_from_the_row_alone(tmp_path, monkeypatch) -> None:
    """``_routing_note`` is what BOTH the live fan-out and the boot sweep call,
    and the sweep re-derives it from the stored row rather than carrying it
    over — so the two agree only because they run the same function on the same
    inputs. Pinned here directly, since the sweep's own tests fake delivery.

    Also the corrupt-stamp case: read leniently here and strictly in the sweep,
    the same row named the target letter by letter, ``[to: c, a, t]``.
    """
    _env(tmp_path, monkeypatch)  # for the agent-name patch
    note = fanout._routing_note

    woken = {"metadata": {"routing": {"targets": ["cat"], "everyone": False}}}
    assert note(woken, "cat", capped=False, routed_away=False) == "[to: you]"
    assert note(woken, "dog", capped=True, routed_away=True) == "[to: Mia]"

    pair = {"metadata": {"routing": {"targets": ["cat", "chicken"], "everyone": False}}}
    assert note(pair, "cat", capped=False, routed_away=False) == "[to: you, Nugget]"

    everyone = {"metadata": {"routing": {"targets": [], "everyone": True}}}
    assert note(everyone, "cat", capped=False, routed_away=False) == (
        "[to: everyone in the room]"
    )

    # Nothing was decided, so there is nobody to name.
    for row in (
        {"metadata": {}},
        {"metadata": {"routing": {"targets": [], "nobody": True, "everyone": False}}},
        {"metadata": {"routing": {"targets": [], "everyone": True, "errored": True}}},
        {"metadata": {"quiet": True, "routing": {"targets": ["cat"], "everyone": False}}},
        {"metadata": {"routing": {"targets": "cat", "everyone": False}}},  # corrupt
    ):
        assert note(row, "dog", capped=False, routed_away=False) == ""
        assert note(row, "dog", capped=True, routed_away=True) == ""
