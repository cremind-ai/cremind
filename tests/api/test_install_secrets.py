"""Tests for /api/config/install-secrets.

This endpoint feeds the Setup Wizard's downloadable config export with
VNC + Postgres connection info from on-disk sources (docker/.env +
bootstrap.toml). It needs to handle four shapes:

- docker install, Postgres = Docker → all values come from docker/.env
  AND bootstrap.toml; bootstrap wins where both populate.
- docker install, Postgres = external → docker/.env has VNC only;
  Postgres details come from bootstrap.toml.
- docker install, SQLite → docker/.env has VNC; no Postgres section.
- native install → no docker/.env; bootstrap.toml may or may not say
  Postgres. ``deployment`` field reports "native".
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from app.api import config as config_api


def _stub_admin_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_api, "require_admin", lambda _req: None)


# Env vars the endpoint consults — the dev host may have any of these set
# from running an actual install, which would leak into hermetic tests.
_DOCKER_RUNTIME_VARS = (
    "CREMIND_COMPOSE_ENV_FILE",
    "VNC_PASSWORD", "RESOLUTION", "APP_URL", "INSTALL_MODE",
    "CORS_ALLOWED_ORIGINS", "SETUP_WIZARD_ENV",
    "API_PORT", "SPA_PORT", "NOVNC_PORT", "VNC_PORT",
)


def _clear_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _DOCKER_RUNTIME_VARS:
        monkeypatch.delenv(name, raising=False)


def _stub_dirs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    install_dir: Path,
    system_dir: Path,
) -> None:
    _clear_runtime_env(monkeypatch)
    monkeypatch.setattr(
        config_api.BaseConfig, "CREMIND_INSTALL_DIR", str(install_dir), raising=False,
    )
    monkeypatch.setattr(
        config_api.BaseConfig, "CREMIND_SYSTEM_DIR", str(system_dir), raising=False,
    )


def _get_handler() -> Callable:
    """Pull ``handle_install_secrets`` out of ``get_config_routes`` closure."""
    state = SimpleNamespace(storage_ready=False)
    routes = config_api.get_config_routes(state)  # type: ignore[arg-type]
    for route in routes:
        if route.path == "/api/config/install-secrets":
            return route.endpoint
    raise AssertionError("install-secrets route not registered")


def _call(handler: Callable) -> dict:
    request = SimpleNamespace(headers={}, cookies={})
    response = asyncio.run(handler(request))
    return json.loads(response.body)


def _write_docker_env(install_dir: Path, *, vnc: str = "vnc-from-env", pg: bool = False) -> None:
    docker_dir = install_dir / "docker"
    docker_dir.mkdir(parents=True, exist_ok=True)
    content = (
        f"VNC_PASSWORD={vnc}\n"
        "CREMIND_VERSION=1.2.3\n"
        "APP_URL=http://example:1112\n"
    )
    if pg:
        content += (
            "PG_USER=user-from-env\n"
            "PG_PASSWORD=pw-from-env\n"
            "PG_DATABASE=db-from-env\n"
        )
    (docker_dir / ".env").write_text(content, encoding="utf-8")


def _write_bootstrap_postgres(
    system_dir: Path,
    *,
    password: str = "pw-from-bootstrap",
    host: str = "postgres",
    deployment_mode: str = "docker",
) -> None:
    system_dir.mkdir(parents=True, exist_ok=True)
    (system_dir / "bootstrap.toml").write_text(
        'db_provider = "postgres"\n'
        "\n"
        "[postgres]\n"
        f'host = "{host}"\n'
        "port = 5432\n"
        'database = "cremind"\n'
        'user = "cremind"\n'
        f'password = "{password}"\n'
        'sslmode = "disable"\n'
        f'deployment_mode = "{deployment_mode}"\n',
        encoding="utf-8",
    )


def _write_bootstrap_sqlite(system_dir: Path) -> None:
    system_dir.mkdir(parents=True, exist_ok=True)
    (system_dir / "bootstrap.toml").write_text('db_provider = "sqlite"\n', encoding="utf-8")


# ── docker install + Postgres / Docker ────────────────────────────────────


def test_docker_postgres_returns_full_connection_from_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir = tmp_path / "install"
    system_dir = tmp_path / "system"
    _write_docker_env(install_dir, pg=True)
    _write_bootstrap_postgres(system_dir, password="pw-from-bootstrap")
    _stub_admin_ok(monkeypatch)
    _stub_dirs(monkeypatch, install_dir=install_dir, system_dir=system_dir)

    body = _call(_get_handler())

    assert body["deployment"] == "docker"
    assert body["available"] is True
    assert body["vnc_password"] == "vnc-from-env"
    # bootstrap.toml wins for postgres password
    assert body["pg_password"] == "pw-from-bootstrap"
    assert body["pg_host"] == "postgres"
    assert body["pg_port"] == 5432
    assert body["pg_user"] == "cremind"
    assert body["pg_database"] == "cremind"
    assert body["pg_sslmode"] == "disable"
    assert body["pg_deployment_mode"] == "docker"


# ── docker install + Postgres / external ──────────────────────────────────


def test_docker_external_postgres_pulls_creds_from_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir = tmp_path / "install"
    system_dir = tmp_path / "system"
    _write_docker_env(install_dir, pg=False)  # only VNC
    _write_bootstrap_postgres(
        system_dir,
        password="external-pw",
        host="db.example.com",
        deployment_mode="external",
    )
    _stub_admin_ok(monkeypatch)
    _stub_dirs(monkeypatch, install_dir=install_dir, system_dir=system_dir)

    body = _call(_get_handler())

    assert body["deployment"] == "docker"
    assert body["vnc_password"] == "vnc-from-env"
    assert body["pg_host"] == "db.example.com"
    assert body["pg_password"] == "external-pw"
    assert body["pg_deployment_mode"] == "external"


# ── docker install + SQLite ───────────────────────────────────────────────


def test_docker_sqlite_returns_vnc_and_null_postgres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir = tmp_path / "install"
    system_dir = tmp_path / "system"
    _write_docker_env(install_dir, pg=False)
    _write_bootstrap_sqlite(system_dir)
    _stub_admin_ok(monkeypatch)
    _stub_dirs(monkeypatch, install_dir=install_dir, system_dir=system_dir)

    body = _call(_get_handler())

    assert body["deployment"] == "docker"
    assert body["vnc_password"] == "vnc-from-env"
    assert body["pg_password"] is None
    assert body["pg_host"] is None
    assert body["pg_port"] is None
    assert body["pg_deployment_mode"] is None


# ── docker install with VNC placeholder unrendered ────────────────────────


def test_docker_unrendered_vnc_placeholder_returns_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A literal ``__VNC_PASSWORD__`` in docker/.env means the template
    never got rendered — the parser drops it and the endpoint surfaces
    ``null`` rather than the literal placeholder."""
    install_dir = tmp_path / "install"
    system_dir = tmp_path / "system"
    _write_docker_env(install_dir, vnc="__VNC_PASSWORD__")
    _stub_admin_ok(monkeypatch)
    _stub_dirs(monkeypatch, install_dir=install_dir, system_dir=system_dir)

    body = _call(_get_handler())
    assert body["vnc_password"] is None


