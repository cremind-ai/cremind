"""Reasoning agent -- native function-calling loop over the unified Tool registry.

A single configured model drives the whole turn. Every enabled tool's *leaf*
functions are flattened into one ``tools=`` list (with their real JSON-Schema),
and the model calls them directly via native function calling -- there is no
ReAct ``Thought/Action/Action_Input`` scaffolding and no inner per-group routing
LLM. The loop is:

    messages = [system, *history, {user: query}, *turn_messages]
    while steps < max_steps:
        stream model(messages, tools=<flat leaf specs>)
          - CONTENT deltas  -> streamed to the user in real time
          - tool_calls      -> collected
        if no tool_calls:           # plain text == final answer
            DONE; return
        append assistant(tool_calls); for each call: execute leaf, append tool result
        continue

UI continuity: each tool call still emits a ``THINKING_ARTIFACT`` (the tool
name and its arguments) and a ``RESULT_ARTIFACT`` (the tool result) so the
frontend Thinking Process, ``thinking_steps`` persistence and the
reasoning-trace summary keep working unchanged.
"""

from __future__ import annotations

import asyncio
import json
import platform
import uuid
from typing import TYPE_CHECKING, Any, AsyncGenerator, Dict, List, Optional, Tuple

# OpenAI SDK lives in the ``llm-openai`` extras group. Its types are
# referenced only in PEP 563-stringified annotations here, so importing
# under TYPE_CHECKING keeps the agent loadable on a thin-core install.
if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam

from a2a.types import DataPart, Part, TextPart

from app.agent.usage import UsageRecord
from app.utils.formatting import dict_to_text
from app.config import (
    model_parallel_tool_calls,
    model_supports_reasoning,
    model_supports_vision,
    vision_feature_enabled,
)
from app.config.settings import get_user_working_directory
from app.config.user_config import resolve_agent_config, resolve_memory_config
from app.constants import ChatCompletionTypeEnum
from app.constants.status import Status
from app.lib.exception import AgentException
from app.lib.llm.base import LLMProvider, is_context_overflow
from app.tools import (
    ToolErrorEvent,
    ToolRegistry,
    ToolResultEvent,
    ToolStatusEvent,
    ToolThinkingEvent,
    ToolType,
)
from app.tools.base import make_leaf_name
from app.skills.scanner import generate_dir_tree
from app.types import ReasoningStreamResponseType
from app.utils.common import truncate_to_tokens
from app.utils.context_storage import clear_context, get_context, set_context
from app.utils.logger import logger
from app.utils.message_tokens import resolve_system_var_tokens
from app.utils.persona import read_persona_file
from app.utils.working_directory import set_in_memory_override


# Mirror of ``self._loaded_skill_ids`` written into ContextStorage so that
# per-request callbacks (e.g. ``change_working_directory``'s prepare_tools)
# can read the current loaded-skill set without holding an agent reference.
# Must match the constant of the same name in
# ``app.tools.builtin.change_working_directory``.
LOADED_SKILLS_KEY = "_loaded_skill_ids"

# ContextStorage key holding the current turn's raw user query. Leaf tools (e.g.
# the registration tools' self-containment gate) read it to compare a frozen
# ``action`` against the request that asked for it, without an agent reference.
# Duplicated at the read sites (scheduler_actions / register_file_watcher) per
# the LOADED_SKILLS_KEY mirror-constant convention.
CURRENT_QUERY_KEY = "_current_query"

# Parameter name a skill function exposes so the model can pass the user's
# intent. On first use it is overwritten to a fixed marker and the SKILL.md
# content is returned as the tool result (see ``_handle_skill_call``).
SKILL_REQUEST_ARG = "request"

# What we overwrite a skill load call's ``request`` arg with so the persisted /
# replayed trace is deterministic and byte-stable (better prompt-cache reuse).
# Phrased as a completed label, NOT an imperative ("You need to load…") — a weak
# model re-reading the replayed trace must not mistake it for a pending instruction
# and re-call the skill (the SKILL.md is already above in history).
SKILL_LOAD_REQUEST = "Load skill '{name}' instructions (SKILL.md)"


SYSTEM_TEMPLATE = '''{persona_description}

Current OS: {current_os}
Current User Working Directory: `{current_user_working_directory}`
Active profile: $CREMIND_PROFILE
Your name: $CREMIND_AGENT_NAME
{long_term_memory}
You are a capable assistant. Fulfil the user's request by calling the available
tools (functions) when you need to act or fetch information, then reply to the
user in plain text. Call a tool ONLY when it is actually needed; when you have
enough information, answer directly. Do not narrate which tool you are about to
call -- just call it. When you are done, respond with the final answer as plain
text (no tool call) -- that text is shown to the user and ends the turn.

For any request that contains a time expression you MUST first call the
`datetime_parser` tool to normalise it; if it also contains a recurring
schedule you MUST call the `scheduler` tool instead. Do this before calling
other tools that need the normalised time.
{reasoning_guidance}{search_guidance}{claude_code_guidance}
PRESERVE THE USER'S LANGUAGE: any human-facing value you put in a tool argument
-- especially the title/name of something you create on the user's behalf (a
schedule event, reminder, note, or task) and any message shown to the user --
MUST be written in the SAME language the user used. Do NOT translate it.
Structured values (ISO datetimes, RRULE strings, enums, file paths, keys) stay
exactly as specified.'''


# Injected into the system prompt ONLY for models that lack native step-by-step
# reasoning (see ``model_supports_reasoning``). It teaches the model to use the
# ``reasoning`` think-tool as a scratchpad before each real tool call, which
# approximates how native reasoning models operate and improves tool selection.
REASONING_GUIDANCE = '''
REASONING STEP: You have no built-in step-by-step reasoning, so think out loud
using the `reasoning` tool. Before EVERY other tool call, FIRST call `reasoning`
and pass your step-by-step thinking -- what the user wants, what you already
know, and which tool you will call next and why. Treat it as a private
scratchpad: think there, then act. Re-call it whenever the situation changes or
before any non-trivial decision. Never put the final user-facing answer in it.
'''


# Appended (only) to an event run's system prompt. Event runs fire automatically
# with no user watching live, so the agent must work autonomously — but when it
# genuinely needs a decision or missing input it parks the run 'pending' via the
# request_user_input tool instead of guessing.
EVENT_RUN_GUIDANCE = '''

AUTOMATED EVENT RUN: This turn was triggered automatically by an event rule, not
by a person typing. No user is watching live. Work autonomously and complete the
requested action end to end. HOW EVENT RUNS WORK: this run is isolated — it has
its own fresh, private conversation created just for this firing, carrying NO
history from the user's chat or from any other event run, and other events fire
independently of it (repeat firings of the same rule run one at a time in order;
different rules run in parallel). Rely only on the trigger content and the tools
in front of you; never assume continuity with a previous run or refer to "the
conversation" or to earlier work that isn't present in this turn. If — and only
if — you genuinely need the user to confirm a risky/irreversible action or to
supply missing information you cannot obtain yourself, call the
`request_user_input` tool with ONE clear question and then stop; the run is
parked as *pending* until the user answers in the run's chat, and their reply
arrives as your next turn (that reply and any follow-up turns share THIS run's
conversation — the only history an event run ever builds up). Do not use
`request_user_input` for progress updates or narration, and never ask when you
can reasonably proceed.
If your action is a multi-step procedure, maintain a live todo list with
`update_todos` (pass the FULL list every call, one item in_progress at a time)
so progress is visible; it does not end your turn.
'''


# Appended (only) to a Plan-mode PLANNING turn's system prompt. The user wants a
# plan before any changes are made, so the agent researches read-only, asks
# clarifying questions, then writes a plan for approval. Kept append-after-format
# (like EVENT_RUN_GUIDANCE) so ordinary chat/instant runs render byte-identical.
PLAN_MODE_PLANNING_GUIDANCE = '''

PLAN MODE — PLANNING PHASE: The user wants a plan before any changes are made.
Work READ-ONLY in this phase: you may read files, search, and inspect, but do NOT
modify anything, run mutating commands, or create/edit files — only the plan
tools below write anything.
1. If the user's message is telling you to carry out a plan that was ALREADY
   written earlier in this conversation (e.g. "continue", "go ahead", "implement
   the plan" — even after a previous cancel), treat that as approval: stop
   planning, maintain a live todo list with `update_todos`, and execute that plan
   to completion using your normal tools.
2. Otherwise, if you have NOT yet asked clarifying questions in this planning
   cycle, first research the request read-only, then call `ask_user_question`
   with 1-4 focused questions and STOP — end your turn. Always ask at least one
   clarifying question before writing a plan.
3. Once the user has answered your questions (their answers appear earlier in
   this turn's history), call `write_plan` with a detailed, complete Markdown
   plan, then STOP — end your turn and wait for the user's Accept/Cancel.
AUTOMATION REQUESTS: If the request is to set up a recurring or event-triggered
automation (a schedule, a file watcher, or a skill event), the plan's deliverable
is the REGISTRATION — not a one-off run. The step list you write runs on EVERY
fire, later, in a FRESH conversation that has no memory of this one, so the plan
must contain: (a) the trigger (what fires and when), and (b) a complete,
self-contained per-fire procedure in the user's language with every detail,
condition, recipient, dedupe rule, and output format spelled out — with NO
references to "this conversation" or to work already done here. The execution
section of the plan covers ONLY registering the trigger; do NOT plan a first
inline run of the task itself.
Do not call any other tool in the same step as `ask_user_question` or
`write_plan`. Never start a fresh planning cycle while an approved plan's todos
are still incomplete — treat such messages as steering for the ongoing work.
Write your questions and plan in the user's language.
'''


