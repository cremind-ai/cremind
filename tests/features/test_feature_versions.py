"""Version-aware feature checks in :mod:`app.features.manifest`.

A probe only proves a package imports, and nothing re-syncs a runtime venv,
so an install that got ``openai-codex`` 0.1.0b3 under the old pin kept it —
and that SDK's bundled binary cannot read ChatGPT's live model catalog. These
cases pin what "outdated" means and, just as important, what it must NOT
disturb: ``is_installed`` stays probe-only, and a check that can't be made
fails open.

Every case patches ``importlib.metadata.version`` (and the probe where it
matters) so the answers don't depend on what the venv running the suite
happens to have installed.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import sys

import pytest

from app.features import manifest
from app.features.manifest import FEATURES, Feature

CODEX_REQ = "openai-codex>=0.154.0,<0.155"


def _versions(monkeypatch: pytest.MonkeyPatch, versions: dict[str, str]) -> None:
    """Answer ``importlib.metadata.version`` from ``versions``; anything else
    has no metadata."""

    def fake_version(dist: str) -> str:
        if dist not in versions:
            raise importlib.metadata.PackageNotFoundError(dist)
        return versions[dist]

    monkeypatch.setattr(importlib.metadata, "version", fake_version)


def _installed(monkeypatch: pytest.MonkeyPatch, installed: bool = True) -> None:
    monkeypatch.setattr(manifest, "is_installed", lambda _key: installed)


# ── the codex declaration ─────────────────────────────────────────────────


def test_codex_declares_its_version_range() -> None:
    assert FEATURES["codex"].requirements == (CODEX_REQ,)
    # A first install still loads in-process; only an UPDATE needs a restart,
    # and the installer tracks that on its own.
    assert FEATURES["codex"].requires_restart is False


# ── is_outdated ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "installed_version,outdated",
    [
        ("0.1.0b3", True),     # the old beta: bundled binary can't read the catalog
        ("0.137.0", True),
        ("0.154.0", False),
        ("0.154.7", False),
        ("0.154.1b1", False),  # a pre-release inside the range is inside it
        ("0.155.0", True),     # above the upper bound
    ],
)
def test_is_outdated_follows_the_range(
    monkeypatch: pytest.MonkeyPatch, installed_version: str, outdated: bool,
) -> None:
    _installed(monkeypatch)
    _versions(monkeypatch, {"openai-codex": installed_version})

    assert manifest.is_outdated("codex") is outdated


def test_version_checks_report_the_installed_version(monkeypatch: pytest.MonkeyPatch) -> None:
    _versions(monkeypatch, {"openai-codex": "0.1.0b3"})

    assert manifest.version_checks("codex") == [
        manifest.VersionCheck(
            requirement=CODEX_REQ,
            dist="openai-codex",
            installed="0.1.0b3",
            satisfied=False,
        ),
    ]


def test_missing_metadata_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importable but no dist-info (a vendored copy, a hand-built image): there
    is nothing to compare, so no update nag."""
    _installed(monkeypatch)
    _versions(monkeypatch, {})

    [check] = manifest.version_checks("codex")
    assert check.installed is None and check.satisfied is True
    assert manifest.is_outdated("codex") is False


def test_unparseable_version_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    _installed(monkeypatch)
    _versions(monkeypatch, {"openai-codex": "not a version"})

    [check] = manifest.version_checks("codex")
    assert check.installed == "not a version" and check.satisfied is True
    assert manifest.is_outdated("codex") is False


def test_missing_packaging_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """``packaging`` is a core dependency, but a venv without it must still
    serve the status endpoints — the version is reported, never judged."""
    _installed(monkeypatch)
    _versions(monkeypatch, {"openai-codex": "0.1.0b3"})
    # A ``None`` entry makes the import raise ImportError even when the real
    # module was already loaded.
    monkeypatch.setitem(sys.modules, "packaging.requirements", None)

    [check] = manifest.version_checks("codex")
    assert check.dist == "openai-codex"
    assert check.installed == "0.1.0b3"
    assert check.satisfied is True
    assert manifest.is_outdated("codex") is False


def test_a_requirement_whose_marker_excludes_this_platform_is_satisfied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        FEATURES,
        "fake.marker",
        Feature(
            key="fake.marker",
            extras=("fake",),
            probes=("fake",),
            requirements=("fakepkg>=2 ; python_version < '3'",),
        ),
    )
    _installed(monkeypatch)
    _versions(monkeypatch, {"fakepkg": "1.0"})

    assert manifest.is_outdated("fake.marker") is False


