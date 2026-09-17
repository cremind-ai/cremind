"""Feature installer — runs ``pip install`` for opt-in extras at runtime.

Used by the Setup Wizard endpoint (``POST /api/config/setup``) and the
post-setup Settings page (``POST /api/features/install``). Reuses the
upgrader's pip plumbing in :mod:`app.upgrade.runner` so we don't duplicate
channel-aware index handling, ``PIP_CACHE_DIR`` setup, or the
``sys.executable -m pip`` subprocess form.

The same call also *updates* a feature that imports fine but sits below the
version range its manifest entry declares
(:func:`app.features.manifest.is_outdated`). Nothing else ever re-syncs a
runtime venv, so without this an install that got an SDK under an older pin
would keep it forever. An update only takes effect after a restart — the old
modules may already be loaded — so :func:`restart_pending` remembers each
updated feature for the rest of the process's life.

Public surface:

- :func:`install_features` — install the union of extras groups needed by
  ``feature_keys``, update the outdated ones, and emit progress events.
- :func:`feature_status` — snapshot of every feature's install state, used
  by ``GET /api/features``.
- :func:`restart_pending` — whether a feature was updated in place and the
  server has not restarted since.
- :class:`InstallResult` and :class:`InstallEvent` — return / streaming
  payloads.
"""

from __future__ import annotations

import importlib
import os
import re
import sys
import threading
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable

from app.features.manifest import (
    FEATURES,
    is_installed,
    is_outdated,
    missing_features,
    outdated_features,
    pip_requirements,
    version_checks,
    version_report,
)
from app.upgrade.channel import get_channel
from app.upgrade.runner import (
    ProgressCallback,
    UpgradeEvent,
    _pip_install,
    _run,
)
from app.utils.logger import logger


@dataclass
class InstallEvent:
    """One progress notification streamed from :func:`install_features`.

    ``kind`` is one of: ``start``, ``log``, ``post_install``, ``done``,
    ``error``.
    """

    kind: str
    message: str
    ok: bool = True
    meta: dict = field(default_factory=dict)


EventCallback = Callable[[InstallEvent], None]


@dataclass
class InstallResult:
    """Outcome of one :func:`install_features` call.

    ``installed`` lists features that were missing and now import;
    ``upgraded`` lists features that were installed but outdated and are now
    inside their range. The two never overlap, and every ``upgraded`` entry
    implies ``restart_required``.
    """

    restart_required: bool
    installed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)
    error: str | None = None
    upgraded: list[str] = field(default_factory=list)


# ── restart-pending state ───────────────────────────────────────────────────

# Features updated in place since this process started. A same-process
# re-import does not swap modules that are already loaded, so the old SDK
# would keep driving the new binary until a restart; the tool's loader asks
# :func:`restart_pending` and refuses instead. System-wide on purpose: the
# venv is shared by every profile, so one profile's update is everyone's
# pending restart. The installer writes it from a worker thread while the
# event loop reads it, hence the lock. Only the restart itself clears it.
_restart_pending: set[str] = set()
_restart_pending_lock = threading.Lock()


def restart_pending(feature_key: str) -> bool:
    """Return True if ``feature_key`` was updated and the server hasn't restarted."""
    with _restart_pending_lock:
        return feature_key in _restart_pending


def _mark_restart_pending(feature_keys: Iterable[str]) -> None:
    with _restart_pending_lock:
        _restart_pending.update(feature_keys)


# ── Windows file locks ──────────────────────────────────────────────────────

# How pip reports that Windows refused to replace a file some process still
# holds open — on an update, typically the old version's running binary.
# ``WinError 5`` is "Access is denied", ``WinError 32`` is "being used by
# another process"; pip prints either form depending on where it failed.
_WINDOWS_FILE_LOCK_RE = re.compile(
    r"winerror\s+(?:5|32)\b|access is denied|being used by another process",
    re.IGNORECASE,
)


