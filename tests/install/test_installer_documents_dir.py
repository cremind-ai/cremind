"""Parity + contract guards for the installers' Docker documents folder.

A Docker install binds one host folder at ``/root/Documents`` — what Documentation
search indexes and where the agent works. The two installers are
hand-maintained mirrors, nothing executes either one end to end in CI, and the
value crosses four parsers on its way (the TUI's output file, bash's ``.``,
install.ps1's read-back regex, and compose's ``.env``). So the couplings
asserted here are the ones that rot silently:

- the flags exist, are documented, fall back to the environment, and reach
  the TUI from *both* of install.sh's launch blocks; install.ps1's read-back
  is a switch whitelist where a missing case drops the answer with no error;
- the previous install's answers are read BEFORE the TUI, so a re-run offers
  them and an unattended one keeps them;
- the ``.env`` lines are appended (printf / Add-Content), never substituted
  into a placeholder, and only after a validation that refuses exactly what
  compose's unquoted ``.env`` parsing would mangle — with the character named;
- install.ps1's apostrophe read-back: it used to match ``'\\\\''`` instead of
  the ``'\\''`` the TUI writes, so ``John's Documents`` came back corrupted.

The validation functions themselves run for real where a bash / PowerShell is
available, and a docker-gated test proves the accepted characters survive
compose's ``.env`` parser (and the refused ones would not).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from app.installer.output import _sh_quote

REPO_ROOT = Path(__file__).resolve().parents[2]
PS1 = REPO_ROOT / "install" / "install.ps1"
SH = REPO_ROOT / "install" / "install.sh"
CATALOG_TOML = REPO_ROOT / "install" / "catalog.toml"
CATALOG_SH = REPO_ROOT / "install" / "_catalog.sh"
CATALOG_PS1 = REPO_ROOT / "install" / "_catalog.ps1"
ENV_TMPL = REPO_ROOT / "install" / "templates" / "docker.env.tmpl"
COMPOSE_TMPL = REPO_ROOT / "install" / "templates" / "docker-compose.yml.tmpl"
README = REPO_ROOT / "install" / "README.md"
OUTPUT_PY = REPO_ROOT / "app" / "installer" / "output.py"
MAIN_PY = REPO_ROOT / "app" / "installer" / "__main__.py"

#: Every documents answer the TUI can produce, as (output key, sh flag, ps
#: parameter). The TUI's output file is SOURCED by install.sh, so the shell
#: variable carries the output key's name: the shell passes its value in, the
#: TUI echoes it back, and sourcing is a no-op for a flag-supplied answer.
ANSWER_KEYS = [
    ("DOCUMENTS_DIR_INPUT", "--documents-dir", "DocumentsDir"),
    ("DOCUMENTS_ACCESS_INPUT", "--documents-access", "DocumentsAccess"),
]

#: The .env keys the installers append. Never TUI output keys.
ENV_KEYS = ["CREMIND_HOST_DOCUMENTS", "CREMIND_DOCUMENTS_READ_ONLY", "CREMIND_COMPOSE_HOST_DIR"]

#: The [docker_documents] catalog keys the shells, the TUI and the tests share.
CATALOG_KEYS = [
    "prompt", "hint", "access_prompt", "rw_label", "rw_disclosure", "ro_label",
    "ro_disclosure", "linux_owner_note", "macos_privacy_note", "wsl_note",
]


def _ps1() -> str:
    return PS1.read_text(encoding="utf-8")


def _sh() -> str:
    return SH.read_text(encoding="utf-8")


def _sh_tui_launch() -> str:
    sh = _sh()
    start = sh.index("tui_run_bootstrap() {")
    return sh[start:sh.index("\ntui_run_bootstrap\n", start)]


def _sh_function(name: str) -> str:
    sh = _sh()
    start = sh.index(f"{name}() {{")
    return sh[start:sh.index("\n}\n", start) + 3]


def _ps1_function(name: str) -> str:
    ps1 = _ps1()
    start = ps1.index(f"function {name} {{")
    return ps1[start:ps1.index("\n}\n", start) + 3]


def _between(script: str, start: str, end: str) -> str:
    first = script.index(start)
    return script[first:script.index(end, first)]


def _ps_name(key: str) -> str:
    return "".join(part.capitalize() for part in key.split("_"))


# ── the flags ─────────────────────────────────────────────────────────────


def test_flags_declared_in_both_installers() -> None:
    sh, ps1 = _sh(), _ps1()
    for _key, flag, _param in ANSWER_KEYS:
        assert f"{flag})" in sh and f"{flag}=*)" in sh, flag
    assert "[string] $DocumentsDir = ''" in ps1
    # The read-back assigns this one with Set-Variable, so the ValidateSet is
    # what keeps a stray value out.
    assert "[ValidateSet('','rw','ro')] [string] $DocumentsAccess = ''" in ps1


def test_flags_documented_in_help_and_readme() -> None:
    header = _sh().split("set -e", 1)[0]
    assert "--documents-dir PATH" in header
    assert "--documents-access rw|ro" in header
    ps1 = _ps1()
    for _key, _flag, param in ANSWER_KEYS:
        assert f".PARAMETER {param}" in ps1, param
    readme = README.read_text(encoding="utf-8")
    for needle in ("`--documents-dir PATH`", "`--documents-access rw\\|ro`",
                   "`-DocumentsDir PATH`", "`-DocumentsAccess rw\\|ro`",
                   "### The Documents folder"):
        assert needle in readme, needle


def test_the_environment_is_the_fallback_for_both_flags() -> None:
    """flag > env > previous .env > default: the env step lives up front."""
    sh = _sh()
    assert 'if [ -z "$DOCUMENTS_DIR_INPUT" ] && [ -n "${CREMIND_DOCUMENTS_DIR:-}" ]; then' in sh
    assert 'if [ -z "$DOCUMENTS_ACCESS_INPUT" ] && [ -n "${CREMIND_DOCUMENTS_ACCESS:-}" ]; then' in sh
    ps1 = _ps1()
    assert "if (-not $DocumentsDir -and $env:CREMIND_DOCUMENTS_DIR) {" in ps1
    assert "if (-not $DocumentsAccess -and $env:CREMIND_DOCUMENTS_ACCESS) {" in ps1


def test_a_bad_value_fails_early_in_every_mode() -> None:
    """Like --vnc-password: checked before the TUI, before any mode logic, so
    an unattended install cannot write a .env compose would misread."""
    sh = _sh()
    check = sh.index('err "Invalid $DOCUMENTS_DIR_SOURCE')
    assert check < sh.index("\ntui_run_bootstrap\n")
    assert check < sh.index('step "Docker install"')
    assert 'err "Invalid $DOCUMENTS_ACCESS_SOURCE: $DOCUMENTS_ACCESS_INPUT (must be rw or ro)"' in sh
    ps1 = _ps1()
    check = ps1.index("Write-Err2 \"Invalid $DocumentsDirSource '$DocumentsDir'")
    assert check < ps1.index("\nInvoke-InstallerTuiBootstrap\n")
    assert "Invalid CREMIND_DOCUMENTS_ACCESS:" in ps1
    # ValidateSet and -in ignore case; the TUI's argparse choices do not.
    assert "$DocumentsAccess = $DocumentsAccess.ToLowerInvariant()" in ps1


# ── plumbing through the TUI ──────────────────────────────────────────────


def test_both_tui_launch_blocks_forward_every_answer() -> None:
    """A flag forwarded from only one launch path re-asks a question the
    operator already answered."""
    launch, ps1 = _sh_tui_launch(), _ps1()
    for key, flag, _param in ANSWER_KEYS:
        assert launch.count(f'{flag} "${key}"') == 2, flag
        assert f"$tuiArgs.Add('{flag}')" in ps1, flag
    # Context only: the prefill, never an answer.
    assert launch.count('--documents-default "$DOCUMENTS_DEFAULT"') == 2
    assert "$tuiArgs.Add('--documents-default')" in ps1


def test_powershell_reads_every_answer_back() -> None:
    """The read-back is a switch WHITELIST — an absent case silently discards
    the TUI's answer with no error anywhere."""
    ps1 = _ps1()
    for key, _flag, param in ANSWER_KEYS:
        assert re.search(
            rf"'{re.escape(key)}'\s*\{{[^}}]*Set-Variable -Scope Script {param}\b", ps1
        ), key
    # The access is only taken when it is one the ValidateSet accepts.
    assert "$v -in @('rw', 'ro')) { Set-Variable -Scope Script DocumentsAccess $v }" in ps1


