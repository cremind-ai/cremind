"""The reasoning agent gates the ``reasoning`` think-tool (and its system-prompt
guidance) on the active model's native-reasoning capability.

- Non-reasoning model  -> ``reasoning`` tool kept, REASONING STEP block injected.
- Native-reasoning model -> ``reasoning`` tool dropped, no REASONING STEP block.
"""

from __future__ import annotations

import re
from pathlib import Path
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
    # openai/gpt-6-astra is flagged supports_reasoning in the catalog.
    agent = _build(monkeypatch, "openai", "gpt-6-astra")
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


# ── runtime-environment line ──────────────────────────────────────────────
#
# "Am I running in Docker?", "is the VNC desktop on?", "is this the dev
# channel?" are questions the agent used to send to the CLI (or guess at). The
# facts are fixed for the life of the process, so one line of the cached system
# prompt answers them for free.

# Everything the description reads that could otherwise leak in from the
# developer machine (or the CI container) running the suite.
_ENV_KEYS = (
    "INSTALL_MODE", "CREMIND_IMAGE_FLAVOR", "CREMIND_UPGRADE_CHANNEL", "ENV",
    "SETUP_WIZARD_ENV", "CREMIND_ELECTRON_PARENT", "CREMIND_SUPERVISED",
    "VNC_PASSWORD",
)


@pytest.fixture
def clean_runtime_env(monkeypatch):
    """An uncached, environment-free runtime description for one case.

    The description is memoised per process, so a case that sets env vars must
    clear it on the way IN and on the way OUT — otherwise this file's other
    prompt tests would inherit a fake Docker install. The container marker is
    pointed at a path that cannot exist because CI itself may run in a
    container, which would flip every native assertion on the build machine and
    nowhere else.
    """
    from app.config import runtime_env

    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(runtime_env, "_CONTAINER_MARKER", Path("/nonexistent/.dockerenv"))
    runtime_env.describe_runtime_environment.cache_clear()
    yield runtime_env
    runtime_env.describe_runtime_environment.cache_clear()


def test_runtime_environment_rendered_into_prompt(monkeypatch, clean_runtime_env):
    monkeypatch.setenv("INSTALL_MODE", "docker")
    monkeypatch.setenv("CREMIND_IMAGE_FLAVOR", "desktop")
    monkeypatch.setenv("CREMIND_UPGRADE_CHANNEL", "test")
    clean_runtime_env.describe_runtime_environment.cache_clear()

    prompt = _build(monkeypatch, "fake", "fake-model")._build_instruction()

    assert "Runtime environment:" in prompt
    assert "Docker install" in prompt
    assert "VNC" in prompt
    assert "test release channel" in prompt
    # The line is a line: the next template row must not be glued onto it.
    assert "\nCurrent User Working Directory:" in prompt


def test_runtime_environment_absent_safe(monkeypatch, clean_runtime_env):
    # A dev checkout sets none of the installer's variables. The line must still
    # render (the prompt is built on every turn — a raise here breaks every
    # conversation) and must not invent a Docker/VNC install out of nothing.
    prompt = _build(monkeypatch, "fake", "fake-model")._build_instruction()

    assert "Runtime environment: native install" in prompt
    assert "VNC" not in prompt


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
    # _build_instruction injects it. openai/gpt-6-astra is native-reasoning so the
    # REASONING STEP block is absent and only the search block is present.
    monkeypatch.setattr(ra, "resolve_agent_config", lambda profile: _fake_cfg())
    monkeypatch.setattr(ra, "read_persona_file", lambda profile: "PERSONA")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")
    monkeypatch.setattr(ra, "get_context", lambda *a, **k: None)

    llm = SimpleNamespace(provider_name="openai", model_name="gpt-6-astra")
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


# ── skill-authoring clause ↔ change_working_directory's live enum ─────────────
def _skill_creator_group(tool_id: str = "default__skill_creator"):
    """The enabled skill-creator skill as the registry exposes it."""
    from app.tools import ToolType

    return SimpleNamespace(tool_type=ToolType.SKILL, tool_id=tool_id)


