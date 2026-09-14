"""The wizard's step grammar: what the user said → what the server stores.

The web wizard builds its payload keys out of provider TOMLs and tool catalogs
that a form has already read. A person dictating answers into a chat window has
none of that, so this layer accepts the short form (``provider=anthropic
model=claude-sonnet-5 api_key=sk-...``) and expands it into the exact keys
``POST /api/config/setup`` persists — including the ones that are easy to forget
and silent when missing, like the opt-in toggle beside a vision model.

The validation half exists because every mistake here is expensive later: a
mistyped model name produces a profile that answers nothing, a missing channel
field comes back as a ``channel_errors`` entry long after the user has gone, and
an OAuth sign-in method is dropped by the server without explanation.
"""

from __future__ import annotations

import pytest

from app.cli import wizard_steps as steps

# Shaped like GET /api/llm/providers.
_PROVIDERS = [
    {
        "name": "anthropic",
        "auth_methods": [
            {
                "id": "api_key", "kind": "api_key", "is_default": True,
                "fields": {"api_key": {"required": True, "secret": True}},
            },
            {
                "id": "claude_oauth", "kind": "oauth", "is_default": False, "fields": {},
            },
        ],
    },
    {
        "name": "ollama",
        "auth_methods": [{"id": "none", "kind": "none", "is_default": True, "fields": {}}],
    },
]

# Shaped like GET /api/config/channel-catalog.
_CATALOG = {
    "telegram": {
        "channel": {
            "modes": [
                {"id": "bot", "fields": {"bot_token": {"required": True, "secret": True}}},
                {"id": "userbot", "setup_kind": "qr",
                 "fields": {"api_id": {"required": True}, "api_hash": {"required": True}}},
                {"id": "future", "implemented": False, "fields": {}},
            ],
        },
    },
}


# ── pairs ────────────────────────────────────────────────────────────────


def test_a_value_may_contain_an_equals_sign() -> None:
    """API keys and connection strings routinely do."""
    assert steps.parse_pairs(["api_key=sk-a=b=c"]) == {"api_key": "sk-a=b=c"}


@pytest.mark.parametrize("bad", ["novalue", "=novalue"])
def test_a_pair_without_a_key_is_refused(bad) -> None:
    with pytest.raises(steps.StepInputError):
        steps.parse_pairs([bad])


# ── llm ──────────────────────────────────────────────────────────────────


def test_the_short_form_expands_into_the_keys_the_server_stores() -> None:
    payload = steps.apply_llm({}, {
        "provider": "anthropic", "model": "claude-sonnet-5", "api_key": "sk-ant",
    })
    assert payload == {
        "default_provider": "anthropic",
        "model_group.high": "anthropic/claude-sonnet-5",
        "anthropic.api_key": "sk-ant",
    }


def test_a_model_may_name_its_own_provider() -> None:
    payload = steps.apply_llm({}, {"provider": "anthropic", "low_model": "groq/llama-3"})
    assert payload["model_group.low"] == "groq/llama-3"


def test_a_bare_model_with_no_provider_is_refused_rather_than_guessed() -> None:
    with pytest.raises(steps.StepInputError, match="provider="):
        steps.apply_llm({}, {"model": "claude-sonnet-5"})


def test_an_opt_in_role_gets_its_toggle_too() -> None:
    """The web form always emits this beside the model. Without it the model is
    stored and never used, which looks like the setting simply did not work."""
    payload = steps.apply_llm({}, {"provider": "anthropic", "vision_model": "claude-sonnet-5"})
    assert payload["model_group.vision"] == "anthropic/claude-sonnet-5"
    assert payload["model_group.vision.enabled"] == "true"


def test_answers_merge_across_turns() -> None:
    """The provider and model arrive in one message, the key in the next."""
    first = steps.apply_llm({}, {"provider": "anthropic", "model": "claude-sonnet-5"})
    second = steps.apply_llm(first, {"api_key": "sk-ant"})
    assert second["model_group.high"] == "anthropic/claude-sonnet-5"
    assert second["anthropic.api_key"] == "sk-ant"


def test_a_dotted_key_passes_through_untouched() -> None:
    """An agent that read the provider TOML may prefer to be literal."""
    payload = steps.apply_llm({}, {"anthropic.setup_token": "st-1", "provider": "anthropic"})
    assert payload["anthropic.setup_token"] == "st-1"


def test_reasoning_effort_attaches_to_the_main_model() -> None:
    payload = steps.apply_llm({}, {"provider": "anthropic", "reasoning_effort": "high"})
    assert payload["model_group.high.reasoning_effort"] == "high"


