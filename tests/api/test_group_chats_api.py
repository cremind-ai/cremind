"""API: the group-chat endpoints.

A group is a system-wide resource with per-profile membership, which makes the
auth matrix the thing worth pinning: the admin owns the room's shape, members
can look in and speak, and everybody else gets a 403. "Owns" stops at the shape,
though — a member reads the whole group dict, settings included, because every
key left in it describes how the ROOM behaves rather than who is wired into it.
What is still per-viewer is a running turn: one member never sees another
member's steps. Alongside that, three behaviours that are easy to regress
silently — creating a group must leave every member a seat, changing the
membership must create and destroy those seats, and the trace endpoint must
never hand one profile another profile's raw provider transcript.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402
from sqlalchemy import text  # noqa: E402
from starlette.datastructures import QueryParams  # noqa: E402

import app.config.settings as settings_module  # noqa: E402
import app.events.stream_bus as stream_bus_module  # noqa: E402
import app.groups.bus as group_bus  # noqa: E402
import app.groups.fanout as group_fanout  # noqa: E402
import app.groups.index as group_index  # noqa: E402
import app.storage.group_chat_storage as gcs_module  # noqa: E402
from app.api.group_chats import get_group_chat_routes  # noqa: E402
from app.events.stream_bus import ConversationStreamBus  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402
from app.storage.group_chat_storage import GroupChatStorage  # noqa: E402

_TABLES = (
    "profiles",
    "channels",
    "channel_senders",
    "conversations",
    "messages",
    "group_chats",
    "group_chat_members",
    "group_chat_messages",
)

_PROFILES = ("admin", "dog", "cat", "chicken")


class _Req:
    def __init__(
        self, username="admin", path_params=None, body=None, method="GET", query="",
    ):
        self.user = SimpleNamespace(is_authenticated=True, username=username)
        self.path_params = path_params or {}
        self.method = method
        self.query_params = QueryParams(query)
        self._body = body or {}

    async def json(self):
        return self._body

    async def is_disconnected(self):
        return False


def _body(resp) -> dict:
    return json.loads(resp.body)


def _setup(tmp_path: Path, monkeypatch):
    provider = SqliteDatabaseProvider(str(tmp_path / "groups.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        for i, name in enumerate(_PROFILES):
            c.execute(text(
                "INSERT INTO profiles (id, name, created_at, updated_at) "
                f"VALUES ('p{i}','{name}',0,0)"
            ))

    cs = ConversationStorage(provider)
    cs._initialized = True
    # The API reaches the group tables through the process-wide singleton.
    monkeypatch.setattr(gcs_module, "_instance", GroupChatStorage(provider))
    # Fresh in-memory index/bus per test, or one test's room leaks into the next.
    monkeypatch.setattr(group_index, "_instance", None)
    monkeypatch.setattr(group_bus, "_instance", None)
    return cs, gcs_module._instance


def _handler(cs, path: str, method: str) -> Callable:
    for route in get_group_chat_routes(cs):
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


def _call(cs, path, method, *, username="admin", params=None, body=None, query=""):
    handler = _handler(cs, path, method)
    return handler(_Req(
        username=username, path_params=params, body=body, method=method, query=query,
    ))


_GROUPS = "/api/group-chats"
_DETAIL = "/api/group-chats/{group_id}"
_MESSAGES = "/api/group-chats/{group_id}/messages"
_TRACE = "/api/group-chats/{group_id}/messages/{message_id}/trace"
_STREAM = "/api/group-chats/{group_id}/stream"


async def _create_group(cs, *, members=("dog", "cat"), settings=None):
    resp = await _call(cs, _GROUPS, "POST", body={
        "name": "Ops",
        "members": list(members),
        "settings": settings or {"web_sender_name": "Ops Lead"},
    })
    assert resp.status_code == 201, _body(resp)
    return _body(resp)["group"]


# ── auth matrix ────────────────────────────────────────────────────────────


def test_a_non_admin_cannot_create_a_group(tmp_path, monkeypatch):
    cs, _gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        return await _call(
            cs, _GROUPS, "POST", username="dog", body={"name": "Ops"},
        )

    resp = asyncio.run(_run())
    assert resp.status_code == 403


def test_a_member_sees_the_room_and_an_outsider_does_not(tmp_path, monkeypatch):
    cs, _gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _create_group(cs)
        params = {"group_id": group["id"]}
        member = await _call(cs, _DETAIL, "GET", username="dog", params=params)
        outsider = await _call(cs, _DETAIL, "GET", username="chicken", params=params)
        listed = await _call(cs, _GROUPS, "GET", username="chicken")
        return member, outsider, listed

    member, outsider, listed = asyncio.run(_run())
    assert member.status_code == 200
    assert _body(member)["group"]["members"] == ["cat", "dog"]
    # Nobody is mid-turn, so the room reports an empty thinking list.
    assert _body(member)["thinking"] == []
    assert outsider.status_code == 403
    assert _body(listed)["groups"] == []


def test_a_member_reads_the_whole_settings_blob(tmp_path, monkeypatch):
    """A member's copy used to be trimmed, because the blob carried the room's
    wiring — the bound platform chats and the operators' platform sender ids.
    Nothing left in it describes who is wired in: the hop cap, the post rate, the
    web sender's name and the routing switch all describe how the room BEHAVES,
    which is exactly what a member needs to read to make sense of it. So the two
    views are now the same view, and only WRITING stays the admin's.
    """
    cs, _gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _create_group(cs, settings={
            "web_sender_name": "Ops Lead", "max_agent_hops": 3,
        })
        params = {"group_id": group["id"]}
        return (
            await _call(cs, _DETAIL, "GET", username="dog", params=params),
            await _call(cs, _DETAIL, "GET", params=params),
            await _call(cs, _GROUPS, "GET", username="dog"),
        )

    member, admin, listed = asyncio.run(_run())
    assert member.status_code == 200
    member_settings = _body(member)["group"]["settings"]
    assert member_settings == _body(admin)["group"]["settings"]
    assert member_settings["web_sender_name"] == "Ops Lead"
    assert member_settings["max_agent_hops"] == 3
    # Same dict, second door: the room list is the first thing a member loads,
    # so a filter that only covered the detail route would still show there.
    assert _body(listed)["groups"][0]["settings"] == member_settings


def test_the_group_updated_frame_reaches_a_member_whole(tmp_path, monkeypatch):
    """Third door, and the one with no route of its own. The bus publishes one
    frame to the whole room and the endpoint used to trim each viewer's copy on
    the way out; it must now hand the member the same frame as the admin."""
    cs, gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        created = await _create_group(cs, settings={"web_sender_name": "Ops Lead"})
        gid = created["id"]
        group = await gcs.get_group(gid)
        bus = group_bus.get_group_stream_bus()
        bus.discard(gid)  # the create frame is not what is under test
        as_dog, _ = await _open_stream(cs, gid, "dog")
        as_admin, _ = await _open_stream(cs, gid, "admin")

        await bus.publish(gid, "group_updated", group)

        seen = (await _next_frame(as_dog), await _next_frame(as_admin))
        await as_dog.aclose()
        await as_admin.aclose()
        return seen

    dog_frame, admin_frame = asyncio.run(_run())
    assert dog_frame["type"] == "group_updated"
    assert dog_frame["data"] == admin_frame["data"]
    assert dog_frame["data"]["settings"]["web_sender_name"] == "Ops Lead"


def test_only_the_admin_can_change_a_room(tmp_path, monkeypatch):
    cs, _gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _create_group(cs)
        params = {"group_id": group["id"]}
        patched = await _call(
            cs, _DETAIL, "PATCH", username="dog", params=params,
            body={"name": "Mine"},
        )
        deleted = await _call(cs, _DETAIL, "DELETE", username="dog", params=params)
        return patched, deleted

    patched, deleted = asyncio.run(_run())
    assert patched.status_code == 403
    assert deleted.status_code == 403


# ── seats ──────────────────────────────────────────────────────────────────


def test_creating_a_group_seats_every_member(tmp_path, monkeypatch):
    cs, _gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _create_group(cs)
        seats = []
        for profile in ("dog", "cat"):
            seats.append(await cs.get_conversation_by_context(
                profile, f"group:{group['id']}:{profile}",
            ))
        return group, seats

    group, seats = asyncio.run(_run())
    assert group["settings"]["web_sender_name"] == "Ops Lead"
    assert all(seat is not None for seat in seats)
    assert {seat["kind"] for seat in seats} == {"group_chat"}
    # The seat pointer is recorded, so delivery never has to guess.
    assert {row["shadow_conversation_id"] for row in group["member_rows"]} == {
        seat["id"] for seat in seats
    }


def test_admin_can_be_a_member(tmp_path, monkeypatch):
    """The admin profile is a profile like any other: it runs an agent, and a
    room it sits in needs a seat for it. Leaving admin out was only ever a
    filter in the UI's member picker — the backend has always allowed it, and
    this pins that so a future filter is not mistaken for a rule."""
    cs, _gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _create_group(cs, members=("admin", "dog"))
        context = f"group:{group['id']}:admin"
        seat = await cs.get_conversation_by_context("admin", context)
        listed = await _call(cs, _GROUPS, "GET", username="admin")
        dropped = await _call(
            cs, _DETAIL, "PATCH", params={"group_id": group["id"]},
            body={"members": ["dog"]},
        )
        return (
            group, seat, listed, dropped,
            await cs.get_conversation_by_context("admin", context),
        )

    group, seat, listed, dropped, seat_after = asyncio.run(_run())
    assert group["members"] == ["admin", "dog"]
    assert seat is not None and seat["kind"] == "group_chat"
    assert group["id"] in {g["id"] for g in _body(listed)["groups"]}
    assert dropped.status_code == 200
    assert _body(dropped)["group"]["members"] == ["dog"]
    assert seat_after is None


def test_an_unknown_profile_cannot_be_seated(tmp_path, monkeypatch):
    cs, _gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        return await _call(cs, _GROUPS, "POST", body={
            "name": "Ops", "members": ["dog", "ghost"],
        })

    resp = asyncio.run(_run())
    assert resp.status_code == 400
    assert "ghost" in _body(resp)["message"]


def test_patching_the_membership_creates_and_destroys_seats(tmp_path, monkeypatch):
    cs, _gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _create_group(cs)
        cat_seat = await cs.get_conversation_by_context(
            "cat", f"group:{group['id']}:cat",
        )
        resp = await _call(
            cs, _DETAIL, "PATCH", params={"group_id": group["id"]},
            body={"members": ["dog", "chicken"]},
        )
        chicken_seat = await cs.get_conversation_by_context(
            "chicken", f"group:{group['id']}:chicken",
        )
        return resp, cat_seat, chicken_seat, await cs.get_conversation(cat_seat["id"])

    resp, cat_seat, chicken_seat, cat_seat_after = asyncio.run(_run())
    assert resp.status_code == 200
    assert _body(resp)["group"]["members"] == ["chicken", "dog"]
    assert chicken_seat is not None and chicken_seat["kind"] == "group_chat"
    # The member who left keeps no seat behind to receive messages in.
    assert cat_seat is not None
    assert cat_seat_after is None


def test_settings_are_replaced_whole_and_validated(tmp_path, monkeypatch):
    cs, _gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _create_group(cs)
        params = {"group_id": group["id"]}
        bad = await _call(cs, _DETAIL, "PATCH", params=params, body={
            "settings": {"max_agent_hops": "x"},
        })
        good = await _call(cs, _DETAIL, "PATCH", params=params, body={
            "settings": {"max_agent_hops": 2},
        })
        return bad, good

    bad, good = asyncio.run(_run())
    assert bad.status_code == 400
    settings = _body(good)["group"]["settings"]
    assert settings["max_agent_hops"] == 2
    # Replaced, not merged: the name the create call set is gone.
    assert settings["web_sender_name"] == "Operator"


def test_the_routing_switch_round_trips(tmp_path, monkeypatch):
    """The knob is validated by ``normalize_settings`` like the rest of the blob,
    so what the settings page needs from the API is only that it survives a POST
    and a PATCH — including being turned off, which a truthiness bug would eat."""
    from app.groups.constants import ROUTING_SETTING_KEY

    cs, _gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        created = await _create_group(cs)
        params = {"group_id": created["id"]}
        off = await _call(cs, _DETAIL, "PATCH", params=params, body={
            "settings": {ROUTING_SETTING_KEY: False},
        })
        back_on = await _call(cs, _DETAIL, "PATCH", params=params, body={
            "settings": {ROUTING_SETTING_KEY: True},
        })
        return created, off, back_on

    created, off, back_on = asyncio.run(_run())
    # A room nobody configured gets it on.
    assert created["settings"][ROUTING_SETTING_KEY] is True
    assert _body(off)["group"]["settings"][ROUTING_SETTING_KEY] is False
    assert _body(back_on)["group"]["settings"][ROUTING_SETTING_KEY] is True


def test_deleting_a_group_removes_its_seats(tmp_path, monkeypatch):
    cs, gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _create_group(cs)
        seat = await cs.get_conversation_by_context("dog", f"group:{group['id']}:dog")
        resp = await _call(cs, _DETAIL, "DELETE", params={"group_id": group["id"]})
        return (
            resp,
            await cs.get_conversation(seat["id"]),
            await gcs.get_group(group["id"]),
        )

    resp, seat_after, group_after = asyncio.run(_run())
    assert resp.status_code == 200
    assert _body(resp)["deleted"] is True
    assert seat_after is None
    assert group_after is None


# ── posting ────────────────────────────────────────────────────────────────


def _record_posts(monkeypatch, *, result="row"):
    """Replace the fan-out with a recorder. Returns the list of call kwargs."""
    calls: list = []

    async def fake_post(**kwargs):
        calls.append(kwargs)
        if result is None:
            return None
        return {"id": "m1", "ordering": 0, **kwargs}

    monkeypatch.setattr(group_fanout, "post_message", fake_post)
    return calls


def test_a_web_post_carries_the_operator_identity(tmp_path, monkeypatch):
    cs, _gcs = _setup(tmp_path, monkeypatch)
    calls = _record_posts(monkeypatch)

    async def _run():
        group = await _create_group(cs)
        return await _call(
            cs, _MESSAGES, "POST", username="dog",
            params={"group_id": group["id"]}, body={"text": "  status?  "},
        )

    resp = asyncio.run(_run())
    assert resp.status_code == 202
    call = calls[0]
    assert call["sender_kind"] == "user"
    assert call["sender_name"] == "Ops Lead"
    # Who typed it, on which front-end — the group's user accounts map platform
    # identities, and the web one is the profile that was signed in.
    assert call["sender_identity"] == {"channel_type": "web", "sender_id": "dog"}
    assert call["hop"] == 0
    assert call["content"] == "status?"


def test_an_outsider_cannot_post(tmp_path, monkeypatch):
    cs, _gcs = _setup(tmp_path, monkeypatch)
    calls = _record_posts(monkeypatch)

    async def _run():
        group = await _create_group(cs)
        return await _call(
            cs, _MESSAGES, "POST", username="chicken",
            params={"group_id": group["id"]}, body={"text": "hi"},
        )

    assert asyncio.run(_run()).status_code == 403
    assert calls == []


def test_an_empty_post_is_refused(tmp_path, monkeypatch):
    cs, _gcs = _setup(tmp_path, monkeypatch)
    calls = _record_posts(monkeypatch)

    async def _run():
        group = await _create_group(cs)
        return await _call(
            cs, _MESSAGES, "POST", params={"group_id": group["id"]},
            body={"text": "   "},
        )

    assert asyncio.run(_run()).status_code == 400
    assert calls == []


def test_a_duplicate_post_is_a_conflict(tmp_path, monkeypatch):
    cs, _gcs = _setup(tmp_path, monkeypatch)
    _record_posts(monkeypatch, result=None)

    async def _run():
        group = await _create_group(cs)
        return await _call(
            cs, _MESSAGES, "POST", params={"group_id": group["id"]},
            body={"text": "again"},
        )

    resp = asyncio.run(_run())
    assert resp.status_code == 409
    assert _body(resp)["error"] == "Duplicate"


def test_posting_as_a_member_agent_is_admin_or_self(tmp_path, monkeypatch):
    cs, _gcs = _setup(tmp_path, monkeypatch)
    calls = _record_posts(monkeypatch)

    async def _run():
        group = await _create_group(cs)
        params = {"group_id": group["id"]}
        return (
            # A member speaking for another member: never.
            await _call(cs, _MESSAGES, "POST", username="dog", params=params,
                        body={"text": "hi", "as_profile": "cat"}),
            # A member speaking as itself: allowed.
            await _call(cs, _MESSAGES, "POST", username="dog", params=params,
                        body={"text": "hi", "as_profile": "dog"}),
            # The admin speaking for any member: allowed.
            await _call(cs, _MESSAGES, "POST", params=params,
                        body={"text": "hi", "as_profile": "cat"}),
            # ...but only for members of this room.
            await _call(cs, _MESSAGES, "POST", params=params,
                        body={"text": "hi", "as_profile": "chicken"}),
        )

    other, self_post, admin_post, outsider = asyncio.run(_run())
    assert other.status_code == 403
    assert self_post.status_code == 202
    assert admin_post.status_code == 202
    assert outsider.status_code == 400
    assert [c["sender_profile"] for c in calls] == ["dog", "cat"]
    # Never hop 0 — only a human resets the loop guard.
    assert {c["sender_kind"] for c in calls} == {"agent"}
    assert {c["hop"] for c in calls} == {1}
    assert all(c["sender_name"] for c in calls)


# ── reading the timeline ───────────────────────────────────────────────────


async def _seed_messages(gcs, group_id, count=3):
    rows = []
    for i in range(count):
        rows.append(await gcs.add_message(
            group_id=group_id, sender_kind="user", sender_name="Ops Lead",
            content=f"m{i}",
        ))
    return rows


def test_messages_default_to_the_newest_slice_in_reading_order(tmp_path, monkeypatch):
    cs, gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _create_group(cs)
        await _seed_messages(gcs, group["id"], count=3)
        return await _call(
            cs, _MESSAGES, "GET", username="dog",
            params={"group_id": group["id"]}, query="limit=2",
        )

    rows = _body(asyncio.run(_run()))["messages"]
    assert [r["content"] for r in rows] == ["m1", "m2"]


def test_messages_after_a_cursor_return_only_what_followed(tmp_path, monkeypatch):
    cs, gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _create_group(cs)
        seeded = await _seed_messages(gcs, group["id"], count=3)
        return await _call(
            cs, _MESSAGES, "GET", username="dog",
            params={"group_id": group["id"]},
            query=f"after={seeded[0]['ordering']}",
        )

    rows = _body(asyncio.run(_run()))["messages"]
    assert [r["content"] for r in rows] == ["m1", "m2"]


def test_the_trace_never_leaks_the_raw_provider_transcript(tmp_path, monkeypatch):
    cs, gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _create_group(cs)
        seat = await cs.get_conversation_by_context("cat", f"group:{group['id']}:cat")
        turn = await cs.add_message(
            conversation_id=seat["id"], role="agent", content="all good",
            thinking_steps=[{"tool": "shell", "result": "ok"}],
            parts=[{"kind": "data", "data": {"process_id": "p1", "command": "ls"}}],
            llm_messages=[{"role": "assistant", "content": "SECRET TOOL TRACE"}],
            metadata={"provider": "anthropic", "model": "claude-x"},
        )
        posted = await gcs.add_message(
            group_id=group["id"], sender_kind="agent", sender_name="Cat",
            sender_profile="cat", content="all good",
            source_conversation_id=seat["id"], source_message_id=turn["id"],
        )
        plain = await gcs.add_message(
            group_id=group["id"], sender_kind="user", sender_name="Ops Lead",
            content="hello",
        )
        params = {"group_id": group["id"], "message_id": posted["id"]}
        # Cat wrote the turn, so Cat may read behind it.
        traced = await _call(cs, _TRACE, "GET", username="cat", params=params)
        untraced = await _call(cs, _TRACE, "GET", username="cat", params={
            "group_id": group["id"], "message_id": plain["id"],
        })
        return traced, untraced

    traced, untraced = asyncio.run(_run())
    assert traced.status_code == 200
    raw = traced.body.decode("utf-8")
    assert "llm_messages" not in raw
    assert "SECRET TOOL TRACE" not in raw
    payload = _body(traced)
    assert payload["message"]["content"] == "all good"
    assert payload["message"]["thinking_steps"] == [{"tool": "shell", "result": "ok"}]
    # The turn's artefacts travel with its steps — a trace that cannot show the
    # shell it opened is only half of what happened.
    assert payload["message"]["parts"] == [
        {"kind": "data", "data": {"process_id": "p1", "command": "ls"}}
    ]
    assert payload["message"]["provider"] == "anthropic"
    assert payload["message"]["model"] == "claude-x"
    # A human post has no turn behind it — nothing to show, not an empty trace.
    assert untraced.status_code == 404


def test_the_trace_is_only_for_its_author_and_the_admin(tmp_path, monkeypatch):
    """Sharing a room means the others hear what an agent decided to SAY.

    The steps behind it are that profile's own tool calls — the arguments it
    passed and what came back — so a fellow member must not be able to read
    them just by being in the room.
    """
    cs, gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _create_group(cs)
        seat = await cs.get_conversation_by_context("cat", f"group:{group['id']}:cat")
        turn = await cs.add_message(
            conversation_id=seat["id"], role="agent", content="all good",
            thinking_steps=[{"tool": "shell", "tool_input": "cat /etc/secrets"}],
        )
        posted = await gcs.add_message(
            group_id=group["id"], sender_kind="agent", sender_name="Cat",
            sender_profile="cat", content="all good",
            source_conversation_id=seat["id"], source_message_id=turn["id"],
        )
        params = {"group_id": group["id"], "message_id": posted["id"]}
        return (
            await _call(cs, _TRACE, "GET", username="dog", params=params),
            await _call(cs, _TRACE, "GET", username="cat", params=params),
            await _call(cs, _TRACE, "GET", username="admin", params=params),
        )

    peer, author, admin = asyncio.run(_run())
    assert peer.status_code == 403
    assert "cat /etc/secrets" not in peer.body.decode("utf-8")
    assert author.status_code == 200
    assert admin.status_code == 200


# ── the stream ───────────────────────────────────────────────────────────────


def test_an_outsider_cannot_open_the_stream(tmp_path, monkeypatch):
    cs, _gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _create_group(cs)
        return await _call(
            cs, _STREAM, "GET", username="chicken",
            params={"group_id": group["id"]},
        )

    assert asyncio.run(_run()).status_code == 403


# ── the stream's seat frames ───────────────────────────────────────────────
#
# A room's stream carries the steps of every member's running turn, mirrored off
# its seat. Those steps are that profile's own tool calls — the arguments it
# passed and what came back — so the per-frame filter here is the privacy
# boundary between two members of the same room, the live counterpart of the
# trace endpoint's author-or-admin rule.


async def _next_frame(iterator, timeout=1.0):
    """One SSE frame, or ``None`` if the stream had nothing to say in time."""
    while True:
        try:
            chunk = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
        except (asyncio.TimeoutError, StopAsyncIteration):
            return None
        text = chunk.decode("utf-8")
        if not text.startswith("data: "):
            continue  # keepalive comment
        return json.loads(text[len("data: "):])


async def _quiet_room(cs, **kwargs):
    """A group whose creation frame has been dropped from the replay ring, so a
    connecting client's replay is only what the test itself put there."""
    group = await _create_group(cs, **kwargs)
    group_bus.get_group_stream_bus().discard(group["id"])
    return group


