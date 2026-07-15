"""Unit tests for the ``claude_code`` permission-mode listing.

Covers ``claude_code_runner.list_permission_modes`` — introspecting the
installed Claude Agent SDK's ``PermissionMode`` Literal (with a fallback to the
``ClaudeAgentOptions.permission_mode`` type hint), the SDK-missing and
unintrospectable paths, and the ``get_variable_options`` hook now returning both
the model and permission-mode option lists with friendly labels.

A fake ``claude_agent_sdk`` is injected into ``sys.modules`` so nothing depends
on the real SDK being installed (the dev venv has it; CI may not). Coroutines
are driven with ``asyncio.run`` to match the repo's other tool tests.

NOTE: no ``from __future__ import annotations`` here — the fallback test relies
on ``ClaudeAgentOptions.permission_mode`` being a real ``Optional[Literal[...]]``
object (not a string) so ``typing.get_type_hints`` resolves it without globals.
"""

import asyncio
import sys
import types
from dataclasses import dataclass
from typing import Any, Literal, Optional

import pytest

import app.tools.builtin.claude_code as claude_code
import app.tools.builtin.claude_code_runner as runner
from app.tools.builtin.claude_code_runner import Var


_SDK_MODES = ["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"]


def _install_sdk(monkeypatch, *, permission_mode_literal=None, options_hint=None):
    """Install a minimal fake ``claude_agent_sdk``.

    ``permission_mode_literal`` sets the top-level ``PermissionMode`` alias;
    ``options_hint`` sets ``ClaudeAgentOptions.permission_mode``'s annotation
    (used only when ``PermissionMode`` is absent). Passing neither leaves the
    module unintrospectable.
    """
    mod = types.ModuleType("claude_agent_sdk")
    if permission_mode_literal is not None:
        mod.PermissionMode = permission_mode_literal
    if options_hint is not None:
        @dataclass
        class ClaudeAgentOptions:
            permission_mode: options_hint = None

        mod.ClaudeAgentOptions = ClaudeAgentOptions
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return mod


# ── list_permission_modes: introspection paths ──────────────────────────────

def test_list_permission_modes_from_sdk_literal(monkeypatch):
    _install_sdk(monkeypatch, permission_mode_literal=Literal[tuple(_SDK_MODES)])
    out = runner.list_permission_modes()
    assert out["modes"] == _SDK_MODES  # SDK Literal order preserved
    assert out["source"] == "claude_agent_sdk"
    assert out["error"] is None


def test_list_permission_modes_falls_back_to_options_hint(monkeypatch):
    # No PermissionMode alias -> read the ClaudeAgentOptions hint instead.
    _install_sdk(monkeypatch, options_hint=Optional[Literal["plan", "auto"]])
    out = runner.list_permission_modes()
    assert out["modes"] == ["plan", "auto"]
    assert out["source"] == "claude_agent_sdk"
    assert out["error"] is None


def test_list_permission_modes_sdk_missing(monkeypatch):
    # A None entry in sys.modules makes `import claude_agent_sdk` raise ImportError.
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    out = runner.list_permission_modes()
    assert out["modes"] == []
    assert out["source"] is None
    assert "cremind features install claude_code" in out["error"]


def test_list_permission_modes_unintrospectable_sdk(monkeypatch):
    _install_sdk(monkeypatch)  # neither PermissionMode nor ClaudeAgentOptions
    out = runner.list_permission_modes()
    assert out["modes"] == []
    assert out["source"] == "claude_agent_sdk"
    assert "does not expose" in out["error"]


# ── get_variable_options hook: both variables + labels ──────────────────────

def _stub_empty_models(monkeypatch):
    async def _empty(variables, profile, *, force_refresh=False):
        return {"models": [], "error": "no creds", "source": None}

    monkeypatch.setattr(runner, "list_models", _empty)


def test_hook_returns_both_variables(monkeypatch):
    _stub_empty_models(monkeypatch)
    _install_sdk(monkeypatch, permission_mode_literal=Literal[tuple(_SDK_MODES)])
    out = asyncio.run(claude_code.get_variable_options(variables={}, profile="admin"))
    assert Var.MODEL in out
    assert Var.PERMISSION_MODE in out
    mode_ids = [o["id"] for o in out[Var.PERMISSION_MODE]["options"]]
    assert mode_ids == _SDK_MODES
    assert out[Var.PERMISSION_MODE]["source"] == "claude_agent_sdk"
    assert out[Var.PERMISSION_MODE]["error"] is None


def test_hook_permission_mode_labels_with_raw_fallback(monkeypatch):
    _stub_empty_models(monkeypatch)
    _install_sdk(
        monkeypatch,
        permission_mode_literal=Literal["bypassPermissions", "newMode"],
    )
    out = asyncio.run(claude_code.get_variable_options(variables={}, profile="admin"))
    labels = {o["id"]: o["label"] for o in out[Var.PERMISSION_MODE]["options"]}
    # A known mode gets its friendly label...
    assert labels["bypassPermissions"] == runner._PERMISSION_MODE_LABELS["bypassPermissions"]
    # ...an unknown future mode falls back to its raw id.
    assert labels["newMode"] == "newMode"


def test_hook_permission_mode_empty_when_sdk_missing(monkeypatch):
    _stub_empty_models(monkeypatch)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    out = asyncio.run(claude_code.get_variable_options(variables={}, profile="admin"))
    assert out[Var.PERMISSION_MODE]["options"] == []
    assert "install" in out[Var.PERMISSION_MODE]["error"].lower()
