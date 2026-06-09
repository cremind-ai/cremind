"""Install/setup catalog loader.

Reads ``install_catalog.toml`` (a generated copy of ``install/catalog.toml``)
and exposes the parsed catalogue to:

  - the wizard API (``GET /api/config/install-catalog``)
  - the service-capability filter in :mod:`app.api.features`

The master at ``install/catalog.toml`` is hand-edited; running
``python install/scripts/build_catalog.py`` regenerates this copy along
with the bash and PowerShell includes consumed by the install scripts.

The active install mode is read from the ``INSTALL_MODE`` environment
variable (typically set in the project ``.env`` written by the
installer). It seeds the ``mode_rules`` lookup so the wizard's
service-mode pickers only show options the install mode supports.

If the file is missing or malformed we fall back to a hardcoded
catalog so the wizard is never bricked by a bad TOML — same defensive
pattern as :mod:`app.config.setup_profiles`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import toml

from app.utils.logger import logger


_ACTIVE_MODE_ENV = "INSTALL_MODE"

_CATALOG_FILE = Path(__file__).parent / "install_catalog.toml"


# Hardcoded fallback. Mirrors the shipped install_catalog.toml minus the
# verbose comments. Used only when the TOML file cannot be loaded.
_DEFAULT_CATALOG: dict[str, Any] = {
    "schema_version": 1,
    "deployments": {
        "local": {
            "label": "Local",
            "short": "this machine only",
            "description": "bind to 127.0.0.1, only this machine can reach it",
            "env_value": "local",
            "host": "127.0.0.1",
            "requires_host": False,
            "order": 10,
        },
        "server": {
            "label": "Server",
            "short": "reachable from other devices",
            "description": "bind to all interfaces, reachable from other devices",
            "env_value": "production",
            "host": "0.0.0.0",
            "requires_host": True,
            "order": 20,
        },
        "custom": {
            "label": "Custom (advanced)",
            "short": "I'll configure host, URL, and CORS myself",
            "description": "advanced setup — choose where Cremind listens and how it's reached",
            "env_value": "custom",
            "order": 30,
            "advanced_fields": [
                {
                    "key": "listen_host",
                    "prompt": "Where should Cremind listen for connections?",
                    "hint": (
                        "Use 127.0.0.1 to only allow this machine, or 0.0.0.0 to "
                        "allow other devices / containers on the network."
                    ),
                    "default": "0.0.0.0",
                },
                {
                    "key": "public_url",
                    "prompt": "What URL will you use to open Cremind in a browser?",
                    "hint": (
                        "This is the address users type into their browser. Inside a "
                        "container it's usually http://localhost:1112."
                    ),
                    "default": "http://localhost:1112",
                },
                {
                    "key": "allowed_origins",
                    "prompt": "Which web origins should be allowed to talk to the API?",
                    "hint": (
                        "A comma-separated list of URLs the browser UI will be served "
                        "from. Leave blank to use the public URL plus localhost variants."
                    ),
                    "default": "",
                },
                {
                    "key": "wizard_preset",
                    "prompt": "Which preset should pre-fill the Setup Wizard?",
                    "hint": (
                        "Presets fill in sensible defaults for the next setup screens "
                        "— you can always edit any field afterwards."
                    ),
                    "choices": ["local", "docker", "server"],
                    "default": "local",
                },
            ],
        },
    },
    "modes": {
        "docker": {
            "label": "Docker",
            "description": "sandboxed VNC desktop with a bundled storage stack",
            "hint": "The agent runs inside a container with its own GUI.",
            "badge": "recommended",
            "requires": ["docker"],
            "order": 10,
        },
        "native": {
            "label": "Native",
            "description": "Python venv at ~/.cremind/venv with embedded storage",
            "hint": "Simpler, but the agent shares your desktop and home directory.",
            "order": 20,
        },
    },
    "mode_rules": {
        "docker": {
            "allowed_service_modes": ["docker", "native"],
            "default_service_mode": "docker",
        },
        "native": {
            "allowed_service_modes": ["native", "external"],
            "default_service_mode": "external",
        },
        "custom": {
            "allowed_service_modes": ["docker", "native", "external"],
            "default_service_mode": "external",
        },
    },
    "service_modes": {
        "docker": {
            "label": "Docker",
            "description_template": "Cremind starts a {service} container alongside itself.",
        },
        "native": {
            "label": "Native",
            "description_template": "Cremind runs {service} locally (no extra container).",
        },
        "external": {
            "label": "External",
            "description_template": "Connect to an existing {service} instance.",
        },
    },
}


_cached: dict[str, Any] | None = None


def load_install_catalog(*, force_reload: bool = False) -> dict[str, Any]:
    """Return the parsed install catalog as a dict.

    Cached after the first call. Pass ``force_reload=True`` from tests to
    pick up a freshly written TOML.
    """
    global _cached
    if _cached is not None and not force_reload:
        return _cached

    if _CATALOG_FILE.is_file():
        try:
            _cached = toml.loads(_CATALOG_FILE.read_text(encoding="utf-8"))
            return _cached
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"install_catalog.toml is malformed ({exc}); using built-in fallback."
            )

    _cached = _DEFAULT_CATALOG
    return _cached


def get_active_install_mode() -> str | None:
    """Return the install mode selected via ``INSTALL_MODE`` env var.

    ``None`` when unset, blank, or pointing at a mode the catalog
    doesn't define — in that case service-mode filtering is a no-op.
    Resolved fresh on every call so test setups that mutate the env
    take effect without bouncing the loader cache.
    """
    raw = os.environ.get(_ACTIVE_MODE_ENV, "").strip()
    if not raw:
        return None
    catalog = load_install_catalog()
    modes = catalog.get("modes", {}) or {}
    if raw not in modes:
        logger.warning(
            f"{_ACTIVE_MODE_ENV}={raw!r} does not match any mode in install_catalog.toml; "
            "ignoring."
        )
        return None
    return raw


def is_kubernetes_mode() -> bool:
    """True iff the active install mode (``INSTALL_MODE`` env var) is ``kubernetes``.

    The Helm chart sets ``INSTALL_MODE=kubernetes`` on the pod. The wizard's
    ``[mode_rules.kubernetes]`` already trims every backing service to the
    External (in-cluster) deployment mode, but that filter is advisory — a
    hand-crafted POST to the unauthenticated first-setup endpoint could still
    submit SQLite or a persistent/native vector store. The storage enforcement
    seams (``app/api/config.py``, ``app/databases/factory.py``,
    ``app/lib/embedding_lifecycle.py``, ``app/vectorstores/factory.py``,
    ``app/cli/commands/db.py``) call this to reject pod-local storage choices
    that would break horizontal scaling.

    Resolved fresh on every call (via :func:`get_active_install_mode`) so test
    setups that mutate the env take effect without bouncing the loader cache.
    """
    return get_active_install_mode() == "kubernetes"


def get_mode_rule(install_mode: str | None) -> dict[str, Any]:
    """Return the mode-rule entry for ``install_mode``.

    Falls back to ``{}`` when ``install_mode`` is None or the catalog
    has no rule for it — callers should treat that as "no filter".
    """
    if not install_mode:
        return {}
    rules = load_install_catalog().get("mode_rules", {}) or {}
    return rules.get(install_mode, {}) or {}


def apply_mode_rule_to_services(
    services: dict[str, dict[str, Any]],
    install_mode: str | None,
) -> dict[str, dict[str, Any]]:
    """Filter each service's ``supported_modes`` against the mode rule.

    Mutates the passed-in service entries (and returns them) for callers
    that already work with a freshly-built capabilities payload. When
    ``install_mode`` resolves to no rule, services are returned as-is.

    The filter intersects each service's declared ``supported_modes``
    with the rule's ``allowed_service_modes``. A service whose
    supported_modes ends up empty keeps its empty list — the wizard's
    radio component is responsible for rendering a sensible message
    rather than the backend having to invent a synthetic option.
    """
    rule = get_mode_rule(install_mode)
    allowed = rule.get("allowed_service_modes") or []
    if not allowed:
        return services
    allowed_set = set(allowed)
    for svc in services.values():
        modes = svc.get("supported_modes") or []
        svc["supported_modes"] = [m for m in modes if m in allowed_set]
    return services
