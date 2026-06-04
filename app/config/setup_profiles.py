"""Setup wizard environment profiles.

Loads ``setup_profiles.toml`` (next to this module) and exposes the parsed
profiles to the wizard API and any external tooling that wants to share the
same defaults — e.g. a future ``install.sh`` that needs to know which DB and
vector store ship with each environment shape.

Layout: see ``setup_profiles.toml``.

The active preset id is taken from the ``SETUP_WIZARD_ENV`` environment
variable (typically set in the project ``.env`` file). The wizard uses it
to pre-fill its forms — every field remains editable; the preset only
seeds the defaults so an operator can mostly click through the steps.

If the file is missing or malformed we fall back to a hardcoded copy of the
``local`` profile so the wizard is never bricked by a bad TOML — same
defensive pattern as :mod:`app.config.bootstrap`.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import toml

from app.utils.logger import logger


_ACTIVE_PROFILE_ENV = "SETUP_WIZARD_ENV"


_PROFILES_FILE = Path(__file__).parent / "setup_profiles.toml"


# Hardcoded fallback. Used only when ``setup_profiles.toml`` cannot be loaded.
# Keep this minimal — a single profile that is guaranteed to work without any
# external service so the wizard always has something safe to land on.
_DEFAULT_PROFILES: dict[str, dict[str, Any]] = {
    "local": {
        "label": "Local Development",
        "description": "SQLite + ChromaDB persistent. No external services required.",
        "server_config": {
            "db_provider": "sqlite",
            "sqlite_db_path": "cremind.db",
            "service_name": "cremind-agent",
            "agent_name": "Cremind Agent",
            "user_working_dir": "~/.cremind",
        },
        "embedding_config": {
            "enabled": False,
            "provider": "me5",
            "hf_token": "",
            "vectorstore": {
                "provider": "chroma",
                "qdrant": {"host": "localhost", "port": 6333, "api_key": "", "https": False},
                "chroma": {
                    "mode": "persistent",
                    "host": "localhost",
                    "port": 8000,
                    "ssl": False,
                    "api_key": "",
                    "persist_path": "",
                },
            },
        },
    },
}


_cached: dict[str, dict[str, Any]] | None = None


def _normalise_profile(profile_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Stamp ``id`` onto each profile.

    The TOML doesn't carry a self-id field; we set it from the table name so
    the API response is self-describing and the wizard can identify which
    preset it just received.
    """
    return {
        "id": profile_id,
        "label": raw.get("label") or profile_id.title(),
        "description": raw.get("description", ""),
        "server_config": raw.get("server_config") or {},
        "embedding_config": raw.get("embedding_config") or {},
    }


def load_setup_profiles(*, force_reload: bool = False) -> dict[str, dict[str, Any]]:
    """Return all setup profiles keyed by id.

    Cached after the first call. Pass ``force_reload=True`` from tests to
    pick up a freshly written TOML.
    """
    global _cached
    if _cached is not None and not force_reload:
        return _cached

    raw: dict[str, Any] = {}
    if _PROFILES_FILE.is_file():
        try:
            raw = toml.loads(_PROFILES_FILE.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"setup_profiles.toml is malformed ({exc}); falling back to built-in defaults."
            )
            raw = {}
    else:
        logger.warning(
            f"setup_profiles.toml not found at {_PROFILES_FILE}; falling back to built-in defaults."
        )

    if not raw:
        _cached = {pid: _normalise_profile(pid, body) for pid, body in _DEFAULT_PROFILES.items()}
        return _cached

    out: dict[str, dict[str, Any]] = {}
    for profile_id, body in raw.items():
        if not isinstance(body, dict):
            continue
        out[profile_id] = _normalise_profile(profile_id, body)

    if not out:
        out = {pid: _normalise_profile(pid, body) for pid, body in _DEFAULT_PROFILES.items()}

    _cached = out
    return out


def get_setup_profile(name: str) -> dict[str, Any] | None:
    """Look up a single profile by id. Returns ``None`` if unknown."""
    if not name:
        return None
    return load_setup_profiles().get(name)


def list_setup_profiles() -> list[dict[str, Any]]:
    """Return profiles as a stable-ordered list, preserving TOML order."""
    return list(load_setup_profiles().values())


def get_active_setup_profile_id() -> str | None:
    """Return the preset id selected via ``SETUP_WIZARD_ENV`` (typically set
    in ``.env``), or ``None`` if unset, blank, or pointing at an unknown id.

    Resolved fresh on every call so changing ``.env`` and restarting the
    server is enough to take effect — no caching required.
    """
    raw = os.environ.get(_ACTIVE_PROFILE_ENV, "").strip()
    if not raw:
        return None
    profiles = load_setup_profiles()
    if raw not in profiles:
        logger.warning(
            f"{_ACTIVE_PROFILE_ENV}={raw!r} does not match any profile in "
            f"setup_profiles.toml; ignoring."
        )
        return None
    return raw


def _cli() -> int:
    """``python -m app.config.setup_profiles --json`` dumps profiles as JSON.

    Lets shell tooling (e.g. install.sh) consume the same source of truth
    without having to parse TOML themselves.
    """
    args = sys.argv[1:]
    profiles = list_setup_profiles()
    if "--json" in args:
        json.dump(profiles, sys.stdout, indent=2, sort_keys=False)
        sys.stdout.write("\n")
        return 0
    # Default: human-readable summary
    for p in profiles:
        sys.stdout.write(f"{p['id']}: {p['label']} — {p['description']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
