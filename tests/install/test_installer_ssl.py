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
import shlex
import shutil
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


def _ssl_resolution(script: str) -> str:
    # Include the real normalisation/scheme helpers as well as the resolution
    # block so the extracted script exercises the same alias semantics that
    # build APP_URL and the health target in a full install.
    start = script.index("# Only a mode the server recognises")
    end = re.search(r"\n# [^\n]* boot service [^\n]*\n", script[start:])
    assert end
    return script[start:start + end.start()]


def _shell(kind: str) -> str:
    if kind == "ps1":
        executable = shutil.which("pwsh") or shutil.which("powershell")
    elif os.name != "nt":
        executable = shutil.which("bash")
    else:
        # Windows' system32/bash.exe is a WSL launcher, not a local shell.
        candidates = [
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe",
            Path.home() / "scoop/apps/git/current/bin/bash.exe",
        ]
        executable = next((str(p) for p in candidates if p.is_file()), None)
    if not executable:
        pytest.skip(f"{kind} shell unavailable")
    return executable


@pytest.mark.parametrize("kind", ["sh", "ps1"])
@pytest.mark.parametrize("deployment", ["local", "server", "custom"])
@pytest.mark.parametrize("frontend", ["", "electron"])
def test_fresh_install_defaults_to_http(kind, deployment, frontend, tmp_path) -> None:
    mode, ssl, cert = _resolve_ssl(kind, tmp_path, deployment=deployment, frontend=frontend)
    assert (mode, ssl, cert) == ("", "", "")


