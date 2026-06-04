"""Configuration management using Dynaconf + TOML with SQLite dynamic overrides.

Priority chain: SQLite dynamic config > TOML file defaults > environment variable fallback.
"""

from pathlib import Path

import toml
from dynaconf import Dynaconf

# Config directory is the same directory as this file (app/config/)
_CONFIG_DIR = Path(__file__).resolve().parent

# Initialize Dynaconf for TOML-based configuration
settings = Dynaconf(
    settings_files=[str(_CONFIG_DIR / "settings.toml")],
    envvar_prefix="CREMIND",
    environments=False,
    load_dotenv=True,
)


def load_provider_catalog(provider_name: str) -> dict:
    """Load a provider's model catalog from its TOML config file.

    Returns the parsed TOML dict, or empty dict if file not found.
    """
    toml_path = _CONFIG_DIR / "providers" / f"{provider_name}.toml"
    if not toml_path.exists():
        return {}
    with open(toml_path, "r") as f:
        return toml.load(f)


def load_all_provider_catalogs() -> dict[str, dict]:
    """Load all provider catalogs from app/config/providers/*.toml.

    Returns dict keyed by provider name.
    """
    providers_dir = _CONFIG_DIR / "providers"
    catalogs = {}
    if not providers_dir.exists():
        return catalogs
    for toml_file in providers_dir.glob("*.toml"):
        data = toml.load(toml_file)
        provider_info = data.get("provider", {})
        name = provider_info.get("name", toml_file.stem)
        catalogs[name] = data
    return catalogs


def load_channel_catalog(channel_type: str) -> dict:
    """Load a single channel's TOML config (auth modes, fields, instructions)."""
    toml_path = _CONFIG_DIR / "channels" / f"{channel_type}.toml"
    if not toml_path.exists():
        return {}
    with open(toml_path, "r", encoding="utf-8") as f:
        return toml.load(f)


def load_all_channel_catalogs() -> dict[str, dict]:
    """Load every ``app/config/channels/*.toml``, keyed by channel type.

    The loader validates that each file declares ``[channel]`` with
    ``type``, ``display_name``, and at least one ``[[channel.modes]]`` entry.
    Files that fail validation are skipped with a warning.
    """
    channels_dir = _CONFIG_DIR / "channels"
    catalogs: dict[str, dict] = {}
    if not channels_dir.exists():
        return catalogs
    for toml_file in channels_dir.glob("*.toml"):
        try:
            data = toml.load(toml_file)
            channel_info = data.get("channel") or {}
            channel_type = channel_info.get("type")
            if not channel_type or not channel_info.get("display_name"):
                continue
            modes = channel_info.get("modes") or []
            if not modes:
                continue
            catalogs[channel_type] = data
        except Exception:  # noqa: BLE001
            # Bad TOML shouldn't crash the server; skip and continue.
            continue
    return catalogs