def test_the_answer_keys_are_the_tui_output_keys_and_never_the_env_keys() -> None:
    """install.sh sources the TUI's file and resolves CREMIND_HOST_DOCUMENTS /
    DOCUMENTS_DIR itself; emitting either would clobber that resolution."""
    out = OUTPUT_PY.read_text(encoding="utf-8")
    for key, _flag, _param in ANSWER_KEYS:
        assert f'"{key}"' in out, key
    for forbidden in ENV_KEYS + ["DOCUMENTS_DIR", "DOCUMENTS_READ_ONLY"]:
        assert f'"{forbidden}"' not in out, forbidden
    main = MAIN_PY.read_text(encoding="utf-8")
    for _key, flag, _param in ANSWER_KEYS:
        assert f'"{flag}"' in main, flag
    assert '"--documents-default"' in main


def test_the_previous_install_is_read_before_the_tui() -> None:
    """The TUI and the fallback prompt offer it as the default, and an
    unattended re-run keeps it — both need it before anything is asked."""
    sh = _sh()
    tui = sh.index("\ntui_run_bootstrap\n")
    assert sh.index("\nread_prev_documents\n") < tui
    assert sh.index('DOCUMENTS_DEFAULT="$PREV_DOCUMENTS_DIR"') < tui
    assert "sed -n 's/^CREMIND_HOST_DOCUMENTS=//p'" in sh
    assert "sed -n 's/^CREMIND_DOCUMENTS_READ_ONLY=//p'" in sh
    ps1 = _ps1()
    tui = ps1.index("\nInvoke-InstallerTuiBootstrap\n")
    assert ps1.index("$PrevDocumentsDir = $prevDocs.Path") < tui
    assert ps1.index("$DocumentsDefault = $PrevDocumentsDir") < tui
    assert "[Environment]::GetFolderPath('MyDocuments')" in ps1


# ── the questions ─────────────────────────────────────────────────────────


