"""``INSTALL_MODE`` must reach a Docker container as ``docker``, always.

Compose resolves ``${VAR}`` from the shell that runs ``docker compose up``
*before* the project's ``.env``, and that shell is not always clean: the native
install's ``cremind`` PowerShell shim, and the native branch of install.ps1
itself, both loaded ``~\\.cremind\\.env`` — ``INSTALL_MODE=native`` included —
into the calling session and never removed it. A Docker install created from
that window was labelled a native install: it was shown the "press Ctrl+C in the
terminal that runs it" HTTPS runbook for a terminal that does not exist, and its
switch was written to a ``.env`` the container environment shadows.

Three guards, one per layer:

* the rendered compose file pins the literal, so nothing in the shell can reach
  the container;
* the installers drop the variable before they call compose, which covers a
  compose file rendered by an older installer;
* the shim and the native start block put the session back as they found it, so
  the leak stops at the source.

The backend tolerates a container that still claims ``native`` — see
``app.config.tls_managed_env.resolve_install_mode`` — but it should stop
receiving them.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PS1 = REPO_ROOT / "install" / "install.ps1"
SH = REPO_ROOT / "install" / "install.sh"
TEMPLATES = REPO_ROOT / "install" / "templates"

# The provisioner's own idea of what compose will interpolate
# (app/services/provisioner/docker.py), so this test and the code that has to
# scrub such names agree on the definition.
_INTERPOLATED = re.compile(r"(?<!\$)\$\{?([A-Za-z_][A-Za-z0-9_]*)")

# The single-quoted here-string the shim body lives in. The encoding test's
# helper only matches the double-quoted ``@"…"@`` form.
_PS1_SHIM_BODY = re.compile(r"(?s)\$CremindPs1Body = @'\r?\n(.*?)\r?\n'@")


def _ps1() -> str:
    return PS1.read_text(encoding="utf-8")


def _sh() -> str:
    return SH.read_text(encoding="utf-8")


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        pytest.skip("no PowerShell available")
    return executable


def _between(script: str, start: str, end: str) -> str:
    """The slice between two unique markers, so an assertion cannot drift."""
    first = script.index(start)
    return script[first:script.index(end, first)]


# ── the template pins it ──────────────────────────────────────────────────


def _uncommented(text: str) -> str:
    """The template without its ``#`` lines.

    Comments are worth excluding rather than forbidding: the one above this very
    setting has to quote the ``${INSTALL_MODE:-docker}`` it replaced to explain
    why. YAML comments never reach Compose's interpolation, which
    :func:`test_the_rendered_template_ignores_a_polluted_shell` proves by
    rendering the file.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_compose_template_pins_install_mode_to_docker() -> None:
    """A compose file the Docker installer rendered IS a Docker install."""
    compose = _uncommented((TEMPLATES / "docker-compose.yml.tmpl").read_text(encoding="utf-8"))

    assert re.search(r"(?m)^ +INSTALL_MODE: docker$", compose)
    assert "${INSTALL_MODE" not in compose
    assert "INSTALL_MODE" not in set(_INTERPOLATED.findall(compose))


def test_the_rendered_template_ignores_a_polluted_shell(tmp_path) -> None:
    """The regression itself: ``docker compose config`` with the leak present.

    Compose resolves ``${VAR}`` from the invoking shell before the project's
    ``.env``, which is how ``INSTALL_MODE=native`` reached a Docker container.
    """
    if not shutil.which("docker"):
        pytest.skip("no docker available")
    import os

    (tmp_path / "docker-compose.yml").write_text(
        (TEMPLATES / "docker-compose.yml.tmpl").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / ".env").write_text(
        "CREMIND_IMAGE=cremind/cremind\nCREMIND_VERSION=test\n"
        "APP_URL=http://localhost:1515\nCORS_ALLOWED_ORIGINS=http://localhost:1515\n"
        "VNC_PASSWORD=x\n", encoding="utf-8")

    result = subprocess.run(
        ["docker", "compose", "config"], cwd=tmp_path, capture_output=True, text=True,
        timeout=120, env={**os.environ, "INSTALL_MODE": "native"},
    )
    if result.returncode != 0:
        pytest.skip(f"docker compose could not render the template: {result.stderr.strip()[:200]}")

    assert re.search(r"(?m)^\s+INSTALL_MODE: docker$", result.stdout)
    assert "INSTALL_MODE: native" not in result.stdout


