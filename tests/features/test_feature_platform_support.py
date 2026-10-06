"""Which optional features this computer can't install, and why.

Both Vector Embedding models run on PyTorch, which publishes no wheel (and no
sdist) for an Intel Mac on Python 3.13+ or for Windows on ARM. Before this
check, the Setup Wizard on an Intel Mac sent pip after it anyway: pip
backtracked through old releases of everything else, built a broken
``fastapi`` sdist from Test PyPI, and failed all six features in the request.

The platform is swapped through the manifest's own ``sys`` / ``platform``
references, never the real modules.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.features import manifest

EMBEDDING = ("embedding.me5", "embedding.gemma")


def _platform(monkeypatch: pytest.MonkeyPatch, system: str, machine: str) -> None:
    monkeypatch.setattr(manifest, "sys", SimpleNamespace(platform=system))
    monkeypatch.setattr(manifest, "platform", SimpleNamespace(machine=lambda: machine))


@pytest.mark.parametrize(
    ("system", "machine", "unsupported"),
    [
        ("darwin", "x86_64", True),   # Intel Mac, or an x86_64 Python under Rosetta
        ("darwin", "arm64", False),   # Apple Silicon
        ("win32", "ARM64", True),     # Windows on ARM reports upper case
        ("win32", "AMD64", False),
        ("linux", "x86_64", False),
        ("linux", "aarch64", False),
    ],
)
def test_the_embedding_models_need_a_pytorch_build(
    monkeypatch: pytest.MonkeyPatch, system: str, machine: str, unsupported: bool,
) -> None:
    _platform(monkeypatch, system, machine)

    for key in EMBEDDING:
        reason = manifest.unsupported_reason(key)
        assert (reason is not None) is unsupported, key
        if unsupported:
            assert "PyTorch" in reason


def test_only_the_embedding_models_depend_on_the_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    for system, machine in (("darwin", "x86_64"), ("win32", "ARM64")):
        _platform(monkeypatch, system, machine)
        assert {k for k in manifest.FEATURES if manifest.unsupported_reason(k)} == set(EMBEDDING)


def test_docker_is_suggested_where_the_image_runs_natively(monkeypatch: pytest.MonkeyPatch) -> None:
    """The image is linux/amd64: native on an Intel Mac's Docker, emulated on
    Windows on ARM, where the hint would send the user somewhere slow."""
    _platform(monkeypatch, "darwin", "x86_64")
    assert "Docker install" in manifest.unsupported_reason("embedding.me5")

    _platform(monkeypatch, "win32", "ARM64")
    assert "Docker" not in manifest.unsupported_reason("embedding.me5")


def test_an_unknown_feature_raises() -> None:
    with pytest.raises(KeyError):
        manifest.unsupported_reason("nope")


def test_unavailable_features_skips_an_installed_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever put an unsupported feature on disk worked; the check is there
    to keep pip from trying the impossible, not to switch it off."""
    _platform(monkeypatch, "darwin", "x86_64")
    monkeypatch.setattr(manifest, "is_installed", lambda key: key == "embedding.gemma")

    unavailable = manifest.unavailable_features()

    assert set(unavailable) == {"embedding.me5"}
    assert "PyTorch" in unavailable["embedding.me5"]


def test_nothing_is_unavailable_where_pytorch_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    _platform(monkeypatch, "linux", "x86_64")
    probed: list[str] = []
    monkeypatch.setattr(manifest, "is_installed", lambda key: probed.append(key) or False)

    assert manifest.unavailable_features() == {}
    # Only a feature with a reason is ever probed.
    assert probed == []