def test_the_questions_are_docker_only_and_never_asked_unattended() -> None:
    sh = _sh()
    block = _between(sh, "# ── documents folder (docker mode only)", "# ── kubernetes questions")
    assert 'if [ "$MODE" = "docker" ]; then' in block
    assert 'if [ -z "$DOCUMENTS_DIR_INPUT" ] && [ "$UNATTENDED" -eq 0 ] && [ -e /dev/tty ]; then' in block
    assert 'if [ -z "$DOCUMENTS_ACCESS_INPUT" ] && [ "$UNATTENDED" -eq 0 ] && [ -e /dev/tty ]; then' in block
    ps1 = _ps1()
    block = _between(ps1, "# ── documents folder (docker mode only)", "# ── kubernetes questions")
    assert "if ($Mode -eq 'docker') {" in block
    assert ("$docsCanAsk = (-not $Unattended) -and [Environment]::UserInteractive "
            "-and -not [Console]::IsInputRedirected") in block
    assert "if (-not $DocumentsDir -and $docsCanAsk) {" in block
    assert "if (-not $DocumentsAccess -and $docsCanAsk) {" in block


def test_the_retry_loops_are_bounded() -> None:
    """A tty that only returns EOF must not spin the install forever."""
    assert _sh().count('while [ "$docs_tries" -lt 5 ]; do') == 2
    ps1 = _ps1()
    assert "while (-not $DocumentsDir -and $docsTries -lt 5) {" in ps1
    assert "while (-not $DocumentsAccess -and $docsTries -lt 5) {" in ps1


def test_the_resolution_chain_ends_at_previous_then_default() -> None:
    sh = _sh()
    assert 'DOCUMENTS_DIR="$DOCUMENTS_DEFAULT"' in sh
    assert 'docs_access_default="${PREV_DOCUMENTS_ACCESS:-rw}"' in sh
    ps1 = _ps1()
    assert "$DocumentsHostDir = $DocumentsDefault" in ps1
    assert "$docsAccessDefault = if ($PrevDocumentsAccess) { $PrevDocumentsAccess } else { 'rw' }" in ps1


def test_the_folder_is_created_as_the_user_unless_inside_a_container() -> None:
    sh = _sh()
    block = _between(sh, "# ── documents folder (docker mode only)", "# ── kubernetes questions")
    assert block.index('if [ "$IN_CONTAINER" -eq 1 ]; then') < block.index('mkdir -p "$DOCUMENTS_DIR"')
    assert "so it did not create $DOCUMENTS_DIR" in block
    ps1 = _ps1()
    # PS 5.1's New-Item has no -LiteralPath, and -Path globs [ ].
    assert "[System.IO.Directory]::CreateDirectory($DocumentsHostDir)" in ps1
    for line in ps1.splitlines():
        if not line.lstrip().startswith("#"):
            assert not ("New-Item" in line and "-LiteralPath" in line), line


def test_the_platform_notes_are_printed_where_they_apply() -> None:
    sh = _sh()
    assert "grep -qi microsoft /proc/version" in sh
    block = _between(sh, "# ── documents folder (docker mode only)", "# ── kubernetes questions")
    assert "Darwin)" in block and "$DOCKER_DOCUMENTS_MACOS_PRIVACY_NOTE" in block
    assert "Linux)" in block and "$DOCKER_DOCUMENTS_LINUX_OWNER_NOTE" in block
    assert block.count("$DOCKER_DOCUMENTS_WSL_NOTE") >= 2


def test_the_prompts_use_the_catalog_text() -> None:
    """One text, every front-end: the TUI reads the same keys from the JSON."""
    table = tomllib.loads(CATALOG_TOML.read_text(encoding="utf-8"))["docker_documents"]
    catalog_sh = CATALOG_SH.read_text(encoding="utf-8")
    catalog_ps1 = _between(CATALOG_PS1.read_text(encoding="utf-8"),
                           "$script:DockerDocuments = [ordered]@{", "\n}\n")
    sh, ps1 = _sh(), _ps1()
    for key in CATALOG_KEYS:
        assert table.get(key), key
        var = f"DOCKER_DOCUMENTS_{key.upper()}"
        assert f"\n{var}=" in catalog_sh, var
        assert f"${var}" in sh, var
        assert re.search(rf"(?m)^    {_ps_name(key)} +=", catalog_ps1), key
    # install.ps1 prints no Linux/macOS/WSL notes: it only ever runs on Windows.
    for key in ("prompt", "hint", "access_prompt", "rw_label", "rw_disclosure",
                "ro_label", "ro_disclosure"):
        assert f"$script:DockerDocuments.{_ps_name(key)}" in ps1, key


# ── writing the .env ──────────────────────────────────────────────────────


def test_the_env_lines_are_appended_never_substituted() -> None:
    """A folder name may hold | or & (sed) or $1 (-replace); appending the
    literal value sidesteps both."""
    sh = _sh()
    branch = _between(sh, 'step "Docker install"', "docker compose pull")
    assert "printf 'CREMIND_HOST_DOCUMENTS=%s\\n' \"$DOCUMENTS_DIR\" >>\"$DOCKER_DIR/.env\"" in branch
    assert "printf 'CREMIND_DOCUMENTS_READ_ONLY=%s\\n' \"$DOCUMENTS_READ_ONLY\" >>\"$DOCKER_DIR/.env\"" in branch
    assert "printf 'CREMIND_COMPOSE_HOST_DIR=%s\\n' \"$compose_host_dir\" >>\"$DOCKER_DIR/.env\"" in branch
    # Before the chmod, so the appended lines are covered by it.
    assert branch.index("CREMIND_HOST_DOCUMENTS=%s") < branch.index('chmod 600 "$DOCKER_DIR/.env"')

    ps1 = _ps1()
    branch = _between(ps1, 'Write-Step "Docker install"', "Invoke-NativeLogged { & docker compose")
    for line in (
        'Add-Content -Path $EnvDocker -Value "CREMIND_HOST_DOCUMENTS=$DocumentsHostDir" -Encoding utf8',
        'Add-Content -Path $EnvDocker -Value "CREMIND_DOCUMENTS_READ_ONLY=$DocumentsReadOnly" -Encoding utf8',
        'Add-Content -Path $EnvDocker -Value "CREMIND_COMPOSE_HOST_DIR=$($composeHostDir.Path)" -Encoding utf8',
    ):
        assert line in branch, line

    for text in (sh, ps1, ENV_TMPL.read_text(encoding="utf-8")):
        for key in ENV_KEYS:
            assert f"__{key}__" not in text, key