def test_env_template_keeps_the_host_side_line_and_both_installers_fill_it() -> None:
    """The ``.env`` value is still read on the host — by the desktop app and by
    the config export's file fallback — just not by the container."""
    env = (TEMPLATES / "docker.env.tmpl").read_text(encoding="utf-8")

    assert "\nINSTALL_MODE=__INSTALL_MODE__\n" in env
    assert re.search(r"-replace '__INSTALL_MODE__',\s+\$Mode", _ps1())
    assert '-e "s|__INSTALL_MODE__|$MODE|g"' in _sh()


# ── the installers drop it before compose ─────────────────────────────────


def test_ps1_drops_install_mode_before_docker_compose() -> None:
    """Guards a compose file an older installer rendered with ``${INSTALL_MODE}``."""
    block = _between(_ps1(), 'Write-Step "Docker install"',
                     "Invoke-NativeLogged { & docker compose")

    assert "Remove-Item Env:INSTALL_MODE -ErrorAction SilentlyContinue" in block
    # The CREMIND_SSL* variables are the opposite case: setting them in this
    # process is how the resolved TLS mode reaches compose.
    assert "Remove-Item Env:CREMIND_SSL" not in block


def test_sh_drops_install_mode_before_docker_compose() -> None:
    block = _between(_sh(), 'step "Docker install"', "docker compose pull")

    assert re.search(r"(?m)^\s*unset INSTALL_MODE\s*$", block)
    assert "unset CREMIND_SSL" not in block


# ── the shim stops leaking ────────────────────────────────────────────────


def _shim_body() -> str:
    match = _PS1_SHIM_BODY.search(_ps1())
    assert match, "the cremind.ps1 here-string is gone or renamed"
    return match.group(1)


def test_ps1_shim_removes_only_what_it_added() -> None:
    body = _shim_body()

    # Initialised above the .env guard: the shim inherits the caller's
    # Set-StrictMode, and with no .env the loop never runs.
    guard = body.index("if (Test-Path -LiteralPath $cremindEnvFile)")
    assert body.index("$cremindLoadedKeys = @()") < guard
    assert body.index("$cremindExitCode = 0") < guard

    assert 'if (Test-Path -LiteralPath "Env:$k") { continue }' in body
    assert "$cremindLoadedKeys += $k" in body
    assert "} finally {" in body
    assert 'Remove-Item -Path "Env:$k" -ErrorAction SilentlyContinue' in body


def test_ps1_shim_leaves_the_calling_session_clean(tmp_path) -> None:
    """Run the real shim body in a session and look at the session afterwards.

    A ``.ps1`` on PATH runs INSIDE the caller's PowerShell, so the shim is
    invoked in-process here — a child process could not observe the leak this
    test exists for. The "exe" is another script, which keeps the hand-over line
    identical to the one the installer renders; this runs under ``pwsh`` on the
    Linux CI runner too, so nothing here may be Windows-only.
    """
    body = _shim_body()
    assert body.count("__VENV_CREMIND__") == 1

    child = tmp_path / "child.ps1"
    child.write_text('Write-Output "CHILD=$env:INSTALL_MODE|$env:FOO"\nexit 7\n',
                     encoding="utf-8-sig", newline="\n")
    shim = tmp_path / "cremind.ps1"
    shim.write_text(body.replace("__VENV_CREMIND__", str(child)) + "\n",
                    encoding="utf-8-sig", newline="\n")

    def quoted(path: Path) -> str:
        return str(path).replace("'", "''")

    driver = tmp_path / "driver.ps1"
    driver.write_text(
        "Set-StrictMode -Version Latest\n"
        "$ErrorActionPreference = 'Stop'\n"
        f"$env:CREMIND_SYSTEM_DIR = '{quoted(tmp_path)}'\n"
        "$env:FOO = 'from-session'\n"
        "Remove-Item Env:INSTALL_MODE -ErrorAction SilentlyContinue\n"
        f"& '{quoted(shim)}'\n"
        "$code = $LASTEXITCODE\n"
        "$after = if (Test-Path Env:INSTALL_MODE) { $env:INSTALL_MODE } else { '<absent>' }\n"
        'Write-Output "AFTER=$after|$env:FOO|$code"\n'
        "exit 0\n",
        encoding="utf-8-sig", newline="\n",
    )

    def run() -> str:
        result = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(driver)],
            capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    (tmp_path / ".env").write_text("# a comment\nINSTALL_MODE=native\nFOO=bar\n", encoding="utf-8")
    output = run()
    # The child saw the .env value; the real environment still won for FOO.
    assert "CHILD=native|from-session" in output
    # And the session is as it was, with the exit code forwarded.
    assert "AFTER=<absent>|from-session|7" in output

    # No .env at all: a silent no-op, not a StrictMode error in the finally.
    (tmp_path / ".env").unlink()
    output = run()
    assert "CHILD=|from-session" in output
    assert "AFTER=<absent>|from-session|7" in output


