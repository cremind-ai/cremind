"""Suite-wide safety net: never touch the developer's real install.

Unredirected, ``BaseConfig`` resolves the system directory to ``~/.cremind``
and the install directory to the platform default (``%LOCALAPPDATA%\\Cremind``
on Windows, ``~/Library/Application Support/Cremind`` on macOS,
``~/.local/share/cremind`` on Linux) — so any test that does not redirect them
itself writes the real install's database, ``bootstrap.toml``, ``tokens/``,
``tls/``, skills, blueprints and ``credentials.toml``. Resolving a profile's
working directory creates it (``app.config.working_dirs.profile_working_dir``)
under ``<SYS>/workspaces``, which lands in the same place.

So for the whole session all three point at throwaway folders:
``CREMIND_SYSTEM_DIR``, ``CREMIND_INSTALL_DIR`` and ``CREMIND_WORKSPACES_DIR``.
They are set here, at import time, before any app module is imported —
``BaseConfig`` binds the first two in its class body — and every subprocess a
test starts inherits them. Tests that exercise the default resolution
``monkeypatch.delenv`` the variable (it is present, so monkeypatch restores it
afterwards); tests that want a folder of their own ``monkeypatch.setenv`` it or
patch ``BaseConfig`` as before.
"""

from __future__ import annotations

import os
import shutil
import tempfile

_ROOT = tempfile.mkdtemp(prefix="cremind-test-")
_SYSTEM = os.path.join(_ROOT, "system")
_INSTALL = os.path.join(_ROOT, "install")
_WORKSPACES = os.path.join(_ROOT, "workspaces")
for _d in (_SYSTEM, _INSTALL, _WORKSPACES):
    os.makedirs(_d, exist_ok=True)

os.environ["CREMIND_SYSTEM_DIR"] = _SYSTEM
os.environ["CREMIND_INSTALL_DIR"] = _INSTALL
os.environ["CREMIND_WORKSPACES_DIR"] = _WORKSPACES


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 — pytest hook signature
    shutil.rmtree(_ROOT, ignore_errors=True)
