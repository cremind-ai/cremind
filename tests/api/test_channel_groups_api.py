"""API: the channel-group endpoints.

Nested under a channel, so the auth rule is the ordinary "your own row" one the
rest of the channels API uses — but with one twist worth pinning: a group id
from somebody else's channel must not be reachable through a channel of your
own, which a naive lookup-by-id would allow.

The route that carries real weight is the PATCH. Approving is what lets an agent
start talking to real people on a real platform, so it also has to leave the
group with somewhere to talk.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

pytest.importorskip("a2a")

from a2a.server.models import Base  # noqa: E402
import app.storage.models  # noqa: F401,E402
from sqlalchemy import text  # noqa: E402
from starlette.datastructures import QueryParams  # noqa: E402

import app.storage.channel_group_storage as cgs_module  # noqa: E402
from app.api.channel_groups import get_channel_group_routes  # noqa: E402
from app.databases.sqlite import SqliteDatabaseProvider  # noqa: E402
from app.storage.channel_group_storage import ChannelGroupStorage  # noqa: E402
from app.storage.conversation_storage import ConversationStorage  # noqa: E402

_TABLES = (
    "profiles",
    "channels",
    "conversations",
    "messages",
    "channel_groups",
    "channel_group_members",
)

_GROUPS = "/api/channels/{channel_id}/groups"
_DETAIL = "/api/channels/{channel_id}/groups/{group_id}"
_ROSTER = "/api/channels/{channel_id}/groups/{group_id}/roster"


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


def _body(resp) -> dict:
    return json.loads(resp.body)


def _setup(tmp_path: Path, monkeypatch):
    provider = SqliteDatabaseProvider(str(tmp_path / "cg.db"))
    eng = provider.sync_engine()
    for name in _TABLES:
        Base.metadata.tables[name].create(bind=eng, checkfirst=True)
    now = time.time() * 1000
    with eng.begin() as c:
        for i, name in enumerate(("admin", "dog")):
            c.execute(text(
                "INSERT INTO profiles (id,name,created_at,updated_at) "
                f"VALUES ('p{i}','{name}',:n,:n)"
            ), {"n": now})
        for cid, profile, ctype in (
            ("ch1", "admin", "telegram"), ("ch2", "dog", "slack"),
            # A Telegram *bot* cannot enumerate the groups it is in, so the
            # picker tests need a platform that can.
            ("ch3", "admin", "slack"),
        ):
            c.execute(text(
                "INSERT INTO channels (id,profile,channel_type,mode,auth_mode,"
                "response_mode,enabled,config,created_at,updated_at) VALUES "
                f"('{cid}','{profile}','{ctype}','bot','none','normal',1,"
                "'{\"group_chats_enabled\": true}',:n,:n)"
            ), {"n": now})

    cs = ConversationStorage(provider)
    cs._initialized = True
    monkeypatch.setattr(cgs_module, "_instance", ChannelGroupStorage(provider))
    # No live channel subsystem in a unit test; the API treats a stopped channel
    # as a normal state, which is exactly what this exercises.
    return cs, cgs_module._instance


def _handler(cs, path: str, method: str) -> Callable:
    for route in get_channel_group_routes(cs):
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


def _call(cs, path, method, *, username="admin", params=None, body=None, query=""):
    return _handler(cs, path, method)(_Req(
        username=username, path_params=params, body=body, method=method, query=query,
    ))


async def _group(store, *, channel_id="ch1", profile="admin", **overrides):
    kwargs = {
        "channel_id": channel_id, "profile": profile,
        "platform_chat_id": "-1001", "title": "Ops room",
    }
    kwargs.update(overrides)
    return await store.create_group(**kwargs)


# ── auth ──────────────────────────────────────────────────────────────────


def test_another_profiles_channel_is_forbidden(tmp_path, monkeypatch):
    cs, _store = _setup(tmp_path, monkeypatch)
    resp = asyncio.run(_call(
        cs, _GROUPS, "GET", username="dog", params={"channel_id": "ch1"},
    ))
    assert resp.status_code == 403


def test_a_group_from_another_channel_is_not_reachable(tmp_path, monkeypatch):
    """The 403 above is not enough on its own: without the channel check on the
    group itself, somebody's own channel id plus a stranger's group id would
    resolve."""
    cs, store = _setup(tmp_path, monkeypatch)

    async def _run():
        other = await _group(store, channel_id="ch2", profile="dog")
        return await _call(cs, _DETAIL, "PATCH", params={
            "channel_id": "ch1", "group_id": other["id"],
        }, body={"status": "approved"})

    assert asyncio.run(_run()).status_code == 404


def test_an_unknown_channel_is_a_404(tmp_path, monkeypatch):
    cs, _store = _setup(tmp_path, monkeypatch)
    resp = asyncio.run(_call(cs, _GROUPS, "GET", params={"channel_id": "nope"}))
    assert resp.status_code == 404


# ── listing ───────────────────────────────────────────────────────────────


def test_the_list_reports_whether_the_feature_is_even_on(tmp_path, monkeypatch):
    """An empty list means two different things — nothing has happened yet, or
    nothing ever will — and the UI has to say which."""
    cs, store = _setup(tmp_path, monkeypatch)

    async def _run():
        await _group(store)
        return await _call(cs, _GROUPS, "GET", params={"channel_id": "ch1"})

    payload = _body(asyncio.run(_run()))
    assert payload["group_chats_enabled"] is True
    assert len(payload["groups"]) == 1


def test_the_list_can_be_narrowed_to_one_status(tmp_path, monkeypatch):
    cs, store = _setup(tmp_path, monkeypatch)

    async def _run():
        await _group(store, platform_chat_id="-1")
        approved = await _group(store, platform_chat_id="-2")
        await store.update_group(approved["id"], status="approved")
        return await _call(
            cs, _GROUPS, "GET", params={"channel_id": "ch1"}, query="status=pending",
        )

    groups = _body(asyncio.run(_run()))["groups"]
    assert [g["platform_chat_id"] for g in groups] == ["-1"]


def test_a_nonsense_status_is_refused(tmp_path, monkeypatch):
    cs, _store = _setup(tmp_path, monkeypatch)
    resp = asyncio.run(_call(
        cs, _GROUPS, "GET", params={"channel_id": "ch1"}, query="status=maybe",
    ))
    assert resp.status_code == 400


def test_every_row_carries_normalised_settings_and_the_platforms_limits(
    tmp_path, monkeypatch,
):
    """A blob written before a knob existed must not make the UI reason about
    absent keys, and an empty member list needs explaining rather than
    apologising for."""
    cs, store = _setup(tmp_path, monkeypatch)

    async def _run():
        await _group(store)
        return await _call(cs, _GROUPS, "GET", params={"channel_id": "ch1"})

    (group,) = _body(asyncio.run(_run()))["groups"]
    assert group["settings"]["member_policy"]["mode"] == "everyone"
    assert group["settings"]["respond_mode"] == "mention_or_relevant"
    assert set(group["capabilities"]) == {
        "roster", "join_events", "bot_flag", "listing",
    }
    assert group["member_count"] == 0


def test_each_member_carries_the_answer_the_runtime_would_give(
    tmp_path, monkeypatch,
):
    """The UI renders its per-member switch from this. If it were computed
    client-side the switch could disagree with what the agent actually does."""
    cs, store = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _group(store)
        await store.update_group(group["id"], settings={
            "member_policy": {"mode": "everyone", "allow": [], "deny": ["u-spam"]},
        })
        for member_id in ("u-alexa", "u-spam"):
            await store.upsert_member(group["id"], member_id=member_id)
        return await _call(cs, _GROUPS, "GET", params={"channel_id": "ch1"})

    (group,) = _body(asyncio.run(_run()))["groups"]
    responds = {m["member_id"]: m["responds"] for m in group["members"]}
    assert responds == {"u-alexa": True, "u-spam": False}


# ── the decision ──────────────────────────────────────────────────────────


def test_approving_a_group_lets_the_agent_in(tmp_path, monkeypatch):
    cs, store = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _group(store)
        return await _call(cs, _DETAIL, "PATCH", params={
            "channel_id": "ch1", "group_id": group["id"],
        }, body={"status": "approved"})

    assert _body(asyncio.run(_run()))["group"]["status"] == "approved"


def test_blocking_keeps_the_group_on_the_record(tmp_path, monkeypatch):
    """Unlike forgetting it: a blocked group is a decision, and being added
    again must not ask a second time."""
    cs, store = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _group(store)
        await _call(cs, _DETAIL, "PATCH", params={
            "channel_id": "ch1", "group_id": group["id"],
        }, body={"status": "blocked"})
        return await store.get_group(group["id"])

    assert asyncio.run(_run())["status"] == "blocked"


@pytest.mark.parametrize("body,fragment", [
    ({"status": "maybe"}, "status must be one of"),
    ({"title": "  "}, "cannot be empty"),
    ({"settings": {"respond_mode": "whenever"}}, "respond_mode must be"),
    ({"settings": {"member_policy": {"mode": "sometimes"}}}, "member_policy.mode"),
    ({}, "Nothing to update"),
])
def test_a_patch_it_cannot_use_is_refused_with_a_reason(
    tmp_path, monkeypatch, body, fragment,
):
    cs, store = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _group(store)
        return await _call(cs, _DETAIL, "PATCH", params={
            "channel_id": "ch1", "group_id": group["id"],
        }, body=body)

    resp = asyncio.run(_run())
    assert resp.status_code == 400
    assert fragment in _body(resp)["error"]


def test_a_settings_patch_keeps_the_keys_it_does_not_name(tmp_path, monkeypatch):
    """The UI edits one knob at a time; resending the whole blob every time is
    how a stale tab reverts somebody else's change."""
    cs, store = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _group(store)
        await _call(cs, _DETAIL, "PATCH", params={
            "channel_id": "ch1", "group_id": group["id"],
        }, body={"settings": {"member_policy": {
            "mode": "everyone", "allow": [], "deny": ["u-spam"],
        }}})
        return await _call(cs, _DETAIL, "PATCH", params={
            "channel_id": "ch1", "group_id": group["id"],
        }, body={"settings": {"respond_mode": "mention_only"}})

    settings = _body(asyncio.run(_run()))["group"]["settings"]
    assert settings["respond_mode"] == "mention_only"
    assert settings["member_policy"]["deny"] == ["u-spam"]