def test_a_feature_that_is_not_installed_is_missing_not_outdated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leftover metadata from a half-removed SDK must not turn a missing
    feature into an "update" — installing it already brings the range."""
    _installed(monkeypatch, installed=False)
    _versions(monkeypatch, {"openai-codex": "0.1.0b3"})

    assert manifest.is_outdated("codex") is False


def test_a_feature_without_requirements_is_never_outdated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_probe(_key: str) -> bool:
        raise AssertionError("nothing to check, so nothing to probe")

    monkeypatch.setattr(manifest, "is_installed", no_probe)
    _versions(monkeypatch, {})

    assert FEATURES["browser"].requirements == ()
    assert manifest.version_checks("browser") == []
    assert manifest.is_outdated("browser") is False


def test_is_installed_ignores_the_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every gate that blocks a WORKING tool (enable 409, Setup Wizard,
    channel connect) asks ``is_installed``; an outdated SDK still runs, so it
    must keep reading as installed."""
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, *args, **kwargs):
        if name == "openai_codex":
            return object()
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    _versions(monkeypatch, {"openai-codex": "0.1.0b3"})

    assert manifest.is_installed("codex") is True
    assert manifest.is_outdated("codex") is True
    assert manifest.missing_features(["codex"]) == []


def test_unknown_feature_raises() -> None:
    with pytest.raises(KeyError):
        manifest.version_checks("nope")
    with pytest.raises(KeyError):
        manifest.is_outdated("nope")
    with pytest.raises(KeyError):
        manifest.version_report("nope")


# ── outdated_features / version_report ───────────────────────────────────


def test_outdated_features_preserves_order_and_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    _installed(monkeypatch)
    _versions(monkeypatch, {"openai-codex": "0.1.0b3"})

    assert manifest.outdated_features(["browser", "codex", "codex"]) == ["codex"]
    with pytest.raises(KeyError):
        manifest.outdated_features(["codex", "nope"])


def test_version_report_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    _installed(monkeypatch)
    _versions(monkeypatch, {"openai-codex": "0.1.0b3"})

    assert manifest.version_report("codex") == {
        "outdated": True,
        "required": [CODEX_REQ],
        "installed_versions": {"openai-codex": "0.1.0b3"},
    }
    assert manifest.version_report("browser") == {
        "outdated": False,
        "required": [],
        "installed_versions": {},
    }


# ── pip_requirements ─────────────────────────────────────────────────────


@pytest.mark.parametrize("channel", ["production", "test"])
def test_pip_requirements_pins_cremind_and_appends_the_range(channel: str) -> None:
    from app.__version__ import __version__

    assert manifest.pip_requirements(["codex"], channel=channel) == [
        f"cremind[codex]=={__version__}",
        CODEX_REQ,
    ]


def test_pip_requirements_defaults_to_production() -> None:
    from app.__version__ import __version__

    assert manifest.pip_requirements(["codex"]) == [f"cremind[codex]=={__version__}", CODEX_REQ]


def test_pip_requirements_without_ranges_is_just_the_spec() -> None:
    from app.__version__ import __version__

    assert manifest.pip_requirements(["browser"]) == [f"cremind[browser]=={__version__}"]


def test_pip_requirements_dev_keeps_ranged_features_out_of_the_extras() -> None:
    """The editable cremind's metadata can still carry the OLD pin, so
    ``cremind[codex]`` next to the new range would be unresolvable (or pull a
    PyPI cremind over the checkout). Plain ``cremind`` is already satisfied."""
    assert manifest.pip_requirements(["codex"], channel="dev") == ["cremind", CODEX_REQ]


def test_pip_requirements_dev_with_a_mix_of_features() -> None:
    assert manifest.pip_requirements(["browser", "codex", "vectorstore.qdrant"], channel="dev") == [
        "cremind[browser,vectorstore-qdrant]",
        CODEX_REQ,
    ]


def test_pip_requirements_dedupes_ranges(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        FEATURES,
        "fake.codex_twin",
        Feature(
            key="fake.codex_twin",
            extras=("codex",),
            probes=("openai_codex",),
            requirements=(CODEX_REQ,),
        ),
    )

    assert manifest.pip_requirements(["codex", "fake.codex_twin", "codex"], channel="dev") == [
        "cremind",
        CODEX_REQ,
    ]


def test_pip_requirements_rejects_unknown_features() -> None:
    for channel in ("production", "dev"):
        with pytest.raises(KeyError):
            manifest.pip_requirements(["codex", "nope"], channel=channel)