async def _open_stream(cs, group_id, username):
    """``(iterator, frames_before_ready)`` for one viewer's SSE connection."""
    resp = await _call(
        cs, _STREAM, "GET", username=username, params={"group_id": group_id},
    )
    assert resp.status_code != 403
    iterator = resp.body_iterator
    frames = []
    while True:
        frame = await _next_frame(iterator)
        assert frame is not None, "the stream never reached its ready frame"
        if frame.get("type") == "ready":
            return iterator, frames
        frames.append(frame)


def _seat_frame(profile, step):
    return {
        "profile": profile,
        "conversation_id": f"conv-{profile}",
        "type": "thinking",
        "data": {"Step": step, "Tool": "exec_shell"},
    }


def test_a_member_sees_only_its_own_agents_steps(tmp_path, monkeypatch):
    """Being in a room buys you what the others SAY, not how they work."""
    cs, _gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _create_group(cs)
        bus = group_bus.get_group_stream_bus()
        as_admin, _ = await _open_stream(cs, group["id"], "admin")
        as_dog, _ = await _open_stream(cs, group["id"], "dog")

        await bus.publish(
            group["id"], "seat_event", _seat_frame("cat", 1), ephemeral=True,
        )
        await bus.publish(
            group["id"], "seat_event", _seat_frame("dog", 1), ephemeral=True,
        )
        # A room frame everyone gets, published last: reading it back is how a
        # filtered-out frame is told apart from a slow one.
        await bus.publish(group["id"], "message", {"id": "m1"})

        admin_seen = [await _next_frame(as_admin) for _ in range(3)]
        dog_seen = [await _next_frame(as_dog) for _ in range(2)]
        await as_admin.aclose()
        await as_dog.aclose()
        return admin_seen, dog_seen

    admin_seen, dog_seen = asyncio.run(_run())
    # The admin runs every agent, so the admin watches every agent.
    assert [f["type"] for f in admin_seen] == [
        "seat_event", "seat_event", "message",
    ]
    assert [f["data"]["profile"] for f in admin_seen[:2]] == ["cat", "dog"]
    # Dog's stream skipped Cat's frame entirely and went straight on.
    assert [f["type"] for f in dog_seen] == ["seat_event", "message"]
    assert dog_seen[0]["data"]["profile"] == "dog"
    assert "conv-cat" not in json.dumps(dog_seen)


