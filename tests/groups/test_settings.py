"""A group's settings blob: strict validation, and the accessors that read it.

Strict on purpose. These values decide how a room behaves — how deep the agents
may go answering each other, how fast they may post, whether a cheap model gets
to narrow who answers at all — so a malformed cap that was quietly dropped would
be indistinguishable from "the room ignored my setting", a confusing way to find
out about a typo. Every rejection here is a 400 at the API.

The accessors are separate from the validator because they read RAW stored
blobs: a room created before a knob existed simply does not have the key, and
must take the default rather than reading as "turned off".
"""

from __future__ import annotations

import pytest

pytest.importorskip("a2a")

from app.groups.constants import (  # noqa: E402
    DEFAULT_MAX_AGENT_HOPS,
    DEFAULT_MAX_AGENT_POSTS_PER_MINUTE,
    DEFAULT_ROUTING_ENABLED,
    DEFAULT_WEB_SENDER_NAME,
    ROUTING_SETTING_KEY,
)
from app.groups.settings import (  # noqa: E402
    default_settings,
    max_agent_hops,
    max_agent_posts_per_minute,
    normalize_settings,
    routing_enabled,
    web_sender_name,
)


# ── defaults ────────────────────────────────────────────────────────────────


def test_defaults_are_a_usable_room() -> None:
    """A group created with no settings still has a hop cap and a flood cap —
    the two things that stop a room of agents talking forever."""
    assert default_settings() == {
        "max_agent_hops": DEFAULT_MAX_AGENT_HOPS,
        "max_agent_posts_per_minute": DEFAULT_MAX_AGENT_POSTS_PER_MINUTE,
        "web_sender_name": DEFAULT_WEB_SENDER_NAME,
        ROUTING_SETTING_KEY: DEFAULT_ROUTING_ENABLED,
    }
    assert normalize_settings(None) == default_settings()
    assert normalize_settings({}) == default_settings()


def test_a_non_object_blob_is_rejected() -> None:
    for raw in ("nope", [1, 2], 7):
        with pytest.raises(ValueError, match="settings must be an object"):
            normalize_settings(raw)


def test_unknown_keys_are_dropped_rather_than_stored() -> None:
    """The blob is replaced wholesale on every PATCH, so anything kept here
    would outlive the UI field that wrote it."""
    out = normalize_settings({"colour": "blue", "max_agent_hops": 3})
    assert "colour" not in out
    assert out["max_agent_hops"] == 3


# ── the numeric caps ────────────────────────────────────────────────────────


@pytest.mark.parametrize("value,expected", [(0, 0), (1, 1), ("4", 4), (100, 100)])
def test_max_agent_hops_accepts_its_range(value, expected) -> None:
    assert normalize_settings({"max_agent_hops": value})["max_agent_hops"] == expected


def test_max_agent_hops_rejects_what_it_cannot_mean() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        normalize_settings({"max_agent_hops": -1})
    with pytest.raises(ValueError, match="must be 100 or less"):
        normalize_settings({"max_agent_hops": 101})
    with pytest.raises(ValueError, match="must be a whole number"):
        normalize_settings({"max_agent_hops": "soon"})


def test_max_agent_posts_per_minute_has_its_own_ceiling() -> None:
    assert normalize_settings(
        {"max_agent_posts_per_minute": 600},
    )["max_agent_posts_per_minute"] == 600
    with pytest.raises(ValueError, match="must be 600 or less"):
        normalize_settings({"max_agent_posts_per_minute": 601})


def test_an_explicit_null_cap_keeps_the_default() -> None:
    """The UI sends the whole blob back with empty fields as null."""
    out = normalize_settings({"max_agent_hops": None, "max_agent_posts_per_minute": None})
    assert out["max_agent_hops"] == DEFAULT_MAX_AGENT_HOPS
    assert out["max_agent_posts_per_minute"] == DEFAULT_MAX_AGENT_POSTS_PER_MINUTE


# ── routing ─────────────────────────────────────────────────────────────────


def test_the_routing_key_is_spelled_the_same_in_both_modules() -> None:
    """``app.groups.constants`` repeats the key so the settings blob can be read
    without importing the routing module (and its LLM stack) — two spellings
    would leave the fan-out gating on a key nobody ever writes."""
    from app.groups.routing import ROUTING_SETTING_KEY as ROUTING_KEY_IN_ROUTING

    assert ROUTING_SETTING_KEY == ROUTING_KEY_IN_ROUTING


def test_routing_is_on_unless_it_is_turned_off() -> None:
    assert normalize_settings({})[ROUTING_SETTING_KEY] is True
    assert normalize_settings({ROUTING_SETTING_KEY: False})[ROUTING_SETTING_KEY] is False
    assert normalize_settings({ROUTING_SETTING_KEY: 1})[ROUTING_SETTING_KEY] is True


def test_a_null_routing_field_keeps_the_default() -> None:
    """The UI round-trips the whole blob with unset fields as null, and this one
    defaults to ON — reading null as False would switch routing off every time
    somebody saved an unrelated setting."""
    assert normalize_settings({ROUTING_SETTING_KEY: None})[ROUTING_SETTING_KEY] is True


def test_the_routing_accessor_falls_back_for_a_room_that_predates_it() -> None:
    assert routing_enabled(None) is DEFAULT_ROUTING_ENABLED
    assert routing_enabled({}) is DEFAULT_ROUTING_ENABLED
    assert routing_enabled({ROUTING_SETTING_KEY: False}) is False
    assert routing_enabled({ROUTING_SETTING_KEY: True}) is True


# ── the name the web poster speaks under ────────────────────────────────────


def test_the_web_sender_name_is_trimmed_and_never_blank() -> None:
    assert normalize_settings({"web_sender_name": "  Lee  "})["web_sender_name"] == "Lee"
    # Blanked in the UI: fall back rather than posting under an empty name.
    assert normalize_settings({"web_sender_name": "  "})["web_sender_name"] == (
        DEFAULT_WEB_SENDER_NAME
    )


# ── the accessor helpers ────────────────────────────────────────────────────


def test_the_helpers_fall_back_when_a_group_predates_a_setting() -> None:
    """They read raw stored blobs, not normalized ones — an older row simply
    does not have the key."""
    assert max_agent_hops(None) == DEFAULT_MAX_AGENT_HOPS
    assert max_agent_hops({}) == DEFAULT_MAX_AGENT_HOPS
    assert max_agent_hops({"max_agent_hops": 2}) == 2
    assert max_agent_hops({"max_agent_hops": 0}) == 0

    assert max_agent_posts_per_minute(None) == DEFAULT_MAX_AGENT_POSTS_PER_MINUTE
    assert max_agent_posts_per_minute({"max_agent_posts_per_minute": 5}) == 5

    assert web_sender_name(None) == DEFAULT_WEB_SENDER_NAME
    assert web_sender_name({"web_sender_name": ""}) == DEFAULT_WEB_SENDER_NAME
    assert web_sender_name({"web_sender_name": " Lee "}) == "Lee"
