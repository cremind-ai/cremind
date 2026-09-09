"""Per-profile ``claude`` / ``codex`` CLI homes.

The invariant these guard is a multi-profile one: a profile signs in to its own
directory, borrows the server's login when it has none of its own, and can never
write over that shared login by signing in. Everything else here exists because
the credential files are written by another process and are therefore allowed to
be missing, truncated or half-written at any moment - a bad file must read as
"not signed in", never as an exception out of a status endpoint.

Isolation is by environment only (``BaseConfig.CREMIND_SYSTEM_DIR``, ``HOME`` /
``USERPROFILE``, ``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME``), which is the point of
the module reading those live: the code this replaced could only be kept off the
developer's real ``~/.claude`` by monkeypatching private module constants.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

import app.config.coding_cli_homes as cch
from app.config import runtime_env
from app.config.settings import BaseConfig


def _set_container(monkeypatch, value: bool) -> None:
    """Pretend we are (or are not) inside Docker/Kubernetes.

    ``raising=False`` because ``runtime_env.is_container`` is added by the same
    change as this module and the two land independently; patching it here is
    also what pins that the shared homes ask *that* function rather than
    re-deriving the container answer for themselves.
    """
    monkeypatch.setattr(runtime_env, "is_container", lambda: value, raising=False)


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """A System Directory and a ``~`` that are both this test's alone."""
    system_dir = tmp_path / "system"
    home = tmp_path / "home"
    system_dir.mkdir()
    home.mkdir()
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(system_dir))
    # posixpath.expanduser reads HOME, ntpath.expanduser reads USERPROFILE.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _set_container(monkeypatch, False)
    assert Path.home() == home, "the fixture must own ~ before any test reads it"
    return system_dir, home


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _sign_in_claude(config_dir: Path, token: str = "sk-ant-oat-01") -> None:
    _write_json(config_dir / ".credentials.json", {"claudeAiOauth": {"accessToken": token}})


def _sign_in_codex(home: Path, id_token: str = "") -> None:
    _write_json(home / "auth.json", {"OPENAI_API_KEY": None, "tokens": {"id_token": id_token}})


def _id_token(claims: dict) -> str:
    """A JWT-shaped string carrying ``claims``, base64url with the padding
    stripped exactly as a real issuer emits it."""
    body = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).decode("ascii").rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{body}.not-a-signature"


# -- Paths ---------------------------------------------------------------------


def test_profile_homes_live_under_the_profile_directory(homes):
    system_dir, _ = homes
    assert cch.profile_cli_root("alice") == system_dir / "alice" / "coding-cli"
    assert cch.profile_claude_config_dir("alice") == system_dir / "alice" / "coding-cli" / "claude"
    assert cch.profile_codex_home("alice") == system_dir / "alice" / "coding-cli" / "codex"


def test_two_profiles_never_share_a_home(homes):
    assert cch.profile_claude_config_dir("alice") != cch.profile_claude_config_dir("bob")
    assert cch.profile_codex_home("alice") != cch.profile_codex_home("bob")


