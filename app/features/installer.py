"""Feature installer — runs ``pip install`` for opt-in extras at runtime.

Used by the Setup Wizard endpoint (``POST /api/config/setup``) and the
post-setup Settings page (``POST /api/features/install``). Reuses the
upgrader's pip plumbing in :mod:`app.upgrade.runner` so we don't duplicate
channel-aware index handling, ``PIP_CACHE_DIR`` setup, or the
``sys.executable -m pip`` subprocess form.

Public surface:

- :func:`install_features` — install the union of extras groups needed by
  ``feature_keys`` and emit progress events.
- :func:`feature_status` — snapshot of every feature's install state, used
  by ``GET /api/features``.
- :class:`InstallResult` and :class:`InstallEvent` — return / streaming
  payloads.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import asdict, dataclass, field
from typing import Callable

from app.features.manifest import (
    FEATURES,
    is_installed,
    missing_features,
    pip_spec,
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
    """Outcome of one :func:`install_features` call."""

    restart_required: bool
    installed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)
    error: str | None = None


def install_features(
    feature_keys: list[str],
    emit: EventCallback | None = None,
) -> InstallResult:
    """Install the deps required by ``feature_keys`` (if missing).

    Builds one ``pip install cremind[a,b,c]==<version>`` invocation for the
    union of extras groups across all requested features. After pip exits,
    runs each feature's :attr:`Feature.post_install` step and re-probes to
    confirm the deps landed.

    Returns immediately if every feature is already importable — useful for
    backwards compat with installs that ran ``pip install cremind`` before
    this refactor (every dep is already on disk; nothing to do).
    """
    _emit_event(emit, InstallEvent("start", f"Resolving features: {', '.join(feature_keys) or '(none)'}"))

    # Validate up-front so a typo can't slip past as a successful no-op.
    for key in feature_keys:
        if key not in FEATURES:
            err = f"Unknown feature: {key!r}"
            _emit_event(emit, InstallEvent("error", err, ok=False))
            return InstallResult(restart_required=False, error=err)

    already_present = [k for k in feature_keys if is_installed(k)]
    needed = missing_features(feature_keys)

    if not needed:
        _emit_event(emit, InstallEvent("done", "All requested features already installed."))
        return InstallResult(
            restart_required=False,
            already_present=already_present,
        )

    channel = get_channel()
    spec = pip_spec(needed, channel=channel)
    _emit_event(emit, InstallEvent("log", f"pip install {spec}"))

    try:
        # ``upgrade=False`` so pip doesn't touch the already-installed
        # cremind wheel — only resolves the extras' transitive deps. This
        # is critical for editable dev installs (otherwise pip would
        # download a PyPI build over the live source tree).
        _pip_install(
            spec,
            _wrap_upgrade_callback(emit),
            channel=channel,
            upgrade=False,
        )
    except Exception as exc:  # noqa: BLE001 — pip failures surface as RuntimeError
        logger.error(f"[features] pip install failed for spec={spec}: {exc}")
        _emit_event(emit, InstallEvent("error", f"pip install failed: {exc}", ok=False))
        return InstallResult(
            restart_required=False,
            already_present=already_present,
            failed=list(needed),
            error=str(exc),
        )

    # Pip wrote new files to ``site-packages``. Tell the import machinery to
    # forget any previous "module not found" lookups so subsequent
    # ``find_spec`` probes see the new packages.
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

    # Re-probe to determine which features actually landed.
    installed_now: list[str] = []
    failed_now: list[str] = []
    for key in needed:
        if is_installed(key):
            installed_now.append(key)
        else:
            failed_now.append(key)

    restart_required = any(FEATURES[k].requires_restart for k in installed_now)
    error: str | None = None
    if failed_now:
        error = f"Some features could not be activated: {', '.join(failed_now)}"
    elif post_install_errors:
        error = "; ".join(post_install_errors)

    result = InstallResult(
        restart_required=restart_required,
        installed=installed_now,
        failed=failed_now,
        already_present=already_present,
        error=error,
    )

    _emit_event(emit, InstallEvent(
        "done",
        "Install complete." + (f" Restart required for: {', '.join(installed_now)}" if restart_required else ""),
        ok=not failed_now,
        meta=asdict(result),
    ))
    return result


def feature_status() -> dict[str, dict]:
    """Snapshot of every feature's install state.

    Returns a dict keyed by feature id:
    ``{installed: bool, requires_restart_after_install: bool, extras: [...]}``.
    """
    return {
        key: {
            "installed": is_installed(key),
            "requires_restart_after_install": feat.requires_restart,
            "extras": list(feat.extras),
        }
        for key, feat in FEATURES.items()
    }


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
