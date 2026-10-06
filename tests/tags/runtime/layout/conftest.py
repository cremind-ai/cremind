"""Fixtures: the locally built font packs (fonts/out/<profile>) and the verified font cache.

Tests that need them are marked ``fonts`` and skip cleanly when
``cremind tags tools fonts fetch`` / ``fonts build`` have not been run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[4]


def load_pack(profile: str) -> Any:
    from app.tags.runtime.fonts.fontset import FontSet
    from app.tags.runtime.fonts.manifest import ManifestError

    pack = REPO / "fonts" / "out" / profile / "fontpack.ctfp"
    cache = REPO / "fonts" / "cache"
    if not pack.is_file() or not cache.is_dir():
        pytest.skip(f"{pack} or {cache} is missing (run `cremind tags tools fonts fetch` and `fonts build`)")
    try:
        return FontSet.load(pack, cache)
    except (OSError, ValueError, ManifestError) as exc:
        pytest.skip(f"font pack {profile} not usable: {exc}")


@pytest.fixture(scope="session")
def fonts() -> Any:
    """The full pack (172 faces: icons, 170 regular text faces with emoji, Noto Sans Bold; 12/14/16/24/32 px)."""
    return load_pack("full")


@pytest.fixture(scope="session")
def dev_fonts() -> Any:
    """The dev pack (6 faces, 16/24 px — no 32 px strikes)."""
    return load_pack("dev")
