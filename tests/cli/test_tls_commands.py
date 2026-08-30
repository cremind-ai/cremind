"""`cremind tls` — export, fingerprint, and the trust-store install.

The load-bearing properties:

* it runs entirely locally and never resolves a profile or a token — the whole
  point is the state where nothing can talk to the server yet;
* the commands it *prints* (``--print-only``) are the commands it *runs*, so
  the manual fallback can never drift from the automated path;
* it refuses to install anything that is not a CA, because ``--file`` takes an
  arbitrary path and the file is about to be given root authority.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from app.config.tls_auto import ensure_local_tls


def _invoke(runner: CliRunner, monkeypatch, args: list[str]):
    """Invoke the CLI, mirroring ``args`` into ``sys.argv``.

    ``_should_resolve_profile`` reads ``sys.argv`` (Click doesn't expose the
    deep subcommand at root-callback stage) and ``CliRunner`` leaves it alone.
    """
    from app.cli.main import app

    monkeypatch.setattr(sys, "argv", ["cremind", *args])
    return runner.invoke(app, args)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def sysdir(tmp_path, monkeypatch):
    """A system dir with a generated CA, and no ambient credentials."""
    d = tmp_path / "sysdir"
    d.mkdir(parents=True)
    monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(d))
    monkeypatch.delenv("CREMIND_TOKEN", raising=False)
    monkeypatch.delenv("CREMIND_PROFILE", raising=False)
    ensure_local_tls(str(d))
    return d


@pytest.fixture
def ca(sysdir) -> Path:
    return sysdir / "tls" / "ca.pem"


# ── gating ───────────────────────────────────────────────────────────────


def test_never_resolves_a_profile_or_token(runner, monkeypatch, sysdir):
    """`tls` is token-free: the picker must not run, even with no token file."""
    import app.cli.tui.profile_picker as picker

    def _boom(*_a, **_kw):  # pragma: no cover - only runs on regression
        raise AssertionError("the profile picker must never run for `cremind tls`")

    monkeypatch.setattr(picker, "pick_profile", _boom, raising=False)

    result = _invoke(runner, monkeypatch, ["tls", "fingerprint"])

    assert result.exit_code == 0


# ── export ───────────────────────────────────────────────────────────────


def test_export_writes_the_ca_verbatim(runner, monkeypatch, sysdir, ca, tmp_path):
    out = tmp_path / "copied-ca.pem"

    result = _invoke(runner, monkeypatch, ["tls", "export", "--out", str(out)])

    assert result.exit_code == 0
    assert out.read_bytes() == ca.read_bytes()


def test_export_to_stdout(runner, monkeypatch, sysdir, ca):
    result = _invoke(runner, monkeypatch, ["tls", "export", "--out", "-"])

    assert result.exit_code == 0
    assert "-----BEGIN CERTIFICATE-----" in result.stdout


def test_export_reports_an_unwritable_destination(runner, monkeypatch, sysdir, tmp_path):
    """A bad --out is a user error, not a reason to print a traceback."""
    unwritable = tmp_path / "no-such-dir" / "ca.pem"

    result = _invoke(runner, monkeypatch, ["tls", "export", "--out", str(unwritable)])

    assert result.exit_code == 1
    assert "Could not write" in result.output
    assert "Traceback" not in result.output


def test_export_reports_a_missing_ca(runner, monkeypatch, tmp_path):
    monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(tmp_path / "empty"))

    result = _invoke(runner, monkeypatch, ["tls", "export"])

    assert result.exit_code == 1
    assert "CREMIND_SSL=auto" in result.output


# ── fingerprint ──────────────────────────────────────────────────────────


def test_fingerprint_matches_an_independent_digest(runner, monkeypatch, sysdir, ca):
    import hashlib

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    cert = x509.load_pem_x509_certificate(ca.read_bytes())
    expected = hashlib.sha256(
        cert.public_bytes(serialization.Encoding.DER)
    ).hexdigest().upper()

    result = _invoke(runner, monkeypatch, ["--json", "tls", "fingerprint"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["sha256"].replace(":", "") == expected
    assert payload["subject"] == "Cremind Local CA"


def test_fingerprint_is_colon_separated_hex(runner, monkeypatch, sysdir):
    """Browsers show the digest this way, so it can be compared by eye."""
    result = _invoke(runner, monkeypatch, ["tls", "fingerprint"])

    assert result.exit_code == 0
    line = next(ln for ln in result.stdout.splitlines() if "SHA-256" in ln)
    digest = line.split(":", 1)[1].strip()
    assert len(digest.split(":")) == 32


# ── trust: platform dispatch ─────────────────────────────────────────────


def test_print_only_windows(runner, monkeypatch, sysdir, ca):
    monkeypatch.setattr(sys, "platform", "win32")

    result = _invoke(runner, monkeypatch, ["tls", "trust", "--print-only"])

    assert result.exit_code == 0
    assert result.stdout.strip().startswith("certutil -addstore -user Root")
    assert str(ca) in result.stdout


def test_print_only_macos(runner, monkeypatch, sysdir):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("os.geteuid", lambda: 501, raising=False)

    result = _invoke(runner, monkeypatch, ["tls", "trust", "--print-only"])

    assert result.exit_code == 0
    assert "sudo security add-trusted-cert -d -r trustRoot" in result.stdout
    assert "/Library/Keychains/System.keychain" in result.stdout


def test_print_only_debian(runner, monkeypatch, sysdir):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("os.geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(
        "os.path.isdir", lambda p: p == "/usr/local/share/ca-certificates"
    )

    result = _invoke(runner, monkeypatch, ["tls", "trust", "--print-only"])

    assert result.exit_code == 0
    assert "/usr/local/share/ca-certificates/cremind-local-ca.crt" in result.stdout
    assert "update-ca-certificates" in result.stdout


def test_print_only_rhel(runner, monkeypatch, sysdir):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("os.geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(
        "os.path.isdir", lambda p: p == "/etc/pki/ca-trust/source/anchors"
    )

    result = _invoke(runner, monkeypatch, ["tls", "trust", "--print-only"])

    assert result.exit_code == 0
    assert "/etc/pki/ca-trust/source/anchors/cremind-local-ca.crt" in result.stdout
    assert "update-ca-trust extract" in result.stdout


def test_root_drops_sudo(runner, monkeypatch, sysdir):
    """In a container there is no sudo, and none is needed."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("os.geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(
        "os.path.isdir", lambda p: p == "/usr/local/share/ca-certificates"
    )

    result = _invoke(runner, monkeypatch, ["tls", "trust", "--print-only"])

    assert result.exit_code == 0
    # Check the command verb, not the whole output: a tmp path can itself
    # contain the substring "sudo".
    commands = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert commands, "expected at least one command"
    assert not any(ln.startswith("sudo ") for ln in commands)