def test_shared_homes_follow_the_environment(homes, monkeypatch, tmp_path):
    """The container images and the Helm chart state the homes this way."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "stated-claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "stated-codex"))
    assert cch.shared_claude_config_dir() == tmp_path / "stated-claude"
    assert cch.shared_codex_home() == tmp_path / "stated-codex"


def test_shared_homes_default_to_the_user_home_on_a_native_install(homes):
    _, home = homes
    assert cch.shared_claude_config_dir() == home / ".claude"
    assert cch.shared_codex_home() == home / ".codex"


def test_shared_homes_default_under_the_system_directory_in_a_container(homes, monkeypatch):
    """``~`` is not a volume in a container: a login there dies on the next
    image upgrade, so the default has to be the System Directory."""
    system_dir, _ = homes
    _set_container(monkeypatch, True)
    assert cch.shared_claude_config_dir() == system_dir / "coding-cli" / "claude"
    assert cch.shared_codex_home() == system_dir / "coding-cli" / "codex"


def test_environment_wins_over_the_container_default(homes, monkeypatch, tmp_path):
    _set_container(monkeypatch, True)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "stated"))
    assert cch.shared_codex_home() == tmp_path / "stated"


# -- Resolution order ----------------------------------------------------------


def test_a_profiles_own_login_wins(homes):
    _, home = homes
    _sign_in_claude(cch.profile_claude_config_dir("alice"))
    _sign_in_claude(home / ".claude")
    resolved = cch.resolve_claude_config_dir("alice")
    assert resolved.path == str(cch.profile_claude_config_dir("alice"))
    assert (resolved.scope, resolved.has_login) == ("profile", True)


def test_a_profile_without_a_login_borrows_the_servers(homes):
    _, home = homes
    _sign_in_codex(home / ".codex")
    resolved = cch.resolve_codex_home("alice")
    assert resolved.path == str(home / ".codex")
    # Labelled "shared" so the UI can say whose login it is - and refuse to let
    # this profile sign it out.
    assert (resolved.scope, resolved.has_login) == ("shared", True)


def test_no_login_anywhere_still_points_at_the_shared_home(homes):
    _, home = homes
    resolved = cch.resolve_claude_config_dir("alice")
    assert resolved.path == str(home / ".claude")
    assert (resolved.scope, resolved.has_login) == ("shared", False)


def test_without_a_profile_only_the_shared_tiers_apply(homes):
    _, home = homes
    _sign_in_claude(cch.profile_claude_config_dir("alice"))
    resolved = cch.resolve_claude_config_dir(None)
    assert resolved.path == str(home / ".claude")
    assert (resolved.scope, resolved.has_login) == ("shared", False)


def test_a_container_still_reads_a_login_left_in_the_legacy_home(homes, monkeypatch):
    """A pod upgrades its venv code before its image, so an operator who signed
    in yesterday has the credential in ``~/.claude`` while the new code's shared
    home is under the System Directory. Missing that tier signs them out."""
    _, home = homes
    _set_container(monkeypatch, True)
    _sign_in_claude(home / ".claude")
    resolved = cch.resolve_claude_config_dir("alice")
    assert resolved.path == str(home / ".claude")
    assert (resolved.scope, resolved.has_login) == ("shared", True)


def test_the_legacy_home_is_never_consulted_on_a_native_install(homes, monkeypatch, tmp_path):
    """An operator who pointed ``CODEX_HOME`` somewhere meant it; falling back
    to ``~/.codex`` would run as an account they deliberately moved off."""
    _, home = homes
    stated = tmp_path / "stated-codex"
    stated.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(stated))
    _sign_in_codex(home / ".codex")
    resolved = cch.resolve_codex_home("alice")
    assert resolved.path == str(stated)
    assert resolved.has_login is False


def test_resolved_path_is_a_plain_string(homes):
    """It goes straight into a subprocess env, where a Path raises TypeError."""
    assert isinstance(cch.resolve_codex_home("alice").path, str)


# -- Login markers -------------------------------------------------------------


def test_claude_login_marker(homes, tmp_path):
    config_dir = tmp_path / "claude-home"
    assert cch.claude_login_present(config_dir) is False  # not even a directory

    _write_json(config_dir / ".credentials.json", {"claudeAiOauth": {"accessToken": ""}})
    assert cch.claude_login_present(config_dir) is False  # signed out leaves the file

    _sign_in_claude(config_dir)
    assert cch.claude_login_present(config_dir) is True


def test_codex_login_marker_accepts_every_shape_the_cli_writes(homes, tmp_path):
    home = tmp_path / "codex-home"
    assert cch.codex_login_present(home) is False

    # The ChatGPT flow writes a null API key beside its tokens - a membership
    # test on the key would call this "signed in with a key".
    _write_json(home / "auth.json", {"OPENAI_API_KEY": None, "tokens": {}})
    assert cch.codex_login_present(home) is False

    _write_json(home / "auth.json", {"OPENAI_API_KEY": "sk-live"})
    assert cch.codex_login_present(home) is True
    _write_json(home / "auth.json", {"openai_api_key": "sk-live"})
    assert cch.codex_login_present(home) is True
    _sign_in_codex(home)
    assert cch.codex_login_present(home) is True


@pytest.mark.parametrize("garbage", ["", "   ", "{not json", "[]", '"a string"', "null"])
def test_malformed_credentials_read_as_signed_out(homes, tmp_path, garbage):
    """These files are written by another process, mid-refresh included."""
    config_dir = tmp_path / "half-written"
    config_dir.mkdir()
    (config_dir / ".credentials.json").write_text(garbage, encoding="utf-8")
    (config_dir / "auth.json").write_text(garbage, encoding="utf-8")
    assert cch.claude_login_present(config_dir) is False
    assert cch.codex_login_present(config_dir) is False
    assert cch.read_claude_account_hint(config_dir) is None
    assert cch.read_codex_account_hint(config_dir) is None


def test_macos_falls_back_to_the_config_file(homes, monkeypatch, tmp_path):
    """macOS keeps the credential in the Keychain, so ``.credentials.json`` is
    never written and the only readable trace is ``oauthAccount``."""
    config_dir = tmp_path / "mac-home"
    _write_json(config_dir / ".claude.json", {"oauthAccount": {"emailAddress": "a@b.c"}})

    monkeypatch.setattr(cch.platform, "system", lambda: "Linux")
    assert cch.claude_login_present(config_dir) is False

    monkeypatch.setattr(cch.platform, "system", lambda: "Darwin")
    assert cch.claude_login_present(config_dir) is True

    # An empty account block is what a signed-out CLI leaves behind.
    _write_json(config_dir / ".claude.json", {"oauthAccount": {}})
    assert cch.claude_login_present(config_dir) is False


def test_macos_also_reads_the_config_file_beside_the_home(homes, monkeypatch):
    """With ``CLAUDE_CONFIG_DIR`` unset the CLI writes ``~/.claude.json``, not
    ``~/.claude/.claude.json`` - the legacy home is exactly that case."""
    _, home = homes
    monkeypatch.setattr(cch.platform, "system", lambda: "Darwin")
    _write_json(home / ".claude.json", {"oauthAccount": {"emailAddress": "a@b.c"}})
    assert cch.claude_login_present(home / ".claude") is True


# -- Environment block ---------------------------------------------------------


def test_coding_cli_env_hands_a_run_the_resolved_homes(homes):
    _, home = homes
    _sign_in_codex(home / ".codex")
    env = cch.coding_cli_env("alice")
    assert env == {
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "CODEX_HOME": str(home / ".codex"),
    }
    assert all(isinstance(value, str) for value in env.values())


def test_force_profile_never_points_a_sign_in_at_the_shared_home(homes):
    """The whole point: a login terminal spawned with the resolved home would
    write the new credential over the operator's."""
    _, home = homes
    _sign_in_claude(home / ".claude")
    _sign_in_codex(home / ".codex")
    env = cch.coding_cli_env("alice", force_profile=True)
    assert env == {
        "CLAUDE_CONFIG_DIR": str(cch.profile_claude_config_dir("alice")),
        "CODEX_HOME": str(cch.profile_codex_home("alice")),
    }


