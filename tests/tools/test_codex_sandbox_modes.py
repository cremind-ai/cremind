"""Unit tests for the ``codex`` sandbox-mode listing.

Covers ``codex_runner.list_sandbox_modes`` — introspecting the installed Codex
SDK's ``Sandbox`` enum, the SDK-missing and unintrospectable paths, and the
``get_variable_options`` hook returning both the model and sandbox option lists
with friendly labels.

A fake ``openai_codex`` is injected into ``sys.modules`` so nothing depends on
the real SDK being installed. Coroutines are driven with ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import enum
import sys
import types

import pytest

import app.tools.builtin.codex as codex
import app.tools.builtin.codex_runner as runner
from app.tools.builtin.codex_runner import Var


_SDK_MODES = ["read-only", "workspace-write", "full-access"]


def _install_sdk(monkeypatch, *, sandbox_values=None):
    """Install a minimal fake ``openai_codex``.

    ``sandbox_values`` becomes the ``Sandbox`` enum's members; passing None
    leaves the module without a ``Sandbox`` (unintrospectable).
    """
    mod = types.ModuleType("openai_codex")
    if sandbox_values is not None:
        mod.Sandbox = enum.Enum(
            "Sandbox", {v.replace("-", "_"): v for v in sandbox_values}, type=str,
        )
    monkeypatch.setitem(sys.modules, "openai_codex", mod)
    return mod


# ── list_sandbox_modes: introspection paths ──────────────────────────────────

def test_list_sandbox_modes_from_sdk_enum(monkeypatch):
    _install_sdk(monkeypatch, sandbox_values=_SDK_MODES)
    out = runner.list_sandbox_modes()
    assert out["modes"] == _SDK_MODES
    assert out["source"] == "openai_codex"
    assert out["error"] is None


def test_list_sandbox_modes_sdk_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai_codex", None)
    out = runner.list_sandbox_modes()
    assert out["modes"] == []
    assert out["source"] is None
    assert "cremind features install codex" in out["error"]


def test_list_sandbox_modes_unintrospectable_sdk(monkeypatch):
    _install_sdk(monkeypatch)  # no Sandbox enum
    out = runner.list_sandbox_modes()
    assert out["modes"] == []
    assert out["source"] == "openai_codex"
    assert "does not expose" in out["error"]


# ── get_variable_options hook: both variables + labels ──────────────────────

def _stub_empty_models(monkeypatch):
    async def _empty(variables, profile, *, force_refresh=False):
        return {"models": [], "error": "no creds", "source": None}

    monkeypatch.setattr(runner, "list_models", _empty)


def test_hook_returns_both_variables(monkeypatch):
    _stub_empty_models(monkeypatch)
    _install_sdk(monkeypatch, sandbox_values=_SDK_MODES)
    out = asyncio.run(codex.get_variable_options(variables={}, profile="admin"))
    assert Var.MODEL in out
    assert Var.SANDBOX in out
    mode_ids = [o["id"] for o in out[Var.SANDBOX]["options"]]
    assert mode_ids == _SDK_MODES
    assert out[Var.SANDBOX]["source"] == "openai_codex"
    assert out[Var.SANDBOX]["error"] is None


def test_hook_sandbox_labels_with_raw_fallback(monkeypatch):
    _stub_empty_models(monkeypatch)
    _install_sdk(monkeypatch, sandbox_values=["full-access", "new-mode"])
    out = asyncio.run(codex.get_variable_options(variables={}, profile="admin"))
    labels = {o["id"]: o["label"] for o in out[Var.SANDBOX]["options"]}
    # A known mode gets its friendly label...
    assert labels["full-access"] == runner._SANDBOX_LABELS["full-access"]
    # ...an unknown future mode falls back to its raw id.
    assert labels["new-mode"] == "new-mode"


def test_hook_sandbox_empty_when_sdk_missing(monkeypatch):
    _stub_empty_models(monkeypatch)
    monkeypatch.setitem(sys.modules, "openai_codex", None)
    out = asyncio.run(codex.get_variable_options(variables={}, profile="admin"))
    assert out[Var.SANDBOX]["options"] == []
    assert "install" in out[Var.SANDBOX]["error"].lower()


# ── sandbox_autonomy: classification + advisory (pure; no SDK) ───────────────

@pytest.mark.parametrize(
    "mode,expected",
    [
        ("full-access", "autonomous"),
        ("workspace-write", "edits_only"),
        ("read-only", "blocked"),
        ("new-mode", "unknown"),
        (None, "unknown"),
        ("", "unknown"),
    ],
)
def test_sandbox_autonomy_classification(mode, expected):
    assert runner.sandbox_autonomy(mode) == expected


def test_sandbox_advisory_none_for_autonomous():
    assert runner._sandbox_advisory("full-access") is None


def test_sandbox_advisory_blocked_carries_exact_command():
    adv = runner._sandbox_advisory("read-only")
    assert adv is not None
    assert adv["autonomy"] == "blocked"
    assert adv["command"] == "cremind tools set-var codex CODEX_SANDBOX=full-access"
    assert "ask once" in adv["remediation"].lower()


def test_sandbox_advisory_unknown_never_claims_blocked():
    adv = runner._sandbox_advisory("someFutureSandbox")
    assert adv is not None
    assert adv["autonomy"] == "unknown"
    assert "someFutureSandbox" in adv["warning"]


def test_sandbox_advisory_edits_only_is_milder():
    adv = runner._sandbox_advisory("workspace-write")
    assert adv is not None
    assert adv["autonomy"] == "edits_only"
    assert adv["command"].endswith("CODEX_SANDBOX=full-access")


# ── resolve_sandbox: effective value the run actually uses ───────────────────

def test_resolve_sandbox_valid_value_unchanged(monkeypatch):
    mod = _install_sdk(monkeypatch, sandbox_values=_SDK_MODES)
    assert runner.resolve_sandbox(mod, {"CODEX_SANDBOX": "read-only"}) == ("read-only", None)


def test_resolve_sandbox_empty_defaults_full_access(monkeypatch):
    mod = _install_sdk(monkeypatch, sandbox_values=_SDK_MODES)
    assert runner.resolve_sandbox(mod, {}) == ("full-access", None)


def test_resolve_sandbox_garbage_falls_open_with_note(monkeypatch):
    mod = _install_sdk(monkeypatch, sandbox_values=_SDK_MODES)
    effective, note = runner.resolve_sandbox(mod, {"CODEX_SANDBOX": "readonly"})
    assert effective == "full-access"
    assert note and "readonly" in note


def test_resolve_sandbox_config_override_wins(monkeypatch):
    mod = _install_sdk(monkeypatch, sandbox_values=_SDK_MODES)
    effective, note = runner.resolve_sandbox(
        mod, {"CODEX_SANDBOX": "full-access", "CODEX_CONFIG_OVERRIDES": "sandbox_mode=read-only"},
    )
    assert effective == "read-only"
    assert note is None


def test_config_override_sandbox_parsing(monkeypatch):
    assert runner._config_override_sandbox({"CODEX_CONFIG_OVERRIDES": "a=1, sandbox_mode=read-only, b=2"}) == "read-only"
    assert runner._config_override_sandbox({"CODEX_CONFIG_OVERRIDES": "model_reasoning_effort=high"}) is None
    assert runner._config_override_sandbox({}) is None