def test_the_env_template_documents_the_keys_in_comments_only() -> None:
    """The installer appends the real lines at the END. An uncommented line in
    the template would come first — and compose's .env keeps the FIRST."""
    text = ENV_TMPL.read_text(encoding="utf-8")
    for key in ENV_KEYS:
        lines = [line for line in text.splitlines() if key in line]
        assert lines, key
        assert all(line.lstrip().startswith("#") for line in lines), key


def test_the_shell_cannot_shadow_what_the_env_file_says() -> None:
    """Compose prefers the invoking shell's value to the .env's."""
    sh = _sh()
    branch = _between(sh, 'step "Docker install"', "docker compose pull")
    # CREMIND_DOCKER_WORKSPACES_DIR: written with a read-only mount (the
    # profiles' working directories then live in the cremind-data volume).
    assert ("for _doc_key in CREMIND_HOST_DOCUMENTS CREMIND_DOCUMENTS_READ_ONLY "
            "CREMIND_COMPOSE_HOST_DIR CREMIND_DOCKER_WORKSPACES_DIR; do") in branch
    assert 'unset "$_doc_key"' in branch
    ps1 = _ps1()
    branch = _between(ps1, 'Write-Step "Docker install"', "Invoke-NativeLogged { & docker compose")
    assert ("@('CREMIND_HOST_DOCUMENTS', 'CREMIND_DOCUMENTS_READ_ONLY', 'CREMIND_COMPOSE_HOST_DIR', "
            "'CREMIND_DOCKER_WORKSPACES_DIR')") in branch
    assert 'Remove-Item -LiteralPath "Env:$docKey"' in branch


def test_powershell_writes_forward_slashes() -> None:
    """The same .env is read by the Linux compose CLI inside the container,
    where a backslash is an ordinary character."""
    fn = _ps1_function("Resolve-DocumentsDir")
    assert "$path = $path -replace '\\\\', '/'" in fn


# ── the apostrophe read-back ──────────────────────────────────────────────


def test_powershell_unescapes_what_the_tui_actually_writes() -> None:
    """output._sh_quote writes John's as 'John'\\''s'. The old regex
    ``'\\\\\\\\''`` matched a quote, TWO backslashes and two quotes, so it never
    fired and the path came back as John'\\''s."""
    ps1 = _ps1()
    assert "$v = $Matches[1].Replace(\"'\\''\", \"'\")" in ps1
    assert "-replace \"'\\\\\\\\''\"" not in ps1
    # What the TUI writes is exactly the sequence that Replace undoes.
    assert _sh_quote("John's Documents") == "'John'\\''s Documents'"
    inner = re.match(r"^'(.*)'$", _sh_quote("John's Documents")).group(1)
    assert inner.replace("'\\''", "'") == "John's Documents"


# ── running the validators for real ───────────────────────────────────────


