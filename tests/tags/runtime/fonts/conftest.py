"""Fixtures for the font-pack pipeline tests: a synthetic repository and the real one."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[4]


def _synthetic() -> ModuleType:
    name = "_fonts_synthetic"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("synthetic.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def synthetic() -> ModuleType:
    return _synthetic()


@pytest.fixture(scope="session")
def repo(tmp_path_factory: pytest.TempPathFactory, synthetic: ModuleType) -> Any:
    """A locked synthetic repository (no network)."""
    from app.tags.runtime.fonts.fetch import lock
    from app.tags.runtime.fonts.manifest import load_manifest
    from app.tags.runtime.fonts.notice import install_licenses

    r = synthetic.make_repo(tmp_path_factory.mktemp("fontrepo"))
    manifest = load_manifest(r.manifest)
    lk = lock(manifest, r.cache)
    install_licenses(manifest, lk, r.cache)
    return r


@pytest.fixture(scope="session")
def built(repo: Any, tmp_path_factory: pytest.TempPathFactory) -> Any:
    """The synthetic repository's full profile, built once."""
    from app.tags.runtime.fonts.build import build, plan_build
    from app.tags.runtime.fonts.manifest import load_manifest

    manifest = load_manifest(repo.manifest)
    return build(manifest, plan_build(manifest, "full"), cache_dir=repo.cache,
                 out_dir=tmp_path_factory.mktemp("out") / "full", jobs=1, strict_freetype=False)


@pytest.fixture(scope="session")
def real_cache() -> Path:
    """The checkout's verified font cache; skips when `cremind tags tools fonts fetch` has not run."""
    from app.tags.runtime.fonts.fetch import VerificationError, verify_cached
    from app.tags.runtime.fonts.manifest import ManifestError, load_lock, load_manifest

    manifest = load_manifest(REPO / "fonts" / "manifest.yaml")
    cache = REPO / "fonts" / "cache"
    try:
        verify_cached(load_lock(manifest.lock_path), cache, [f.key for f in manifest.faces])
    except (ManifestError, VerificationError) as exc:
        pytest.skip(f"font cache not available: {exc}")
    return cache
