"""API: the search-tools endpoints behind the composer's **Search tools** control.

What is pinned here is the contract every client (web UI, ``cremind conv
search-tools``, ``cremind group search-tools``) relies on:

- defaults for a new conversation, and an explicit selection surviving a read;
- compare-and-set on the version: a stale write is a 409 carrying the current
  state, a no-op keeps the version, a real change bumps it and is published;
- ownership: another profile's conversation is refused, a group seat is
  refused (the room holds its choice), a room is changed by its members and
  the admin only;
- the pending flag and the prompt-cache warning follow the baseline the last
  main-model request recorded, never the save itself;
- a room's availability is the aggregate of its member agents', and one
  member's private tool settings never show through as another's.
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

import app.agent.search_tools as st  # noqa: E402
import app.api.search_tools as api  # noqa: E402
import app.events.stream_bus as stream_bus_module  # noqa: E402
import app.groups.bus as group_bus  # noqa: E402
import app.storage.group_chat_storage as gcs_module  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402
from app.storage.group_chat_storage import GroupChatStorage  # noqa: E402

_TABLES = (
    "profiles",
    "channels",
    "channel_senders",
    "conversations",
    "messages",
    "usage_records",
    "group_chats",
    "group_chat_members",
    "group_chat_messages",
)
_PROFILES = ("admin", "alice", "bob", "carol")

ALL = list(st.SEARCH_TOOL_IDS)


class _Req:
    def __init__(self, username="alice", path_params=None, body=None, method="GET"):
        self.user = SimpleNamespace(is_authenticated=True, username=username)
        self.path_params = path_params or {}
        self.method = method
        self._body = body

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _body(resp) -> dict:
    return json.loads(resp.body)


class _Published:
    def __init__(self):
        self.conversation: list[tuple] = []
        self.group: list[tuple] = []


def _setup(tmp_path: Path, monkeypatch, *, avail: dict | None = None):
    provider = SqliteDatabaseProvider(str(tmp_path / "search_tools.db"))
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
    monkeypatch.setattr(gcs_module, "_instance", GroupChatStorage(provider))
    monkeypatch.setattr(group_bus, "_instance", None)

    # Availability is the registry's and the gate's business (pinned in
    # tests/agent); here it is a table: profile -> {tool id: Availability}.
    table = avail or {}

    def _availability(profile, origin, registry=None):
        per = table.get(profile) or {}
        return {t: per.get(t, st.Availability(t, True)) for t in st.SEARCH_TOOL_IDS}

    monkeypatch.setattr(st, "availability", _availability)

    published = _Published()

    class _Bus:
        async def publish_transient(self, conversation_id, event_type, data, *, profile=None):
            published.conversation.append((conversation_id, event_type, data, profile))

    class _GroupBus:
        async def publish(self, group_id, event_type, data, ephemeral=False):
            published.group.append((group_id, event_type, data))

    monkeypatch.setattr(stream_bus_module, "get_event_stream_bus", lambda: _Bus())
    monkeypatch.setattr(group_bus, "get_group_stream_bus", lambda: _GroupBus())
    return cs, gcs_module._instance, published


def _handler(cs, path: str, method: str) -> Callable:
    for route in api.get_search_tools_routes(cs):
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


def _call(cs, path, method, *, username="alice", params=None, body=None):
    return _handler(cs, path, method)(_Req(username=username, path_params=params, body=body, method=method))


_DEFAULTS = "/api/search-tools"
_CONV = "/api/conversations/{conversation_id}/search-tools"
_GROUP = "/api/group-chats/{group_id}/search-tools"


async def _conv(cs, profile="alice", **kwargs):
    return await cs.create_conversation(profile=profile, title="t", **kwargs)


async def _put(cs, conv_id, version, enabled, username="alice"):
    return await _call(cs, _CONV, "PUT", username=username,
                       params={"conversation_id": conv_id},
                       body={"version": version, "enabled": enabled})


async def _get(cs, conv_id, username="alice"):
    return await _call(cs, _CONV, "GET", username=username, params={"conversation_id": conv_id})


# ── defaults and reads ──────────────────────────────────────────────────────


def test_a_new_conversation_starts_with_every_available_source(tmp_path, monkeypatch):
    cs, _gcs, _pub = _setup(tmp_path, monkeypatch, avail={
        "alice": {"memory_search": st.Availability("memory_search", False, st.REASON_TURNED_OFF)},
    })

    resp = asyncio.run(_call(cs, _DEFAULTS, "GET"))
    body = _body(resp)
    assert resp.status_code == 200
    assert body["version"] == 0
    assert body["enabled"] == ALL
    assert body["effective"] == ["documentation_search", "cremind_documentation_search", "web_search"]
    assert [t["id"] for t in body["tools"]] == ALL, "fixed priority order"
    memory = next(t for t in body["tools"] if t["id"] == "memory_search")
    assert memory == {**memory, "available": False, "unavailable_reason": st.REASON_TURNED_OFF}
    assert body["pending_next_response"] is False and body["cache_warning"] is None


def test_documentation_search_is_absent_when_hidden(tmp_path, monkeypatch):
    cs, _gcs, _pub = _setup(tmp_path, monkeypatch, avail={
        "alice": {"documentation_search": st.Availability("documentation_search", False, None, hidden=True)},
    })
    body = _body(asyncio.run(_call(cs, _DEFAULTS, "GET")))
    assert "documentation_search" not in [t["id"] for t in body["tools"]]
    # Hidden is not forgotten: the desire stays, only the effective set drops it.
    assert "documentation_search" in body["enabled"]
    assert "documentation_search" not in body["effective"]


def test_create_carries_the_new_chat_draft(tmp_path, monkeypatch):
    cs, _gcs, _pub = _setup(tmp_path, monkeypatch)

    async def _run():
        conv = await _conv(cs, search_tools=["web_search", "documentation_search"])
        return conv, await _get(cs, conv["id"])

    conv, resp = asyncio.run(_run())
    assert conv["search_tools"] == ["documentation_search", "web_search"]
    assert _body(resp)["enabled"] == ["documentation_search", "web_search"]
    assert _body(resp)["version"] == 0


def test_another_profiles_conversation_is_refused(tmp_path, monkeypatch):
    cs, _gcs, _pub = _setup(tmp_path, monkeypatch)

    async def _run():
        conv = await _conv(cs, profile="bob")
        return await _get(cs, conv["id"]), await _put(cs, conv["id"], 0, [])

    got, put = asyncio.run(_run())
    assert got.status_code == 403 and put.status_code == 403


def test_a_group_seat_is_refused(tmp_path, monkeypatch):
    cs, _gcs, _pub = _setup(tmp_path, monkeypatch)

    async def _run():
        seat = await cs.create_conversation(profile="alice", title="seat", kind="group_chat",
                                            context_id="group:g1:alice")
        return await _get(cs, seat["id"]), await _put(cs, seat["id"], 0, [])

    got, put = asyncio.run(_run())
    assert got.status_code == 403 and put.status_code == 403
    assert _body(put)["error"] == "GroupSeat"


# ── writes ──────────────────────────────────────────────────────────────────


def test_a_change_bumps_the_version_and_is_published(tmp_path, monkeypatch):
    cs, _gcs, pub = _setup(tmp_path, monkeypatch)

    async def _run():
        conv = await _conv(cs)
        return conv, await _put(cs, conv["id"], 0, ["web_search"])

    conv, resp = asyncio.run(_run())
    body = _body(resp)
    assert resp.status_code == 200
    assert body["version"] == 1 and body["enabled"] == ["web_search"] and body["effective"] == ["web_search"]
    assert pub.conversation == [(conv["id"], "search_tools", {"version": 1}, "alice")]
    # A brand-new conversation never ran: no cache to lose, nothing pending.
    assert body["cache_warning"] is None and body["pending_next_response"] is False


def test_all_unchecked_is_a_real_choice_and_reset_restores_defaults(tmp_path, monkeypatch):
    cs, _gcs, _pub = _setup(tmp_path, monkeypatch)

    async def _run():
        conv = await _conv(cs)
        none = await _put(cs, conv["id"], 0, [])
        reset = await _put(cs, conv["id"], 1, None)
        return none, reset

    none, reset = asyncio.run(_run())
    assert _body(none)["enabled"] == [] and _body(none)["effective"] == []
    assert _body(reset)["enabled"] == ALL and _body(reset)["version"] == 2


def test_a_stale_version_is_a_409_with_the_current_state(tmp_path, monkeypatch):
    cs, _gcs, pub = _setup(tmp_path, monkeypatch)

    async def _run():
        conv = await _conv(cs)
        first = await _put(cs, conv["id"], 0, ["web_search"])  # tab A
        stale = await _put(cs, conv["id"], 0, ["memory_search"])  # tab B, still on v0
        return first, stale

    first, stale = asyncio.run(_run())
    assert first.status_code == 200
    assert stale.status_code == 409
    body = _body(stale)
    assert body["error"] == "VersionConflict"
    assert body["state"]["version"] == 1 and body["state"]["enabled"] == ["web_search"]
    assert len(pub.conversation) == 1, "a refused write publishes nothing"


def test_a_noop_keeps_the_version_and_publishes_nothing(tmp_path, monkeypatch):
    cs, _gcs, pub = _setup(tmp_path, monkeypatch)

    async def _run():
        conv = await _conv(cs)
        # Every source listed == the default: same stored value.
        return await _put(cs, conv["id"], 0, ALL)

    resp = asyncio.run(_run())
    assert resp.status_code == 200 and _body(resp)["version"] == 0
    assert pub.conversation == []


@pytest.mark.parametrize("body,code", [
    ({"enabled": []}, "InvalidVersion"),
    ({"version": "1", "enabled": []}, "InvalidVersion"),
    ({"version": True, "enabled": []}, "InvalidVersion"),
    ({"version": 0}, "InvalidSelection"),
    ({"version": 0, "enabled": ["nope"]}, "InvalidSelection"),
    ({"version": 0, "enabled": ["web_search", "web_search"]}, "InvalidSelection"),
    ({"version": 0, "enabled": "web_search"}, "InvalidSelection"),
    (["not", "an", "object"], "InvalidBody"),
])
def test_invalid_bodies_are_400(tmp_path, monkeypatch, body, code):
    cs, _gcs, _pub = _setup(tmp_path, monkeypatch)

    async def _run():
        conv = await _conv(cs)
        return await _call(cs, _CONV, "PUT", params={"conversation_id": conv["id"]}, body=body)

    resp = asyncio.run(_run())
    assert resp.status_code == 400 and _body(resp)["error"] == code


def test_a_saved_choice_changes_no_availability(tmp_path, monkeypatch):
    """Selecting a source the profile turned off is kept as a desire but never
    becomes effective: the choice narrows, it never grants."""
    off = st.Availability("web_search", False, st.REASON_TURNED_OFF)
    cs, _gcs, _pub = _setup(tmp_path, monkeypatch, avail={"alice": {"web_search": off}})

    async def _run():
        conv = await _conv(cs)
        return await _put(cs, conv["id"], 0, ["web_search", "memory_search"])

    body = _body(asyncio.run(_run()))
    assert body["enabled"] == ["memory_search", "web_search"]
    assert body["effective"] == ["memory_search"]


# ── pending and the cache warning ───────────────────────────────────────────


def _baseline(version, effective):
    return st.make_baseline(version=version, effective=effective, fingerprint_hex="f" * 64)


def test_pending_until_a_new_response_adopts_the_choice(tmp_path, monkeypatch):
    cs, _gcs, _pub = _setup(tmp_path, monkeypatch)

    async def _run():
        conv = await _conv(cs)
        await cs.record_search_cache_baseline(conv["id"], _baseline(0, ALL))
        saved = await _put(cs, conv["id"], 0, ["web_search"])
        still = await _get(cs, conv["id"])
        await cs.record_search_cache_baseline(conv["id"], _baseline(1, ["web_search"]))
        adopted = await _get(cs, conv["id"])
        return saved, still, adopted

    saved, still, adopted = asyncio.run(_run())
    assert _body(saved)["pending_next_response"] is True
    assert _body(saved)["cache_warning"] == st.CACHE_WARNING
    assert _body(still)["pending_next_response"] is True
    assert _body(still)["cache_warning"] == st.CACHE_WARNING
    assert _body(adopted)["pending_next_response"] is False
    assert _body(adopted)["cache_warning"] is None


def test_no_warning_for_a_change_to_an_unavailable_source_only(tmp_path, monkeypatch):
    off = st.Availability("memory_search", False, st.REASON_TURNED_OFF)
    cs, _gcs, _pub = _setup(tmp_path, monkeypatch, avail={"alice": {"memory_search": off}})

    async def _run():
        conv = await _conv(cs)
        avail_eff = ["documentation_search", "cremind_documentation_search", "web_search"]
        await cs.record_search_cache_baseline(conv["id"], _baseline(0, avail_eff))
        return await _put(cs, conv["id"], 0, avail_eff)  # memory_search unticked

    body = _body(asyncio.run(_run()))
    assert body["version"] == 1, "the desire changed"
    assert body["cache_warning"] is None, "what the next response exposes did not"


def test_no_warning_for_a_revert_to_the_baseline(tmp_path, monkeypatch):
    cs, _gcs, _pub = _setup(tmp_path, monkeypatch)

    async def _run():
        conv = await _conv(cs)
        await cs.record_search_cache_baseline(conv["id"], _baseline(0, ALL))
        away = await _put(cs, conv["id"], 0, ["web_search"])
        back = await _put(cs, conv["id"], 1, None)
        return away, back

    away, back = asyncio.run(_run())
    assert _body(away)["cache_warning"] == st.CACHE_WARNING
    assert _body(back)["cache_warning"] is None


def test_a_pre_upgrade_conversation_with_history_warns_conservatively(tmp_path, monkeypatch):
    """No baseline (it never ran on this version) but a reply exists: the
    last responses used the defaults, so narrowing them may miss the cache."""
    cs, _gcs, _pub = _setup(tmp_path, monkeypatch)

    async def _run():
        conv = await _conv(cs)
        await cs.add_message(conv["id"], "agent", "an earlier answer",
                             token_usage={"input_tokens": 40, "output_tokens": 9})
        return await _put(cs, conv["id"], 0, ["web_search"])

    assert _body(asyncio.run(_run()))["cache_warning"] == st.CACHE_WARNING


# ── group rooms ─────────────────────────────────────────────────────────────


async def _room(cs, gcs, members=("alice", "bob")):
    group = await gcs.create_group(name="Ops", members=list(members), settings={}, created_by="admin")
    for profile in members:
        seat = await cs.create_conversation(profile=profile, title="seat", kind="group_chat",
                                            context_id=f"group:{group['id']}:{profile}")
        await gcs.set_shadow_conversation(group["id"], profile, seat["id"])
    return await gcs.get_group(group["id"])


def _seat(group, profile):
    return next(r["shadow_conversation_id"] for r in group["member_rows"] if r["profile"] == profile)


def test_a_member_and_the_admin_may_change_the_room_an_outsider_may_not(tmp_path, monkeypatch):
    cs, gcs, pub = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _room(cs, gcs)
        params = {"group_id": group["id"]}
        member = await _call(cs, _GROUP, "PUT", username="bob", params=params,
                             body={"version": 0, "enabled": ["web_search"]})
        admin = await _call(cs, _GROUP, "PUT", username="admin", params=params,
                            body={"version": 1, "enabled": None})
        outsider_get = await _call(cs, _GROUP, "GET", username="carol", params=params)
        outsider_put = await _call(cs, _GROUP, "PUT", username="carol", params=params,
                                   body={"version": 2, "enabled": []})
        return group, member, admin, outsider_get, outsider_put

    group, member, admin, outsider_get, outsider_put = asyncio.run(_run())
    assert member.status_code == 200 and _body(member)["version"] == 1
    assert admin.status_code == 200 and _body(admin)["version"] == 2
    assert outsider_get.status_code == 403 and outsider_put.status_code == 403
    assert pub.group == [(group["id"], "search_tools", {"version": 1}),
                         (group["id"], "search_tools", {"version": 2})]


def test_room_availability_is_an_aggregate(tmp_path, monkeypatch):
    """alice may use Documentation search in rooms, bob may not and turned web
    search off: the room offers both, notes that it varies, and says nothing
    about which member is which."""
    cs, gcs, _pub = _setup(tmp_path, monkeypatch, avail={
        "bob": {
            "documentation_search": st.Availability("documentation_search", False, None, hidden=True),
            "web_search": st.Availability("web_search", False, st.REASON_TURNED_OFF),
        },
    })

    async def _run():
        group = await _room(cs, gcs)
        return await _call(cs, _GROUP, "GET", username="bob", params={"group_id": group["id"]})

    body = _body(asyncio.run(_run()))
    rows = {t["id"]: t for t in body["tools"]}
    assert rows["documentation_search"]["available"] is True
    assert rows["documentation_search"]["unavailable_reason"] == st.REASON_VARIES
    assert rows["web_search"]["available"] is True
    assert rows["web_search"]["unavailable_reason"] == st.REASON_VARIES
    assert rows["memory_search"]["unavailable_reason"] is None
    assert "alice" not in json.dumps(body) and "bob" not in json.dumps(body)


def test_documentation_search_is_hidden_in_a_room_no_member_can_use_it(tmp_path, monkeypatch):
    hidden = st.Availability("documentation_search", False, None, hidden=True)
    cs, gcs, _pub = _setup(tmp_path, monkeypatch, avail={
        "alice": {"documentation_search": hidden}, "bob": {"documentation_search": hidden},
    })

    async def _run():
        group = await _room(cs, gcs)
        return await _call(cs, _GROUP, "GET", username="admin", params={"group_id": group["id"]})

    body = _body(asyncio.run(_run()))
    assert "documentation_search" not in [t["id"] for t in body["tools"]]


def test_a_room_edit_warns_when_any_seat_may_miss_its_cache(tmp_path, monkeypatch):
    cs, gcs, _pub = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _room(cs, gcs)
        # Only alice's agent has answered in the room so far.
        await cs.record_search_cache_baseline(_seat(group, "alice"), _baseline(0, ALL))
        params = {"group_id": group["id"]}
        edit = await _call(cs, _GROUP, "PUT", username="alice", params=params,
                           body={"version": 0, "enabled": ["web_search"]})
        # alice's agent adopts it; bob's never ran — nothing is pending any more.
        await cs.record_search_cache_baseline(_seat(group, "alice"), _baseline(1, ["web_search"]))
        after = await _call(cs, _GROUP, "GET", username="alice", params=params)
        return edit, after

    edit, after = asyncio.run(_run())
    assert _body(edit)["cache_warning"] == st.CACHE_WARNING
    assert _body(edit)["pending_next_response"] is True
    assert _body(after)["pending_next_response"] is False
    assert _body(after)["cache_warning"] is None


def test_a_stale_room_write_is_a_409(tmp_path, monkeypatch):
    cs, gcs, _pub = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _room(cs, gcs)
        params = {"group_id": group["id"]}
        await _call(cs, _GROUP, "PUT", username="alice", params=params,
                    body={"version": 0, "enabled": ["web_search"]})
        return await _call(cs, _GROUP, "PUT", username="bob", params=params,
                           body={"version": 0, "enabled": []})

    resp = asyncio.run(_run())
    assert resp.status_code == 409
    assert _body(resp)["state"]["enabled"] == ["web_search"]


def test_a_save_during_a_first_response_is_pending_before_any_baseline(tmp_path, monkeypatch):
    """The first response is running but has not reached the model yet, so it
    has recorded no baseline. A choice saved now is still the NEXT response's —
    the running one keeps its tools — and the server says so."""
    cs, _gcs, _pub = _setup(tmp_path, monkeypatch)

    async def _run():
        conv = await _conv(cs)
        st.begin_run(conv["id"], "run-1", 0)
        try:
            saved = await _put(cs, conv["id"], 0, ["web_search"])
        finally:
            st.end_run(conv["id"], "run-1")
        after = await _get(cs, conv["id"])
        return saved, after

    saved, after = asyncio.run(_run())
    assert _body(saved)["pending_next_response"] is True
    # A brand-new conversation had no cache to lose.
    assert _body(saved)["cache_warning"] is None
    # The response ended without reaching the model: nothing adopted, and no
    # run is in flight any more — the flag falls back to the (absent) baseline.
    assert _body(after)["pending_next_response"] is False
    assert st.running_versions("anything") == []
