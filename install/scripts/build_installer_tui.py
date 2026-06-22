#!/usr/bin/env python3
"""Bundle the installer TUI as a single-file Python zipapp.

The shell installer downloads ``installer_tui.pyz`` from the same base
URL as the catalog and runs it via ``uv run --with prompt_toolkit --with
rich python installer_tui.pyz --output …``. Shipping one file (instead
of curl-fetching every Python module individually) keeps the curl | bash
bootstrap robust on flaky connections and avoids a multi-file integrity
problem.

This script is stdlib-only so it can run in the same CI step as
build_catalog.py.

Usage:

  python install/scripts/build_installer_tui.py          # regenerate
  python install/scripts/build_installer_tui.py --check  # exit 1 if stale
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import tempfile
import zipapp
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent
OUT_PYZ = ROOT / "installer_tui.pyz"

# The TUI needs everything it imports — including the upgrade helpers
# (`app.upgrade.channel`, `app.upgrade.manifest`) and `app.__version__`
# that manifest.py pulls in.
SOURCES: tuple[tuple[str, str], ...] = (
    ("app/__init__.py", "app/__init__.py"),
    ("app/__version__.py", "app/__version__.py"),
    ("app/upgrade/__init__.py", "app/upgrade/__init__.py"),
    ("app/upgrade/channel.py", "app/upgrade/channel.py"),
    ("app/upgrade/manifest.py", "app/upgrade/manifest.py"),
    ("app/installer/__init__.py", "app/installer/__init__.py"),
    ("app/installer/__main__.py", "app/installer/__main__.py"),
    ("app/installer/tui.py", "app/installer/tui.py"),
    ("app/installer/catalog.py", "app/installer/catalog.py"),
    ("app/installer/output.py", "app/installer/output.py"),
    ("app/installer/releases.py", "app/installer/releases.py"),
)


def _stage(staging: Path) -> None:
    for src_rel, dst_rel in SOURCES:
        src = REPO_ROOT / src_rel
        if not src.is_file():
            raise SystemExit(f"build_installer_tui: missing source: {src}")
        dst = staging / dst_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


def _build_pyz(target: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "src"
        staging.mkdir()
        _stage(staging)
        zipapp.create_archive(
            source=staging,
            target=target,
            main="app.installer.__main__:main",
            interpreter=None,
            compressed=True,
        )


def _zip_contents(path: Path) -> dict[str, bytes]:
    """Return ``{member_name: bytes}`` for every regular file in a zip."""
    out: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            out[info.filename] = zf.read(info.filename)
    return out


# ``app/__version__.py``'s ``__version__`` string is bumped on every release
# (release-prod.yml's verify gate requires the bump on main), but the bundled
# installer only uses that value for an HTTP User-Agent header and a synthetic
# dev-forced label — never for an install decision. Comparing it verbatim would
# mark the committed pyz "stale" on every version bump, failing CI on each prod
# deploy. Neutralise just that one line so the freshness guard still catches
# real source drift, including the load-bearing ``MIN_SUPPORTED_UPGRADE_FROM``
# line in the same file.
_VERSION_LINE_RE = re.compile(rb"^(__version__\s*=\s*).*$", re.MULTILINE)


def _normalize(name: str, data: bytes) -> bytes:
    if name == "app/__version__.py":
        return _VERSION_LINE_RE.sub(rb'\g<1>"0.0.0"', data)
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if installer_tui.pyz is stale relative to sources.",
    )
    args = parser.parse_args()

    if args.check:
        # zipapp embeds the current timestamp in each zip entry, so two
        # consecutive builds never produce byte-identical archives. Compare
        # the zip's *contents* (member name → file bytes) instead, with the
        # per-release ``__version__`` string normalised out (see _normalize).
        if not OUT_PYZ.is_file():
            print(f"installer_tui.pyz is missing: {OUT_PYZ}", file=sys.stderr)
            return 1
        with tempfile.NamedTemporaryFile(suffix=".pyz", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            _build_pyz(tmp_path)
            existing = {
                k: _normalize(k, v) for k, v in _zip_contents(OUT_PYZ).items()
            }
            fresh = {
                k: _normalize(k, v) for k, v in _zip_contents(tmp_path).items()
            }
        finally:
            tmp_path.unlink(missing_ok=True)
        if existing != fresh:
            print(
                f"installer_tui.pyz is stale ({OUT_PYZ}).\n"
                "Run: python install/scripts/build_installer_tui.py",
                file=sys.stderr,
            )
            return 1
        return 0

    _build_pyz(OUT_PYZ)
    digest = hashlib.sha256(OUT_PYZ.read_bytes()).hexdigest()[:16]
    print(f"wrote {OUT_PYZ} (sha256:{digest})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
