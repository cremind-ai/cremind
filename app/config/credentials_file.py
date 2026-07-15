"""Consolidated post-install credentials file: ``$CREMIND_INSTALL_DIR/credentials.toml``.

After a Docker- or native-mode install, the user expects a single file
that lists every active service and its access info (VNC password,
Postgres password, URLs, ports). Without this file the data is split
across ``$CREMIND_INSTALL_DIR/docker/.env`` (VNC + PG env),
``$CREMIND_SYSTEM_DIR/bootstrap.toml`` (PG bootstrap), and a marker
``$CREMIND_SYSTEM_DIR/.env`` (install mode only) — and the user has no
single place to look up "how do I connect to service X again?".

This module owns the canonical Python writer. The bash and PowerShell
installers emit the same schema via heredocs at install time so the file
exists right after install completes; this writer takes over when the
Setup Wizard provisions a new service (Postgres today; vector stores
later) and regenerates the file from the on-disk sources of truth.

The file lives under ``CREMIND_INSTALL_DIR`` rather than the System
Directory because it is purely derived/regenerated — nothing in Cremind
reads it, only humans do — and pinning it to the install location keeps
a user-chosen ``CREMIND_SYSTEM_DIR`` clean of auto-emitted files.

Source-of-truth split (regenerated from disk on every call):

- ``[app]`` / ``[desktop]`` → ``$CREMIND_INSTALL_DIR/docker/.env``
  (clobbered + re-rendered on every installer run; VNC password rotates).
- ``[postgres]`` → ``$CREMIND_SYSTEM_DIR/bootstrap.toml``
  (survives installer re-runs; authoritative record of the creds Cremind
  itself will use to connect).
"""

from __future__ import annotations

import datetime as _dt
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import toml


# Default port values from install/templates/docker.env.tmpl. Used when
# the .env doesn't have them (older installs, native mode, etc.).
_DEFAULT_API_PORT = 1112
_DEFAULT_SPA_PORT = 1515
_DEFAULT_NOVNC_PORT = 6080
_DEFAULT_VNC_PORT = 5900
_DEFAULT_RESOLUTION = "1280x720"


def _credentials_path(install_dir: Path | None = None) -> Path:
    if install_dir is not None:
        return Path(install_dir) / "credentials.toml"
    from app.config.settings import BaseConfig
    return Path(BaseConfig.CREMIND_INSTALL_DIR) / "credentials.toml"


def _docker_env_path(install_dir: Path | None = None) -> Path:
    if install_dir is not None:
        return Path(install_dir) / "docker" / ".env"
    from app.config.settings import BaseConfig
    return Path(BaseConfig.CREMIND_INSTALL_DIR) / "docker" / ".env"


def _native_env_path(system_dir: Path | None = None) -> Path:
    if system_dir is not None:
        return Path(system_dir) / ".env"
    from app.config.settings import BaseConfig
    return Path(BaseConfig.CREMIND_SYSTEM_DIR) / ".env"


def parse_docker_env(path: Path) -> dict[str, str]:
    """Parse a Docker-style ``KEY=VALUE`` .env into a plain dict.

    Strips comments and blank lines, drops a single layer of matching
    quotes around values, and skips the install template's untouched
    ``__PLACEHOLDER__`` values (so an unrendered VNC_PASSWORD doesn't
    leak as a literal). Returns ``{}`` if the file is missing.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        eq = line.find("=")
        if eq <= 0:
            continue
        key = line[:eq].strip()
        value = line[eq + 1:].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if value.startswith("__") and value.endswith("__"):
            continue
        out[key] = value
    return out


def _derive_health_host(api_url: str) -> str:
    """Extract the host portion from an APP_URL like ``http://1.2.3.4:1112``."""
    if not api_url:
        return "localhost"
    rest = api_url.split("://", 1)[-1]
    host = rest.split("/", 1)[0].split(":", 1)[0]
    return host or "localhost"


def _spa_url_from_api(api_url: str, spa_port: int) -> str:
    """Derive the SPA URL from APP_URL by swapping the port."""
    if not api_url:
        return f"http://localhost:{spa_port}"
    if "://" not in api_url:
        return api_url
    scheme, rest = api_url.split("://", 1)
    host = rest.split("/", 1)[0]
    if ":" in host:
        host = host.rsplit(":", 1)[0]
    return f"{scheme}://{host}:{spa_port}"