def test_a_seat_frame_in_the_ring_is_filtered_on_replay_too(tmp_path, monkeypatch):
    """Seat frames are published ephemerally, but a filter that only guarded the
    live tail would leak the moment anything put one in the ring."""
    cs, _gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _quiet_room(cs)
        bus = group_bus.get_group_stream_bus()
        await bus.publish(group["id"], "seat_event", _seat_frame("cat", 1))
        await bus.publish(group["id"], "message", {"id": "m1"})

        dog_stream, dog_replay = await _open_stream(cs, group["id"], "dog")
        admin_stream, admin_replay = await _open_stream(cs, group["id"], "admin")
        await dog_stream.aclose()
        await admin_stream.aclose()
        return dog_replay, admin_replay

    dog_replay, admin_replay = asyncio.run(_run())
    assert [f["type"] for f in dog_replay] == ["message"]
    assert [f["type"] for f in admin_replay] == ["seat_event", "message"]


async def _busy_seat(cs, group, profile, conv_bus, *, ended=False):
    """Put a member's seat mid-turn with a couple of frames already emitted."""
    row = next(r for r in group["member_rows"] if r["profile"] == profile)
    seat_id = row["shadow_conversation_id"]
    await conv_bus.start_run(seat_id, profile)
    await conv_bus.publish(seat_id, "thinking", {"Step": 1, "Tool": "exec_shell"})
    await conv_bus.publish(seat_id, "text", {"token": "half a sentence"})
    await conv_bus.publish(seat_id, "result", {"step": 1, "Result": "ok"})
    if ended:
        await conv_bus.end_run(seat_id)
    return seat_id