# ── the native start block puts the session back ──────────────────────────


def test_native_start_block_restores_the_session() -> None:
    """Order matters as much as presence.

    The restore has to come after the two helpers that read ``$env:CREMIND_SSL``
    (``Get-CremindScheme`` / ``Get-CremindBootScheme`` fall back to the file only
    when the variable is empty), and the snapshot has to be declared outside the
    Electron gate — the Electron front-end skips the whole block, and under
    ``Set-StrictMode`` a restore reading an unset variable would end an
    otherwise successful install with a terminating error.
    """
    ps1 = _ps1()
    # ``Start-Process $WizardUrl`` also appears in the Docker branch.
    block = ps1.split("# ── start the server", 1)[1]
    gate = block.index("if ($env:CREMIND_INSTALLER_FRONTEND -ne 'electron')")
    restore = block.rindex("foreach ($k in @($SessionEnvBefore.Keys))")

    assert block.index("$SessionEnvBefore = @{}") < gate < block.index("$ParsedEnv = @{}")
    assert block.index("$SessionEnvBefore[$k] =") < block.index('Set-Item -Path "Env:$k"')
    assert block.index("Get-CremindBootScheme -EnvPath $EnvFile") < restore
    assert block.index("Start-Process $WizardUrl") < restore
    assert restore < block.index("switch ($Channel)")


def test_the_native_start_block_restore_survives_the_electron_path(tmp_path) -> None:
    """The Electron front-end never loads the .env, so the restore must be a
    no-op there rather than a terminating error."""
    ps1 = _ps1()
    block = ps1.split("# ── start the server", 1)[1]
    loop = re.search(
        r"(?s)(    \$ParsedEnv = @\{\}\r?\n    Get-Content \$EnvFile \| ForEach-Object \{.*?\r?\n    \})",
        block)
    assert loop, "the .env load loop moved or was rewritten"
    restore = re.search(
        r"(?s)(foreach \(\$k in @\(\$SessionEnvBefore\.Keys\)\) \{.*?\r?\n\})", block)
    assert restore, "the restore loop moved or was rewritten"

    env_file = tmp_path / ".env"
    env_file.write_text("INSTALL_MODE=native\nFOO=bar\n", encoding="utf-8")

    def quoted(path: Path) -> str:
        return str(path).replace("'", "''")

    script = tmp_path / "block.ps1"
    script.write_text(
        "Set-StrictMode -Version Latest\n"
        "$ErrorActionPreference = 'Stop'\n"
        f"$EnvFile = '{quoted(env_file)}'\n"
        "$SessionEnvBefore = @{}\n"
        "if ($env:CREMIND_INSTALLER_FRONTEND -ne 'electron') {\n"
        + loop.group(1).replace("\r\n", "\n") + "\n"
        "}\n"
        + restore.group(1).replace("\r\n", "\n") + "\n"
        "$after = if (Test-Path Env:INSTALL_MODE) { $env:INSTALL_MODE } else { '<absent>' }\n"
        'Write-Output "AFTER=$after|$env:FOO"\n'
        "exit 0\n",
        encoding="utf-8-sig", newline="\n",
    )

    def run(frontend: str | None) -> str:
        import os
        environment = {**os.environ, "FOO": "from-session"}
        environment.pop("INSTALL_MODE", None)
        if frontend:
            environment["CREMIND_INSTALLER_FRONTEND"] = frontend
        else:
            environment.pop("CREMIND_INSTALLER_FRONTEND", None)
        result = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            capture_output=True, text=True, timeout=120, env=environment,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    # The block ran: the .env keys are put back the way they were found.
    assert "AFTER=<absent>|from-session" in run(None)
    # The block was skipped entirely, and the restore still exits cleanly.
    assert "AFTER=<absent>|from-session" in run("electron")
