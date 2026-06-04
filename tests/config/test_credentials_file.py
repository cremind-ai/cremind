"""Unit tests for the consolidated credentials.toml writer."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import toml

from app.config import credentials_file


# ── parse_docker_env ──────────────────────────────────────────────────────


def test_parse_docker_env_missing_file_returns_empty(tmp_path: Path) -> None:
    assert credentials_file.parse_docker_env(tmp_path / "nope.env") == {}


def test_parse_docker_env_strips_comments_blank_lines_and_quotes(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# top comment\n"
        "\n"
        "FOO=bar\n"
        '  BAZ = "quoted value" \n'
        "EMPTY=\n"
        "# trailing comment\n",
        encoding="utf-8",
    )
    parsed = credentials_file.parse_docker_env(env)
    assert parsed["FOO"] == "bar"
    assert parsed["BAZ"] == "quoted value"
    assert parsed["EMPTY"] == ""


def test_parse_docker_env_skips_unrendered_placeholders(tmp_path: Path) -> None:
    """A literal ``__VNC_PASSWORD__`` value means the template never got rendered."""
    env = tmp_path / ".env"
    env.write_text("VNC_PASSWORD=__VNC_PASSWORD__\nOK=real\n", encoding="utf-8")
    parsed = credentials_file.parse_docker_env(env)
    assert "VNC_PASSWORD" not in parsed
    assert parsed["OK"] == "real"


# ── write_credentials_file: docker mode ───────────────────────────────────


def _docker_env_text(*, vnc: str = "supersecret24chars") -> str:
    return (
        "CREMIND_VERSION=1.2.3\n"
        "APP_URL=http://example.local:1112\n"
        "CORS_ALLOWED_ORIGINS=http://example.local:1515,http://localhost:1515\n"
        "SETUP_WIZARD_ENV=server\n"
        "INSTALL_MODE=docker\n"
        "API_PORT=1112\n"
        "SPA_PORT=1515\n"
        "NOVNC_PORT=6080\n"
        "VNC_PORT=5900\n"
        "COMPOSE_PROFILES=\n"
        f"VNC_PASSWORD={vnc}\n"
        "RESOLUTION=1280x720\n"
    )


def _seed_docker_install(system_dir: Path, install_dir: Path) -> None:
    docker_dir = install_dir / "docker"
    docker_dir.mkdir(parents=True)
    (docker_dir / ".env").write_text(_docker_env_text(), encoding="utf-8")
    system_dir.mkdir(parents=True, exist_ok=True)


def test_docker_mode_renders_metadata_and_app_and_desktop(tmp_path: Path) -> None:
    system_dir = tmp_path / "system"
    install_dir = tmp_path / "install"
    _seed_docker_install(system_dir, install_dir)

    path = credentials_file.write_credentials_file(
        system_dir=system_dir, install_dir=install_dir,
    )
    assert path == install_dir / "credentials.toml"

    data = toml.loads(path.read_text(encoding="utf-8"))
    assert data["install_mode"] == "docker"
    assert data["cremind_version"] == "1.2.3"
    assert data["system_dir"] == str(system_dir)
    assert data["install_dir"] == str(install_dir)
    assert "generated_at" in data

    app = data["app"]
    assert app["api_url"] == "http://example.local:1112"
    assert app["spa_url"] == "http://example.local:1515"
    assert app["api_port"] == 1112
    assert app["spa_port"] == 1515
    assert app["setup_wizard_env"] == "server"

    desktop = data["desktop"]
    assert desktop["vnc_password"] == "supersecret24chars"
    assert desktop["vnc_port"] == 5900
    assert desktop["novnc_port"] == 6080
    assert desktop["novnc_url"] == "http://example.local:6080/vnc.html"
    assert desktop["resolution"] == "1280x720"

    assert "postgres" not in data


def test_docker_mode_skips_desktop_when_vnc_placeholder_unrendered(tmp_path: Path) -> None:
    system_dir = tmp_path / "system"
    install_dir = tmp_path / "install"
    docker_dir = install_dir / "docker"
    docker_dir.mkdir(parents=True)
    (docker_dir / ".env").write_text(
        # All other vars normal, but VNC_PASSWORD never got rendered.
        _docker_env_text(vnc="__VNC_PASSWORD__"),
        encoding="utf-8",
    )
    system_dir.mkdir(parents=True, exist_ok=True)

    path = credentials_file.write_credentials_file(
        system_dir=system_dir, install_dir=install_dir,
    )
    data = toml.loads(path.read_text(encoding="utf-8"))
    # docker/.env exists but the password placeholder is dropped — empty
    # string surfaces as missing in the rendered file.
    assert data["desktop"]["vnc_password"] == ""


# ── write_credentials_file: native mode ───────────────────────────────────


def test_native_mode_omits_desktop_section(tmp_path: Path) -> None:
    system_dir = tmp_path / "system"
    install_dir = tmp_path / "install"
    # No docker/.env → native mode.
    system_dir.mkdir(parents=True)
    (system_dir / ".env").write_text(
        "HOST=0.0.0.0\n"
        "PORT=1112\n"
        "APP_URL=http://1.2.3.4:1112\n"
        "CORS_ALLOWED_ORIGINS=http://1.2.3.4:1515,http://localhost:1515\n"
        "SETUP_WIZARD_ENV=server\n"
        "INSTALL_MODE=native\n",
        encoding="utf-8",
    )

    path = credentials_file.write_credentials_file(
        system_dir=system_dir, install_dir=install_dir,
    )
    data = toml.loads(path.read_text(encoding="utf-8"))
    assert data["install_mode"] == "native"
    assert "desktop" not in data
    assert data["app"]["api_url"] == "http://1.2.3.4:1112"
    assert data["app"]["spa_url"] == "http://1.2.3.4:1515"
    assert data["app"]["setup_wizard_env"] == "server"


# ── postgres section follows bootstrap.toml ───────────────────────────────


def _seed_bootstrap_postgres(system_dir: Path, password: str = "bootstrap-pw") -> None:
    (system_dir / "bootstrap.toml").write_text(
        'db_provider = "postgres"\n'
        "\n"
        "[postgres]\n"
        'host = "postgres"\n'
        "port = 5432\n"
        'database = "cremind"\n'
        'user = "cremind"\n'
        f'password = "{password}"\n'
        'sslmode = "disable"\n'
        'deployment_mode = "docker"\n',
        encoding="utf-8",
    )


def test_postgres_section_pulled_from_bootstrap_not_docker_env(tmp_path: Path) -> None:
    """The writer must source [postgres] from bootstrap.toml even if PG_*
    in docker/.env says something different — bootstrap.toml is the
    authoritative record of the creds Cremind itself will use to connect.
    """
    system_dir = tmp_path / "system"
    install_dir = tmp_path / "install"
    _seed_docker_install(system_dir, install_dir)
    # docker/.env says one password
    (install_dir / "docker" / ".env").write_text(
        _docker_env_text() + "PG_PASSWORD=from-docker-env\nPG_USER=u\nPG_DATABASE=d\n",
        encoding="utf-8",
    )
    # bootstrap.toml says another
    _seed_bootstrap_postgres(system_dir, password="from-bootstrap")

    path = credentials_file.write_credentials_file(
        system_dir=system_dir, install_dir=install_dir,
    )
    data = toml.loads(path.read_text(encoding="utf-8"))
    assert data["postgres"]["password"] == "from-bootstrap"
    assert data["postgres"]["host"] == "postgres"
    assert data["postgres"]["deployment_mode"] == "docker"


def test_no_postgres_section_when_bootstrap_says_sqlite(tmp_path: Path) -> None:
    system_dir = tmp_path / "system"
    install_dir = tmp_path / "install"
    _seed_docker_install(system_dir, install_dir)
    (system_dir / "bootstrap.toml").write_text('db_provider = "sqlite"\n', encoding="utf-8")

    path = credentials_file.write_credentials_file(
        system_dir=system_dir, install_dir=install_dir,
    )
    data = toml.loads(path.read_text(encoding="utf-8"))
    assert "postgres" not in data


def test_no_postgres_section_when_bootstrap_missing(tmp_path: Path) -> None:
    system_dir = tmp_path / "system"
    install_dir = tmp_path / "install"
    _seed_docker_install(system_dir, install_dir)
    # No bootstrap.toml — read_bootstrap defaults to sqlite.

    path = credentials_file.write_credentials_file(
        system_dir=system_dir, install_dir=install_dir,
    )
    data = toml.loads(path.read_text(encoding="utf-8"))
    assert "postgres" not in data


# ── atomicity, permissions, idempotency ───────────────────────────────────


def test_writer_leaves_no_tmp_files(tmp_path: Path) -> None:
    system_dir = tmp_path / "system"
    install_dir = tmp_path / "install"
    _seed_docker_install(system_dir, install_dir)

    credentials_file.write_credentials_file(
        system_dir=system_dir, install_dir=install_dir,
    )
    leftovers = list(install_dir.glob(".credentials.*.tmp"))
    assert leftovers == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_writer_chmods_600_on_posix(tmp_path: Path) -> None:
    system_dir = tmp_path / "system"
    install_dir = tmp_path / "install"
    _seed_docker_install(system_dir, install_dir)
    path = credentials_file.write_credentials_file(
        system_dir=system_dir, install_dir=install_dir,
    )
    mode = os.stat(path).st_mode & 0o777
    assert mode == 0o600


def test_writer_is_idempotent_ignoring_generated_at(tmp_path: Path) -> None:
    system_dir = tmp_path / "system"
    install_dir = tmp_path / "install"
    _seed_docker_install(system_dir, install_dir)

    path = credentials_file.write_credentials_file(
        system_dir=system_dir, install_dir=install_dir,
    )
    first = toml.loads(path.read_text(encoding="utf-8"))
    credentials_file.write_credentials_file(
        system_dir=system_dir, install_dir=install_dir,
    )
    second = toml.loads(path.read_text(encoding="utf-8"))

    first.pop("generated_at", None)
    second.pop("generated_at", None)
    assert first == second