def test_opening_a_busy_room_replays_the_steps_already_taken(tmp_path, monkeypatch):
    """Seat frames never enter the group ring, so without this a client opening
    a room mid-turn watches an agent it can see is thinking with no idea what it
    has been doing for the last minute."""
    cs, _gcs = _setup(tmp_path, monkeypatch)
    conv_bus = ConversationStreamBus()
    monkeypatch.setattr(stream_bus_module, "_instance", conv_bus)

    async def _run():
        group = await _quiet_room(cs)
        dog_seat = await _busy_seat(cs, group, "dog", conv_bus)
        await _busy_seat(cs, group, "cat", conv_bus)

        dog_stream, dog_replay = await _open_stream(cs, group["id"], "dog")
        admin_stream, admin_replay = await _open_stream(cs, group["id"], "admin")
        await dog_stream.aclose()
        await admin_stream.aclose()
        return dog_seat, dog_replay, admin_replay

    dog_seat, dog_replay, admin_replay = asyncio.run(_run())
    # Dog catches up on its own two allowlisted frames — the streamed token is
    # not one of them (the room renders whole messages, posted at turn end).
    assert [f["data"]["type"] for f in dog_replay] == ["thinking", "result"]
    assert {f["data"]["profile"] for f in dog_replay} == {"dog"}
    assert {f["data"]["conversation_id"] for f in dog_replay} == {dog_seat}
    assert all(f["type"] == "seat_event" for f in dog_replay)
    # Replayed frames carry no seq: they never went through the group bus, and
    # inventing one would collide with a real frame.
    assert all("seq" not in f for f in dog_replay)
    # The admin catches up on both members.
    assert {f["data"]["profile"] for f in admin_replay} == {"cat", "dog"}
    assert len(admin_replay) == 4


