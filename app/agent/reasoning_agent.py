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
from app.config.user_config import resolve_agent_config
from app.constants import ChatCompletionTypeEnum
from app.lib.exception import AgentException
from app.lib.llm.base import LLMProvider
from app.tools import (
    ToolErrorEvent,
    ToolRegistry,
    ToolResultEvent,
    ToolStatusEvent,
    ToolThinkingEvent,
    ToolType,
)
from app.skills.scanner import generate_dir_tree
from app.types import ReasoningStreamResponseType
from app.utils.common import truncate_to_tokens
from app.utils.context_storage import clear_context, get_context, set_context
from app.utils.logger import logger
from app.utils.persona import read_persona_file
from app.utils.working_directory import set_in_memory_override


# Mirror of ``self._loaded_skill_ids`` written into ContextStorage so that
# per-request callbacks (e.g. ``change_working_directory``'s prepare_tools)
# can read the current loaded-skill set without holding an agent reference.
# Must match the constant of the same name in
# ``app.tools.builtin.change_working_directory``.
LOADED_SKILLS_KEY = "_loaded_skill_ids"

# Parameter name a skill function exposes so the model can pass the user's
# intent. On first use it is overwritten to a fixed marker and the SKILL.md
# content is returned as the tool result (see ``_handle_skill_call``).
SKILL_REQUEST_ARG = "request"

# What we overwrite a skill load call's ``request`` arg with so the persisted /
# replayed trace is deterministic and byte-stable (better prompt-cache reuse).
SKILL_LOAD_REQUEST = "You need to load the SKILL.md file for skill {name}"


