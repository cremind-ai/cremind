"""`cremind channels groups` — the picker commands and the policy edits.

The commands do function-body imports (`from app.cli.client.channels import
...`), so every stub goes on the **client module**, not on the command module —
the name is looked up when the command runs.

What is worth pinning: a group reference is resolved to an id before anything is
changed, "this platform cannot list groups" reads as an explanation rather than
an empty table, the allow/deny edits compute the new policy from the current
one instead of overwriting it, and `brakes` sends exactly the settings keys its
flags name — or, with no flag, sends nothing at all.
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


# ── brakes ────────────────────────────────────────────────────────────────
#
# The two loop brakes lived in `channel_groups.settings` from the day the
# feature shipped and nothing an operator can type ever set them. They became
# worth reaching the moment two of somebody's own Telegram bots could hear
# each other: `max_consecutive_bot_messages` is what ends that conversation,
# and at its default of 8 it ends after about four rounds each, so raising it
# is the whole reason this command exists.
#
# What is pinned here is therefore the two ways it can quietly do nothing.
# Asking what the caps are must not turn into an edit — an empty patch would
# round-trip to the same numbers while stamping `updated_at` on the row. And a
# settings patch is merged server-side one level deep, so the request has to
# carry exactly the keys the flags name: a misspelt or stray key is accepted,
# changes no cap, and still reports success.


def _kv(text: str) -> dict[str, str]:
    """The aligned `key: value` block `print_kv` writes, read back as a dict."""
    pairs: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            pairs[key.strip()] = value.strip()
    return pairs


@pytest.fixture
def patches(monkeypatch):
    """Record every settings patch sent, and answer with the merged row.

    The server is the thing that merges, so the stub merges too — otherwise a
    command that patched one cap and printed the response back would look like
    it had wiped the other one.
    """
    import app.cli.client.channels as client

    sent: list[dict] = []

    async def set_channel_group_settings(_client, channel_id, group_id, settings):
        sent.append(
            {"channel_id": channel_id, "group_id": group_id, "settings": settings},
        )
        return _group(settings={**_group()["settings"], **settings})

    monkeypatch.setattr(
        client, "set_channel_group_settings", set_channel_group_settings,
    )
    return sent


def test_brakes_with_no_flag_reads_without_writing(resolved, patches):
    """No flag is a question, and a question must not count as an edit."""
    result = _invoke(["channels", "groups", "brakes", "ch-1", "Ops room"])
    assert result.exit_code == 0, result.output
    assert resolved["ref"] == "Ops room"
    assert patches == []
    # Both caps are named, not just the one an operator came looking for —
    # they interact, and seeing one of them alone invites tuning the wrong one.
    assert _kv(result.stdout) == {
        "id": "g-1",
        "title": "Ops room",
        "posts_per_minute": "20",
        "bot_streak": "8",
    }


@pytest.mark.parametrize("flags,settings", [
    (["--posts-per-minute", "60"], {"max_agent_posts_per_minute": 60}),
    (["--bot-streak", "40"], {"max_consecutive_bot_messages": 40}),
    (
        ["--posts-per-minute", "60", "--bot-streak", "40"],
        {
            "max_agent_posts_per_minute": 60,
            "max_consecutive_bot_messages": 40,
        },
    ),
])
def test_each_flag_sends_its_own_settings_key_and_no_other(
    resolved, patches, flags, settings,
):
    """The payload is asserted whole, key for key.

    A one-level merge forgives everything: a cap sent under the wrong name is
    stored beside the real one, the command prints a cheerful block, and the
    agent keeps pausing exactly where it did before. An omitted flag must send
    no key at all, so that tuning one cap cannot reset the other to whatever
    the CLI happened to read a moment earlier.
    """
    result = _invoke(["channels", "groups", "brakes", "ch-1", "Ops room", *flags])
    assert result.exit_code == 0, result.output
    assert patches == [
        {"channel_id": "ch-1", "group_id": "g-1", "settings": settings},
    ]


def test_an_edit_prints_the_row_the_server_returned(resolved, patches):
    """The read-back is the server's row, not the flags echoed at the operator.

    Raising the bot streak leaves the rate cap alone, and the block has to say
    so — that is how somebody confirms the edit landed on the cap they meant.
    """
    result = _invoke(
        ["channels", "groups", "brakes", "ch-1", "Ops room", "--bot-streak", "40"],
    )
    assert result.exit_code == 0, result.output
    printed = _kv(result.stdout)
    assert printed["bot_streak"] == "40"
    assert printed["posts_per_minute"] == "20"


@pytest.mark.parametrize("flag", ["--posts-per-minute", "--bot-streak"])
def test_a_negative_cap_is_refused_before_anything_is_sent(resolved, patches, flag):
    """`--bot-streak -5` parses happily as an integer, so nothing but this
    check stands between a typo and a 400 from the server — or, worse, a
    silently clamped cap. The group is not even resolved, which is what
    `resolved` staying empty pins."""
    result = _invoke(
        ["channels", "groups", "brakes", "ch-1", "Ops room", flag, "-5"],
    )
    assert result.exit_code == 1
    assert "cannot be negative" in result.stderr
    assert resolved == {}
    assert patches == []


def test_brakes_json_is_the_whole_group_row(resolved, patches):
    """JSON mode matches the other groups commands: the row, not a summary of
    it. A read in JSON is still a read."""
    result = _invoke(["--json", "channels", "groups", "brakes", "ch-1", "Ops room"])
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.stdout)
    assert parsed["id"] == "g-1"
    assert parsed["settings"]["max_agent_posts_per_minute"] == 20
    assert parsed["settings"]["max_consecutive_bot_messages"] == 8
    assert patches == []