# ── neither file present ──────────────────────────────────────────────────


def test_no_files_returns_native_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir = tmp_path / "install"
    system_dir = tmp_path / "system"
    install_dir.mkdir()
    system_dir.mkdir()
    _stub_admin_ok(monkeypatch)
    _stub_dirs(monkeypatch, install_dir=install_dir, system_dir=system_dir)

    body = _call(_get_handler())
    assert body == {"deployment": "native", "available": False}


# ── native install with Postgres in bootstrap.toml ────────────────────────


def test_native_postgres_returns_bootstrap_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_dir = tmp_path / "install"
    system_dir = tmp_path / "system"
    install_dir.mkdir()  # no docker/.env inside
    _write_bootstrap_postgres(system_dir, password="native-pw", host="localhost", deployment_mode="external")
    _stub_admin_ok(monkeypatch)
    _stub_dirs(monkeypatch, install_dir=install_dir, system_dir=system_dir)

    body = _call(_get_handler())
    assert body["deployment"] == "native"
    assert body["available"] is True
    assert body["vnc_password"] is None
    assert body["pg_host"] == "localhost"
    assert body["pg_password"] == "native-pw"
    assert body["pg_deployment_mode"] == "external"


# ── docker runtime resolution paths ───────────────────────────────────────


def test_vnc_resolved_from_env_var_when_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside the cremind container, CREMIND_INSTALL_DIR/docker/.env doesn't
    exist — the install bundle is host-only. But the compose template sets
    VNC_PASSWORD + RESOLUTION + APP_URL + ports as env vars on the
    container. The endpoint must surface those without needing the file."""
    install_dir = tmp_path / "install"
    system_dir = tmp_path / "system"
    install_dir.mkdir()
    system_dir.mkdir()
    _stub_admin_ok(monkeypatch)
    _stub_dirs(monkeypatch, install_dir=install_dir, system_dir=system_dir)
    monkeypatch.setenv("VNC_PASSWORD", "envvar-vnc")
    monkeypatch.setenv("RESOLUTION", "1920x1080")
    monkeypatch.setenv("APP_URL", "http://10.0.0.5:1112")
    monkeypatch.setenv("INSTALL_MODE", "docker")
    monkeypatch.setenv("SETUP_WIZARD_ENV", "server")

    body = _call(_get_handler())
    assert body["deployment"] == "docker"
    assert body["available"] is True
    assert body["vnc_password"] == "envvar-vnc"
    assert body["resolution"] == "1920x1080"
    assert body["app_url"] == "http://10.0.0.5:1112"
    assert body["install_mode"] == "docker"
    assert body["setup_wizard_env"] == "server"
    # Defaults from docker.env.tmpl when not set in env.
    assert body["api_port"] == 1112
    assert body["spa_port"] == 1515
    assert body["novnc_port"] == 6080
    assert body["vnc_port"] == 5900


def test_vnc_resolved_from_compose_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CREMIND_COMPOSE_ENV_FILE points at the host's docker/.env bind-mounted
    into the container at /opt/cremind-compose/.env. When env vars aren't
    set (e.g. older compose templates), parse this file instead."""
    install_dir = tmp_path / "install"
    system_dir = tmp_path / "system"
    install_dir.mkdir()
    system_dir.mkdir()
    compose_dir = tmp_path / "opt-cremind-compose"
    compose_dir.mkdir()
    (compose_dir / ".env").write_text(
        "VNC_PASSWORD=file-vnc\n"
        "RESOLUTION=1440x900\n"
        "APP_URL=http://host.example:1112\n"
        "NOVNC_PORT=6080\n"
        "VNC_PORT=5900\n",
        encoding="utf-8",
    )
    _stub_admin_ok(monkeypatch)
    _stub_dirs(monkeypatch, install_dir=install_dir, system_dir=system_dir)
    monkeypatch.setenv("CREMIND_COMPOSE_ENV_FILE", str(compose_dir / ".env"))

    body = _call(_get_handler())
    assert body["deployment"] == "docker"
    assert body["vnc_password"] == "file-vnc"
    assert body["resolution"] == "1440x900"
    assert body["app_url"] == "http://host.example:1112"


