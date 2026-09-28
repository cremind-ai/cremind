"""The font asset bundle a hardware host needs, installed and verified.

A worker composes screens with the same fonts the bridges draw from: the
**font asset bundle** (``fonts/<pack_id>/``: the binary pack, its sidecar, the
exact source fonts, notices and licences). People never build it; it comes
from, in order:

1. ``CREMIND_TAG_FONT_BUNDLE`` — an unpacked assets root, a
   ``.tar.gz``, or an ``https://`` URL (operators, air-gapped installs);
2. the bundle Cremind's release pipeline publishes, pinned by pack id and
   SHA-256 in ``app/tags/runtime/fonts/bundle.json``;
3. a developer checkout that already built the pack (``fonts/out/<profile>``
   and ``fonts/cache``).

Whatever the source, it is verified (the archive's SHA-256 when pinned, then
the pack and every source font against the sidecar) and copied read-only to
``<SYS>/.tag-runtime/assets/fonts/<pack_id>`` (:func:`install_font_assets`).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BUNDLE_ENV = "CREMIND_TAG_FONT_BUNDLE"
LOCK_FILE = Path(__file__).resolve().parents[1] / "runtime" / "fonts" / "bundle.json"
DOWNLOAD_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class Installed:
    pack_id: str | None
    changed: bool
    message: str


def bundle_lock() -> dict[str, Any] | None:
    """The release's pinned bundle: ``{pack_id, profile, url, sha256, size}`` (``None`` in a checkout without one)."""
    try:
        doc = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not doc.get("pack_id"):
        return None
    return doc


def installed_packs(assets_dir: Path) -> list[Any]:
    from app.tags.runtime.resources import font_assets_in

    return font_assets_in(assets_dir)


def ensure_installed(assets_dir: Path, say: Callable[[str], None] = lambda _m: None) -> Installed:
    """Install the expected font bundle unless it is already there and verified."""
    from app.tags.runtime.resources import FontAssetsError, install_font_assets, load_font_assets, verify_font_assets

    lock = bundle_lock()
    expected = str(lock["pack_id"]) if lock else None
    for existing in installed_packs(assets_dir):
        if expected is None or existing.pack_id == expected:
            try:
                verify_font_assets(existing)
                return Installed(existing.pack_id, False, f"Font pack {existing.pack_id} is installed.")
            except FontAssetsError as exc:
                say(f"The installed font pack {existing.pack_id} is damaged ({exc}); installing it again.")
    with tempfile.TemporaryDirectory(prefix="ctag-fonts-") as tmp:
        root = _source(Path(tmp), lock, say)
        if root is None:
            return Installed(None, False, "No font pack is available yet: gateways connect and pair, and screens "
                                          "wait until one is installed.")
        packs = [p for p in (root / "fonts").iterdir() if p.is_dir() and not p.name.startswith(".")] \
            if (root / "fonts").is_dir() else []
        chosen = next((p for p in packs if expected is None or p.name == expected), None)
        if chosen is None:
            raise ValueError(f"the font bundle holds no pack {expected or ''}".strip())
        source = load_font_assets(chosen)
        say(f"Verifying font pack {source.pack_id}…")
        installed = install_font_assets(source, assets_dir)
    return Installed(installed.pack_id, True, f"Font pack {installed.pack_id} installed.")


def _source(tmp: Path, lock: dict[str, Any] | None, say: Callable[[str], None]) -> Path | None:
    """An assets root holding ``fonts/<pack_id>`` from the first available source."""
    env = os.environ.get(BUNDLE_ENV, "").strip()
    if env:
        say(f"Using the font bundle named by {BUNDLE_ENV}.")
        return _materialise(env, tmp, None)
    checkout = _checkout_pack(str((lock or {}).get("profile") or "full"))
    # A checkout that already built the pinned pack (or any pack, without a pin) needs no download.
    if checkout is not None and (lock is None or _pack_id(checkout[0]) == str(lock.get("pack_id"))):
        from app.tags.runtime.resources import make_font_assets

        pack_dir, cache = checkout
        say(f"Using the font pack built in this checkout ({pack_dir}).")
        make_font_assets(pack_dir, cache, tmp / "assets")
        return tmp / "assets"
    if lock and lock.get("url"):
        say(f"Downloading font pack {lock['pack_id']} ({int(lock.get('size') or 0) // (1 << 20)} MB)…")
        return _materialise(str(lock["url"]), tmp, str(lock.get("sha256") or "") or None)
    return None


def _pack_id(pack_dir: Path) -> str | None:
    try:
        return str(json.loads((pack_dir / "fontpack.json").read_text(encoding="utf-8")).get("pack_id") or "") or None
    except (OSError, ValueError):
        return None


def _checkout_pack(profile: str) -> tuple[Path, Path] | None:
    try:
        from app.tags.runtime.fonts.manifest import repo_root

        repo = repo_root()
    except Exception:  # noqa: BLE001 - an installed Cremind has no checkout
        return None
    pack_dir, cache = repo / "fonts" / "out" / profile, repo / "fonts" / "cache"
    if (pack_dir / "fontpack.ctfp").is_file() and cache.is_dir():
        return pack_dir, cache
    return None


def _materialise(where: str, tmp: Path, sha256: str | None) -> Path:
    if where.startswith(("https://", "http://")):
        archive = tmp / "bundle.tar.gz"
        _download(where, archive)
    else:
        path = Path(where)
        if path.is_dir():
            return path
        archive = path
    if sha256 is not None:
        actual = _sha256(archive)
        if actual != sha256.lower():
            raise ValueError(f"the font bundle's SHA-256 is {actual}, the release pins {sha256}")
    target = tmp / "unpacked"
    target.mkdir()
    with tarfile.open(archive, "r:*") as tar:
        tar.extractall(target, filter="data")
    if (target / "fonts").is_dir():
        return target
    inner = [p for p in target.iterdir() if p.is_dir()]
    if len(inner) == 1 and (inner[0] / "fonts").is_dir():
        return inner[0]
    raise ValueError("the font bundle has no fonts/ directory")


def _download(url: str, out: Path) -> None:
    import httpx

    with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(DOWNLOAD_TIMEOUT_S, connect=30.0)) as r:
        r.raise_for_status()
        with open(out, "wb") as handle:
            for chunk in r.iter_bytes(1 << 20):
                handle.write(chunk)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def remove_other_packs(assets_dir: Path, keep: str) -> None:
    """Drop superseded packs (read-only copies) once workers use ``keep``."""
    from app.tags.runtime.resources import _rmtree

    for pack in installed_packs(assets_dir):
        if pack.pack_id != keep:
            _rmtree(pack.root)
    shutil.rmtree(assets_dir / "fonts" / ".tmp", ignore_errors=True)


__all__ = ["BUNDLE_ENV", "Installed", "bundle_lock", "ensure_installed", "installed_packs", "remove_other_packs"]