def test_a_finished_turn_is_not_replayed(tmp_path, monkeypatch):
    """Its answer is already a timeline row; replaying the steps behind it would
    show the room a turn that ended before anyone connected."""
    cs, _gcs = _setup(tmp_path, monkeypatch)
    conv_bus = ConversationStreamBus()
    monkeypatch.setattr(stream_bus_module, "_instance", conv_bus)

    async def _run():
        group = await _quiet_room(cs)
        await _busy_seat(cs, group, "dog", conv_bus, ended=True)
        stream, replay = await _open_stream(cs, group["id"], "admin")
        await stream.aclose()
        return replay

    assert asyncio.run(_run()) == []


# ── the room's file panel ──────────────────────────────────────────────────


def test_member_rows_carry_the_working_directory_the_viewer_may_see(
    tmp_path, monkeypatch,
):
    """The room's right panel seeds a file tree per agent, and the seats are not
    addressable through the conversations API — so the room detail carries it.
    Same visibility rule as the steps: your own agent, or every agent if admin.
    """
    cs, _gcs = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        settings_module, "get_user_working_directory", lambda: "/default",
    )
    from app.utils.context_storage import clear_context, set_context
    from app.utils.working_directory import WORKING_DIR_OVERRIDE_KEY

    async def _run():
        group = await _create_group(cs, members=("dog", "cat", "chicken"))
        cat_seat = next(
            r for r in group["member_rows"] if r["profile"] == "cat"
        )["shadow_conversation_id"]
        await cs.update_conversation(cat_seat, working_directory="/persisted/cat")
        # Dog moved this boot: the in-memory override under the SEAT's
        # context_id is what its own tools read, so it outranks the column.
        set_context(
            f"group:{group['id']}:dog", WORKING_DIR_OVERRIDE_KEY, "/live/dog",
        )
        try:
            params = {"group_id": group["id"]}
            return (
                _body(await _call(cs, _DETAIL, "GET", params=params)),
                _body(await _call(
                    cs, _DETAIL, "GET", username="dog", params=params,
                )),
            )
        finally:
            clear_context(f"group:{group['id']}:dog")

    as_admin, as_dog = asyncio.run(_run())
    admin_rows = {r["profile"]: r for r in as_admin["group"]["member_rows"]}
    assert admin_rows["dog"]["working_directory"] == "/live/dog"
    assert admin_rows["cat"]["working_directory"] == "/persisted/cat"
    # A member that never moved falls back to the profile default.
    assert admin_rows["chicken"]["working_directory"] == "/default"

    dog_rows = {r["profile"]: r for r in as_dog["group"]["member_rows"]}
    assert dog_rows["dog"]["working_directory"] == "/live/dog"
    # Where an agent is working is a path on that tenant's disk. A peer gets no
    # key at all rather than a blank one, so the panel cannot render an empty
    # tree that looks like a permission bug.
    assert "working_directory" not in dog_rows["cat"]
    assert "working_directory" not in dog_rows["chicken"]
    assert "/persisted/cat" not in json.dumps(as_dog)


