"""Upgrade orchestration — the backup → install → migrate → health flow.

Two entry points:

- :func:`apply` is what the ``cremind upgrade`` CLI runs. It walks the
  whole flow synchronously, emitting ``UpgradeEvent`` records via the
  callback so the caller can render progress.
- :func:`acquire_lock_or_recover` runs at every server boot. If the
  previous run died mid-upgrade, the lock file directs us to restore
  from the captured backup before we serve any traffic — that's the
  recovery path B7 of the design plan calls for.

The flow is deliberately linear: each step is a small idempotent unit,
and on any failure we restore the backup and downgrade the package
before re-raising. That's a worse outcome than "the upgrade worked"
but a better one than "the install is in a broken state with no easy
way back."

Docker installs aren't covered here. The container itself is the unit
of versioning: ``docker compose pull && up -d`` is the upgrade, and
the migrations run on the next entrypoint pass via ``cremind db upgrade``
(see ``install/desktop/entrypoint.sh``).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.__version__ import (
    __version__ as CURRENT_VERSION,
)
from app.upgrade import manifest
from app.upgrade.channel import Channel, get_channel
from app.utils import logger


# Lock file lives alongside the working dir's other state. Its presence
# means an upgrade is in flight (or crashed mid-flight); the JSON inside
# captures enough state to roll back.
def _lock_path() -> Path:
    from app.config.settings import BaseConfig

    return Path(BaseConfig.CREMIND_SYSTEM_DIR) / ".upgrade.lock"


# ── progress events ──────────────────────────────────────────────────────


@dataclass
class UpgradeEvent:
    """One progress notification.

    ``kind`` is one of: ``check``, ``backup``, ``install``, ``migrate``,
    ``health``, ``rollback``, ``done``, ``error``. ``ok`` is False on
    rollback / error so renderers can flip into a red state. ``detail``
    is human-readable; ``meta`` carries machine-readable extras like the
    backup path so the rollback step can find it without a global var.
    """

    kind: str
    message: str
    ok: bool = True
    meta: dict | None = None


ProgressCallback = Callable[[UpgradeEvent], None]


def _emit(callback: ProgressCallback | None, event: UpgradeEvent) -> None:
    if callback:
        try:
            callback(event)
        except Exception:
            # Logging callbacks should never break the upgrade — swallow
            # and continue. The CLI prints to stdout anyway.
            pass


# ── public surface ────────────────────────────────────────────────────────


def check(callback: ProgressCallback | None = None) -> tuple[manifest.ReleaseInfo | None, str]:
    """Resolve the latest release and decide whether to upgrade.

    Returns ``(release_or_None, status)`` where status is one of:
      - ``up_to_date``: we're already at or above the latest tag.
      - ``available``: a newer version exists and we can upgrade to it.
      - ``too_old``: the latest release refuses to migrate from us.
      - ``unreachable``: the GitHub API call failed.
    """
    channel = get_channel()
    _emit(
        callback,
        UpgradeEvent(
            "check",
            f"Looking up latest release on {channel} channel...",
        ),
    )
    try:
        release = manifest.fetch_latest(channel=channel)
    except urllib.error.URLError as e:
        _emit(callback, UpgradeEvent("check", f"Couldn't reach GitHub: {e}", ok=False))
        return None, "unreachable"
    except Exception as e:  # noqa: BLE001
        _emit(callback, UpgradeEvent("check", f"Manifest lookup failed: {e}", ok=False))
        return None, "unreachable"

    if not manifest.is_newer(release.version, CURRENT_VERSION):
        _emit(
            callback,
            UpgradeEvent(
                "check",
                f"Up to date (current {CURRENT_VERSION}, latest {release.version}).",
            ),
        )
        return release, "up_to_date"

    # The new release's ``min_supported_upgrade_from`` declares whether
    # it knows how to migrate from us. ``manifest.is_at_or_above``
    # compares CURRENT >= floor. If we're below the floor, this build
    # can't take us forward and the user needs the legacy export tool.
    if not manifest.is_at_or_above(CURRENT_VERSION, release.min_supported_upgrade_from):
        _emit(
            callback,
            UpgradeEvent(
                "check",
                (
                    f"Latest is {release.version} but it requires at least "
                    f"{release.min_supported_upgrade_from} to upgrade in place; "
                    f"this install is {CURRENT_VERSION}. See the release notes."
                ),
                ok=False,
            ),
        )
        return release, "too_old"

    _emit(
        callback,
        UpgradeEvent(
            "check",
            f"Update available: {CURRENT_VERSION} → {release.version}.",
            meta={"current": CURRENT_VERSION, "target": release.version},
        ),
    )
    return release, "available"


def apply(
    target_version: str | None = None,
    *,
    callback: ProgressCallback | None = None,
    confirm: Callable[[manifest.ReleaseInfo], bool] | None = None,
) -> bool:
    """Run the full upgrade flow. Returns ``True`` on success.

    If ``target_version`` is None we resolve the latest release first.
    If ``confirm`` is provided, it's called after the version check and
    before any state-mutating step — a no for the CLI's interactive
    prompt cleanly aborts before we touch anything.

    On the **test** channel a ``target_version`` may name *any* published
    RC — including one older than the current install — so testers can
    switch between PR release candidates. That path bypasses the
    "must be newer" gate (see :func:`_apply_target`). On production / dev a
    ``target_version`` must still equal the latest (the historical guard).
    """
    if target_version and get_channel() == "test":
        return _apply_target(target_version, callback=callback, confirm=confirm)

    release, status = check(callback)
    if status == "unreachable" or release is None:
        return False
    if status == "up_to_date":
        return True
    if status == "too_old":
        return False

    if target_version and target_version != release.version:
        _emit(
            callback,
            UpgradeEvent(
                "check",
                f"Requested target {target_version} differs from latest {release.version}; " "skipping upgrade.",
                ok=False,
            ),
        )
        return False

    if confirm and not confirm(release):
        _emit(callback, UpgradeEvent("check", "Upgrade cancelled by user.", ok=False))
        return False

    return _apply_locked(release, callback)


def _apply_target(
    target_version: str,
    *,
    callback: ProgressCallback | None = None,
    confirm: Callable[[manifest.ReleaseInfo], bool] | None = None,
) -> bool:
    """Install a specific test-channel release (may be older than current).

    Unlike the latest-based flow this does NOT gate on ``is_newer`` — the
    whole point is letting a tester jump to a chosen PR's RC even when a
    newer PR's RC exists. It also does NOT hard-reject when the target's
    ``min_supported_upgrade_from`` is above the current install: a
    maintainer who explicitly picked an older RC from the Updates page is
    opting in to testing that combination, and the backup-first /
    rollback-on-failure path inside :func:`_apply_locked` is the actual
    safety guarantee. The floor mismatch is surfaced as a ``WARNING:``
    event before the install proceeds, so it's still visible.
    Operations performed:
      - resolves the exact release (404 → fail),
      - no-ops if we're already on it,
      - warns (but does not refuse) when the target's floor is above
        the current install,
      - runs under the same backup / rollback lock as a normal upgrade.
    """
    _emit(callback, UpgradeEvent("check", f"Resolving test release {target_version}…"))
    try:
        release = manifest.resolve_release(target_version, channel="test")
    except Exception as e:
        _emit(callback, UpgradeEvent("check", f"Couldn't resolve {target_version}: {e}", ok=False))
        return False

    if release.version == CURRENT_VERSION:
        _emit(callback, UpgradeEvent("check", f"Already on {release.version}; nothing to do."))
        return True

    if not manifest.is_at_or_above(CURRENT_VERSION, release.min_supported_upgrade_from):
        _emit(
            callback,
            UpgradeEvent(
                "check",
                (
                    f"WARNING: {release.version} declares "
                    f"min_supported_upgrade_from={release.min_supported_upgrade_from} "
                    f"but this install is {CURRENT_VERSION}. Migration may fail; "
                    "a backup will be taken and the install will be rolled back "
                    "automatically if the new build can't read the live database."
                ),
            ),
        )

    _emit(
        callback,
        UpgradeEvent(
            "check",
            f"Switching test build: {CURRENT_VERSION} → {release.version}.",
            meta={"current": CURRENT_VERSION, "target": release.version},
        ),
    )

    if confirm and not confirm(release):
        _emit(callback, UpgradeEvent("check", "Upgrade cancelled by user.", ok=False))
        return False

    return _apply_locked(release, callback)


def acquire_lock_or_recover(callback: ProgressCallback | None = None) -> None:
    """Boot-time recovery hook. Restores from backup if a lock file exists.

    Call this from ``app/server.py`` before the migration runs (or from
    a CLI ``cremind db check-lock`` for ops). Idempotent — no-op when no
    lock is present.
    """
    lock = _lock_path()
    if not lock.is_file():
        return

    try:
        state = json.loads(lock.read_text(encoding="utf-8"))
    except Exception:
        # Corrupt lock file is treated as "in flight, can't recover" —
        # the safest action is to clear it and continue, since we don't
        # know what state we're in. Prefer manual intervention over
        # a wrong automatic decision.
        lock.unlink(missing_ok=True)
        _emit(
            callback,
            UpgradeEvent(
                "rollback",
                "Found an unreadable upgrade lock; cleared it. Inspect the install if "
                "you didn't expect an upgrade to be in progress.",
                ok=False,
            ),
        )
        return

    backup_path = state.get("backup_path")
    previous_version = state.get("previous_version")
    # Older lock files (from before channel-aware upgrades shipped) won't
    # carry a ``channel`` key; default to production for those, since
    # any host that ran a prior upgrade was a prod host by definition.
    recovery_channel: Channel = "test" if state.get("channel") == "test" else "production"
    _emit(
        callback,
        UpgradeEvent(
            "rollback",
            f"Detected interrupted upgrade (was at {previous_version}); restoring backup.",
            ok=False,
            meta=state,
        ),
    )
    try:
        if backup_path and Path(backup_path).is_file():
            from app.storage.backup import restore as _restore

            _restore(Path(backup_path))
        if previous_version:
            _pip_install(
                f"cremind=={previous_version}",
                callback,
                channel=recovery_channel,
            )
    finally:
        lock.unlink(missing_ok=True)


# ── internals ─────────────────────────────────────────────────────────────


def _apply_locked(release: manifest.ReleaseInfo, callback: ProgressCallback | None) -> bool:
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.is_file():
        _emit(
            callback,
            UpgradeEvent(
                "check",
                "An upgrade lock already exists at " f"{lock} — refusing to start a second upgrade.",
                ok=False,
            ),
        )
        return False

    backup_path: Path | None = None
    state = {
        "started_at": time.time(),
        "previous_version": CURRENT_VERSION,
        "target_version": release.version,
        "backup_path": None,
        # Persist the channel so a crash-recovery rollback uses the
        # right pip index. Without this, a test-install rollback would
        # try to find ``cremind==0.2.1rc1`` on prod PyPI and fail.
        "channel": release.channel,
    }

    def _persist_state() -> None:
        # Best-effort: a missing or unreadable lock will be caught by the
        # next acquire_lock_or_recover. We don't want a tempfile bug to
        # be the thing that derails an otherwise-successful upgrade.
        try:
            lock.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception:
            pass

    _persist_state()

    try:
        # 1. Pre-flight: free disk for the backup. We err on the side of
        # being conservative with the requirement (200 MB headroom);
        # bumping it later is cheap.
        _emit(callback, UpgradeEvent("check", "Pre-flight checks..."))
        _check_disk_space(min_free_mb=200)

        # 2. Backup.
        _emit(callback, UpgradeEvent("backup", "Snapshotting database..."))
        from app.storage.backup import backup as _backup

        backup_path = _backup()
        state["backup_path"] = str(backup_path)
        _persist_state()
        _emit(
            callback,
            UpgradeEvent(
                "backup",
                f"Backup written to {backup_path}",
                meta={"path": str(backup_path)},
            ),
        )

        # 3. Install.
        # Dev channel: skip pip entirely. The running install is a
        # working copy / editable install, so pinning to a synthetic
        # ``cremind==X.Y.Z+devforced`` would just fail (no such wheel on
        # PyPI) and a no-pin ``pip install --upgrade cremind`` could
        # clobber the editable install with whatever's published. The
        # rest of the flow (migrate / health / restart) still runs so
        # the in-app updater UI is fully exercisable on dev.
        if release.channel == "dev":
            _emit(
                callback,
                UpgradeEvent(
                    "install",
                    "Dev channel: pip install skipped (editable install is the source of truth).",
                ),
            )
        else:
            spec = _pip_spec_for(release)
            _emit(callback, UpgradeEvent("install", f"pip install {spec}"))
            _pip_install(spec, callback, channel=release.channel)

        # 4. Migrate. We shell out to a fresh ``cremind db upgrade`` rather
        # than calling the in-process migration helper because pip just
        # replaced our own source files on disk — Python's import cache
        # still holds the old modules in this process. A subprocess gets
        # a clean import and runs the new release's Alembic chain.
        _emit(callback, UpgradeEvent("migrate", "cremind db upgrade"))
        _run(
            [sys.executable, "-m", "app.cli.main", "db", "upgrade"],
            callback,
            prefer="cremind db upgrade",
        )

        # 5. Health gate. We hit /health from outside the running
        # process (the upgrade may have restarted the service) so a
        # mismatched build can't fool us.
        _emit(callback, UpgradeEvent("health", "Probing /health..."))
        if not _wait_for_health(timeout_s=60):
            raise RuntimeError("Post-upgrade health check failed.")

        _emit(
            callback,
            UpgradeEvent(
                "done",
                f"Upgraded to {release.version}.",
                meta={"version": release.version},
            ),
        )
        return True

    except Exception as e:
        _emit(
            callback,
            UpgradeEvent(
                "rollback",
                f"Upgrade failed ({e}); restoring backup.",
                ok=False,
            ),
        )
        try:
            if backup_path and Path(backup_path).is_file():
                from app.storage.backup import restore as _restore

                _restore(backup_path)
            # Mirror the forward path: on dev, the previous version IS
            # the editable install we never replaced, so there's nothing
            # to pip-downgrade to. The backup restore (above) is the
            # data-protection part; that we keep.
            if release.channel != "dev":
                _pip_install(
                    f"cremind=={CURRENT_VERSION}",
                    callback,
                    channel=release.channel,
                )
        except Exception as e2:  # noqa: BLE001
            _emit(
                callback,
                UpgradeEvent(
                    "rollback",
                    f"Rollback ALSO failed: {e2}. Manual intervention required.",
                    ok=False,
                ),
            )
        return False
    finally:
        # Always clear the lock — either the upgrade succeeded (no
        # rollback needed on next boot) or we already rolled back.
        with contextlib.suppress(Exception):
            lock.unlink(missing_ok=True)


def _check_disk_space(*, min_free_mb: int) -> None:
    from app.config.settings import BaseConfig

    target = Path(BaseConfig.CREMIND_SYSTEM_DIR)
    target.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(target)
    free_mb = usage.free // (1024 * 1024)
    if free_mb < min_free_mb:
        raise RuntimeError(
            f"Need at least {min_free_mb} MB free for the backup; " f"only {free_mb} MB available at {target}."
        )


# Defaults for the test installer's pip indexes. The installer also
# writes these to ``~/.cremind/.env`` so they reach this process via the
# .env loader at server start; the literals are the fallback for hosts
# installed before the .env keys were added.
_TEST_PYPI_INDEX_URL = "https://test.pypi.org/simple/"
_TEST_PYPI_EXTRA_INDEX_URL = "https://pypi.org/simple/"


def _pip_spec_for(release: manifest.ReleaseInfo) -> str:
    """Build the pip requirement spec for ``release``.

    For prod, the GitHub tag and the PyPI version match (``0.2.1``).
    For test, the GitHub tag is ``v0.2.1rc3.dev2`` and the wheel on
    Test PyPI is named ``0.2.1rc3.dev2`` — i.e. the tag with the
    leading ``v`` stripped. ``release.version`` already carries the
    PEP 440 form because :func:`manifest._parse_release` stripped it.
    """
    return f"cremind=={release.version}"


def _have_pip() -> bool:
    """Return True if ``sys.executable -m pip --version`` works.

    Native installs use ``python -m venv`` which always bundles pip, so
    this is True in production. ``uv``-managed development venvs (created
    by ``uv sync`` / ``uv run``) skip pip by default; for those we fall
    back to ``uv pip install``.
    """
    try:
        rc = subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
    except (FileNotFoundError, OSError):
        return False
    return rc == 0


def _pip_install(
    spec: str,
    callback: ProgressCallback | None,
    *,
    channel: Channel,
    upgrade: bool = True,
) -> None:
    """Run ``pip install [--upgrade] <spec>`` against the current interpreter.

    Uses ``sys.executable -m pip`` when pip is available in this venv;
    otherwise falls back to ``uv pip install --python <sys.executable>``
    so development venvs created by ``uv sync`` (which omits pip) keep
    working. Either way the install targets the live process's venv.

    ``--upgrade`` (default for the upgrade runner) lets us downgrade as
    well as upgrade, which is what the rollback path needs.

    The feature installer passes ``upgrade=False`` because adding an
    extras group should NOT touch the already-installed cremind wheel —
    otherwise an editable dev install would get clobbered by a PyPI
    download whenever the user enables a feature.

    On the test channel we add ``--pre`` (the test wheels are PEP 440
    pre-releases that pip otherwise refuses) plus the Test PyPI index
    URLs the installer recorded. We also force ``PIP_CACHE_DIR`` into
    the subprocess env: a long-running server may have started before
    the installer set it, so inheriting from ``os.environ`` isn't
    enough.

    Reused by :mod:`app.features.installer` to install opt-in extras
    groups picked in the Setup Wizard (e.g. ``cremind[embeddings-me5]``).
    """
    if _have_pip():
        cmd: list[str] = [sys.executable, "-m", "pip", "install"]
        if upgrade:
            cmd.append("--upgrade")
        installer = "pip"
    else:
        uv_path = shutil.which("uv")
        if uv_path is None:
            raise RuntimeError(
                "Neither pip nor uv is available in this environment. "
                "Re-create the venv with `python -m venv .venv` (which "
                "bundles pip), or install uv (https://docs.astral.sh/uv/).",
            )
        cmd = [uv_path, "pip", "install", "--python", sys.executable]
        if upgrade:
            cmd.append("--upgrade")
        installer = "uv pip"

    env = os.environ.copy()
    # Scope cache to the Install Dir for both channels — same reason
    # the installers do (avoid a stale ~/.cache/pip pinning an old
    # wheel after a reinstall). ``UV_CACHE_DIR`` covers the uv fallback;
    # uv ignores ``PIP_CACHE_DIR`` so we set both to be explicit.
    from app.config.settings import BaseConfig

    cache_dir = str(Path(BaseConfig.CREMIND_INSTALL_DIR) / "pip-cache")
    env["PIP_CACHE_DIR"] = cache_dir
    env["UV_CACHE_DIR"] = cache_dir

    if channel == "test":
        index_url = os.environ.get("CREMIND_PIP_INDEX_URL") or _TEST_PYPI_INDEX_URL
        extra_index_url = os.environ.get("CREMIND_PIP_EXTRA_INDEX_URL") or _TEST_PYPI_EXTRA_INDEX_URL
        if installer == "pip":
            cmd += [
                "--pre",
                "--index-url",
                index_url,
                "--extra-index-url",
                extra_index_url,
            ]
        else:
            # uv pip accepts the same flags but spells some of them
            # differently (``--prerelease=allow`` for ``--pre``); use
            # the env-var form so the syntax stays uniform.
            cmd += [
                "--prerelease",
                "allow",
                "--index-url",
                index_url,
                "--extra-index-url",
                extra_index_url,
            ]

    cmd.append(spec)
    _run(cmd, callback, env=env)


def _run(
    cmd: list[str],
    callback: ProgressCallback | None,
    *,
    ignore_failure: bool = False,
    prefer: str | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Spawn ``cmd``, stream output through the callback, raise on failure.

    ``prefer`` lets a step name a more user-friendly equivalent in
    error messages (e.g., ``"cremind db upgrade"`` instead of the raw
    Alembic invocation). Cosmetic only.

    Every invocation tees the subprocess's combined stdout/stderr to
    ``$CREMIND_INSTALL_DIR/upgrade.log`` (best-effort; a log-open failure
    does not block the install). The 20-line tail in :class:`RuntimeError` is
    deliberately small so it surfaces in API responses without
    swamping them, but it's useless for a pip install that prints
    thousands of "Collecting" lines before the actual error — the log
    file is where you go for the full output. The error message
    includes the log path so the user knows where to look.
    """
    label = prefer or " ".join(cmd)
    log_path, log_file = _open_run_log(label)
    _emit(callback, UpgradeEvent("install", f"$ {label}"))
    logger.info(f"[subprocess:upgrade] launching: {label}")
    _started = time.monotonic()
    try:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
        except FileNotFoundError as e:
            if log_file is not None:
                with contextlib.suppress(OSError):
                    log_file.write(f"FileNotFoundError: {e}\n")
            logger.error(f"[subprocess:upgrade] {label}: executable not found ({e})")
            if ignore_failure:
                return
            raise RuntimeError(f"{label}: {e}") from e

        assert proc.stdout is not None
        tail: list[str] = []
        for line in proc.stdout:
            clean = line.rstrip("\n")
            _emit(callback, UpgradeEvent("install", clean))
            if log_file is not None:
                with contextlib.suppress(OSError):
                    log_file.write(clean + "\n")
            tail.append(clean)
            if len(tail) > 20:
                tail.pop(0)
        rc = proc.wait()
    finally:
        if log_file is not None:
            with contextlib.suppress(OSError):
                log_file.flush()
                log_file.close()

    _elapsed = time.monotonic() - _started
    if rc != 0 and not ignore_failure:
        logger.error(f"[subprocess:upgrade] {label} exited rc={rc} elapsed={_elapsed:.1f}s")
        suffix = "\n".join(tail).strip()
        log_hint = f"\n(full output: {log_path})" if log_path is not None else ""
        if suffix:
            raise RuntimeError(f"{label} exited with code {rc}.\n{suffix}{log_hint}")
        raise RuntimeError(f"{label} exited with code {rc}.{log_hint}")
    logger.info(f"[subprocess:upgrade] {label} exited rc={rc} elapsed={_elapsed:.1f}s")