def test_unknown_linux_distribution_prints_both_and_fails(runner, monkeypatch, sysdir):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("os.geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr("os.path.isdir", lambda p: False)

    result = _invoke(runner, monkeypatch, ["tls", "trust", "--print-only"])

    assert result.exit_code == 1
    assert "update-ca-certificates" in result.output
    assert "update-ca-trust" in result.output


# ── trust: execution ─────────────────────────────────────────────────────


def test_trust_runs_the_command_it_prints(runner, monkeypatch, sysdir):
    """The printed fallback and the executed command must be the same argv."""
    monkeypatch.setattr(sys, "platform", "win32")
    printed = _invoke(runner, monkeypatch, ["tls", "trust", "--print-only"])

    calls: list[list[str]] = []

    def _fake_run(argv, **_kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr("shutil.which", lambda _tool: "C:\\Windows\\certutil.exe")

    result = _invoke(runner, monkeypatch, ["tls", "trust", "--yes"])

    assert result.exit_code == 0
    assert len(calls) == 1
    assert " ".join(calls[0]) in printed.stdout.replace('"', "")


def test_trust_reports_a_failing_tool_and_prints_the_manual_command(
    runner, monkeypatch, sysdir
):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("shutil.which", lambda _tool: "certutil")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_kw: subprocess.CompletedProcess(argv, 1, "", "Access denied."),
    )

    result = _invoke(runner, monkeypatch, ["tls", "trust", "--yes"])

    assert result.exit_code == 1
    assert "Access denied." in result.output
    assert "Run this yourself to finish:" in result.output
    assert "certutil -addstore -user Root" in result.output


def test_trust_reports_a_missing_tool(runner, monkeypatch, sysdir):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("shutil.which", lambda _tool: None)

    result = _invoke(runner, monkeypatch, ["tls", "trust", "--yes"])

    assert result.exit_code == 1
    assert "not on PATH" in result.output


def test_every_tool_is_checked_before_anything_runs(runner, monkeypatch, sysdir):
    """Linux trust is copy-then-rehash. Discovering the rehash tool is missing
    only after the copy has landed would leave an anchor that a later,
    unrelated store refresh silently trusts."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("os.geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(
        "os.path.isdir", lambda p: p == "/usr/local/share/ca-certificates"
    )
    # cp is present, the rehash tool is not.
    monkeypatch.setattr(
        "shutil.which", lambda tool: "/bin/cp" if tool == "cp" else None
    )

    def _never(*_a, **_kw):  # pragma: no cover - only runs on regression
        raise AssertionError("nothing may run when a later tool is missing")

    monkeypatch.setattr(subprocess, "run", _never)

    result = _invoke(runner, monkeypatch, ["tls", "trust", "--yes"])

    assert result.exit_code == 1
    assert "not on PATH" in result.output


def test_a_failure_after_the_copy_says_what_already_ran(runner, monkeypatch, sysdir):
    """Reporting "nothing happened" would be a lie the user acts on."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("os.geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(
        "os.path.isdir", lambda p: p == "/usr/local/share/ca-certificates"
    )
    monkeypatch.setattr("shutil.which", lambda tool: f"/bin/{tool}")

    def _fail_second(argv, **_kw):
        code = 0 if argv[0] == "cp" else 1
        return subprocess.CompletedProcess(argv, code, "", "read-only file system")

    monkeypatch.setattr(subprocess, "run", _fail_second)

    result = _invoke(runner, monkeypatch, ["tls", "trust", "--yes"])

    assert result.exit_code == 1
    assert "was NOT undone" in result.output
    assert "cremind-local-ca.crt" in result.output
    assert "update-ca-certificates" in result.output


def test_trust_declines_at_the_prompt(runner, monkeypatch, sysdir):
    monkeypatch.setattr(sys, "platform", "win32")

    def _never(*_a, **_kw):  # pragma: no cover - only runs on regression
        raise AssertionError("nothing may run before the user confirms")

    monkeypatch.setattr(subprocess, "run", _never)

    result = _invoke(runner, monkeypatch, ["tls", "trust"])

    assert result.exit_code == 1
    assert "SHA-256" in result.output


# ── trust: safety ────────────────────────────────────────────────────────


def test_refuses_a_certificate_that_is_not_a_ca(runner, monkeypatch, sysdir):
    """Pointing --file at the server leaf must not trust it as a root."""
    leaf = sysdir / "tls" / "cert.pem"

    def _never(*_a, **_kw):  # pragma: no cover - only runs on regression
        raise AssertionError("a non-CA certificate must never be installed")

    monkeypatch.setattr(subprocess, "run", _never)

    result = _invoke(
        runner, monkeypatch, ["tls", "trust", "--file", str(leaf), "--yes"]
    )

    assert result.exit_code == 1
    assert "not a CA certificate" in result.output


def test_refuses_a_file_that_is_not_a_certificate(runner, monkeypatch, sysdir, tmp_path):
    junk = tmp_path / "notes.txt"
    junk.write_text("this is not a certificate")

    result = _invoke(
        runner, monkeypatch, ["tls", "trust", "--file", str(junk), "--yes"]
    )

    assert result.exit_code == 1
    assert "not a PEM certificate" in result.output


def test_json_mode_requires_explicit_consent(runner, monkeypatch, sysdir):
    """A confirmation prompt has no meaning in JSON mode, so demand --yes."""
    monkeypatch.setattr(sys, "platform", "win32")

    def _never(*_a, **_kw):  # pragma: no cover - only runs on regression
        raise AssertionError("JSON mode must not install without --yes")

    monkeypatch.setattr(subprocess, "run", _never)

    result = _invoke(runner, monkeypatch, ["--json", "tls", "trust"])

    assert result.exit_code == 1
    assert "--yes" in result.output