def install_features(
    feature_keys: list[str],
    emit: EventCallback | None = None,
) -> InstallResult:
    """Install the deps required by ``feature_keys`` (if missing), and update
    the ones that are installed but outdated.

    Builds one ``pip install`` invocation from
    :func:`app.features.manifest.pip_requirements`: the
    ``cremind[a,b,c]==<version>`` pin for the union of extras groups, plus
    the declared version range of every requested feature. After pip exits,
    runs each feature's :attr:`Feature.post_install` step and re-checks that
    the deps landed — importable, and inside their range.

    Returns immediately if every feature is already importable and up to
    date — useful for backwards compat with installs that ran ``pip install
    cremind`` before this refactor (every dep is already on disk; nothing to
    do).

    An updated feature always sets ``restart_required`` and
    :func:`restart_pending`, whatever its ``requires_restart`` says: that flag
    describes a *first* install, where nothing old is loaded yet.
    """
    _emit_event(emit, InstallEvent("start", f"Resolving features: {', '.join(feature_keys) or '(none)'}"))

    # Validate up-front so a typo can't slip past as a successful no-op.
    for key in feature_keys:
        if key not in FEATURES:
            err = f"Unknown feature: {key!r}"
            _emit_event(emit, InstallEvent("error", err, ok=False))
            return InstallResult(restart_required=False, error=err)

    requested = list(dict.fromkeys(feature_keys))
    missing = missing_features(requested)
    outdated = outdated_features([k for k in requested if k not in missing])
    needed = [k for k in requested if k in missing or k in outdated]
    already_present = [k for k in requested if k not in needed]

    if not needed:
        _emit_event(emit, InstallEvent("done", "All requested features already installed."))
        return InstallResult(
            restart_required=False,
            already_present=already_present,
        )

    for key in outdated:
        for check in version_checks(key):
            if check.satisfied:
                continue
            line = f"{check.dist} {check.installed} does not satisfy {check.requirement} - updating {key}"
            logger.info(f"[features] {line}")
            _emit_event(emit, InstallEvent(
                "log",
                line,
                meta={
                    "feature": key,
                    "dist": check.dist,
                    "installed": check.installed,
                    "required": check.requirement,
                },
            ))

    if os.name == "nt" and "codex" in outdated:
        busy_error = _codex_busy_error()
        if busy_error is not None:
            logger.warning(f"[features] refusing to update codex: {busy_error}")
            _emit_event(emit, InstallEvent("error", busy_error, ok=False))
            return InstallResult(
                restart_required=False,
                already_present=already_present,
                failed=list(needed),
                error=busy_error,
            )

    channel = get_channel()
    requirements = pip_requirements(needed, channel=channel)
    _emit_event(emit, InstallEvent("log", f"pip install {' '.join(requirements)}"))

    try:
        # ``upgrade=False`` so pip doesn't touch the already-installed
        # cremind wheel — only resolves the extras' transitive deps. This
        # is critical for editable dev installs (otherwise pip would
        # download a PyPI build over the live source tree). An outdated
        # feature still gets updated: its version range rides in
        # ``requirements``, and pip replaces an installed version that
        # falls outside a range it was asked for.
        _pip_install(
            requirements,
            _wrap_upgrade_callback(emit),
            channel=channel,
            upgrade=False,
        )
    except Exception as exc:  # noqa: BLE001 — pip failures surface as RuntimeError
        error = str(exc)
        hint = _file_lock_hint(error, outdated)
        if hint:
            error = f"{error}\n\n{hint}"
        logger.error(f"[features] pip install failed for {' '.join(requirements)}: {error}")
        _emit_event(emit, InstallEvent("error", f"pip install failed: {error}", ok=False))
        return InstallResult(
            restart_required=False,
            already_present=already_present,
            failed=list(needed),
            error=error,
        )

    # Pip wrote new files to ``site-packages``. Tell the import machinery to
    # forget any previous "module not found" lookups so subsequent
    # ``find_spec`` probes see the new packages. The same call drops
    # ``importlib.metadata``'s path caches, so the version re-check below
    # reads the freshly written dist-info.
    importlib.invalidate_caches()

    # Post-install steps (e.g. ``playwright install chromium``).
    post_install_errors: list[str] = []
    for key in needed:
        for step in FEATURES[key].post_install:
            try:
                _run_post_install_step(step, emit)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[features] post-install step {step!r} failed for {key}: {exc}")
                post_install_errors.append(f"{key}:{step}: {exc}")
                _emit_event(emit, InstallEvent(
                    "post_install",
                    f"post-install step {step!r} failed for {key}: {exc}",
                    ok=False,
                    meta={"feature": key, "step": step},
                ))

    # Re-check to determine which features actually landed. Importable is
    # not enough for an update: pip can exit 0 having kept the old version
    # (a constraint elsewhere, a resolver backtrack), and that is a failure.
    installed_now: list[str] = []
    upgraded_now: list[str] = []
    failed_now: list[str] = []
    for key in needed:
        if is_installed(key) and not is_outdated(key):
            (upgraded_now if key in outdated else installed_now).append(key)
        else:
            failed_now.append(key)

    if upgraded_now:
        _mark_restart_pending(upgraded_now)
    restart_keys = [k for k in installed_now if FEATURES[k].requires_restart] + upgraded_now
    restart_required = bool(restart_keys)

    error: str | None = None
    if failed_now:
        error = f"Some features could not be activated: {', '.join(_describe_failure(k) for k in failed_now)}"
    elif post_install_errors:
        error = "; ".join(post_install_errors)

    result = InstallResult(
        restart_required=restart_required,
        installed=installed_now,
        failed=failed_now,
        already_present=already_present,
        error=error,
        upgraded=upgraded_now,
    )

    message = "Install complete."
    if upgraded_now:
        message += f" Updated: {', '.join(upgraded_now)}."
    if restart_required:
        message += f" Restart required for: {', '.join(restart_keys)}"
    _emit_event(emit, InstallEvent(
        "done",
        message,
        ok=not failed_now,
        meta=asdict(result),
    ))
    return result


