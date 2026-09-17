"""Unit tests for the ``claude_code`` effort level (``CLAUDE_CODE_EFFORT``).

Covers the three places the variable lives: the schema (``TOOL_CONFIG`` +
``VAR_DEFAULTS``, where it is a strict SDK-listed dropdown), the listing
(``_effort_level_ids`` / ``list_effort_levels`` reading the installed Claude
Agent SDK's ``EffortLevel`` Literal, with a fallback to the
``ClaudeAgentOptions.effort`` type hint for an SDK that predates the export),
and the run (``build_options`` forwarding a listed level and dropping anything
else). The options hook is checked for the SDK's order and the friendly labels.

Ultracode is covered alongside: the Cremind-declared choice appended LAST to
the dropdown when the SDK accepts ``xhigh`` (``effort_choices``), its resolution
to ``xhigh`` plus the ultracode flag (``_resolve_effort``), and the run carrying
``--settings {"ultracode": true, "enableWorkflows": true}`` with ``Workflow``
added to an allowlist.

A fake ``claude_agent_sdk`` is injected into ``sys.modules`` for everything but
the last tests, which pin the listing and the built CLI command against the real
SDK when it is installed (the transport is constructed, never connected).

NOTE: no ``from __future__ import annotations`` here — the fallback tests rely on
``ClaudeAgentOptions.effort`` carrying a real ``Literal`` / union object (not a
string) so ``typing.get_type_hints`` resolves it without globals.
"""

import asyncio
import dataclasses
import inspect
import json
import sys
import types
import typing
from dataclasses import dataclass
from typing import Any, Literal, Optional

import pytest

import app.tools.builtin.claude_code as claude_code
import app.tools.builtin.claude_code_runner as runner
from app.tools.builtin.claude_code_runner import Var


_SDK_LEVELS = ["low", "medium", "high", "xhigh", "max"]

# Sentinel for "this SDK's ClaudeAgentOptions has no ``effort`` field at all".
_NO_FIELD = object()


def _install_sdk(monkeypatch, *, effort_literal=None, effort_hint=_NO_FIELD, options=True):
    """Install a minimal fake ``claude_agent_sdk``.

    ``effort_literal`` sets the top-level ``EffortLevel`` alias; ``effort_hint``
    adds an ``effort`` field with that annotation to ``ClaudeAgentOptions``
    (left out, the options class predates the field). ``options=False`` leaves
    ``ClaudeAgentOptions`` off the module entirely.
    """
    mod = types.ModuleType("claude_agent_sdk")
    if effort_literal is not None:
        mod.EffortLevel = effort_literal
    if options:
        if effort_hint is _NO_FIELD:
            @dataclass
            class ClaudeAgentOptions:
                cwd: Any = None
                permission_mode: Any = None
                model: Any = None
                env: Any = None
        else:
            @dataclass
            class ClaudeAgentOptions:
                cwd: Any = None
                permission_mode: Any = None
                model: Any = None
                env: Any = None
                effort: effort_hint = None

        mod.ClaudeAgentOptions = ClaudeAgentOptions
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return mod


def _install_run_sdk(monkeypatch, *, levels=_SDK_LEVELS, settings=True):
    """A fake SDK whose options class also has the fields ultracode touches.

    ``settings=False`` models an SDK whose ``ClaudeAgentOptions`` predates the
    ``settings`` field. The effort field is typed with a real ``Literal`` so the
    levels resolve from the hint as well as from the ``EffortLevel`` export.
    """
    literal = Literal[tuple(levels)]
    fields = [
        ("cwd", Any, None),
        ("permission_mode", Any, None),
        ("model", Any, None),
        ("env", Any, None),
        ("allowed_tools", Any, None),
        ("disallowed_tools", Any, None),
        ("effort", Optional[literal], None),
    ]
    if settings:
        fields.append(("settings", Optional[str], None))
    mod = types.ModuleType("claude_agent_sdk")
    mod.EffortLevel = literal
    mod.ClaudeAgentOptions = dataclasses.make_dataclass("ClaudeAgentOptions", fields)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return mod


