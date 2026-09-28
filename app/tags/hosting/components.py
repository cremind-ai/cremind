"""Is this computer ready to run gateways? The hardware components and their state.

A gateway worker needs three things beyond Cremind itself:

- **packages** — the ``tags`` extra (serial link, CBOR, ICU/HarfBuzz/FreeType
  layout, previews), installed by the feature installer;
- **fonts** — a verified font asset bundle (the pack the bridges draw from,
  and the source fonts the worker shapes text with) under
  ``<SYS>/.tag-runtime/assets``; without it a worker still connects and
  pairs, but holds screens;
- **the platform** — Windows x64, Linux x64/arm64, macOS 15 or newer (the
  ICU wheels exist for macOS 15+ only: an older macOS keeps every other
  Cremind feature, just not gateways).

:func:`readiness` reports each (``ready`` / ``missing`` / ``unsupported`` /
``broken``) plus what the host can reach (``usb``): whether the backend runs
in a container, where USB only exists when it was mapped in. It never
installs anything; :mod:`.prepare` does.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PACKAGE_PROBES = ("serial", "cbor2", "icu", "uharfbuzz", "freetype", "PIL")
FEATURE_KEY = "tags"
MIN_MACOS = (15, 0)


@dataclass(frozen=True)
class Component:
    key: str
    state: str
    """``ready`` | ``missing`` | ``unsupported`` | ``broken``."""
    detail: str = ""

    def as_json(self) -> dict[str, Any]:
        return {"key": self.key, "state": self.state, "detail": self.detail}


def platform_support() -> Component:
    system = sys.platform
    machine = platform.machine().lower()
    if system == "win32":
        if machine not in ("amd64", "x86_64", "arm64"):
            return Component("platform", "unsupported", f"Windows on {machine} is not supported for gateways.")
        return Component("platform", "ready", "Windows")
    if system == "darwin":
        release = platform.mac_ver()[0] or "0"
        try:
            version = tuple(int(x) for x in release.split(".")[:2])
        except ValueError:
            version = (0, 0)
        if version < MIN_MACOS:
            return Component("platform", "unsupported",
                             f"Gateways need macOS {MIN_MACOS[0]} or newer (this Mac runs {release}).")
        return Component("platform", "ready", f"macOS {release}")
    if system.startswith("linux"):
        if machine not in ("x86_64", "amd64", "aarch64", "arm64"):
            return Component("platform", "unsupported", f"Linux on {machine} is not supported for gateways.")
        return Component("platform", "ready", "Linux")
    return Component("platform", "unsupported", f"{system} is not supported for gateways.")


def packages() -> Component:
    missing = []
    for name in PACKAGE_PROBES:
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)
    if missing:
        return Component("packages", "missing", "Not installed yet: " + ", ".join(missing) + ".")
    try:
        from app.features.installer import restart_pending

        if restart_pending(FEATURE_KEY):
            return Component("packages", "broken", "Updated while Cremind was running: restart Cremind.")
    except Exception:  # noqa: BLE001 - the installer module is optional in the desktop host
        pass
    return Component("packages", "ready")


def fonts(assets_root: Path, expected_pack: str | None = None) -> Component:
    """A verified pack in ``assets_root`` (the expected one, when the release names it)."""
    fonts_dir = assets_root / "fonts"
    if not fonts_dir.is_dir():
        return Component("fonts", "missing", "No font pack is installed yet.")
    try:
        from app.tags.runtime.resources import font_assets_in
    except ImportError:
        return Component("fonts", "missing", "The font tools are not installed yet.")
    found = font_assets_in(assets_root)
    if not found:
        return Component("fonts", "missing", "No font pack is installed yet.")
    if expected_pack and not any(a.pack_id == expected_pack for a in found):
        return Component("fonts", "missing", f"Font pack {expected_pack} is not installed yet "
                                             f"(installed: {', '.join(a.pack_id for a in found)}).")
    return Component("fonts", "ready", ", ".join(a.pack_id for a in found))


def usb_access() -> dict[str, Any]:
    """What the backend can reach: ``{available, container, reason}`` (best effort, never raises)."""
    container = False
    try:
        from app.config.runtime_env import is_container

        container = bool(is_container())
    except Exception:  # noqa: BLE001
        container = os.path.exists("/.dockerenv")
    if container and sys.platform.startswith("linux"):
        mapped = any(Path("/dev").glob(pattern) for pattern in ("ttyACM*", "ttyUSB*"))
        if not mapped:
            return {"available": False, "container": True,
                    "reason": "Cremind runs in a container without USB serial devices mapped in."}
    return {"available": True, "container": container, "reason": None}


def readiness(assets_root: Path, expected_pack: str | None = None) -> dict[str, Any]:
    """Everything the gateway page shows about this host's components."""
    items = [platform_support(), packages(), fonts(assets_root, expected_pack)]
    states = {c.key: c.state for c in items}
    if states["platform"] == "unsupported":
        overall = "unsupported"
    elif all(s == "ready" for s in states.values()):
        overall = "ready"
    elif states["packages"] == "ready" and states["fonts"] != "ready":
        overall = "partial"  # gateways connect and pair; screens wait for the fonts
    else:
        overall = "missing"
    return {"state": overall, "components": [c.as_json() for c in items], "usb": usb_access()}


def can_host(readiness_doc: dict[str, Any]) -> bool:
    """Workers can run (screens may still wait for fonts)."""
    return readiness_doc.get("state") in ("ready", "partial")


__all__ = ["Component", "FEATURE_KEY", "PACKAGE_PROBES", "can_host", "fonts", "packages", "platform_support",
           "readiness", "usb_access"]
