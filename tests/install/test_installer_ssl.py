"""Parity + contract guards for the installers' ``--ssl`` / ``-Ssl`` flag.

The two installers are hand-maintained mirrors of each other, and nothing
executes either one in CI (they install Python, pull images and start
servers). So the things that silently rot are the ones asserted here:

- the flag exists, with the same value set, in both scripts;
- the resolved mode is PERSISTED, in both, for both install modes — the
  whole point of the flag is that the choice outlives the installing shell;
- ``after-setup`` skips BOTH the bootstrap write and ``db upgrade``, because
  either one creates ``bootstrap.toml``, which the server reads as "setup is
  done, serve TLS now" and which would put the Setup Wizard behind an
  untrusted certificate — the exact thing the mode exists to avoid;
- the generated Windows shim still matches the regex the Electron app uses
  to find the real ``cremind.exe`` (``ui/electron/main.ts``). That one is a
  cross-language coupling with no compiler to catch it.

The POSIX wrapper's own parsing is exercised end-to-end further down, on
platforms that can run ``/bin/sh``.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PS1 = REPO_ROOT / "install" / "install.ps1"
SH = REPO_ROOT / "install" / "install.sh"


def _ps1() -> str:
    return PS1.read_text(encoding="utf-8")


def _sh() -> str:
    return SH.read_text(encoding="utf-8")


# ── the flag itself ───────────────────────────────────────────────────────


def test_flag_declared_in_both_installers() -> None:
    assert "[ValidateSet('','none','auto','after-setup')] [string] $Ssl" in _ps1()
    sh = _sh()
    assert "--ssl)" in sh and "--ssl=*)" in sh
    # Same accepted set on both sides, or one platform silently takes a value
    # the other rejects.
    assert re.search(r'""\|none\|auto\|after-setup\)', sh)


def test_flag_documented_in_both_help_texts() -> None:
    assert ".PARAMETER Ssl" in _ps1()
    # install.sh's --help prints its own header block verbatim.
    header = _sh().split("set -e", 1)[0]
    assert "--ssl none|auto|after-setup" in header


def test_default_is_after_setup_in_both() -> None:
    assert "$SslMode = 'after-setup'" in _ps1()
    assert 'SSL_MODE="after-setup"' in _sh()


# ── persistence: the choice must outlive the installing shell ─────────────


@pytest.mark.parametrize("key", ["CREMIND_SSL=", "CREMIND_SSL_AUTO_HOSTS="])
def test_resolved_mode_is_written_to_both_env_files(key: str) -> None:
    """Docker's compose .env and the native .env both get the stamp.

    Without the docker stamp, a later ``docker compose up -d`` from a clean
    terminal recreates the container with TLS off while APP_URL still says
    https://. Without the native stamp, the manual restart the wizard asks
    for under after-setup comes back on plain HTTP.
    """
    ps1, sh = _ps1(), _sh()
    assert ps1.count(f'"{key}$') >= 1 or ps1.count(f'"{key}') >= 2, key
    assert sh.count(f"'{key}%s\\n'") >= 2, key


def test_docker_stamp_writes_even_when_empty() -> None:
    """An empty stamp is how a re-install tells "chose http" from "no prior install"."""
    assert 'Add-Content -Path $EnvDocker -Value "CREMIND_SSL=$SslMode"' in _ps1()
    assert "printf 'CREMIND_SSL=%s\\n' \"$SSL_MODE\" >>\"$DOCKER_DIR/.env\"" in _sh()


# ── the after-setup gate (both halves) ────────────────────────────────────


def test_after_setup_gates_bootstrap_and_migrate_in_ps1() -> None:
    ps1 = _ps1()
    gate = "-not ($SslMode -eq 'after-setup' -and -not (Test-Path $BootstrapFile))"
    # Once for the bootstrap.toml write, once for ``db upgrade`` — which
    # writes bootstrap.toml itself when the file is missing.
    assert ps1.count(gate) == 2


def test_after_setup_gates_bootstrap_and_migrate_in_sh() -> None:
    sh = _sh()
    assert sh.count('[ "$SKIP_BOOTSTRAP_FOR_TLS" = "0" ]') == 2
    assert '"$SSL_MODE" = "after-setup"' in sh


# ── boot scheme after the wizard has already run ──────────────────────────


def test_boot_scheme_accounts_for_completed_setup() -> None:
    """A re-install of a finished install must probe/print https, not http.

    ``after-setup`` defers TLS only until ``bootstrap.toml`` exists; past that
    the server binds TLS from boot one (``app/server.py``). Re-running the
    installer is the documented upgrade path, so without this the health gate
    probes http:// against a TLS listener, burns its whole budget, and then
    hands the user a wizard URL that cannot load. Latent before the flag —
    ``after-setup`` had to be exported by hand every run — and reachable on
    every upgrade now that it is the default and is written into the .env.
    """
    ps1, sh = _ps1(), _sh()
    # Native: the host can see the marker, so the helper takes it as input.
    assert "[switch] $SetupComplete" in ps1
    assert "-and -not $SetupComplete) { return 'http' }" in ps1
    assert "-SetupComplete:(Test-Path $BootstrapFile)" in ps1
    assert 'setup_complete="${2:-0}"' in sh
    assert '[ "$setup_complete" != "1" ]' in sh
    assert 'cremind_boot_scheme "$ENV_FILE" "$SETUP_COMPLETE"' in sh
    # Docker: the marker lives in the container's volume, so the installer
    # probes both schemes and believes whichever answers.
    assert "$BootCandidates += 'https'" in ps1
    assert 'BOOT_CANDIDATES="$BOOT_SCHEME https"' in sh


# ── cross-language coupling with the Electron main process ────────────────


def test_generated_cmd_shim_matches_electron_regex() -> None:
    """``ui/electron/main.ts`` parses the exe path out of the .cmd shim.

    It falls back to a hardcoded default path when the parse fails, which
    silently breaks dev and relocated installs instead of erroring.
    """
    main_ts = (REPO_ROOT / "ui" / "electron" / "main.ts").read_text(encoding="utf-8")
    # Pull the pattern out of main.ts so this test tracks the real thing.
    m = re.search(r"content\.match\((/[^/]+/)m\)", main_ts)
    assert m, "could not find the shim-parsing regex in main.ts"
    js_pattern = m.group(1)[1:-1]          # strip the / delimiters
    py_pattern = js_pattern.replace(r"\s", r"[ \t]")

    body = re.search(r"\$CremindCmdBody = @'\r?\n(.*?)\r?\n'@", _ps1(), re.S)
    assert body, "could not find the cmd shim body in install.ps1"
    shim = body.group(1).replace("__VENV_CREMIND__", r"C:\Users\x\.cremind\venv\Scripts\cremind.exe")
    found = re.search(py_pattern, shim, re.M)
    assert found, f"generated shim does not match Electron's {js_pattern}"
    assert found.group(1).endswith("cremind.exe")


# ── the POSIX wrapper actually parses a .env correctly ────────────────────


ENV_FIXTURE = """\
# a comment
APP_URL=https://localhost:1515

