"""Application settings with priority chain: SQLite > TOML (Dynaconf) > .env fallback.

Server-level vars (HOST, PORT, APP_URL, ENV, DEBUG, LOG_LEVEL) remain in .env.
All other configuration reads from Dynaconf TOML defaults, overridable via SQLite dynamic config.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from app.config import settings as dynaconf_settings

dotenv_path = os.path.join("app/../.env")
load_dotenv(dotenv_path)


def _bool(val) -> bool:
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    return str(val).lower() == "true"


def _csv_list(val) -> list[str]:
    """Parse a comma-separated env var into a list of trimmed, non-empty strings."""
    if not val:
        return []
    return [item.strip() for item in val.split(",") if item.strip()]


def _dynaconf_get(key: str, default=None):
    """Get a value from Dynaconf settings, with a fallback default."""
    try:
        return dynaconf_settings.get(key, default)
    except Exception:
        return default


# Lazy reference to dynamic config storage — set at runtime after DB init.
_dynamic_config_storage = None


def set_dynamic_config_storage(storage):
    """Set the dynamic config storage (called once after DB initialization)."""
    global _dynamic_config_storage
    _dynamic_config_storage = storage


def get_dynamic(table: str, key: str, default=None, profile: str | None = None):
    """Get a value from SQLite dynamic config, falling back to Dynaconf, then to default.

    Args:
        table: One of 'server_config', 'llm_config', 'tool_configs'
        key: The config key to look up
        default: Fallback value if not found anywhere
        profile: Profile name (used for llm_config; ignored for server_config)
    """
    if _dynamic_config_storage is not None:
        kwargs = {}
        if profile is not None:
            kwargs["profile"] = profile
        val = _dynamic_config_storage.get(table, key, **kwargs)
        if val is not None:
            return val
    return default


DEFAULT_USER_WORKING_DIR = os.path.join(os.path.expanduser("~"), "Documents")


def _default_system_dir() -> str:
    """Default for the Cremind System Directory.

    ``~/.cremind`` on every platform — the Cremind "home" where runtime
    state and user content live. Power users can relocate via the
    ``CREMIND_SYSTEM_DIR`` env var.
    """
    return os.path.join(os.path.expanduser("~"), ".cremind")


def _default_install_dir() -> str:
    """Platform-conventional default for the Cremind Install Directory.

    Install scratch only — install.log, docker compose bundle, install
    PID, pip/uv caches, downloaded Python. Kept separate from the
    System Directory so install artifacts never pollute ``~/.cremind``.

    Linux:   ``$XDG_DATA_HOME/cremind`` (falls back to ``~/.local/share/cremind``)
    macOS:   ``~/Library/Application Support/Cremind``
    Windows: ``%LOCALAPPDATA%\\Cremind``
    """
    home = Path.home()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return str(Path(base) / "Cremind")
    if sys.platform == "darwin":
        return str(home / "Library" / "Application Support" / "Cremind")
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else home / ".local" / "share"
    return str(base / "cremind")


def get_system_directory() -> str:
    """Resolve the Cremind System Directory — runtime state + user content root.

    Holds: ``.env``, ``bootstrap.toml``, ``storage/cremind.db``,
    ``tokens/``, Native-mode ``venv/`` and ``bin/``, per-profile state
    (``<profile>/PERSONA.md``, ``<profile>/skills/``,
    ``<profile>/browser-profile/``, …), ``server.log``, upgrade
    artifacts, ``backups/``.

    Override via ``CREMIND_SYSTEM_DIR`` env var; otherwise ``~/.cremind``.
    """
    raw = os.environ.get("CREMIND_SYSTEM_DIR") or _default_system_dir()
    if raw.startswith("~"):
        raw = os.path.expanduser(raw)
    return os.path.normpath(raw)


def get_install_directory() -> str:
    """Resolve the Cremind Install Directory — install-time scratch only.

    Holds: ``install.log``, ``install.pid``, ``docker/`` (compose
    bundle), ``pip-cache/``, ``uv-cache/``, ``python/``. Nothing the
    user is expected to look at directly — everything here is regenerated
    by the install script (or is a transient cache).

    Override via ``CREMIND_INSTALL_DIR`` env var; otherwise platform default.
    """
    raw = os.environ.get("CREMIND_INSTALL_DIR") or _default_install_dir()
    if raw.startswith("~"):
        raw = os.path.expanduser(raw)
    return os.path.normpath(raw)


def relocate_system_directory(new_path: str) -> str:
    """First-setup-only: relocate ``CREMIND_SYSTEM_DIR`` to ``new_path``.

    Intended to be called from the Setup Wizard handler when the user
    chooses a custom System Directory and submits the wizard. At that
    point the current System Directory contains at most the install-
    script-generated ``.env`` (no ``bootstrap.toml``, no SQLite DB yet),
    so the move is cheap.

    Side effects:
      - Expands ``~`` and normalises the new path.
      - Creates ``new_path`` (``mkdir -p``).
      - Moves the existing ``.env`` from the current System Directory
        into ``new_path`` (overwrites if a stale file already exists).
      - Updates ``BaseConfig.CREMIND_SYSTEM_DIR`` in process memory.
      - Sets ``os.environ['CREMIND_SYSTEM_DIR']`` so any subprocess this
        session spawns inherits the new path.
      - Writes ``CREMIND_SYSTEM_DIR=<new_path>`` into ``new_path/.env``
        (idempotent: replaces an existing line, otherwise appends) so
        subsequent restarts pick up the chosen path via the install
        scripts that source ``$CREMIND_SYSTEM_DIR/.env`` before launch.

    Raises ``ValueError`` on invalid paths.
    Returns the normalised new path.
    """
    if not new_path or not new_path.strip():
        raise ValueError("System Directory path cannot be empty.")
    expanded = os.path.expanduser(new_path.strip()) if new_path.strip().startswith("~") else new_path.strip()
    expanded = os.path.normpath(expanded)
    if not os.path.isabs(expanded):
        raise ValueError(f"System Directory must be an absolute path: {new_path!r}")

    # ``BaseConfig`` is defined later in this module; Python resolves the
    # name at call time, so this works as long as relocation never runs
    # during module import (it doesn't — it's wired into a request handler).
    current = os.path.normpath(BaseConfig.CREMIND_SYSTEM_DIR)
    if expanded == current:
        return current

    # Refuse to nest the new path inside the current one — moving a directory
    # into itself is a recipe for partial moves and lost data.
    try:
        common = os.path.commonpath([current, expanded])
    except ValueError:
        common = ""
    if common == current:
        raise ValueError(
            f"New System Directory {expanded!r} cannot live inside the current "
            f"System Directory {current!r}."
        )

    os.makedirs(expanded, exist_ok=True)

    src_env = os.path.join(current, ".env")
    if os.path.isfile(src_env):
        dst_env = os.path.join(expanded, ".env")
        try:
            os.replace(src_env, dst_env)
        except OSError:
            # Fall back to a copy+unlink across filesystems.
            import shutil
            shutil.copy2(src_env, dst_env)
            try:
                os.unlink(src_env)
            except OSError:
                pass

    BaseConfig.CREMIND_SYSTEM_DIR = expanded
    os.environ["CREMIND_SYSTEM_DIR"] = expanded
    # Keep the derived SQLite path in sync — it was computed at class
    # definition time against the original system dir.
    BaseConfig.SQLITE_DB_PATH = os.path.join(
        expanded, "storage",
        _dynaconf_get("general.sqlite_db_path", "cremind.db"),
    )

    env_path = os.path.join(expanded, ".env")
    _upsert_env_line(env_path, "CREMIND_SYSTEM_DIR", expanded)

    return expanded


def _upsert_env_line(env_path: str, key: str, value: str) -> None:
    """Set ``KEY=value`` in a KEY=VALUE .env file, replacing any existing line."""
    lines: list[str] = []
    if os.path.isfile(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            lines = []
    needle = f"{key}="
    replaced = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(needle) or stripped.startswith(f"export {needle}"):
            lines[i] = f'{key}="{value}"'
            replaced = True
            break
    if not replaced:
        lines.append(f'{key}="{value}"')
    try:
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        # Non-fatal: server still works for this session via os.environ above,
        # but the change won't survive restart.
        pass


def get_user_working_directory() -> str:
    """Resolve the User Working Directory — the default active path injected
    into built-in tools as ``_working_directory`` and shown to the LLM as the
    ``Current User Working Directory``.

    This is the user's interaction folder — the root of the Tree Dir file
    panel and the default working directory for every built-in tool call.
    Distinct from ``BaseConfig.CREMIND_SYSTEM_DIR``, which is reserved for
    Cremind's internal storage (bootstrap.toml, credentials.toml, SQLite
    DB, tokens, per-profile state).

    Resolution order:
      1. ``server_config.user_working_dir`` (set via setup wizard)
      2. ``~/Documents`` fallback (auto-created if missing)

    The returned directory is created if it does not exist.
    """
    raw = get_dynamic("server_config", "user_working_dir", default=None)
    path = raw if raw else DEFAULT_USER_WORKING_DIR
    if path.startswith("~"):
        path = os.path.expanduser(path)
    path = os.path.normpath(path)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        # Falls through; tools that actually need the dir will surface the error.
        pass
    return path


class BaseConfig:
    """Application configuration.

    Server-level settings come from .env.
    Application settings use the priority chain: SQLite > TOML > env fallback.
    """

    # ── Server-level (from .env only) ──
    HOST = os.environ.get("HOST", "0.0.0.0")
    PORT = int(os.environ.get("PORT", 1112))
    APP_URL = os.environ.get("APP_URL", f"http://{HOST}:{PORT}")
    ENV = os.environ.get("ENV", "production")
    DEBUG = _bool(os.environ.get("DEBUG", "false"))
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
    DISABLE_LOG = _bool(os.environ.get("DISABLE_LOG", "false"))
    CORS_ALLOWED_ORIGINS = _csv_list(os.environ.get("CORS_ALLOWED_ORIGINS", "")) or ["*"]

    # ── OAuth loopback callback (built-in Google skills) ──
    # ``cremind serve`` runs ONE persistent loopback callback listener (see
    # app/api/oauth_loopback.py) so the gmail/gcalendar ``link`` flow doesn't
    # depend on a per-link ephemeral server inside a short-lived skill
    # subprocess. The Google "Desktop" client only allows loopback redirects,
    # so this is bound to a fixed port at the root path
    # (http://127.0.0.1:<port>/). In Docker the listener binds 0.0.0.0 and the
    # port is published so the host browser's redirect can reach it; on bare
    # metal it binds 127.0.0.1.
    CREMIND_OAUTH_CALLBACK_PORT = int(os.environ.get("CREMIND_OAUTH_CALLBACK_PORT", 1516))
    CREMIND_OAUTH_BIND_ADDR = os.environ.get("CREMIND_OAUTH_BIND_ADDR", "").strip() or "127.0.0.1"

    # ── Application-level (TOML defaults, overridable via SQLite) ──
    SERVICE_NAME = _dynaconf_get("general.service_name", "cremind-agent")
    AGENT_ID = _dynaconf_get("general.agent_id", "cremind_agent")
    AGENT_NAME = _dynaconf_get("general.agent_name", "Cremind Agent")
    JWT_EXPIRATION_HOURS = int(_dynaconf_get("general.jwt_expiration_hours", 720))
    SESSION_TOKEN_DEFAULT_TTL = int(_dynaconf_get("general.session_token_default_ttl", 3600))
    MCP_TOOL_CALL_TIMEOUT = int(_dynaconf_get("general.mcp_tool_call_timeout", 300))
    CREMIND_SYSTEM_DIR = get_system_directory()
    CREMIND_INSTALL_DIR = get_install_directory()

    SQLITE_DB_PATH = os.path.join(
        CREMIND_SYSTEM_DIR, "storage",
        _dynaconf_get("general.sqlite_db_path", "cremind.db"),
    )

    # ── Embedding ──
    EMBEDDING_ENABLED = _bool(_dynaconf_get("embedding.enabled", False))
    EMBEDDING_PROVIDER = _dynaconf_get("embedding.provider", "me5")
    HF_TOKEN = _dynaconf_get("embedding.hf_token", "") or os.environ.get("HF_TOKEN", "")

    # ── Vector store selection ──
    VECTORSTORE_PROVIDER = _dynaconf_get("vectorstore.provider", "qdrant")

    # ── Qdrant ──
    QDRANT_HOST =  _dynaconf_get("qdrant.host", "localhost")
    QDRANT_PORT = int(_dynaconf_get("qdrant.port", 6333))
    QDRANT_API_KEY = _dynaconf_get("qdrant.api_key", "")
    QDRANT_HTTPS = _bool(_dynaconf_get("qdrant.https", "false"))

    # ── Chroma ──
    CHROMA_MODE = _dynaconf_get("chroma.mode", "http")
    CHROMA_HOST = _dynaconf_get("chroma.host", "localhost")
    CHROMA_PORT = int(_dynaconf_get("chroma.port", 8000))
    CHROMA_SSL = _bool(_dynaconf_get("chroma.ssl", "false"))
    CHROMA_API_KEY = _dynaconf_get("chroma.api_key", "")
    _chroma_persist_raw = _dynaconf_get("chroma.persist_path", "")
    CHROMA_PERSIST_PATH = (
        os.path.normpath(os.path.expanduser(_chroma_persist_raw))
        if _chroma_persist_raw
        else os.path.join(CREMIND_SYSTEM_DIR, "storage", "chroma")
    )

    # ── LLM (configured via setup wizard, stored in SQLite) ──
    DEFAULT_MODEL_NAME = _dynaconf_get("llm.model_groups.high", "")
    REASONING_MODEL_NAME = _dynaconf_get("llm.model_groups.low", "")

    # ── Profile ──
    PROFILE = _dynaconf_get("general.profile", "")
    AUTO_AUTH_MODE = _bool(os.environ.get("AUTO_AUTH_MODE", "true"))

    @classmethod
    def get_provider_api_key(cls, provider_name: str, profile: str | None = None) -> str:
        """Get API key for a provider from SQLite dynamic config."""
        # Try SQLite first
        dynamic_val = get_dynamic("llm_config", f"{provider_name}.api_key", profile=profile)
        if dynamic_val:
            return dynamic_val

        return ""

    @classmethod
    def get_model_group(cls, group: str, profile: str | None = None) -> str:
        """Get the model identifier for a model group ('high' or 'low').

        Priority: SQLite > TOML > class default.
        """
        dynamic_val = get_dynamic("llm_config", f"model_group.{group}", profile=profile)
        if dynamic_val:
            return dynamic_val
        return _dynaconf_get(f"llm.model_groups.{group}", "")

    @classmethod
    def get_default_provider(cls, profile: str | None = None) -> str:
        """Get the default LLM provider name."""
        dynamic_val = get_dynamic("llm_config", "default_provider", profile=profile)
        if dynamic_val:
            return dynamic_val
        return _dynaconf_get("llm.default_provider", "")

    @classmethod
    def get_jwt_secret(cls) -> str:
        """Get JWT secret from SQLite dynamic config, falling back to TOML."""
        dynamic_val = get_dynamic("server_config", "jwt_secret")
        if dynamic_val:
            return dynamic_val
        return _dynaconf_get("general.jwt_secret", "")

    @classmethod
    def get_server_config(cls, key: str, default=None):
        """Get a server config value from SQLite, falling back to class attribute or default."""
        dynamic_val = get_dynamic("server_config", key)
        if dynamic_val is not None:
            return dynamic_val
        attr = key.upper()
        if hasattr(cls, attr):
            return getattr(cls, attr)
        return default

    @classmethod
    def is_embedding_enabled(cls) -> bool:
        """Whether vector embedding is enabled for this server.

        Priority: SQLite ``server_config.embedding.enabled`` > TOML default.
        Read at every call site so toggles take effect on next process start
        without rebuilding the cached class attribute.
        """
        dynamic_val = get_dynamic("server_config", "embedding.enabled")
        if dynamic_val is not None:
            return _bool(dynamic_val)
        return cls.EMBEDDING_ENABLED

    @classmethod
    def get_embedding_provider(cls) -> str:
        dynamic_val = get_dynamic("server_config", "embedding.provider")
        if dynamic_val:
            return str(dynamic_val)
        return cls.EMBEDDING_PROVIDER or "me5"

    @classmethod
    def get_hf_token(cls) -> str:
        dynamic_val = get_dynamic("server_config", "embedding.hf_token")
        if dynamic_val:
            return str(dynamic_val)
        return cls.HF_TOKEN or ""

    @classmethod
    def get_vectorstore_provider(cls) -> str:
        dynamic_val = get_dynamic("server_config", "vectorstore.provider")
        if dynamic_val:
            return str(dynamic_val).lower()
        return (cls.VECTORSTORE_PROVIDER or "qdrant").lower()

    @classmethod
    def get_qdrant_host(cls) -> str:
        return get_dynamic("server_config", "qdrant.host", cls.QDRANT_HOST or "localhost")

    @classmethod
    def get_qdrant_port(cls) -> int:
        val = get_dynamic("server_config", "qdrant.port", cls.QDRANT_PORT)
        try:
            return int(val)
        except (TypeError, ValueError):
            return cls.QDRANT_PORT

    @classmethod
    def get_qdrant_api_key(cls) -> str:
        val = get_dynamic("server_config", "qdrant.api_key", cls.QDRANT_API_KEY)
        return val or ""

    @classmethod
    def get_qdrant_https(cls) -> bool:
        val = get_dynamic("server_config", "qdrant.https")
        if val is not None:
            return _bool(val)
        return bool(cls.QDRANT_HTTPS)

    @classmethod
    def get_chroma_mode(cls) -> str:
        return (get_dynamic("server_config", "chroma.mode", cls.CHROMA_MODE) or "http").lower()

    @classmethod
    def get_chroma_host(cls) -> str:
        return get_dynamic("server_config", "chroma.host", cls.CHROMA_HOST or "localhost")

    @classmethod
    def get_chroma_port(cls) -> int:
        val = get_dynamic("server_config", "chroma.port", cls.CHROMA_PORT)
        try:
            return int(val)
        except (TypeError, ValueError):
            return cls.CHROMA_PORT

    @classmethod
    def get_chroma_ssl(cls) -> bool:
        val = get_dynamic("server_config", "chroma.ssl")
        if val is not None:
            return _bool(val)
        return bool(cls.CHROMA_SSL)

    @classmethod
    def get_chroma_api_key(cls) -> str:
        val = get_dynamic("server_config", "chroma.api_key", cls.CHROMA_API_KEY)
        return val or ""

    @classmethod
    def get_chroma_persist_path(cls) -> str:
        raw = get_dynamic("server_config", "chroma.persist_path")
        if raw:
            path = os.path.normpath(os.path.expanduser(str(raw)))
            return path
        return cls.CHROMA_PERSIST_PATH

    # ── Database provider (read from bootstrap.toml, NOT from SQLite) ──
    # The DB provider choice itself can't live in the database — chicken
    # and egg. ``app.config.bootstrap`` reads a tiny TOML file under the
    # working dir; these helpers wrap it so the rest of the app has a
    # single, uniform place to ask "which DB are we on?".

    @classmethod
    def get_database_provider(cls) -> str:
        from app.config.bootstrap import resolve_bootstrap
        return resolve_bootstrap()["db_provider"]

    @classmethod
    def get_postgres_config(cls) -> dict:
        from app.config.bootstrap import resolve_bootstrap
        return resolve_bootstrap()["postgres"]
