"""An uninstall ``--purge`` keeps the profiles' working directories.

Each profile's default working directory lives in the workspaces folder —
``$CREMIND_WORKSPACES_DIR``, else ``<System Dir>/workspaces`` (native), and a
Docker install's ``<documents folder>/cremind-workspaces`` (``./documents``
inside the bundle when no folder was recorded; the ``cremind-data`` volume with
a read-only documents mount) — never the user's own ``<documents
folder>/workspaces``, a common folder name. They are the user's files, so a
purge deletes everything else and keeps them, unless ``--purge-workspaces`` /
``-PurgeWorkspaces`` says otherwise (confirmed on a terminal; the flag alone
without one). Folders an admin chose elsewhere are never touched, and named.

install.sh runs for real here, in a sandbox: HOME (and USERPROFILE /
LOCALAPPDATA / APPDATA / XDG_*), the System Dir, the Install Dir and the
working directory are tmp_path, and ``systemctl`` / ``launchctl`` / ``docker``
/ ``kubectl`` / ``helm`` / ``sqlite3`` are shims on PATH — the boot-service
teardown and the Docker cleanup must never reach this machine's real
services, containers (``docker compose -p cremind down -v`` would delete a
real install's volumes) or folders. Before each run a probe proves every one
of those names resolves to its shim, and after it every "Removed" line must
name a path inside tmp_path. install.ps1's uninstall would delete the real
``Cremind Server`` scheduled task, so its helpers run in isolation and the
flow is pinned statically.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SH = REPO_ROOT / "install" / "install.sh"
PS1 = REPO_ROOT / "install" / "install.ps1"
README = REPO_ROOT / "install" / "README.md"


def _sh() -> str:
    return SH.read_text(encoding="utf-8")


def _ps1() -> str:
    return PS1.read_text(encoding="utf-8")


# ── both installers, statically ─────────────────────────────────────────────


def test_the_flag_exists_is_documented_and_implies_a_purge() -> None:
    sh = _sh()
    header = sh.split("set -e", 1)[0]
    assert "--purge-workspaces)       PURGE_WORKSPACES=1; shift ;;" in sh
    assert "#   --purge-workspaces" in header
    assert "UNINSTALL_MODE=purge" in sh.split("# --purge-workspaces is a purge", 1)[1][:400]

    ps1 = _ps1()
    assert "[switch] $PurgeWorkspaces," in ps1
    assert ".PARAMETER PurgeWorkspaces" in ps1
    assert "if ($Purge -or $PurgeWorkspaces) { $UninstallMode = 'purge' }" in ps1

    readme = README.read_text(encoding="utf-8")
    assert "`--purge-workspaces`" in readme and "`-PurgeWorkspaces`" in readme


def test_keep_and_purge_workspaces_contradict() -> None:
    assert "it cannot be combined with --keep." in _sh()
    assert "if ($Keep -and $PurgeWorkspaces) {" in _ps1()


def test_the_chosen_folders_come_from_the_profiles_table_with_the_legacy_fallback() -> None:
    """Named after a purge: ``profiles.working_dir`` since per-profile
    folders, ``server_config.user_working_dir`` on an older database."""
    for text in (_sh(), _ps1()):
        assert "select name, working_dir from profiles where working_dir is not null" in text
        assert "select 'admin|' || value from server_config where key='user_working_dir'" in text
        assert "preserved at:" in text


def test_a_terminal_confirms_but_the_flag_alone_suffices_without_one() -> None:
    sh = _sh()
    assert '[ "$UNATTENDED" -eq 0 ] && [ -t 0 ]' in sh
    assert 'if [ "$ans" != "delete" ]; then' in sh
    ps1 = _ps1()
    assert ("$wsCanAsk = (-not $Unattended) -and [Environment]::UserInteractive "
            "-and -not [Console]::IsInputRedirected") in ps1
    assert "if ($ans -ne 'delete') {" in ps1


def test_a_read_only_docker_mount_puts_the_workspaces_in_the_volume() -> None:
    """A read-only bind cannot hold the profiles' folders; both installers
    then point the container at the cremind-data volume, and never let the
    shell's value of that key shadow the .env's."""
    sh = _sh()
    assert "printf 'CREMIND_DOCKER_WORKSPACES_DIR=%s\\n' \"/root/.cremind/workspaces\" >>\"$DOCKER_DIR/.env\"" in sh
    assert ("for _doc_key in CREMIND_HOST_DOCUMENTS CREMIND_DOCUMENTS_READ_ONLY "
            "CREMIND_COMPOSE_HOST_DIR CREMIND_DOCKER_WORKSPACES_DIR; do") in sh
    ps1 = _ps1()
    assert ('Add-Content -Path $EnvDocker -Value "CREMIND_DOCKER_WORKSPACES_DIR=/root/.cremind/workspaces" '
            "-Encoding utf8") in ps1
    assert "'CREMIND_COMPOSE_HOST_DIR', 'CREMIND_DOCKER_WORKSPACES_DIR')" in ps1
    # ... and a purge copies them out of the volume before `down -v`.
    for text in (sh, ps1):
        assert "cremind:/root/.cremind/workspaces" in text
        assert text.index("cremind:/root/.cremind/workspaces") < text.index("down -v --remove-orphans")


def test_the_docker_workspaces_folder_is_cremind_workspaces_in_both_installers() -> None:
    """The compose file puts the profiles' folders in
    ``/root/Documents/cremind-workspaces``; the uninstall must look for them
    there, not in a ``workspaces`` folder the user may keep for themselves."""
    sh = _sh()
    assert 'UNINSTALL_WS_DOCKER="${_ws_docs%/}/cremind-workspaces"' in sh
    assert '"${_ws_docs%/}/workspaces"' not in sh
    ps1 = _ps1()
    assert "(Join-Path $wsDocs 'cremind-workspaces')" in ps1
    assert "(Join-Path $wsDocs 'workspaces')" not in ps1
    compose = (REPO_ROOT / "install" / "templates" / "docker-compose.yml.tmpl").read_text(encoding="utf-8")
    assert "/root/Documents/cremind-workspaces}" in compose
    for text in (sh, ps1, compose):
        assert "Documents/workspaces" not in text


# ── install.sh, for real ────────────────────────────────────────────────────


def _usable_bash() -> str:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash available")
    if os.name == "nt" and Path(bash).parent.name.lower() == "system32":
        pytest.skip("only the WSL launcher is on PATH as bash")
    try:
        probe = subprocess.run([bash, "-c", "echo ok"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("bash does not run")
    if probe.returncode != 0 or probe.stdout.strip() != "ok":
        pytest.skip("bash does not run")
    return bash


_SHIMS = {
    # The boot-service teardown's fallbacks: never the real ones.
    "systemctl": 'echo "systemctl $*" >>"$SHIM_LOG"; exit 0',
    "launchctl": 'echo "launchctl $*" >>"$SHIM_LOG"; exit 0',
    # A daemon that is up, a `down` that does nothing, and a `cp` out of the
    # volume that lands one file.
    "docker": (
        'echo "docker $*" >>"$SHIM_LOG"\n'
        'if [ "$1" = compose ]; then\n'
        '    prev=""; dest=""; seen=0\n'
        '    for a in "$@"; do\n'
        '        if [ "$seen" -eq 2 ]; then dest="$a"; break; fi\n'
        '        [ "$seen" -eq 1 ] && seen=2\n'
        '        [ "$a" = cp ] && seen=1\n'
        '    done\n'
        '    if [ -n "$dest" ]; then mkdir -p "$dest/bob" && echo copied >"$dest/bob/from-volume.md"; fi\n'
        'fi\n'
        'exit 0'
    ),
    "sqlite3": (
        'case "$*" in\n'
        '    *"from profiles"*) printf "admin|%s\\n" "$FAKE_ADMIN_DIR"; printf "bob|%s\\n" "$FAKE_BOB_DIR" ;;\n'
        'esac\n'
        'exit 0'
    ),
    # The Helm teardown only runs with a recorded release, which no test
    # writes; should one ever reach a cluster tool, it fails closed.
    "kubectl": 'echo "kubectl $*" >>"$SHIM_LOG"; exit 1',
    "helm": 'echo "helm $*" >>"$SHIM_LOG"; exit 1',
}


def _posix(path: Path) -> str:
    """How Git Bash spells a Windows path (C:\\x → /c/x); POSIX paths as-is."""
    text = str(path).replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", text)
    return f"/{m.group(1).lower()}/{m.group(2)}" if m else text


def _sandbox_env(tmp_path: Path, shims: Path, extra_env: dict[str, str]) -> dict[str, str]:
    """This process's environment minus everything Cremind reads, with every
    per-user root pointed into tmp_path — the Windows ones too, although
    install.sh reads none of them today, so a later edit that does still
    lands in the sandbox."""
    home = tmp_path / "home"
    env = {
        k: v for k, v in os.environ.items()
        if not k.upper().startswith("CREMIND_")
        and k.upper() not in ("INSTALL_MODE", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "HOMEDRIVE", "HOMEPATH")
    }
    # POSIX spellings: install.sh globs these, and a backslash in a glob is
    # an escape (on Linux/macOS they are POSIX already).
    env.update({
        "HOME": _posix(home),
        "USERPROFILE": str(home),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "CREMIND_INSTALL_DIR": _posix(tmp_path / "install"),
        "CREMIND_SYSTEM_DIR": _posix(tmp_path / "system"),
        "XDG_CONFIG_HOME": _posix(home / ".config"),
        "XDG_DATA_HOME": _posix(home / ".local" / "share"),
        "PATH": str(shims) + os.pathsep + os.environ.get("PATH", ""),
        "SHIM_LOG": str(tmp_path / "shim.log"),
    })
    env.update(extra_env)
    return env


def _assert_the_shims_win(bash: str, env: dict[str, str], tmp_path: Path, shims: Path) -> None:
    """Refuse to run the uninstall unless every tool it could reach the
    machine with resolves to its shim. The failure mode this guards: Git
    Bash not converting the Windows PATH, and the real ``docker`` answering
    ``compose -p cremind down -v``."""
    # ``-ef``: the same file, however bash spells the path (Git Bash may say
    # /tmp/... for a folder Windows calls C:\Users\...\Temp\...).
    names = " ".join(_SHIMS)
    probe = subprocess.run(
        [bash, "-c",
         f'for c in {names}; do p="$(command -v "$c")"; '
         f'if [ -n "$p" ] && [ "$p" -ef "$SHIM_DIR/$c" ]; then echo "$c=shim"; else echo "$c=$p"; fi; done'],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
        env={**env, "SHIM_DIR": _posix(shims)}, cwd=str(tmp_path),
    )
    found = dict(
        line.split("=", 1) for line in probe.stdout.decode("utf-8", "replace").splitlines() if "=" in line
    )
    wrong = {name: found.get(name, "") for name in _SHIMS if found.get(name) != "shim"}
    if wrong:
        pytest.fail(f"refusing to run install.sh --uninstall: not every tool resolves to its shim: {wrong}")


def _assert_only_the_sandbox_was_touched(out: str, tmp_path: Path) -> None:
    sandbox = (_posix(tmp_path), str(tmp_path).replace("\\", "/"))
    for line in out.splitlines():
        if "Removed" in line or "Kept the profiles" in line:
            assert any(s in line.replace("\\", "/") for s in sandbox), f"outside the sandbox: {line!r}"


def _run_uninstall(tmp_path: Path, *args: str, **extra_env: str) -> tuple[int, str]:
    bash = _usable_bash()
    script = tmp_path / "bin" / "install.sh"
    script.parent.mkdir(exist_ok=True)
    script.write_bytes(SH.read_bytes().replace(b"\r\n", b"\n"))
    shims = tmp_path / "shims"
    shims.mkdir(exist_ok=True)
    for name, body in _SHIMS.items():
        shim = shims / name
        shim.write_bytes(("#!/usr/bin/env bash\n" + body + "\n").encode("utf-8"))
        shim.chmod(0o755)
    (tmp_path / "home").mkdir(exist_ok=True)
    env = _sandbox_env(tmp_path, shims, extra_env)
    _assert_the_shims_win(bash, env, tmp_path, shims)
    result = subprocess.run(
        [bash, str(script), "--uninstall", *args],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=120, env=env, cwd=str(tmp_path),
    )
    out = (result.stdout + result.stderr).decode("utf-8", "replace")
    _assert_only_the_sandbox_was_touched(out, tmp_path)
    return result.returncode, out


def test_the_sandbox_guard_refuses_a_path_without_the_shims(tmp_path) -> None:
    """The probe itself: with the shims missing from PATH, nothing runs."""
    bash = _usable_bash()
    shims = tmp_path / "shims"
    shims.mkdir()
    env = _sandbox_env(tmp_path, shims, {})
    env["PATH"] = os.environ.get("PATH", "")
    with pytest.raises(pytest.fail.Exception, match="not every tool resolves to its shim"):
        _assert_the_shims_win(bash, env, tmp_path, shims)


def _put(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _native_install(tmp_path: Path) -> Path:
    system = tmp_path / "system"
    _put(system / "venv" / "pyvenv.cfg")
    _put(system / "storage" / "cremind.db")
    _put(system / ".env", "PORT=1112")
    _put(system / "admin" / "PERSONA.md")
    _put(system / "tokens" / "admin.token")
    _put(system / "workspaces" / "admin" / "notes.md", "admin's notes")
    _put(system / "workspaces" / "bob" / "poetry.lock", "bob's lock")
    _put(system / "workspaces" / ".deleted" / "carol-1" / "kept.md")
    _put(tmp_path / "install" / "install.log")
    return system


def test_a_purge_keeps_the_workspaces_and_deletes_everything_else(tmp_path) -> None:
    system = _native_install(tmp_path)
    rc, out = _run_uninstall(tmp_path, "--purge")
    assert rc == 0, out

    assert (system / "workspaces" / "admin" / "notes.md").read_text(encoding="utf-8") == "admin's notes"
    assert (system / "workspaces" / "bob" / "poetry.lock").is_file()
    assert (system / "workspaces" / ".deleted" / "carol-1" / "kept.md").is_file()
    assert sorted(p.name for p in system.iterdir()) == ["workspaces"]
    assert not (tmp_path / "install").exists()
    assert "except the profiles' working directories" in out
    assert "Kept the profiles' working directories at:" in out
    # The sandbox held: no real service manager was asked for anything.
    log = (tmp_path / "shim.log").read_text(encoding="utf-8") if (tmp_path / "shim.log").exists() else ""
    assert "docker" not in log


def test_purge_workspaces_without_a_terminal_deletes_them_too(tmp_path) -> None:
    system = _native_install(tmp_path)
    rc, out = _run_uninstall(tmp_path, "--purge-workspaces")
    assert rc == 0, out
    assert not system.exists()
    assert not (tmp_path / "install").exists()
    assert "Kept the profiles' working directories" not in out


def test_keep_with_purge_workspaces_is_refused_before_anything_goes(tmp_path) -> None:
    system = _native_install(tmp_path)
    rc, out = _run_uninstall(tmp_path, "--keep", "--purge-workspaces")
    assert rc == 2, out
    assert "cannot be combined with --keep" in out
    assert (system / "venv" / "pyvenv.cfg").is_file()
    assert (tmp_path / "install" / "install.log").is_file()


def test_a_workspaces_root_outside_the_system_dir_is_left_alone(tmp_path) -> None:
    system = _native_install(tmp_path)
    outside = tmp_path / "elsewhere" / "workspaces"
    _put(outside / "bob" / "draft.tmp", "bob's draft")

    rc, out = _run_uninstall(tmp_path, "--purge", CREMIND_WORKSPACES_DIR=_posix(outside))
    assert rc == 0, out
    assert (outside / "bob" / "draft.tmp").is_file()
    # <System Dir>/workspaces is not the root now: it goes with the rest.
    assert not system.exists()
    assert f"Kept the profiles' working directories at: {_posix(outside)}" in out

    _native_install(tmp_path)
    rc, out = _run_uninstall(tmp_path, "--purge-workspaces", CREMIND_WORKSPACES_DIR=_posix(outside))
    assert rc == 0, out
    assert not outside.exists()


def test_purge_workspaces_never_deletes_a_root_that_holds_the_home_folder(tmp_path) -> None:
    """A stray ``CREMIND_WORKSPACES_DIR`` (``~`` or above it) is refused,
    not wiped."""
    _native_install(tmp_path)
    _put(tmp_path / "home" / "precious.md")
    rc, out = _run_uninstall(tmp_path, "--purge-workspaces", CREMIND_WORKSPACES_DIR=_posix(tmp_path / "home"))
    assert rc == 0, out
    assert (tmp_path / "home" / "precious.md").is_file()
    assert "it contains your home folder" in out
    # install.ps1 carries the same guard (its flow is pinned statically).
    assert "(Test-UninstallPathInside $env:USERPROFILE $ws)" in _ps1()


def test_the_folders_an_admin_chose_are_named_not_touched(tmp_path) -> None:
    _native_install(tmp_path)
    chosen = _put(tmp_path / "Documents" / "keep.md").parent
    inside_ws = tmp_path / "system" / "workspaces" / "bob-2"
    rc, out = _run_uninstall(
        tmp_path, "--purge", FAKE_ADMIN_DIR=_posix(chosen), FAKE_BOB_DIR=_posix(inside_ws),
    )
    assert rc == 0, out
    assert (chosen / "keep.md").is_file()
    assert f"Working directory of profile 'admin' preserved at: {_posix(chosen)}" in out
    # One inside the workspaces folder is covered by that line instead.
    assert "profile 'bob'" not in out


def _docker_install(tmp_path: Path, env_lines: list[str]) -> Path:
    bundle = tmp_path / "install" / "docker"
    _put(bundle / "docker-compose.yml", "services: {}\n")
    _put(bundle / ".env", "\n".join(["COMPOSE_PROJECT_NAME=cremind", *env_lines]) + "\n")
    return bundle


def test_docker_the_bundles_documents_fallback_keeps_its_workspaces(tmp_path) -> None:
    """No CREMIND_HOST_DOCUMENTS recorded: compose mounted ./documents, inside
    the Install Dir. A purge (and a keep) deletes the bundle but keeps its
    cremind-workspaces/ subfolder."""
    bundle = _docker_install(tmp_path, ["CREMIND_DOCUMENTS_READ_ONLY=false"])
    _put(bundle / "documents" / "cremind-workspaces" / "bob" / "report.md", "bob's report")
    _put(bundle / "documents" / "legacy.md")

    rc, out = _run_uninstall(tmp_path, "--purge")
    assert rc == 0, out
    assert (bundle / "documents" / "cremind-workspaces" / "bob" / "report.md").is_file()
    assert not (bundle / "docker-compose.yml").exists()
    assert not (bundle / ".env").exists()
    assert not (bundle / "documents" / "legacy.md").exists()
    assert "down -v" in (tmp_path / "shim.log").read_text(encoding="utf-8")


def test_docker_a_host_documents_folder_is_only_emptied_on_request(tmp_path) -> None:
    host_docs = tmp_path / "MyDocs"
    _put(host_docs / "mine.md")
    _put(host_docs / "cremind-workspaces" / "bob" / "report.md")
    _docker_install(tmp_path, [f"CREMIND_HOST_DOCUMENTS={_posix(host_docs)}", "CREMIND_DOCUMENTS_READ_ONLY=false"])

    rc, out = _run_uninstall(tmp_path, "--purge")
    assert rc == 0, out
    assert (host_docs / "cremind-workspaces" / "bob" / "report.md").is_file()
    assert f"Kept the profiles' working directories at: {_posix(host_docs)}/cremind-workspaces" in out

    _docker_install(tmp_path, [f"CREMIND_HOST_DOCUMENTS={_posix(host_docs)}", "CREMIND_DOCUMENTS_READ_ONLY=false"])
    rc, out = _run_uninstall(tmp_path, "--purge-workspaces")
    assert rc == 0, out
    assert not (host_docs / "cremind-workspaces").exists()
    # The rest of the user's folder is never Cremind's to delete.
    assert (host_docs / "mine.md").is_file()


def test_docker_purge_workspaces_leaves_the_users_own_workspaces_folder(tmp_path) -> None:
    """``workspaces`` is a common folder name (VS Code and friends): the
    user's own ``<documents>/workspaces/client-x`` is not a profile's folder,
    so even ``--purge-workspaces --unattended`` deletes only the profiles'
    ``cremind-workspaces`` (admin's and bob's) next to it."""
    host_docs = tmp_path / "MyDocs"
    _put(host_docs / "workspaces" / "client-x" / "main.py", "the user's code")
    _put(host_docs / "cremind-workspaces" / "admin" / "notes.md", "admin's notes")
    _put(host_docs / "cremind-workspaces" / "bob" / "report.md", "bob's report")
    _docker_install(tmp_path, [f"CREMIND_HOST_DOCUMENTS={_posix(host_docs)}", "CREMIND_DOCUMENTS_READ_ONLY=false"])

    rc, out = _run_uninstall(tmp_path, "--purge", "--unattended")
    assert rc == 0, out
    assert f"Kept the profiles' working directories at: {_posix(host_docs)}/cremind-workspaces" in out
    assert f"{_posix(host_docs)}/workspaces" not in out
    assert (host_docs / "workspaces" / "client-x" / "main.py").is_file()

    _docker_install(tmp_path, [f"CREMIND_HOST_DOCUMENTS={_posix(host_docs)}", "CREMIND_DOCUMENTS_READ_ONLY=false"])
    rc, out = _run_uninstall(tmp_path, "--purge-workspaces", "--unattended")
    assert rc == 0, out
    assert not (host_docs / "cremind-workspaces").exists()
    assert (host_docs / "workspaces" / "client-x" / "main.py").read_text(encoding="utf-8") == "the user's code"


def test_docker_read_only_copies_the_volumes_workspaces_out_before_down(tmp_path) -> None:
    _docker_install(tmp_path, [
        "CREMIND_DOCUMENTS_READ_ONLY=true", "CREMIND_DOCKER_WORKSPACES_DIR=/root/.cremind/workspaces",
    ])
    rc, out = _run_uninstall(tmp_path, "--purge")
    assert rc == 0, out
    copies = list((tmp_path / "home").glob("cremind-workspaces-*"))
    assert len(copies) == 1, out
    assert (copies[0] / "bob" / "from-volume.md").is_file()
    assert "copied out of the cremind-data volume" in out
    log = (tmp_path / "shim.log").read_text(encoding="utf-8")
    assert log.index("cp cremind:/root/.cremind/workspaces") < log.index("down -v")


# ── install.ps1's helpers, in isolation ─────────────────────────────────────


def _ps1_uninstall_function(name: str) -> str:
    ps1 = _ps1()
    start = ps1.index(f"    function {name}(")
    return ps1[start:ps1.index("\n    }\n", start) + 7].replace("\r\n", "\n")


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        pytest.skip("no PowerShell available")
    return executable


def _run_ps(tmp_path: Path, body: str) -> str:
    script = tmp_path / "driver.ps1"
    functions = "\n".join(
        _ps1_uninstall_function(n)
        for n in ("Get-UninstallFullPath", "Test-UninstallPathInside", "Remove-TreeExcept")
    )
    script.write_text(
        "Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Continue'\n"
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
        + functions + "\n" + body + "\nexit 0\n",
        encoding="utf-8-sig", newline="\n",
    )
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True, timeout=120,
    )
    stdout = result.stdout.decode("utf-8", "replace")
    assert result.returncode == 0, stdout + result.stderr.decode("utf-8", "replace")
    return stdout


@pytest.mark.skipif(os.name != "nt", reason="install.ps1 only ever runs on Windows paths")
def test_ps1_path_inside(tmp_path) -> None:
    out = _run_ps(tmp_path, "\n".join([
        "Write-Output (Test-UninstallPathInside 'C:\\Users\\a\\.cremind\\workspaces' 'C:\\Users\\a\\.cremind')",
        "Write-Output (Test-UninstallPathInside 'c:/users/A/.CREMIND/workspaces/' 'C:\\Users\\a\\.cremind\\')",
        "Write-Output (Test-UninstallPathInside 'C:\\Users\\a\\.cremind' 'C:\\Users\\a\\.cremind')",
        "Write-Output (Test-UninstallPathInside 'C:\\Users\\a\\.cremind-old\\x' 'C:\\Users\\a\\.cremind')",
        "Write-Output (Test-UninstallPathInside 'D:\\x' 'C:\\Users\\a')",
        "Write-Output (Test-UninstallPathInside '' 'C:\\Users\\a')",
    ]))
    assert out.split() == ["True", "True", "True", "False", "False", "False"]


@pytest.mark.skipif(os.name != "nt", reason="install.ps1 only ever runs on Windows paths")
def test_ps1_remove_tree_except(tmp_path) -> None:
    sysdir = tmp_path / "system"
    for rel in ("venv/pyvenv.cfg", "storage/cremind.db", ".env", "admin/PERSONA.md",
                "workspaces/admin/notes.md", "workspaces/bob/poetry.lock"):
        _put(sysdir / rel)
    bundle = tmp_path / "install"
    for rel in ("docker/.env", "docker/documents/legacy.md", "docker/documents/cremind-workspaces/bob/r.md",
                "install.log"):
        _put(bundle / rel)
    # A junction on the way down to the kept folder is never entered.
    target = tmp_path / "junction-target"
    _put(target / "precious.md")
    other = tmp_path / "other"
    _put(other / "a.md")

    def lit(p: Path) -> str:
        return "'" + str(p).replace("'", "''") + "'"

    _run_ps(tmp_path, "\n".join([
        f"New-Item -ItemType Junction -Path {lit(sysdir / 'link')} -Target {lit(target)} | Out-Null",
        f"Remove-TreeExcept {lit(sysdir)} {lit(sysdir / 'workspaces')}",
        f"Remove-TreeExcept {lit(bundle)} {lit(bundle / 'docker' / 'documents' / 'cremind-workspaces')}",
        # Outside the tree: the tree goes whole. Equal to it: nothing goes.
        f"Remove-TreeExcept {lit(other)} {lit(tmp_path / 'elsewhere')}",
        f"Remove-TreeExcept {lit(target)} {lit(target)}",
    ]))
    assert sorted(p.name for p in sysdir.iterdir()) == ["workspaces"]
    assert (sysdir / "workspaces" / "admin" / "notes.md").is_file()
    assert (sysdir / "workspaces" / "bob" / "poetry.lock").is_file()
    assert (target / "precious.md").is_file(), "the junction's target must survive"
    assert sorted(p.name for p in bundle.iterdir()) == ["docker"]
    assert sorted(p.name for p in (bundle / "docker").iterdir()) == ["documents"]
    assert sorted(p.name for p in (bundle / "docker" / "documents").iterdir()) == ["cremind-workspaces"]
    assert (bundle / "docker" / "documents" / "cremind-workspaces" / "bob" / "r.md").is_file()
    assert not other.exists()
