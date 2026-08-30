"""`cremind group` — argument plumbing, output modes, and the follow loop.

The commands do function-body imports (`from app.cli.client.groups import ...`),
so every stub goes on the **client module**, not on the command module — the
name is looked up when the command runs.

What is worth pinning here: the group argument is resolved to an id before
anything is posted (posting into the wrong room cannot be undone), a body can
arrive on stdin (PowerShell quoting mangles inline text), `set` with no flags is
an error rather than a silent no-op, an identity has to look like
`CHANNEL:ID` before it is sent, and `--follow` ends when the group is deleted.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner


def _group(**overrides):
    from app.cli.client.groups import GroupChat

    payload = {
        "id": "g-1",
        "name": "Ops",
        "members": ["Cat", "Dog"],
        "settings": {"max_agent_hops": 6, "web_sender_name": "Operator"},
        "last_message": {"sender_name": "Rex", "content": "14:20 here."},
    }
    payload.update(overrides)
    return GroupChat.from_dict(payload)


def _message(**overrides):
    from app.cli.client.groups import GroupMessage

    payload = {
        "id": "m-1",
        "group_id": "g-1",
        "ordering": 5,
        "sender_kind": "user",
        "sender_name": "Operator",
        "content": "status?",
        "created_at": 1_700_000_000_000.0,
    }
    payload.update(overrides)
    return GroupMessage.from_dict(payload)


@pytest.fixture
def groups_client(monkeypatch):
    """Stub the whole client module; each test overrides what it cares about."""
    import app.cli.client.groups as client

    async def resolve_group_id(_client, ref):
        captured["ref"] = ref
        return "g-1"

    captured: dict = {}
    monkeypatch.setattr(client, "resolve_group_id", resolve_group_id)
    return captured


def _invoke(args: list[str], **kwargs):
    from app.cli.main import app

    return CliRunner().invoke(app, ["--token", "t", *args], **kwargs)


# ── list ──────────────────────────────────────────────────────────────────


def test_list_renders_a_table(monkeypatch):
    import app.cli.client.groups as client

    async def list_groups(_client):
        return [_group()]

    monkeypatch.setattr(client, "list_groups", list_groups)
    result = _invoke(["group", "list"])
    assert result.exit_code == 0, result.output
    assert "Ops" in result.stdout
    assert "Cat, Dog" in result.stdout


def test_list_json_is_parseable(monkeypatch):
    import app.cli.client.groups as client

    async def list_groups(_client):
        return [_group()]

    monkeypatch.setattr(client, "list_groups", list_groups)
    result = _invoke(["--json", "group", "list"])
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed[0]["id"] == "g-1"
    assert parsed[0]["settings"]["max_agent_hops"] == 6


# ── send ──────────────────────────────────────────────────────────────────


def test_send_posts_the_resolved_id_and_the_as_profile(monkeypatch, groups_client):
    import app.cli.client.groups as client

    captured: dict = {}

    async def post_message(_client, group_id, text, *, as_profile=""):
        captured.update(
            group_id=group_id, text=text, as_profile=as_profile,
        )
        return _message(sender_kind="agent", sender_name="Rex")

    monkeypatch.setattr(client, "post_message", post_message)
    result = _invoke(["group", "send", "Ops", "on it", "--as", "Dog"])
    assert result.exit_code == 0, result.output
    # The name reached the resolver, and the id — not the name — reached the POST.
    assert groups_client["ref"] == "Ops"
    assert captured == {"group_id": "g-1", "text": "on it", "as_profile": "Dog"}
    assert "posted #5" in result.stdout


def test_send_reads_the_body_from_stdin(monkeypatch, groups_client):
    import app.cli.client.groups as client

    captured: dict = {}

    async def post_message(_client, group_id, text, *, as_profile=""):
        captured["text"] = text
        return _message()

    monkeypatch.setattr(client, "post_message", post_message)
    result = _invoke(["group", "send", "Ops"], input="piped body\n")
    assert result.exit_code == 0, result.output
    assert captured["text"] == "piped body"


def test_send_refuses_an_empty_body(monkeypatch, groups_client):
    import app.cli.client.groups as client

    async def post_message(*a, **k):
        raise AssertionError("must not post an empty message")

    monkeypatch.setattr(client, "post_message", post_message)
    result = _invoke(["group", "send", "Ops"], input="   \n")
    assert result.exit_code == 1
    assert "empty" in result.output


def test_send_follow_subscribes_at_its_own_post(monkeypatch, groups_client):
    """`since` is exclusive, so subscribing *at* the row we just printed is what
    keeps the replay ring from handing the caller their own post back."""
    import app.cli.client.groups as client

    async def post_message(_client, group_id, text, *, as_profile=""):
        return _message(ordering=5, content="status?")

    monkeypatch.setattr(client, "post_message", post_message)
    captured: dict = {}
    _patch_stream(monkeypatch, [_frame("deleted", {})], captured)

    result = _invoke(["group", "send", "Ops", "status?", "--follow"])
    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/group-chats/g-1/stream?since=5"
    # Printed once by the command, and not a second time by the replay.
    assert result.stdout.count("status?") == 1


def test_an_unknown_group_name_is_one_clean_line(monkeypatch):
    import app.cli.client.groups as client

    async def resolve_group_id(_client, ref):
        raise RuntimeError(f"no group matches {ref!r} — run `cremind group list`")

    monkeypatch.setattr(client, "resolve_group_id", resolve_group_id)
    result = _invoke(["group", "send", "ghost", "hi"])
    assert result.exit_code == 1
    assert "no group matches 'ghost'" in result.output
    assert "Traceback" not in result.output


# ── set ───────────────────────────────────────────────────────────────────


def test_set_without_any_flag_exits_1(monkeypatch, groups_client):
    import app.cli.client.groups as client

    async def update_group(*a, **k):
        raise AssertionError("nothing to update — must not reach the server")

    monkeypatch.setattr(client, "update_group", update_group)
    result = _invoke(["group", "set", "Ops"])
    assert result.exit_code == 1
    assert "--max-hops" in result.output


def test_set_merges_into_the_existing_settings(monkeypatch, groups_client):
    import app.cli.client.groups as client

    captured: dict = {}

    async def get_group(_client, group_id):
        return _group(settings={"max_agent_hops": 6, "web_sender_name": "Alexa"})

    async def update_group(_client, group_id, fields):
        captured.update(group_id=group_id, fields=fields)
        return _group(settings=fields.get("settings") or {})

    monkeypatch.setattr(client, "get_group", get_group)
    monkeypatch.setattr(client, "update_group", update_group)
    result = _invoke(["group", "set", "Ops", "--max-hops", "2"])
    assert result.exit_code == 0, result.output
    settings = captured["fields"]["settings"]
    assert settings["max_agent_hops"] == 2
    # The knobs we did not name survive the whole-blob replace.
    assert settings["web_sender_name"] == "Alexa"


def test_set_sends_the_routing_switch_both_ways(monkeypatch, groups_client):
    """`--no-routing` is the case a truthiness bug eats: it has to reach the
    server as `false`, not be dropped as "no value given"."""
    import app.cli.client.groups as client

    captured: dict = {}

    async def get_group(_client, group_id):
        return _group(settings={"smart_routing": True})

    async def update_group(_client, group_id, fields):
        captured.update(fields=fields)
        return _group(settings=fields.get("settings") or {})

    monkeypatch.setattr(client, "get_group", get_group)
    monkeypatch.setattr(client, "update_group", update_group)

    assert _invoke(["group", "set", "Ops", "--no-routing"]).exit_code == 0
    assert captured["fields"]["settings"]["smart_routing"] is False

    assert _invoke(["group", "set", "Ops", "--routing"]).exit_code == 0
    assert captured["fields"]["settings"]["smart_routing"] is True


def test_create_forwards_the_routing_switch(monkeypatch):
    import app.cli.client.groups as client

    captured: dict = {}

    async def create_group(_client, *, name, members, settings):
        captured.update(settings=settings)
        return _group()

    monkeypatch.setattr(client, "create_group", create_group)
    result = _invoke(["group", "create", "Ops", "-m", "Dog", "--no-routing"])
    assert result.exit_code == 0, result.output
    assert captured["settings"] == {"smart_routing": False}


def test_an_unset_routing_switch_is_left_alone(monkeypatch, groups_client):
    """The blob is replaced whole, so a flag nobody passed must not travel — it
    would overwrite whatever the room is set to."""
    import app.cli.client.groups as client

    captured: dict = {}

    async def get_group(_client, group_id):
        return _group(settings={"smart_routing": False})

    async def update_group(_client, group_id, fields):
        captured.update(fields=fields)
        return _group(settings=fields.get("settings") or {})

    monkeypatch.setattr(client, "get_group", get_group)
    monkeypatch.setattr(client, "update_group", update_group)
    assert _invoke(["group", "set", "Ops", "--max-hops", "3"]).exit_code == 0
    # Carried through from the read, not rewritten by an unpassed flag.
    assert captured["fields"]["settings"]["smart_routing"] is False


def _kv_lines(stdout: str) -> list[str]:
    """`print_kv` pads to the widest key, so compare on collapsed whitespace."""
    return [" ".join(line.split()) for line in stdout.splitlines()]


def test_show_renders_a_zero_hop_ceiling(monkeypatch, groups_client):
    """`--max-hops 0` stops agent-to-agent replies outright — a real setting, so
    it must not render like a knob nobody ever touched."""
    import app.cli.client.groups as client

    async def get_group(_client, group_id):
        return _group(settings={"max_agent_hops": 0, "web_sender_name": "Operator"})

    monkeypatch.setattr(client, "get_group", get_group)
    result = _invoke(["group", "show", "Ops"])
    assert result.exit_code == 0, result.output
    assert "max_agent_hops: 0" in _kv_lines(result.stdout)


def test_show_renders_routing_on_for_a_room_that_predates_it(
    monkeypatch, groups_client,
):
    """An older group simply has no key, and the server treats that as ON —
    printing `False` would describe the opposite of what the room does."""
    import app.cli.client.groups as client

    async def get_group(_client, group_id):
        return _group(settings={"max_agent_hops": 6})

    monkeypatch.setattr(client, "get_group", get_group)
    result = _invoke(["group", "show", "Ops"])
    assert result.exit_code == 0, result.output
    assert "smart_routing: True" in _kv_lines(result.stdout)


def test_show_renders_routing_that_was_switched_off(monkeypatch, groups_client):
    import app.cli.client.groups as client

    async def get_group(_client, group_id):
        return _group(settings={"smart_routing": False})

    monkeypatch.setattr(client, "get_group", get_group)
    result = _invoke(["group", "show", "Ops"])
    assert result.exit_code == 0, result.output
    assert "smart_routing: False" in _kv_lines(result.stdout)


def test_set_renders_a_zero_hop_ceiling(monkeypatch, groups_client):
    import app.cli.client.groups as client

    async def get_group(_client, group_id):
        return _group(settings={"max_agent_hops": 6})

    async def update_group(_client, group_id, fields):
        return _group(settings=fields.get("settings") or {})

    monkeypatch.setattr(client, "get_group", get_group)
    monkeypatch.setattr(client, "update_group", update_group)
    result = _invoke(["group", "set", "Ops", "--max-hops", "0"])
    assert result.exit_code == 0, result.output
    assert "max_agent_hops: 0" in _kv_lines(result.stdout)


# ── delete and unbind ─────────────────────────────────────────────────────


def test_delete_honours_json_mode(monkeypatch, groups_client):
    """The doc promises `--json` on every subcommand, so a script must never have
    to parse prose out of a stream it expects to be JSON."""
    import app.cli.client.groups as client

    deleted: list[str] = []

    async def delete_group(_client, group_id):
        deleted.append(group_id)

    monkeypatch.setattr(client, "delete_group", delete_group)
    result = _invoke(["--json", "group", "delete", "Ops", "--yes"])
    assert result.exit_code == 0, result.output
    assert deleted == ["g-1"]
    assert json.loads(result.stdout) == {"deleted": True, "group": "Ops"}


# ── history paging ────────────────────────────────────────────────────────


def _capture_list_messages(monkeypatch) -> dict:
    import app.cli.client.groups as client

    captured: dict = {}

    async def list_messages(_client, group_id, *, after=None, limit=100):
        captured.update(after=after, limit=limit)
        return [_message(ordering=7)]

    monkeypatch.setattr(client, "list_messages", list_messages)
    return captured


def test_history_forwards_after_and_limit(monkeypatch, groups_client):
    captured = _capture_list_messages(monkeypatch)
    result = _invoke(["group", "history", "Ops", "--after", "3", "--limit", "10"])
    assert result.exit_code == 0, result.output
    assert captured == {"after": 3, "limit": 10}


def test_history_after_minus_one_survives_to_the_client(monkeypatch, groups_client):
    """`ordering` counts from 0, so `-1` is the only way to ask for a room's
    first post — and it is exactly the value a `>= 0` guard used to swallow."""
    captured = _capture_list_messages(monkeypatch)
    result = _invoke(["group", "history", "Ops", "--after", "-1"])
    assert result.exit_code == 0, result.output
    assert captured["after"] == -1


def test_history_leaves_an_unpassed_after_unset(monkeypatch, groups_client):
    """Unset is its own request: no `after` asks the server for the newest page,
    which is not the same thing as `after=-1`."""
    captured = _capture_list_messages(monkeypatch)
    result = _invoke(["group", "history", "Ops"])
    assert result.exit_code == 0, result.output
    assert captured["after"] is None


def test_list_messages_only_sends_after_when_it_was_given():
    """The client contract behind the two tests above, checked on the wire."""
    import asyncio

    from app.cli.client.groups import list_messages

    calls: list[dict] = []

    class _FakeClient:
        async def get_json(self, path, *, params=None):
            calls.append({"path": path, "params": params})
            return {"messages": []}

    fake = _FakeClient()
    asyncio.run(list_messages(fake, "g-1", after=-1, limit=5))
    assert calls[-1]["params"] == {"after": -1, "limit": 5}

    asyncio.run(list_messages(fake, "g-1", limit=5))
    assert calls[-1]["params"] == {"limit": 5}


# ── follow ────────────────────────────────────────────────────────────────


def _patch_stream(monkeypatch, frames, captured):
    """Replace `Client.stream` with a canned frame sequence."""
    import app.cli.client._base as base

    def stream(self, path):
        captured["path"] = path

        async def _gen():
            for frame in frames:
                yield frame
            raise AssertionError("the follow loop must stop at the deleted frame")

        return _gen()

    monkeypatch.setattr(base.Client, "stream", stream)


def _frame(kind: str, data: dict):
    from app.cli.client._sse import Event

    payload = {"seq": 1, "type": kind, "data": data}
    return Event(type=kind, data=payload, raw=json.dumps(payload))


def test_history_follow_stops_on_a_deleted_frame(monkeypatch, groups_client):
    import app.cli.client.groups as client

    async def list_messages(_client, group_id, *, after=None, limit=100):
        return [_message(ordering=5, content="status?")]

    monkeypatch.setattr(client, "list_messages", list_messages)
    captured: dict = {}
    _patch_stream(
        monkeypatch,
        [
            _frame("agent_status", {"profile": "Dog", "agent_name": "Rex", "state": "thinking"}),
            _frame("message", {
                "id": "m-2", "ordering": 6, "sender_kind": "agent",
                "sender_name": "Rex", "content": "14:20 here.",
            }),
            _frame("deleted", {}),
        ],
        captured,
    )

    result = _invoke(["group", "history", "Ops", "--follow"])
    assert result.exit_code == 0, result.output
    # The tail resumes after the last row already printed, so nothing repeats.
    assert captured["path"] == "/api/group-chats/g-1/stream?since=5"
    assert "[#5" in result.stdout and "status?" in result.stdout
    assert "Rex (agent): 14:20 here." in result.stdout
    # agent_status is progress, not content: never on stdout.
    assert "thinking" not in result.stdout


def test_history_follow_json_is_one_homogeneous_frame_stream(monkeypatch, groups_client):
    """The replayed page and the live tail share a shape, so one jq filter reads
    the whole run."""
    import app.cli.client.groups as client

    async def list_messages(_client, group_id, *, after=None, limit=100):
        return [_message(ordering=5)]

    monkeypatch.setattr(client, "list_messages", list_messages)
    captured: dict = {}
    _patch_stream(
        monkeypatch,
        [_frame("message", {"id": "m-2", "ordering": 6}), _frame("deleted", {})],
        captured,
    )

    result = _invoke(["--json", "group", "history", "Ops", "--follow"])
    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert [line["type"] for line in lines] == ["message", "message", "deleted"]
    assert lines[0]["data"]["ordering"] == 5
    assert lines[1]["data"]["ordering"] == 6
