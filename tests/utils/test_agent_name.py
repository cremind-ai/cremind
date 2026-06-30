"""Per-profile agent name: defaults, override round-trip, and how it surfaces
as the ``CREMIND_AGENT_NAME`` system variable (env injection + ``$VAR``
rendering used for PERSONA.md)."""

from __future__ import annotations

import pytest

from app.config.settings import BaseConfig
from app.config.system_vars import build_system_env
from app.utils import agent_name as an
from app.utils.message_tokens import resolve_system_var_tokens


@pytest.fixture
def system_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))
    return tmp_path


def test_default_admin_is_cremind():
    assert an.default_agent_name("admin") == "Cremind"


def test_default_other_is_profile_name():
    assert an.default_agent_name("lee") == "lee"


def test_read_falls_back_to_default_when_unset(system_dir):
    assert an.read_agent_name("admin") == "Cremind"
    assert an.read_agent_name("lee") == "lee"


def test_write_then_read_round_trip(system_dir):
    an.write_agent_name("admin", "  Jarvis  ")
    assert an.read_agent_name("admin") == "Jarvis"  # stripped on write


def test_empty_override_uses_default(system_dir):
    an.write_agent_name("lee", "   ")  # strips to empty
    assert an.read_agent_name("lee") == "lee"


def test_system_env_exposes_override(system_dir):
    an.write_agent_name("admin", "Jarvis")
    assert build_system_env("admin")["CREMIND_AGENT_NAME"] == "Jarvis"


def test_persona_token_is_rendered(system_dir):
    assert resolve_system_var_tokens("I am $CREMIND_AGENT_NAME.", "admin") == "I am Cremind."


def test_no_profile_omits_var(system_dir):
    assert "CREMIND_AGENT_NAME" not in build_system_env(None)