# ── forgetting ────────────────────────────────────────────────────────────


def test_forgetting_a_group_removes_it_and_its_transcript(tmp_path, monkeypatch):
    cs, store = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _group(store)
        conv = await cs.create_conversation(profile="admin", title="Ops room")
        await store.update_group(group["id"], conversation_id=conv["id"])
        resp = await _call(cs, _DETAIL, "DELETE", params={
            "channel_id": "ch1", "group_id": group["id"],
        })
        return (
            resp,
            await store.get_group(group["id"]),
            await cs.get_conversation(conv["id"]),
        )

    resp, group, conv = asyncio.run(_run())
    assert _body(resp)["deleted"] is True
    assert group is None
    assert conv is None


def test_forgetting_is_refused_while_a_turn_is_running(tmp_path, monkeypatch):
    """The same rule the sender endpoints use: never delete rows out from under
    a live turn."""
    cs, store = _setup(tmp_path, monkeypatch)

    class _Bus:
        def is_active(self, _conversation_id):
            return True

    import app.events.stream_bus as bus_mod
    monkeypatch.setattr(bus_mod, "get_event_stream_bus", lambda: _Bus())

    async def _run():
        group = await _group(store)
        await store.update_group(group["id"], conversation_id="conv-live")
        return await _call(cs, _DETAIL, "DELETE", params={
            "channel_id": "ch1", "group_id": group["id"],
        })

    resp = asyncio.run(_run())
    assert resp.status_code == 409
    assert "run in progress" in _body(resp)["error"]