def feature_status() -> dict[str, dict]:
    """Snapshot of every feature's install state.

    Returns a dict keyed by feature id:
    ``{installed: bool, requires_restart_after_install: bool, extras: [...],
    outdated: bool, required: [...], installed_versions: {dist: version},
    restart_pending: bool}``. ``outdated`` is only ever true for an installed
    feature; ``required`` / ``installed_versions`` are empty for a feature
    that declares no version range. All system facts — the venv is shared by
    every profile.
    """
    status: dict[str, dict] = {}
    for key, feat in FEATURES.items():
        report = version_report(key)
        status[key] = {
            "installed": is_installed(key),
            "requires_restart_after_install": feat.requires_restart,
            "extras": list(feat.extras),
            "outdated": report["outdated"],
            "required": report["required"],
            "installed_versions": report["installed_versions"],
            "restart_pending": restart_pending(key),
        }
    return status


# ── update guards ───────────────────────────────────────────────────────────

def _codex_busy_error() -> str | None:
    """Why a Codex update must wait, or ``None`` when nothing holds the binary.

    Windows cannot replace an executable while it runs, and every running
    Codex task and live sign-in holds a ``codex`` child process. Letting pip
    start anyway fails part-way through, possibly after the old SDK is
    already uninstalled. Counts only: whoever clicked Update must not learn
    another profile's task ids or sessions. Either count that can't be read
    (the tool module failed to import, say) counts as zero — this is a
    courtesy check, and the pip-failure hint still covers a lock it misses.
    """
    tasks = _count_or_zero("app.tools.builtin.codex_runner", "busy_count")
    sign_ins = _count_or_zero("app.tools.builtin.codex_login", "active_count")
    if tasks <= 0 and sign_ins <= 0:
        return None
    return (
        f"Codex is in use on this server ({tasks} running task(s), "
        f"{sign_ins} sign-in(s) in progress). Windows cannot replace the codex "
        "binary while it runs, so the update was not started. Wait for them "
        "to finish (or restart the Cremind server), then update before "
        "starting Codex."
    )