def _int_or(default: int, raw: str | None) -> int:
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _atomic_write_toml(payload: dict[str, Any], dest: Path) -> None:
    """Write ``payload`` to ``dest`` via temp + rename, chmod 600 on POSIX."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".credentials.", suffix=".tmp", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(
                "# Cremind service credentials and connection info.\n"
                "# Auto-generated by the installer and the Setup Wizard.\n"
                "# Postgres credentials reflect the last successful wizard\n"
                "# setup; re-running the installer does not rotate them.\n"
                "# To change a value, edit the source of truth (the docker\n"
                "# bundle .env or bootstrap.toml) and re-run the installer\n"
                "# or the wizard.\n\n"
            )
            toml.dump(payload, f)
        os.replace(tmp, dest)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if sys.platform != "win32":
        try:
            os.chmod(dest, 0o600)
        except OSError:
            pass


def _read_bootstrap_from(path: Path) -> dict[str, Any]:
    """Read a bootstrap.toml at a specific path (sibling to ``read_bootstrap``).

    Mirrors :func:`app.config.bootstrap.read_bootstrap` but takes an explicit
    path so the writer can be driven against a test fixture or a relocated
    system dir without depending on ``BaseConfig`` global state.
    """
    if not path.is_file():
        return {"db_provider": "sqlite", "postgres": {}}
    try:
        data = toml.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"db_provider": "sqlite", "postgres": {}}
    provider = (data.get("db_provider") or "sqlite").strip().lower()
    pg = data.get("postgres") or {}
    return {"db_provider": provider, "postgres": pg}


def write_credentials_file(
    *,
    system_dir: Path | None = None,
    install_dir: Path | None = None,
) -> Path:
    """Regenerate ``<install_dir>/credentials.toml`` from current disk state.

    Auto-detects install mode by checking whether
    ``<install_dir>/docker/.env`` exists. Idempotent: safe to call
    multiple times in one setup flow. Returns the path written.

    Section presence rules:

    - top-level metadata: always
    - ``[app]``: always
    - ``[desktop]``: docker mode with the desktop image only (detected by a
      ``VNC_PASSWORD`` in docker/.env; the basic image's .env has none)
    - ``[postgres]``: only when ``bootstrap.toml`` has
      ``db_provider = "postgres"``
    """
    creds_path = _credentials_path(install_dir)
    docker_env = _docker_env_path(install_dir)
    native_env = _native_env_path(system_dir)
    if system_dir is not None:
        bootstrap_path = Path(system_dir) / "bootstrap.toml"
    else:
        from app.config.settings import BaseConfig
        bootstrap_path = Path(BaseConfig.CREMIND_SYSTEM_DIR) / "bootstrap.toml"

    docker_vars = parse_docker_env(docker_env)
    install_mode = "docker" if docker_vars else "native"

    payload: dict[str, Any] = {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "install_mode": install_mode,
    }

    cremind_version = docker_vars.get("CREMIND_VERSION", "")
    if cremind_version:
        payload["cremind_version"] = cremind_version

    if system_dir is not None:
        payload["system_dir"] = str(system_dir)
    else:
        from app.config.settings import BaseConfig
        payload["system_dir"] = BaseConfig.CREMIND_SYSTEM_DIR
    if install_dir is not None:
        payload["install_dir"] = str(install_dir)
    else:
        from app.config.settings import BaseConfig
        payload["install_dir"] = BaseConfig.CREMIND_INSTALL_DIR

    api_port = _int_or(_DEFAULT_API_PORT, docker_vars.get("API_PORT"))
    spa_port = _int_or(_DEFAULT_SPA_PORT, docker_vars.get("SPA_PORT"))

    if install_mode == "docker":
        api_url = docker_vars.get("APP_URL", "")
        cors = docker_vars.get("CORS_ALLOWED_ORIGINS", "")
        wizard_env = docker_vars.get("SETUP_WIZARD_ENV", "")
    else:
        native_vars = parse_docker_env(native_env)
        api_url = native_vars.get("APP_URL", "")
        cors = native_vars.get("CORS_ALLOWED_ORIGINS", "")
        wizard_env = native_vars.get("SETUP_WIZARD_ENV", "")
        api_port = _int_or(api_port, native_vars.get("PORT"))

    payload["app"] = {
        "api_url": api_url,
        "spa_url": _spa_url_from_api(api_url, spa_port),
        "api_port": api_port,
        "spa_port": spa_port,
        "cors_allowed_origins": cors,
        "setup_wizard_env": wizard_env,
    }

    # Desktop image only: the basic image's docker/.env carries no
    # VNC_PASSWORD (the installer strips the desktop-only block), so its
    # presence is the flavor signal. Mirrors install.sh / install.ps1, which
    # gate the [desktop] block the same way.
    if install_mode == "docker" and docker_vars.get("VNC_PASSWORD"):
        novnc_port = _int_or(_DEFAULT_NOVNC_PORT, docker_vars.get("NOVNC_PORT"))
        vnc_port = _int_or(_DEFAULT_VNC_PORT, docker_vars.get("VNC_PORT"))
        health_host = _derive_health_host(api_url)
        payload["desktop"] = {
            "novnc_url": f"http://{health_host}:{novnc_port}/vnc.html",
            "novnc_port": novnc_port,
            "vnc_port": vnc_port,
            "vnc_password": docker_vars.get("VNC_PASSWORD", ""),
            "resolution": docker_vars.get("RESOLUTION", _DEFAULT_RESOLUTION),
        }

    bootstrap = _read_bootstrap_from(bootstrap_path)
    if bootstrap.get("db_provider") == "postgres":
        pg = bootstrap.get("postgres") or {}
        deployment_mode = pg.get("deployment_mode")
        if not deployment_mode:
            deployment_mode = "docker" if install_mode == "docker" and pg.get("host") == "postgres" else "external"
        payload["postgres"] = {
            "deployment_mode": deployment_mode,
            "host": pg.get("host", ""),
            "port": int(pg.get("port", 5432)),
            "database": pg.get("database", ""),
            "user": pg.get("user", ""),
            "password": pg.get("password", ""),
            "sslmode": pg.get("sslmode", "prefer"),
        }

    _atomic_write_toml(payload, creds_path)
    return creds_path
