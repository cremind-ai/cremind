"""CLI: per-terminal profile selection & on-disk token auto-resolution.

Covers `app/cli/session.py` (listing profiles from `tokens/*.token`, reading a
token, the per-terminal remembered selection, and `resolve_profile`) plus the
`profile use/which/clear` commands and the root-callback wiring that fills in
`cfg.token` from the resolved profile without the user exporting `CREMIND_TOKEN`.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner


def _invoke(runner: CliRunner, monkeypatch, args: list[str]):
    """Invoke the CLI, mirroring `args` into `sys.argv`.

    The root callback reads `sys.argv` to detect the token-free `profile
    use/which/clear` subcommands (Click doesn't expose the deep subcommand at
    that stage). `CliRunner` deliberately leaves `sys.argv` untouched, so tests
    that exercise that gating must set it explicitly.
    """
    from app.cli.main import app

    monkeypatch.setattr(sys, "argv", ["cremind", *args])
    return runner.invoke(app, args)


@pytest.fixture
def sysdir(tmp_path, monkeypatch):
    """A throwaway CREMIND_SYSTEM_DIR with a stable per-terminal session key."""
    d = tmp_path / "sysdir"
    (d / "tokens").mkdir(parents=True)
    monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(d))
    # Pin the session key so the remembered-selection round-trips are stable and
    # isolated from the host terminal / parent PID.
    monkeypatch.setenv("WT_SESSION", "test-session-fixed")
    monkeypatch.delenv("CREMIND_TOKEN", raising=False)
    monkeypatch.delenv("CREMIND_PROFILE", raising=False)
    return d


def _write_token(sysdir, name: str, value_prefix: str = "jwt-") -> None:
    (sysdir / "tokens" / f"{name}.token").write_text(value_prefix + name, encoding="utf-8")


# ── session module ─────────────────────────────────────────────────────────


def test_list_profiles_from_disk_sorted_and_hides_underscore(sysdir):
    import app.cli.session as s

    _write_token(sysdir, "beta")
    _write_token(sysdir, "admin")
    (sysdir / "tokens" / "__internal.token").write_text("x", encoding="utf-8")
    (sysdir / "tokens" / "notatoken.txt").write_text("x", encoding="utf-8")

    assert s.list_profiles() == ["admin", "beta"]


def test_list_profiles_missing_dir_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(tmp_path / "does-not-exist"))
    import app.cli.session as s

    assert s.list_profiles() == []


def test_read_token(sysdir):
    import app.cli.session as s

    _write_token(sysdir, "admin", "TOKENVALUE-")
    assert s.read_token("admin") == "TOKENVALUE-admin"
    assert s.read_token("ghost") is None
    assert s.read_token("") is None


def test_resolve_single_profile_autoselects_even_noninteractive(sysdir):
    import app.cli.session as s

    _write_token(sysdir, "solo")
    assert s.resolve_profile(None, interactive=False) == "solo"


def test_resolve_multi_noninteractive_returns_none(sysdir):
    import app.cli.session as s

    _write_token(sysdir, "a")
    _write_token(sysdir, "b")
    assert s.resolve_profile(None, interactive=False) is None


def test_resolve_explicit_is_sticky(sysdir):
    import app.cli.session as s

    _write_token(sysdir, "a")
    _write_token(sysdir, "b")
    assert s.resolve_profile("b", interactive=False) == "b"
    # remembered for the terminal, so a later bare call resolves to it
    assert s.get_session_profile() == "b"
    assert s.resolve_profile(None, interactive=False) == "b"


def test_session_roundtrip(sysdir):
    import app.cli.session as s

    _write_token(sysdir, "admin")
    assert s.get_session_profile() is None
    s.set_session_profile("admin")
    assert s.get_session_profile() == "admin"
    s.clear_session_profile()
    assert s.get_session_profile() is None


def test_session_self_heals_deleted_profile(sysdir):
    import app.cli.session as s

    _write_token(sysdir, "admin")
    s.set_session_profile("admin")
    (sysdir / "tokens" / "admin.token").unlink()
    # The remembered profile no longer has a token file → dropped transparently.
    assert s.get_session_profile() is None


# ── profile use / which / clear commands ─────────────────────────────────────


def test_profile_use_which_clear(sysdir, monkeypatch):
    import app.cli.session as s

    _write_token(sysdir, "admin")
    runner = CliRunner()

    r = _invoke(runner, monkeypatch, ["profile", "use", "admin"])
    assert r.exit_code == 0, r.output
    assert s.get_session_profile() == "admin"

    r = _invoke(runner, monkeypatch, ["profile", "which"])
    assert r.exit_code == 0, r.output
    assert "admin" in r.output

    r = _invoke(runner, monkeypatch, ["profile", "clear"])
    assert r.exit_code == 0, r.output

    # After clear, `which` must report "none" and NOT auto-resolve the lone
    # profile (proves the root callback skips resolution for session commands).
    r = _invoke(runner, monkeypatch, ["profile", "which"])
    assert r.exit_code == 1, r.output


def test_profile_use_unknown_rejected(sysdir, monkeypatch):
    _write_token(sysdir, "admin")
    r = _invoke(CliRunner(), monkeypatch, ["profile", "use", "ghost"])
    assert r.exit_code == 1, r.output
    assert "no token file" in r.output


# ── root-callback token auto-resolution ─────────────────────────────────────


def _patch_get_me(monkeypatch, captured: dict) -> None:
    import app.cli.client.me as me_client

    async def fake_get_me(client):  # noqa: ANN001
        captured["token"] = client.token
        return SimpleNamespace(
            profile="solo", subject="solo", issued_at=0, expires_at=0,
            system_dir="", user_working_dir="",
        )

    monkeypatch.setattr(me_client, "get_me", fake_get_me)


def test_me_autoresolves_single_profile_token(sysdir, monkeypatch):
    from app.cli.main import app

    _write_token(sysdir, "solo", "JWTVAL-")
    captured: dict = {}
    _patch_get_me(monkeypatch, captured)

    r = CliRunner().invoke(app, ["me"])
    assert r.exit_code == 0, r.output
    assert captured["token"] == "JWTVAL-solo"


def test_explicit_profile_flag_resolves_token(sysdir, monkeypatch):
    from app.cli.main import app

    _write_token(sysdir, "solo", "JWTVAL-")
    _write_token(sysdir, "other", "JWTVAL-")
    captured: dict = {}
    _patch_get_me(monkeypatch, captured)

    r = CliRunner().invoke(app, ["--profile", "solo", "me"])
    assert r.exit_code == 0, r.output
    assert captured["token"] == "JWTVAL-solo"


def test_env_token_takes_precedence_over_profile(sysdir, monkeypatch):
    from app.cli.main import app

    _write_token(sysdir, "solo", "JWTVAL-")
    monkeypatch.setenv("CREMIND_TOKEN", "env-token-wins")
    captured: dict = {}
    _patch_get_me(monkeypatch, captured)

    r = CliRunner().invoke(app, ["me"])
    assert r.exit_code == 0, r.output
    # exec_shell path: an explicit CREMIND_TOKEN is used verbatim, no resolution.
    assert captured["token"] == "env-token-wins"


def test_me_multi_profile_noninteractive_errors(sysdir, monkeypatch):
    from app.cli.main import app

    _write_token(sysdir, "a")
    _write_token(sysdir, "b")
    captured: dict = {}
    _patch_get_me(monkeypatch, captured)

    r = CliRunner().invoke(app, ["me"])
    assert r.exit_code == 1, r.output
    assert "no Cremind profile selected" in r.output
    assert "token" not in captured  # never reached the client
