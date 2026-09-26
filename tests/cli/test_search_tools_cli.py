"""`cremind conv search-tools` and `cremind group search-tools`.

The client functions are imported inside the command bodies, so they are
patched in ``app.cli.client.search_tools`` and nothing reaches the network.
What is pinned: the flags map to the selection the server expects (a list
replaces, ``--all`` restores the defaults with ``null``, ``--none`` is ``[]``),
the flags are mutually exclusive and ids are checked locally, a write carries
the version it read and retries one conflict, the human output shows the
pending notice and the cache warning, and ``--json`` prints the whole state.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("typer")

from typer.testing import CliRunner  # noqa: E402

WARNING = (
    "Changing search tools may reduce prompt-cache reuse on the next response "
    "and increase input-token cost."
)


def _state(version=0, enabled=None, pending=False, warning=None):
    ids = ["documentation_search", "cremind_documentation_search", "memory_search", "web_search"]
    enabled = ids if enabled is None else enabled
    return {
        "version": version,
        "enabled": enabled,
        "effective": [i for i in enabled if i != "memory_search"],
        "tools": [
            {"id": i, "label": i.replace("_", " "), "description": "", "available": i != "memory_search",
             "unavailable_reason": None if i != "memory_search" else "Turned off in Tools settings."}
            for i in ids
        ],
        "pending_next_response": pending,
        "cache_warning": warning,
    }


def _patch(monkeypatch, *, current=None, answer=None, conflicts=0):
    import app.cli.client.search_tools as client

    calls: dict = {"get": [], "put": []}
    state = {"current": current or _state(), "conflicts": conflicts}

    async def fake_get(c, path):
        calls["get"].append(path)
        return state["current"]

    async def fake_put(c, path, *, version, enabled):
        calls["put"].append((path, version, enabled))
        if state["conflicts"]:
            state["conflicts"] -= 1
            fresh = _state(version=version + 1, enabled=["web_search"])
            state["current"] = fresh
            raise client.VersionConflict(fresh)
        return answer or _state(version=version + 1, enabled=enabled if enabled is not None else None)

    monkeypatch.setattr(client, "get_state", fake_get)
    monkeypatch.setattr(client, "put_state", fake_put)
    return calls


def _run(*args):
    from app.cli.main import app

    return CliRunner().invoke(app, ["--token", "t", *args])


def test_show_prints_the_table_in_priority_order(monkeypatch):
    calls = _patch(monkeypatch)
    result = _run("conv", "search-tools", "c1")
    assert result.exit_code == 0, result.output
    assert calls["get"] == ["/api/conversations/c1/search-tools"] and calls["put"] == []
    out = result.stdout
    assert out.index("documentation_search)") < out.index("cremind_documentation_search)") \
        < out.index("memory_search)") < out.index("web_search)")
    assert "Turned off in Tools settings." in out
    assert "Next response uses: documentation_search, cremind_documentation_search, web_search" in out


def test_enable_replaces_the_selection_with_the_read_version(monkeypatch):
    calls = _patch(monkeypatch, current=_state(version=4))
    result = _run("conv", "search-tools", "c1", "--enable", "web_search", "--enable",
                  "documentation_search,memory_search")
    assert result.exit_code == 0, result.output
    assert calls["put"] == [("/api/conversations/c1/search-tools", 4,
                             ["web_search", "documentation_search", "memory_search"])]


@pytest.mark.parametrize("flag,expected", [("--all", None), ("--none", [])])
def test_all_and_none(monkeypatch, flag, expected):
    calls = _patch(monkeypatch)
    result = _run("conv", "search-tools", "c1", flag)
    assert result.exit_code == 0, result.output
    assert calls["put"][0][2] == expected


@pytest.mark.parametrize("args", [
    ["--all", "--none"],
    ["--enable", "web_search", "--all"],
    ["--enable", "web_search", "--none"],
])
def test_the_flags_are_mutually_exclusive(monkeypatch, args):
    calls = _patch(monkeypatch)
    result = _run("conv", "search-tools", "c1", *args)
    assert result.exit_code != 0
    assert calls["get"] == [] and calls["put"] == []


@pytest.mark.parametrize("bad", ["user_documents", "web_search,web_search"])
def test_unknown_or_repeated_ids_are_rejected_locally(monkeypatch, bad):
    calls = _patch(monkeypatch)
    result = _run("conv", "search-tools", "c1", "--enable", bad)
    assert result.exit_code != 0
    assert calls["put"] == []


def test_one_conflict_is_retried_against_the_fresh_version(monkeypatch):
    calls = _patch(monkeypatch, current=_state(version=2), conflicts=1)
    result = _run("conv", "search-tools", "c1", "--none")
    assert result.exit_code == 0, result.output
    assert [(v, e) for _p, v, e in calls["put"]] == [(2, []), (3, [])]


def test_a_second_conflict_is_reported(monkeypatch):
    calls = _patch(monkeypatch, conflicts=2)
    result = _run("conv", "search-tools", "c1", "--none")
    assert result.exit_code == 1
    assert "changed elsewhere" in result.stderr
    assert len(calls["put"]) == 2


def test_pending_notice_and_cache_warning(monkeypatch):
    _patch(monkeypatch, answer=_state(version=1, enabled=["web_search"], pending=True, warning=WARNING))
    result = _run("conv", "search-tools", "c1", "--enable", "web_search")
    assert result.exit_code == 0, result.output
    assert "Saved for the next response (a response already running keeps its existing search tools)."         in result.stdout
    assert f"warning: {WARNING}" in result.stderr


def test_json_prints_the_whole_state(monkeypatch):
    state = _state(version=7, enabled=["web_search"], pending=True, warning=WARNING)
    _patch(monkeypatch, current=state)
    result = _run("--json", "conv", "search-tools", "c1")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == state


def test_group_resolves_a_name_and_uses_the_room_path(monkeypatch):
    import app.cli.client.groups as groups

    async def fake_resolve(client, name_or_id):
        assert name_or_id == "Standup"
        return "g-42"

    monkeypatch.setattr(groups, "resolve_group_id", fake_resolve)
    calls = _patch(monkeypatch)
    result = _run("group", "search-tools", "Standup", "--enable", "cremind_documentation_search")
    assert result.exit_code == 0, result.output
    assert calls["get"] == ["/api/group-chats/g-42/search-tools"]
    assert calls["put"] == [("/api/group-chats/g-42/search-tools", 0, ["cremind_documentation_search"])]


def test_the_client_turns_a_409_body_into_a_conflict(monkeypatch):
    import asyncio

    import app.cli.client.search_tools as client
    from app.cli.client._base import APIError

    fresh = _state(version=9)

    class _C:
        async def put_json(self, path, body):
            # The shape Client._check_response really raises: a human body and
            # the server's JSON in ``raw``.
            raw = json.dumps({"error": "VersionConflict", "message": "changed", "state": fresh}).encode()
            raise APIError(409, "VersionConflict: changed", raw)

    with pytest.raises(client.VersionConflict) as info:
        asyncio.run(client.put_state(_C(), "/x", version=1, enabled=[]))
    assert info.value.state == fresh


def test_a_real_409_from_the_server_becomes_a_conflict():
    """Driven through the real Client and httpx, not a hand-built APIError."""
    import asyncio

    import httpx

    import app.cli.client.search_tools as client
    from app.cli.client._base import Client
    from app.cli.config import Config

    fresh = _state(version=3, enabled=["web_search"])

    def handler(request):
        return httpx.Response(409, json={"error": "VersionConflict", "message": "changed", "state": fresh})

    async def go():
        cfg = Config(server="http://cremind.test", token="t", output="table", no_color=True)
        async with Client(cfg) as c:
            c._http = httpx.AsyncClient(base_url="http://cremind.test", transport=httpx.MockTransport(handler))
            await client.put_state(c, "/api/conversations/c1/search-tools", version=2, enabled=[])

    with pytest.raises(client.VersionConflict) as info:
        asyncio.run(go())
    assert info.value.state == fresh


def test_an_unknown_group_name_is_one_line_of_advice(monkeypatch):
    import app.cli.client.groups as groups

    async def fake_resolve(client, name_or_id):
        raise RuntimeError(f"no group named {name_or_id!r} — see `cremind group list`")

    monkeypatch.setattr(groups, "resolve_group_id", fake_resolve)
    _patch(monkeypatch)
    result = _run("group", "search-tools", "Stndup")
    assert result.exit_code == 1
    assert "no group named 'Stndup'" in result.stderr
    assert "Traceback" not in result.output
