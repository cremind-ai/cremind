"""`cremind channels groups` — the picker commands and the policy edits.

The commands do function-body imports (`from app.cli.client.channels import
...`), so every stub goes on the **client module**, not on the command module —
the name is looked up when the command runs.

What is worth pinning: a group reference is resolved to an id before anything is
changed, "this platform cannot list groups" reads as an explanation rather than
an empty table, and the allow/deny edits compute the new policy from the current
one instead of overwriting it.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner


def _group(**overrides) -> dict:
    payload = {
        "id": "g-1",
        "channel_id": "ch-1",
        "platform_chat_id": "-1001",
        "title": "Ops room",
        "status": "pending",
        "discovered_via": "message",
        "conversation_id": None,
        "settings": {
            "member_policy": {"mode": "everyone", "allow": [], "deny": []},
            "respond_mode": "mention_or_relevant",
            "max_agent_posts_per_minute": 20,
            "max_consecutive_bot_messages": 8,
        },
        "members": [],
        "member_count": 0,
        "capabilities": {
            "roster": True, "join_events": True, "bot_flag": True, "listing": True,
        },
        "last_message_at": None,
        "roster_refreshed_at": None,
        "created_at": 0.0,
        "updated_at": 0.0,
    }
    payload.update(overrides)
    return payload


def _invoke(args: list[str], **kwargs):
    from app.cli.main import app

    return CliRunner().invoke(app, ["--token", "t", *args], **kwargs)


@pytest.fixture
def resolved(monkeypatch):
    """Every `<group>` reference resolves to one known row."""
    import app.cli.client.channels as client

    captured: dict = {}

    async def resolve_channel_group(_client, channel_id, ref):
        captured["channel_id"] = channel_id
        captured["ref"] = ref
        return _group()

    monkeypatch.setattr(client, "resolve_channel_group", resolve_channel_group)
    return captured


# ── available ─────────────────────────────────────────────────────────────


def test_available_lists_what_the_account_is_in(monkeypatch):
    import app.cli.client.channels as client

    async def list_available_channel_groups(_client, channel_id):
        assert channel_id == "ch-1"
        return {
            "supported": True,
            "groups": [
                {
                    "platform_chat_id": "-1001", "title": "Ops room",
                    "member_count": 6, "tracked": {"id": "g-1", "status": "approved"},
                },
                {
                    "platform_chat_id": "-2002", "title": "Lunch",
                    "member_count": 12, "tracked": None,
                },
            ],
        }

    monkeypatch.setattr(
        client, "list_available_channel_groups", list_available_channel_groups,
    )
    result = _invoke(["channels", "groups", "available", "ch-1"])
    assert result.exit_code == 0, result.output
    assert "Ops room" in result.stdout and "Lunch" in result.stdout
    # A group already enabled says so rather than being offered again.
    assert "approved" in result.stdout


def test_available_explains_a_platform_that_cannot_list(monkeypatch):
    """An empty table would read as "you are in no groups", which is a different
    and wrong statement — a Telegram bot simply cannot be asked."""
    import app.cli.client.channels as client

    async def list_available_channel_groups(_client, _channel_id):
        return {"supported": False, "groups": []}

    monkeypatch.setattr(
        client, "list_available_channel_groups", list_available_channel_groups,
    )
    result = _invoke(["channels", "groups", "available", "ch-1"])
    assert result.exit_code == 0, result.output
    assert "will not list" in result.stdout


def test_available_says_so_when_there_are_none(monkeypatch):
    import app.cli.client.channels as client

    async def list_available_channel_groups(_client, _channel_id):
        return {"supported": True, "groups": []}

    monkeypatch.setattr(
        client, "list_available_channel_groups", list_available_channel_groups,
    )
    result = _invoke(["channels", "groups", "available", "ch-1"])
    assert result.exit_code == 0, result.output
    assert "not in any groups" in result.stdout


def test_available_json_is_parseable(monkeypatch):
    import app.cli.client.channels as client

    async def list_available_channel_groups(_client, _channel_id):
        return {
            "supported": True,
            "groups": [{
                "platform_chat_id": "-1001", "title": "Ops room",
                "member_count": 6, "tracked": None,
            }],
        }

    monkeypatch.setattr(
        client, "list_available_channel_groups", list_available_channel_groups,
    )
    result = _invoke(["--json", "channels", "groups", "available", "ch-1"])
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["groups"][0]["platform_chat_id"] == "-1001"


# ── add ───────────────────────────────────────────────────────────────────


def test_add_enables_a_group_the_account_is_already_in(monkeypatch):
    import app.cli.client.channels as client

    captured: dict = {}

    async def add_channel_group(_client, channel_id, chat_id, *, title="", chat_type=""):
        captured["args"] = (channel_id, chat_id, title)
        return _group(status="approved", discovered_via="picked")

    monkeypatch.setattr(client, "add_channel_group", add_channel_group)
    result = _invoke(["channels", "groups", "add", "ch-1", "--", "-1001"])
    assert result.exit_code == 0, result.output
    assert captured["args"] == ("ch-1", "-1001", "")
    assert "approved" in result.stdout


def test_add_takes_several_chat_ids(monkeypatch):
    """Enabling a handful at once is the normal case after a first listing.

    Note the ``--``: a Telegram or Zalo chat id starts with a minus sign, which
    every CLI parser reads as the start of an option.
    """
    import app.cli.client.channels as client

    seen: list = []

    async def add_channel_group(_client, _channel_id, chat_id, *, title="", chat_type=""):
        seen.append(chat_id)
        return _group(platform_chat_id=chat_id, status="approved")

    monkeypatch.setattr(client, "add_channel_group", add_channel_group)
    result = _invoke(["channels", "groups", "add", "ch-1", "--", "-1001", "-2002"])
    assert result.exit_code == 0, result.output
    assert seen == ["-1001", "-2002"]


def test_a_title_is_only_applied_to_a_single_pick(monkeypatch):
    """One `--title` across several groups would name them all the same."""
    import app.cli.client.channels as client

    titles: list = []

    async def add_channel_group(_client, _channel_id, chat_id, *, title="", chat_type=""):
        titles.append(title)
        return _group(platform_chat_id=chat_id)

    monkeypatch.setattr(client, "add_channel_group", add_channel_group)
    _invoke(["channels", "groups", "add", "ch-1", "--title", "Ops", "--", "-1", "-2"])
    assert titles == ["", ""]

    titles.clear()
    _invoke(["channels", "groups", "add", "ch-1", "--title", "Ops", "--", "-1"])
    assert titles == ["Ops"]


# ── the reference is resolved before anything changes ─────────────────────


def test_approve_resolves_the_reference_first(monkeypatch, resolved):
    import app.cli.client.channels as client

    captured: dict = {}

    async def set_channel_group_status(_client, channel_id, group_id, status):
        captured["call"] = (channel_id, group_id, status)
        return _group(status=status)

    monkeypatch.setattr(
        client, "set_channel_group_status", set_channel_group_status,
    )
    result = _invoke(["channels", "groups", "approve", "ch-1", "Ops room"])
    assert result.exit_code == 0, result.output
    assert resolved["ref"] == "Ops room"
    assert captured["call"] == ("ch-1", "g-1", "approved")