# ── the roster ────────────────────────────────────────────────────────────


def test_refreshing_the_roster_needs_a_running_channel(tmp_path, monkeypatch):
    """The member list comes from the platform, and a stopped channel has
    nothing to ask."""
    cs, store = _setup(tmp_path, monkeypatch)

    async def _run():
        group = await _group(store)
        return await _call(cs, _ROSTER, "POST", params={
            "channel_id": "ch1", "group_id": group["id"],
        })

    resp = asyncio.run(_run())
    assert resp.status_code == 409
    assert "not running" in _body(resp)["error"]


# ── groups the account was already in ─────────────────────────────────────

_AVAILABLE = "/api/channels/{channel_id}/groups/available"


class _ListingAdapter:
    """A running adapter that can enumerate the account's groups."""

    supports_group_listing = True

    _DEFAULT = object()

    def __init__(self, groups=_DEFAULT):
        # ``None`` is a meaningful answer here — "this platform will not tell
        # you" — so it cannot double as "use the defaults".
        self.groups_listed = [
            {
                "platform_chat_id": "-1001", "title": "Ops room",
                "chat_type": "supergroup", "member_count": 4,
            },
            {
                "platform_chat_id": "-2002", "title": "Lunch",
                "chat_type": "group", "member_count": 9,
            },
        ] if groups is _ListingAdapter._DEFAULT else groups
        self.rosters: list = []

    async def fetch_joined_groups(self):
        return self.groups_listed


def _running(monkeypatch, adapter):
    import app.api.channel_groups as api_mod
    monkeypatch.setattr(api_mod, "_adapter_for", lambda _cid: adapter)