def test_env_var_wins_over_compose_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both env var and CREMIND_COMPOSE_ENV_FILE provide a value, the
    env var wins (it's the in-container runtime truth — what the cremind
    process is actually using right now)."""
    install_dir = tmp_path / "install"
    system_dir = tmp_path / "system"
    install_dir.mkdir()
    system_dir.mkdir()
    compose_dir = tmp_path / "opt-cremind-compose"
    compose_dir.mkdir()
    (compose_dir / ".env").write_text("VNC_PASSWORD=file-vnc\n", encoding="utf-8")
    _stub_admin_ok(monkeypatch)
    _stub_dirs(monkeypatch, install_dir=install_dir, system_dir=system_dir)
    monkeypatch.setenv("CREMIND_COMPOSE_ENV_FILE", str(compose_dir / ".env"))
    monkeypatch.setenv("VNC_PASSWORD", "envvar-vnc")

    body = _call(_get_handler())
    assert body["vnc_password"] == "envvar-vnc"


def test_install_dir_file_used_as_last_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host install case: no env vars, no CREMIND_COMPOSE_ENV_FILE — the
    endpoint falls back to $CREMIND_INSTALL_DIR/docker/.env on disk."""
    install_dir = tmp_path / "install"
    system_dir = tmp_path / "system"
    _write_docker_env(install_dir, vnc="host-file-vnc")
    system_dir.mkdir()
    _stub_admin_ok(monkeypatch)
    _stub_dirs(monkeypatch, install_dir=install_dir, system_dir=system_dir)

    body = _call(_get_handler())
    assert body["deployment"] == "docker"
    assert body["vnc_password"] == "host-file-vnc"


def test_compose_env_file_provides_postgres_creds_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When PG_* live in CREMIND_COMPOSE_ENV_FILE (docker-mode docker-postgres
    runtime view) and bootstrap.toml is absent (rare but possible during
    setup), the endpoint should still surface them via the file fallback
    rather than dropping to None."""
    install_dir = tmp_path / "install"
    system_dir = tmp_path / "system"
    install_dir.mkdir()
    system_dir.mkdir()
    compose_dir = tmp_path / "opt-cremind-compose"
    compose_dir.mkdir()
    (compose_dir / ".env").write_text(
        "VNC_PASSWORD=v\n"
        "PG_USER=u\n"
        "PG_PASSWORD=runtime-pw\n"
        "PG_DATABASE=d\n",
        encoding="utf-8",
    )
    _stub_admin_ok(monkeypatch)
    _stub_dirs(monkeypatch, install_dir=install_dir, system_dir=system_dir)
    monkeypatch.setenv("CREMIND_COMPOSE_ENV_FILE", str(compose_dir / ".env"))

    body = _call(_get_handler())
    assert body["pg_password"] == "runtime-pw"
    assert body["pg_user"] == "u"
    assert body["pg_database"] == "d"
