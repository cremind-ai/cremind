"""Stability tests for the catalog code generator.

The generator is deterministic: re-running it on the master TOML must
produce byte-identical outputs. CI relies on ``--check`` to fail when
the committed includes drift from the master; these tests pin the
deterministic-ness so subtle regressions (set ordering, dict ordering,
trailing newlines) show up in unit-test failures rather than mysterious
CI failures.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "install" / "scripts" / "build_catalog.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("build_catalog", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_catalog"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_build_is_deterministic() -> None:
    """Two builds in a row produce identical outputs."""
    mod = _load_module()
    first = mod.build()
    second = mod.build()
    assert first == second


def test_committed_includes_match_master() -> None:
    """The committed _catalog.* files match the master TOML.

    Equivalent to running ``python install/scripts/build_catalog.py --check``
    inside a pytest run, so CI catches stale includes whether the
    operator forgets to re-run the generator or the test suite.
    """
    mod = _load_module()
    digest, bash_text, ps_text, json_text, pkg_toml_text, ui_json_text = mod.build()
    pairs = [
        (mod.OUT_SH, bash_text),
        (mod.OUT_PS1, ps_text),
        (mod.OUT_JSON, json_text),
        (mod.OUT_PKG_TOML, pkg_toml_text),
        (mod.OUT_UI_JSON, ui_json_text),
    ]
    stale: list[str] = []
    for path, expected in pairs:
        actual = path.read_text(encoding="utf-8") if path.exists() else ""
        if actual != expected:
            stale.append(str(path))
    assert not stale, (
        f"Catalog includes are stale (re-run: python install/scripts/build_catalog.py): "
        + ", ".join(stale)
    )


def test_bash_include_quotes_special_chars() -> None:
    """Em-dashes (UTF-8) survive the bash quoter; $ and \" are escaped."""
    mod = _load_module()
    assert mod._bash_quote("a—b") == '"a—b"'
    assert mod._bash_quote('say "hi"') == '"say \\"hi\\""'
    assert mod._bash_quote("$VAR") == '"\\$VAR"'
    assert mod._bash_quote("back`tick") == '"back\\`tick"'


def test_ps_quote_escapes_single_quotes() -> None:
    mod = _load_module()
    assert mod._ps_quote("don't") == "'don''t'"


def test_bash_quote_rejects_newlines() -> None:
    mod = _load_module()
    with pytest.raises(ValueError):
        mod._bash_quote("line1\nline2")
