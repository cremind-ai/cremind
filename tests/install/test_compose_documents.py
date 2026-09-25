"""The Docker bundle mounts the user's Documents folder, on every host.

The installers ask for a host folder and append three keys to the bundle's
``.env``; ``docker-compose.yml`` turns them into a long-syntax bind mount on
``/root/Documents`` and tells the app what it did. Without the mount the folder
is a plain directory in the container's writable layer, and everything the agent
saved there is lost on the next recreate.

The value that has to survive is a Windows one: the installers always write
forward slashes, so Docker Desktop on Windows receives ``C:/Users/x/Documents``,
and so does the Linux compose CLI inside the cremind container when the Setup
Wizard brings a sidecar up (it parses the whole file). The docker-gated tests
below run ``docker compose config`` on exactly that and must pass wherever
compose exists — they do not skip on a render failure the way the older
INSTALL_MODE test does.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = REPO_ROOT / "install" / "templates"
COMPOSE_TEMPLATE = TEMPLATES / "docker-compose.yml.tmpl"
ENV_TEMPLATE = TEMPLATES / "docker.env.tmpl"
SH = REPO_ROOT / "install" / "install.sh"
PS1 = REPO_ROOT / "install" / "install.ps1"
DEPLOY_ENV = REPO_ROOT / "app" / "userdocs" / "deploy_env.py"

WINDOWS_DOCUMENTS = "C:/Users/x/Documents"
BUNDLE_DIR = "C:/Users/x/AppData/Local/cremind/docker"

# What compose substitutes (the same definition as the provisioner's
# ``_interpolated_names`` and test_installer_install_mode.py).
_INTERPOLATED = re.compile(r"(?<!\$)\$\{?([A-Za-z_][A-Za-z0-9_]*)")

# The placeholders install.sh / install.ps1 fill in docker.env.tmpl.
_ENV_PLACEHOLDERS = {
    "__CREMIND_IMAGE__": "cremind/cremind-desktop",
    "__CREMIND_VERSION__": "0.0.0",
    "__APP_URL__": "http://localhost:1515",
    "__CORS_ALLOWED_ORIGINS__": "http://localhost:1515",
    "__SETUP_WIZARD_ENV__": "local",
    "__INSTALL_MODE__": "docker",
    "__VNC_PASSWORD__": "abc12345",
}


def strip_desktop(text: str) -> str:
    """The basic flavor's filter: ``maybe_strip_desktop`` in install.sh and
    ``Remove-DesktopOnly`` in install.ps1 drop each marker block, markers
    included. :func:`test_the_installers_strip_exactly_like_this` holds the
    real ones to this copy."""
    kept: list[str] = []
    inside = False
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("# >>> desktop-only >>>"):
            inside = True
            continue
        if inside and stripped.startswith("# <<< desktop-only <<<"):
            inside = False
            continue
        if not inside:
            kept.append(line)
    return "".join(kept)


def render_bundle(
    directory: Path,
    *,
    desktop: bool = True,
    documents: str | None = WINDOWS_DOCUMENTS,
    read_only: str | None = "false",
    bundle_dir: str | None = BUNDLE_DIR,
) -> Path:
    """Write ``docker-compose.yml`` + ``.env`` the way the installers do.

    ``None`` leaves a key out, which is what an ``.env`` written by an installer
    that predates the documents question looks like.
    """
    compose = COMPOSE_TEMPLATE.read_text(encoding="utf-8")
    env = ENV_TEMPLATE.read_text(encoding="utf-8")
    for placeholder, value in _ENV_PLACEHOLDERS.items():
        env = env.replace(placeholder, value)
    if not desktop:
        compose, env = strip_desktop(compose), strip_desktop(env)
        env = env.replace("CREMIND_IMAGE=cremind/cremind-desktop", "CREMIND_IMAGE=cremind/cremind")
    leftover = re.findall(r"__[A-Z_]+__", env)
    assert not leftover, f"docker.env.tmpl grew placeholders this test does not fill: {leftover}"

    appended = ["CREMIND_SSL="]
    if documents is not None:
        appended.append(f"CREMIND_HOST_DOCUMENTS={documents}")
    if read_only is not None:
        appended.append(f"CREMIND_DOCUMENTS_READ_ONLY={read_only}")
    if bundle_dir is not None:
        appended.append(f"CREMIND_COMPOSE_HOST_DIR={bundle_dir}")
    env = env.rstrip("\n") + "\n" + "\n".join(appended) + "\n"

    (directory / "docker-compose.yml").write_text(compose, encoding="utf-8")
    (directory / ".env").write_text(env, encoding="utf-8")
    return directory


def _template() -> dict:
    return yaml.safe_load(COMPOSE_TEMPLATE.read_text(encoding="utf-8"))


def _documents_volume(service: dict) -> dict:
    matches = [v for v in service.get("volumes", [])
               if isinstance(v, dict) and v.get("target") == "/root/Documents"]
    assert len(matches) == 1, f"expected one /root/Documents mount, got {service.get('volumes')}"
    return matches[0]


# ── the template, statically ──────────────────────────────────────────────


def test_the_template_binds_documents_with_the_long_syntax() -> None:
    cremind = _template()["services"]["cremind"]
    volume = _documents_volume(cremind)

    assert volume == {
        "type": "bind",
        "source": "${CREMIND_HOST_DOCUMENTS:-./documents}",
        "target": "/root/Documents",
        "read_only": "${CREMIND_DOCUMENTS_READ_ONLY:-false}",
        "bind": {"create_host_path": True},
    }
    # No second, short-syntax spelling of the same mount next to it.
    assert not [v for v in cremind["volumes"] if isinstance(v, str) and "/root/Documents" in v]


def test_the_template_tells_the_app_what_it_mounted() -> None:
    environment = _template()["services"]["cremind"]["environment"]

    # A literal string, so nothing in a shell or an .env can switch it off.
    assert environment["CREMIND_DOCUMENTS_BIND"] == "1"
    assert environment["CREMIND_HOST_DOCUMENTS_HINT"] == "${CREMIND_HOST_DOCUMENTS:-}"
    assert environment["CREMIND_COMPOSE_HOST_DIR"] == "${CREMIND_COMPOSE_HOST_DIR:-}"

    interpolated = set(_INTERPOLATED.findall(COMPOSE_TEMPLATE.read_text(encoding="utf-8")))
    assert "CREMIND_DOCUMENTS_BIND" not in interpolated
    assert {"CREMIND_HOST_DOCUMENTS", "CREMIND_DOCUMENTS_READ_ONLY",
            "CREMIND_COMPOSE_HOST_DIR"} <= interpolated


def test_the_app_reads_the_names_the_template_sets() -> None:
    """``app.userdocs.deploy_env.docker_root_status`` is the reader."""
    source = DEPLOY_ENV.read_text(encoding="utf-8")
    for name in ("CREMIND_DOCUMENTS_BIND", "CREMIND_HOST_DOCUMENTS_HINT", "CREMIND_COMPOSE_HOST_DIR"):
        assert f'"{name}"' in source, name


def test_the_basic_flavor_keeps_the_documents_mount() -> None:
    """The desktop-only markers must not swallow the new lines."""
    basic = yaml.safe_load(strip_desktop(COMPOSE_TEMPLATE.read_text(encoding="utf-8")))
    cremind = basic["services"]["cremind"]

    _documents_volume(cremind)
    assert cremind["environment"]["CREMIND_DOCUMENTS_BIND"] == "1"
    assert "CREMIND_HOST_DOCUMENTS_HINT" in cremind["environment"]
    assert "CREMIND_COMPOSE_HOST_DIR" in cremind["environment"]
    # ... and still removes what it exists to remove.
    assert "VNC_PASSWORD" not in cremind["environment"]
    assert "RESOLUTION" not in cremind["environment"]
    assert not [p for p in cremind["ports"] if "VNC_PORT" in p or "NOVNC_PORT" in p]


def _sed_strip(text: str) -> str:
    sed = shutil.which("sed")
    if not sed:
        pytest.skip("no sed available")
    match = re.search(r"maybe_strip_desktop\(\) \{.*?sed '([^']+)'", SH.read_text(encoding="utf-8"), re.S)
    assert match, "maybe_strip_desktop moved or was rewritten"
    result = subprocess.run([sed, match.group(1)], input=text.encode("utf-8"),
                            capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout.decode("utf-8").replace("\r\n", "\n")


def _powershell_strip(text: str, tmp_path: Path) -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        pytest.skip("no PowerShell available")
    match = re.search(r"(?ms)^function Remove-DesktopOnly \{.*?^\}", PS1.read_text(encoding="utf-8"))
    assert match, "Remove-DesktopOnly moved or was rewritten"
    source, target = tmp_path / "in.yml", tmp_path / "out.yml"
    source.write_text(text, encoding="utf-8")

    def quoted(path: Path) -> str:
        return str(path).replace("'", "''")

    script = tmp_path / "strip.ps1"
    script.write_text(
        "Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"
        + match.group(0).replace("\r\n", "\n") + "\n"
        f"$text = [IO.File]::ReadAllText('{quoted(source)}')\n"
        f"[IO.File]::WriteAllText('{quoted(target)}', (Remove-DesktopOnly -Text $text), "
        "(New-Object System.Text.UTF8Encoding $false))\n",
        encoding="utf-8-sig", newline="\n",
    )
    result = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return target.read_text(encoding="utf-8")


@pytest.mark.parametrize("tool", ["sed", "powershell"])
def test_the_installers_strip_exactly_like_this(tool: str, tmp_path: Path) -> None:
    """The real filters, run on the real templates, agree with ``strip_desktop``."""
    for template in (COMPOSE_TEMPLATE, ENV_TEMPLATE):
        text = template.read_text(encoding="utf-8")
        stripped = _sed_strip(text) if tool == "sed" else _powershell_strip(text, tmp_path)
        assert stripped == strip_desktop(text), template.name


# ── docker compose config ─────────────────────────────────────────────────


@pytest.fixture(scope="module")
def docker() -> str:
    """The docker CLI, once compose itself answers. ``config`` never talks to
    the daemon, so a stopped Docker Desktop still runs these."""
    executable = shutil.which("docker")
    if not executable:
        pytest.skip("no docker available")
    probe = subprocess.run([executable, "compose", "version"], capture_output=True, text=True, timeout=60)
    if probe.returncode != 0:
        pytest.skip("docker is installed without the compose plugin")
    return executable


def _config(docker: str, directory: Path, *, succeeds: bool = True) -> dict | str:
    """``docker compose config`` from the bundle folder, with a shell that
    cannot leak a value past the ``.env`` (compose reads the shell first)."""
    names = set(_INTERPOLATED.findall((directory / "docker-compose.yml").read_text(encoding="utf-8")))
    env = {key: value for key, value in os.environ.items()
           if key not in names and not key.startswith("COMPOSE_")}
    result = subprocess.run([docker, "compose", "config"], cwd=directory, capture_output=True,
                            text=True, encoding="utf-8", timeout=120, env=env)
    if not succeeds:
        assert result.returncode != 0, result.stdout
        return result.stderr + result.stdout
    assert result.returncode == 0, result.stderr
    return yaml.safe_load(result.stdout)


@pytest.mark.parametrize("desktop", [True, False], ids=["desktop", "basic"])
@pytest.mark.parametrize("read_only", ["true", "false"])
def test_compose_accepts_a_windows_path_and_the_read_only_flag(
    docker: str, tmp_path: Path, read_only: str, desktop: bool,
) -> None:
    config = _config(docker, render_bundle(tmp_path, desktop=desktop, read_only=read_only))
    cremind = config["services"]["cremind"]
    volume = _documents_volume(cremind)

    assert volume["type"] == "bind"
    # Kept verbatim: not resolved against the project folder.
    assert volume["source"] == WINDOWS_DOCUMENTS
    # Compose prints a false read_only by leaving it out.
    assert volume.get("read_only", False) is (read_only == "true")
    # Newer compose leaves the (default) true out as well.
    assert (volume.get("bind") or {}).get("create_host_path", True) is True

    environment = cremind["environment"]
    assert environment["CREMIND_DOCUMENTS_BIND"] == "1"
    assert environment["CREMIND_HOST_DOCUMENTS_HINT"] == WINDOWS_DOCUMENTS
    assert environment["CREMIND_COMPOSE_HOST_DIR"] == BUNDLE_DIR
    assert ("VNC_PASSWORD" in environment) is desktop


@pytest.mark.parametrize("documents", [
    "C:/Users/John's Documents/My Files",
    "/home/lee/My Documents",
])
def test_apostrophes_and_spaces_survive_unquoted(docker: str, tmp_path: Path, documents: str) -> None:
    """The installers write the value unquoted, which the ``.env`` parser keeps
    whole: only ``$``, ``#`` after a space, quotes and newlines are special."""
    config = _config(docker, render_bundle(tmp_path, documents=documents))
    cremind = config["services"]["cremind"]

    assert _documents_volume(cremind)["source"] == documents
    assert cremind["environment"]["CREMIND_HOST_DOCUMENTS_HINT"] == documents


def test_an_env_from_an_older_installer_falls_back_to_the_bundle_folder(
    docker: str, tmp_path: Path,
) -> None:
    config = _config(docker, render_bundle(tmp_path, documents=None, read_only=None, bundle_dir=None))
    cremind = config["services"]["cremind"]
    volume = _documents_volume(cremind)

    source = Path(volume["source"])
    assert source.name == "documents"
    assert os.path.samefile(source.parent, tmp_path)
    assert volume.get("read_only", False) is False
    # ``config`` resolves, it does not create: that is ``up``'s job.
    assert not (tmp_path / "documents").exists()
    assert cremind["environment"]["CREMIND_HOST_DOCUMENTS_HINT"] == ""
    assert cremind["environment"]["CREMIND_COMPOSE_HOST_DIR"] == ""


def test_a_read_only_value_compose_cannot_parse_fails_loudly(docker: str, tmp_path: Path) -> None:
    """Why the installers write exactly ``true``/``false``: anything else stops
    every ``docker compose`` command on this bundle, not just the mount."""
    output = _config(docker, render_bundle(tmp_path, read_only="garbage"), succeeds=False)
    assert "garbage" in output
