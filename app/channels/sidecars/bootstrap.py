"""Bootstrap for Node-based channel sidecars.

Each sibling directory under ``app/channels/sidecars/`` that contains a
``package.json`` is treated as a sidecar. At server startup we verify the
sidecar's ``node_modules`` is present and matches the committed
``package-lock.json``; if anything is off we run ``npm ci`` synchronously so
the server never reaches a "ready" state with broken sidecar dependencies.

The same freshness check is reused by channel adapters as a runtime guard
(in case ``node_modules`` is removed while the server is running).
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from app.utils.logger import logger

SIDECARS_ROOT = Path(__file__).resolve().parent


class SidecarBootstrapError(RuntimeError):
    """Raised when a sidecar's dependencies cannot be installed."""


def discover_sidecars() -> list[Path]:
    """Every directory under ``sidecars/`` containing a ``package.json``."""
    return sorted(p.parent for p in SIDECARS_ROOT.glob("*/package.json"))


def is_install_fresh(sidecar_dir: Path) -> tuple[bool, str]:
    """Return ``(fresh?, reason)`` for ``sidecar_dir``.

    "Fresh" means ``node_modules`` exists and was last installed against the
    current ``package-lock.json`` / ``package.json``. We use npm's own marker
    file ``node_modules/.package-lock.json`` as the canonical timestamp of
    the last install.
    """
    pkg = sidecar_dir / "package.json"
    lock = sidecar_dir / "package-lock.json"
    nm = sidecar_dir / "node_modules"
    marker = nm / ".package-lock.json"

    if not pkg.exists():
        return False, "package.json missing"
    if not lock.exists():
        return False, "package-lock.json missing"
    if not nm.exists():
        return False, "node_modules missing"
    if not marker.exists():
        return False, "node_modules/.package-lock.json missing (incomplete install)"
    marker_mtime = marker.stat().st_mtime
    if marker_mtime < lock.stat().st_mtime:
        return False, "node_modules is stale relative to package-lock.json"
    if marker_mtime < pkg.stat().st_mtime:
        return False, "node_modules is stale relative to package.json"
    return True, "fresh"


def ensure_sidecar_installed(sidecar_dir: Path, *, timeout_s: int = 600) -> None:
    """Run ``npm ci`` in ``sidecar_dir`` if dependencies are missing or stale.

    Raises :class:`SidecarBootstrapError` if ``npm ci`` exits non-zero or
    times out. Returns silently when the install is already fresh, or when
    npm is not available (a warning is logged in that case so the server can
    still boot for users who do not enable any sidecar channels).
    """
    fresh, reason = is_install_fresh(sidecar_dir)
    if fresh:
        logger.info(f"sidecar[{sidecar_dir.name}]: dependencies fresh — skipping npm ci")
        return

    npm = shutil.which("npm")
    if npm is None:
        logger.warning(
            f"sidecar[{sidecar_dir.name}]: {reason}, but `npm` is not on PATH. "
            "Install Node.js 18+ to enable sidecar channels.",
        )
        return

    if not (sidecar_dir / "package-lock.json").exists():
        logger.warning(
            f"sidecar[{sidecar_dir.name}]: package-lock.json missing; "
            f"run `npm install` once in {sidecar_dir} to generate it.",
        )
        return

    logger.info(f"sidecar[{sidecar_dir.name}]: {reason} — running `npm ci`")
    started = time.monotonic()
    proc = subprocess.Popen(
        [npm, "ci"],
        cwd=str(sidecar_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            logger.info(f"npm[{sidecar_dir.name}]: {line}")
    try:
        rc = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.wait()
        logger.error(f"[sidecar:{sidecar_dir.name}] npm ci timed out after {timeout_s}s")
        raise SidecarBootstrapError(
            f"`npm ci` timed out after {timeout_s}s for sidecar {sidecar_dir.name}",
        ) from exc

    if rc != 0:
        logger.error(f"[sidecar:{sidecar_dir.name}] npm ci failed rc={rc}")
        raise SidecarBootstrapError(
            f"`npm ci` failed for sidecar {sidecar_dir.name} (exit code {rc})",
        )

    elapsed = time.monotonic() - started
    logger.info(f"sidecar[{sidecar_dir.name}]: npm ci completed in {elapsed:.1f}s")


def ensure_all_sidecars_installed(*, timeout_s: int = 600) -> None:
    """Verify and (re)install every discovered sidecar's ``node_modules``.

    Called once during server startup, before any channels are enabled.
    """
    sidecars = discover_sidecars()
    if not sidecars:
        return
    logger.info(f"Bootstrapping {len(sidecars)} channel sidecar(s)…")
    for sidecar_dir in sidecars:
        ensure_sidecar_installed(sidecar_dir, timeout_s=timeout_s)