def test_an_unknown_provider_names_the_command_that_lists_them() -> None:
    result = steps.validate_llm({"default_provider": "nope"}, _PROVIDERS)
    assert not result.ok
    assert "cremind llm providers list" in result.errors[0]
    assert "anthropic" in result.errors[0]


def test_a_browser_sign_in_is_refused_with_the_reason() -> None:
    """The server drops these silently (they need a browser round-trip the CLI
    cannot perform), which leaves a profile that cannot reach its provider and
    no explanation anywhere."""
    payload = {"default_provider": "anthropic", "anthropic.auth_method": "claude_oauth"}
    result = steps.validate_llm(payload, _PROVIDERS)
    assert not result.ok
    assert "browser" in result.errors[0]
    assert "Settings → LLM Providers" in result.errors[0]


def test_a_model_the_provider_does_not_offer_is_refused_with_examples() -> None:
    payload = {"default_provider": "anthropic", "model_group.high": "anthropic/typo-5"}
    result = steps.validate_llm(payload, _PROVIDERS, {"anthropic": ["claude-sonnet-5"]})
    assert not result.ok
    assert "claude-sonnet-5" in result.errors[0]


def test_an_empty_model_list_does_not_block_the_answer() -> None:
    """A provider whose catalogue could not be listed (no key yet, a network
    blip) must not stop the user from recording what they typed."""
    payload = {"default_provider": "anthropic", "model_group.high": "anthropic/claude-sonnet-5"}
    assert steps.validate_llm(payload, _PROVIDERS, {"anthropic": []}).ok


def test_the_required_credential_is_reported_as_missing_not_as_an_error() -> None:
    """A key can legitimately come from the server's environment, so this
    informs rather than refuses."""
    result = steps.validate_llm(
        {"default_provider": "anthropic", "model_group.high": "anthropic/claude-sonnet-5"},
        _PROVIDERS,
    )
    assert result.ok
    assert any("api_key" in m for m in result.missing)


def test_a_provider_that_needs_no_credential_reports_nothing_missing() -> None:
    result = steps.validate_llm(
        {"default_provider": "ollama", "model_group.high": "ollama/llama-3"}, _PROVIDERS,
    )
    assert result.ok and result.missing == []


def test_a_missing_main_model_is_always_reported() -> None:
    """It is the one answer whose absence makes the profile answer nothing."""
    missing = steps.llm_missing({"default_provider": "anthropic", "anthropic.api_key": "k"}, _PROVIDERS)
    assert any("main model" in m for m in missing)


# ── tools ────────────────────────────────────────────────────────────────


_TOOLS = [
    {"tool_id": "web_search", "required_fields": {"API_KEY": {"required": True}},
     "requires_feature": "browser"},
    {"tool_id": "system_file", "toggle_locked": True, "required_fields": {}},
]


def test_the_four_kinds_of_tool_setting_land_in_the_right_place() -> None:
    payload = steps.apply_tools({}, {
        "web_search.enabled": "true",
        "web_search.API_KEY": "k",
        "web_search._arg.depth": "3",
        "web_search._description": "search the web",
    })
    assert payload["tool_configs"]["web_search"] == {
        "_enabled": "true", "API_KEY": "k", "_arg.depth": "3",
    }
    assert payload["agent_configs"]["web_search"] == {"description": "search the web"}


def test_a_setting_without_a_tool_id_is_refused() -> None:
    with pytest.raises(steps.StepInputError, match="cremind tools list"):
        steps.apply_tools({}, {"enabled": "true"})


def test_an_unknown_tool_is_an_error_that_explains_the_skill_prefix() -> None:
    result = steps.validate_tools({"tool_configs": {"nope": {"_enabled": "true"}}}, _TOOLS)
    assert not result.ok
    assert "admin__" in result.errors[0]


def test_a_locked_tool_cannot_be_disabled() -> None:
    result = steps.validate_tools({"tool_configs": {"system_file": {"_enabled": "false"}}}, _TOOLS)
    assert not result.ok


def test_enabling_a_tool_that_needs_packages_warns_about_the_wait() -> None:
    """The install happens inside the setup POST, so this is the difference
    between "it hung" and "it is installing"."""
    result = steps.validate_tools(
        {"tool_configs": {"web_search": {"_enabled": "true", "API_KEY": "k"}}}, _TOOLS,
    )
    assert result.ok
    assert any("pip-install" in w for w in result.warnings)


def test_a_missing_tool_variable_is_reported_but_not_fatal() -> None:
    result = steps.validate_tools({"tool_configs": {"web_search": {"_enabled": "true"}}}, _TOOLS)
    assert result.ok
    assert "web_search.API_KEY" in result.missing


# ── memory ───────────────────────────────────────────────────────────────


