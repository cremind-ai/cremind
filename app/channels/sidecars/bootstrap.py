"""Bootstrap for Node-based channel sidecars.

Each sibling directory under ``app/channels/sidecars/`` that contains a
``package.json`` is treated as a sidecar. At server startup we verify the
sidecar's ``node_modules`` is present and matches the committed
``package-lock.json``; if anything is off we run ``npm ci`` synchronously so
the server never reaches a "ready" state with broken sidecar dependencies.

The lockfiles *are* tracked in git (root ``.gitignore`` un-ignores
``app/channels/sidecars/*/package-lock.json``) and therefore ship inside the
wheel, which is what makes the reproducible ``npm ci`` path possible on a
fresh install. Releases before that fix shipped without them, so when the
lockfile is absent we fall back to a plain ``npm install`` — that heals an
older install in place (e.g. a Kubernetes venv PVC carrying a pre-fix wheel)
instead of leaving the channel permanently unstartable. ``node`` itself never
reads the lockfile; it resolves imports out of ``node_modules`` alone.

Startup is best-effort: a failing install (registry unreachable, offline
host) is logged and skipped rather than aborting boot, because a broken
sidecar should only cost you that channel. Enabling the channel retries the
install through :func:`ensure_sidecar_ready`, so a transient failure heals
without a restart.

The same freshness check is reused by channel adapters as a runtime guard
(in case ``node_modules`` is removed while the server is running).

Note on scope: ``node_modules`` lives next to the sidecar source inside the
installed package, so it is system-wide process state shared by every
profile — the same as the Python virtualenv itself. Per-profile sidecar state
(sessions, auth) is separate and keyed by the ``--profile`` argument the
adapters pass when they spawn the sidecar.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import threading
import time
from pathlib import Path

from app.channels.exceptions import ChannelNotImplemented
from app.utils.logger import logger

SIDECARS_ROOT = Path(__file__).resolve().parent

# One lock per sidecar directory, so a boot-time install and an enable-time
# heal of the same sidecar can never run npm concurrently in one directory.
_INSTALL_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class SidecarBootstrapError(RuntimeError):
    """Raised when a sidecar's dependencies cannot be installed."""


def _lock_for(sidecar_dir: Path) -> threading.Lock:
    key = str(sidecar_dir)
    with _LOCKS_GUARD:
        lock = _INSTALL_LOCKS.get(key)
        if lock is None:
            lock = _INSTALL_LOCKS[key] = threading.Lock()
        return lock


def discover_sidecars() -> list[Path]:
    """Every directory under ``sidecars/`` containing a ``package.json``."""
    return sorted(p.parent for p in SIDECARS_ROOT.glob("*/package.json"))


def is_install_fresh(sidecar_dir: Path) -> tuple[bool, str]:
    """Return ``(fresh?, reason)`` for ``sidecar_dir``.

    "Fresh" means ``node_modules`` exists and was last installed against the
    current ``package-lock.json`` / ``package.json``. We use npm's own marker
    file ``node_modules/.package-lock.json`` as the canonical timestamp of
    the last install.

    A complete install with no ``package-lock.json`` counts as fresh: the
    lockfile is an input to ``npm ci``, never something node reads at run
    time. That keeps an install healed by the ``npm install`` fallback from
    being re-installed on every boot. Once a lockfile does appear (a wheel
    upgrade writes one with a current mtime), it lands newer than the marker
    and the staleness check below converges the tree onto ``npm ci``.
    """
    pkg = sidecar_dir / "package.json"
    lock = sidecar_dir / "package-lock.json"
    nm = sidecar_dir / "node_modules"
    marker = nm / ".package-lock.json"

    if not pkg.exists():
        return False, "package.json missing"
    if not nm.exists():
        return False, "node_modules missing"
    if not marker.exists():
        return False, "node_modules/.package-lock.json missing (incomplete install)"
    marker_mtime = marker.stat().st_mtime
    if lock.exists() and marker_mtime < lock.stat().st_mtime:
        return False, "node_modules is stale relative to package-lock.json"
    if marker_mtime < pkg.stat().st_mtime:
        return False, "node_modules is stale relative to package.json"
    return True, "fresh"


