"""The LLM factory selects the Codex transport for auth_method=codex_oauth and
the standard OpenAI client otherwise (app/lib/llm/factory.py)."""

from __future__ import annotations

from typing import Optional

from app.lib.llm.factory import create_llm_provider
from app.lib.llm.openai_codex import CodexLLMProvider
from app.lib.llm.openai import OpenAILLMProvider


class FakeConfigStorage:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], str] = {}

    def get(self, table: str, key: str, profile: str = "admin") -> Optional[str]:
        return self.rows.get((profile, key))

    def set(self, table: str, key: str, value: str, is_secret: bool = False, profile: str = "admin") -> None:
        self.rows[(profile, key)] = value


def test_factory_selects_codex_provider_for_codex_oauth():
    st = FakeConfigStorage()
    st.set("llm_config", "openai.auth_method", "codex_oauth")
    p = create_llm_provider("openai", model_name="gpt-5.6-sol", config_storage=st, profile="admin")
    assert isinstance(p, CodexLLMProvider)
    assert p.provider_name == "openai"
    assert p.model_name == "gpt-5.6-sol"


def test_factory_selects_openai_client_for_api_key():
    st = FakeConfigStorage()
    st.set("llm_config", "openai.api_key", "sk-test")
    p = create_llm_provider("openai", model_name="gpt-5.4", config_storage=st, profile="admin")
    assert isinstance(p, OpenAILLMProvider)
    assert not isinstance(p, CodexLLMProvider)
