"""A failed ``pip install cremind`` fails the install, and says why.

install.ps1 runs pip through ``Invoke-NativeLogged``, which logs its output and
nothing more: an unchecked install that failed still went on to print "Done",
and the desktop app's first sign was a backend that never started ("backend at
http://127.0.0.1:1515/health did not respond after 30s").

install.sh runs under ``set -euo pipefail``, which did stop at a failing pip,
but with nothing on screen: pip's output goes to the log only, so on an Intel
Mac the installer simply ended after "Installing cremind from Test PyPI". It
now runs pip through ``run_logged``, which names the failure and shows the end
of pip's output. What failed there is fixed too: pip prefers wheels (an Intel
Mac compiled cryptography, which has shipped no Intel-Mac wheels since 49.0.0)
and the venv's Python is checked first (macOS on 3.14 compiles watchdog; pip
refuses a 3.13 older than 3.13.9).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PS1 = REPO_ROOT / "install" / "install.ps1"
SH = REPO_ROOT / "install" / "install.sh"

# Stand-ins for install.sh's colour-wrapped message helpers.
_MESSAGES = (
    "err() { printf 'ERR %s\\n' \"$1\" >&2; }\n"
    "warn() { printf '!!! %s\\n' \"$1\" >&2; }\n"
)


def _sh() -> str:
    return SH.read_text(encoding="utf-8")


def _sh_function(name: str) -> str:
    sh = _sh()
    start = sh.index(f"{name}() {{")
    return sh[start:sh.index("\n}\n", start) + 3]


def _usable_bash() -> str:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash available")
    # C:\Windows\System32\bash.exe is the WSL launcher, not a usable bash.
    if os.name == "nt" and Path(bash).parent.name.lower() == "system32":
        pytest.skip("only the WSL launcher is on PATH as bash")
    return bash


def _run_bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_usable_bash(), "-s"], input=script.replace("\r\n", "\n"),
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )


def test_install_ps1_checks_every_cremind_install() -> None:
    lines = PS1.read_text(encoding="utf-8").splitlines()
    installs = [i for i, line in enumerate(lines) if re.search(r"& \$VenvPip install .*\$InstallSpec", line)]
    assert len(installs) == 2, "a fresh venv and an in-place upgrade"
    for i in installs:
        assert lines[i + 1].strip() == "if ($LASTEXITCODE -ne 0) {", lines[i].strip()
        assert lines[i + 3].strip() == "exit 1", lines[i].strip()


def test_install_ps1_requires_the_cremind_executable() -> None:
    text = PS1.read_text(encoding="utf-8")
    venv_created = text.index("Invoke-NativeLogged { & $Python -m venv $VenvDir }")
    assert text[venv_created:].lstrip().splitlines()[1].strip() == "if ($LASTEXITCODE -ne 0) {"
    # After both branches, and before anything is written that marks the
    # machine as installed.
    check = text.index("if (-not (Test-Path $VenvCremind)) {", venv_created)
    assert check < text.index("$InstalledVersion = ''", venv_created)


# ── install.sh ────────────────────────────────────────────────────────────


def test_install_sh_installs_cremind_through_run_logged_preferring_wheels() -> None:
    lines = _sh().splitlines()
    installs = [i for i, line in enumerate(lines) if re.search(r'bin/pip" install .*"\$INSTALL_SPEC"', line)]
    assert len(installs) == 2, "a fresh venv and an in-place upgrade"
    for i in installs:
        assert "--prefer-binary" in lines[i], lines[i]
        assert lines[i - 1].strip().startswith('run_logged "'), lines[i - 1]
        assert lines[i - 1].endswith("\\"), lines[i - 1]
    venv = next(line for line in lines if '-m venv "$VENV_DIR"' in line)
    assert venv.strip().startswith('run_logged "'), venv


def test_install_sh_rebuilds_the_venv_a_failed_install_left() -> None:
    """A venv without cremind has nothing to upgrade, and it keeps the Python
    that failed: a re-run must not go "upgrading in place" into it."""
    sh = _sh()
    cleanup = sh.index('[ -d "$VENV_DIR" ] && [ ! -x "$VENV_DIR/bin/cremind" ]')
    assert sh.index('rm -rf "$VENV_DIR"', cleanup) < sh.index("Existing install detected", cleanup)


def test_install_sh_checks_the_venv_python_before_the_isolated_bootstrap() -> None:
    sh = _sh()
    check = sh.index('py_problem="$(venv_python_problem ')
    bootstrap = sh.index("    prompt_for_python_install\n    install_uv_locally\n    install_python_via_uv\n")
    assert check < bootstrap
    # The isolated Python is asked for by range: a bare "3.13" settles for an
    # older 3.13 already there.
    fn = _sh_function("install_python_via_uv")
    assert "python install '>=3.13.9,<3.14'" in fn
    assert "python find '>=3.13.9,<3.14'" in fn


def test_venv_python_problem() -> None:
    cases = [
        ("linux", "3.13.9", None),
        ("linux", "3.13.12", None),
        ("linux", "3.14.8", None),
        ("linux", "3.13.5", "3.13.9 or newer"),  # Debian 13's python3.13
        ("linux", "3.12.11", "3.13.9 or newer"),
        ("linux", "", "3.13.9 or newer"),  # the interpreter did not answer
        ("macos", "3.13.9", None),
        ("macos", "3.13.12", None),
        ("macos", "3.14.8", "watchdog"),
        ("macos", "3.13.5", "3.13.9 or newer"),
    ]
    lines = ["set -euo pipefail", _sh_function("venv_python_problem")]
    lines += [f"OS={os_name}; printf '[%s]\\n' \"$(venv_python_problem '{ver}')\"" for os_name, ver, _ in cases]
    result = _run_bash("\n".join(lines) + "\n")
    assert result.returncode == 0, result.stderr
    rows = result.stdout.splitlines()
    assert len(rows) == len(cases), rows
    for (os_name, ver, problem), row in zip(cases, rows):
        if problem is None:
            assert row == "[]", (os_name, ver, row)
        else:
            assert problem in row, (os_name, ver, row)


@pytest.mark.parametrize("log_exists", [True, False])
def test_run_logged_stops_and_shows_the_end_of_the_output(tmp_path: Path, log_exists: bool) -> None:
    log = tmp_path / "install.log"
    if log_exists:
        log.write_text("an earlier step\n" * 3, encoding="utf-8")
    script = "\n".join([
        "set -euo pipefail",
        _MESSAGES,
        f"LOG_FILE='{log.as_posix()}'",
        _sh_function("run_logged"),
        "pip_fails() { for i in {1..30}; do echo \"pip line $i\"; done; "
        "echo 'error: metadata-generation-failed' >&2; return 3; }",
        'run_logged "Installing cremind with pip" pip_fails',
        "echo AFTER",
    ]) + "\n"
    result = _run_bash(script)
    assert result.returncode == 1
    assert "AFTER" not in result.stdout
    err = result.stderr.splitlines()
    assert err[0] == "ERR Installing cremind with pip failed (exit 3). The end of its output:"
    # The last 25 lines of this command's output: nothing earlier in the log.
    assert err[1:-1] == [f"pip line {i}" for i in range(7, 31)] + ["error: metadata-generation-failed"]
    assert err[-1] == f"ERR Full log: {log.as_posix()}"
    assert log.read_text(encoding="utf-8").count("pip line") == 30


def test_run_logged_is_quiet_on_success(tmp_path: Path) -> None:
    log = tmp_path / "install.log"
    script = "\n".join([
        "set -euo pipefail",
        _MESSAGES,
        f"LOG_FILE='{log.as_posix()}'",
        _sh_function("run_logged"),
        'run_logged "Saying hello" echo hello',
        "echo AFTER",
    ]) + "\n"
    result = _run_bash(script)
    assert (result.returncode, result.stdout, result.stderr) == (0, "AFTER\n", "")
    assert log.read_text(encoding="utf-8") == "hello\n"
