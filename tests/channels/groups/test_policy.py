"""The settings blob, and the one question it exists to answer.

``normalize_settings`` is strict on purpose: these values decide who an agent
talks to in a room full of real people, and a malformed policy quietly dropped
would look exactly like "the agent ignored my block".
"""

from __future__ import annotations

import pytest

from app.channels.groups import policy


def test_the_defaults_answer_everyone_when_mentioned_or_relevant():
    assert policy.default_settings() == {
        "member_policy": {"mode": "everyone", "allow": [], "deny": []},
        "respond_mode": "mention_or_relevant",
        "max_agent_posts_per_minute": 20,
        "max_consecutive_bot_messages": 8,
    }


def test_a_blob_from_before_a_knob_existed_gets_the_default():
    settings = policy.normalize_settings({"respond_mode": "mention_only"})
    assert settings["respond_mode"] == "mention_only"
    assert settings["max_agent_posts_per_minute"] == 20


@pytest.mark.parametrize("raw,message", [
    ("not an object", "settings must be an object"),
    ({"member_policy": []}, "member_policy must be an object"),
    ({"member_policy": {"mode": "sometimes"}}, "member_policy.mode must be"),
    ({"member_policy": {"mode": "everyone", "deny": "u1"}}, "must be a list"),
    ({"respond_mode": "whenever"}, "respond_mode must be"),
    ({"max_agent_posts_per_minute": "lots"}, "must be a whole number"),
    ({"max_agent_posts_per_minute": -1}, "cannot be negative"),
    ({"max_agent_posts_per_minute": 10_000}, "600 or less"),
])
def test_something_unusable_is_refused_with_a_reason(raw, message):
    with pytest.raises(ValueError, match=message):
        policy.normalize_settings(raw)


def test_platform_ids_that_arrive_as_numbers_are_settled_to_strings():
    """Some clients send a Telegram user id as an int; every comparison
    downstream is on strings."""
    settings = policy.normalize_settings(
        {"member_policy": {"mode": "everyone", "deny": [1644772063, "  x  ", ""]}},
    )
    assert settings["member_policy"]["deny"] == ["1644772063", "x"]


def test_switching_mode_keeps_the_list_you_are_not_using():
    """Flipping to "only selected" and back must not lose a curated deny list."""
    stored = policy.normalize_settings({"member_policy": {
        "mode": "everyone", "allow": ["u-boss"], "deny": ["u-spam"],
    }})
    merged = policy.merge_settings(stored, {"respond_mode": "mention_only"})
    assert merged["member_policy"]["deny"] == ["u-spam"]
    assert merged["member_policy"]["allow"] == ["u-boss"]
    assert merged["respond_mode"] == "mention_only"


# ── who may be answered ───────────────────────────────────────────────────


def test_everyone_mode_answers_anybody_not_denied():
    settings = policy.normalize_settings({"member_policy": {
        "mode": "everyone", "deny": ["u-spam"],
    }})
    assert policy.member_allowed(settings, "u-alexa") is True
    assert policy.member_allowed(settings, "u-spam") is False


def test_selected_mode_answers_only_the_allow_list():
    settings = policy.normalize_settings({"member_policy": {
        "mode": "selected", "allow": ["u-boss"],
    }})
    assert policy.member_allowed(settings, "u-boss") is True
    assert policy.member_allowed(settings, "u-alexa") is False


def test_an_empty_allow_list_answers_nobody():
    """What "only these people" means when the list is empty — and recoverable
    in one click, unlike the other reading."""
    settings = policy.normalize_settings({"member_policy": {"mode": "selected"}})
    assert policy.member_allowed(settings, "u-anyone") is False


@pytest.mark.parametrize("listed,arrived", [
    ("1555@s.whatsapp.net", "99@lid"),
    ("99@lid", "1555@s.whatsapp.net"),
])
def test_a_rule_matches_whichever_id_the_platform_reported(listed, arrived):
    """One WhatsApp account is two JIDs, and which one arrives depends on the
    sender's device. A rule written against either has to match both."""
    settings = policy.normalize_settings({"member_policy": {
        "mode": "everyone", "deny": [listed],
    }})
    allowed = policy.member_allowed(
        settings, arrived, ["1555@s.whatsapp.net", "99@lid"],
    )
    assert allowed is False


def test_a_sender_with_no_id_at_all_is_not_answered():
    assert policy.member_allowed(policy.default_settings(), "", []) is False


def test_the_stored_row_and_the_runtime_gate_agree():
    """The UI renders its switch from ``member_responds``; if the two ever
    disagreed the switch would lie about what the agent does."""
    settings = policy.normalize_settings({"member_policy": {
        "mode": "everyone", "deny": ["99@lid"],
    }})
    member = {"member_id": "1555@s.whatsapp.net", "alt_ids": ["99@lid"]}
    assert policy.member_responds(settings, member) is False
    assert policy.member_allowed(
        settings, member["member_id"], member["alt_ids"],
    ) is False
