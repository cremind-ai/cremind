"""Tests for ``app.config.tls_trust`` — the server-side trust plans.

The module powers the wizard's one-click "Trust it on this device", which
only exists for a server process that cannot escalate. So what these tests
pin is the non-escalation contract per platform, plus one parity pin: the
Windows command must stay identical to the CLI's (``cremind tls trust``),
because the wizard's manual instructions and the CLI both teach it — three
places telling the user different commands is the drift being guarded.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import tls_trust


CA = "C:\\x\\tls\\ca.pem" if sys.platform == "win32" else "/x/tls/ca.pem"


def _plan_for(monkeypatch, platform: str, *, euid: int | None = None):
    monkeypatch.setattr(sys, "platform", platform)
    if euid is not None:
        monkeypatch.setattr(tls_trust.os, "geteuid", lambda: euid, raising=False)
    return tls_trust.server_trust_plan(CA)


def test_windows_command_matches_the_cli(monkeypatch):
    """One command, taught in three places (CLI, wizard copy, this API) —
    pin the API's to the CLI's so they cannot drift apart."""
    from app.cli.commands.tls import _platform_commands

    plan = _plan_for(monkeypatch, "win32")
    _store, cli_commands = _platform_commands(Path(CA))

    assert plan.supported is True
    assert plan.commands == cli_commands
    assert plan.store == "the current user's Trusted Root store"
    assert plan.os_prompt == "windows"


def test_macos_uses_the_login_keychain_without_sudo(monkeypatch):
    """The CLI escalates to the System keychain; a server process cannot
    answer a sudo prompt, so it must target the user's own keychain."""
    plan = _plan_for(monkeypatch, "darwin")

    assert plan.supported is True
    (argv,) = plan.commands
    assert argv[0] == "security" and "sudo" not in argv
    assert "login.keychain-db" in " ".join(argv)
    assert "trustRoot" in argv


def test_linux_non_root_is_unsupported_with_a_reason(monkeypatch):
    plan = _plan_for(monkeypatch, "linux", euid=1000)

    assert plan.supported is False
    assert plan.reason and "root" in plan.reason


def test_linux_as_root_writes_the_anchor_without_sudo(monkeypatch):
    monkeypatch.setattr(
        tls_trust.os.path, "isdir",
        lambda p: p == "/usr/local/share/ca-certificates",
    )
    plan = _plan_for(monkeypatch, "linux", euid=0)

    assert plan.supported is True
    assert plan.commands[0][0] == "cp"
    assert plan.commands[1] == ["update-ca-certificates"]
    assert all("sudo" not in argv for argv in plan.commands)


def test_unknown_platform_is_unsupported(monkeypatch):
    plan = _plan_for(monkeypatch, "sunos5")
    assert plan.supported is False and plan.reason


# ── run_trust_plan ────────────────────────────────────────────────────────


def _fake_plan(commands):
    return tls_trust.TrustPlan(supported=True, store="s", commands=commands)


def test_run_refuses_when_a_tool_is_missing(monkeypatch):
    """Checked up front for EVERY command: the Linux plan is copy-then-
    rehash, and failing between the two leaves an anchor a later unrelated
    rehash would silently trust."""
    ran: list[list[str]] = []
    monkeypatch.setattr(
        tls_trust.subprocess, "run",
        lambda argv, **_k: ran.append(argv) or SimpleNamespace(returncode=0, stderr="", stdout=""),
    )
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None if name == "update-ca-certificates" else "/bin/" + name)

    ok, error = tls_trust.run_trust_plan(
        _fake_plan([["cp", "a", "b"], ["update-ca-certificates"]])
    )

    assert ok is False
    assert "update-ca-certificates" in error
    assert ran == [], "nothing may run once any tool is known to be missing"


def test_run_reports_a_nonzero_exit_with_the_tools_words(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(
        tls_trust.subprocess, "run",
        lambda argv, **_k: SimpleNamespace(
            returncode=2, stderr="The operation was canceled by the user.", stdout="",
        ),
    )

    ok, error = tls_trust.run_trust_plan(_fake_plan([["certutil", "-addstore"]]))

    assert ok is False
    assert "canceled" in error


def test_run_succeeds_when_every_command_does(monkeypatch):
    import shutil

    ran: list[list[str]] = []
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(
        tls_trust.subprocess, "run",
        lambda argv, **_k: ran.append(argv) or SimpleNamespace(returncode=0, stderr="", stdout=""),
    )

    ok, error = tls_trust.run_trust_plan(
        _fake_plan([["cp", "a", "b"], ["update-ca-certificates"]])
    )

    assert (ok, error) == (True, None)
    assert ran == [["cp", "a", "b"], ["update-ca-certificates"]]


def test_run_handles_a_timeout(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/bin/" + name)

    def _boom(argv, **_k):
        raise subprocess.TimeoutExpired(argv, 120)

    monkeypatch.setattr(tls_trust.subprocess, "run", _boom)

    ok, error = tls_trust.run_trust_plan(_fake_plan([["certutil"]]))

    assert ok is False and "certutil" in error


# ── already_trusted ───────────────────────────────────────────────────────


def test_already_trusted_is_unknown_off_windows(tmp_path):
    """None means unknown — the UI must not read it as 'not trusted'."""
    if sys.platform == "win32":
        pytest.skip("exercises the non-Windows branch")
    assert tls_trust.already_trusted(str(tmp_path / "ca.pem")) is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Root store API")
def test_already_trusted_reads_the_real_store(tmp_path):
    """A CA that was just generated cannot be in the user's Root store."""
    from app.config.tls_auto import ensure_local_tls

    ensure_local_tls(str(tmp_path))
    assert tls_trust.already_trusted(str(tmp_path / "tls" / "ca.pem")) is False


def test_already_trusted_with_no_ca_file(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    assert tls_trust.already_trusted(str(tmp_path / "missing.pem")) is None
