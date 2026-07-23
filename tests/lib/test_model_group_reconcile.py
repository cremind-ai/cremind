"""Tests for ``reconcile_model_groups_for_auth`` — clearing model-group
assignments that a provider's new auth method can't serve. Uses the real
``openai.toml`` catalog so it also guards the api_key-vs-codex_oauth model split.
"""

from __future__ import annotations

from typing import Optional

from app.lib.llm.model_group_reconcile import reconcile_model_groups_for_auth


class FakeConfigStorage:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], str] = {}

    def get(self, table: str, key: str, profile: str = "admin") -> Optional[str]:
        return self.rows.get((profile, key))

    def set(self, table: str, key: str, value: str, is_secret: bool = False, profile: str = "admin") -> None:
        self.rows[(profile, key)] = value

    def delete(self, table: str, key: str, profile: str = "admin") -> None:
        self.rows.pop((profile, key), None)


def test_clears_incompatible_openai_group_on_codex_switch():
    st = FakeConfigStorage()
    st.set("llm_config", "model_group.high", "github-copilot/gpt-4.1")
    st.set("llm_config", "model_group.low", "openai/gpt-4.1-mini")  # api_key-only
    cleared = reconcile_model_groups_for_auth(st, "openai", "codex_oauth", profile="admin")
    assert cleared == ["low"]
    assert st.get("llm_config", "model_group.low") is None
    # A group belonging to another provider is untouched.
    assert st.get("llm_config", "model_group.high") == "github-copilot/gpt-4.1"


def test_keeps_codex_eligible_openai_group():
    st = FakeConfigStorage()
    st.set("llm_config", "model_group.high", "openai/gpt-5.4")  # dual auth
    cleared = reconcile_model_groups_for_auth(st, "openai", "codex_oauth", profile="admin")
    assert cleared == []
    assert st.get("llm_config", "model_group.high") == "openai/gpt-5.4"


def test_reverse_direction_clears_codex_only_model_on_api_key_switch():
    st = FakeConfigStorage()
    st.set("llm_config", "model_group.low", "openai/gpt-5.6-sol")  # codex-only
    cleared = reconcile_model_groups_for_auth(st, "openai", "api_key", profile="admin")
    assert cleared == ["low"]
    assert st.get("llm_config", "model_group.low") is None


def test_ignores_groups_of_other_providers():
    st = FakeConfigStorage()
    st.set("llm_config", "model_group.high", "github-copilot/gpt-4.1")
    st.set("llm_config", "model_group.low", "anthropic/claude-opus-4-8")
    cleared = reconcile_model_groups_for_auth(st, "openai", "codex_oauth", profile="admin")
    assert cleared == []
    assert st.get("llm_config", "model_group.high") == "github-copilot/gpt-4.1"
    assert st.get("llm_config", "model_group.low") == "anthropic/claude-opus-4-8"