_SCHEMA = {"groups": {"memory": {"fields": {"enabled": {"type": "boolean"}}}}}


@pytest.mark.parametrize(("said", "stored"), [("true", "true"), ("yes", "true"), ("off", "false")])
def test_the_answer_is_normalised_to_what_the_server_stores(said, stored) -> None:
    assert steps.apply_memory({}, {"enabled": said}) == {"memory.enabled": stored}


def test_any_other_schema_key_can_be_set_too() -> None:
    assert steps.apply_memory({}, {"memory.enabled": "true"}) == {"memory.enabled": "true"}


def test_a_key_the_schema_does_not_declare_is_refused() -> None:
    """The server skips unknown keys with a log line, so a typo would look like
    it worked."""
    result = steps.validate_memory({"memory.nope": "true"}, _SCHEMA)
    assert not result.ok
    assert "cremind config schema" in result.errors[0]


# ── channels ─────────────────────────────────────────────────────────────


def test_a_channel_is_built_in_the_shape_the_server_takes() -> None:
    entries = steps.apply_channel([], {
        "channel_type": "telegram", "mode": "bot", "bot_token": "123:abc",
    }, _CATALOG)
    assert entries == [{
        "channel_type": "telegram",
        "mode": "bot",
        "auth_mode": "none",
        "response_mode": "normal",
        "enabled": True,
        "config": {"subscribe_auth": "open", "bot_token": "123:abc"},
    }]


def test_the_mode_defaults_to_the_platforms_first_implemented_one() -> None:
    entries = steps.apply_channel([], {"channel_type": "telegram", "bot_token": "x"}, _CATALOG)
    assert entries[0]["mode"] == "bot"


def test_group_chats_is_a_real_boolean_not_the_string() -> None:
    """The adapters branch on the type."""
    entries = steps.apply_channel(
        [], {"channel_type": "telegram", "group_chats": "true", "bot_token": "x"}, _CATALOG,
    )
    assert entries[0]["config"]["group_chats_enabled"] is True


def test_naming_the_same_platform_twice_replaces_it() -> None:
    """The server allows one channel per type, so a second entry would be
    rejected at finish rather than here."""
    entries = steps.apply_channel([], {"channel_type": "telegram", "bot_token": "a"}, _CATALOG)
    entries = steps.apply_channel(entries, {"channel_type": "telegram", "bot_token": "b"}, _CATALOG)
    assert len(entries) == 1
    assert entries[0]["config"]["bot_token"] == "b"


def test_a_channel_with_no_type_is_refused() -> None:
    with pytest.raises(steps.StepInputError, match="cremind channels catalog"):
        steps.apply_channel([], {"bot_token": "x"}, _CATALOG)


def test_a_missing_required_field_is_a_hard_error() -> None:
    """The server would accept the setup and report this in ``channel_errors``
    minutes later, by which time the user has stopped reading."""
    result = steps.validate_channels(
        [{"channel_type": "telegram", "mode": "bot", "config": {}}], _CATALOG,
    )
    assert not result.ok
    assert "bot_token=" in result.errors[0]


def test_an_unimplemented_mode_is_refused() -> None:
    result = steps.validate_channels(
        [{"channel_type": "telegram", "mode": "future", "config": {}}], _CATALOG,
    )
    assert not result.ok


def test_a_mode_that_needs_pairing_warns_about_the_extra_step() -> None:
    result = steps.validate_channels([{
        "channel_type": "telegram", "mode": "userbot",
        "config": {"api_id": "1", "api_hash": "h"},
    }], _CATALOG)
    assert result.ok
    assert any("pairing" in w for w in result.warnings)
    assert steps.mode_needs_pairing(_CATALOG, "telegram", "userbot") is True
    assert steps.mode_needs_pairing(_CATALOG, "telegram", "bot") is False


# ── the questions ────────────────────────────────────────────────────────


@pytest.mark.parametrize("step", ["llm", "tools", "memory", "channels"])
def test_every_step_can_say_what_it_needs_and_what_skipping_costs(step) -> None:
    """This block is what the agent relays to the user, so it has to name the
    commands that list real values rather than inviting a guess."""
    text = steps.questions(step, "javis")
    assert f"step {step}" in text
    assert "cremind profile wizard skip javis " + step in text
    assert "cremind profile wizard set javis " + step in text

    payload = steps.questions_json(step, "javis")
    assert payload["step"] == step
    assert payload["fields"] and payload["skip_effect"]


def test_the_llm_questions_say_what_skipping_actually_costs() -> None:
    text = steps.questions("llm", "javis")
    assert "REQUIRED" in text
    assert "cremind llm providers list" in text
