"""The agent side of per-conversation search-tool selection.

A conversation (or a group room) chooses which of the four search sources —
Documentation search, Cremind documentation search, Memory search, Web search —
the agent may use. The run freezes that choice (a ``search_tools.Snapshot``) at
its start, and the reasoning agent turns it into:

- the tools block: an unselected source loses its WHOLE group, after every other
  gate, and unrelated tools are untouched;
- the SEARCH SOURCES guidance: the exposed sources in priority order, naming
  only functions the run actually sends — so equal effective selections render
  byte-equal text, whatever the stored selection looked like;
- historical function names (``user_documents__*`` and the manual's old
  ``documentation_search__*``) routed to today's functions, dispatch only, and
  only while today's function is exposed;
- one cache baseline per run, recorded at the first MAIN model request — never
  for the mid-turn acknowledgement, a maintenance fold, or a run that sent
  nothing.

Profiles are independent tenants, so the cases that could leak run on two.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import re
from types import SimpleNamespace
from typing import AsyncGenerator, List

import pytest

pytest.importorskip("a2a")

import app.agent.reasoning_agent as ra  # noqa: E402
from app.agent import search_tools as st  # noqa: E402
from app.constants import ChatCompletionTypeEnum  # noqa: E402
from app.constants.status import Status  # noqa: E402
from app.lib.exception import AgentException  # noqa: E402
from app.tools.base import FunctionSpec, ToolResultEvent, ToolType, make_leaf_name  # noqa: E402


# ── fakes ──────────────────────────────────────────────────────────────────


class _Group:
    """A built-in tool group shaped like ``BuiltInToolGroup``: static ``skills``
    (what the guidance reads) and per-step ``leaf_function_specs`` (what the
    tools block is built from), both from the same leaf list."""

    tool_type = ToolType.BUILTIN

    def __init__(self, tool_id: str, *leaves: str, config_name: str | None = None,
                 hidden: bool = False) -> None:
        self.tool_id = tool_id
        self.config_name = config_name or tool_id
        self.name = tool_id
        self.hidden = hidden
        self._leaves = list(leaves)
        self.skills = [SimpleNamespace(name=leaf) for leaf in leaves]
        self.executed: list[str] = []

    def leaf_function_specs(self, *, context_id, profile, query="", arguments=None):
        out = []
        for leaf in self._leaves:
            name = make_leaf_name(self.tool_id, leaf)
            out.append(FunctionSpec(name=name, leaf_name=leaf, schema={
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"{self.tool_id}: {leaf}",
                    "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
                },
            }))
        return out

    async def execute_leaf(self, *, leaf_name, args, context_id, profile,
                           arguments, variables) -> AsyncGenerator[object, None]:
        self.executed.append(leaf_name)
        yield ToolResultEvent(observation_text=f"{self.tool_id}.{leaf_name} ran")


def _search_groups() -> list[_Group]:
    return [
        _Group("documentation_search", "find_files", "search", "read", "research"),
        _Group("cremind_documentation_search", "search_documentation", "read_documentation_section"),
        _Group("memory_search", "search_memory", config_name="search_memory"),
        _Group("web_search", "search_web"),
    ]


def _calc() -> _Group:
    return _Group("calc", "run", hidden=True)


class _Registry:
    """Per-profile tools and per-profile disabled leaves."""

    def __init__(self, by_profile: dict, disabled: dict | None = None) -> None:
        self._by_profile = by_profile
        self._disabled = disabled or {}

    def tools_for_profile(self, profile):
        return list(self._by_profile[profile])

    def disabled_leaves_by_tool(self, profile):
        return {k: set(v) for k, v in self._disabled.get(profile, {}).items()}


class _ScriptedLLM:
    """Scripted main-model answers; every call (the mid-turn acknowledgement
    included) is recorded. An acknowledgement call is recognised by its
    trailing ``[Pause`` request and answered ``SKIP``.

    Script entries: ``("tool", fn_name)``, ``("text", text)``,
    ``("raise", AgentException)``, or a callable run before answering text.
    """

    provider_name = "fake"
    model_name = "fake-model"
    model_label = "Fake fake-model"

    def __init__(self, script: list, events: list | None = None, on_call=None) -> None:
        self._script = list(script)
        self.calls: list[dict] = []
        self.events = events if events is not None else []
        self._on_call = on_call

    async def chat_completion_stream(self, *, messages, tools=None, tool_choice=None, **kwargs):
        last = messages[-1].get("content") if messages else ""
        is_ack = isinstance(last, str) and last.startswith("[Pause")
        self.calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice,
                           "ack": is_ack, **kwargs})
        self.events.append("ack" if is_ack else "main")
        if is_ack:
            yield {"type": ChatCompletionTypeEnum.CONTENT, "data": "SKIP"}
            yield {"type": ChatCompletionTypeEnum.DONE, "input_tokens": 1, "output_tokens": 1}
            return
        if self._on_call is not None:
            self._on_call(len([c for c in self.calls if not c["ack"]]))
        action = self._script.pop(0)
        if action[0] == "raise":
            raise action[1]
        if action[0] == "tool":
            yield {
                "type": ChatCompletionTypeEnum.FUNCTION_CALLING,
                "data": {"function": [{"index": 0, "id": f"call_{len(self.calls)}",
                                       "name": action[1], "arguments": {"q": "x"}}]},
            }
            yield {"type": ChatCompletionTypeEnum.DONE, "input_tokens": 5,
                   "output_tokens": 2, "finish_reason": "tool_calls"}
            return
        yield {"type": ChatCompletionTypeEnum.CONTENT, "data": action[1]}
        yield {"type": ChatCompletionTypeEnum.DONE, "input_tokens": 3,
               "output_tokens": 1, "finish_reason": "stop"}


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        max_llm_retries=0, reasoning_temperature=1.0, reasoning_max_tokens=1024,
        reasoning_retry=0, tool_result_enabled=False, tool_result_max_tokens=4096,
        enable_prompt_cache=False, max_steps=6,
    )


@pytest.fixture
def env(monkeypatch):
    """Everything the constructor and the loop read from outside, pinned."""
    import app.documents.gate as gate

    monkeypatch.setattr(ra, "resolve_agent_config", lambda p: _cfg())
    monkeypatch.setattr(ra, "read_persona_file", lambda p: "PERSONA")
    monkeypatch.setattr(ra, "read_instructions_file", lambda p: "")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda: "/work")
    monkeypatch.setattr(ra, "get_context", lambda *a, **k: None)

    async def _no_ltm(self):
        return ""

    monkeypatch.setattr(ra.ReasoningAgent, "_load_long_term_memory_block", _no_ltm)
    ra._LONG_TERM_MEMORY_SNAPSHOT.clear()
    state = SimpleNamespace(gate_open=True)
    monkeypatch.setattr(gate, "documents_tool_available", lambda p, o: state.gate_open)
    yield state
    ra._LONG_TERM_MEMORY_SNAPSHOT.clear()


def _agent(*, registry=None, profile="alice", snapshot=None, hook=None, llm=None,
           maintenance=False) -> ra.ReasoningAgent:
    registry = registry or _Registry({profile: _search_groups() + [_calc()]})
    agent = ra.ReasoningAgent(
        llm=llm or SimpleNamespace(provider_name="openai", model_name="gpt-6-astra",
                                   model_label="gpt"),
        registry=registry, profile=profile, context_id=f"ctx-{profile}",
        search_tools=snapshot, on_search_baseline=hook, maintenance=maintenance,
    )
    agent._current_query = "q"
    return agent


def _specs(agent) -> tuple[list[dict], dict]:
    return agent._build_tools_and_dispatch()


def _names(specs) -> list[str]:
    return [s["function"]["name"] for s in specs]


def _run(agent, query="hi") -> list[dict]:
    async def go():
        return [c async for c in agent.run(query, history_messages=[])]
    return asyncio.run(go())


_SEARCH_FN = re.compile(
    r"`((?:documentation_search|cremind_documentation_search|memory_search|web_search"
    r"|user_documents)__\w+|web_search)`"
)


# ── the tools block ────────────────────────────────────────────────────────


def test_an_unselected_source_loses_its_whole_group_and_its_aliases(env):
    agent = _agent(snapshot=st.Snapshot.of(["documentation_search", "memory_search", "web_search"], 2))
    specs, dispatch = _specs(agent)
    names = _names(specs)

    assert "cremind_documentation_search" not in agent._tools_by_id
    assert not [n for n in names if n.startswith("cremind_documentation_search__")]
    # The manual's historical names go with it — an alias never outlives its target.
    assert "documentation_search__search_documentation" not in dispatch
    assert "documentation_search__read_documentation_section" not in dispatch
    # Every other group, search or not, is untouched.
    assert "calc" in agent._tools_by_id and "calc__run" in names
    assert {"documentation_search__search", "memory_search__search_memory",
            "web_search__search_web"} <= set(names)
    prompt = agent._build_instruction()
    assert "cremind_documentation_search__" not in prompt

    agent = _agent(snapshot=st.Snapshot.of(["cremind_documentation_search"], 3))
    specs, dispatch = _specs(agent)
    assert not [n for n in _names(specs) if n.startswith("documentation_search__")]
    assert not [a for a in dispatch if a.startswith("user_documents__")]
    assert "calc__run" in _names(specs)


def test_selection_never_brings_back_a_gated_source(env):
    """Selection is a filter ON TOP of the gates: choosing Documentation search
    cannot expose it where its origin gate withheld it."""
    env.gate_open = False
    agent = _agent(snapshot=st.Snapshot.of(["documentation_search"], 1))
    specs, dispatch = _specs(agent)
    assert "documentation_search" not in agent._tools_by_id
    assert not [n for n in list(dispatch) if "documentation_search" in n or "user_documents" in n]
    assert agent._search_guidance == ""


def test_the_registry_lock_is_not_consulted(env):
    """Cremind's manual search is locked ON in the registry; a conversation may
    still leave it out, and the tool object itself is not touched."""
    groups = _search_groups()
    manual = groups[1]
    manual.locked = True
    agent = _agent(registry=_Registry({"alice": groups}), snapshot=st.Snapshot.of(["web_search"], 1))
    assert "cremind_documentation_search" not in agent._tools_by_id
    assert manual.locked is True


def test_identical_effective_selections_send_identical_search_tools_and_guidance(env):
    """Default selection with Documentation search gated off, and an explicit
    selection of the other three with the gate open, are the same effective
    choice — and must be the same bytes."""
    env.gate_open = False
    a = _agent(snapshot=None)
    env.gate_open = True
    b = _agent(snapshot=st.Snapshot.of(
        ["cremind_documentation_search", "memory_search", "web_search"], 9))

    assert _names(_specs(a)[0]) == _names(_specs(b)[0])
    assert json.dumps(_specs(a)[0], sort_keys=True) == json.dumps(_specs(b)[0], sort_keys=True)
    assert a._search_guidance == b._search_guidance != ""
    assert a._builtin_tools_guidance == b._builtin_tools_guidance
    assert a._documentation_search_guidance == b._documentation_search_guidance == ""
    assert a._build_instruction() == b._build_instruction()


def test_the_same_selection_on_two_profiles_renders_the_same_search_text(env):
    registry = _Registry({"alice": _search_groups(), "bob": _search_groups()})
    snap = st.Snapshot.of(["memory_search", "web_search"], 4)
    alice = _agent(registry=registry, profile="alice", snapshot=snap)
    bob = _agent(registry=registry, profile="bob", snapshot=snap)
    assert alice._search_guidance == bob._search_guidance
    assert _specs(alice)[0] == _specs(bob)[0]


@pytest.mark.parametrize("chosen", [
    list(c) for n in range(len(st.SEARCH_TOOL_IDS) + 1)
    for c in itertools.combinations(st.SEARCH_TOOL_IDS, n)
])
def test_the_prompt_names_only_functions_the_run_sends_in_priority_order(env, chosen):
    """For every one of the sixteen selections: each search function named
    anywhere in the system prompt is one the tools block really carries, and
    the SEARCH SOURCES block lists the sources in priority order."""
    agent = _agent(snapshot=st.Snapshot.of(chosen, 1))
    specs, _ = _specs(agent)
    sent = set(_names(specs))
    prompt = agent._build_instruction()

    named = set(_SEARCH_FN.findall(prompt))
    assert named <= sent, f"prompt names unexposed functions: {sorted(named - sent)}"
    exposed_sources = [t for t in st.SEARCH_TOOL_IDS
                       if any(n == t or n.startswith(t + "__") for n in sent)]
    assert exposed_sources == chosen
    if not chosen:
        assert "SEARCH SOURCES" not in prompt
        return
    block = prompt[prompt.index("SEARCH SOURCES — IN PRIORITY ORDER"):]
    labels = [st.CATALOG[t].label for t in chosen]
    positions = [block.index(f". {label} (") for label in labels]
    assert positions == sorted(positions)


def test_a_disabled_function_is_neither_sent_nor_named(env):
    registry = _Registry(
        {"alice": _search_groups()},
        disabled={"alice": {"cremind_documentation_search": {"read_documentation_section"},
                            "documentation_search": {"research"}}},
    )
    agent = _agent(registry=registry)
    specs, dispatch = _specs(agent)
    names = set(_names(specs))
    prompt = agent._build_instruction()
    for fn in ("cremind_documentation_search__read_documentation_section",
               "documentation_search__research"):
        assert fn not in names and fn not in prompt
    # …and neither is its historical name.
    assert "user_documents__research" not in dispatch
    assert "documentation_search__read_documentation_section" not in dispatch
    assert "cremind_documentation_search__search_documentation" in prompt


def test_leaf_switches_are_frozen_for_the_run(env):
    """A leaf toggled mid-response changes neither the tools block nor the
    guidance until the next run — they cannot drift apart."""
    disabled = {"alice": {}}
    registry = _Registry({"alice": _search_groups()}, disabled=disabled)
    agent = _agent(registry=registry)
    before = _names(_specs(agent)[0])
    disabled["alice"] = {"web_search": {"search_web"}}
    assert _names(_specs(agent)[0]) == before
    assert "web_search__search_web" in agent._search_guidance
    # The next run adopts it.
    after = _agent(registry=registry)
    assert "web_search__search_web" not in _names(_specs(after)[0])
    assert "web_search__search_web" not in after._search_guidance


def test_web_only_and_no_search(env):
    web = _agent(snapshot=st.Snapshot.of(["web_search"], 1))
    names = _names(_specs(web)[0])
    assert [n for n in names if n != "calc__run"] == ["web_search__search_web"]
    assert "`web_search__search_web`" in web._search_guidance
    assert "fallback" not in web._search_guidance and "sources above" not in web._search_guidance

    none = _agent(snapshot=st.Snapshot.of([], 1))
    specs, dispatch = _specs(none)
    assert _names(specs) == ["calc__run"]
    assert none._search_guidance == "" and none._documentation_search_guidance == ""
    assert not _SEARCH_FN.findall(none._build_instruction())
    assert not [a for a in dispatch if a in st.HISTORICAL_FUNCTION_ALIASES]


# ── historical names ───────────────────────────────────────────────────────


def test_historical_names_route_to_todays_functions_dispatch_only(env):
    specs, dispatch = _specs(_agent())
    names = set(_names(specs))
    for alias, (tool_id, leaf) in st.HISTORICAL_FUNCTION_ALIASES.items():
        target = make_leaf_name(tool_id, leaf)
        assert dispatch[alias] is dispatch[target]
        assert alias not in names  # never offered under the old name


def test_an_old_name_in_a_replayed_call_runs_the_current_tool(env):
    groups = _search_groups()
    agent = _agent(registry=_Registry({"alice": groups}),
                   llm=_ScriptedLLM([("tool", "user_documents__search"), ("text", "done")]))
    _run(agent)
    assert groups[0].executed == ["search"]


def test_an_old_name_cannot_reach_an_unselected_source(env):
    groups = _search_groups()
    agent = _agent(registry=_Registry({"alice": groups}),
                   snapshot=st.Snapshot.of(["web_search"], 1),
                   llm=_ScriptedLLM([("tool", "user_documents__search"), ("text", "done")]))
    chunks = _run(agent)
    assert groups[0].executed == []
    results = [c for c in chunks if c["type"] == ChatCompletionTypeEnum.RESULT_ARTIFACT]
    assert "Unknown tool 'user_documents__search'" in results[0]["data"]["Result"][0].root.text


def test_resolve_historical_aliases_is_pure():
    assert st.resolve_historical_aliases(set()) == {}
    present = {"documentation_search__search", "cremind_documentation_search__search_documentation"}
    assert st.resolve_historical_aliases(present) == {
        "user_documents__search": "documentation_search__search",
        "documentation_search__search_documentation":
            "cremind_documentation_search__search_documentation",
    }
    # A live function that happens to carry an old name keeps it.
    present.add("user_documents__search")
    assert "user_documents__search" not in st.resolve_historical_aliases(present)
    for tool_id, leaf in [("web_search", "web_search"), ("memory_search", "search_memory")]:
        assert st._leaf_name(tool_id, leaf) == make_leaf_name(tool_id, leaf)


# ── the cache baseline ─────────────────────────────────────────────────────


def test_the_baseline_is_recorded_once_per_run_with_the_snapshot_version(env):
    seen: list[dict] = []
    llm = _ScriptedLLM([("tool", "web_search__search_web"), ("text", "done")])
    agent = _agent(snapshot=st.Snapshot.of(["memory_search", "web_search"], 7),
                   hook=seen.append, llm=llm)
    _run(agent)

    assert len([c for c in llm.calls if not c["ack"]]) == 2
    assert len(seen) == 1
    (baseline,) = seen
    assert baseline["version"] == 7
    assert baseline["effective"] == ["memory_search", "web_search"]
    assert re.fullmatch(r"[0-9a-f]{64}", baseline["fingerprint"])
    assert st.read_baseline(baseline) == baseline


def test_equal_effective_selections_record_equal_fingerprints(env):
    def record(snapshot):
        seen: list[dict] = []
        _run(_agent(snapshot=snapshot, hook=seen.append, llm=_ScriptedLLM([("text", "ok")])))
        return seen[0]

    all_but_docs = ["cremind_documentation_search", "memory_search", "web_search"]
    env.gate_open = False
    a = record(None)
    env.gate_open = True
    b = record(st.Snapshot.of(all_but_docs, 3))
    c = record(st.Snapshot.of(["web_search"], 4))
    assert a["effective"] == b["effective"] == all_but_docs
    assert a["fingerprint"] == b["fingerprint"]
    assert c["fingerprint"] != a["fingerprint"]
    assert (a["version"], b["version"]) == (0, 3)


def test_an_async_hook_is_awaited_and_a_failing_one_never_breaks_the_run(env):
    seen: list[dict] = []

    async def hook(baseline):
        await asyncio.sleep(0)
        seen.append(baseline)

    _run(_agent(hook=hook, llm=_ScriptedLLM([("text", "ok")])))
    assert len(seen) == 1

    def boom(baseline):
        raise RuntimeError("storage down")

    chunks = _run(_agent(hook=boom, llm=_ScriptedLLM([("text", "still answered")])))
    text = "".join(c.get("data") or "" for c in chunks if c["type"] == ChatCompletionTypeEnum.CONTENT)
    assert text == "still answered"


def test_a_refused_main_request_records_no_baseline(env):
    """A request the provider never answered (a bad key, an outage) reached no
    cache and adopted nothing: no baseline, so a pending selection stays
    pending and later edits are still judged against the last real request."""
    seen: list[dict] = []
    refused = AgentException(Status.LLM_CHAT_COMPLETION_ERROR, "401 invalid api key")
    llm = _ScriptedLLM([("raise", refused)] * 10)
    agent = _agent(hook=seen.append, llm=llm)
    agent._max_llm_retries = 0
    _run(agent)
    assert [c for c in llm.calls if not c["ack"]], "the request was attempted"
    assert seen == []


def test_a_retried_first_request_records_once(env):
    seen: list[dict] = []
    overflow = AgentException(Status.LLM_CONTEXT_OVERFLOW, "prompt is too long")
    llm = _ScriptedLLM([("raise", overflow), ("text", "ok")])
    _run(_agent(hook=seen.append, llm=llm))
    assert len(llm.calls) == 2
    assert len(seen) == 1


def test_no_baseline_for_a_maintenance_fold_or_a_run_that_sent_nothing(env):
    seen: list[dict] = []
    _run(_agent(hook=seen.append, maintenance=True, llm=_ScriptedLLM([("text", "ok")])))
    assert seen == []

    agent = _agent(hook=seen.append, llm=_ScriptedLLM([]))
    agent.max_steps = 0
    _run(agent)
    assert seen == []


# ── mid-turn messages ──────────────────────────────────────────────────────


@pytest.fixture
def bound_run():
    from app.events import task_result_inbox
    from app.utils.task_context import current_task_id_var

    task_result_inbox.clear_all()
    run_id, conv = "msg:conv-st:1", "conv-st"
    task_result_inbox.bind_run(run_id, conv)
    token = current_task_id_var.set(run_id)
    yield SimpleNamespace(conv=conv, inbox=task_result_inbox)
    current_task_id_var.reset(token)
    task_result_inbox.clear_all()


def _park(bound, text="actually, also check the web"):
    bound.inbox.park_user_message_if_bound(bound.conv, {
        "message_id": "m1", "text": text, "agent_text": text,
    })


def test_injected_mid_turn_messages_do_not_change_the_tools(env, bound_run):
    """A message folded into a running response is new INPUT, never a new
    selection: every request of the run — acknowledgement included — carries
    the same tools and the same system prompt."""
    seen: list[dict] = []

    def on_call(n):
        if n == 1:
            _park(bound_run)

    llm = _ScriptedLLM([("tool", "memory_search__search_memory"), ("text", "done")],
                       on_call=on_call)
    agent = _agent(snapshot=st.Snapshot.of(["memory_search"], 2), hook=seen.append, llm=llm)
    _run(agent)

    assert [c["ack"] for c in llm.calls] == [False, True, False]
    tools = {json.dumps(c["tools"], sort_keys=True) for c in llm.calls}
    systems = {c["messages"][0]["content"] for c in llm.calls}
    assert len(tools) == 1 and len(systems) == 1
    assert len(seen) == 1 and seen[0]["effective"] == ["memory_search"]


def test_the_acknowledgement_is_not_the_main_request(env, bound_run):
    """A message waiting before the first step makes the acknowledgement the
    run's first model call; the baseline still waits for the main request —
    and for the provider's first answer to it."""
    events: list[str] = []
    _park(bound_run)
    llm = _ScriptedLLM([("text", "done")], events=events)
    agent = _agent(hook=lambda b: events.append("baseline"), llm=llm)
    _run(agent)
    assert events == ["ack", "main", "baseline"]


# ── two profiles ───────────────────────────────────────────────────────────


def test_two_profiles_keep_their_own_sources_switches_and_selection(env):
    registry = _Registry(
        {"alice": _search_groups() + [_calc()], "bob": _search_groups()[:3]},
        disabled={"alice": {"memory_search": {"search_memory"}}},
    )
    alice = _agent(registry=registry, profile="alice", snapshot=st.Snapshot.of(["memory_search", "web_search"], 5))
    bob = _agent(registry=registry, profile="bob", snapshot=None)

    alice_names = set(_names(_specs(alice)[0]))
    bob_names = set(_names(_specs(bob)[0]))
    # Alice: memory's only function is off for her, so web is her one source.
    assert {n for n in alice_names if n != "calc__run"} == {"web_search__search_web"}
    assert "memory_search__search_memory" not in alice._search_guidance
    # Bob: no web search registered for him, his memory function is on.
    assert "memory_search__search_memory" in bob_names
    assert "web_search__search_web" not in bob_names and "web_search" not in bob._search_guidance
    assert "documentation_search__search" in bob_names
    # Nothing either run froze leaked into the class (or into the other run).
    assert ra.ReasoningAgent._search_disabled_leaves == {}
    assert ra.ReasoningAgent._search_snapshot is None
    assert alice._search_disabled_leaves["memory_search"] == frozenset({"search_memory"})
    assert bob._search_disabled_leaves["memory_search"] == frozenset()


# ── the snapshot resolver ──────────────────────────────────────────────────


class _ConvStorage:
    def __init__(self, rows: dict, *, fail: bool = False) -> None:
        self.rows = rows
        self.fail = fail
        self.baselines: list[tuple] = []

    async def get_search_tools_row(self, conversation_id):
        if self.fail:
            raise RuntimeError("db down")
        return self.rows.get(conversation_id)

    async def record_search_cache_baseline(self, conversation_id, baseline):
        self.baselines.append((conversation_id, baseline))


def _resolve(*args, **kwargs):
    return asyncio.run(st.snapshot_for_conversation(*args, **kwargs))


def test_a_conversation_reads_its_own_row():
    storage = _ConvStorage({
        "a": {"id": "a", "search_tools": ["web_search"], "search_tools_version": 5,
              "kind": "chat", "context_id": "ctx-a"},
        "b": {"id": "b", "search_tools": '["memory_search"]', "search_tools_version": 2,
              "kind": "chat", "context_id": "ctx-b"},
        "c": {"id": "c", "search_tools": None, "search_tools_version": 0,
              "kind": "chat", "context_id": "ctx-c"},
    })
    assert _resolve("a", conversation_storage=storage) == st.Snapshot(("web_search",), 5, "conversation")
    assert _resolve("b", conversation_storage=storage) == st.Snapshot(("memory_search",), 2, "conversation")
    assert _resolve("c", conversation_storage=storage).allows("documentation_search")


def test_a_channel_group_conversation_is_an_ordinary_conversation():
    storage = _ConvStorage({"g": {"id": "g", "search_tools": ["memory_search"],
                                  "search_tools_version": 1, "context_id": "channel_group:x"}})
    assert _resolve("g", conversation_storage=storage).source == "conversation"


class _Groups:
    """Group storage with only the full read (older shape)."""

    _rows = {"g1": {"id": "g1", "search_tools": ["cremind_documentation_search"],
                    "search_tools_version": 7}}

    async def get_group(self, gid):
        return self._rows.get(gid)


class _NarrowGroups(_Groups):
    """Group storage with the narrow read, which is preferred."""

    async def get_group(self, gid):  # pragma: no cover - must not be called
        raise AssertionError("the narrow read should have been used")

    async def get_group_search_tools(self, gid):
        return self._rows.get(gid)


@pytest.mark.parametrize("groups", [_Groups, _NarrowGroups])
def test_a_seat_reads_its_rooms_selection(monkeypatch, groups):
    import app.storage as storage_pkg

    monkeypatch.setattr(storage_pkg, "get_group_chat_storage", lambda: groups())
    storage = _ConvStorage({
        "seat": {"id": "seat", "kind": "group_chat", "context_id": "group:g1:alice",
                 # A seat's own columns are never the room's choice.
                 "search_tools": ["web_search"], "search_tools_version": 99},
        "gone": {"id": "gone", "kind": "group_chat", "context_id": "group:g2:alice"},
    })
    snap = _resolve("seat", conversation_storage=storage)
    assert snap == st.Snapshot(("cremind_documentation_search",), 7, "room")
    assert _resolve("gone", conversation_storage=storage) == st.DEFAULT_SNAPSHOT
    # Two profiles' seats in the same room share the room's choice.
    storage.rows["bob-seat"] = {"id": "bob-seat", "kind": "group_chat", "context_id": "group:g1:bob"}
    assert _resolve("bob-seat", conversation_storage=storage) == snap


def test_anything_unreadable_is_the_default():
    assert _resolve("x", conversation_storage=_ConvStorage({}, fail=True)) == st.DEFAULT_SNAPSHOT
    assert _resolve("x", conversation_storage=object()) == st.DEFAULT_SNAPSHOT
    assert _resolve(None, conversation_storage=_ConvStorage({})) == st.DEFAULT_SNAPSHOT
    # A caller-held row carrying the columns is used when storage cannot read.
    conv = {"id": "x", "search_tools": ["web_search"], "search_tools_version": 3}
    assert _resolve("x", conv=conv, conversation_storage=object()) == st.Snapshot(("web_search",), 3, "conversation")


# ── the snapshot reaches the agent on every path ───────────────────────────


def test_cremind_agent_forwards_the_snapshot_and_the_hook(monkeypatch):
    from app.agent import agent as agent_mod
    import app.config.settings as settings_mod

    seen: list = []

    class _Recorder:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        async def run(self, query, history):
            if False:  # pragma: no cover - never yields; shape only
                yield None

    monkeypatch.setattr(agent_mod, "ReasoningAgent", _Recorder)
    monkeypatch.setattr(settings_mod.BaseConfig, "is_embedding_enabled", staticmethod(lambda: False))
    mgr = SimpleNamespace(
        config_storage=SimpleNamespace(is_setup_complete=lambda: True),
        create_llm_for_model=lambda profile=None: SimpleNamespace(provider_name="fake", model_name="m"),
    )
    cremind = agent_mod.CremindAgent(registry=_Registry({}), embedding=None, model_group_mgr=mgr)
    snap = st.Snapshot.of(["web_search"], 4)

    async def drive(**kwargs):
        async for _ in cremind.run(query="q", task_history=[], context_id="c", profile="p", **kwargs):
            pass

    asyncio.run(drive(search_tools=snap, on_search_baseline=print))
    asyncio.run(drive())
    assert seen[0]["search_tools"] is snap and seen[0]["on_search_baseline"] is print
    assert seen[1]["search_tools"] is None and seen[1]["on_search_baseline"] is None


def _fold_env(monkeypatch):
    from app.agent import compaction

    monkeypatch.setattr(compaction, "resolve_compaction_config", lambda p: SimpleNamespace(max_tokens=100))

    async def _history(**kwargs):
        return []

    async def _no_usage(**kwargs):
        return None

    monkeypatch.setattr(compaction, "build_compacted_history", _history)
    monkeypatch.setattr(compaction, "_record_fold_usage", _no_usage)
    return compaction


class _FoldAgent:
    def __init__(self):
        self.kwargs: list[dict] = []

    async def run(self, **kwargs):
        self.kwargs.append(kwargs)
        yield {"type": ChatCompletionTypeEnum.DONE, "data": ""}


class _FoldStorage(_ConvStorage):
    async def get_compaction_state(self, conversation_id):
        return ("", -1, None)


def test_the_fold_carries_the_conversations_selection_and_no_hook(monkeypatch):
    compaction = _fold_env(monkeypatch)
    storage = _FoldStorage({"c1": {"id": "c1", "search_tools": ["memory_search"],
                                   "search_tools_version": 6, "context_id": "ctx"}})
    agent = _FoldAgent()

    # The post-turn path hands over the snapshot of the run it follows…
    given = st.Snapshot.of(["web_search"], 5)
    asyncio.run(compaction.run_model_fold(agent, "c1", "alice", storage, context_id="ctx",
                                          search_tools=given))
    # …the manual path lets the fold read the conversation's own.
    asyncio.run(compaction.run_model_fold(agent, "c1", "alice", storage, context_id="ctx"))

    assert agent.kwargs[0]["search_tools"] is given
    assert agent.kwargs[1]["search_tools"] == st.Snapshot(("memory_search",), 6, "conversation")
    for kwargs in agent.kwargs:
        assert kwargs["maintenance"] is True
        assert "on_search_baseline" not in kwargs


def test_after_turn_compaction_hands_the_snapshot_to_the_fold(monkeypatch):
    from app.agent import compaction

    monkeypatch.setattr(compaction, "resolve_compaction_config", lambda p: SimpleNamespace(
        enabled=True, auto_compact_enabled=True, compact_threshold_percent=50,
        keep_recent_tokens=0, max_tokens=10))

    async def _usage(**kwargs):
        return {"current_tokens": 990, "context_window": 1000, "ceiling": 950, "threshold": 500}

    monkeypatch.setattr(compaction, "context_usage", _usage)
    seen: list = []

    async def _fold(*args, **kwargs):
        seen.append(kwargs.get("search_tools"))
        return False

    monkeypatch.setattr(compaction, "run_model_fold", _fold)
    snap = st.Snapshot.of(["web_search"], 2)
    asyncio.run(compaction.after_turn_compaction(
        object(), "c1", "alice", _FoldStorage({}), context_id="ctx", search_tools=snap,
    ))
    assert seen == [snap]


def test_the_a2a_path_resolves_the_selection_and_records_the_baseline():
    from app.agent.executor import CremindAgentExecutor

    storage = _ConvStorage({"c1": {"id": "c1", "search_tools": ["web_search"],
                                   "search_tools_version": 3, "context_id": "ctx"}})
    executor = CremindAgentExecutor.__new__(CremindAgentExecutor)
    executor.conversation_storage = storage

    snap, hook = asyncio.run(executor._search_tools_for({"id": "c1", "context_id": "ctx"}))
    assert snap == st.Snapshot(("web_search",), 3, "conversation")
    asyncio.run(hook({"version": 3, "effective": ["web_search"]}))
    assert storage.baselines == [("c1", {"version": 3, "effective": ["web_search"]})]

    # No conversation row yet (a first A2A message): defaults, nothing to record.
    assert asyncio.run(executor._search_tools_for(None)) == (st.DEFAULT_SNAPSHOT, None)
    # A storage that cannot record baselines gets no hook.
    executor.conversation_storage = SimpleNamespace()
    snap, hook = asyncio.run(executor._search_tools_for({"id": "c1"}))
    assert snap == st.DEFAULT_SNAPSHOT and hook is None