def _count_or_zero(module_name: str, func_name: str) -> int:
    try:
        return int(getattr(importlib.import_module(module_name), func_name)())
    except Exception as exc:  # noqa: BLE001 — fail open, see _codex_busy_error
        logger.warning(f"[features] could not read {module_name}.{func_name}(): {exc}")
        return 0


def _file_lock_hint(error_text: str, outdated: list[str]) -> str | None:
    """The next step after a Windows file lock broke an update, or ``None``.

    Only for an update: a first install has no old binary running, so the
    same error there has some other cause and this advice would mislead.
    """
    if not outdated or not _WINDOWS_FILE_LOCK_RE.search(error_text):
        return None
    names = ", ".join("Codex" if key == "codex" else key for key in outdated)
    return (
        "Windows could not replace a file that is still in use - most likely "
        f"the running binary of the old version. Restart the Cremind server, "
        f"then update before starting {names}."
    )


def _describe_failure(feature_key: str) -> str:
    """``feature_key``, plus the versions when pip left it outdated."""
    if not is_installed(feature_key):
        return feature_key
    stale = [check for check in version_checks(feature_key) if not check.satisfied]
    if not stale:
        return feature_key
    detail = "; ".join(
        f"{check.dist} {check.installed} still installed, needs {check.requirement}"
        for check in stale
    )
    return f"{feature_key} ({detail})"


# ── post-install steps ──────────────────────────────────────────────────────

def _run_post_install_step(step: str, emit: EventCallback | None) -> None:
    """Dispatch ``step`` to its handler. Raises on failure."""
    if step == "playwright_install_chromium":
        _playwright_install_chromium(emit)
        return
    raise ValueError(f"Unknown post-install step: {step!r}")


def _playwright_install_chromium(emit: EventCallback | None) -> None:
    """Download the chromium binary playwright drives.

    Playwright ships browser binaries separately from the wheel. The CLI
    ``python -m playwright install chromium`` downloads them into the user
    cache (``~/.cache/ms-playwright`` on Linux/macOS,
    ``%LOCALAPPDATA%\\ms-playwright`` on Windows). We pipe its output
    through the upgrade-runner's subprocess helper so logs reach the SSE
    stream the same way pip's do.

    ``--with-deps`` makes the CLI also install the OS-level libraries
    chromium needs (apt-get on Linux, no-op on macOS/Windows). Inside
    the Docker container we run as root so the apt-get call succeeds.
    On native Linux installs it falls back to logging the libs the user
    needs to install with sudo; the binary download still proceeds.
    """
    _emit_event(emit, InstallEvent("post_install", "Installing playwright chromium browser"))
    cmd = [sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"]
    _run(cmd, _wrap_upgrade_callback(emit), prefer="playwright install --with-deps chromium")


# ── emit-callback adapters ──────────────────────────────────────────────────

def _emit_event(emit: EventCallback | None, event: InstallEvent) -> None:
    if emit is None:
        return
    try:
        emit(event)
    except Exception:  # noqa: BLE001 — never let a callback break install
        pass


def _wrap_upgrade_callback(emit: EventCallback | None) -> ProgressCallback | None:
    """Translate ``UpgradeEvent`` records from the upgrade runner into our
    :class:`InstallEvent` shape so the SSE stream is uniform.
    """
    if emit is None:
        return None

    def _callback(ev: UpgradeEvent) -> None:
        _emit_event(emit, InstallEvent(
            kind="log",
            message=ev.message,
            ok=ev.ok,
            meta=ev.meta or {},
        ))

    return _callback
