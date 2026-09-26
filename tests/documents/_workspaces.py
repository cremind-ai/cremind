"""Per-profile working directories for Documentation search tests.

Documentation search always indexes the profile's own working directory
(:mod:`app.config.working_dirs`), which resolves through the dynamic config
storage. :func:`install` puts a fake one in place: ``rows`` maps each live
profile to the folder the admin chose for it, or None for its default,
``<system dir>/workspaces/<profile>``. List every profile the test uses —
ownership (who may reach which folder) is computed from these rows.

The ownership snapshot is cached in the module; ``monkeypatch`` restores it
at teardown, so nothing a test resolved leaks into the next one.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from app.config import working_dirs

# By module path: ``app.config.settings`` the attribute is a Dynaconf object.
_cfg = importlib.import_module("app.config.settings")


class WorkingDirStore:
    """The DynamicConfigStorage surface the working-directory code reads."""

    def __init__(self, rows: dict[str, Any]):
        self.rows = {k: (str(v) if v else None) for k, v in rows.items()}

    def get(self, *_a: Any, **_k: Any) -> None:  # get_dynamic's read: nothing stored
        return None

    def get_profile_working_dir(self, profile: str) -> str | None:
        return self.rows.get(profile)

    def set_profile_working_dir(self, profile: str, value: str | None) -> bool:
        if profile not in self.rows:
            return False
        self.rows[profile] = value
        return True

    def profile_working_dirs(self) -> dict[str, str | None]:
        return dict(self.rows)


def install(monkeypatch, sysdir: Path | str, rows: dict[str, Any]) -> WorkingDirStore:
    """Point the system dir at ``sysdir`` and every profile's working
    directory at ``rows`` (None = the default under ``<sysdir>/workspaces``)."""
    monkeypatch.setattr(_cfg.BaseConfig, "CREMIND_SYSTEM_DIR", str(sysdir))
    monkeypatch.delenv(working_dirs.WORKSPACES_ENV, raising=False)
    store = WorkingDirStore(rows)
    monkeypatch.setattr(_cfg, "_dynamic_config_storage", store)
    monkeypatch.setattr(working_dirs, "_snapshot", None)
    return store


def move(store: WorkingDirStore, profile: str, path: Path | str | None) -> None:
    """What the admin's change does: store it, drop the cache, tell listeners."""
    store.rows[profile] = str(path) if path else None
    working_dirs.notify_changed(profile)