def _usable_bash() -> str:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash available")
    # C:\Windows\System32\bash.exe is the WSL launcher: it neither passes the
    # environment through nor exists without a distro. Git Bash is the one.
    if os.name == "nt" and Path(bash).parent.name.lower() == "system32":
        pytest.skip("only the WSL launcher is on PATH as bash")
    try:
        probe = subprocess.run([bash, "-c", "echo ok"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("bash does not run")
    if probe.returncode != 0 or probe.stdout.strip() != "ok":
        pytest.skip("bash does not run")
    return bash


def _ansi_c(value: str) -> str:
    """A bash $'...' literal: every byte survives, newlines included."""
    escaped = (value.replace("\\", "\\\\").replace("'", "\\'")
               .replace("\n", "\\n").replace("\r", "\\r"))
    return f"$'{escaped}'"


def _run_bash(script: str, *, cwd: Path | None = None, env: dict[str, str] | None = None):
    return subprocess.run(
        [_usable_bash(), "-s"], input=script.replace("\r\n", "\n").encode("utf-8"),
        capture_output=True, timeout=60, cwd=cwd, env=env,
    )


_SH_CASES = [
    # (raw, expected path or None, words the refusal must contain)
    ("~", "/home/tester", ()),
    ("~/Docs", "/home/tester/Docs", ()),
    ("/data/Docs/", "/data/Docs", ()),
    ("/", "/", ()),
    ("rel/dir", "/rel/dir", ()),
    ("/home/u/John's Documents", "/home/u/John's Documents", ()),
    ("/home/u/Tài liệu", "/home/u/Tài liệu", ()),
    ("/home/u/a|b&c", "/home/u/a|b&c", ()),
    ("", None, ("empty",)),
    (" /lead", None, ("whitespace",)),
    ("/trail ", None, ("whitespace",)),
    ("/a$b", None, ("$",)),
    ("/a#b", None, ("#",)),
    ('/a"b', None, ("double quote",)),
    ("/a\nb", None, ("line break",)),
    ("/a\rb", None, ("line break",)),
    ("~bob/x", None, ("~user",)),
]


def test_sh_normalize_function_in_isolation() -> None:
    """The real function from install.sh, under the script's own set -euo."""
    lines = [
        "set -euo pipefail",
        "HOME=/home/tester",
        "cd /",
        _sh_function("documents_dir_normalize"),
        'check() { if out="$(documents_dir_normalize "$1")"; then '
        "printf 'OK\\t%s\\n' \"$out\"; else printf 'ERR\\t%s\\n' \"$out\"; fi; }",
    ]
    lines += [f"check {_ansi_c(raw)}" for raw, _expected, _words in _SH_CASES]
    # The result is checked too: $HOME can carry what a typed path may not.
    lines += ["HOME='/home/a#b'", "check '~'"]
    # And ~ without a home is refused rather than turned into "/" or "".
    lines += ["HOME=''", "check '~/x'", "unset HOME", "check '~'"]
    result = _run_bash("\n".join(lines) + "\n")
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")

    rows = result.stdout.decode("utf-8").splitlines()
    assert len(rows) == len(_SH_CASES) + 3, rows
    for (raw, expected, words), row in zip(_SH_CASES, rows):
        status, _, text = row.partition("\t")
        if expected is not None:
            assert (status, text) == ("OK", expected), raw
        else:
            assert status == "ERR", raw
            for word in words:
                assert word in text, (raw, text)
    assert rows[-3].startswith("ERR\t") and "#" in rows[-3]
    assert rows[-2] == rows[-1] == "ERR\t$HOME is not set, so ~ cannot be expanded"


def test_sh_sources_the_tui_answer_intact() -> None:
    """The other half of the round trip: bash's ``.`` of what _sh_quote wrote."""
    value = "C:\\Users\\lee\\John's Documents"
    script = (
        f"DOCUMENTS_DIR_INPUT={_sh_quote(value)}\n"
        "printf '%s' \"$DOCUMENTS_DIR_INPUT\"\n"
    )
    result = _run_bash(script)
    assert result.returncode == 0
    assert result.stdout.decode("utf-8") == value


def _run_sh_docs_block(tmp_path: Path, assignments: str) -> tuple[int, str, dict[str, str]]:
    """The real ``── documents folder ──`` block of install.sh, non-interactive,
    with the real catalog include and validator. Returns (rc, output, the
    variables it resolved). ``$ROOT`` in ``assignments`` is tmp_path, as bash
    spells it."""
    block = _between(_sh(), "# ── documents folder (docker mode only)", "# ── kubernetes questions")
    script = "\n".join([
        "set -euo pipefail",
        f"cd {_ansi_c(str(tmp_path).replace(chr(92), '/'))}",
        'ROOT="$(pwd)"',
        "BOLD= DIM= RESET=",
        "info() { printf 'INFO %s\\n' \"$1\"; }",
        "warn() { printf 'WARN %s\\n' \"$1\"; }",
        "err()  { printf 'ERR %s\\n' \"$1\"; }",
        "ok()   { printf 'OK %s\\n' \"$1\"; }",
        f". {_ansi_c(str(CATALOG_SH).replace(chr(92), '/'))}",
        _sh_function("documents_dir_normalize"),
        "MODE=docker UNATTENDED=1 IN_CONTAINER=0 IS_WSL=0",
        "CREMIND_INSTALL_DIR=\"$ROOT/install\"",
        "DOCUMENTS_DIR_INPUT= DOCUMENTS_ACCESS_INPUT= PREV_DOCUMENTS_ACCESS=",
        'DOCUMENTS_DEFAULT="$ROOT/home/Documents"',
        assignments,
        block,
        "printf 'RESULT %s|%s\\n' \"$DOCUMENTS_DIR\" \"$DOCUMENTS_READ_ONLY\"",
    ])
    result = _run_bash(script + "\n")
    out = result.stdout.decode("utf-8", "replace")
    resolved: dict[str, str] = {}
    for line in out.splitlines():
        if line.startswith("RESULT "):
            folder, _, read_only = line[len("RESULT "):].partition("|")
            resolved = {"dir": folder, "read_only": read_only}
    return result.returncode, out + result.stderr.decode("utf-8", "replace"), resolved


def test_sh_unattended_takes_the_default_and_creates_it(tmp_path) -> None:
    rc, out, got = _run_sh_docs_block(tmp_path, "")
    assert rc == 0, out
    assert got["dir"].endswith("/home/Documents") and got["read_only"] == "false", out
    assert (tmp_path / "home" / "Documents").is_dir()
    assert "Documents folder: " in out and "(read-write)" in out


def test_sh_an_answer_beats_the_previous_install(tmp_path) -> None:
    rc, out, got = _run_sh_docs_block(
        tmp_path,
        "DOCUMENTS_DIR_INPUT=\"$ROOT/John's Documents/\" DOCUMENTS_ACCESS_INPUT=ro "
        "PREV_DOCUMENTS_ACCESS=rw DOCUMENTS_DEFAULT=\"$ROOT/prev\"",
    )
    assert rc == 0, out
    assert got["dir"].endswith("/John's Documents"), out
    assert got["read_only"] == "true"
    assert (tmp_path / "John's Documents").is_dir()
    assert not (tmp_path / "prev").exists()


def test_sh_the_previous_access_is_kept_unasked(tmp_path) -> None:
    rc, out, got = _run_sh_docs_block(tmp_path, "PREV_DOCUMENTS_ACCESS=ro")
    assert rc == 0, out
    assert got["read_only"] == "true"


def test_sh_inside_a_container_nothing_is_created(tmp_path) -> None:
    rc, out, got = _run_sh_docs_block(tmp_path, "IN_CONTAINER=1")
    assert rc == 0, out
    assert got["dir"].endswith("/home/Documents")
    assert not (tmp_path / "home").exists()
    assert "runs inside a container, so it did not create" in out


def test_sh_a_bad_tui_answer_is_refused(tmp_path) -> None:
    rc, out, _got = _run_sh_docs_block(tmp_path, "DOCUMENTS_DIR_INPUT='relative/a#b'")
    assert rc == 2, out
    assert "Invalid documents folder 'relative/a#b': it contains #" in out


def test_sh_no_usable_default_leaves_the_key_out(tmp_path) -> None:
    rc, out, got = _run_sh_docs_block(tmp_path, "DOCUMENTS_DEFAULT=")
    assert rc == 0, out
    assert got["dir"] == ""
    assert "which an uninstall deletes" in out


def test_sh_wsl_gets_its_note_unless_the_folder_is_a_windows_drive(tmp_path) -> None:
    rc, out, _got = _run_sh_docs_block(tmp_path, "IS_WSL=1 IN_CONTAINER=1")
    assert rc == 0, out
    assert "INFO Inside WSL, ~/Documents is your Linux home" in out
    rc, out, _got = _run_sh_docs_block(
        tmp_path, "IS_WSL=1 IN_CONTAINER=1 DOCUMENTS_DIR_INPUT=/mnt/c/Users/lee/Documents")
    assert rc == 0, out
    assert "Inside WSL" not in out


def test_sh_other_modes_ignore_the_setting(tmp_path) -> None:
    rc, out, got = _run_sh_docs_block(tmp_path, "MODE=native DOCUMENTS_DIR_INPUT=/x")
    assert rc == 0, out
    assert got["dir"] == ""
    assert "applies to Docker installs only; ignoring it for native" in out
    assert not (tmp_path / "home").exists()


def _sandbox_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """This process's environment minus anything Cremind reads, pointed at
    tmp_path. The real script runs under it, so nothing it could reach before
    the refusal lands outside the test's directory."""
    env = {
        k: v for k, v in os.environ.items()
        if not k.upper().startswith("CREMIND_")
        and k.upper() not in ("INSTALL_MODE", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "HOMEDRIVE", "HOMEPATH")
    }
    env.update({
        "HOME": str(tmp_path / "home"),
        # install.ps1's defaults (and PowerShell's $HOME) come from these.
        "USERPROFILE": str(tmp_path / "home"),
        "LOCALAPPDATA": str(tmp_path / "home" / "AppData" / "Local"),
        "APPDATA": str(tmp_path / "home" / "AppData" / "Roaming"),
        "CREMIND_INSTALL_DIR": str(tmp_path / "install"),
        "CREMIND_SYSTEM_DIR": str(tmp_path / "system"),
        # Unreachable: if validation ever moves below the catalog fetch, the
        # run stops there instead of installing anything.
        "CREMIND_CATALOG_BASE": "http://127.0.0.1:9",
        "CREMIND_TEMPLATE_BASE": "http://127.0.0.1:9",
        "CREMIND_NO_TUI": "1",
    })
    env.update(extra)
    return env


@pytest.mark.parametrize(
    "args, extra_env, expected",
    [
        (["--documents-dir", "/a$b"], {}, "Invalid --documents-dir '/a$b': it contains $"),
        (["--documents-dir=/a#b"], {}, "Invalid --documents-dir '/a#b': it contains #"),
        ([], {"CREMIND_DOCUMENTS_DIR": '/a"b'}, "Invalid CREMIND_DOCUMENTS_DIR"),
        (["--documents-access", "rx"], {}, "Invalid --documents-access: rx (must be rw or ro)"),
        ([], {"CREMIND_DOCUMENTS_ACCESS": "RW"}, "Invalid CREMIND_DOCUMENTS_ACCESS: RW"),
    ],
)
def test_sh_refuses_a_bad_value_before_doing_anything(tmp_path, args, extra_env, expected) -> None:
    """The real script, from a copy (so it has no checkout to lean on)."""
    bash = _usable_bash()
    script = tmp_path / "bin" / "install.sh"
    script.parent.mkdir()
    script.write_bytes(SH.read_bytes().replace(b"\r\n", b"\n"))
    (tmp_path / "home").mkdir()
    result = subprocess.run(
        [bash, str(script), "--unattended", "--mode", "docker", *args],
        capture_output=True, timeout=120, env=_sandbox_env(tmp_path, **extra_env),
    )
    output = (result.stdout + result.stderr).decode("utf-8", "replace")
    assert result.returncode == 2, output
    assert expected in output
    # Nothing was installed: the refusal came before the install dir existed.
    assert not (tmp_path / "install").exists()


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        pytest.skip("no PowerShell available")
    return executable


def _ps_literal(value: str) -> str:
    """A PowerShell double-quoted literal with every special character escaped."""
    escaped = (value.replace("`", "``").replace("$", "`$").replace('"', '`"')
               .replace("\n", "`n").replace("\r", "`r"))
    return f'"{escaped}"'


def _run_ps(tmp_path: Path, body: str) -> str:
    script = tmp_path / "driver.ps1"
    script.write_text(
        "Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"
        # Windows PowerShell writes redirected output in the OEM code page.
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
        + body + "\nexit 0\n",
        encoding="utf-8-sig", newline="\n",
    )
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(script)],
        capture_output=True, timeout=120,
    )
    stdout = result.stdout.decode("utf-8", "replace")
    assert result.returncode == 0, stdout + result.stderr.decode("utf-8", "replace")
    return stdout