CREMIND_SSL=after-setup
LOG_LEVEL=INFO
EQUALS_IN_VALUE=a=b
export EXPORTED_KEY=yes
QUOTED_PATH="/tmp/some dir"
not a valid line
2FA_MODE=digit-leading
BAD-KEY=hyphen
"""


@pytest.mark.skipif(os.name == "nt", reason="needs /bin/sh")
def test_posix_wrapper_loads_env_and_lets_real_env_win() -> None:
    """Generate the wrapper with the installer's own heredoc, then run it.

    The block is run verbatim rather than reimplemented, so the thing under
    test is the text that actually ships.
    """
    # Capture the chmod too — a wrapper without the executable bit is a hard
    # install failure, and it is one line away from being forgotten.
    block = re.search(
        r'(cat > "\$BIN_DIR/cremind" <<EOF\n.*?\nEOF\nchmod \+x "\$BIN_DIR/cremind"\n)',
        _sh(), re.S,
    )
    assert block, "could not find the POSIX wrapper heredoc (+ chmod) in install.sh"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "venv" / "bin").mkdir(parents=True)
        target = root / "venv" / "bin" / "cremind"
        target.write_text(
            "#!/bin/sh\n"
            'echo "CREMIND_SSL=${CREMIND_SSL:-}"\n'
            'echo "LOG_LEVEL=${LOG_LEVEL:-}"\n'
            'echo "EQUALS_IN_VALUE=${EQUALS_IN_VALUE:-}"\n'
            'echo "EXPORTED_KEY=${EXPORTED_KEY:-}"\n'
            'echo "QUOTED_PATH=${QUOTED_PATH:-}"\n'
            "exit 7\n",
            encoding="utf-8",
        )
        target.chmod(target.stat().st_mode | stat.S_IEXEC)

        generator = root / "generate.sh"
        generator.write_text(
            f'VENV_DIR="{root.as_posix()}/venv"\n'
            f'BIN_DIR="{root.as_posix()}"\n'
            f"{block.group(1)}",
            encoding="utf-8",
        )
        subprocess.run(["bash", str(generator)], capture_output=True, text=True, check=True)

        wrapper = root / "cremind"
        assert wrapper.exists(), "the heredoc did not produce a wrapper"
        assert os.access(wrapper, os.X_OK), "the wrapper must be executable"

        (root / ".env").write_text(ENV_FIXTURE, encoding="utf-8")
        env = {**os.environ, "CREMIND_SYSTEM_DIR": str(root), "LOG_LEVEL": "DEBUG"}
        out = subprocess.run([str(wrapper)], capture_output=True, text=True, env=env)

        # A digit-leading key is the sharp one: it passes a naive
        # "word characters only" filter but makes ``${2FA_MODE+x}`` a bad
        # substitution, and `eval` is a special built-in — the wrapper would
        # die before exec'ing anything, breaking EVERY cremind invocation over
        # one stray line in the user's .env.
        assert out.returncode == 7, out.stderr           # exit code passes through
        assert not out.stderr.strip(), out.stderr        # no shell diagnostics
        assert "CREMIND_SSL=after-setup" in out.stdout   # .env is loaded
        assert "LOG_LEVEL=DEBUG" in out.stdout           # real env wins
        assert "EQUALS_IN_VALUE=a=b" in out.stdout       # split on the FIRST '='
        assert "EXPORTED_KEY=yes" in out.stdout          # 'export ' prefix tolerated
        assert "QUOTED_PATH=/tmp/some dir" in out.stdout # quotes stripped

        # And with no .env at all it must still hand over cleanly.
        (root / ".env").unlink()
        bare = subprocess.run([str(wrapper)], capture_output=True, text=True, env=env)
        assert bare.returncode == 7, bare.stderr
        assert "CREMIND_SSL=\n" in bare.stdout


def test_posix_wrapper_is_written_not_symlinked_through() -> None:
    """``cat >`` through the old symlink would clobber the venv entry script."""
    sh = _sh()
    rm = sh.index('rm -f "$BIN_DIR/cremind"')
    cat = sh.index('cat > "$BIN_DIR/cremind" <<EOF')
    assert rm < cat, "the wrapper must be unlinked before it is written"


# ── nothing regressed in how a plain-HTTP install reads ───────────────────


def test_none_clears_inherited_env_in_both() -> None:
    """Otherwise the opt-out is a lie: the scheme helper still answers https."""
    ps1, sh = _ps1(), _sh()
    assert "Remove-Item Env:CREMIND_SSL -ErrorAction SilentlyContinue" in ps1
    assert "unset CREMIND_SSL CREMIND_SSL_CERTFILE" in sh


def test_electron_is_exempt_in_both_installers() -> None:
    """The desktop app runs install.sh on macOS/Linux and install.ps1 on Windows.

    It loads the UI over http://127.0.0.1, where the server refuses to bind
    TLS at all, so an https APP_URL there describes an origin that will never
    exist. Both scripts must exempt it — and must clear an INHERITED
    CREMIND_SSL too, or the exemption leaks.
    """
    ps1, sh = _ps1(), _sh()
    assert "$env:CREMIND_INSTALLER_FRONTEND -eq 'electron'" in ps1
    assert '[ "${CREMIND_INSTALLER_FRONTEND:-}" = "electron" ]' in sh
    for script, unset in ((ps1, "Remove-Item Env:CREMIND_SSL_CERTFILE"),
                          (sh, "unset CREMIND_SSL CREMIND_SSL_CERTFILE")):
        # Once in the Electron arm, once in the explicit-none tail.
        assert script.count(unset) >= 2, unset


def test_none_does_not_downgrade_urls_when_a_certfile_still_serves_tls() -> None:
    """A cert pair in a kept .env is a separate, documented way to ask for TLS.

    Rewriting APP_URL to http:// while that pair still binds TLS would leave
    the install describing an origin it does not serve.
    """
    assert "^CREMIND_SSL_CERTFILE\\s*=\\s*\\S" in _ps1()
    assert "^CREMIND_SSL_CERTFILE[[:space:]]*=[[:space:]]*[^[:space:]]" in _sh()


# ── host-side CA trust (docker) ───────────────────────────────────────────
#
# A Docker install's CA lives inside the container, where the server's own
# one-click trust (POST /api/tls/trust, native-only) can never reach the
# host's store — so the INSTALLER is the only process that can automate the
# trust step there. These guards keep that block present, offered rather
# than forced, and mirrored across both scripts.


def test_docker_installs_offer_host_ca_trust_in_both() -> None:
    ps1, sh = _ps1(), _sh()
    # Both fetch the CA from the running container's public endpoint...
    assert re.search(r"host-side CA trust[\s\S]{0,3000}ca\.pem", ps1)
    assert re.search(r"host-side CA trust[\s\S]{0,3000}ca\.pem", sh)
    # ...and write the store a browser actually consults on that host.
    assert "X509Store]::new('Root', 'CurrentUser')" in ps1
    assert "update-ca-certificates" in sh and "add-trusted-cert" in sh


def test_host_ca_trust_is_offered_not_forced() -> None:
    """Installing a root CA needs a consent moment. On Windows the OS dialog
    provides a second one, but the scripts must ask first — and honour a No."""
    ps1, sh = _ps1(), _sh()
    assert re.search(r"host-side CA trust[\s\S]{0,4000}Read-Host", ps1)
    assert re.search(r"host-side CA trust[\s\S]{0,4500}read -r -p \"Trust it", sh)


def test_host_ca_trust_skips_unattended_and_electron() -> None:
    """--unattended has nobody to consent, and under Electron the server
    never serves TLS — both must skip the block entirely."""
    ps1, sh = _ps1(), _sh()
    m = re.search(
        r"if \(\$env:CREMIND_INSTALLER_FRONTEND -ne 'electron' -and "
        r"-not \$Unattended -and \$UrlScheme -eq 'https'\)",
        ps1,
    )
    assert m, "install.ps1 lost the trust block's gate"
    assert re.search(
        r'\[ "\$\{CREMIND_INSTALLER_FRONTEND:-\}" != "electron" \] '
        r'&& \[ "\$UNATTENDED" -eq 0 \]',
        sh,
    ), "install.sh lost the trust block's gate"


def test_host_ca_trust_skips_when_already_trusted() -> None:
    """A re-install must not pop the OS dialog / sudo prompt again."""
    ps1, sh = _ps1(), _sh()
    assert re.search(r"host-side CA trust[\s\S]{0,4000}FindByThumbprint", ps1)
    # Linux compares the shipped anchor; macOS asks the keychain.
    assert re.search(r"host-side CA trust[\s\S]{0,5000}cmp -s", sh)
    assert re.search(r"host-side CA trust[\s\S]{0,5000}find-certificate", sh)
