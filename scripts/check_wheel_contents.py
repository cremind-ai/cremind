"""Assert the built wheel carries the files the runtime needs.

Guards a packaging failure mode that is invisible until deploy: hatchling
only ships VCS-tracked files, so a stray ``.gitignore`` rule silently drops
a *data* file from the wheel while every import still succeeds. That is
exactly how the channel sidecars shipped without their ``package-lock.json``
— the app installed fine and then refused to start Zalo/WhatsApp on Docker
and Kubernetes, where nobody can run ``npm install`` by hand.

Two assertions:

- Every sidecar's ``package-lock.json`` is present, so the boot-time
  ``npm ci`` (``app/channels/sidecars/bootstrap.py``) has its input.
- No ``node_modules/`` leaks in. Running the server from a checkout
  installs those in-tree, and they must never bloat the wheel; the nested
  ``app/channels/sidecars/*/.gitignore`` files are what prevent it.

Usage:
    python scripts/check_wheel_contents.py [dist/cremind-*.whl]

With no argument it picks the newest wheel in ``dist/``. Exits non-zero
with an explanation on failure.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

# Sidecars whose lockfile must ship. Keep in sync with the directories under
# app/channels/sidecars/ that contain a package.json.
REQUIRED_LOCKFILES = (
    "app/channels/sidecars/whatsapp/package-lock.json",
    "app/channels/sidecars/zalo/package-lock.json",
)


def _resolve_wheel(argv: list[str]) -> Path:
    if len(argv) > 1:
        wheel = Path(argv[1])
        if not wheel.is_file():
            sys.exit(f"not a file: {wheel}")
        return wheel
    candidates = sorted(
        Path("dist").glob("*.whl"),
        key=lambda p: p.stat().st_mtime,
    )
    if not candidates:
        sys.exit("no wheel found in dist/ — run `hatch build` first")
    return candidates[-1]


def main(argv: list[str]) -> int:
    wheel = _resolve_wheel(argv)
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())

    problems: list[str] = []

    missing = [name for name in REQUIRED_LOCKFILES if name not in names]
    if missing:
        problems.append(
            "wheel is missing sidecar lockfile(s):\n  "
            + "\n  ".join(missing)
            + "\nIs .gitignore swallowing them again? They must stay tracked "
            "(see the lockfile block in .gitignore).",
        )

    leaked = sorted(name for name in names if "/node_modules/" in name)
    if leaked:
        problems.append(
            f"wheel bundles node_modules ({len(leaked)} entries), e.g.:\n  "
            + "\n  ".join(leaked[:5])
            + "\nSidecar dependencies are installed at run time, not shipped.",
        )

    if problems:
        print(f"FAIL: {wheel}", file=sys.stderr)
        for problem in problems:
            print(f"\n{problem}", file=sys.stderr)
        return 1

    print(f"OK: {wheel.name} — sidecar lockfiles present, no node_modules leakage")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