def test_ps1_resolve_function_in_isolation(tmp_path) -> None:
    """The real Resolve-DocumentsDir from install.ps1. Paths only mean what
    they mean on Windows there, so the path cases run only on Windows; the
    refusals run wherever a PowerShell does."""
    cases: list[tuple[str, str | None, tuple[str, ...]]] = [
        ("", None, ("empty",)),
        (" C:/lead", None, ("whitespace",)),
        ("C:/trail ", None, ("whitespace",)),
        ("C:/a$b", None, ("$",)),
        ("C:/a#b", None, ("#",)),
        ('C:/a"b', None, ("double quote",)),
        ("C:/a\nb", None, ("line break",)),
        ("C:/a\rb", None, ("line break",)),
        ("~bob/x", None, ("~user",)),
    ]
    if os.name == "nt":
        home = str(Path.home()).replace("\\", "/")
        cases += [
            ("C:\\Users\\lee\\John's Documents", "C:/Users/lee/John's Documents", ()),
            ("C:\\Users\\lee\\Docs\\", "C:/Users/lee/Docs", ()),
            ("C:/Users/lee/Tài liệu", "C:/Users/lee/Tài liệu", ()),
            ("~", home, ()),
            ("~\\Docs", f"{home}/Docs", ()),
            ("docs", str(tmp_path).replace("\\", "/") + "/docs", ()),
        ]
    tmp_literal = str(tmp_path).replace("'", "''")
    body = [
        _ps1_function("Resolve-DocumentsDir"),
        f"Set-Location -LiteralPath '{tmp_literal}'",
        "function Show($r) { if ($r.Problem) { \"ERR`t$($r.Problem)\" } else { \"OK`t$($r.Path)\" } }",
    ]
    body += [f"Show (Resolve-DocumentsDir {_ps_literal(raw)})" for raw, _e, _w in cases]
    rows = _run_ps(tmp_path, "\n".join(body)).splitlines()
    rows = [row for row in rows if row.startswith(("OK\t", "ERR\t"))]
    assert len(rows) == len(cases), rows
    for (raw, expected, words), row in zip(cases, rows):
        status, _, text = row.partition("\t")
        if expected is not None:
            assert (status, text) == ("OK", expected), raw
        else:
            assert status == "ERR", (raw, row)
            for word in words:
                assert word in text, (raw, text)


