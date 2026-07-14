"""The reasoning agent renders a dynamic "built-in tools" section into the system
prompt so the model understands what each built-in tool is for and which scattered
sub-tool functions belong to it.

``_build_builtin_tools_guidance`` lists each ENABLED, non-hidden built-in group
that declares an authored ``description`` in its module ``TOOL_CONFIG`` — the
tool's purpose plus the exact leaf function names the model sees in the ``tools=``
block. It is opt-in (only described tools), GROUP-level (byte-stable within a run),
and re-rendered every turn from the live enabled set (enabled → present,
disabled → absent). The stubs use REAL module ``config_name`` values so
``get_builtin_tool_config`` returns each module's authored text, doubling as a
drift guard against a renamed description/leaf.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

import app.agent.reasoning_agent as ra  # noqa: E402


def _grp(config_name, tool_id, *, leaves=(), hidden=False, name=None):
    """Stand-in for a live built-in group. ``config_name`` matches a REAL module
    so ``get_builtin_tool_config`` returns that module's authored description;
    ``leaves`` are raw leaf names, exposed via ``make_leaf_name(tool_id, leaf)``
    exactly as the ``tools=`` block does."""
    return SimpleNamespace(
        config_name=config_name,
        tool_id=tool_id,
        name=name or config_name,
        hidden=hidden,
        skills=[SimpleNamespace(name=leaf) for leaf in leaves],
    )


# Real exec_shell leaf names -> the function names the model actually sees.
# ``exec_shell`` collapses (leaf == tool_id); the rest are namespaced.
_EXEC_LEAVES = ("exec_shell", "exec_shell_input", "exec_shell_output", "exec_shell_stop")
_EXEC_FNS = (
    "exec_shell",
    "exec_shell__exec_shell_input",
    "exec_shell__exec_shell_output",
    "exec_shell__exec_shell_stop",
)


def test_enabled_tool_present_with_description_and_function_names():
    g = ra._build_builtin_tools_guidance(
        [_grp("exec_shell", "exec_shell", leaves=_EXEC_LEAVES, name="Shell Executor")]
    )
    assert "BUILT-IN TOOLS" in g
    assert "Shell Executor" in g
    # A distinctive phrase from exec_shell's authored blurb (drift guard).
    assert "platform shell" in g
    for fn in _EXEC_FNS:
        assert f"`{fn}`" in g


def test_disabled_tool_drops_out_of_section():
    enabled = _grp("exec_shell", "exec_shell", leaves=_EXEC_LEAVES, name="Shell Executor")
    with_tool = ra._build_builtin_tools_guidance([enabled])
    assert "Shell Executor" in with_tool
    # Same run with the tool disabled (absent from the enabled snapshot): its line
    # is gone, and since it was the only described tool the section collapses to "".
    without_tool = ra._build_builtin_tools_guidance([SimpleNamespace(tool_id="calc")])
    assert "Shell Executor" not in without_tool
    assert without_tool == ""


def test_opt_in_only_unknown_module_skipped():
    # config_name with no matching module -> get_builtin_tool_config -> {} -> skipped.
    g = ra._build_builtin_tools_guidance(
        [_grp("calc", "calc", leaves=("calc",), name="Calculator")]
    )
    assert g == ""
    assert "Calculator" not in g


def test_hidden_builtin_excluded_even_if_described():
    # scheduler is hidden AND carries an authored description (for DB/CLI). It must
    # not leak into the prompt — it has its own dedicated guidance.
    g = ra._build_builtin_tools_guidance(
        [_grp("scheduler", "scheduler", leaves=("scheduler",), hidden=True, name="Scheduler")]
    )
    assert g == ""
    assert "Scheduler" not in g


def test_described_tool_with_no_leaves_omits_functions_clause():
    g = ra._build_builtin_tools_guidance(
        [_grp("search_memory", "memory_search", leaves=(), name="Memory Search")]
    )
    assert "Memory Search" in g
    assert "long-term memory" in g  # authored text
    assert "Functions:" not in g


def test_non_builtin_tools_skipped():
    # Skills / MCP / intrinsic tools have no config_name -> skipped.
    assert ra._build_builtin_tools_guidance([SimpleNamespace(tool_id="my_skill")]) == ""


def test_byte_stable_and_order_preserved():
    tools = [
        _grp("exec_shell", "exec_shell", leaves=_EXEC_LEAVES, name="Shell Executor"),
        _grp("web_search", "web_search", leaves=("search_web",), name="Web Search"),
    ]
    first = ra._build_builtin_tools_guidance(tools)
    second = ra._build_builtin_tools_guidance(tools)
    assert first == second  # deterministic
    # Input order preserved: exec_shell's line precedes web_search's line.
    assert first.index("Shell Executor") < first.index("Web Search")


# ── end-to-end wiring through the agent ────────────────────────────────────


def _fake_cfg() -> SimpleNamespace:
    return SimpleNamespace(
        max_llm_retries=0,
        reasoning_temperature=1.0,
        reasoning_max_tokens=1024,
        reasoning_retry=0,
        tool_result_enabled=False,
        tool_result_max_tokens=4096,
        enable_prompt_cache=False,
        max_steps=6,
    )


class _FakeRegistry:
    def __init__(self, tools) -> None:
        self._tools = tools

    def tools_for_profile(self, profile):
        return list(self._tools)


def test_agent_wires_builtin_tools_guidance_into_prompt(monkeypatch):
    # End-to-end: __init__ computes _builtin_tools_guidance from the live tools and
    # _build_instruction injects it. openai/o3 reasons natively, so the REASONING
    # STEP block is absent and the section is byte-identical across steps.
    monkeypatch.setattr(ra, "resolve_agent_config", lambda profile: _fake_cfg())
    monkeypatch.setattr(ra, "read_persona_file", lambda profile: "PERSONA")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")
    monkeypatch.setattr(ra, "get_context", lambda *a, **k: None)

    llm = SimpleNamespace(provider_name="openai", model_name="o3")  # native reasoning
    registry = _FakeRegistry(
        [_grp("exec_shell", "exec_shell", leaves=_EXEC_LEAVES, name="Shell Executor")]
    )
    agent = ra.ReasoningAgent(
        llm=llm, registry=registry, profile="default", context_id="ctx"
    )

    first = agent._build_instruction()
    second = agent._build_instruction()
    assert first == second  # byte-identical across steps
    assert "BUILT-IN TOOLS" in first
    assert "Shell Executor" in first
    for fn in _EXEC_FNS:
        assert f"`{fn}`" in first