SYSTEM_TEMPLATE = '''{persona_description}

Current OS: {current_os}
Current User Working Directory: `{current_user_working_directory}`

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
{reasoning_guidance}
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

    def __init__(
        self,
        llm: LLMProvider,
        registry: ToolRegistry,
        profile: str,
        context_id: Optional[str] = None,
        max_steps: Optional[int] = None,
        reasoning: bool = True,
        triggered_by_event: bool = False,
    ):
        self.llm = llm
        self.registry = registry
        self.profile = profile
        self.reasoning = reasoning

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
        # Event-triggered runs must not register new watchers/events (recursive
        # event storms). This is enforced at DISPATCH time — ``_handle_skill_call``
        # refuses a ``subscribe`` call and ``_loop`` refuses a ``register_file_watcher``
        # call when ``_triggered_by_event`` is set — NOT by mutating the tool schema.
        # The ``tools=`` block stays byte-identical to a normal run so the prompt
        # cache prefix is reused on event runs (a prior schema divergence here
        # dropped event cache hits to ~29%).
        self._triggered_by_event = triggered_by_event
        # The ``reasoning`` think-tool exists only to give models WITHOUT native
        # step-by-step reasoning a place to think before acting. Drop it (and
        # skip its system-prompt guidance) for models that reason natively.
        native_reasoning = model_supports_reasoning(
            getattr(self.llm, "provider_name", ""),
            getattr(self.llm, "model_name", ""),
        )
        self._inject_reasoning_guidance = not native_reasoning
        if native_reasoning:
            tools = [t for t in tools if t.tool_id != "reasoning"]
        # Whether the model may emit several tool calls in one turn. Default-on for
        # every provider; the agent already runs leaf calls concurrently (see the
        # asyncio.gather in _loop). A per-model TOML flag can opt a model out.
        self._parallel_tool_calls = model_parallel_tool_calls(
            getattr(self.llm, "provider_name", ""),
            getattr(self.llm, "model_name", ""),
        )
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
        )
        if not vision_feature_enabled(profile) and not main_can_see:
            tools = [t for t in tools if t.tool_id != "image_understanding"]
        self._tools = tools
        self._tools_by_id = {t.tool_id: t for t in self._tools}

        self.max_steps = max_steps if max_steps is not None else cfg.max_steps
        self.current_step_count = 0

        # Skill tool_ids whose SKILL.md is present in this conversation's context.
        # Seeded at run() start from the replayed history (a skill stays "loaded"
        # only while its load tool call is still in the tail; it drops out once
        # compaction folds that call into the summary), then extended as new skills
        # load this turn. Used to skip re-loading an already-loaded skill.
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

    def _load_llm_params(self, tool_id: str) -> dict:
        try:
            return self.registry.config.get_llm_params(tool_id, self.profile)
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

    def _build_instruction(self) -> str:
        persona = read_persona_file(self.profile)
        override = (
            get_context(self.context_id, "_working_directory_override")
            if self.context_id else None
        )
        cwd = override or get_user_working_directory()
        return SYSTEM_TEMPLATE.format(
            persona_description=persona,
            current_os=platform.system(),
            current_user_working_directory=cwd,
            reasoning_guidance=REASONING_GUIDANCE if self._inject_reasoning_guidance else "",
        )

    def _render_input(self) -> str:
        """The volatile per-turn user message (just the query).

        Long-term memory is no longer injected here — the model retrieves it on
        demand via the ``search_memory`` tool — so the [system + tools + history]
        prefix stays byte-stable for prompt caching.
        """
        return self._current_query

    # ── tool spec assembly ─────────────────────────────────────────────

    def _skill_function_spec(self, tool) -> dict:
        """Build the native function spec for one skill tool.

        The spec is derived purely from static skill metadata (description +
        declared events), so it is byte-stable across steps and turns — loaded
        skills are NOT removed and no per-request mutation happens, which keeps
        the cached ``tools=`` prefix intact. Event-bearing skills additionally
        expose a ``subscribe`` object carrying that skill's own event enum, so a
        subscription is unambiguously tied to the skill whose tool was called
        (no active-skill state). The ``subscribe`` block is present on every run
        — including event-triggered ones — so the ``tools=`` prefix is identical
        regardless of how the turn started (a previous schema divergence here cost
        the prompt cache on every event run). Recursive event storms are prevented
        at dispatch time instead: ``_handle_skill_call`` refuses a ``subscribe``
        call when ``_triggered_by_event`` is set.
        """
        properties: Dict[str, Any] = {
            SKILL_REQUEST_ARG: {
                "type": "string",
                "description": (
                    "What you want this skill to do right now (one-shot use). "
                    "Provide this to load and use the skill."
                ),
            },
        }
        items = self._skill_event_items(tool)
        if items:
            names = [i["name"] for i in items]
            desc_lines = [
                f"- {i['name']}: {i.get('description', '')}".rstrip(": ").rstrip()
                for i in items
            ]
            properties["subscribe"] = {
                "type": "object",
                "description": (
                    "Provide INSTEAD of `request` to subscribe this conversation "
                    "to one or more of this skill's events so an action runs "
                    "automatically whenever an event fires."
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
                            "Short imperative of WHAT to do when an event fires "
                            "(no trigger phrasing)."
                        ),
                    },
                },
                "required": ["trigger", "action"],
                "additionalProperties": False,
            }
        return {
            "type": "function",
            "function": {
                "name": tool.tool_id,
                "description": (
                    f"{tool.description} Call this to use the skill; pass the "
                    "user's request. The skill's instructions load on first use."
                ),
                "parameters": {"type": "object", "properties": properties},
            },
        }

    def _build_tools_and_dispatch(self) -> Tuple[List[dict], Dict[str, tuple]]:
        """Flatten enabled tools' leaves into native function specs + a dispatch map.

        Returns ``(specs, dispatch)`` where ``dispatch[name]`` is
        ``("leaf", tool, leaf_name)`` for built-in/MCP sub-tools or
        ``("skill", tool, None)`` for skill functions. Skill specs are static
        (all skills always present, derived from metadata) so the tools block is
        byte-stable for prompt caching.
        """
        specs: List[dict] = []
        dispatch: Dict[str, tuple] = {}
        # Per-profile disabled sub-tools ("leaves"), resolved in one read.
        disabled_by_tool = self.registry.disabled_leaves_by_tool(self.profile)

        for tool in self._tools:
            if tool.tool_type is ToolType.SKILL:
                specs.append(self._skill_function_spec(tool))
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
                specs.append(fs.schema)
                dispatch[fs.name] = ("leaf", tool, fs.leaf_name)

        return specs, dispatch

    # Leaf sub-tools whose schema stays exposed on every run (byte-stable tools
    # prefix) but whose EXECUTION is blocked while reacting to an event, to stop
    # recursive event storms. Matched on the bare leaf name + owning tool_id (the
    # model emits the namespaced ``system_file__register_file_watcher``, but the
    # dispatch entry carries the bare ``register_file_watcher`` in ``entry[2]``).
    _EVENT_BLOCKED_LEAVES: frozenset = frozenset({("system_file", "register_file_watcher")})

    def _is_event_blocked_leaf(self, entry) -> bool:
        return (
            self._triggered_by_event
            and bool(entry)
            and entry[0] == "leaf"
            and (entry[1].tool_id, entry[2]) in self._EVENT_BLOCKED_LEAVES
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
        async for item in self._loop():
            yield item

    # ── reasoning loop ────────────────────────────────────────────────

    async def _loop(self) -> AsyncGenerator[ReasoningStreamResponseType, None]:
        llm_retry = 0
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
                    tool_choice="auto" if specs else None,
                    parallel_tool_calls=self._parallel_tool_calls,
                    temperature=self._reasoning_temperature,
                    max_tokens=self._reasoning_max_tokens,
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
                if llm_retry < self._max_llm_retries:
                    llm_retry += 1
                    self.current_step_count -= 1
                    continue
                yield self._final_chunk(
                    "I encountered an error processing your request. Please try again."
                )
                return

            assistant_text = "".join(assistant_parts)

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

            # Storm-blocked leaves keep their schema but are NOT executed on an
            # event run; they still get a paired role:"tool" result below so the
            # turn's tool_calls group is fully answered (an unanswered tool_use
            # would 400 on replay / be truncated by _normalize_turn_messages).
            leaf_calls = [
                (c, n, a, e) for (c, n, a, e) in resolved
                if e and e[0] == "leaf" and not self._is_event_blocked_leaf(e)
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
                        "Registering a file watcher is not allowed while reacting "
                        "to an event (this prevents recursive event loops). "
                        "Ignoring this request."
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
        llm_params = self._load_llm_params(tool.tool_id)

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
                llm_params=llm_params,
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
            "`action` = a short imperative of what to do when an event fires."
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
            # prefix), so the model can emit it even while reacting to an event.
            # Refuse it at runtime there: subscribing during an event run risks a
            # recursive event storm (event → reasoning → subscribe → event → …).
            if self._triggered_by_event:
                obs = (
                    "Subscriptions cannot be created while reacting to an event "
                    "(this prevents recursive event loops). Ignoring this "
                    "subscribe request."
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
                "instructions are available above. Follow them directly — no need "
                "to load it again."
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