def test_ps1_read_back_restores_an_apostrophe(tmp_path) -> None:
    """The fixed line itself, fed exactly what the TUI writes."""
    line = next(
        (raw.strip() for raw in _ps1().splitlines() if "$v = $Matches[1].Replace(" in raw), None
    )
    assert line, "the read-back unescape line moved or was rewritten"
    value = "C:\\Users\\lee\\John's Documents"
    written = _sh_quote(value)
    body = "\n".join([
        "$v = '" + written.replace("'", "''") + "'",
        line,
        "[Console]::Out.Write(\"V=$v\")",
    ])
    assert f"V={value}" in _run_ps(tmp_path, body)


def _run_ps_docs_block(tmp_path: Path, assignments: str) -> tuple[int, str, dict[str, str]]:
    """The real ``── documents folder ──`` block of install.ps1, unattended,
    with the real catalog include and validator. ``$Root`` is tmp_path."""
    block = _between(_ps1(), "# ── documents folder (docker mode only)", "# ── kubernetes questions")

    def quoted(path: Path) -> str:
        return "'" + str(path).replace("'", "''") + "'"

    script = tmp_path / "docs-block.ps1"
    script.write_text("\n".join([
        "Set-StrictMode -Version Latest",
        "$ErrorActionPreference = 'Stop'",
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)",
        "function Write-Info  { param($Msg) Write-Output \"INFO $Msg\" }",
        "function Write-Ok    { param($Msg) Write-Output \"OK $Msg\" }",
        "function Write-Warn2 { param($Msg) Write-Output \"WARN $Msg\" }",
        "function Write-Err2  { param($Msg) Write-Output \"ERR $Msg\" }",
        f". {quoted(CATALOG_PS1)}",
        _ps1_function("Resolve-DocumentsDir"),
        f"$Root = {quoted(tmp_path)}",
        "$Mode = 'docker'; $Unattended = $true",
        "$DocumentsDir = ''; $DocumentsAccess = ''; $PrevDocumentsAccess = ''",
        "$DocumentsDefault = (Resolve-DocumentsDir (Join-Path $Root 'home\\Documents')).Path",
        "$CremindInstallDir = Join-Path $Root 'install'",
        assignments,
        block,
        'Write-Output "RESULT $DocumentsHostDir|$DocumentsReadOnly"',
        "exit 0",
    ]) + "\n", encoding="utf-8-sig", newline="\n")
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(script)],
        capture_output=True, timeout=120,
    )
    out = result.stdout.decode("utf-8", "replace")
    resolved: dict[str, str] = {}
    for line in out.splitlines():
        if line.startswith("RESULT "):
            folder, _, read_only = line[len("RESULT "):].partition("|")
            resolved = {"dir": folder, "read_only": read_only}
    return result.returncode, out + result.stderr.decode("utf-8", "replace"), resolved


def test_ps1_unattended_takes_the_default_and_creates_it(tmp_path) -> None:
    rc, out, got = _run_ps_docs_block(tmp_path, "")
    assert rc == 0, out
    assert got["dir"] == str(tmp_path / "home" / "Documents").replace("\\", "/"), out
    assert got["read_only"] == "false"
    assert (tmp_path / "home" / "Documents").is_dir()


