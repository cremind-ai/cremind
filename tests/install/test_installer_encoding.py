"""The bytes the installers write, for the files Python has to parse back.

A native Windows install boot-looped because ``Set-Content -Encoding utf8``
under Windows PowerShell 5.1 prefixes a UTF-8 BOM, and the ``toml`` parser reads
that BOM as part of the first comment's ``#``. The backend now tolerates a BOM
wherever an older installer may have left one, but the installer must stop
producing new ones — and the two scripts must keep writing the same file, which
is only checkable by comparing their text.

``install.sh`` was never affected: a quoted heredoc copies bytes verbatim. So
the parity assertion here doubles as the specification.

The bytes of ``install.ps1`` itself matter as much: ``iwr | iex`` decodes the
script as ASCII, so it must be pure ASCII, without even a BOM.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import toml

REPO_ROOT = Path(__file__).resolve().parents[2]
PS1 = REPO_ROOT / "install" / "install.ps1"
SH = REPO_ROOT / "install" / "install.sh"


def _ps1() -> str:
    return PS1.read_text(encoding="utf-8")


def _sh() -> str:
    return SH.read_text(encoding="utf-8")


def _ps1_here_string(script: str, variable: str) -> str:
    match = re.search(rf'(?ms)^\s*\${variable} = @"\r?\n(.*?)\r?\n"@\r?$', script)
    assert match, f"${variable} is no longer written from a here-string"
    return match.group(1).replace("\r\n", "\n")


def _sh_heredoc(script: str, path_variable: str) -> str:
    match = re.search(
        rf'(?ms)^\s*cat > "\${path_variable}" <<\'EOF\'\r?\n(.*?)\r?\nEOF\r?$', script)
    assert match, f"${path_variable} is no longer written from a heredoc"
    return match.group(1).replace("\r\n", "\n")


# ── no new BOMs ───────────────────────────────────────────────────────────


def test_the_toml_files_are_written_without_a_bom() -> None:
    """Both TOML files, and only through the helper that guarantees it."""
    ps1 = _ps1()

    assert re.search(r"Set-Content -Path \$(BootstrapFile|CredsFile)\b", ps1) is None
    assert "Write-Utf8NoBomFile -Path $BootstrapFile -Content $bootstrapText" in ps1
    # Twice: the docker branch and the native branch each write one.
    assert ps1.count("Write-Utf8NoBomFile -Path $CredsFile -Content $credsText") == 2
    assert "[System.Text.UTF8Encoding]::new($false)" in ps1


def test_the_env_files_deliberately_keep_their_encoding() -> None:
    """Not an oversight. This script reads its own .env back with ``-Encoding
    utf8``, which is what stops PowerShell 5.1 decoding it as cp1252 and
    mangling the em-dashes in the template comments; the ``cremind`` shim skips
    a BOM by design, and python-dotenv tolerates one."""
    ps1 = _ps1()

    assert "Set-Content -Path $EnvFile -Encoding utf8" in ps1
    assert "Set-Content -Path $EnvDocker -Encoding utf8" in ps1


def test_both_installers_write_the_same_bootstrap_file() -> None:
    """The file is a database commitment; the two scripts disagreeing about it
    would give the same install a different backend on different platforms."""
    assert _ps1_here_string(_ps1(), "bootstrapText") == _sh_heredoc(_sh(), "BOOTSTRAP_FILE")


# ── the helper, actually run ──────────────────────────────────────────────


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        pytest.skip("no PowerShell available")
    return executable


def test_the_helper_writes_bytes_python_can_parse(tmp_path) -> None:
    """Extract the helper and the bootstrap write, run them, read the bytes.

    On this repo's CI that is PowerShell 7, which would not have produced the
    BOM anyway; on a Windows box it is the 5.1 that did. Either way what is
    asserted is the output, not the interpreter.
    """
    ps1 = _ps1()
    helper = re.search(r"(?ms)^function Write-Utf8NoBomFile \{\r?\n.*?^\}\r?$", ps1)
    assert helper, "the helper is gone or no longer a top-level function"
    target = tmp_path / "bootstrap.toml"
    body = (
        "Set-StrictMode -Version Latest\n"
        "$ErrorActionPreference = 'Stop'\n"
        + helper.group(0).replace("\r\n", "\n") + "\n"
        + "$BootstrapFile = '" + str(target).replace("'", "''") + "'\n"
        + '$bootstrapText = @"\n' + _ps1_here_string(ps1, "bootstrapText") + '\n"@\n'
        + "Write-Utf8NoBomFile -Path $BootstrapFile -Content $bootstrapText\n"
    )
    script = tmp_path / "write.ps1"
    script.write_text(body, encoding="utf-8-sig", newline="\n")

    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(script)],
        capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stderr
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "the BOM is back"
    assert b"\r" not in raw
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    # The failure this whole file exists for: the backend reading it as UTF-8.
    assert toml.loads(raw.decode("utf-8"))["db_provider"] == "sqlite"
    # And byte-identical to what install.sh's heredoc leaves behind.
    assert raw == (_sh_heredoc(_sh(), "BOOTSTRAP_FILE") + "\n").encode("utf-8")


# ── the script's own bytes: pure ASCII ────────────────────────────────────


def test_install_ps1_is_pure_ascii() -> None:
    """``iwr -useb https://cremind.io/install.ps1 | iex`` hands ``iex`` the
    response as ASCII on Windows PowerShell 5.1, so every byte above 0x7F
    becomes ``?``. While this file began with a UTF-8 BOM, line 1 arrived as
    ``???<#``: one bare word, so the help block never opened and its text was
    parsed as code (``MissingExpressionAfterToken`` at line 21). The one-liner
    never worked on 5.1. ``irm | iex`` keeps the BOM as U+FEFF, just as fatal.

    Dropping only the BOM breaks the other delivery. The desktop app and
    ``-OutFile`` run the file from disk, and 5.1 reads a BOM-less file in the
    ANSI code page, where the UTF-8 bytes of an em dash or a box-drawing rule
    include 0x94: a curly quote, which PowerShell treats as ``"``, so strings
    end early. Pure ASCII reads the same under every decoder; build anything
    fancier at runtime, as ``Write-Step`` does with ``[char]0x2500``.
    """
    raw = PS1.read_bytes()

    assert not raw.startswith(b"\xef\xbb\xbf"), "install.ps1 starts with a UTF-8 BOM again"
    offenders = [
        f"line {number}: {line.decode('utf-8', 'replace').strip()[:100]}"
        for number, line in enumerate(raw.splitlines(), start=1)
        if not line.isascii()
    ]
    assert not offenders, "install.ps1 must stay pure ASCII:\n" + "\n".join(offenders[:20])


def test_the_one_liner_parses_what_it_downloads(tmp_path) -> None:
    """What ``iex`` receives from ``iwr | iex``: the bytes decoded as ASCII.

    It must parse without a single error, and its first token must be the help
    block (a comment), not a command named ``???<#``.
    """
    probe = tmp_path / "parse.ps1"
    probe.write_text(
        "$text = [System.Text.Encoding]::ASCII.GetString("
        "[System.IO.File]::ReadAllBytes($args[0]))\n"
        "$tokens = $null; $errors = $null\n"
        "[void][System.Management.Automation.Language.Parser]::ParseInput("
        "$text, [ref]$tokens, [ref]$errors)\n"
        "'{0} {1}' -f $errors.Count, $tokens[0].Kind\n",
        encoding="ascii", newline="\n",
    )

    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(probe), str(PS1)],
        capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["0", "Comment"], result.stdout


# ── a failed migration is never reported as success ───────────────────────


def test_a_failed_migration_stops_both_installers() -> None:
    ps1, sh = _ps1(), _sh()

    assert "Write-Err2 \"Database migration failed" in ps1
    assert 'err "Database migration failed' in sh
    assert '! "$VENV_DIR/bin/cremind" db upgrade' in sh


def test_an_unreadable_revision_is_never_printed_as_ok() -> None:
    """``OK Database at revision ?`` was the first visible symptom of the boot
    loop, reported as a success one step before the server failed to start."""
    ps1, sh = _ps1(), _sh()

    assert "Write-Warn2 \"Could not read the database revision" in ps1
    assert 'warn "Could not read the database revision' in sh
    assert "$Revision = '?'" not in ps1
    assert 'db current 2>/dev/null || echo "?"' not in sh
