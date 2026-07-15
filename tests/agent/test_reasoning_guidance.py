"""The reasoning agent gates the ``reasoning`` think-tool (and its system-prompt
guidance) on the active model's native-reasoning capability.

- Non-reasoning model  -> ``reasoning`` tool kept, REASONING STEP block injected.
- Native-reasoning model -> ``reasoning`` tool dropped, no REASONING STEP block.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

import app.agent.reasoning_agent as ra  # noqa: E402


class _FakeTool:
    def __init__(self, tool_id: str) -> None:
        self.tool_id = tool_id


class _FakeRegistry:
    def __init__(self, tools) -> None:
        self._tools = tools

    def tools_for_profile(self, profile):
        return list(self._tools)


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


def _build(monkeypatch, provider_name, model_name):
    monkeypatch.setattr(ra, "resolve_agent_config", lambda profile: _fake_cfg())
    monkeypatch.setattr(ra, "read_persona_file", lambda profile: "PERSONA")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")
    monkeypatch.setattr(ra, "get_context", lambda *a, **k: None)

    llm = SimpleNamespace(provider_name=provider_name, model_name=model_name)
    registry = _FakeRegistry([_FakeTool("reasoning"), _FakeTool("calc")])
    return ra.ReasoningAgent(llm=llm, registry=registry, profile="default", context_id="ctx")


def test_non_reasoning_model_keeps_tool_and_injects_guidance(monkeypatch):
    # "fake/fake-model" is in no catalog -> treated as non-reasoning.
    agent = _build(monkeypatch, "fake", "fake-model")
    assert "reasoning" in agent._tools_by_id
    assert agent._inject_reasoning_guidance is True
    assert "REASONING STEP" in agent._build_instruction()


def test_reasoning_model_drops_tool_and_omits_guidance(monkeypatch):
    # openai/o3 is flagged supports_reasoning in the catalog.
    agent = _build(monkeypatch, "openai", "o3")
    assert "reasoning" not in agent._tools_by_id
    assert "calc" in agent._tools_by_id  # other tools untouched
    assert agent._inject_reasoning_guidance is False
    assert "REASONING STEP" not in agent._build_instruction()


def test_event_run_appends_events_mechanism_note(monkeypatch):
    # The "how event runs work" note (isolated run, no shared history) is appended
    # ONLY on event runs. On an ordinary run it is absent, so the chat/instant/plan
    # cache prefix stays byte-identical (the note lives in EVENT_RUN_GUIDANCE, a
    # disjoint cache population, not in SYSTEM_TEMPLATE).
    agent = _build(monkeypatch, "fake", "fake-model")

    agent._event_run = False
    assert "this run is isolated" not in agent._build_instruction()

    agent._event_run = True
    prompt = agent._build_instruction()
    assert "this run is isolated" in prompt
    assert "AUTOMATED EVENT RUN" in prompt  # the note rides inside the event block


def test_profile_and_agent_name_rendered_into_prompt(monkeypatch):
    # $CREMIND_PROFILE / $CREMIND_AGENT_NAME in SYSTEM_TEMPLATE are resolved to
    # runtime values (not left as literal tokens) when the prompt is built.
    import app.utils.agent_name as an
    monkeypatch.setattr(an, "read_agent_name", lambda profile: "Aria")
    agent = _build(monkeypatch, "fake", "fake-model")
    prompt = agent._build_instruction()
    assert "Active profile: default" in prompt   # $CREMIND_PROFILE resolved
    assert "Your name: Aria" in prompt            # $CREMIND_AGENT_NAME resolved
    assert "$CREMIND_" not in prompt              # no literal token leaked


# ── fallback-search guidance ──────────────────────────────────────────────
#
# The agent injects a "when the tools can't fulfil it, search first" block that
# names ONLY the search tools enabled for the run. The function names are derived
# from each tool's own class (imported from app.tools.builtin) plus the live
# group's registry tool_id -- never hard-coded in reasoning_agent.

# Function names the model actually sees. Note the memory tool registers as
# ``memory_search`` (slug of "Memory Search"), so its function is
# ``memory_search__search_memory`` -- NOT a bare ``search_memory``.
_DOC_FN = "documentation_search__search_documentation"
_MEM_FN = "memory_search__search_memory"
_WEB_FN = "web_search__search_web"


def _grp(config_name, tool_id):
    """Stand-in for a live built-in search tool group. The helper matches it to a
    tool class by ``config_name`` (== the class's defining module) and builds the
    exposed name from ``tool_id`` + the real class ``name``."""
    return SimpleNamespace(config_name=config_name, tool_id=tool_id)


# (module stem == config_name, registry tool_id) for the three search tools.
_DOC = ("documentation_search", "documentation_search")
_MEM = ("search_memory", "memory_search")
_WEB = ("web_search", "web_search")


def test_all_three_search_tools_named_with_web_fallback():
    g = ra._build_search_guidance([_grp(*_DOC), _grp(*_MEM), _grp(*_WEB)])
    # The guidance's characteristic instruction: don't claim a request can/can't
    # be fulfilled — verify via the search tools first.
    assert "must not affirm whether" in g
    assert _DOC_FN in g
    assert _MEM_FN in g
    assert _WEB_FN in g
    assert "search the public internet" in g


def test_web_search_disabled_omits_internet_fallback():
    g = ra._build_search_guidance([_grp(*_DOC), _grp(*_MEM)])
    assert _DOC_FN in g
    assert _MEM_FN in g
    assert _WEB_FN not in g
    assert "public internet" not in g


def test_search_memory_disabled_not_named():
    g = ra._build_search_guidance([_grp(*_DOC), _grp(*_WEB)])
    assert _DOC_FN in g
    assert _WEB_FN in g
    assert _MEM_FN not in g


def test_no_search_tools_omits_block():
    assert ra._build_search_guidance([_grp("calc", "calc")]) == ""


def test_agent_wires_search_guidance_into_prompt(monkeypatch):
    # End-to-end: __init__ computes _search_guidance from the live tools and
    # _build_instruction injects it. openai/o3 is native-reasoning so the
    # REASONING STEP block is absent and only the search block is present.
    monkeypatch.setattr(ra, "resolve_agent_config", lambda profile: _fake_cfg())
    monkeypatch.setattr(ra, "read_persona_file", lambda profile: "PERSONA")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")
    monkeypatch.setattr(ra, "get_context", lambda *a, **k: None)

    llm = SimpleNamespace(provider_name="openai", model_name="o3")
    registry = _FakeRegistry([_grp(*_DOC), _grp(*_MEM), _grp(*_WEB)])
    agent = ra.ReasoningAgent(llm=llm, registry=registry, profile="default", context_id="ctx")

    prompt = agent._build_instruction()
    assert _DOC_FN in prompt and _MEM_FN in prompt and _WEB_FN in prompt
    assert "REASONING STEP" not in prompt


def test_search_tool_classes_resolve():
    # Guards the lazy imports: a rename/move of any search tool class would make
    # these resolve to the wrong set (or drop one), failing loudly here.
    from app.tools.builtin.documentation_search import DocumentationSearchTool
    from app.tools.builtin.search_memory import SearchMemoryTool
    from app.tools.builtin.web_search import WebSearchTool

    local, web = ra._search_tool_classes()
    assert local == [DocumentationSearchTool, SearchMemoryTool]
    assert web is WebSearchTool


def test_exposed_names_match_real_tool_definitions():
    # Faithful drift guard: build group stand-ins with tool_id = slugify(SERVER_NAME)
    # exactly as registration does, and confirm the guidance emits the documented
    # function names. Catches a rename of SERVER_NAME (changes the tool_id) or of a
    # class ``name`` (changes the leaf, surfaced via _search_tool_classes()).
    from app.tools.ids import slugify
    from app.tools.builtin import (
        documentation_search as d,
        search_memory as m,
        web_search as w,
    )

    groups = [
        _grp("documentation_search", slugify(d.SERVER_NAME)),
        _grp("search_memory", slugify(m.SERVER_NAME)),
        _grp("web_search", slugify(w.SERVER_NAME)),
    ]
    g = ra._build_search_guidance(groups)
    assert _DOC_FN in g
    assert _MEM_FN in g
    assert _WEB_FN in g