def _authoring_clause(*delegates: str) -> str:
    """The delegation guidance including the skill-authoring clause."""
    groups = [
        SimpleNamespace(config_name=name, tool_id=name) for name in (delegates or ("claude_code",))
    ]
    return ra._build_coding_delegation_guidance(groups + [_skill_creator_group()])


def _offered_targets(monkeypatch, loaded) -> list:
    """The ``target`` enum ``change_working_directory`` really offers once
    ``loaded`` skills are in the conversation — the state path (b) is reached in."""
    import app.tools.builtin.change_working_directory as cwd

    monkeypatch.setattr(
        cwd,
        "_get_skill_row",
        lambda skill_id: {"name": skill_id, "tool_type": "skill", "source": "/s"},
    )
    ctx = "clause-enum-ctx"
    cwd.set_context(ctx, cwd.LOADED_SKILLS_KEY, list(loaded))
    tools = [{
        "function": {
            "name": "change_working_directory",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "enum": list(cwd._TARGETS),
                        "description": "base",
                    }
                },
            },
        }
    }]
    try:
        prepared = cwd.get_prepare_tools()("q", tools, context_id=ctx, profile="default")
        return prepared[0]["function"]["parameters"]["properties"]["target"]["enum"]
    finally:
        cwd.clear_context(ctx, cwd.LOADED_SKILLS_KEY)


@pytest.mark.parametrize(
    "loaded",
    [
        ["default__skill_creator"],  # arrived via skill-creator's own SKILL.md
        ["default__gmail"],          # any other skill loaded narrows it too
    ],
)
def test_skill_authoring_clause_names_a_target_the_tool_still_offers(monkeypatch, loaded):
    # Path (b) is reachable with a skill already loaded, and prepare_tools drops
    # the generic 'skills' target in exactly that state, so a clause that only
    # named 'skills' would tell the model to emit an enum member it is not
    # offered. Whatever the clause names must survive the narrowing.
    named = set(re.findall(r"target='([a-z_]+)'", _authoring_clause()))
    offered = _offered_targets(monkeypatch, loaded)
    assert "skills" not in offered, "the narrowing this clause has to live with is gone"
    assert named & set(offered), (
        f"the clause only names withdrawn targets {sorted(named)}; "
        f"change_working_directory offers {offered}"
    )
    assert "custom" in named, "the fallback must be the one target never narrowed away"


def test_skill_authoring_clause_does_not_hard_require_the_withdrawn_target():
    # 'skills' may still be named as the shortcut it is — the tool offers it
    # whenever nothing is loaded — but never as a step the model must run first.
    text = _authoring_clause()
    assert "FIRST call `change_working_directory` with target='skills'" not in text
    sentence = next(s for s in text.split(". ") if "target='skills'" in s)
    assert "offered" in sentence and "withdrawn" in sentence, (
        f"the sentence naming 'skills' must say it can be absent: {sentence!r}"
    )
    # The cwd-independent definition of the root, matching skill-creator's SKILL.md.
    assert "PARENT" in text


def test_skill_authoring_clause_is_byte_stable_across_skill_loads(monkeypatch):
    # The clause rides the prompt-cached system message, so it may depend on the
    # ENABLED tool set only. Keying it off the loaded set would rewrite the cached
    # prefix on every skill load — which is why (b) states the rule for both
    # states instead of rendering whichever target is live.
    import app.tools.builtin.change_working_directory as cwd

    before = _authoring_clause()
    ctx = "clause-stable-ctx"
    cwd.set_context(ctx, cwd.LOADED_SKILLS_KEY, ["default__skill_creator", "default__gmail"])
    monkeypatch.setattr(ra, "get_context", lambda *a, **k: ["default__skill_creator"])
    try:
        assert _authoring_clause() == before
    finally:
        cwd.clear_context(ctx, cwd.LOADED_SKILLS_KEY)