def _build(sdk, effort, **extra):
    return runner.build_options(
        sdk,
        cwd="/work",
        variables=runner.merge_variables({Var.EFFORT: effort, **extra}),
        model=None,
        session_id=None,
        env_overrides={},
        on_stderr=None,
    )


# ── schema ───────────────────────────────────────────────────────────────────

def test_effort_defaults_to_empty():
    # Empty = Claude Code's own default; the run passes no --effort at all.
    assert runner.VAR_DEFAULTS[Var.EFFORT] == ""
    assert Var.EFFORT == "CLAUDE_CODE_EFFORT"


def test_effort_field_is_a_strict_dynamic_dropdown():
    fields = claude_code.TOOL_CONFIG["required_config"]
    spec = fields[Var.EFFORT]
    assert spec["type"] == "string"
    assert spec["default"] == ""
    assert spec["dynamic_options"] is True
    assert spec["options_only"] is True
    # Placed beside the other run-shaping choices, right after the permission mode.
    keys = list(fields)
    assert keys.index(Var.EFFORT) == keys.index(Var.PERMISSION_MODE) + 1


def test_only_the_effort_field_is_options_only():
    # MODEL / PERMISSION_MODE keep accepting a typed value in the UI.
    fields = claude_code.TOOL_CONFIG["required_config"]
    strict = sorted(k for k, spec in fields.items() if spec.get("options_only"))
    assert strict == [Var.EFFORT]


# ── _effort_level_ids / list_effort_levels ───────────────────────────────────

def test_effort_ids_from_exported_alias(monkeypatch):
    sdk = _install_sdk(monkeypatch, effort_literal=Literal[tuple(_SDK_LEVELS)])
    assert runner._effort_level_ids(sdk) == _SDK_LEVELS  # SDK order preserved


@pytest.mark.parametrize(
    "hint",
    [
        Optional[Literal["low", "medium", "high"]],
        Literal["low", "medium", "high"] | int | None,
        Literal["low", "medium", "high"],
    ],
    ids=["optional-literal", "literal-int-none-union", "bare-literal"],
)
def test_effort_ids_from_options_hint_without_the_export(monkeypatch, hint):
    # An SDK older than the package-root export names the levels only here;
    # the int / None members of a union are skipped, not fatal.
    sdk = _install_sdk(monkeypatch, effort_hint=hint)
    assert runner._effort_level_ids(sdk) == ["low", "medium", "high"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # ClaudeAgentOptions without an effort field
        {"options": False},  # no ClaudeAgentOptions at all
        {"effort_hint": Optional[str]},  # a field with no Literal to read
    ],
    ids=["no-field", "no-options-class", "untyped-field"],
)
def test_effort_ids_empty_when_nothing_to_read(monkeypatch, kwargs):
    sdk = _install_sdk(monkeypatch, **kwargs)
    assert runner._effort_level_ids(sdk) == []


def test_list_effort_levels_installed(monkeypatch):
    _install_sdk(monkeypatch, effort_literal=Literal[tuple(_SDK_LEVELS)])
    assert runner.list_effort_levels() == {
        "levels": _SDK_LEVELS,
        "source": "claude_agent_sdk",
        "error": None,
    }


def test_list_effort_levels_sdk_missing(monkeypatch):
    # A None entry in sys.modules makes `import claude_agent_sdk` raise ImportError.
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    out = runner.list_effort_levels()
    assert out["levels"] == []
    assert out["source"] is None
    assert "cremind features install claude_code" in out["error"]


def test_list_effort_levels_sdk_without_a_list(monkeypatch):
    _install_sdk(monkeypatch)
    out = runner.list_effort_levels()
    assert out["levels"] == []
    assert out["source"] == "claude_agent_sdk"
    assert out["error"] == "Installed claude_agent_sdk does not expose effort levels."


# ── build_options ────────────────────────────────────────────────────────────

