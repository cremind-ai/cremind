"""Sync ui/package.json's "version" field from app/__version__.py.

The backend version in app/__version__.py is the single source of truth.
This script copies it into ui/package.json so the Electron app, the Vite
bundle, and the wheel all advertise the same version.

PEP 440 rc versions (e.g. ``0.2.1rc12`` or ``0.2.1rc12.dev3``) are
translated to the SemVer pre-release form (``0.2.1-rc.12`` /
``0.2.1-rc.12.dev.3``) on the way in. electron-builder and npm validate
strictly against SemVer and reject the PEP 440 no-separator form;
stable releases pass through unchanged.

Wired into ui/package.json as prebuild/preweb:build/predev hooks so it
runs automatically before any UI build. Also safe to invoke manually:
    python scripts/sync_ui_version.py

Idempotent — exits 0 with a no-op message when the versions already match.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_VERSION_FILE = REPO_ROOT / "app" / "__version__.py"
UI_PACKAGE_JSON = REPO_ROOT / "ui" / "package.json"

VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)
PEP440_RC_RE = re.compile(r"^(\d+\.\d+\.\d+(?:\.\d+)?)rc(\d+)(?:\.dev(\d+))?$")


def read_backend_version() -> str:
    text = BACKEND_VERSION_FILE.read_text(encoding="utf-8")
    match = VERSION_RE.search(text)
    if not match:
        raise SystemExit(f"Could not find __version__ in {BACKEND_VERSION_FILE}")
    return match.group(1)


def pep440_to_semver(version: str) -> str:
    m = PEP440_RC_RE.match(version)
    if not m:
        return version
    base, rc, dev = m.group(1), m.group(2), m.group(3)
    if dev is not None:
        return f"{base}-rc.{rc}.dev.{dev}"
    return f"{base}-rc.{rc}"


def main() -> int:
    backend_version = read_backend_version()
    target_version = pep440_to_semver(backend_version)
    package = json.loads(UI_PACKAGE_JSON.read_text(encoding="utf-8"))
    current = package.get("version")

    if current == target_version:
        print(f"[sync_ui_version] ui/package.json already at {target_version}")
        return 0

    package["version"] = target_version
    UI_PACKAGE_JSON.write_text(
        json.dumps(package, indent=2) + "\n",
        encoding="utf-8",
    )
    if target_version != backend_version:
        print(
            f"[sync_ui_version] ui/package.json: {current} -> {target_version} "
            f"(from PEP 440 {backend_version})"
        )
    else:
        print(f"[sync_ui_version] ui/package.json: {current} -> {target_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
