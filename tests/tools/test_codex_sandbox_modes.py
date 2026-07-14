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
