"""Standing-instructions file management utilities.

Handles reading and writing per-profile INSTRUCTIONS.md files within the
CREMIND_SYSTEM_DIR.

This is the sibling of :mod:`app.utils.persona`, split off on purpose: the
persona says *who the agent is* (identity, personality, tone), while these
instructions say *what it must do* — standing directives the operator wants
followed in every conversation ("when a new user messages this channel,
register them in the Active-User sheet").

Unlike the persona there is no seed template: an absent file means "no
standing directives", so reads return ``""`` and never create anything. That
keeps the rendered system prompt byte-identical for profiles that never use
the feature, which matters for prompt-cache reuse.
"""

from pathlib import Path

from app.config.settings import BaseConfig

INSTRUCTIONS_FILENAME = "INSTRUCTIONS.md"


def _profile_instructions_path(profile: str) -> Path:
    """Return the absolute path to a profile's INSTRUCTIONS.md."""
    return Path(BaseConfig.CREMIND_SYSTEM_DIR) / profile / INSTRUCTIONS_FILENAME


def read_instructions_file(profile: str) -> str:
    """Read a profile's INSTRUCTIONS.md, or ``""`` when it does not exist.

    Never creates the file — "no file" and "empty file" both mean the profile
    has no standing instructions.
    """
    path = _profile_instructions_path(profile)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def write_instructions_file(profile: str, content: str) -> None:
    """Write *content* to a profile's INSTRUCTIONS.md.

    Creates the profile directory if it does not yet exist.
    """
    path = _profile_instructions_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