# Appended (only) to a Plan-mode EXECUTION turn's system prompt (the user
# approved the plan, or is resuming an interrupted execution).
PLAN_MODE_EXECUTION_GUIDANCE = '''

PLAN MODE — EXECUTION PHASE: The user approved the plan. Execute it end to end.
The plan is earlier in this conversation; if it has scrolled out of context
(compaction, or a later resume) read it back from its saved file with
`system_file` before proceeding. Maintain a live todo list with the
`update_todos` tool: call it with the FULL current list (each item has content +
status pending|in_progress|completed) right after you read the plan, whenever you
start an item (mark it in_progress — keep at most one in_progress at a time), and
whenever you complete one. Do the real work with your normal tools between
updates. Keep executing until every todo is completed, then give a short final
summary. If the user sends a message while you are executing, treat it as
steering for the ongoing plan (adjust and continue) rather than a request for a
brand-new plan.
AUTOMATION PLANS ARE DIFFERENT: If the approved plan is to set up an automation
(schedule / file watcher / skill event), EXECUTION means registering the trigger
ONLY. Call the matching registration tool exactly once — `schedule_create`,
`register_file_watcher`, or the skill's `subscribe` — and copy the full per-fire
procedure from the plan verbatim into its `action` field (in the user's language,
self-contained). Inline every concrete value from the plan and the user's request
verbatim (full URLs, email addresses, file paths, IDs) — the action runs later
with no access to this conversation, so phrases like 'the provided URL' will
dangle. Do NOT perform the task itself even once, and do NOT drive a
todo list for the task's steps — registration is one or two tool calls, so no
todo list is needed. Claim success only AFTER the registration tool returns OK,
then end with a one-line summary of what was registered (the trigger and where
results go).
'''


def _plan_parked_final_text(questions: Optional[dict], plan: Optional[dict]) -> str:
    """Readable final-assistant text for a parked Plan-mode turn.

    The plan turn's content is the full plan markdown (so execution turns, the
    CLI, and compaction-resume see it verbatim); the questions turn's content is
    a readable rendering of the questions (the structured form is delivered
    separately via the ``ask_user_question`` bus event + message metadata).
    """
    if plan:
        return str(plan.get("markdown") or "").strip() or "I've prepared a plan for your review."
    if questions:
        qs = questions.get("questions") or []
        lines = ["I have a few questions before I write the plan:"]
        for i, q in enumerate(qs, 1):
            lines.append(f"{i}. {q.get('question', '')}")
        return "\n".join(lines)
    return ""


# Upper bound on facts folded into the frozen system-prompt memory section. The
# DB path is already queue-capped (``memory.long_term_queue_size``); this also
# bounds the effectively-unlimited vector path so the block can't bloat the cache
# prefix.
_MEMORY_SNAPSHOT_LIMIT = 50

# Per-process, per-profile snapshot of the long-term-memory section injected into
# ``SYSTEM_TEMPLATE``. Loaded ONCE (first turn to need it for a profile) and never
# refreshed on memory writes -- coupling the system block to frequently-changing
# memory would bust the prompt cache. A process restart is the only refresh.
_LONG_TERM_MEMORY_SNAPSHOT: dict[str, str] = {}


def _format_memory_block(facts: list[str]) -> str:
    """Render durable facts as the ``{long_term_memory}`` section, or "" for none.

    Self-wrapped ``'\\n...\\n'`` (like REASONING_GUIDANCE) so it drops cleanly into
    the template's identity block and collapses to a single blank line when empty.
    """
    facts = [f.strip() for f in facts if f and f.strip()]
    if not facts:
        return ""
    body = "\n".join(f"- {f}" for f in facts)
    return (
        "\nLONG-TERM MEMORY (durable facts about the user, remembered across past "
        "conversations):\n" + body + "\n"
    )


def _search_tool_classes():
    """The built-in search tool CLASSES in guidance order: ``(local_tier, web)``.

    Imported lazily and defensively straight from ``app.tools.builtin`` so this
    module hard-codes no tool names: each tool's own class is the single source
    of truth for its identity (the defining module == the registered group's
    ``config_name``) and the function name the model sees (the class ``name`` ==
    the leaf). A rename/move of a class therefore breaks the import loudly here
    instead of silently drifting. Lazy + guarded because a tool module may fail
    to import when an optional dependency is absent -- in which case the tool is
    not registered either, so omitting it from the guidance is correct. By the
    time an agent is constructed these modules are already imported (at
    registration), so the lazy import is just a cached lookup.

    Returns ``([local_classes], web_class_or_None)``.
    """
    local = []
    web = None
    try:
        from app.tools.builtin.documentation_search import DocumentationSearchTool
        local.append(DocumentationSearchTool)
    except Exception:  # noqa: BLE001 - missing optional dep => tool not registered
        pass
    try:
        from app.tools.builtin.search_memory import SearchMemoryTool
        local.append(SearchMemoryTool)
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.tools.builtin.web_search import WebSearchTool
        web = WebSearchTool
    except Exception:  # noqa: BLE001
        pass
    return local, web


def _build_search_guidance(tools) -> str:
    """Assemble the fallback-search guidance from the search tools actually
    enabled for this run, naming ONLY the ones present so the prompt never
    references a tool the user turned off. Local lookup (documentation/memory) is
    tried first; web search is the last-resort internet fallback. Returns "" when
    none are enabled. Wrapped ``'\\n...\\n'`` to match REASONING_GUIDANCE's spacing.

    Each search tool is matched to its LIVE registered group by its defining
    module (``cls.__module__`` == the group's ``config_name``); the function name
    the model sees is ``make_leaf_name(group.tool_id, cls.name)`` -- the group's
    real registry id plus the class-defined leaf -- so it always matches the
    ``tools=`` block and re-derives automatically on any rename.

    Enablement is judged at the group level (presence in ``tools``). The rare
    case of a single-leaf group whose sole leaf is disabled via the API-only
    per-leaf path (the Settings UI hides leaf toggles for single-leaf groups) is
    not handled -- the group still counts as enabled here.
    """
    local_classes, web_class = _search_tool_classes()
    group_by_stem = {}
    for tool in tools:
        stem = getattr(tool, "config_name", None)
        if stem is not None:
            group_by_stem[stem] = tool

    def exposed(cls) -> Optional[str]:
        if cls is None:
            return None
        group = group_by_stem.get(cls.__module__.rsplit(".", 1)[-1])
        return make_leaf_name(group.tool_id, cls.name) if group is not None else None

    local = [f"`{fn}`" for fn in (exposed(c) for c in local_classes) if fn]
    web = exposed(web_class)

    if not local and not web:
        return ""
    if local:
        joined = " and ".join(local)
        body = (
            "When there is a request or information lookup from a user — if it is "
            "casual chat you can respond immediately. If it is not casual chat, "
            "you can check the list of supported tools to see if any tool can fulfill "
            "the user's need. If not, do not give up too quickly — you must not affirm "
            f"whether this request/information can be fulfilled or not — instead call {joined} "
            f"tool{'s' if len(local) > 1 else ''} to ensure that the request/information "
            "has been verified."
        )
        if web:
            body += (
                f" Only if that returns nothing useful may you then call `{web}` "
                "to search the public internet."
            )
    else:
        # documentation_search is locked-on, so a local tool is normally always
        # present; this web-only branch is a defensive fallback.
        body = (
            "When there is a request or information lookup from a user — if it is "
            "casual chat you can respond immediately. If it is not casual chat, "
            "you can check the list of supported tools to see if any tool can fulfill "
            "the user's need. If not, do not give up too quickly — you must not affirm "
            f"whether this request/information can be fulfilled or not — instead call the "
            f"`{web}` tool to search the public internet to ensure that the "
            "request/information has been verified."
        )
    return "\n" + body + "\n"


def _build_claude_code_guidance(tools) -> str:
    """Delegation guidance for the ``claude_code`` tool, present ONLY when it is
    enabled for this run (matched to its live registered group by module stem),
    naming the exact leaf function names the model sees. Returns "" otherwise so
    the prompt stays byte-identical for the vast majority of profiles that keep
    the disabled-by-default tool off — no prompt-cache impact for them. Wrapped
    ``'\\n...\\n'`` like the other guidance blocks.
    """
    try:
        from app.tools.builtin.claude_code import (
            ClaudeCodeRunTool,
            ClaudeCodeStatusTool,
            ClaudeCodeStopTool,
            ClaudeCodeWaitTool,
        )
    except Exception:  # noqa: BLE001 - missing optional dep => tool not registered
        return ""

    group_by_stem = {}
    for tool in tools:
        stem = getattr(tool, "config_name", None)
        if stem is not None:
            group_by_stem[stem] = tool
    group = group_by_stem.get("claude_code")
    if group is None:
        return ""

    run_fn = make_leaf_name(group.tool_id, ClaudeCodeRunTool.name)
    wait_fn = make_leaf_name(group.tool_id, ClaudeCodeWaitTool.name)
    stop_fn = make_leaf_name(group.tool_id, ClaudeCodeStopTool.name)
    status_fn = make_leaf_name(group.tool_id, ClaudeCodeStatusTool.name)
    body = (
        f"CODING TASKS — DELEGATE TO CLAUDE CODE: `{run_fn}` drives Claude Code, an "
        "expert autonomous coding agent working in the current working directory. "
        "For software-engineering work — creating projects or apps, "
        "writing/refactoring/debugging code, reviewing or explaining a codebase, "
        "running and fixing tests — PREFER delegating to it instead of editing "
        "files yourself with file/shell tools. Give it a complete task brief "
        f"(goal, constraints, relevant paths). If it returns status 'running', keep "
        f"calling `{wait_fn}` with the task_id until it completes, then report the "
        "result (mention cost/duration when notable). Continue the same coding "
        f"session by passing the returned session_id to a new `{run_fn}`. Stop a "
        f"runaway task with `{stop_fn}`. To answer whether Claude Code is set up or "
        f"logged in (before running a task), call `{status_fn}` (add probe=true for a "
        "definitive live check). Do not use it for trivial single-line edits or "
        "non-coding tasks."
    )
    return "\n" + body + "\n"


