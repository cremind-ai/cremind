"""The stream runner freezes a run's search-tool selection and records its baseline.

``run_agent_to_bus`` is the one door every queued run goes through, so it is
where the conversation's (or, for a seat, the room's) selection is read and
handed to the agent as an immutable snapshot, and where the agent's
"first real main-model request" hook is turned into a stored cache baseline
plus a ``search_tools`` frame telling open views the choice was adopted.

Pinned here:
- the snapshot the agent receives is the stored selection at the run's start;
- saving a new choice while the run is going changes nothing for that run,
  and the next run adopts it;
- a seat reads its room's selection, not its own row;
- the baseline hook stores the baseline on the conversation (a seat: on the
  seat) and announces the adoption to the conversation and the room.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402
from sqlalchemy import text  # noqa: E402

import app.agent.search_tools as st  # noqa: E402
import app.agent.stream_runner as sr  # noqa: E402
import app.groups.bus as group_bus  # noqa: E402
import app.storage.group_chat_storage as gcs_module  # noqa: E402
from app.constants import ChatCompletionTypeEnum as T  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402
from app.storage.group_chat_storage import GroupChatStorage  # noqa: E402

_TABLES = (
    "profiles", "channels", "channel_senders", "conversations", "messages", "event_runs",
    "usage_records", "group_chats", "group_chat_members", "group_chat_messages",
)
_DONE = {"type": T.DONE, "input_tokens": 1, "output_tokens": 1, "finish_reason": "stop"}


class _Agent:
    """Records what each run was given; optionally saves a new choice mid-run
    and fires the baseline hook the way ReasoningAgent does."""

    def __init__(self, *, during=None, fire_baseline=True):
        self.calls: list[dict] = []
        self.during = during
        self.fire_baseline = fire_baseline

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        snap = kwargs["search_tools"]
        if self.during is not None:
            await self.during()
        if self.fire_baseline and kwargs.get("on_search_baseline"):
            await kwargs["on_search_baseline"](st.make_baseline(
                version=snap.version, effective=snap.desired(), fingerprint_hex="0" * 64,
            ))
        yield {"type": T.CONTENT, "data": "ok"}
        yield _DONE


def _setup(tmp_path: Path, monkeypatch):
    provider = SqliteDatabaseProvider(str(tmp_path / "runner.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    with eng.begin() as c:
        for i, name in enumerate(("alice", "bob")):
            c.execute(text(
                "INSERT INTO profiles (id, name, created_at, updated_at) "
                f"VALUES ('p{i}','{name}',0,0)"
            ))
    cs = ConversationStorage(provider)
    cs._initialized = True

    import app.storage as storage_pkg
    monkeypatch.setattr(storage_pkg, "get_conversation_storage", lambda *a, **k: cs)
    monkeypatch.setattr(gcs_module, "_instance", GroupChatStorage(provider))

    frames: list[tuple] = []
    room_frames: list[tuple] = []

    class _Bus:
        async def start_run(self, *a, **k):
            return None

        async def end_run(self, *a, **k):
            return None

        def is_active(self, *a, **k):
            return False

        async def publish(self, conversation_id, event_type, data=None):
            return None

        async def publish_transient(self, conversation_id, event_type, data, *, profile=None):
            frames.append((conversation_id, event_type, data, profile))

    class _GroupBus:
        async def publish(self, group_id, event_type, data, ephemeral=False):
            room_frames.append((group_id, event_type, data))

    monkeypatch.setattr(sr, "get_event_stream_bus", lambda: _Bus())
    monkeypatch.setattr(group_bus, "get_group_stream_bus", lambda: _GroupBus())
    return cs, gcs_module._instance, frames, room_frames


async def _run(cs, agent, conversation_id, profile="alice"):
    await sr.run_agent_to_bus(
        cremind_agent=agent,
        conversation_storage=cs,
        conversation_id=conversation_id,
        run_id=sr.make_run_id(conversation_id, kind="msg"),
        profile=profile,
        query="where is the Q3 report?",
        history_messages=[],
        push_user_message=False,
        update_title_from_query=False,
    )


def test_the_agent_gets_the_stored_selection_frozen_at_run_start(tmp_path, monkeypatch):
    cs, _gcs, frames, _room = _setup(tmp_path, monkeypatch)

    async def scenario():
        conv = await cs.create_conversation(profile="alice", title="c", search_tools=["web_search"])
        agent = _Agent()
        await _run(cs, agent, conv["id"])
        return conv, agent, await cs.get_search_tools_row(conv["id"])

    conv, agent, row = asyncio.run(scenario())
    snap = agent.calls[0]["search_tools"]
    assert isinstance(snap, st.Snapshot)
    assert snap.desired() == ["web_search"] and snap.version == 0
    # The baseline landed on the row, and open views were told it was adopted.
    assert st.baseline_effective(row["search_cache_baseline"]) == ["web_search"]
    assert row["search_cache_baseline"]["version"] == 0
    assert (conv["id"], "search_tools", {"version": 0, "adopted": True}, "alice") in frames


def test_a_choice_saved_mid_run_waits_for_the_next_run(tmp_path, monkeypatch):
    cs, _gcs, _frames, _room = _setup(tmp_path, monkeypatch)

    async def scenario():
        conv = await cs.create_conversation(profile="alice", title="c")

        async def save_mid_run():
            status, _row = await cs.set_search_tools(conv["id"], expected_version=0, selection=[])
            assert status == "ok"

        first = _Agent(during=save_mid_run)
        await _run(cs, first, conv["id"])
        after_first = await cs.get_search_tools_row(conv["id"])
        second = _Agent()
        await _run(cs, second, conv["id"])
        after_second = await cs.get_search_tools_row(conv["id"])
        return first, second, after_first, after_second

    first, second, after_first, after_second = asyncio.run(scenario())
    # The running response kept its tools: the defaults it started with.
    assert first.calls[0]["search_tools"].desired() == list(st.SEARCH_TOOL_IDS)
    assert first.calls[0]["search_tools"].version == 0
    # Its baseline records the version it ran on, so the saved v1 is pending…
    assert st.pending_next_response(after_first["search_tools_version"], after_first["search_cache_baseline"])
    # …until the next response adopts it.
    assert second.calls[0]["search_tools"].desired() == []
    assert second.calls[0]["search_tools"].version == 1
    assert not st.pending_next_response(after_second["search_tools_version"], after_second["search_cache_baseline"])


def test_no_baseline_without_a_main_model_request(tmp_path, monkeypatch):
    cs, _gcs, frames, _room = _setup(tmp_path, monkeypatch)

    async def scenario():
        conv = await cs.create_conversation(profile="alice", title="c")
        await _run(cs, _Agent(fire_baseline=False), conv["id"])
        return await cs.get_search_tools_row(conv["id"])

    row = asyncio.run(scenario())
    assert row["search_cache_baseline"] is None
    assert not any(f[1] == "search_tools" for f in frames)


def test_a_seat_runs_on_its_rooms_selection_and_records_on_the_seat(tmp_path, monkeypatch):
    cs, gcs, frames, room_frames = _setup(tmp_path, monkeypatch)

    async def scenario():
        group = await gcs.create_group(name="Ops", members=["alice", "bob"], settings={}, created_by="alice")
        status, group = await gcs.set_group_search_tools(
            group["id"], expected_version=0, selection=["cremind_documentation_search"],
        )
        assert status == "ok"
        seat = await cs.create_conversation(profile="bob", title="seat", kind="group_chat",
                                            context_id=f"group:{group['id']}:bob")
        # The seat's own columns are ignored: the room is the source of truth.
        await cs.set_search_tools(seat["id"], expected_version=0, selection=["web_search"])
        agent = _Agent()
        await _run(cs, agent, seat["id"], profile="bob")
        return group, seat, agent, await cs.get_search_tools_row(seat["id"])

    group, seat, agent, row = asyncio.run(scenario())
    snap = agent.calls[0]["search_tools"]
    assert snap.desired() == ["cremind_documentation_search"]
    assert snap.version == 1 and snap.source == "room"
    assert st.baseline_effective(row["search_cache_baseline"]) == ["cremind_documentation_search"]
    assert (group["id"], "search_tools", {"version": 1, "adopted": True}) in room_frames
    assert any(f[0] == seat["id"] and f[1] == "search_tools" for f in frames)


def test_a_failed_narrow_read_falls_back_to_the_row_the_runner_holds(tmp_path, monkeypatch):
    cs, _gcs, _frames, _room = _setup(tmp_path, monkeypatch)

    async def broken(*_a, **_k):
        raise RuntimeError("database is locked")

    async def scenario():
        conv = await cs.create_conversation(profile="alice", title="c", search_tools=[])
        monkeypatch.setattr(cs, "get_search_tools_row", broken)
        agent = _Agent(fire_baseline=False)
        await _run(cs, agent, conv["id"])
        return agent

    agent = asyncio.run(scenario())
    # The conversation row loaded at run start carries the same columns.
    assert agent.calls[0]["search_tools"].desired() == []


def test_an_unresolvable_selection_runs_on_the_defaults(tmp_path, monkeypatch):
    cs, _gcs, _frames, _room = _setup(tmp_path, monkeypatch)

    async def broken(*_a, **_k):
        raise RuntimeError("resolver exploded")

    async def scenario():
        conv = await cs.create_conversation(profile="alice", title="c", search_tools=[])
        monkeypatch.setattr(st, "snapshot_for_conversation", broken)
        agent = _Agent(fire_baseline=False)
        await _run(cs, agent, conv["id"])
        return agent

    agent = asyncio.run(scenario())
    assert agent.calls[0]["search_tools"] is st.DEFAULT_SNAPSHOT


def test_only_an_actual_adoption_is_announced(tmp_path, monkeypatch):
    """Every response records its baseline, but only one that starts on a
    newer saved choice (or the very first) tells open views to re-read —
    a busy room is not flooded with hints on every seat turn."""
    cs, _gcs, frames, _room = _setup(tmp_path, monkeypatch)

    async def scenario():
        conv = await cs.create_conversation(profile="alice", title="c")
        await _run(cs, _Agent(), conv["id"])            # first ever: announced
        await _run(cs, _Agent(), conv["id"])            # same version: silent
        await cs.set_search_tools(conv["id"], expected_version=0, selection=["web_search"])
        await _run(cs, _Agent(), conv["id"])            # adopts v1: announced
        return conv

    conv = asyncio.run(scenario())
    adopted = [f[2] for f in frames if f[0] == conv["id"] and f[1] == "search_tools"]
    assert adopted == [{"version": 0, "adopted": True}, {"version": 1, "adopted": True}]


@pytest.fixture(autouse=True)
def _workspaces_in_tmp(tmp_path, monkeypatch):
    """Each profile's working directory resolves under the workspaces root;
    keep it in this test's tmp dir, never the developer's ~/.cremind."""
    monkeypatch.setenv("CREMIND_WORKSPACES_DIR", str(tmp_path / "workspaces"))