def test_force_profile_without_a_profile_is_the_shared_pair(homes):
    _, home = homes
    assert cch.coding_cli_env(None, force_profile=True) == {
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "CODEX_HOME": str(home / ".codex"),
    }


# -- Removal -------------------------------------------------------------------


def test_remove_takes_one_profiles_homes_and_leaves_its_siblings(homes):
    _sign_in_claude(cch.profile_claude_config_dir("alice"))
    _sign_in_codex(cch.profile_codex_home("alice"))
    _sign_in_codex(cch.profile_codex_home("bob"))

    assert cch.remove_profile_cli_homes("alice") is True
    assert not cch.profile_cli_root("alice").exists()
    assert cch.codex_login_present(cch.profile_codex_home("bob")) is True


def test_remove_is_a_no_op_when_the_profile_never_signed_in(homes):
    assert cch.remove_profile_cli_homes("alice") is False


def test_remove_never_raises(homes, monkeypatch):
    """It runs inside profile deletion: a file Windows has open must leave the
    profile deletable, not wedge the request."""
    _sign_in_codex(cch.profile_codex_home("alice"))

    def _boom(*_args, **_kwargs):
        raise OSError("in use by another process")

    monkeypatch.setattr(cch.shutil, "rmtree", _boom)
    assert cch.remove_profile_cli_homes("alice") is False
    assert cch.profile_cli_root("alice").exists()


# -- Account hints -------------------------------------------------------------


def test_claude_account_hint(homes, tmp_path):
    config_dir = tmp_path / "claude-home"
    assert cch.read_claude_account_hint(config_dir) is None

    _write_json(
        config_dir / ".claude.json",
        {
            "oauthAccount": {
                "emailAddress": "lee@example.com",
                "organizationName": "Cremind",
                "accountUuid": "u-1",
            },
            "projects": {"/tmp": {"history": ["secret prompt"]}},
        },
    )
    assert cch.read_claude_account_hint(config_dir) == {
        "type": "oauth",
        "email": "lee@example.com",
        "org_name": "Cremind",
    }


def test_codex_account_hint_for_an_api_key_says_nothing_more(homes, tmp_path):
    home = tmp_path / "codex-home"
    _write_json(home / "auth.json", {"OPENAI_API_KEY": "sk-live-do-not-leak"})
    hint = cch.read_codex_account_hint(home)
    assert hint == {"type": "api_key"}
    assert "sk-live-do-not-leak" not in json.dumps(hint)


def test_codex_account_hint_decodes_the_id_token_locally(homes, tmp_path):
    home = tmp_path / "codex-home"
    token = _id_token(
        {
            "email": "lee@example.com",
            "https://api.openai.com/auth": {"chatgpt_plan_type": "pro"},
        }
    )
    _sign_in_codex(home, id_token=token)
    hint = cch.read_codex_account_hint(home)
    assert hint == {"type": "chatgpt", "email": "lee@example.com", "plan_type": "pro"}
    # The credential itself must never travel to the UI or the CLI table.
    assert token not in json.dumps(hint)


def test_codex_account_hint_survives_an_unreadable_id_token(homes, tmp_path):
    """The login is real whether or not its label decodes; claiming "not signed
    in" over an unreadable claim would be the worse lie."""
    home = tmp_path / "codex-home"
    _sign_in_codex(home, id_token="not.a.jwt")
    assert cch.read_codex_account_hint(home) == {
        "type": "chatgpt",
        "email": None,
        "plan_type": None,
    }