# ── the timeline carries its reasoning ─────────────────────────────────────


def test_the_timeline_inlines_the_steps_for_the_admin(tmp_path, monkeypatch):
    """A reload must show the thinking process, like the two-party chat does.

    The room row stores no steps — they live on the seat message the post came
    from — so the timeline reads them back per request. One turn's payload rides
    the last of its segments: they all share the source id, and sending it under
    each would repeat a large blob for one answer.
    """
    cs, gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _create_group(cs)
        seat = await cs.get_conversation_by_context("cat", f"group:{group['id']}:cat")
        turn = await cs.add_message(
            conversation_id=seat["id"], role="agent", content="checking… all good",
            thinking_steps=[{"tool": "shell", "result": "ok"}],
            parts=[{"kind": "data", "data": {"process_id": "p1", "command": "ls"}}],
            llm_messages=[{"role": "assistant", "content": "SECRET TOOL TRACE"}],
        )
        for segment, text_ in enumerate(("checking…", "all good")):
            await gcs.add_message(
                group_id=group["id"], sender_kind="agent", sender_name="Cat",
                sender_profile="cat", content=text_,
                source_conversation_id=seat["id"], source_message_id=turn["id"],
                segment=segment,
            )
        return await _call(cs, _MESSAGES, "GET", params={"group_id": group["id"]})

    resp = asyncio.run(_run())
    assert resp.status_code == 200
    rows = _body(resp)["messages"]
    first, last = rows[0], rows[1]
    assert last["thinking_steps"] == [{"tool": "shell", "result": "ok"}]
    assert last["source_parts"] == [
        {"kind": "data", "data": {"process_id": "p1", "command": "ls"}}
    ]
    # Once per turn, on the segment the client renders the panel under.
    assert "thinking_steps" not in first
    assert "source_parts" not in first
    raw = resp.body.decode("utf-8")
    assert "llm_messages" not in raw
    assert "SECRET TOOL TRACE" not in raw


