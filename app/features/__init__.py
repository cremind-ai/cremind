"""Feature gating: optional dependencies installed at runtime.

The Setup Wizard collects per-feature opt-in (embedding on/off, vectorstore
provider, LLM provider, channels, db_provider). When the user enables a
feature, ``app.features.installer.install_features`` runs ``pip install
cremind[<group>]`` against the live venv so we don't pre-bundle every SDK at
``pip install cremind`` time.

The manifest (``app.features.manifest``) is the single source of truth that
maps a feature key (e.g. ``"embedding.me5"``) to its extras group and to
``importlib`` probes used to detect whether the dep is already present.
"""

from .manifest import (
    FEATURES,
    Feature,
    is_installed,
    missing_features,
    pip_spec,
)

__all__ = [
    "FEATURES",
    "Feature",
    "is_installed",
    "missing_features",
    "pip_spec",
]
