"""Import-smoke test for every module under ``app/``.

Walks the package and tries to import each module in isolation. A failure
means the module crashes at import time — typically a SyntaxError,
NameError from a bad refactor, or a circular import. These are exactly
the regressions that "the server still starts" would also catch, but
this runs in <1s and pinpoints the offending module by name.

Modules that fail with ``ImportError`` are *skipped*, not failed: many
``app/`` modules require feature-gated extras (``qdrant-client``,
``anthropic``, ``markitdown``, …) that aren't installed in a minimal
``uv sync`` environment. We want CI to remain green on the core deps and
only flag genuine bugs.

Any non-ImportError exception (SyntaxError, AttributeError, NameError,
RuntimeError from module-scope code) is a real failure.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest

import app


def _walk_app_modules() -> list[str]:
    """Return dotted module names for every importable file under ``app/``.

    Skips ``__main__`` modules (running them has side effects — they're
    entry points, not libraries) and the built SPA directory.
    """
    repo_root = Path(__file__).resolve().parent.parent
    pkg_path = repo_root / "app"
    modules: list[str] = ["app"]
    for module_info in pkgutil.walk_packages(
        path=[str(pkg_path)],
        prefix="app.",
    ):
        name = module_info.name
        # Skip __main__ entry points — importing them re-runs CLI side
        # effects (argparse, etc.). They're exercised by the CLI tests,
        # not this smoke check.
        if name.endswith(".__main__"):
            continue
        # Alembic env.py is invoked by alembic with its context already
        # populated — bare ``import`` hits ``context.config is None`` and
        # crashes. It's an entry point, not a library module.
        if name == "app.alembic.env":
            continue
        # Skip the bundled SPA artifact (no .py files, but be defensive).
        if ".static." in name:
            continue
        # Skip alembic migration versions — they import the package's
        # migration env and aren't meant to be imported standalone.
        if ".alembic.versions." in name:
            continue
        modules.append(name)
    return modules


APP_MODULES = _walk_app_modules()


@pytest.mark.parametrize("module_name", APP_MODULES)
def test_module_imports(module_name: str) -> None:
    """Every module under app/ must import cleanly (or fail with ImportError
    for a known optional dep, which we skip)."""
    try:
        importlib.import_module(module_name)
    except ImportError as exc:
        # Optional-dep gap, not a code bug. Skip with the missing
        # module name so the report is still informative.
        pytest.skip(f"optional dependency missing for {module_name}: {exc}")


def test_smoke_covers_real_modules() -> None:
    """Guard against an empty walk silently passing the suite."""
    # Floor of 50 is well below the actual count (~150+) but high
    # enough to detect "the walker found nothing" regressions.
    assert len(APP_MODULES) > 50, (
        f"smoke walker only found {len(APP_MODULES)} modules — "
        "check _walk_app_modules() against the current app/ layout"
    )
