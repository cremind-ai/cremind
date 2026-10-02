"""A failed ``pip install cremind`` fails the install.

install.ps1 runs pip through ``Invoke-NativeLogged``, which logs its output and
nothing more: an unchecked install that failed still went on to print "Done",
and the desktop app's first sign was a backend that never started ("backend at
http://127.0.0.1:1515/health did not respond after 30s"). install.sh runs under
``set -euo pipefail``, which stops at the failing pip itself.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PS1 = REPO_ROOT / "install" / "install.ps1"
SH = REPO_ROOT / "install" / "install.sh"


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


def test_install_sh_stops_at_a_failed_pip() -> None:
    text = SH.read_text(encoding="utf-8")
    assert "\nset -euo pipefail\n" in text
    for line in text.splitlines():
        if re.search(r'bin/pip" install .*"\$INSTALL_SPEC"', line):
            assert "||" not in line, line