def test_build_options_passes_a_listed_effort(monkeypatch):
    sdk = _install_sdk(
        monkeypatch,
        effort_literal=Literal[tuple(_SDK_LEVELS)],
        effort_hint=Optional[Literal[tuple(_SDK_LEVELS)]],
    )
    assert _build(sdk, " max ").effort == "max"


def test_build_options_drops_an_unlisted_effort(monkeypatch):
    # e.g. set with `set-var --force`, or removed by an SDK downgrade: the run
    # falls back to Claude Code's default instead of passing an unknown --effort.
    sdk = _install_sdk(
        monkeypatch,
        effort_literal=Literal[tuple(_SDK_LEVELS)],
        effort_hint=Optional[Literal[tuple(_SDK_LEVELS)]],
    )
    assert _build(sdk, "ultra").effort is None


@pytest.mark.parametrize("value", ["", "   ", None])
def test_build_options_omits_an_empty_effort(monkeypatch, value):
    sdk = _install_sdk(
        monkeypatch,
        effort_literal=Literal[tuple(_SDK_LEVELS)],
        effort_hint=Optional[Literal[tuple(_SDK_LEVELS)]],
    )
    assert _build(sdk, value).effort is None


def test_build_options_passes_through_when_the_sdk_lists_nothing(monkeypatch):
    # Nothing to validate against: the value goes through and the SDK decides.
    sdk = _install_sdk(monkeypatch, effort_hint=Optional[str])
    assert _build(sdk, "turbo").effort == "turbo"


def test_build_options_on_an_sdk_without_the_field(monkeypatch):
    # _construct_options drops a field the options class lacks.
    sdk = _install_sdk(monkeypatch)
    options = _build(sdk, "high")
    assert not hasattr(options, "effort")
    assert options.cwd == "/work"


def test_build_options_sends_no_settings_for_a_plain_level(monkeypatch):
    sdk = _install_run_sdk(monkeypatch)
    options = _build(sdk, "xhigh", **{Var.ALLOWED_TOOLS: "Read,Edit"})
    assert options.effort == "xhigh"
    assert options.settings is None  # xhigh alone is not ultracode
    assert options.allowed_tools == ["Read", "Edit"]


# ── ultracode ────────────────────────────────────────────────────────────────

def test_effort_choices_append_ultracode_last_when_the_sdk_accepts_xhigh():
    choices = runner.effort_choices(_SDK_LEVELS)
    assert [c["id"] for c in choices] == _SDK_LEVELS + ["ultracode"]
    assert choices[-1]["label"] == (
        "ultracode (xhigh effort + dynamic workflow orchestration; xhigh-capable "
        "models, workflows enabled)"
    )
    for choice in choices[:-1]:
        assert choice["label"] == runner._EFFORT_LABELS[choice["id"]]


@pytest.mark.parametrize(
    "levels",
    [["low", "medium", "high", "max"], []],
    ids=["no-xhigh", "no-levels"],
)
def test_effort_choices_omit_ultracode_without_xhigh(levels):
    # Ultracode runs at xhigh, so an SDK that cannot send xhigh cannot run it.
    assert [c["id"] for c in runner.effort_choices(levels)] == levels


def test_effort_choices_do_not_duplicate_an_sdk_listed_ultracode():
    ids = [c["id"] for c in runner.effort_choices(_SDK_LEVELS + ["ultracode"])]
    assert ids == _SDK_LEVELS + ["ultracode"]