def _open_run_log(label: str):
    """Open ``$CREMIND_INSTALL_DIR/upgrade.log`` for appending and write a header.

    Returns ``(log_path, file_handle)`` on success or ``(None, None)`` if
    the log can't be opened — _run() treats logging as best-effort so a
    full disk or a permissions glitch can't block an install.

    Lives in ``CREMIND_INSTALL_DIR`` (alongside ``install.log``) rather
    than the System Directory: it is a transcript of installer/upgrader
    operations, not user data, and pinning it to the install location
    keeps a user-chosen ``CREMIND_SYSTEM_DIR`` clean.

    Rotates when the file exceeds 5MB by truncating to the last 2MB, so
    a long-lived install doesn't accumulate unbounded pip output.
    """
    try:
        from app.config.settings import BaseConfig

        log_path = Path(BaseConfig.CREMIND_INSTALL_DIR) / "upgrade.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
            tail_bytes = log_path.read_bytes()[-2 * 1024 * 1024 :]
            log_path.write_bytes(tail_bytes)
        log_file = log_path.open("a", encoding="utf-8", errors="replace")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log_file.write(f"\n=== {ts} $ {label} ===\n")
        log_file.flush()
        return log_path, log_file
    except OSError:
        return None, None


def _wait_for_health(*, timeout_s: int) -> bool:
    """Poll /health on the configured backend until it returns 200 or we time out."""
    from app.config.settings import BaseConfig

    host = BaseConfig.HOST or "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    port = int(BaseConfig.PORT or 1112)
    url = f"http://{host}:{port}/health"

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if 200 <= resp.status < 300:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    return False


__all__ = [
    "ProgressCallback",
    "UpgradeEvent",
    "acquire_lock_or_recover",
    "apply",
    "check",
]