def test_the_groups_the_account_is_in_are_offered(tmp_path, monkeypatch):
    """A group nobody was added to raises no join event and gets no
    notification, so this listing is the only way to reach it."""
    cs, _store = _setup(tmp_path, monkeypatch)
    _running(monkeypatch, _ListingAdapter())

    body = _body(asyncio.run(_call(
        cs, _AVAILABLE, "GET", params={"channel_id": "ch3"},
    )))
    assert body["supported"] is True
    assert [g["platform_chat_id"] for g in body["groups"]] == ["-1001", "-2002"]
    assert all(g["tracked"] is None for g in body["groups"])


def test_a_group_already_enabled_is_marked_as_such(tmp_path, monkeypatch):
    """Otherwise the picker offers the operator something they already did."""
    cs, store = _setup(tmp_path, monkeypatch)
    _running(monkeypatch, _ListingAdapter())

    async def _run():
        group = await _group(store, channel_id="ch3")
        await store.update_group(group["id"], status="approved")
        return await _call(cs, _AVAILABLE, "GET", params={"channel_id": "ch3"})

    groups = _body(asyncio.run(_run()))["groups"]
    tracked = {g["platform_chat_id"]: g["tracked"] for g in groups}
    assert tracked["-1001"]["status"] == "approved"
    assert tracked["-2002"] is None


def test_a_platform_that_cannot_list_says_so_rather_than_erroring(
    tmp_path, monkeypatch,
):
    """A Telegram bot has no such API. That is a fact about the platform, and
    the UI explains it instead of showing an empty list that looks broken."""
    cs, _store = _setup(tmp_path, monkeypatch)
    _running(monkeypatch, _ListingAdapter(groups=None))

    body = _body(asyncio.run(_call(
        cs, _AVAILABLE, "GET", params={"channel_id": "ch3"},
    )))
    assert body == {"supported": False, "groups": []}


def test_listing_needs_a_running_channel(tmp_path, monkeypatch):
    cs, _store = _setup(tmp_path, monkeypatch)
    resp = asyncio.run(_call(cs, _AVAILABLE, "GET", params={"channel_id": "ch3"}))
    assert resp.status_code == 409


def test_picking_a_group_enables_it_without_asking_again(tmp_path, monkeypatch):
    """Choosing from your own group list IS the approval — the operator is
    looking at the groups and saying which ones. Asking a second time on the
    Channels page would be ceremony."""
    cs, store = _setup(tmp_path, monkeypatch)

    body = _body(asyncio.run(_call(
        cs, _GROUPS, "POST", params={"channel_id": "ch1"},
        body={"platform_chat_id": "-1001", "title": "Ops room"},
    )))
    assert body["group"]["status"] == "approved"
    assert body["group"]["discovered_via"] == "picked"


def test_picking_a_group_that_is_pending_approves_it(tmp_path, monkeypatch):
    """The same group can be discovered by a message first and picked second;
    the pick must not leave it waiting."""
    cs, store = _setup(tmp_path, monkeypatch)

    async def _run():
        await _group(store)  # pending, discovered by a message
        return await _call(
            cs, _GROUPS, "POST", params={"channel_id": "ch1"},
            body={"platform_chat_id": "-1001"},
        )

    body = _body(asyncio.run(_run()))
    assert body["group"]["status"] == "approved"


def test_picking_needs_a_chat_id(tmp_path, monkeypatch):
    cs, _store = _setup(tmp_path, monkeypatch)
    resp = asyncio.run(_call(
        cs, _GROUPS, "POST", params={"channel_id": "ch1"}, body={},
    ))
    assert resp.status_code == 400


def test_picking_into_another_profiles_channel_is_forbidden(tmp_path, monkeypatch):
    cs, _store = _setup(tmp_path, monkeypatch)
    resp = asyncio.run(_call(
        cs, _GROUPS, "POST", username="dog", params={"channel_id": "ch1"},
        body={"platform_chat_id": "-1001"},
    ))
    assert resp.status_code == 403


def test_capabilities_answer_for_the_channels_own_mode(tmp_path, monkeypatch):
    """Two modes of one platform are two adapters with different answers: a Zalo
    *bot* can name nobody in a group and cannot list the groups it is in, while
    the QR-paired personal account does both. Resolving by channel TYPE alone
    reported the bot's answer for both, which hid the member list and the group
    picker on exactly the channels that support them.
    """
    import app.api.channel_groups as api_mod

    bot = api_mod._capabilities("zalo", "bot")
    userbot = api_mod._capabilities("zalo", "userbot")

    assert bot["roster"] is False and bot["listing"] is False
    assert userbot["roster"] is True and userbot["listing"] is True