@pytest.mark.parametrize(
    "levels, value, expected",
    [
        (_SDK_LEVELS, "ultracode", ("xhigh", True)),
        (_SDK_LEVELS, " ULTRACODE ", ("xhigh", True)),
        (["low", "medium", "high", "max"], "ultracode", (None, False)),
        ([], "ultracode", ("xhigh", True)),  # nothing to check against
        ([], "turbo", ("turbo", False)),  # nothing to check against
        (_SDK_LEVELS, "max", ("max", False)),
        (_SDK_LEVELS, " xhigh ", ("xhigh", False)),
        (_SDK_LEVELS, "ultra", (None, False)),
        (_SDK_LEVELS, "", (None, False)),
        (_SDK_LEVELS, None, (None, False)),
    ],
    ids=[
        "ultracode",
        "ultracode-any-case",
        "ultracode-sdk-without-xhigh",
        "ultracode-empty-list",
        "unknown-empty-list",
        "listed-level",
        "xhigh-is-not-ultracode",
        "unlisted-level",
        "empty",
        "none",
    ],
)
def test_resolve_effort(monkeypatch, levels, value, expected):
    if levels:
        sdk = _install_sdk(
            monkeypatch,
            effort_literal=Literal[tuple(levels)],
            effort_hint=Optional[Literal[tuple(levels)]],
        )
    else:
        sdk = _install_sdk(monkeypatch, effort_hint=Optional[str])
    assert runner._resolve_effort(sdk, value) == expected


def test_resolve_effort_forwards_an_sdk_listed_ultracode_verbatim(monkeypatch):
    # Should the SDK ever model it as a level, the SDK owns it.
    levels = _SDK_LEVELS + ["ultracode"]
    sdk = _install_sdk(monkeypatch, effort_literal=Literal[tuple(levels)])
    assert runner._resolve_effort(sdk, "ultracode") == ("ultracode", False)


def test_build_options_ultracode_sends_xhigh_and_the_settings(monkeypatch):
    sdk = _install_run_sdk(monkeypatch)
    options = _build(sdk, "ultracode")
    assert options.effort == "xhigh"
    assert json.loads(options.settings) == {"ultracode": True, "enableWorkflows": True}
    # No allowlist stays no allowlist (None = every standard tool).
    assert options.allowed_tools is None


@pytest.mark.parametrize(
    "allowed, expected",
    [
        ("Read,Edit", ["Read", "Edit", "Workflow"]),
        ("Read, Workflow ,Edit", ["Read", "Workflow", "Edit"]),
    ],
    ids=["appended", "already-listed"],
)
def test_build_options_ultracode_adds_workflow_to_an_allowlist_once(
    monkeypatch, allowed, expected
):
    sdk = _install_run_sdk(monkeypatch)
    options = _build(sdk, "ultracode", **{Var.ALLOWED_TOOLS: allowed})
    assert options.allowed_tools == expected


def test_build_options_ultracode_leaves_the_denylist_alone(monkeypatch):
    # An explicit deny wins: ultracode never un-denies the Workflow tool.
    sdk = _install_run_sdk(monkeypatch)
    options = _build(sdk, "ultracode", **{Var.DISALLOWED_TOOLS: "Workflow,Bash"})
    assert options.disallowed_tools == ["Workflow", "Bash"]
    assert options.allowed_tools is None
    assert options.effort == "xhigh"


def test_build_options_ultracode_on_an_sdk_without_xhigh(monkeypatch):
    # Dropped like any other unlisted value: default effort, no settings.
    sdk = _install_run_sdk(monkeypatch, levels=["low", "medium", "high", "max"])
    options = _build(sdk, "ultracode", **{Var.ALLOWED_TOOLS: "Read"})
    assert options.effort is None
    assert options.settings is None
    assert options.allowed_tools == ["Read"]


def test_build_options_ultracode_on_an_sdk_without_the_settings_field(monkeypatch):
    # _construct_options drops the field; the run still gets xhigh.
    sdk = _install_run_sdk(monkeypatch, settings=False)
    options = _build(sdk, "ultracode")
    assert options.effort == "xhigh"
    assert not hasattr(options, "settings")


# ── get_variable_options hook ────────────────────────────────────────────────

def _stub_empty_models(monkeypatch):
    async def _empty(variables, profile, *, force_refresh=False):
        return {"models": [], "error": "no creds", "source": None}

    monkeypatch.setattr(runner, "list_models", _empty)


