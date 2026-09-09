"""``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME`` in the system-var block.

Every shell Cremind spawns - ``exec_shell``, autostart scripts, the built-in
terminal - gets this block, so these two variables are what stop a member
profile's ``claude auth login`` from landing in (and overwriting) the server's
shared login. The pin that matters is therefore not "the variables are present"
but "they point at the PROFILE's home even when only the shared one holds a
login": resolving them the way a *run* resolves them would hand the first
profile to sign in the operator's account to overwrite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.config.coding_cli_homes as cch
import app.config.system_vars as sv
from app.config import runtime_env
from app.config.settings import BaseConfig


@pytest.fixture
def system_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    # raising=False: runtime_env.is_container() ships with the same change.
    monkeypatch.setattr(runtime_env, "is_container", lambda: False, raising=False)
    return tmp_path, home


def test_both_homes_are_emitted_for_a_profile(system_dir):
    env = sv.build_system_env("alice")
    assert env["CLAUDE_CONFIG_DIR"] == str(cch.profile_claude_config_dir("alice"))
    assert env["CODEX_HOME"] == str(cch.profile_codex_home("alice"))


def test_both_homes_are_absent_without_a_profile(system_dir):
    """Nothing to isolate, and pointing a profile-less shell at a profile
    directory would be a lie about who it is running as."""
    env = sv.build_system_env(None)
    assert "CLAUDE_CONFIG_DIR" not in env
    assert "CODEX_HOME" not in env


def test_the_shells_home_is_the_profiles_even_when_only_the_server_signed_in(system_dir):
    """The whole reason these resolve to the profile directory rather than
    through ``resolve_*``: the tool falls back to the shared login, a sign-in
    from a Cremind shell must not."""
    _, home = system_dir
    (home / ".claude").mkdir()
    (home / ".claude" / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "operator-token"}}), encoding="utf-8"
    )
    assert cch.resolve_claude_config_dir("alice").scope == "shared"
    assert sv.build_system_env("alice")["CLAUDE_CONFIG_DIR"] == str(
        cch.profile_claude_config_dir("alice")
    )


def test_two_profiles_get_two_homes(system_dir):
    alice = sv.build_system_env("alice")
    bob = sv.build_system_env("bob")
    assert alice["CODEX_HOME"] != bob["CODEX_HOME"]
    assert Path(alice["CODEX_HOME"]).parts[-3] == "alice"