def test_ps1_an_answer_beats_the_previous_install(tmp_path) -> None:
    rc, out, got = _run_ps_docs_block(
        tmp_path,
        "$DocumentsDir = Join-Path $Root \"John's Documents\"; $DocumentsAccess = 'ro'; "
        "$PrevDocumentsAccess = 'rw'",
    )
    assert rc == 0, out
    assert got["dir"] == str(tmp_path / "John's Documents").replace("\\", "/"), out
    assert got["read_only"] == "true"
    assert (tmp_path / "John's Documents").is_dir()


def test_ps1_the_previous_access_is_kept_unasked(tmp_path) -> None:
    rc, out, got = _run_ps_docs_block(tmp_path, "$PrevDocumentsAccess = 'ro'")
    assert rc == 0, out
    assert got["read_only"] == "true"


def test_ps1_a_bad_tui_answer_is_refused(tmp_path) -> None:
    rc, out, _got = _run_ps_docs_block(tmp_path, "$DocumentsDir = 'relative/a#b'")
    assert rc == 2, out
    assert "Invalid documents folder 'relative/a#b': it contains #" in out


def test_ps1_other_modes_ignore_the_setting(tmp_path) -> None:
    rc, out, got = _run_ps_docs_block(tmp_path, "$Mode = 'native'; $DocumentsDir = 'C:/x'")
    assert rc == 0, out
    assert got["dir"] == ""
    assert "applies to Docker installs only; ignoring it for native" in out


@pytest.mark.skipif(os.name != "nt", reason="install.ps1 runs on Windows")
@pytest.mark.parametrize(
    "args, extra_env, expected",
    [
        (["-DocumentsDir", "C:/a$b"], {}, "Invalid -DocumentsDir 'C:/a$b': it contains $"),
        ([], {"CREMIND_DOCUMENTS_DIR": "C:/x #y"}, "Invalid CREMIND_DOCUMENTS_DIR"),
        ([], {"CREMIND_DOCUMENTS_ACCESS": "rx"}, "Invalid CREMIND_DOCUMENTS_ACCESS: rx"),
    ],
)
def test_ps1_refuses_a_bad_value_before_doing_anything(tmp_path, args, extra_env, expected) -> None:
    script = tmp_path / "bin" / "install.ps1"
    script.parent.mkdir()
    script.write_bytes(PS1.read_bytes())
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(script), "-Unattended", "-Mode", "docker", *args],
        capture_output=True, timeout=120, env=_sandbox_env(tmp_path, **extra_env),
    )
    output = (result.stdout + result.stderr).decode("utf-8", "replace")
    assert result.returncode == 2, output
    assert expected in output
    assert not (tmp_path / "install").exists()


# ── compose's .env parser ─────────────────────────────────────────────────


def _compose_config(tmp_path: Path, env_lines: list[str]) -> str:
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("no docker available")
    (tmp_path / "docker-compose.yml").write_text(COMPOSE_TMPL.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / ".env").write_text(
        "\n".join([
            "CREMIND_IMAGE=cremind/cremind", "CREMIND_VERSION=test",
            "APP_URL=http://localhost:1515", "CORS_ALLOWED_ORIGINS=http://localhost:1515",
            "VNC_PASSWORD=x", *env_lines,
        ]) + "\n",
        encoding="utf-8", newline="\n",
    )
    # Compose prefers the invoking shell's value to the .env's.
    env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
    result = subprocess.run(
        [docker, "compose", "config"], cwd=tmp_path, capture_output=True, timeout=120, env=env,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result.stdout.decode("utf-8", "replace").replace("\r\n", "\n")


@pytest.mark.parametrize(
    "folder",
    ["C:/Users/lee/John's Documents", "/home/lee/John's Documents", "/home/lee/Tài liệu"],
)
@pytest.mark.parametrize("read_only", ["true", "false"])
def test_what_the_installers_accept_survives_compose_unquoted(tmp_path, folder, read_only) -> None:
    """The lines exactly as the installers append them: apostrophes, inner
    spaces and non-ASCII reach the bind source untouched."""
    out = _compose_config(tmp_path, [
        f"CREMIND_HOST_DOCUMENTS={folder}",
        f"CREMIND_DOCUMENTS_READ_ONLY={read_only}",
        "CREMIND_COMPOSE_HOST_DIR=C:/Users/lee/AppData/Local/Cremind/docker",
    ])
    assert re.search(rf"(?m)^\s+source: {re.escape(folder)}$", out), out
    if read_only == "true":
        assert re.search(r"(?m)^\s+read_only: true$", out), out
    else:
        assert "read_only: true" not in out, out
    assert "CREMIND_COMPOSE_HOST_DIR: C:/Users/lee/AppData/Local/Cremind/docker" in out


@pytest.mark.parametrize("folder", ["/home/lee/a #b", "/home/lee/a$b", "/home/lee/x "])
def test_what_the_installers_refuse_compose_would_mangle(tmp_path, folder) -> None:
    """Why the refusals exist: each of these reaches compose as another path
    (a truncated comment, an empty variable, a trimmed space)."""
    out = _compose_config(tmp_path, [f"CREMIND_HOST_DOCUMENTS={folder}"])
    assert re.search(rf"(?m)^\s+source: {re.escape(folder)}$", out) is None, out
    assert re.search(r"(?m)^\s+source: /home/lee/(a|x)$", out), out
