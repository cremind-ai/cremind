"""Per-profile agent-name management utilities.

Each profile has a human-facing *agent name* — the assistant's own name —
stored as a small text file at ``<CREMIND_SYSTEM_DIR>/<profile>/agent_name.txt``
(mirroring the per-profile ``PERSONA.md`` in :mod:`app.utils.persona`).

The file holds only an explicit override. When it is missing or empty the
default is computed at read time: ``"Cremind"`` for the ``admin`` profile, and
the profile's own name for every other profile. Reads are synchronous so the
``CREMIND_AGENT_NAME`` system-variable resolver (which must be sync) can use them
directly.
"""

from pathlib import Path

from app.config.settings import BaseConfig

AGENT_NAME_FILENAME = "agent_name.txt"

# The admin profile's agent is the product itself.
DEFAULT_ADMIN_AGENT_NAME = "Cremind"


def _profile_agent_name_path(profile: str) -> Path:
    """Return the absolute path to a profile's agent-name file."""
    return Path(BaseConfig.CREMIND_SYSTEM_DIR) / profile / AGENT_NAME_FILENAME


def default_agent_name(profile: str) -> str:
    """The default agent name for *profile* when no override is set.

    ``admin`` defaults to ``"Cremind"``; every other profile defaults to its
    own name.
    """
    return DEFAULT_ADMIN_AGENT_NAME if profile == "admin" else profile


def read_agent_name(profile: str) -> str:
    """Read a profile's agent name, falling back to :func:`default_agent_name`.

    Returns the default when the file is missing, empty, or unreadable, so the
    caller always gets a usable name.
    """
    path = _profile_agent_name_path(profile)
    try:
        name = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return default_agent_name(profile)
    return name or default_agent_name(profile)


def write_agent_name(profile: str, name: str) -> None:
    """Persist *name* as a profile's agent-name override.

    Creates the profile directory if it does not yet exist. The value is
    stripped of surrounding whitespace before writing.
    """
    path = _profile_agent_name_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(name.strip(), encoding="utf-8")
