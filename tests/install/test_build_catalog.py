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


# ── kubernetes block ──────────────────────────────────────────────────────


def test_bash_include_renders_the_kubernetes_block() -> None:
    """install.sh reads these variable names directly."""
    mod = _load_module()
    _digest, bash_text, _ps, _json, _toml, _ui = mod.build()

    assert 'MODE_IDS="docker native kubernetes"' in bash_text
    # The requirements are the visibility gate, so the shell has to see them.
    assert 'MODE_REQUIRES_kubernetes="kubectl helm"' in bash_text
    assert 'MODE_REQUIRES_native=""' in bash_text

    assert 'K8S_NAMESPACE_DEFAULT="cremind"' in bash_text
    assert "K8S_CONTEXT_PROMPT=" in bash_text
    assert "K8S_ADVANCED_PROMPT=" in bash_text
    assert (
        'K8S_FIELD_IDS="release_name app_url legacy_postgres_image '
        'delete_postgres_data extra_set"'
    ) in bash_text
    assert 'K8S_FIELD_DEFAULT_release_name="cremind"' in bash_text
    assert 'K8S_FIELD_CHOICES_legacy_postgres_image="yes no"' in bash_text
    assert 'K8S_FIELD_DEFAULT_delete_postgres_data="no"' in bash_text
    # Free-text fields still emit an (empty) choices variable, so the shell's
    # indirect lookup never reads an unset name under `set -u`.
    assert 'K8S_FIELD_CHOICES_app_url=""' in bash_text


def test_powershell_include_renders_the_kubernetes_block() -> None:
    mod = _load_module()
    _digest, _bash, ps_text, _json, _toml, _ui = mod.build()

    assert "$script:ModeIds = @('docker', 'native', 'kubernetes')" in ps_text
    assert "Requires    = @('kubectl', 'helm')" in ps_text
    assert "$script:Kubernetes = [ordered]@{" in ps_text
    assert "NamespaceDefault = 'cremind'" in ps_text
    assert "AdvancedFields   = @(" in ps_text
    assert "Key     = 'release_name'" in ps_text
    assert "Choices = @('yes', 'no')" in ps_text
    # Emitted even when empty: Set-StrictMode faults on a missing property.
    assert "Choices = @()" in ps_text


def test_renderers_tolerate_a_catalog_without_kubernetes() -> None:
    """The generator must not require the table it renders."""
    mod = _load_module()
    bash_text = mod.render_bash({}, "deadbeef")
    ps_text = mod.render_powershell({}, "deadbeef")
    assert 'K8S_FIELD_IDS=""' in bash_text
    assert "AdvancedFields   = @(" in ps_text