def ensure_sidecar_installed(sidecar_dir: Path, *, timeout_s: int = 600) -> None:
    """Install ``sidecar_dir``'s dependencies if they are missing or stale.

    Uses ``npm ci`` when a ``package-lock.json`` is present, and falls back to
    ``npm install`` when it is not (see the module docstring).

    Raises :class:`SidecarBootstrapError` if npm exits non-zero or times out.
    Returns silently when the install is already fresh, or when npm is not
    available (a warning is logged in that case so the server can still boot
    for users who do not enable any sidecar channels).
    """
    with _lock_for(sidecar_dir):
        fresh, reason = is_install_fresh(sidecar_dir)
        if fresh:
            logger.info(f"sidecar[{sidecar_dir.name}]: dependencies fresh — skipping install")
            return

        npm = shutil.which("npm")
        if npm is None:
            logger.warning(
                f"sidecar[{sidecar_dir.name}]: {reason}, but `npm` is not on PATH. "
                "Install Node.js 20+ to enable sidecar channels.",
            )
            return

        if (sidecar_dir / "package-lock.json").exists():
            cmd = [npm, "ci"]
        else:
            # Pre-fix wheels shipped without the lockfile. `npm ci` refuses to
            # run without one, so resolve from package.json ranges instead.
            logger.warning(
                f"sidecar[{sidecar_dir.name}]: package-lock.json missing "
                "(install predates the lockfile shipping in the wheel) — "
                "falling back to a non-reproducible `npm install`.",
            )
            cmd = [npm, "install", "--no-audit", "--no-fund"]

        logger.info(f"sidecar[{sidecar_dir.name}]: {reason} — running `{' '.join(cmd[1:])}`")
        started = time.monotonic()
        proc = subprocess.Popen(
            cmd,
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
            logger.error(f"[sidecar:{sidecar_dir.name}] npm timed out after {timeout_s}s")
            raise SidecarBootstrapError(
                f"npm timed out after {timeout_s}s for sidecar {sidecar_dir.name}",
            ) from exc

        if rc != 0:
            logger.error(f"[sidecar:{sidecar_dir.name}] npm failed rc={rc}")
            raise SidecarBootstrapError(
                f"npm failed for sidecar {sidecar_dir.name} (exit code {rc})",
            )

        elapsed = time.monotonic() - started
        logger.info(f"sidecar[{sidecar_dir.name}]: npm completed in {elapsed:.1f}s")


def ensure_all_sidecars_installed(*, timeout_s: int = 600) -> None:
    """Verify and (re)install every discovered sidecar's ``node_modules``.

    Called once during server startup, before any channels are enabled. A
    sidecar that fails to install is logged and skipped — boot must not die
    because npm could not reach the registry. Enabling the channel retries.
    """
    sidecars = discover_sidecars()
    if not sidecars:
        return
    logger.info(f"Bootstrapping {len(sidecars)} channel sidecar(s)…")
    for sidecar_dir in sidecars:
        try:
            ensure_sidecar_installed(sidecar_dir, timeout_s=timeout_s)
        except SidecarBootstrapError as exc:
            logger.error(
                f"sidecar[{sidecar_dir.name}]: bootstrap failed — {exc}. "
                "The channel will retry the install when it is enabled.",
            )


async def ensure_sidecar_ready(
    sidecar_dir: Path,
    *,
    label: str,
    node_hint: str = "Node 20+",
    timeout_s: int = 600,
) -> None:
    """Adapter-side prerequisite guard: heal the install, then verify it.

    Shared by every sidecar-backed adapter. Runs the (blocking) npm install
    on a worker thread so enabling a channel never stalls the event loop, and
    raises :class:`ChannelNotImplemented` with remediation that is true for
    the specific thing that is wrong.
    """
    if shutil.which("node") is None:
        raise ChannelNotImplemented(
            f"Node.js is not installed or not on PATH. Install {node_hint} "
            f"to use the {label} channel.",
        )
    index = sidecar_dir / "index.js"
    if not index.exists():
        raise ChannelNotImplemented(f"{label} sidecar source missing: {index}")

    if is_install_fresh(sidecar_dir)[0]:
        return

    try:
        await asyncio.to_thread(ensure_sidecar_installed, sidecar_dir, timeout_s=timeout_s)
    except SidecarBootstrapError as exc:
        raise ChannelNotImplemented(
            f"Installing {label} sidecar dependencies failed: {exc}. Check that the "
            "server can reach registry.npmjs.org, then re-enable the channel to retry.",
        ) from exc

    fresh, reason = is_install_fresh(sidecar_dir)
    if fresh:
        return

    # The install was a no-op or left the tree incomplete. Missing npm is the
    # only path that returns without installing anything.
    if shutil.which("npm") is None:
        raise ChannelNotImplemented(
            f"{label} sidecar dependencies are not ready ({reason}) and `npm` is not on "
            f"PATH, so they cannot be installed automatically. Install {node_hint} with "
            f"npm in the server environment, or run `npm install` in {sidecar_dir}, then "
            "re-enable the channel.",
        )
    raise ChannelNotImplemented(
        f"{label} sidecar dependencies are still not ready after installing ({reason}). "
        f"Run `npm install` in {sidecar_dir}, then re-enable the channel.",
    )