class _LeafOutcome:
    """Collected output of one leaf tool run (so leaves can run concurrently)."""

    __slots__ = ("call_id", "status_chunks", "tool_text", "parts")

    def __init__(self, call_id: str, status_chunks: list, tool_text: str, parts: list):
        self.call_id = call_id
        self.status_chunks = status_chunks
        self.tool_text = tool_text
        self.parts = parts


def _coerce_args(raw: Any) -> Dict[str, Any]:
    """Normalise a tool call's ``arguments`` (dict or JSON string) to a dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


class ReasoningAgent:
    """Native function-calling loop driven by the unified :class:`Tool` registry."""

    # Safe default for the per-model parallel-tool-calls flag; ``__init__`` resolves
    # the real value from the model catalog. Keeps construction via ``__new__``
    # (used in tests) and any future subclass from tripping on a missing attribute.
    _parallel_tool_calls: bool = True

    # Fallback-search system-prompt block; ``__init__`` recomputes it from the
    # run's enabled tools. Class-level default keeps ``__new__`` construction
    # (used in tests) from tripping on a missing attribute.
    _search_guidance: str = ""

    # Claude Code delegation block; ``__init__`` recomputes it from the run's
    # enabled tools (empty unless the disabled-by-default claude_code tool is on).
    # Class-level default keeps ``__new__`` construction (tests) safe.
    _claude_code_guidance: str = ""

    # Frozen long-term-memory section; ``_loop`` fills it once per run from the
    # per-process snapshot. Class-level default keeps ``__new__`` construction and
    # direct ``_build_instruction`` calls (tests) from tripping on a missing attr.
    _long_term_memory_block: str = ""

    # Turn mode + plan phase. Class-level defaults keep ``__new__`` construction
    # (tests) and direct ``_build_instruction`` calls from tripping on a missing
    # attribute; a normal run always sets them in ``__init__``.
    _mode: str = "reasoning"
    _plan_phase: Optional[str] = None

    def __init__(
        self,
        llm: LLMProvider,
        registry: ToolRegistry,
        profile: str,
        context_id: Optional[str] = None,
        max_steps: Optional[int] = None,
        reasoning: bool = True,
        triggered_by_event: bool = False,
        event_run: bool = False,
        mode: str = "reasoning",
        plan_phase: Optional[str] = None,
    ):
        self.llm = llm
        self.registry = registry
        self.profile = profile
        self.reasoning = reasoning
        # Per-request turn mode ("reasoning" | "instant" | "plan") and, for plan
        # mode, the phase ("planning" | "execute") computed server-side.
        self._mode = mode
        self._plan_phase = plan_phase
        # This run executes inside a hidden event-run conversation (both the
        # trigger turn and any user-reply turns). Gates the request_user_input
        # tool + its guidance, and drives the "park pending" turn-end below.
        self._event_run = event_run

        cfg = resolve_agent_config(profile)
        self._runtime_cfg = cfg
        self._max_llm_retries = cfg.max_llm_retries
        self._reasoning_temperature = cfg.reasoning_temperature
        self._reasoning_max_tokens = cfg.reasoning_max_tokens
        self._reasoning_retry = cfg.reasoning_retry
        self._tool_result_enabled = cfg.tool_result_enabled
        self._tool_result_max_tokens = cfg.tool_result_max_tokens
        self._enable_prompt_cache = cfg.enable_prompt_cache

        # Snapshot the tool list available to this profile for this run.
        tools = registry.tools_for_profile(profile)
        # Event runs must not register new watchers/events (recursive event
        # storms) AND should not even SEE the registration tools. The event-
        # CREATION leaves (schedule_create / register_file_watcher) and the skill
        # ``subscribe`` object are hidden from the ``tools=`` schema on event runs
        # — see ``_build_tools_and_dispatch`` and ``_skill_function_spec``. This is
        # gated on ``event_run`` (constant across the WHOLE event conversation:
        # the trigger turn AND later reply turns), so the tools block stays byte-
        # stable across an event run's own turns; event runs form a prompt-cache
        # population disjoint from chat, so hiding them costs no chat-run cache
        # hits. (An earlier divergence gated on the turn-varying
        # ``triggered_by_event`` was NOT stable within a conversation and dropped
        # event cache hits to ~29% — that is why the gate is ``event_run``.)
        # Dispatch-time refusal (``_is_event_blocked_leaf`` / ``_handle_skill_call``)
        # stays as a backstop for replayed-history or hallucinated calls.
        self._triggered_by_event = triggered_by_event
        # The ``reasoning`` think-tool exists only to give models WITHOUT native
        # step-by-step reasoning a place to think before acting. Drop it (and
        # skip its system-prompt guidance) for models that reason natively.
        native_reasoning = model_supports_reasoning(
            getattr(self.llm, "provider_name", ""),
            getattr(self.llm, "model_name", ""),
            profile=profile,
        )
        # Instant mode also drops the think-tool + its guidance for the turn (on
        # top of suppressing extended thinking and capping the turn at one round
        # of tool calls at the LLM call — see _loop).
        instant = self._mode == "instant"
        self._inject_reasoning_guidance = not native_reasoning and not instant
        if native_reasoning or instant:
            tools = [t for t in tools if t.tool_id != "reasoning"]
        # ``request_user_input`` lets an event run park itself 'pending' awaiting
        # the user's reply. It only makes sense inside an event-run conversation
        # (a normal chat turn ends by just asking in text). Withhold it
        # everywhere else. Event runs form their own prompt-cache population
        # (disjoint hidden conversations), so this divergence costs no chat-run
        # cache hits, and it's stable across an event run's own turns.
        if not event_run:
            tools = [t for t in tools if t.tool_id != "request_user_input"]
        # Plan-mode tools are exposed only on plan runs, and only in the matching
        # phase (mirrors the request_user_input gate above). Withholding them
        # everywhere else keeps reasoning/instant chat runs byte-identical to
        # today, so their prompt-cache prefix is unaffected.
        #   planning phase → ask_user_question, write_plan, update_todos
        #   execute  phase → update_todos only
        #   event run      → update_todos only (see below)
        # ``update_todos`` is ALSO exposed to event runs so a multi-step action
        # can drive a live todo panel in its hidden conversation. Event runs form
        # their own prompt-cache population (disjoint from chat), so adding it here
        # costs a one-time event-run cache miss and no chat-run cache hits, and
        # stays byte-stable across an event run's own turns.
        plan_planning = self._mode == "plan" and self._plan_phase == "planning"
        plan_execute = self._mode == "plan" and self._plan_phase == "execute"
        if not plan_planning:
            tools = [t for t in tools if t.tool_id not in ("ask_user_question", "write_plan")]
        if not (plan_planning or plan_execute or event_run):
            tools = [t for t in tools if t.tool_id != "update_todos"]
        # Whether the model may emit several tool calls in one turn. Default-on for
        # every provider; the agent already runs leaf calls concurrently (see the
        # asyncio.gather in _loop). A per-model TOML flag can opt a model out.
        self._parallel_tool_calls = model_parallel_tool_calls(
            getattr(self.llm, "provider_name", ""),
            getattr(self.llm, "model_name", ""),
        )
        # In the planning phase force one tool per step so ask_user_question /
        # write_plan can't be batched with other calls (they park + end the turn).
        if plan_planning:
            self._parallel_tool_calls = False
        # Image understanding only ever happens through the ``image_understanding``
        # tool. The Specialized Vision Model toggle picks *which* model runs it:
        # ON → a dedicated vision model; OFF → the main model (``self.llm``).
        # Expose the tool whenever the model that would run it can see images —
        # i.e. always when the feature is on, or (when off) only if the main model
        # itself supports vision. Withhold it only when off AND the main model is
        # text-only (mirrored in Settings → Tools).
        main_can_see = model_supports_vision(
            getattr(self.llm, "provider_name", "") or "",
            getattr(self.llm, "model_name", "") or "",
            profile=profile,
        )
        if not vision_feature_enabled(profile) and not main_can_see:
            tools = [t for t in tools if t.tool_id != "image_understanding"]
        self._tools = tools
        self._tools_by_id = {t.tool_id: t for t in self._tools}
        # Fallback-search guidance, built from the live enabled tool groups
        # (static for the run) so the system prompt stays byte-identical across
        # steps. Names only the search tools actually enabled for this profile,
        # using the exact function names those groups expose to the model.
        self._search_guidance = _build_search_guidance(self._tools)
        # Claude Code delegation guidance — present only when the (disabled-by-
        # default) claude_code tool is enabled for this profile; empty otherwise.
        self._claude_code_guidance = _build_claude_code_guidance(self._tools)

        self.max_steps = max_steps if max_steps is not None else cfg.max_steps
        self.current_step_count = 0

        # Skill tool_ids whose SKILL.md is present in this conversation's context.
        # Seeded at run() start from the replayed history (a skill stays "loaded"
        # only while its load tool call is still in the tail; it drops out once
        # compaction folds that call into the summary), then extended as new skills
        # load this turn. Drives ``_skill_function_spec``: a loaded skill's load
        # affordance is removed from the ``tools=`` block (subscribe-only, or
        # dropped entirely) so it cannot be re-loaded, and its call short-circuits.
        self._loaded_skill_ids: set[str] = set()

        # Per-turn token accounting (cached reads/writes split for cost attribution).
        self._total_input_tokens = 0
        self._total_cache_read_input_tokens = 0
        self._total_cache_creation_input_tokens = 0
        self._total_output_tokens = 0
        self._usage_records: list[UsageRecord] = []

        # In-turn native message trace (assistant tool_calls + tool results) and a
        # plain-text mirror of it for the optional reasoning-trace summary.
        self._turn_messages: List[Dict[str, Any]] = []
        self._trace_lines: List[str] = []
        # The terminating (toolless) step's text — the turn's final answer. Captured
        # separately from the UI ``content`` so the persisted ``llm_messages`` trace
        # ends with exactly the final-answer assistant message (no intermediate
        # narration mixed in).
        self._final_answer_text: str = ""

        if context_id is None:
            context_id = str(uuid.uuid4())
        self.context_id = context_id

    # ── usage attribution ─────────────────────────────────────────────────

    _SOURCE_KIND_BY_TOOL_TYPE = {
        ToolType.BUILTIN: "tool",
        ToolType.MCP: "tool",
        ToolType.SKILL: "tool",
    }

    def _provider_model_for(self, tool) -> tuple[str | None, str | None]:
        if tool is None or tool.tool_type is ToolType.SKILL:
            return self.llm.provider_name, getattr(self.llm, "model_name", None)
        adapter = getattr(tool, "adapter", None)
        inner = getattr(adapter, "_llm", None)
        if inner is not None:
            return getattr(inner, "provider_name", None), getattr(inner, "model_name", None)
        return None, None

    def _model_label_for(self, tool) -> str | None:
        if tool is None or tool.tool_type is ToolType.SKILL:
            return self.llm.model_label
        adapter = getattr(tool, "adapter", None)
        inner = getattr(adapter, "_llm", None)
        return getattr(inner, "model_label", None) if inner else self.llm.model_label

    def _record_reasoning_usage(self, response: dict) -> None:
        it = response.get("input_tokens") or 0
        cr = response.get("cache_read_input_tokens") or 0
        cc = response.get("cache_creation_input_tokens") or 0
        ot = response.get("output_tokens") or 0
        if not (it or cr or cc or ot):
            return
        self._usage_records.append(UsageRecord(
            source_kind="reasoning", tool_id=None, label=self.llm.model_label,
            provider=self.llm.provider_name, model=getattr(self.llm, "model_name", None),
            model_group=None, step_index=self.current_step_count,
            input_tokens=it, cache_read_input_tokens=cr,
            cache_creation_input_tokens=cc, output_tokens=ot,
        ))

    def _record_tool_usage(self, tool, token_usage: dict) -> None:
        it = token_usage.get("input_tokens", 0) or 0
        cr = token_usage.get("cache_read_input_tokens", 0) or 0
        cc = token_usage.get("cache_creation_input_tokens", 0) or 0
        ot = token_usage.get("output_tokens", 0) or 0
        if not (it or cr or cc or ot):
            return
        provider, model = self._provider_model_for(tool)
        self._usage_records.append(UsageRecord(
            source_kind=self._SOURCE_KIND_BY_TOOL_TYPE.get(tool.tool_type, "tool"),
            tool_id=tool.tool_id, label=getattr(tool, "name", None) or tool.tool_id,
            provider=provider, model=model, model_group=None,
            step_index=self.current_step_count,
            input_tokens=it, cache_read_input_tokens=cr,
            cache_creation_input_tokens=cc, output_tokens=ot,
        ))

    def _accumulate_tokens(self, token_usage: dict) -> None:
        self._total_input_tokens += token_usage.get("input_tokens", 0) or 0
        self._total_cache_read_input_tokens += token_usage.get("cache_read_input_tokens", 0) or 0
        self._total_cache_creation_input_tokens += token_usage.get("cache_creation_input_tokens", 0) or 0
        self._total_output_tokens += token_usage.get("output_tokens", 0) or 0

    def _token_fields(self) -> dict:
        return {
            "input_tokens": self._total_input_tokens,
            "cache_read_input_tokens": self._total_cache_read_input_tokens,
            "cache_creation_input_tokens": self._total_cache_creation_input_tokens,
            "output_tokens": self._total_output_tokens,
            "usage_records": [r.to_dict() for r in self._usage_records],
        }

    # ── config lookups ────────────────────────────────────────────────

    def _load_arguments(self, tool_id: str) -> dict:
        try:
            return self.registry.config.get_arguments(tool_id, self.profile)
        except Exception:  # noqa: BLE001
            return {}

    def _load_variables(self, tool_id: str) -> dict[str, str]:
        try:
            return self.registry.config.get_variables(tool_id, self.profile, include_secrets=True)
        except Exception:  # noqa: BLE001
            return {}

    def _tool_arguments(self, tool_id: str) -> dict:
        """Persisted client arguments (config defaults) for a tool.

        No run-kind injection here: event-triggered runs MUST send the exact same
        tool schema as normal runs (the ``tools=`` block is the front of the prompt
        cache prefix). Storm-prevention that used to filter the schema on event runs
        is now enforced at dispatch time in ``_loop`` / ``_handle_skill_call``.
        """
        return self._load_arguments(tool_id) if tool_id in self._tools_by_id else {}

    # ── prompt building ───────────────────────────────────────────────

    async def _load_long_term_memory_block(self) -> str:
        """Snapshot this profile's long-term memory as the ``{long_term_memory}`` section.

        Read ONCE per process per profile (cached in ``_LONG_TERM_MEMORY_SNAPSHOT``)
        and never refreshed on memory writes, so the system block stays byte-stable
        for prompt-cache reuse across every step AND turn. Facts remembered later in
        the session surface only after a process restart; they remain reachable
        in-session via the ``search_memory`` tool. Mirrors that tool's retrieval
        paths (:mod:`app.tools.builtin.search_memory`) but lists all facts rather
        than a query-ranked slice. Returns "" when memory is off, empty, or
        unreadable -- a snapshot failure must never break the turn.
        """
        if not resolve_memory_config(self.profile).enabled:
            return ""
        from types import SimpleNamespace

        from app.agent import memory_vectorstore
        from app.config.embedding_state import embedding_state

        facts: list[str] = []
        shim = SimpleNamespace(
            embedding=embedding_state.embedding, vector_store=embedding_state.vector_store,
        )
        if memory_vectorstore.vector_long_term_available(shim):
            rows = memory_vectorstore.list_long_term(
                agent=shim, profile=self.profile, limit=_MEMORY_SNAPSHOT_LIMIT,
            )
            facts = [r["content"] for r in rows if r.get("content")]
        else:
            try:
                from app.storage import get_memory_storage

                rows = await get_memory_storage().get_long_term(self.profile)
                facts = [r["content"] for r in rows if r.get("content")][:_MEMORY_SNAPSHOT_LIMIT]
            except Exception:  # noqa: BLE001
                logger.exception("[memory] long-term snapshot DB read failed")
                facts = []
        return _format_memory_block(facts)

    async def _ensure_long_term_memory_loaded(self) -> None:
        """Fill ``self._long_term_memory_block`` from the frozen per-process,
        per-profile snapshot, loading it once on first need. Called at the start of
        ``_loop`` (the run's init point) -- never inside ``_build_instruction`` --
        so a memory write never re-renders the system block and busts the cache
        prefix. Concurrent first-turns for a profile may both load; the result is
        identical, so last-write-wins is safe.
        """
        if self.profile not in _LONG_TERM_MEMORY_SNAPSHOT:
            _LONG_TERM_MEMORY_SNAPSHOT[self.profile] = await self._load_long_term_memory_block()
        self._long_term_memory_block = _LONG_TERM_MEMORY_SNAPSHOT[self.profile]

    def _build_instruction(self) -> str:
        override = (
            get_context(self.context_id, "_working_directory_override")
            if self.context_id else None
        )
        cwd = override or get_user_working_directory()
        instruction = SYSTEM_TEMPLATE.format(
            persona_description=read_persona_file(self.profile),  # raw; resolved below
            current_os=platform.system(),
            current_user_working_directory=cwd,
            reasoning_guidance=REASONING_GUIDANCE if self._inject_reasoning_guidance else "",
            search_guidance=self._search_guidance,
            claude_code_guidance=getattr(self, "_claude_code_guidance", ""),
            long_term_memory=self._long_term_memory_block,
        )
        # Render `$CREMIND_*` system-variable tokens (e.g. $CREMIND_PROFILE,
        # $CREMIND_AGENT_NAME) across the whole prompt -- persona body AND the
        # template's own tokens -- in one pass. Doing it AFTER .format() means a
        # token value that happens to contain braces can't break formatting, and
        # the persona is still resolved exactly once. The persona GET endpoint
        # keeps serving the raw file, so the editor still shows the template.
        resolved = resolve_system_var_tokens(instruction, self.profile)
        # Event-run guidance is appended (not a template slot) so ordinary chat
        # runs render byte-identical; event runs are a disjoint cache population,
        # and this block is stable across an event run's own turns. ``getattr``
        # tolerates skeleton agents built via ``__new__`` in tests.
        if getattr(self, "_event_run", False):
            resolved = resolved + EVENT_RUN_GUIDANCE
        # Plan-mode guidance is likewise appended (not a template slot) so
        # reasoning/instant runs render byte-identical. Plan runs are their own
        # cache population and this block is stable across a plan phase's turns.
        if getattr(self, "_mode", "reasoning") == "plan":
            phase = getattr(self, "_plan_phase", None)
            if phase == "execute":
                resolved = resolved + PLAN_MODE_EXECUTION_GUIDANCE
            else:
                resolved = resolved + PLAN_MODE_PLANNING_GUIDANCE
        return resolved

    def _render_input(self) -> str:
        """The volatile per-turn user message (just the query).

        Long-term memory is not injected into this volatile input — that would
        break the cached prefix. A frozen per-process snapshot of it now lives in
        the system block instead (see ``_load_long_term_memory_block``), and the
        model can still pull more on demand via the ``search_memory`` tool, so the
        [system + tools + history] prefix stays byte-stable for prompt caching.

        Plan and instant modes prepend a short marker HERE (adjacent to the task,
        where weaker models actually attend — appended system guidance alone is too
        easy to ignore). This is the volatile turn input, never part of the cached
        prefix, so it can't fragment the cache; reasoning returns the raw query
        byte-identically.
        """
        if self._mode == "instant":
            marker = (
                "[Instant mode: answer directly and fast. You may use AT MOST ONE "
                "round of tool calls this turn — if you need tools, batch everything "
                "into that single round; once the results arrive you must give your "
                "final answer in plain text.]"
            )
            return f"{marker}\n\n{self._current_query}"
        if self._mode == "plan":
            if self._plan_phase == "execute":
                marker = (
                    "[Plan mode — EXECUTION phase: carry out the approved plan and "
                    "keep the todo list current with `update_todos`. If it is an "
                    "automation-registration plan, just register the trigger with "
                    "the full action — do NOT run the task or drive a todo list.]"
                )
            else:
                marker = (
                    "[Plan mode — PLANNING phase: do NOT execute the task yet. "
                    "Work read-only; ask your clarifying questions with "
                    "`ask_user_question`, then call `write_plan` and stop.]"
                )
            return f"{marker}\n\n{self._current_query}"
        return self._current_query

    # ── tool spec assembly ─────────────────────────────────────────────

    def _skill_subscribe_spec(self, items: List[dict]) -> dict:
        """The ``subscribe`` object shared by an event-bearing skill's spec.

        Byte-identical whether or not the skill is loaded, so the ``tools=`` block
        stays cache-stable across event-triggered and normal runs for a given
        loaded-skill set.
        """
        names = [i["name"] for i in items]
        desc_lines = [
            f"- {i['name']}: {i.get('description', '')}".rstrip(": ").rstrip()
            for i in items
        ]
        return {
            "type": "object",
            "description": (
                "Subscribe this conversation to one or more of this skill's "
                "events so an action runs automatically whenever an event fires."
            ),
            "properties": {
                "trigger": {
                    "type": "array",
                    "items": {"type": "string", "enum": names},
                    "minItems": 1,
                    "uniqueItems": True,
                    "description": (
                        "One or more event names declared by this skill. "
                        "Available events:\n" + "\n".join(desc_lines)
                    ),
                },
                "action": {
                    "type": "string",
                    "description": (
                        "WHAT to do when an event fires, capturing the user's "
                        "full request. Preserve every detail, condition, "
                        "qualifier, recipient, and format in the user's own "
                        "wording and language — do NOT summarize, shorten, or "
                        "omit information. Only leave out the bare event name "
                        "itself (it is captured in `trigger`); keep any "
                        "conditions that decide WHEN to act (e.g. 'only if "
                        "from my boss'). The action MAY be a multi-line, "
                        "step-by-step procedure; when a plan for this automation "
                        "exists, embed its full per-fire steps here so they run "
                        "on every fire. The action runs later in a FRESH "
                        "conversation with no access to this one: inline every "
                        "concrete value verbatim (full URLs, email addresses, "
                        "file paths, IDs, criteria) — never write 'the provided "
                        "X' or 'the X above'."
                    ),
                },
            },
            "required": ["trigger", "action"],
            "additionalProperties": False,
        }

    def _skill_function_spec(self, tool, *, loaded: bool) -> Optional[dict]:
        """Build the native function spec for one skill tool, or ``None`` to omit it.

        The spec depends on whether the skill is already loaded this turn
        (``_loaded_skill_ids``):

        - **Not loaded** → expose ``request`` (load-and-use) plus, for
          event-bearing skills, the ``subscribe`` object.
        - **Loaded, has events** → expose ``subscribe`` ONLY. The SKILL.md is
          already in history, so re-loading is pointless; dropping ``request``
          makes a re-load call impossible while keeping the skill subscribable.
        - **Loaded, no events** → return ``None`` so the stub is dropped from the
          ``tools=`` block entirely; the model must act on the loaded instructions
          (via Exec Shell), not by re-calling the skill.

        The spec is therefore byte-stable GIVEN a fixed loaded-skill set; it
        changes exactly once when a skill loads — a deliberate one-time prompt-cache
        break that makes skill-load loops impossible — and reverts only if
        compaction folds the load call out of the replayed tail (the content is
        then genuinely gone from history, so re-loading is correct). Event enums
        come from static metadata. The ``subscribe`` block stays present on every
        run — including event-triggered ones — so recursive event storms are
        prevented at dispatch time instead: ``_handle_skill_call`` refuses a
        ``subscribe`` call when ``_triggered_by_event`` is set.
        """
        items = self._skill_event_items(tool)
        # Event runs never see the `subscribe` object — an automation must not
        # register further event subscriptions (recursive storms). Gated on
        # ``_event_run`` (constant across the whole event conversation) so the
        # tools block stays byte-stable across its turns. A not-loaded event
        # skill then exposes only ``request`` (still loadable/usable); a loaded
        # event skill's stub drops entirely (subscribe_spec is None → return None).
        if getattr(self, "_event_run", False):
            items = []
        subscribe_spec = self._skill_subscribe_spec(items) if items else None

        if loaded:
            if subscribe_spec is None:
                return None  # nothing left to expose — drop the stub entirely
            properties: Dict[str, Any] = {"subscribe": subscribe_spec}
            description = (
                f"{tool.description} Its instructions are already loaded in this "
                "conversation — follow them directly to act (do NOT call this to "
                "use it). Call this ONLY to subscribe to one of its events."
            )
            parameters: Dict[str, Any] = {
                "type": "object",
                "properties": properties,
                "required": ["subscribe"],
            }
        else:
            properties = {
                SKILL_REQUEST_ARG: {
                    "type": "string",
                    "description": (
                        "What you want this skill to do right now (one-shot use). "
                        "Provide this to load and use the skill."
                    ),
                },
            }
            if subscribe_spec is not None:
                properties["subscribe"] = subscribe_spec
            description = (
                f"{tool.description} Call this to use the skill; pass the "
                "user's request. The skill's instructions load on first use."
            )
            parameters = {"type": "object", "properties": properties}

        return {
            "type": "function",
            "function": {
                "name": tool.tool_id,
                "description": description,
                "parameters": parameters,
            },
        }

    def _build_tools_and_dispatch(self) -> Tuple[List[dict], Dict[str, tuple]]:
        """Flatten enabled tools' leaves into native function specs + a dispatch map.

        Returns ``(specs, dispatch)`` where ``dispatch[name]`` is
        ``("leaf", tool, leaf_name)`` for built-in/MCP sub-tools or
        ``("skill", tool, None)`` for skill functions. A skill's spec depends on
        whether it is already loaded (see ``_skill_function_spec``): the block is
        byte-stable for a fixed loaded-skill set and changes once per skill load,
        so re-loading an already-loaded skill is made impossible.
        """
        specs: List[dict] = []
        dispatch: Dict[str, tuple] = {}
        # Per-profile disabled sub-tools ("leaves"), resolved in one read.
        disabled_by_tool = self.registry.disabled_leaves_by_tool(self.profile)

        for tool in self._tools:
            if tool.tool_type is ToolType.SKILL:
                loaded = tool.tool_id in self._loaded_skill_ids
                spec = self._skill_function_spec(tool, loaded=loaded)
                if spec is not None:
                    specs.append(spec)
                # Keep the dispatch entry even when the spec is omitted: a
                # same-step parallel duplicate call, or a model that echoes the
                # now-removed function name, must still route to the already-loaded
                # short-circuit rather than "Unknown tool '<skill>'".
                dispatch[tool.tool_id] = ("skill", tool, None)
                continue

            try:
                leaf_specs = tool.leaf_function_specs(
                    context_id=self.context_id,
                    profile=self.profile,
                    query=self._current_query,
                    arguments=self._tool_arguments(tool.tool_id),
                )
            except Exception:  # noqa: BLE001
                logger.exception(f"leaf_function_specs failed for '{tool.tool_id}'")
                continue
            disabled = disabled_by_tool.get(tool.tool_id, ())
            for fs in leaf_specs:
                if fs.leaf_name in disabled:
                    continue  # sub-tool disabled for this profile
                # Event runs never SEE the event-CREATION leaves (schedule_create /
                # register_file_watcher): an automation must not register further
                # automations. Keep the dispatch entry as a backstop (a replayed or
                # hallucinated call is still gracefully refused, not "Unknown tool");
                # drop only the schema so the model isn't offered the tool.
                if not self._is_event_blocked_leaf(("leaf", tool, fs.leaf_name)):
                    specs.append(fs.schema)
                dispatch[fs.name] = ("leaf", tool, fs.leaf_name)

        return specs, dispatch

    # Event-CREATION leaves: their schema stays exposed on every run (byte-stable
    # tools prefix) but their EXECUTION is blocked anywhere inside an event-run
    # conversation (the trigger turn AND later reply turns) to stop recursive
    # event storms — an action that registers another event would re-register on
    # every fire. De-registration leaves (schedule_cancel, delete_file_watcher)
    # are intentionally NOT listed: they can't storm, and an event action may
    # legitimately cancel/remove a schedule or watcher. Matched on the bare leaf
    # name + owning tool_id (the model emits the namespaced
    # ``system_file__register_file_watcher``, but the dispatch entry carries the
    # bare ``register_file_watcher`` in ``entry[2]``).
    _EVENT_BLOCKED_LEAVES: frozenset = frozenset({
        ("system_file", "register_file_watcher"),
        ("scheduler", "schedule_create"),
    })

    def _is_event_blocked_leaf(self, entry) -> bool:
        return (
            (self._triggered_by_event or getattr(self, "_event_run", False))
            and bool(entry)
            and entry[0] == "leaf"
            and (entry[1].tool_id, entry[2]) in self._EVENT_BLOCKED_LEAVES
        )

    # Mutating leaves refused (at dispatch, schema still exposed) during the plan
    # PLANNING phase: planning is strictly read-only, so nothing changes on the
    # system before the user accepts the plan. Read-only leaves (search/grep/list/
    # read/get_file_info, exec_shell_output/_stop) stay allowed so the agent can
    # research. Guidance alone isn't enough — a weaker model will run these anyway.
    _PLAN_BLOCKED_LEAVES: frozenset = frozenset({
        ("exec_shell", "exec_shell"),
        ("exec_shell", "exec_shell_input"),
        ("system_file", "write_file"),
        ("system_file", "overwrite_file"),
        ("system_file", "move_file"),
        ("system_file", "copy_file"),
        ("system_file", "register_file_watcher"),
        ("system_file", "delete_file_watcher"),
        ("scheduler", "schedule_create"),
        ("scheduler", "schedule_cancel"),
    })

    def _is_plan_blocked_leaf(self, entry) -> bool:
        return (
            self._mode == "plan"
            and self._plan_phase == "planning"
            and bool(entry)
            and entry[0] == "leaf"
            and (entry[1].tool_id, entry[2]) in self._PLAN_BLOCKED_LEAVES
        )

    # ── main entry point ──────────────────────────────────────────────

    async def run(
        self,
        input: str,
        history_messages: List["ChatCompletionMessageParam"],
    ) -> AsyncGenerator[ReasoningStreamResponseType, None]:
        self.history_messages = history_messages or []
        self._current_query = input
        self._usage_records = []
        self._turn_messages = []
        self._final_answer_text = ""
        self.current_step_count = 0
        # A skill is "loaded" iff its SKILL.md load call is still in the replayed
        # history (it drops out once compaction folds it past the watermark). Mirror
        # the derived set into ContextStorage for change_working_directory.
        self._loaded_skill_ids = self._derive_loaded_skills_from_history()
        if self.context_id:
            set_context(self.context_id, LOADED_SKILLS_KEY, sorted(self._loaded_skill_ids))
            # Expose the raw query so registration tools' self-containment gate can
            # compare a frozen action against the request that asked for it.
            set_context(self.context_id, CURRENT_QUERY_KEY, input)
        async for item in self._loop():
            yield item

    # ── reasoning loop ────────────────────────────────────────────────

    async def _loop(self) -> AsyncGenerator[ReasoningStreamResponseType, None]:
        llm_retry = 0
        overflow_retry = 0
        await self._ensure_long_term_memory_loaded()
        while True:
            if self.current_step_count >= self.max_steps:
                final = (
                    f"I've reached the maximum number of steps ({self.max_steps}) "
                    "for this turn. Here's what I have so far."
                )
                yield self._final_chunk(final)
                return

            self.current_step_count += 1
            instruction = self._build_instruction()
            specs, dispatch = self._build_tools_and_dispatch()

            # Instant mode: at most ONE round of tool calls per turn. Step 1
            # offers tools normally; from step 2 the model must answer in text.
            # Tools stay attached (Anthropic 400s on tool_use history without a
            # tools param, and dropping them would bust the tools+system cache
            # prefix) — tool_choice "none" forbids new calls instead.
            instant_final = self._mode == "instant" and self.current_step_count >= 2

            messages: List["ChatCompletionMessageParam"] = [
                {"role": "system", "content": instruction},
                *self.history_messages,
                {"role": "user", "content": self._render_input()},
                *self._turn_messages,
            ]

            assistant_parts: List[str] = []
            tool_calls: List[dict] = []
            finish_reason = None
            try:
                async for resp in self.llm.chat_completion_stream(
                    messages=messages,
                    tools=specs or None,
                    tool_choice=("none" if instant_final else "auto") if specs else None,
                    parallel_tool_calls=self._parallel_tool_calls,
                    temperature=self._reasoning_temperature,
                    max_tokens=self._reasoning_max_tokens,
                    # Instant mode: an explicit empty string suppresses the
                    # provider's default reasoning_effort (falsy in every
                    # provider's ``if _re:`` guard) without touching the shared
                    # client; None preserves today's default-fallback behavior.
                    reasoning_effort="" if self._mode == "instant" else None,
                    retry=self._reasoning_retry,
                    args={"prompt_cache": True} if self._enable_prompt_cache else None,
                ):
                    rtype = resp["type"]
                    if rtype == ChatCompletionTypeEnum.CONTENT:
                        data = resp.get("data")
                        if data:
                            assistant_parts.append(data)
                            # Real, native token streaming to the user.
                            yield {"type": ChatCompletionTypeEnum.CONTENT, "data": data}
                    elif rtype == ChatCompletionTypeEnum.FUNCTION_CALLING:
                        fns = (resp.get("data") or {}).get("function")
                        if fns:
                            tool_calls = fns
                    elif rtype == ChatCompletionTypeEnum.DONE:
                        self._accumulate_tokens(resp)
                        self._record_reasoning_usage(resp)
                        finish_reason = resp.get("finish_reason")
            except AgentException as err:
                logger.error(f"LLM call failed at step {self.current_step_count}: {err}")
                # L3b — a context overflow (rare: the pre-flight floor should prevent
                # it) is never fixed by an identical retry. Clip history harder and
                # retry the step once; halving guarantees forward progress.
                is_overflow = (
                    getattr(err, "code", None) == Status.LLM_CONTEXT_OVERFLOW
                    or is_context_overflow(err)
                )
                if is_overflow and overflow_retry < 1:
                    from app.agent.compaction import enforce_ceiling, estimate_prompt_tokens
                    overflow_retry += 1
                    target = max(1024, int(estimate_prompt_tokens(self.history_messages) * 0.5))
                    self.history_messages = enforce_ceiling(self.history_messages, target)
                    self.current_step_count -= 1
                    logger.warning(
                        f"[reasoning] context overflow at step {self.current_step_count + 1}; "
                        f"clipped history to ~{target} tokens and retrying once"
                    )
                    continue
                if llm_retry < self._max_llm_retries:
                    llm_retry += 1
                    self.current_step_count -= 1
                    continue
                yield self._final_chunk(
                    "I encountered an error processing your request. Please try again."
                )
                return

            assistant_text = "".join(assistant_parts)

            if instant_final and tool_calls:
                # A backend that ignores tool_choice="none" (e.g. some Ollama
                # builds) can still emit calls on the forced-final step. Never
                # run a second tool round: drop the calls and end the turn.
                logger.warning(
                    f"[instant] dropped {len(tool_calls)} tool call(s) on the forced-final step"
                )
                tool_calls = []
                if not assistant_text:
                    yield self._final_chunk(
                        "I reached Instant Mode's one-round tool limit before I could "
                        "finish. Try Reasoning mode for multi-step work."
                    )
                    return

            if not tool_calls:
                # Plain text with no tool call == the final answer (already streamed).
                if not assistant_text and finish_reason == "length":
                    yield self._final_chunk(
                        "I couldn't finish the response within the configured length."
                    )
                    return
                # Remember the terminating step's text so the persisted reasoning
                # trace ends with the real final-answer assistant message.
                self._final_answer_text = assistant_text
                yield self._final_chunk("")  # answer was streamed via CONTENT
                return

            # Record the assistant turn (text + tool calls) for the model's context.
            self._turn_messages.append({
                "role": "assistant",
                "content": assistant_text or None,
                "tool_calls": [
                    {
                        "id": tc.get("id") or f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": tc.get("name") or "",
                            "arguments": json.dumps(_coerce_args(tc.get("arguments"))),
                        },
                    }
                    for i, tc in enumerate(tool_calls)
                ],
            })

            # Execute the step's tool calls. One THINKING artifact per call is
            # emitted up-front so the UI shows the whole step's tool set; leaf
            # tools then run CONCURRENTLY (parallel tool calls), while skill
            # calls run sequentially (they mutate the system prompt).
            step_no = self.current_step_count
            resolved: list[tuple] = []  # (call_id, name, args, entry)
            for i, tc in enumerate(tool_calls):
                call_id = tc.get("id") or f"call_{i}"
                name = tc.get("name") or ""
                args = _coerce_args(tc.get("arguments"))
                entry = dispatch.get(name)
                resolved.append((call_id, name, args, entry))
                tool = entry[1] if entry else None
                # For a skill *load* call the recorded request is overwritten to a
                # fixed marker (see _handle_skill_call); show that same marker in
                # the UI Thinking Process so it matches the trace the model sees.
                display_args = (
                    self._skill_call_display_args(tool, args)
                    if entry and entry[0] == "skill" else args
                )
                yield self._thinking_artifact(step_no, call_id, name, display_args, tool)

            # Storm-blocked leaves (event runs) and mutating leaves (plan-mode
            # planning phase) keep their schema but are NOT executed; they still
            # get a paired role:"tool" result below so the turn's tool_calls group
            # is fully answered (an unanswered tool_use would 400 on replay / be
            # truncated by _normalize_turn_messages).
            leaf_calls = [
                (c, n, a, e) for (c, n, a, e) in resolved
                if e and e[0] == "leaf"
                and not self._is_event_blocked_leaf(e)
                and not self._is_plan_blocked_leaf(e)
            ]
            outcomes: Dict[str, "_LeafOutcome"] = {}
            if leaf_calls:
                gathered = await asyncio.gather(*[
                    self._collect_leaf(tool=e[1], leaf_name=e[2], args=a, call_id=c)
                    for (c, n, a, e) in leaf_calls
                ])
                outcomes = {o.call_id: o for o in gathered}

            # Emit results + append the role:"tool" messages in call order.
            for call_id, name, args, entry in resolved:
                if entry is None:
                    obs = f"Unknown tool '{name}'."
                    self._append_tool_result(call_id, obs)
                    yield self._result_artifact(step_no, call_id, [Part(root=TextPart(text=obs))])
                    continue
                if entry[0] == "skill":
                    async for item in self._handle_skill_call(entry[1], args, call_id, step_no):
                        yield item
                    continue
                if self._is_event_blocked_leaf(entry):
                    obs = (
                        "Registering a new event or automation (schedule, file "
                        "watcher, or skill subscription) is not allowed from "
                        "inside an event-triggered run — this prevents recursive "
                        "event storms where an action keeps creating more events. "
                        "Ignoring this request."
                    )
                    self._append_tool_result(call_id, obs, fn_name=name)
                    yield self._result_artifact(
                        step_no, call_id, [Part(root=TextPart(text=obs))]
                    )
                    continue
                if self._is_plan_blocked_leaf(entry):
                    obs = (
                        "Plan mode (planning phase) is READ-ONLY — this action was "
                        "NOT executed. Do not try to carry out the task yet. "
                        "Research with read-only tools, ask the user what you need "
                        "with `ask_user_question`, and call `write_plan` when ready; "
                        "execution begins only after the user accepts the plan."
                    )
                    self._append_tool_result(call_id, obs, fn_name=name)
                    yield self._result_artifact(
                        step_no, call_id, [Part(root=TextPart(text=obs))]
                    )
                    continue
                outcome = outcomes.get(call_id)
                if outcome is None:
                    continue
                for status_chunk in outcome.status_chunks:
                    yield status_chunk
                self._append_tool_result(call_id, outcome.tool_text, fn_name=name)
                yield self._result_artifact(step_no, call_id, outcome.parts)

            # Event run parked pending: the agent called request_user_input this
            # step (the tool recorded the question in run_state, keyed by the
            # stream run id). Its tool group is fully answered above, so end the
            # turn now — the question becomes the final assistant message and the
            # user's reply arrives as the next turn.
            if getattr(self, "_event_run", False):
                from app.events import run_state
                from app.utils.task_context import current_task_id_var
                run_id = current_task_id_var.get()
                question = run_state.get_pending(run_id) if run_id else None
                if question:
                    self._final_answer_text = question
                    yield self._final_chunk(question)
                    return

            # Plan mode: surface any UI events the plan tools queued this step
            # (ask_user_question / plan_ready / todos) as PLAN_EVENT chunks the
            # stream runner translates to bus events. Then, if the agent asked
            # questions or wrote a plan, park + end the turn (like event runs):
            # the readable questions / plan markdown becomes the final assistant
            # message and the user's answer/decision arrives as the next turn.
            # Event runs also drain here: they expose ``update_todos`` (not the
            # question/plan tools), so their ``todos`` emits reach the bus while
            # ``parked_q``/``parked_p`` stay None — no false parking.
            if getattr(self, "_mode", "reasoning") == "plan" or getattr(self, "_event_run", False):
                from app.agent import plan_state
                from app.utils.task_context import current_task_id_var
                run_id = current_task_id_var.get()
                if run_id:
                    for item in plan_state.drain_emit(run_id):
                        yield {
                            "type": ChatCompletionTypeEnum.PLAN_EVENT,
                            "data": item,
                        }
                    parked_q = plan_state.get_questions(run_id)
                    parked_p = plan_state.get_plan(run_id)
                    if parked_q or parked_p:
                        final = _plan_parked_final_text(parked_q, parked_p)
                        self._final_answer_text = final
                        yield self._final_chunk(final)
                        return

    def _final_chunk(self, data: str) -> Dict[str, Any]:
        """Terminal DONE chunk. ``data`` is empty when the answer was streamed.

        Carries the turn's native reasoning trace (``llm_messages``) for persistence.
        The loaded-skill set is NOT cleared here: it is re-derived from history at
        the next run() and the ContextStorage mirror must survive across turns (so
        change_working_directory still resolves loaded skills). It resets naturally
        once compaction folds a skill's load call out of the replayed tail.
        """
        self._loaded_skill_ids = set()
        if self.context_id:
            clear_context(self.context_id, "current_shell_directory")
        final_answer = data or self._final_answer_text
        return {
            "type": ChatCompletionTypeEnum.DONE,
            "data": data,
            "llm_messages": self._build_llm_messages(final_answer),
            **self._token_fields(),
        }

    def _build_llm_messages(self, final_answer: str) -> Optional[List[Dict[str, Any]]]:
        """Assemble the turn's canonical native message trace for replay/persistence.

        ``= self._turn_messages (reasoning steps) + the final-answer assistant
        message``. Returns ``None`` when no tools were called (a direct answer needs
        no separate trace — it replays content-only). The trace is pairing-validated
        so every ``tool_call`` id is answered by a following ``role:"tool"`` message
        (a dangling ``tool_use`` would make Anthropic/OpenAI 400 on replay).
        """
        msgs = self._normalize_turn_messages(self._turn_messages)
        if not msgs:
            return None
        if final_answer:
            msgs = msgs + [{"role": "assistant", "content": final_answer}]
        return msgs

    @staticmethod
    def _normalize_turn_messages(
        turn_messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Return a copy with any trailing, unanswered assistant ``tool_calls`` group
        dropped (along with anything after it), so the trace is replay-safe.

        Tool results are appended immediately after each assistant ``tool_calls``
        message, so only the final group can be incomplete (e.g. an error/cancel
        after the call but before all results). Truncating before that group keeps
        every remaining ``tool_use`` paired with its ``tool_result``.
        """
        msgs = list(turn_messages)
        answered = {
            m.get("tool_call_id") for m in msgs if m.get("role") == "tool"
        }
        for i in range(len(msgs) - 1, -1, -1):
            m = msgs[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                ids = {tc.get("id") for tc in m["tool_calls"]}
                if not ids.issubset(answered):
                    return msgs[:i]
                break
        return msgs

    # ── per-call dispatch ──────────────────────────────────────────────

    def _thinking_artifact(self, step: int, call_id: str, tool_name: str,
                           args: Dict[str, Any], tool) -> Dict[str, Any]:
        """UI artifact announcing one tool call in a step (Thought removed)."""
        return {
            "type": ChatCompletionTypeEnum.THINKING_ARTIFACT,
            "data": {
                "Step": step,
                "Call_Id": call_id,
                "Tool": tool_name,
                "Tool_Input": json.dumps(args, ensure_ascii=False),
                "Model_Label": self._model_label_for(tool) if tool is not None else self.llm.model_label,
            },
        }

    def _result_artifact(self, step: int, call_id: str, parts: List[Part]) -> Dict[str, Any]:
        return {
            "type": ChatCompletionTypeEnum.RESULT_ARTIFACT,
            "data": {"Step": step, "Call_Id": call_id, "Result": parts},
        }

    @staticmethod
    def _render_result_text(parts: List[Part], fallback: str) -> str:
        """Render tool result parts as readable text for the model.

        Structured (``DataPart``) results are rendered with ``dict_to_text``
        (YAML-like) instead of raw JSON so the model reads them more reliably.
        """
        chunks: list[str] = []
        for p in parts:
            root = getattr(p, "root", p)
            if isinstance(root, DataPart):
                data = root.data
                chunks.append(dict_to_text(data) if isinstance(data, dict) else str(data))
            elif isinstance(root, TextPart):
                if root.text:
                    chunks.append(root.text)
        rendered = "\n".join(c for c in chunks if c)
        return rendered or fallback

    async def _collect_leaf(
        self, *, tool, leaf_name: str,
        args: Dict[str, Any], call_id: str,
    ) -> "_LeafOutcome":
        """Run one leaf tool to completion and COLLECT its output (no yielding).

        Returns a :class:`_LeafOutcome` so several leaves can run concurrently
        via ``asyncio.gather``; the caller emits the collected chunks in order.
        """
        status_chunks: list[dict] = []

        client_args = self._tool_arguments(tool.tool_id)
        variables = self._load_variables(tool.tool_id)

        result_event: Optional[ToolResultEvent] = None
        error_event: Optional[ToolErrorEvent] = None
        try:
            async for ev in tool.execute_leaf(
                leaf_name=leaf_name,
                args=dict(args),
                context_id=self.context_id,
                profile=self.profile,
                arguments=client_args,
                variables=variables,
            ):
                if isinstance(ev, ToolThinkingEvent):
                    continue
                if isinstance(ev, ToolStatusEvent):
                    status_chunks.append(
                        {"type": ChatCompletionTypeEnum.STATUS_UPDATE, "data": ev.raw}
                    )
                elif isinstance(ev, ToolResultEvent):
                    result_event = ev
                elif isinstance(ev, ToolErrorEvent):
                    error_event = ev
                    break
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Tool '{tool.tool_id}' leaf '{leaf_name}' raised")
            error_event = ToolErrorEvent(message=str(e))

        if error_event is not None:
            msg = error_event.message
            return _LeafOutcome(call_id, status_chunks, msg,
                                [Part(root=TextPart(text=msg))])
        if result_event is None:
            return _LeafOutcome(call_id, status_chunks, "No result.",
                                [Part(root=TextPart(text="No result."))])

        if result_event.token_usage:
            self._accumulate_tokens(result_event.token_usage)
            self._record_tool_usage(tool, result_event.token_usage)

        parts = result_event.observation_parts or [
            Part(root=TextPart(text=result_event.observation_text or ""))
        ]
        text = self._render_result_text(parts, result_event.observation_text or "")
        return _LeafOutcome(call_id, status_chunks, text, parts)

    def _append_tool_result(
        self, call_id: str, observation_text: str, fn_name: str | None = None,
        *, truncate: bool = True,
    ) -> None:
        """Append the native ``role:"tool"`` message for the model's context.

        ``truncate=False`` skips the per-tool token clamp — used for a skill load
        result so the full SKILL.md content reaches (and stays in) the model's
        context.
        """
        text = observation_text or "No result"
        if truncate and self._tool_result_enabled:
            text = truncate_to_tokens(text, self._tool_result_max_tokens)
        self._turn_messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": text,
        })

    # ── skills ─────────────────────────────────────────────────────────

    @staticmethod
    def _skill_event_items(tool) -> List[Dict[str, Any]]:
        """The skill's declared events (``metadata.events.event_type``).

        Returns a list of ``{name, description?}`` dicts (entries without a
        ``name`` are dropped); empty when the skill declares no events.
        """
        info = getattr(tool, "info", None)
        metadata = getattr(info, "metadata", None) if info is not None else None
        if not isinstance(metadata, dict):
            return []
        events = metadata.get("events") or {}
        if not isinstance(events, dict):
            return []
        items = events.get("event_type") or []
        if not isinstance(items, list):
            return []
        return [i for i in items if isinstance(i, dict) and i.get("name")]

    def _render_events_hint(self, tool) -> str:
        """Note appended to a skill's load result describing its events + how to
        subscribe (via this same skill tool's ``subscribe`` field)."""
        items = self._skill_event_items(tool)
        if not items:
            return ""
        bullets = [
            f"- {i['name']}: {i['description']}" if i.get("description") else f"- {i['name']}"
            for i in items
        ]
        return (
            "## Automatic actions on events\n"
            "This skill can run an action automatically whenever one of these "
            "events fires:\n"
            + "\n".join(bullets)
            + "\n\nIf the user wants that, call this same skill again with a "
            "`subscribe` object: `trigger` = one or more of the event names above, "
            "`action` = what to do when an event fires, preserving the user's full "
            "request and wording (every detail and condition) — do not summarize it. "
            "The action may be a multi-line, step-by-step procedure and should embed "
            "the full plan-derived steps when a plan for this automation exists. It "
            "runs later in a fresh conversation with no access to this one, so inline "
            "every concrete value verbatim (URLs, emails, paths, IDs) — never 'the "
            "provided X'."
        )

    @staticmethod
    def _is_skill_subscribe_args(args: Dict[str, Any]) -> bool:
        """True when a skill call carries a ``subscribe`` payload (event mode).

        A load call has no ``subscribe``; this distinguishes the two modes both
        when dispatching a live call and when deriving loaded skills from history.
        """
        sub = args.get("subscribe") if isinstance(args, dict) else None
        return isinstance(sub, dict) and bool(sub.get("trigger"))

    def _derive_loaded_skills_from_history(self) -> set[str]:
        """Skill tool_ids whose SKILL.md load call is still in the replayed history.

        Scans assistant ``tool_calls`` for skill functions whose args are *load*
        args (not a ``subscribe`` payload). Because SKILL.md now rides the load
        call's tool result, a skill counts as loaded only while that call remains
        in the tail — it drops out for free once compaction folds it away.
        """
        skill_ids = {t.tool_id for t in self._tools if t.tool_type is ToolType.SKILL}
        if not skill_ids:
            return set()
        loaded: set[str] = set()
        for msg in self.history_messages:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            for tc in (msg.get("tool_calls") or []):
                fn = tc.get("function") or {}
                name = fn.get("name")
                if name not in skill_ids or name in loaded:
                    continue
                if not self._is_skill_subscribe_args(_coerce_args(fn.get("arguments"))):
                    loaded.add(name)
        return loaded

    def _skill_call_display_args(self, tool, args: Dict[str, Any]) -> Dict[str, Any]:
        """Args to SHOW for a skill call in the UI Thinking Process.

        Mirrors ``_handle_skill_call``'s branch: a subscribe call shows its
        ``subscribe`` payload verbatim (even on an event-triggered run, where the
        call is refused — the displayed payload matches what the model emitted and
        the refusal trace); any other (load) call shows the same fixed marker the
        recorded ``request`` is overwritten to, so the UI step matches the trace
        the model actually sees.
        """
        if self._is_skill_subscribe_args(args):
            return args
        return {SKILL_REQUEST_ARG: SKILL_LOAD_REQUEST.format(name=tool.name)}

    def _set_skill_call_request(self, call_id: str, text: str) -> None:
        """Overwrite the recorded skill tool-call's args to ``{request: text}``.

        Keeps the persisted/replayed trace deterministic regardless of what the
        model typed, so the cached history prefix stays byte-stable.
        """
        payload = json.dumps({SKILL_REQUEST_ARG: text})
        for msg in reversed(self._turn_messages):
            if msg.get("role") != "assistant":
                continue
            for tc in (msg.get("tool_calls") or []):
                if tc.get("id") == call_id:
                    tc.setdefault("function", {})["arguments"] = payload
                    return

    async def _handle_skill_call(
        self, tool, args: Dict[str, Any], call_id: str, step_no: int,
    ) -> AsyncGenerator[ReasoningStreamResponseType, None]:
        """Handle a model call to a skill function.

        Two modes, distinguished by the args shape:

        - ``subscribe`` present -> register an event subscription for THIS skill
          (pinned by its own tool_id/source dir — no active-skill state).
        - otherwise -> load: on first call, overwrite the recorded ``request`` to
          a fixed marker and return the full SKILL.md as the tool result so later
          steps (and replayed turns) carry the instructions. A repeat call (the
          content is already in context) short-circuits.
        """
        dir_path = tool.info.dir_path  # type: ignore[attr-defined]
        skill_md_path = dir_path / "SKILL.md"

        # ── subscribe path: register an event for this exact skill ──────────
        if self._is_skill_subscribe_args(args):
            # The ``subscribe`` block is always in the spec (byte-stable tools
            # prefix), so the model can emit it even inside an event run. Refuse
            # it there (trigger turn AND reply turns): subscribing during an event
            # run risks a recursive event storm (event → reasoning → subscribe →
            # event → …).
            if self._triggered_by_event or getattr(self, "_event_run", False):
                obs = (
                    "Subscriptions cannot be created from inside an "
                    "event-triggered run (this prevents recursive event loops). "
                    "Ignoring this subscribe request."
                )
                self._append_tool_result(call_id, obs, fn_name=tool.tool_id)
                yield self._result_artifact(
                    step_no, call_id, [Part(root=TextPart(text=obs))]
                )
                return
            # Registration is a mutation; the plan PLANNING phase is read-only
            # (mirrors _PLAN_BLOCKED_LEAVES for the leaf tools). A subscribe is a
            # "skill" dispatch entry, not a "leaf", so that gate can't catch it —
            # refuse it here. Registration happens in the EXECUTION phase.
            if self._mode == "plan" and self._plan_phase == "planning":
                obs = (
                    "Plan mode (planning phase) is READ-ONLY — no event "
                    "subscription was created. Registration happens in the "
                    "EXECUTION phase, after the user accepts the plan. For now, "
                    "capture the full per-fire action in the plan you write with "
                    "`write_plan`."
                )
                self._append_tool_result(call_id, obs, fn_name=tool.tool_id)
                yield self._result_artifact(
                    step_no, call_id, [Part(root=TextPart(text=obs))]
                )
                return
            from app.tools.builtin.register_skill_event import (
                register_skill_events,
                _normalize_triggers,
            )
            sub = args.get("subscribe") or {}
            obs = await register_skill_events(
                profile=self.profile,
                context_id=self.context_id or "",
                skill_id=tool.tool_id,
                skill_source=str(dir_path),
                triggers=_normalize_triggers(sub.get("trigger")),
                action=(sub.get("action") or "").strip(),
                request_context=self._current_query or "",
            )
            self._append_tool_result(call_id, obs, fn_name=tool.tool_id)
            yield self._result_artifact(step_no, call_id, [Part(root=TextPart(text=obs))])
            return

        # ── load path ───────────────────────────────────────────────────────
        canonical = SKILL_LOAD_REQUEST.format(name=tool.name)
        self._set_skill_call_request(call_id, canonical)

        if tool.tool_id in self._loaded_skill_ids:
            obs = (
                f"Skill '{tool.name}' is already loaded in this conversation; its "
                "instructions are available above. Do NOT call this tool again. To "
                "act on those instructions, run the skill's commands with the Exec "
                f"Shell tool using '{tool.tool_id}' as the <skill-name> argument."
            )
            self._append_tool_result(call_id, obs, fn_name=tool.tool_id, truncate=False)
            yield self._result_artifact(
                step_no, call_id,
                [Part(root=TextPart(text=f"[Skill already loaded: {skill_md_path}]"))],
            )
            return

        # Build the full SKILL.md result. The guidance that used to live in the
        # system prompt now rides this tool result, so the system prompt stays
        # byte-stable across skill loads (cache-friendly).
        content = getattr(tool.info, "full_content", "") or ""  # type: ignore[attr-defined]
        header = (
            f"[Skill '{tool.name}' loaded from {skill_md_path}.]\n"
            f"Only follow the instructions in the content below. Use the name "
            f"`{tool.tool_id}` as the <skill-name> argument for the Exec Shell "
            f"tool. Do not use the System File tool to read the <skill_directory> "
            f"structure for security reasons."
        )
        sections = [header]
        tree_text = generate_dir_tree(dir_path)
        if tree_text:
            sections.append(f"Skill Directory Structure:\n```\n{tree_text}\n```")
        if content:
            sections.append(content)
        events_note = self._render_events_hint(tool)
        if events_note:
            sections.append(events_note)
        obs = "\n\n".join(sections)

        self._loaded_skill_ids.add(tool.tool_id)

        # Anchor the conversation working directory to the skill's own dir.
        if self.context_id:
            set_in_memory_override(self.context_id, str(dir_path))
            set_context(self.context_id, LOADED_SKILLS_KEY, sorted(self._loaded_skill_ids))
            try:
                from app.events.runner import get_conversation_storage
                from app.utils.working_directory import persist_working_directory
                await persist_working_directory(
                    self.context_id, str(dir_path), get_conversation_storage(),
                )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to persist skill cwd anchor for %s", self.context_id)
            try:
                from app.events import get_event_stream_bus
                await get_event_stream_bus().publish(
                    self.context_id, "cwd", {"working_directory": str(dir_path)},
                )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to publish skill cwd anchor for %s", self.context_id)

        self._append_tool_result(call_id, obs, fn_name=tool.tool_id, truncate=False)
        yield self._result_artifact(
            step_no, call_id, [Part(root=TextPart(text=f"[Skill loaded: {skill_md_path}]"))],
        )