def test_a_member_never_receives_a_peers_steps(tmp_path, monkeypatch):
    """The privacy boundary, on the path that now carries the most of it.

    Being in a room buys you what the others SAY. Their steps are that profile's
    own tool calls — the arguments it passed, the paths that came back — so the
    enrichment has to be per viewer, not per room.
    """
    cs, gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _create_group(cs)
        for profile, secret in (("cat", "cat /etc/secrets"), ("dog", "dig /var/dog")):
            seat = await cs.get_conversation_by_context(
                profile, f"group:{group['id']}:{profile}",
            )
            turn = await cs.add_message(
                conversation_id=seat["id"], role="agent", content=f"{profile} done",
                thinking_steps=[{"tool": "shell", "tool_input": secret}],
            )
            await gcs.add_message(
                group_id=group["id"], sender_kind="agent", sender_name=profile.title(),
                sender_profile=profile, content=f"{profile} done",
                source_conversation_id=seat["id"], source_message_id=turn["id"],
            )
        params = {"group_id": group["id"]}
        return (
            await _call(cs, _MESSAGES, "GET", username="dog", params=params),
            await _call(cs, _MESSAGES, "GET", username="cat", params=params),
        )

    as_dog, as_cat = asyncio.run(_run())
    for resp, mine, theirs in (
        (as_dog, "dog", "cat"), (as_cat, "cat", "dog"),
    ):
        rows = {r["sender_profile"]: r for r in _body(resp)["messages"]}
        assert rows[mine]["thinking_steps"], f"{mine} lost its own steps"
        assert "thinking_steps" not in rows[theirs]
        assert "source_parts" not in rows[theirs]
    assert "cat /etc/secrets" not in as_dog.body.decode("utf-8")
    assert "dig /var/dog" not in as_cat.body.decode("utf-8")


