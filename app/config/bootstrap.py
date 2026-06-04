"""Bootstrap config: the database provider choice and its connection details.

This file is the only durable home for the database-provider selection. It is
read **before** any database connection because we have to know which engine
to build before we can read anything from a DB.

Layout (TOML):

    db_provider = "sqlite"          # or "postgres"

    [postgres]
    host     = "localhost"
    port     = 5432
    database = "cremind"
    user     = "cremind"
    password = "..."
    sslmode  = "prefer"             # disable | allow | prefer | require | verify-ca | verify-full

The file lives at ``<CREMIND_SYSTEM_DIR>/bootstrap.toml`` (default
``~/.cremind/bootstrap.toml``) and is created by the setup wizard. If the file
is missing, the system defaults to SQLite, which is also what a fresh
installation gets.

Environment variables (``CREMIND_DB_PROVIDER``, ``CREMIND_POSTGRES_HOST`` etc.)
override anything written to the file — useful for container deployments and
test harnesses that want to point the same image at different databases
without mutating the working dir.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import toml

from app.utils import logger


_DEFAULT_PROVIDER = "sqlite"
_DEFAULT_POSTGRES_PORT = 5432
_DEFAULT_POSTGRES_SSLMODE = "prefer"


def _bootstrap_path() -> Path:
    """Resolve the bootstrap file path from the Cremind working dir."""
    # Imported lazily so this module stays import-safe even if settings.py
    # itself wants to read the bootstrap during its own import.
    from app.config.settings import BaseConfig
    return Path(BaseConfig.CREMIND_SYSTEM_DIR) / "bootstrap.toml"


def bootstrap_exists() -> bool:
    """True iff a durable provider choice exists (file OR env override).

    Distinguishes "user has committed to a backend" from "we'd silently
    fall back to SQLite". ``read_bootstrap()`` is unsuitable for this check
    because it returns ``sqlite`` for both cases.

    The server uses this signal to decide whether to boot fully (initialize
    storage, run migrations, persist built-in tools) or to enter the
    deferred-storage mode in which the Setup Wizard's POST /api/config/setup
    is the trigger that materialises storage.
    """
    if os.environ.get("CREMIND_DB_PROVIDER"):
        return True
    return _bootstrap_path().is_file()


def read_bootstrap() -> dict[str, Any]:
    """Return the parsed bootstrap config, or sane defaults if the file is missing.

    Always returns a dict shaped like::

        {
            "db_provider": "sqlite" | "postgres",
            "postgres": {host, port, database, user, password, sslmode},
        }

    Missing keys are filled with defaults so callers can index without
    branching on presence.
    """
    path = _bootstrap_path()
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            data = toml.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            # A malformed file is treated as missing — the wizard can rewrite
            # it. We deliberately avoid raising here so a corrupt bootstrap
            # never bricks the server's ability to come up on SQLite.
            logger.warning(f"[boot] bootstrap.toml unreadable; falling back to SQLite: {e}")
            data = {}

    provider = (data.get("db_provider") or _DEFAULT_PROVIDER).strip().lower()
    pg = data.get("postgres") or {}
    postgres: dict[str, Any] = {
        "host": pg.get("host", "localhost"),
        "port": int(pg.get("port", _DEFAULT_POSTGRES_PORT)),
        "database": pg.get("database", "cremind"),
        "user": pg.get("user", "cremind"),
        "password": pg.get("password", ""),
        "sslmode": pg.get("sslmode", _DEFAULT_POSTGRES_SSLMODE),
    }
    # deployment_mode is set by the Setup Wizard ("docker" | "external")
    # so the wizard can render the right service-mode picker on reconfigure
    # and the install-secrets endpoint can expose it to the downloadable
    # config bundle. Preserve when present; omit otherwise.
    if pg.get("deployment_mode"):
        postgres["deployment_mode"] = pg["deployment_mode"]
    return {"db_provider": provider, "postgres": postgres}


def write_bootstrap(data: dict[str, Any]) -> None:
    """Atomically persist a new bootstrap config.

    Accepts the same shape as :func:`read_bootstrap`. Writes via a temp file +
    rename so a crashed write never leaves a half-formed bootstrap behind.
    """
    path = _bootstrap_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    payload: dict[str, Any] = {
        "db_provider": (data.get("db_provider") or _DEFAULT_PROVIDER).strip().lower(),
    }
    pg = data.get("postgres") or {}
    if payload["db_provider"] == "postgres" or pg:
        postgres: dict[str, Any] = {
            "host": pg.get("host", "localhost"),
            "port": int(pg.get("port", _DEFAULT_POSTGRES_PORT)),
            "database": pg.get("database", "cremind"),
            "user": pg.get("user", "cremind"),
            "password": pg.get("password", ""),
            "sslmode": pg.get("sslmode", _DEFAULT_POSTGRES_SSLMODE),
        }
        # Preserve deployment_mode ("docker" | "external") when the wizard
        # supplies it — the install-secrets endpoint and the reconfigure
        # flow read this back. Omit when unset rather than writing an
        # empty string so a later read doesn't accidentally lock in a
        # bad default.
        if pg.get("deployment_mode"):
            postgres["deployment_mode"] = pg["deployment_mode"]
        payload["postgres"] = postgres

    fd, tmp_path = tempfile.mkstemp(prefix=".bootstrap.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            toml.dump(payload, f)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError as e:
            logger.debug(f"[boot] bootstrap.toml tmp cleanup raised: {e}")
        raise


def _env_override(key: str, current: Any) -> Any:
    val = os.environ.get(key)
    if val is None or val == "":
        return current
    return val


def resolve_bootstrap() -> dict[str, Any]:
    """Read bootstrap.toml and overlay any ``CREMIND_*`` env vars on top.

    Env vars win so containerized/CI deploys can reuse a single image without
    touching the working dir. Recognized vars:

      CREMIND_DB_PROVIDER, CREMIND_POSTGRES_HOST, CREMIND_POSTGRES_PORT,
      CREMIND_POSTGRES_DATABASE, CREMIND_POSTGRES_USER,
      CREMIND_POSTGRES_PASSWORD, CREMIND_POSTGRES_SSLMODE
    """
    cfg = read_bootstrap()
    cfg["db_provider"] = (_env_override("CREMIND_DB_PROVIDER", cfg["db_provider"]) or _DEFAULT_PROVIDER).strip().lower()
    pg = cfg["postgres"]
    pg["host"] = _env_override("CREMIND_POSTGRES_HOST", pg["host"])
    port_raw = _env_override("CREMIND_POSTGRES_PORT", pg["port"])
    try:
        pg["port"] = int(port_raw)
    except (TypeError, ValueError):
        pg["port"] = _DEFAULT_POSTGRES_PORT
    pg["database"] = _env_override("CREMIND_POSTGRES_DATABASE", pg["database"])
    pg["user"] = _env_override("CREMIND_POSTGRES_USER", pg["user"])
    pg["password"] = _env_override("CREMIND_POSTGRES_PASSWORD", pg["password"])
    pg["sslmode"] = _env_override("CREMIND_POSTGRES_SSLMODE", pg["sslmode"])
    return cfg
