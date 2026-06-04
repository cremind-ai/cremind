"""Persona file management utilities.

Handles reading, writing, and ensuring per-profile PERSONA.md files
within the CREMIND_SYSTEM_DIR.
"""

import os
import shutil
from pathlib import Path

from app.config.settings import BaseConfig

PERSONA_FILENAME = "PERSONA.md"
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


def _profile_persona_path(profile: str) -> Path:
    """Return the absolute path to a profile's PERSONA.md."""
    return Path(BaseConfig.CREMIND_SYSTEM_DIR) / profile / PERSONA_FILENAME


def ensure_persona_file(profile: str) -> Path:
    """Ensure ``<CREMIND_SYSTEM_DIR>/<profile>/PERSONA.md`` exists.

    If the file is missing it is copied from ``app/templates/PERSONA.md``.
    The profile directory is created when necessary.

    Returns the absolute path to the persona file.
    """
    target = _profile_persona_path(profile)
    if target.is_file():
        return target

    target.parent.mkdir(parents=True, exist_ok=True)

    template = _TEMPLATE_DIR / PERSONA_FILENAME
    if template.is_file():
        shutil.copy2(template, target)
    else:
        # Fallback: create a minimal default
        target.write_text("You are a personal AI assistant.\n", encoding="utf-8")

    return target


def read_persona_file(profile: str) -> str:
    """Read and return the content of a profile's PERSONA.md.

    Creates the file from the template if it does not yet exist.
    """
    path = ensure_persona_file(profile)
    return path.read_text(encoding="utf-8")


def write_persona_file(profile: str, content: str) -> None:
    """Write *content* to a profile's PERSONA.md.

    Creates the profile directory if it does not yet exist.
    """
    path = _profile_persona_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