def test_rows_without_a_turn_behind_them_carry_no_trace(tmp_path, monkeypatch):
    """A human post, a tool-made post and a post whose seat message is gone.

    None of the three has reasoning to show. The first two say nothing at all —
    there is no turn to look behind. The third answers with an EMPTY trace,
    because a client that cannot tell "nothing there" from "not told" would go
    and ask the trace endpoint, which can only answer 404, once per visit.
    """
    cs, gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _create_group(cs)
        await gcs.add_message(
            group_id=group["id"], sender_kind="user", sender_name="Ops Lead",
            content="hello",
        )
        # What ``send_group_message`` writes: an agent post with no turn behind it.
        await gcs.add_message(
            group_id=group["id"], sender_kind="agent", sender_name="Cat",
            sender_profile="cat", content="fyi",
        )
        seat = await cs.get_conversation_by_context("cat", f"group:{group['id']}:cat")
        await gcs.add_message(
            group_id=group["id"], sender_kind="agent", sender_name="Cat",
            sender_profile="cat", content="stale",
            source_conversation_id=seat["id"], source_message_id="gone-forever",
        )
        return await _call(cs, _MESSAGES, "GET", params={"group_id": group["id"]})

    resp = asyncio.run(_run())
    assert resp.status_code == 200
    rows = _body(resp)["messages"]
    assert [r["content"] for r in rows] == ["hello", "fyi", "stale"]
    for row in rows[:2]:
        assert "thinking_steps" not in row
        assert "source_parts" not in row
    assert rows[2]["thinking_steps"] == []
    assert rows[2]["source_parts"] == []


def test_the_enrichment_reads_every_seat_in_one_query(tmp_path, monkeypatch):
    """A page can reference a few hundred turns; a lookup each is a page load
    made of hundreds of round trips."""
    cs, gcs = _setup(tmp_path, monkeypatch)

    calls: list[int] = []
    bulk = cs.get_messages_by_ids

    async def _counted(ids):
        calls.append(len(ids))
        return await bulk(ids)

    async def _forbidden(_message_id):
        raise AssertionError("the timeline fetched a seat message per id")

    monkeypatch.setattr(cs, "get_messages_by_ids", _counted)
    monkeypatch.setattr(cs, "get_message", _forbidden)

    async def _run():
        group = await _create_group(cs)
        for profile in ("cat", "dog", "cat"):
            seat = await cs.get_conversation_by_context(
                profile, f"group:{group['id']}:{profile}",
            )
            turn = await cs.add_message(
                conversation_id=seat["id"], role="agent", content="done",
                thinking_steps=[{"tool": "shell"}],
            )
            await gcs.add_message(
                group_id=group["id"], sender_kind="agent", sender_name=profile.title(),
                sender_profile=profile, content="done",
                source_conversation_id=seat["id"], source_message_id=turn["id"],
            )
        return await _call(cs, _MESSAGES, "GET", params={"group_id": group["id"]})

    resp = asyncio.run(_run())
    assert resp.status_code == 200
    assert calls == [3]
    assert all(r["thinking_steps"] for r in _body(resp)["messages"])


# ── what the turn cost, on the post it produced ─────────────────────────────


def test_a_post_carries_what_its_turn_spent(tmp_path, monkeypatch):
    """The room row records no usage of its own — the tokens were spent in the
    seat — so the timeline reads it off the same seat message it already reads
    the steps from, without a second query."""
    cs, gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _create_group(cs)
        seat = await cs.get_conversation_by_context("dog", f"group:{group['id']}:dog")
        turn = await cs.add_message(
            conversation_id=seat["id"], role="agent", content="done",
            token_usage={"input_tokens": 100, "output_tokens": 20},
        )
        await gcs.add_message(
            group_id=group["id"], sender_kind="agent", sender_name="Rex",
            sender_profile="dog", content="done",
            source_conversation_id=seat["id"], source_message_id=turn["id"],
        )
        return await _call(cs, _MESSAGES, "GET", params={"group_id": group["id"]})

    row = _body(asyncio.run(_run()))["messages"][0]
    assert row["source_token_usage"] == {"input_tokens": 100, "output_tokens": 20}


def test_a_member_never_learns_what_a_peers_turn_cost(tmp_path, monkeypatch):
    """Behind the same gate as the steps, and for the same reason: how much an
    agent spent thinking is part of how it works, not of what it said."""
    cs, gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _create_group(cs)
        for profile in ("dog", "cat"):
            seat = await cs.get_conversation_by_context(
                profile, f"group:{group['id']}:{profile}",
            )
            turn = await cs.add_message(
                conversation_id=seat["id"], role="agent", content="done",
                token_usage={"input_tokens": 100, "output_tokens": 20},
            )
            await gcs.add_message(
                group_id=group["id"], sender_kind="agent", sender_name=profile.title(),
                sender_profile=profile, content="done",
                source_conversation_id=seat["id"], source_message_id=turn["id"],
            )
        return await _call(
            cs, _MESSAGES, "GET", username="dog", params={"group_id": group["id"]},
        )

    rows = {r["sender_profile"]: r for r in _body(asyncio.run(_run()))["messages"]}
    assert rows["dog"]["source_token_usage"] == {
        "input_tokens": 100, "output_tokens": 20,
    }
    assert "source_token_usage" not in rows["cat"]


def test_a_post_with_no_turn_behind_it_reports_no_usage(tmp_path, monkeypatch):
    """An explicit ``None`` rather than a missing key, for the same reason the
    steps answer an empty list: "there is nothing" must not read as "you were
    not told" and send the client to the trace endpoint."""
    cs, gcs = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _create_group(cs)
        await gcs.add_message(
            group_id=group["id"], sender_kind="agent", sender_name="Rex",
            sender_profile="dog", content="done",
            source_conversation_id="gone", source_message_id="also-gone",
        )
        return await _call(cs, _MESSAGES, "GET", params={"group_id": group["id"]})

    row = _body(asyncio.run(_run()))["messages"][0]
    assert row["source_token_usage"] is None
