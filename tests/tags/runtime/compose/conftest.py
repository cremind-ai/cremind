"""Fixtures: the locally built font packs and a realistic card set (see tests/layout/conftest.py).

``fonts`` is the full pack (``fonts/out/full``) unless ``CREMIND_TAG_TEST_PACK`` names another pack's asset
directory (``fontpack.ctfp`` and ``cache/`` inside, e.g. a host's
``~/.cremind/.tag-runtime/assets/fonts/<pack id>``): then the whole suite runs on that pack — an older one
without 12/14 px or bold text included, which is how the composer's fallbacks are checked against the pack a
host really has. A pack named that way that cannot be loaded **fails** the tests (a skip would hide it).
``dev_fonts`` is always the dev pack (16/24 px, regular only: the old shape).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[4]
NOW = datetime(2026, 9, 27, 7, 5, tzinfo=UTC)
TEST_PACK = "CREMIND_TAG_TEST_PACK"
"""Environment variable: an asset directory whose pack replaces the full pack for these tests."""


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


def load_test_pack(directory: str) -> Any:
    """The pack in ``directory`` (relative paths from the repository root); fails when it is not usable."""
    from app.tags.runtime.fonts.fontset import FontSet

    path = Path(directory).expanduser()
    if not path.is_absolute():
        path = REPO / path
    try:
        return FontSet.load(path / "fontpack.ctfp", path / "cache")
    except Exception as exc:  # any reason at all: the pack the run was asked for is not testable
        pytest.fail(f"{TEST_PACK}={directory}: the font pack is not usable: {exc!r}", pytrace=False)


@pytest.fixture(scope="session")
def fonts() -> Any:
    directory = os.environ.get(TEST_PACK, "").strip()
    return load_test_pack(directory) if directory else load_pack("full")


@pytest.fixture(scope="session")
def dev_fonts() -> Any:
    return load_pack("dev")


@pytest.fixture
def now() -> datetime:
    return NOW