def test_hook_lists_effort_levels_in_sdk_order_with_labels(monkeypatch):
    _stub_empty_models(monkeypatch)
    _install_sdk(monkeypatch, effort_literal=Literal[tuple(_SDK_LEVELS + ["turbo"])])
    out = asyncio.run(claude_code.get_variable_options(variables={}, profile="admin"))

    entry = out[Var.EFFORT]
    # The SDK's levels in its order, then ultracode last (the SDK accepts xhigh).
    assert [o["id"] for o in entry["options"]] == _SDK_LEVELS + ["turbo", "ultracode"]
    assert entry["source"] == "claude_agent_sdk"
    assert entry["error"] is None
    labels = {o["id"]: o["label"] for o in entry["options"]}
    for level in _SDK_LEVELS:
        assert labels[level] == runner._EFFORT_LABELS[level]
    # A level this build has no label for is shown as itself.
    assert labels["turbo"] == "turbo"
    assert labels["ultracode"].startswith("ultracode (xhigh effort")
    # The other two lists are still there.
    assert Var.MODEL in out and Var.PERMISSION_MODE in out


def test_hook_effort_has_no_ultracode_when_the_sdk_lacks_xhigh(monkeypatch):
    _stub_empty_models(monkeypatch)
    _install_sdk(monkeypatch, effort_literal=Literal["low", "medium", "high", "max"])
    out = asyncio.run(claude_code.get_variable_options(variables={}, profile="admin"))
    ids = [o["id"] for o in out[Var.EFFORT]["options"]]
    assert ids == ["low", "medium", "high", "max"]


def test_hook_effort_empty_when_sdk_missing(monkeypatch):
    _stub_empty_models(monkeypatch)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    out = asyncio.run(claude_code.get_variable_options(variables={}, profile="admin"))
    assert out[Var.EFFORT]["options"] == []
    assert "install" in out[Var.EFFORT]["error"].lower()


# ── the real SDK ─────────────────────────────────────────────────────────────

def test_list_effort_levels_matches_the_installed_sdk():
    sdk = pytest.importorskip("claude_agent_sdk")
    alias = getattr(sdk, "EffortLevel", None)
    if alias is None:
        pytest.skip("installed claude_agent_sdk does not export EffortLevel")
    out = runner.list_effort_levels()
    assert out["error"] is None
    assert out["source"] == "claude_agent_sdk"
    assert out["levels"] == list(typing.get_args(alias))


def test_real_sdk_builds_the_ultracode_command():
    # Pins the recipe end to end against the installed SDK: the options Cremind
    # builds turn into `--effort xhigh --settings {...ultracode...}` on the CLI
    # command line. The transport is only constructed - never connected, so no
    # CLI process is spawned.
    sdk = pytest.importorskip("claude_agent_sdk")
    transport_mod = pytest.importorskip("claude_agent_sdk._internal.transport.subprocess_cli")
    if "xhigh" not in runner._effort_level_ids(sdk):
        pytest.skip("installed claude_agent_sdk does not accept xhigh")
    if "settings" not in {f.name for f in dataclasses.fields(sdk.ClaudeAgentOptions)}:
        pytest.skip("installed claude_agent_sdk has no ClaudeAgentOptions.settings")

    options = runner.build_options(
        sdk,
        cwd="/work",
        variables=runner.merge_variables({
            Var.EFFORT: "ultracode",
            Var.ALLOWED_TOOLS: "Read,Edit",
            # A path is needed to build the command; nothing is ever run from it.
            Var.CLI_PATH: "/nonexistent/claude",
        }),
        model=None,
        session_id=None,
        env_overrides={},
        on_stderr=None,
    )
    cls = transport_mod.SubprocessCLITransport
    kwargs = {"options": options}
    if "prompt" in inspect.signature(cls.__init__).parameters:
        kwargs["prompt"] = ""
    cmd = cls(**kwargs)._build_command()

    i = cmd.index("--effort")
    assert cmd[i + 1] == "xhigh"
    settings = json.loads(cmd[cmd.index("--settings") + 1])
    assert settings["ultracode"] is True
    assert settings["enableWorkflows"] is True
    assert "Workflow" in cmd[cmd.index("--allowedTools") + 1].split(",")
