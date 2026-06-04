"""Channel-aware install/upgrade version filtering.

Thin compatibility shim. The rules used to live here, but they're
version-format knowledge, so they now live with the other version
primitives in :mod:`app.upgrade.channel` — the single source of truth
the install scripts also run standalone. This module re-exports them so
existing callers (``from app.upgrade import version_filter``) and tests
keep working.

Two layers (see the docstrings on the re-exported functions):

  - :func:`matches_channel` — the looser shape check used by standalone
    ``install.sh`` / ``install.ps1`` invocations (no ``rcN`` on prod,
    mandatory ``rcN`` — with optional ``.devM`` — on test).
  - :func:`matches_electron_line` — adds the Electron-build line
    constraint the in-app installer needs.
"""

from __future__ import annotations

from app.upgrade.channel import (
    filter_same_line,
    matches_channel,
    matches_electron_line,
    validate,
)

__all__ = [
    "filter_same_line",
    "matches_channel",
    "matches_electron_line",
    "validate",
]
