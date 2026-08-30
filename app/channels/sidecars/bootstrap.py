"""Bootstrap for Node-based channel sidecars.

Each sibling directory under ``app/channels/sidecars/`` that contains a
``package.json`` is treated as a sidecar. We verify the sidecar's
``node_modules`` is present and matches the committed ``package-lock.json``,
and run ``npm ci`` when it is not.

That happens in two places, and neither is on the critical path to the socket
bind. :func:`start_background_bootstrap` warms the trees on a daemon thread at
startup, and :func:`ensure_sidecar_ready` heals on demand when an adapter's
channel is actually enabled. The second is the one that carries the
correctness guarantee — the first is only there so the common case is already
paid for by the time someone enables a channel. A shared per-directory lock
keeps the two from running npm concurrently in one sidecar.

The lockfiles *are* tracked in git (root ``.gitignore`` un-ignores
``app/channels/sidecars/*/package-lock.json``) and therefore ship inside the
wheel, which is what makes the reproducible ``npm ci`` path possible on a
fresh install. Releases before that fix shipped without them, so when the
lockfile is absent we fall back to a plain ``npm install`` — that heals an
older install in place (e.g. a Kubernetes venv PVC carrying a pre-fix wheel)
instead of leaving the channel permanently unstartable. ``node`` itself never
reads the lockfile; it resolves imports out of ``node_modules`` alone.

Bootstrap is best-effort: a failing install (registry unreachable, offline
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
import json
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

    "Fresh" means ``node_modules`` exists and holds what the current
    ``package-lock.json`` asks for. npm writes ``node_modules/.package-lock.json``
    describing the tree it just built, so the two are directly comparable.

    We compare *content*, not mtimes. pip rewrites every file it installs with
    a fresh mtime and does no content-hash skip, so an mtime rule calls the
    tree stale after each release upgrade and re-downloads ~66MB even when the
    dependency trees are byte-identical — for users who never enable a sidecar
    channel at all.

    A complete install with no ``package-lock.json`` counts as fresh: the
    lockfile is an input to ``npm ci``, never something node reads at run
    time. That keeps an install healed by the ``npm install`` fallback from
    being re-installed on every boot.
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
    if not lock.exists():
        return True, "fresh"
    return _installed_matches_lock(lock, marker)


def _installed_matches_lock(lock: Path, marker: Path) -> tuple[bool, str]:
    """Compare npm's install marker against the lockfile it should satisfy.

    The marker is a *subset* of the lockfile: npm records only what it
    actually installed, so every entry gated to another platform (the
    ``@img/sharp-*`` binaries, say) is absent by design. Hence the two
    directions differ — everything installed must still be in the lockfile at
    the same version, and everything the lockfile requires unconditionally
    must be installed.
    """
    try:
        locked = json.loads(lock.read_text(encoding="utf-8")).get("packages") or {}
        installed = json.loads(marker.read_text(encoding="utf-8")).get("packages") or {}
    except (OSError, ValueError) as exc:
        # An unreadable or malformed pair is not something we can reason
        # about; reinstalling is the cheap, always-correct answer.
        return False, f"could not compare node_modules against package-lock.json ({exc})"

    for path, entry in installed.items():
        want = locked.get(path)
        if want is None:
            return False, f"{path} is installed but no longer in package-lock.json"
        if entry.get("version") != want.get("version"):
            return (
                False,
                f"{path} is installed at {entry.get('version')}, "
                f"package-lock.json wants {want.get('version')}",
            )
        if entry.get("resolved") != want.get("resolved"):
            return False, f"{path} resolves to a different artifact than package-lock.json records"

    for path, entry in locked.items():
        # "" is the root project itself, never a node_modules entry. Optional
        # deps are the platform-gated binaries npm legitimately skips.
        if not path or entry.get("optional") or entry.get("dev"):
            continue
        if path not in installed:
            return False, f"{path} is in package-lock.json but not installed"

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

    A sidecar that fails to install is logged and skipped — boot must not die
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


def start_background_bootstrap(*, timeout_s: int = 600) -> threading.Thread | None:
    """Warm every sidecar's ``node_modules`` without delaying the listener.

    A cold ``npm ci`` for both sidecars is ~66MB and tens of seconds. Doing it
    inline at startup pushed the socket bind out past the window the
    installers wait for ``/health`` (10x1s in ``install/install.sh`` and
    ``install.ps1``), so a fresh native install auto-opened the browser onto a
    connection-refused page with the only clue buried in the log.

    Correctness does not depend on this finishing, or even running: an adapter
    calls :func:`ensure_sidecar_ready` when its channel is enabled, which
    heals the tree on demand and reports what went wrong. This is a warm-up so
    that the common case — enabling a channel later — is already paid for.
    Both paths take the same per-directory lock, so they cannot race npm.

    Returns the thread (for tests) or ``None`` when there is nothing to do.
    """
    sidecars = discover_sidecars()
    if not sidecars:
        return None

    stale = [d for d in sidecars if not is_install_fresh(d)[0]]
    if not stale:
        logger.info("sidecar: dependencies fresh — nothing to bootstrap")
        return None

    names = ", ".join(d.name for d in stale)
    logger.info(f"sidecar: warming {names} in the background; startup continues")
    thread = threading.Thread(
        target=ensure_all_sidecars_installed,
        kwargs={"timeout_s": timeout_s},
        name="sidecar-bootstrap",
        daemon=True,
    )
    thread.start()
    return thread


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