def _resolve_ssl(kind, root, *, flag="", inherited="", certificate="", keyfile="",
                 key_password="", auto_hosts="", previous=None, deployment="local",
                 frontend="", install_mode="native", choice="", full=False):
    """Execute the real resolution block without installing or starting anything."""
    native_env = root / "native.env"
    docker_dir = root / "docker"
    docker_dir.mkdir(exist_ok=True)
    target_env = docker_dir / ".env" if install_mode == "docker" else native_env
    if previous is not None:
        target_env.write_text(previous, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("CREMIND_SSL")}
    env.update(
        CREMIND_SSL=inherited,
        CREMIND_SSL_CERTFILE=certificate,
        CREMIND_SSL_KEYFILE=keyfile,
        CREMIND_SSL_KEYFILE_PASSWORD=key_password,
        CREMIND_SSL_AUTO_HOSTS=auto_hosts,
        CREMIND_INSTALLER_FRONTEND=frontend,
    )
    variables = {
        "ENV_FILE": native_env.as_posix(), "CREMIND_INSTALL_DIR": root.as_posix(),
        "MODE": install_mode, "DEPLOYMENT": deployment,
        "SSL_MODE": flag, "SSL_CHOICE": choice,
    }
    if kind == "sh":
        prelude = "set -euo pipefail\nwarn() { :; }\nUNATTENDED=1\n"
        prelude += f"SSL_EXPLICIT={int(bool(flag))}\n"
        prelude += "".join(f"{key}={shlex.quote(value)}\n" for key, value in variables.items())
        body = prelude + _ssl_resolution(_sh())
        body += ('\nprintf "RESULT=%s|%s|%s|%s|%s|%s|%s\\n" '
                 '"$SSL_MODE" "${CREMIND_SSL:-}" "${CREMIND_SSL_CERTFILE:-}" '
                 '"${CREMIND_SSL_KEYFILE:-}" "${CREMIND_SSL_KEYFILE_PASSWORD:-}" '
                 '"${CREMIND_SSL_AUTO_HOSTS:-}" "$(cremind_scheme)"\n')
        command = [_shell(kind)]
    else:
        names = {"ENV_FILE": "EnvFile", "CREMIND_INSTALL_DIR": "CremindInstallDir",
                 "MODE": "Mode", "DEPLOYMENT": "Deployment", "SSL_MODE": "Ssl"}
        prelude = "$ErrorActionPreference = 'Stop'\nfunction Write-Warn2 { param($message) }\n$Unattended = $true\n"
        for key, name in names.items():
            prelude += f"${name} = '" + variables[key].replace("'", "''") + "'\n"
        body = prelude + _ssl_resolution(_ps1())
        body += ('\nWrite-Output "RESULT=$SslMode|$env:CREMIND_SSL|$env:CREMIND_SSL_CERTFILE|'
                 '$env:CREMIND_SSL_KEYFILE|$env:CREMIND_SSL_KEYFILE_PASSWORD|'
                 '$env:CREMIND_SSL_AUTO_HOSTS|$(Get-CremindScheme)"\n')
        command = [_shell(kind), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"]
    script = root / f"resolve.{kind}"
    script.write_text(body, encoding="utf-8-sig" if kind == "ps1" else "utf-8", newline="\n")
    result = subprocess.run([*command, script.as_posix()], capture_output=True, text=True, env=env, timeout=20)
    assert result.returncode == 0, result.stderr
    line = next(line for line in result.stdout.splitlines() if line.startswith("RESULT="))
    values = tuple(line.removeprefix("RESULT=").split("|"))
    return values if full else values[:3]


@pytest.mark.parametrize("kind", ["sh", "ps1"])
@pytest.mark.parametrize("flag, expected", [("none", ""), ("auto", "auto"), ("after-setup", "after-setup")])
def test_explicit_ssl_works_in_electron_and_overrides_inherited_mode(kind, flag, expected, tmp_path) -> None:
    mode, ssl, _ = _resolve_ssl(kind, tmp_path, flag=flag, inherited="auto", frontend="electron")
    assert (mode, ssl) == (expected, expected)


@pytest.mark.parametrize("kind", ["sh", "ps1"])
@pytest.mark.parametrize("install_mode", ["native", "docker"])
@pytest.mark.parametrize("previous, expected", [("APP_URL=http://localhost:1515\n", ""),
                                                 ("CREMIND_SSL=\n", ""),
                                                 ("CREMIND_SSL=none\n", ""),
                                                 ("CREMIND_SSL=false\n", ""),
                                                 ("CREMIND_SSL=0\n", ""),
                                                 ("CREMIND_SSL=no\n", ""),
                                                 ("CREMIND_SSL=auto\n", "auto"),
                                                 ("CREMIND_SSL=after-setup\n", "after-setup")])
def test_reinstall_keeps_previous_transport(kind, install_mode, previous, expected, tmp_path) -> None:
    mode, ssl, _ = _resolve_ssl(kind, tmp_path, previous=previous, install_mode=install_mode)
    assert (mode, ssl) == (expected, expected)


@pytest.mark.parametrize("kind", ["sh", "ps1"])
@pytest.mark.parametrize("value", ["none", "false", "0", "no", "FALSE", " false "])
def test_false_like_ssl_aliases_stay_http(kind, value, tmp_path) -> None:
    state = _resolve_ssl(kind, tmp_path, inherited=value, full=True)
    assert state[0:2] == ("", "")
    assert state[6] == "http"


@pytest.mark.parametrize("kind", ["sh", "ps1"])
@pytest.mark.parametrize("value", ["true", "1", "yes", "TRUE", " true "])
def test_true_like_ssl_aliases_are_canonical_auto(kind, value, tmp_path) -> None:
    state = _resolve_ssl(kind, tmp_path, inherited=value, full=True)
    assert state[0:2] == ("auto", "auto")
    assert state[6] == "https"


@pytest.mark.parametrize("kind", ["sh", "ps1"])
def test_docker_reinstall_stages_complete_custom_tls_configuration(kind, tmp_path) -> None:
    previous = """\
CREMIND_SSL=
CREMIND_SSL_CERTFILE=/certs/fullchain.pem
CREMIND_SSL_KEYFILE=/certs/privkey.pem
CREMIND_SSL_KEYFILE_PASSWORD=correct-horse
CREMIND_SSL_AUTO_HOSTS=chat.example.test,10.0.0.8
"""
    state = _resolve_ssl(
        kind, tmp_path, previous=previous, install_mode="docker", full=True,
    )
    assert state == (
        "", "", "/certs/fullchain.pem", "/certs/privkey.pem", "correct-horse",
        "chat.example.test,10.0.0.8", "https",
    )


@pytest.mark.parametrize("kind", ["sh", "ps1"])
def test_process_custom_tls_overrides_previous_docker_pair(kind, tmp_path) -> None:
    previous = "CREMIND_SSL_CERTFILE=/old/cert.pem\nCREMIND_SSL_KEYFILE=/old/key.pem\n"
    state = _resolve_ssl(
        kind, tmp_path, previous=previous, install_mode="docker",
        certificate="/new/cert.pem", keyfile="/new/key.pem", full=True,
    )
    assert state[2:4] == ("/new/cert.pem", "/new/key.pem")
    assert state[6] == "https"


@pytest.mark.parametrize("kind", ["sh", "ps1"])
def test_explicit_http_clears_inherited_certificate(kind, tmp_path) -> None:
    assert _resolve_ssl(kind, tmp_path, flag="none", inherited="auto", certificate="/certs/server.pem") == ("", "", "")


@pytest.mark.parametrize("kind", ["sh", "ps1"])
def test_explicit_http_clears_previous_docker_certificate(kind, tmp_path) -> None:
    previous = "CREMIND_SSL_CERTFILE=/certs/server.pem\nCREMIND_SSL_KEYFILE=/certs/server.key\n"
    state = _resolve_ssl(
        kind, tmp_path, flag="none", previous=previous, install_mode="docker", full=True,
    )
    assert state[0:5] == ("", "", "", "", "")
    assert state[6] == "http"


@pytest.mark.parametrize("choice, expected", [("none", ""), ("after-setup", "after-setup")])
def test_tui_choice_is_applied_without_overwriting_flags(choice, expected, tmp_path) -> None:
    assert _resolve_ssl("sh", tmp_path, choice=choice)[:2] == (expected, expected)
    assert _resolve_ssl("sh", tmp_path, choice=choice, flag="auto")[:2] == ("auto", "auto")


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


def test_docker_rewrite_restores_every_tls_input() -> None:
    ps1, sh = _ps1(), _sh()
    for suffix in ("CERTFILE", "KEYFILE", "KEYFILE_PASSWORD", "AUTO_HOSTS"):
        assert f"CREMIND_SSL_{suffix}=$ResolvedSsl" in ps1
        assert f"CREMIND_SSL_{suffix}=%s\\n' \"$RESOLVED_SSL_" in sh


def test_native_install_persists_environment_tls_overrides() -> None:
    """Process-level TLS settings must survive the installer's own process.

    The native boot service and Electron launch from the canonical ``.env``;
    merely exporting a certificate or mode while installing loses HTTPS on
    the first restart. Reinstalls must also let a false-like environment mode
    clear an older certificate pair and HTTPS URL.
    """
    ps1, sh = _ps1(), _sh()
    assert "$SslExplicit -or $SslEnvironmentExplicit" in ps1
    assert '[ "$SSL_EXPLICIT" = "1" ] || [ "$SSL_ENVIRONMENT_EXPLICIT" = "1" ]' in sh

    for suffix in ("CERTFILE", "KEYFILE", "KEYFILE_PASSWORD"):
        assert f'Add-Content -Path $EnvFile -Value "CREMIND_SSL_{suffix}=$ResolvedSsl' in ps1
        assert f"printf 'CREMIND_SSL_{suffix}=%s\\n' \"$RESOLVED_SSL_" in sh
        assert f"Set-CremindEnvKey -Path $EnvFile -Key 'CREMIND_SSL_{suffix}'" in ps1
        assert f'upsert_env_key "$ENV_FILE" CREMIND_SSL_{suffix} "$RESOLVED_SSL_' in sh

    assert 'Add-Content -Path $EnvFile -Value "CREMIND_SSL_AUTO_HOSTS=$ResolvedSslAutoHosts"' in ps1
    assert "printf 'CREMIND_SSL_AUTO_HOSTS=%s\\n' \"$RESOLVED_SSL_AUTO_HOSTS\" >> \"$ENV_FILE\"" in sh
    assert "if ($UrlScheme -eq 'http')" in ps1
    assert '[ "$URL_SCHEME" = "http" ]' in sh


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
    every upgrade of an install that opted into it and wrote it into the .env.
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
    assert "unset CREMIND_SSL" in sh
    assert "unset CREMIND_SSL_CERTFILE CREMIND_SSL_KEYFILE" in sh


def test_tui_ssl_is_forwarded_and_read_in_both_installers() -> None:
    ps1, sh = _ps1(), _sh()
    assert "'SSL_CHOICE'" in ps1
    assert "'none', 'auto', 'after-setup'" in ps1
    for script in (ps1, sh):
        assert "--ssl-inherited" in script
        assert "--native-env" in script
        assert "--docker-env" in script
        assert "Enable HTTPS (SSL)? [y/N]" in script
        assert "CREMIND_INSTALLER_FRONTEND" not in _ssl_resolution(script)


def test_explicit_none_clears_kept_custom_certificate_before_downgrade() -> None:
    """An explicit HTTP choice must also disable certificate-driven TLS.

    Certificate paths independently enable the TLS listener, so leaving them
    in a kept .env while rewriting APP_URL to HTTP would make ``none`` a lie.
    Both installers clear the full pair (and an optional key password) first.
    """
    ps1, sh = _ps1(), _sh()
    resolved_names = {
        "CREMIND_SSL_CERTFILE": "ResolvedSslCertFile",
        "CREMIND_SSL_KEYFILE": "ResolvedSslKeyFile",
        "CREMIND_SSL_KEYFILE_PASSWORD": "ResolvedSslKeyFilePassword",
    }
    for key, resolved in resolved_names.items():
        suffix = key.removeprefix("CREMIND_SSL_")
        assert f"Set-CremindEnvKey -Path $EnvFile -Key '{key}' -Value ${resolved}" in ps1
        assert f'upsert_env_key "$ENV_FILE" {key} "$RESOLVED_SSL_{suffix}"' in sh
    assert "if ($SslExplicit) {\n    $ResolvedSslCertFile = ''" in ps1
    assert 'if [ "$SSL_EXPLICIT" = "1" ]; then\n    RESOLVED_SSL_CERTFILE=""' in sh


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
    manages certificate trust in the desktop app — both must skip the block entirely."""
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


# ── the installer → Setup Wizard trust hand-off ───────────────────────────
#
# A container install's server can only ever read the CONTAINER's trust store
# (``_trust_environment_error`` in app/api/tls.py refuses docker/kubernetes
# outright, which forces ``local_trust.already_trusted`` to null). So when the
# block above writes the HOST's store, the installer is the only witness — and
# without these guards it threw that fact away, leaving the wizard to demand
# the same work again by hand. It now says so on the wizard URL it opens.
#
# What must not rot: the hand-off is emitted ONLY from evidence that names the
# exact certificate, and never from a URL meant for another device.


def _sh_trust_helper(sh: str) -> str:
    """Just ``offer_host_ca_trust``'s body. Slicing the whole script instead
    would hit the uninstall block's own ``Darwin)`` case first."""
    start = sh.index("offer_host_ca_trust() {")
    return sh[start:sh.index("ca_trust_hint_url() {", start)]


def _between(text: str, start: str, end: str) -> str:
    at = text.index(start)
    return text[at:text.index(end, at)]


def test_host_ca_trust_declares_its_output_before_the_block() -> None:
    """``set -u`` / ``Set-StrictMode`` make an unset read fatal, and the whole
    block is skipped in most install modes — so the variable has to exist
    before anything reads it."""
    ps1, sh = _ps1(), _sh()
    assert sh.index('HOST_CA_TRUST_FP=""') < sh.index("host-side CA trust")
    assert ps1.index("$script:HostCaTrustFingerprint = ''") < ps1.index("host-side CA trust")


def test_host_ca_trust_hint_only_on_fingerprint_exact_evidence() -> None:
    """The hint names a CA, so it may only come from evidence that identified
    that exact CA: install.ps1's thumbprint lookup, install.sh's byte-for-byte
    anchor comparison, or a trust command that just succeeded.

    install.sh's macOS branch matches by COMMON NAME. After a CA regeneration
    it reports "already trusts" for the OLD CA while the fingerprint would name
    the NEW one — a hint that matches the server exactly and is a lie. It must
    stay out of this.
    """
    ps1, helper = _ps1(), _sh_trust_helper(_sh())

    darwin = _between(helper, "            Darwin)", "            *)")
    assert "find-certificate" in darwin, "wrong slice — the macOS branch moved"
    assert "HOST_CA_VERIFIED=1" not in darwin

    linux = _between(helper, "                if [ -d /usr/local", "        esac")
    assert "cmp -s" in linux, "wrong slice — the Linux anchor comparison moved"
    assert "HOST_CA_VERIFIED=1" in linux

    # Declining stays silent; only the branch that ran the command speaks.
    declined = _between(helper, "                [nN]*)", ";;")
    assert "Skipped. The Setup Wizard" in declined, "wrong slice — the decline branch moved"
    assert "HOST_CA_VERIFIED=1" not in declined
    granted = _between(helper, 'if sh -c "$CA_TRUST_CMD"; then', "else")
    assert "HOST_CA_VERIFIED=1" in granted
    failed = _between(helper, 'warn "CA not trusted', "fi")
    assert "HOST_CA_VERIFIED=1" not in failed
    assert re.search(
        r"\$CaStore\.Add\(\$CaCert\)\s*\n\s*#[\s\S]{0,200}?\$script:HostCaTrustFingerprint = \$CaHintFp",
        ps1,
    ), "install.ps1 must publish only after Add() returned"
    ps1_catch = ps1[ps1.index("Includes the user clicking No"):]
    assert "$script:HostCaTrustFingerprint" not in ps1_catch[:600]


def test_container_wizard_urls_carry_the_trust_hint() -> None:
    """Docker and Kubernetes are exactly the two modes that call the trust
    block, and the two whose server cannot check the host store itself."""
    ps1, sh = _ps1(), _sh()
    assert 'K8S_WIZARD_URL="$(ca_trust_hint_url "${BOOT_SCHEME}://localhost:1515/#/setup")"' in sh
    assert 'Get-CremindWizardTrustHintUrl "${BootScheme}://localhost:1515/#/setup"' in ps1
    # Docker hands off only for a loopback host — a `custom` deployment's URL
    # is meant for somebody else's browser.
    assert re.search(r"localhost\|127\.0\.0\.1[\s\S]{0,120}ca_trust_hint_url", sh)
    assert re.search(
        r"@\('localhost', '127\.0\.0\.1'[\s\S]{0,200}Get-CremindWizardTrustHintUrl \$WizardUrl",
        ps1,
    )
    # The query rides INSIDE the hash: the UI router is createWebHashHistory,
    # so anything before the '#' never reaches route.query.
    assert "'%s?ca_trusted=%s' \"$1\" \"$HOST_CA_TRUST_FP\"" in sh
    assert '"${Url}?ca_trusted=$($script:HostCaTrustFingerprint)"' in ps1


def test_ingress_and_native_wizard_urls_are_never_hinted() -> None:
    """An ingress serves a different chain than the port-forward we trusted,
    and native never runs the trust block at all (the server's own one-click
    covers it). Native also strips a '/#/setup' suffix, which a query would
    silently defeat."""
    ps1, sh = _ps1(), _sh()
    assert 'K8S_WIZARD_URL="${K8S_POD_APP_URL%/}/#/setup"' in sh
    assert "$K8sWizardUrl = ($K8sPodAppUrl.TrimEnd('/')) + '/#/setup'" in ps1
    native_sh = sh[sh.index('WIZARD_URL="${BOOT_SCHEME}://$APP_HOST:1515/#/setup"'):]
    assert "ca_trust_hint_url" not in native_sh
    assert "${WIZARD_URL%/#/setup}" in native_sh
    native_ps1 = ps1[ps1.index('$WizardUrl = "${BootScheme}://${AppHost}:1515/#/setup"'):]
    assert "Get-CremindWizardTrustHintUrl" not in native_ps1


def test_trust_block_proximity_budget_has_headroom() -> None:
    """The guards above are distance-bounded regexes over the raw script. When
    the budget runs out they fail as ``assert None`` somewhere else entirely,
    so measure it here and fail with the number instead."""
    sh, ps1 = _sh(), _ps1()
    anchor = sh.index("host-side CA trust")
    spent = sh.index('read -r -p "Trust it', anchor) - anchor
    assert spent < 4500, (
        f"install.sh: {spent} chars between the 'host-side CA trust' anchor and the "
        f"prompt, over the 4500 budget in test_host_ca_trust_is_offered_not_forced. "
        f"Move additions after the prompt, or raise both bounds together."
    )
    ps1_anchor = ps1.index("host-side CA trust")
    ps1_spent = ps1.index("Read-Host", ps1_anchor) - ps1_anchor
    assert ps1_spent < 4000, f"install.ps1: {ps1_spent} chars, over the 4000 budget"
