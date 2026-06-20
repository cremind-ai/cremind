"""Reasoning agent (ReAct loop) -- registry-driven, polymorphic dispatch.

The agent treats every capability uniformly through :class:`Tool`. The Action
enum is built from ``tool_id``s (which are guaranteed unique). When the LLM
selects an Action, the registry returns the corresponding :class:`Tool` and
``tool.execute(...)`` produces a :class:`ToolEvent` stream that drives:

- ``CONTENT`` streamed to the user  (CONTINUE behavior)
- DONE / CLARIFY responses           (TERMINATE / CLARIFY behaviors)
- Step-history observation appended  (OBSERVE behavior, default)
- Authentication redirects           (ToolErrorEvent with auth_required)

There is **no** per-type branching here -- the polymorphism is in the Tool
implementations. The reasoning agent only knows about Tool / ToolEvent.
"""

from __future__ import annotations

import json
import os
import platform
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, AsyncGenerator, Dict, List, Optional, Tuple, cast

import jsonschema

# OpenAI SDK lives in the ``llm-openai`` extras group. Its types are
# referenced only in PEP 563-stringified annotations here, so importing
# under TYPE_CHECKING keeps the agent loadable on a thin-core install.
if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam
    from openai.types.chat.chat_completion_tool_param import ChatCompletionToolParam

from a2a.types import Part, TextPart

from app.agent.context_store import ReasoningContextStore
from app.agent.skill_classifier import classify_skill_request
from app.config.settings import BaseConfig, get_user_working_directory
from app.config.user_config import resolve_agent_config
from app.constants import ChatCompletionTypeEnum
from app.lib.exception import AgentException
from app.lib.llm.base import LLMProvider
from app.tools import (
    ToolBehavior,
    ToolErrorEvent,
    ToolRegistry,
    ToolResultEvent,
    ToolStatusEvent,
    ToolThinkingEvent,
    ToolType,
)
from app.tools.ids import slugify
from app.skills.scanner import generate_dir_tree
from app.types import ReasoningStreamResponseType
from app.utils.common import limit_messages, truncate_messages, truncate_old_observations
from app.utils.context_storage import clear_context, get_context, set_context
from app.utils.logger import logger
from app.utils.persona import read_persona_file
from app.utils.working_directory import set_in_memory_override


OBSERVATION_START_MARKER = "------------OBS-START------------"
OBSERVATION_END_MARKER = "------------OBS-END------------"

# Mirror of ``self._loaded_skill_ids`` written into ContextStorage so that
# per-request callbacks (e.g. ``change_working_directory``'s prepare_tools)
# can read the current loaded-skill set without holding an agent reference.
# Must match the constant of the same name in
# ``app.tools.builtin.change_working_directory``.
LOADED_SKILLS_KEY = "_loaded_skill_ids"

template_instruction = '''(***[VERY IMPORTANT] You MUST always call the "personal_assistant_react" function tool to return your reasoning step. DO NOT return any plain text. Always use the function call with the fields: Thought, Action, Action_Input, Thought_In_Next.***)
{persona_description}

Current time: {current_time}
Current OS: {current_os}
Current User Working Directory: `{current_user_working_directory}`

You will have these tools to support you. Your task is to respond to user commands by determining the next step you need to take by querying the relevant tool(s) until the goal is achieved, at which point you will return the Final Answer.
Please carefully consider the user's needs and construct a thoughtful plan to address them in the most intelligent way possible. You have access to the following tools below; only use the necessary tools in the reasoning steps.
You must be cautious and think carefully about which tool to use and whether to use it at all.
Reasoning steps should avoid repeating previous reasoning to prevent redundant information and conserve resources. If you have sufficient information, proceed to the next step. Please detect if the Thought content across the steps is repeating itself. If it is, immediately modify the Thought content to avoid entering a loop.
When a user request targets a skill, just call the skill's tool with the user's request as **Action_Input** — the system decides whether to load the skill for immediate execution or register a recurring event subscription.
For all user commands that contain a time structure within them, you must call the `datetime_parser` tool, and if they contain a time structure combined with a schedule, you must call the `schedule_parser` tool before calling any other tools, in order to analyze and normalize the time structure or the time + schedule structure for other tools to use. Please choose the tool type very carefully and do not confuse them — if the command belongs to a schedule structure, you must choose the `schedule_parser` tool and must not use `datetime_parser`.
Tools:
{tools}
Use the following format:

Input: the input you must answer
Thought: you should always think about what to do
Action: the action to take from Thought, should be one of {tool_names} (Note that Action should only include the tool name and must not contain the SubTool name)
Action_Input: the input sent to the action. For intrinsic tools, this is the response content to be sent to the user. Please respond in an easy-to-understand way, only text for the human listening (System information, technical specifications, and the device ID are hidden information). When the chosen Action lists Sub-Tools, begin Action_Input with the exact sub-tool name followed by its arguments as key="value" pairs, and ALWAYS include every required argument such as the file `path` (e.g. `overwrite_file path="notes.txt" diff="@@ -2,1 +2,1 @@ ..."`). Never send a bare diff or raw content without the sub-tool name and path.
Thought_In_Next: you should always think about what to do next after observing the results of the action
Observation: the result of the action, the observation content will be placed between '{OBSERVATION_START_MARKER}' and '{OBSERVATION_END_MARKER}' to help you clearly identify the observation content.
{loaded_skills}'''

template_input = '''
Begin!
{steps}
Thought:
'''


class StepData:
    def __init__(
        self,
        input: Optional[str] = None,
        thought: Optional[str] = None,
        action: Optional[str] = None,
        action_input: Optional[str] = None,
        thought_in_next: Optional[str] = None,
        observation: Optional[list] = None,
        observation_text: Optional[str] = None,
    ):
        self.input = input
        self.thought = thought
        self.action = action
        self.action_input = action_input
        self.thought_in_next = thought_in_next
        self.observation = observation
        self.observation_text = observation_text


def _format_arguments_section(arguments: dict) -> str:
    if not arguments:
        return ""
    lines = "*   Default Arguments:\n"
    for key, value in arguments.items():
        lines += f"    *   {key}: {value}\n"
    return lines


def _format_events_section(metadata: Optional[dict]) -> str:
    if not isinstance(metadata, dict):
        return ""
    events = metadata.get("events") or {}
    if not isinstance(events, dict):
        return ""
    items = events.get("event_type") or []
    if not isinstance(items, list):
        return ""
    bullets: List[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name:
            continue
        desc = item.get("description") or ""
        bullets.append(f"    *   {name}: {desc}" if desc else f"    *   {name}")
    if not bullets:
        return ""
    return "*   Events:\n" + "\n".join(bullets) + "\n"


def _format_tool_for_prompt(tool, arguments: dict) -> str:
    """Render one tool block (id, description, default args, sub-tools, events)."""
    text = f"[{tool.tool_id}]: {tool.description}\n"
    text += _format_arguments_section(arguments)
    tools = tool.skills
    if tools:
        text += "*   Sub-Tools:\n"
        for skill in tools:
            # Render a call signature (name + argument names) so the model
            # knows it must write `name arg="value" ...`, not bare content.
            sig = skill.name
            schema = skill.parameters if isinstance(skill.parameters, dict) else None
            if schema:
                shown = schema.get("required") or list((schema.get("properties") or {}).keys())
                if shown:
                    sig = f"{skill.name}({', '.join(shown)})"
            examples_str = ", ".join([f"'{ex}'" for ex in skill.examples]) if skill.examples else ""
            text += f"    *   {sig}: {skill.description}"
            if examples_str:
                text += f", examples: {examples_str}"
            text += "\n"
    info = getattr(tool, "info", None)
    metadata = getattr(info, "metadata", None) if info is not None else None
    text += _format_events_section(metadata)
    return text


class ReasoningAgent:
    """ReAct loop driven by the unified :class:`Tool` registry."""

    def __init__(
        self,
        llm: LLMProvider,
        registry: ToolRegistry,
        profile: str,
        context_id: Optional[str] = None,
        max_steps: Optional[int] = None,
        steps_length: Optional[int] = None,
        reasoning: bool = True,
        allowed_skill_ids: Optional[set[str]] = None,
        triggered_by_event: bool = False,
        memory_context: str = "",
    ):
        self.llm = llm
        self.registry = registry
        self.profile = profile
        self.reasoning = reasoning
        # Pre-rendered MEMORY block (short/long-term) fetched by the caller and
        # appended to the system instruction so it is never truncated by the
        # history window. Empty when the memory feature is off / has no entries.
        self._memory_context = memory_context or ""
        self.current_skills_directory = os.path.join(BaseConfig.CREMIND_SYSTEM_DIR, self.profile, "skills")

        cfg = resolve_agent_config(profile)
        self._runtime_cfg = cfg
        self._max_llm_retries = cfg.max_llm_retries
        self._reasoning_temperature = cfg.reasoning_temperature
        self._reasoning_max_tokens = cfg.reasoning_max_tokens
        self._reasoning_retry = cfg.reasoning_retry
        self._history_max_tokens_total = cfg.history_max_tokens_total
        self._history_max_tokens_per_message = cfg.history_max_tokens_per_message
        self._tool_result_enabled = cfg.tool_result_enabled
        self._tool_result_max_tokens = cfg.tool_result_max_tokens
        self._tool_result_preserve_recent = cfg.tool_result_preserve_recent
        self._tool_result_head_tokens = cfg.tool_result_head_tokens
        self._tool_result_tail_tokens = cfg.tool_result_tail_tokens

        # Snapshot the tool list available to this profile for this run.
        # When ``allowed_skill_ids`` is provided (automatic skill mode), skills
        # outside that set are hidden from both the Tools block and the Action
        # enum, so the LLM can only pick from the vector-retrieved top matches.
        tools = registry.tools_for_profile(profile)
        if allowed_skill_ids is not None:
            tools = [
                t for t in tools
                if t.tool_type is not ToolType.SKILL or t.tool_id in allowed_skill_ids
            ]
        # Event-triggered runs must not register new watchers/events — that
        # path leads to recursive event storms (event → reasoning → register →
        # event → …). ``register_skill_event`` is a standalone tool, so drop it
        # outright. ``register_file_watcher`` is now a *subtool* of the
        # ``system_file`` group (which we must keep for its file ops), so it is
        # suppressed deeper down: ``_dispatch`` injects ``_triggered_by_event``
        # into the System File call and ``system_file.get_prepare_tools`` hides
        # the register subtool from the child LLM.
        self._triggered_by_event = triggered_by_event
        if triggered_by_event:
            tools = [t for t in tools if t.tool_id != "register_skill_event"]
        self._tools = tools
        self._tools_by_id = {t.tool_id: t for t in self._tools}
        self._action_names = list(self._tools_by_id.keys())

        self.context_store = ReasoningContextStore()
        self.steps: List[str] = []
        self.max_steps = max_steps if max_steps is not None else cfg.max_steps
        self.steps_length = steps_length if steps_length is not None else cfg.steps_length
        self.current_step_count = 0
        self.instruction = ""
        self._agent_call_history: List[Tuple[str, str]] = []
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        # Skills whose SKILL.md content has been folded into self.instruction.
        # Such skills are removed from the rendered Tools block and from the
        # Action enum so the LLM cannot re-invoke them. Cleared when the loop
        # ends via Casual Chat (CLARIFY) or Final Answer (TERMINATE).
        self._loaded_skill_ids: set[str] = set()
        self._loaded_skill_sections: Dict[str, str] = {}

        self.reasoning_tool: ChatCompletionToolParam = {
            "type": "function",
            "function": {
                "name": "personal_assistant_react",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "Thought": {"type": "string"},
                        "Action": {"type": "string", "enum": [""]},
                        "Action_Input": {"type": "string"},
                        "Thought_In_Next": {"type": "string"},
                    },
                    "required": ["Thought", "Action", "Action_Input", "Thought_In_Next"],
                    "additionalProperties": False,
                },
            },
        }

        if context_id is None:
            context_id = str(uuid.uuid4())
            logger.debug(f"Generated new context_id: {context_id}")
        self.context_id = context_id

    # ── prompt building ───────────────────────────────────────────────

    def _build_tools_block(self) -> str:
        # ``register_skill_event`` is only meaningful in iterations where at
        # least one skill with ``metadata.events`` has already been folded
        # into "Start Skills Instructions". Hide it from the Tools section
        # (and the action enum, see _active_action_names) by default.
        skill_events_available = self._has_loaded_skill_with_events()
        sections = []
        for tool in self._tools:
            if tool.tool_id in self._loaded_skill_ids:
                continue
            if tool.tool_id == "register_skill_event" and not skill_events_available:
                continue
            args = self._load_arguments(tool.tool_id) if tool.tool_type in (
                ToolType.A2A, ToolType.MCP, ToolType.BUILTIN,
            ) else {}
            sections.append(_format_tool_for_prompt(tool, args))
        return "".join(sections)

    def _build_loaded_skills_block(self) -> str:
        if not self._loaded_skill_sections:
            return ""
        parts = [
            "\n==========Start Skills Instructions==========\n"
            "You should only follow the instructions provided in the content loaded below.\n"
            "Do not use the **System File** tool to read the directory structure of `<skill_directory>` for security reasons."
        ]
        # If any loaded skill declares environment variables, remind the agent
        # to source the per-skill .env file before running any script -- exec_shell
        # is generic and won't preload them automatically.
        # if any(
        #     getattr(self._tools_by_id.get(tid), "environment_variables", None)
        #     for tid in self._loaded_skill_sections
        # ):
        #     parts.append(
        #         "Before running any script/app/binary in the scripts directory, "
        #         "you must export variables from the .env file so that the script/app/binary "
        #         "has the necessary environment variables to run.\n\n"
        #         "For Windows:\n"
        #         "Get-Content .env | ForEach-Object {\n"
        #         "    if ($_ -notmatch '^#|^\\s*$') {\n"
        #         "        $name, $value = $_ -split '=', 2\n"
        #         "        Set-Content \"env:\\$($name.Trim())\" $value.Trim()\n"
        #         "    }\n"
        #         "}\n\n"
        #         "For Linux and macOS:\n"
        #         "export $(grep -v '^#' .env | xargs)\n"
        #     )
        for tool_id, content in self._loaded_skill_sections.items():
            tool = self._tools_by_id.get(tool_id)
            display_name = tool.name if tool else tool_id
            parts.append(f"\n----- Skill: {tool_id} -----\n\nPlease use this name `{tool_id}` to provide <skill-name> for **Exec Shell** tool\n{content}\n-------------------------\n")
        parts.append("==========End Skills Instructions==========\n")
        return "".join(parts)

    def _active_action_names(self) -> List[str]:
        names = [name for name in self._action_names if name not in self._loaded_skill_ids]
        # ``register_skill_event`` is only useful once a skill that declares
        # ``metadata.events`` has been folded into the system prompt. Hide
        # it from the action enum otherwise so the LLM doesn't try to call
        # it without an event-bearing skill loaded.
        if "register_skill_event" in names and not self._has_loaded_skill_with_events():
            names = [n for n in names if n != "register_skill_event"]
        return names

    def _has_loaded_skill_with_events(self) -> bool:
        for tool_id in self._loaded_skill_ids:
            tool = self._tools_by_id.get(tool_id)
            info = getattr(tool, "info", None) if tool is not None else None
            metadata = getattr(info, "metadata", None) if info is not None else None
            if not isinstance(metadata, dict):
                continue
            events = metadata.get("events") or {}
            if isinstance(events, dict) and events.get("event_type"):
                return True
        return False

    def _render_events_hint(self, tool: object) -> str:
        """Build the trailing hint that nudges the LLM toward register_skill_event.

        Returns an empty string when the loaded skill declares no events.
        """
        info = getattr(tool, "info", None)
        metadata = getattr(info, "metadata", None) if info is not None else None
        if not isinstance(metadata, dict):
            return ""
        events = metadata.get("events") or {}
        if not isinstance(events, dict):
            return ""
        items = events.get("event_type") or []
        if not isinstance(items, list) or not items:
            return ""
        # Use the canonical tool_id (e.g. ``admin__imap_email``) — that's the
        # same identifier the LLM already sees as this skill's Action label,
        # and the only key that resolves the ``tools`` table row directly.
        skill_name = getattr(tool, "tool_id", None) \
            or getattr(info, "name", None) \
            or getattr(tool, "name", "")
        bullets: List[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue
            desc = item.get("description") or ""
            bullets.append(f"- {name}: {desc}" if desc else f"- {name}")
        if not bullets:
            return ""
        names_csv = ", ".join(
            i["name"] for i in items if isinstance(i, dict) and i.get("name")
        )
        return (
            "## Available events for this skill\n"
            + "\n".join(bullets)
            + "\n\n"
            "If the user wants something to happen automatically whenever one of "
            "these events fires, call the `register_skill_event` tool with:\n"
            f"- skill_name: \"{skill_name}\" (use this exact identifier — it "
            "is the same value you would pass as Action when invoking the "
            "skill).\n"
            f"- trigger: an array of one or more from [{names_csv}] — list "
            "every event the user wants to subscribe to (deduplicated). "
            "Each entry fully captures one WHEN.\n"
            "- action: only WHAT to do, written as a short imperative (e.g. "
            "\"Summarize the email content\"). DO NOT include the trigger "
            "condition — phrasings like \"when a new email arrives\", \"on "
            "new email\", \"whenever ...\" are forbidden, because the trigger "
            "field already conveys that. The event file's content is "
            "appended to the action automatically when the event fires."
        )

    def _build_instruction(self) -> str:
        persona = read_persona_file(self.profile)
        # Re-resolve every step so a per-conversation override set by the
        # ``change_working_directory`` tool is reflected in the next prompt.
        override = (
            get_context(self.context_id, "_working_directory_override")
            if self.context_id else None
        )
        current_user_working_directory = override or get_user_working_directory()
        instruction = template_instruction.format(
            persona_description=persona,
            current_time=f"{datetime.now().isoformat()}",
            current_os=platform.system(),
            current_user_working_directory=current_user_working_directory,
            tools=self._build_tools_block(),
            loaded_skills=self._build_loaded_skills_block(),
            tool_names=", ".join(self._active_action_names()),
            OBSERVATION_START_MARKER=OBSERVATION_START_MARKER,
            OBSERVATION_END_MARKER=OBSERVATION_END_MARKER,
        )
        if self._memory_context:
            instruction = f"{instruction}\n\n{self._memory_context}"
        return instruction

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

    def _model_label_for(self, tool) -> str | None:
        if tool.tool_type in (ToolType.INTRINSIC, ToolType.SKILL):
            return self.llm.model_label
        if tool.tool_type == ToolType.A2A:
            return None
        if tool.tool_type in (ToolType.MCP, ToolType.BUILTIN):
            adapter = getattr(tool, "adapter", None)
            inner_llm = getattr(adapter, "_llm", None)
            return getattr(inner_llm, "model_label", None) if inner_llm else None
        return self.llm.model_label

    # ── main entry point ──────────────────────────────────────────────

    async def run(
        self,
        input: str,
        history_messages: List[ChatCompletionMessageParam],
    ) -> AsyncGenerator[ReasoningStreamResponseType, None]:
        self.history_messages = history_messages
        self._agent_call_history = []

        context = self.context_store.get_context(self.context_id)
        if context:
            logger.debug(f"Loading existing context for context_id: {self.context_id}")
            stored_steps = context.get("steps") or []
            self.steps = stored_steps.copy()
            self.current_step_count = context.get("step_count", 0)
            self.instruction = self._build_instruction()
            self._append_input_step(input)
            self._trim_steps()
            initial_step = StepData(input=input)
            async for item in self._loop(initial_step):
                yield item
            return

        logger.debug(f"Creating new context for context_id: {self.context_id}")
        self.steps = []
        self.current_step_count = 0
        self.instruction = self._build_instruction()
        logger.debug(f"instruction: {self.instruction}")
        self._append_input_step(input)
        async for item in self._loop(StepData(input=input)):
            yield item

    # ── reasoning loop ────────────────────────────────────────────────

    async def _loop(
        self, step: StepData, llm_retry: int = 0,
    ) -> AsyncGenerator[ReasoningStreamResponseType, None]:
        if self.current_step_count >= self.max_steps:
            logger.warning(f"Max reasoning steps ({self.max_steps}) reached.")
            final_answer = (
                f"I've reached the maximum number of reasoning steps ({self.max_steps}). "
                f"Based on my analysis so far, here's what I can conclude: "
                f"{step.thought if step.thought else 'Unable to complete the full reasoning process within the step limit.'}"
            )
            self.steps.append(f"Thought: Maximum steps reached\nFinal_Answer: {final_answer}\n")
            self._trim_steps()
            done_chunk: Dict[str, Any] = {
                "type": ChatCompletionTypeEnum.DONE,
                "data": final_answer,
                "input_tokens": self._total_input_tokens,
                "output_tokens": self._total_output_tokens,
            }
            if self.current_step_count > 1:
                done_chunk["input_section"] = template_input.format(steps="\n".join(self.steps))
            yield done_chunk
            return

        self.current_step_count += 1
        steps_for_prompt = self.steps
        if self._tool_result_enabled:
            steps_for_prompt = truncate_old_observations(
                self.steps,
                max_tokens=self._tool_result_max_tokens,
                preserve_recent=self._tool_result_preserve_recent,
                head_tokens=self._tool_result_head_tokens,
                tail_tokens=self._tool_result_tail_tokens,
            )
        input_section = template_input.format(steps="\n".join(steps_for_prompt))

        # Rebuild the system prompt every iteration so newly loaded skills are
        # appended (and re-invoked skills are filtered out of the Tools block).
        self.instruction = self._build_instruction()

        # logger.debug(f"=== Instruction ===\n{self.instruction}")
        logger.debug(f"=== Reasoning Step ===\n{input_section}")

        # Action enum is the set of tool_ids, minus skills already folded into
        # the system prompt — this prevents the LLM from re-loading them.
        func_def = cast(dict, self.reasoning_tool["function"])
        func_def["parameters"]["properties"]["Action"]["enum"] = [""] + self._active_action_names()

        truncated = truncate_messages(self.history_messages, max_tokens_per_message=self._history_max_tokens_per_message)
        limited = limit_messages(truncated, max_length=self._history_max_tokens_total)
        messages: List[ChatCompletionMessageParam] = [
            {"role": "system", "content": self.instruction},
            *limited,
            {"role": "user", "content": input_section},
        ]

        llm_responses: list = []
        try:
            async for response in self.llm.chat_completion(
                messages=messages,
                tools=[self.reasoning_tool],
                tool_choice="auto",
                temperature=self._reasoning_temperature,
                max_tokens=self._reasoning_max_tokens,
                retry=self._reasoning_retry,
            ):
                llm_responses.append(response)
        except AgentException as err:
            logger.error(f"LLM call failed at step {self.current_step_count}: {err}")
            if llm_retry < self._max_llm_retries:
                self.current_step_count -= 1
                async for r in self._loop(step, llm_retry + 1):
                    yield r
                return
            yield {
                "type": ChatCompletionTypeEnum.CLARIFY,
                "data": step.thought or "I encountered an error processing your request. Please try again.",
                "input_tokens": self._total_input_tokens,
                "output_tokens": self._total_output_tokens,
            }
            return

        # Token usage extraction
        finish_reason = None
        for response in llm_responses:
            if response["type"] == ChatCompletionTypeEnum.DONE:
                self._total_input_tokens += response.get("input_tokens") or 0
                self._total_output_tokens += response.get("output_tokens") or 0
                finish_reason = response.get("finish_reason")
                break

        has_result = False

        logger.debug(f"LLM responses: {llm_responses}")
        for response in llm_responses:
            if response["type"] == ChatCompletionTypeEnum.CONTENT:
                content = response.get("data")
                if content:
                    has_result = True
                    fallback_id = self._first_tool_id_of(ToolType.INTRINSIC, name="Casual Chat Tool")
                    step = StepData(thought=content, action=fallback_id, action_input=content)
                    step_text = (
                        f"Thought: {step.thought}\n"
                        f"Action: {step.action}\nAction_Input: {content}\n"
                    )
                    self.steps.append(step_text)
                    self._trim_steps()
                    self.current_step_count = 0
                    if self.context_id:
                        self._save_context(self.context_id)
                    yield {
                        "type": ChatCompletionTypeEnum.CLARIFY,
                        "data": content,
                        "input_tokens": self._total_input_tokens,
                        "output_tokens": self._total_output_tokens,
                    }
                    return

            elif response["type"] == ChatCompletionTypeEnum.FUNCTION_CALLING:
                if not (response.get("data")
                        and "function" in response["data"]
                        and response["data"]["function"]):
                    continue
                response_data = response["data"]["function"][0]["arguments"]
                if not response_data:
                    continue

                step = StepData(
                    thought=response_data.get("Thought", ""),
                    action=response_data.get("Action") or None,
                    action_input=response_data.get("Action_Input") or None,
                    thought_in_next=response_data.get("Thought_In_Next") or None,
                )

                tool = self._tools_by_id.get(step.action) if step.action else None
                if tool is None:
                    has_result = True
                    chat_response = step.action_input or step.thought or "I encountered an issue processing your request."
                    fallback_id = self._first_tool_id_of(ToolType.INTRINSIC, name="Casual Chat Tool")
                    step_text = (
                        f"Thought: {step.thought}\n"
                        f"Action: {fallback_id}\nAction_Input: {chat_response}\n"
                    )
                    self.steps.append(step_text)
                    self._trim_steps()
                    self.current_step_count = 0
                    if self.context_id:
                        self._save_context(self.context_id)
                    yield {
                        "type": ChatCompletionTypeEnum.CLARIFY,
                        "data": chat_response,
                        "input_tokens": self._total_input_tokens,
                        "output_tokens": self._total_output_tokens,
                    }
                    return

                has_result = True
                async for item in self._dispatch(tool, step):
                    yield item
                return

        if finish_reason == "length":
            fallback = (
                "I encountered an issue completing your request. The response "
                "was too long for the current configuration."
            )
        else:
            if not has_result and llm_retry < self._max_llm_retries:
                self.current_step_count -= 1
                async for r in self._loop(step, llm_retry + 1):
                    yield r
                return
            fallback = step.thought or "I was unable to complete the reasoning process."

        yield {
            "type": ChatCompletionTypeEnum.CLARIFY,
            "data": fallback,
            "input_tokens": self._total_input_tokens,
            "output_tokens": self._total_output_tokens,
        }

    # ── tool-stream helper ────────────────────────────────────────────

    def _skill_has_events(self, tool) -> bool:
        """True if ``tool`` is a SKILL whose SKILL.md declares metadata.events."""
        info = getattr(tool, "info", None)
        metadata = getattr(info, "metadata", None) if info is not None else None
        if not isinstance(metadata, dict):
            return False
        events = metadata.get("events") or {}
        if not isinstance(events, dict):
            return False
        items = events.get("event_type") or []
        return isinstance(items, list) and any(
            isinstance(i, dict) and i.get("name") for i in items
        )

    def _pin_skill_for_register_event(
        self, action_input: str,
    ) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
        """Resolve which loaded event-bearing skill ``register_skill_event`` is
        being asked to subscribe.

        Mirrors what ``_handle_skill_event_route`` injects on the routed path:
        returns ``({"_skill_id", "_skill_source"}, None)`` on success, or
        ``(None, error_message)`` if no candidate fits — the caller surfaces
        the message as an observation so the loop can recover.
        """
        requested: Optional[str] = None
        try:
            parsed = json.loads(action_input) if action_input else None
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            sn = parsed.get("skill_name")
            if isinstance(sn, str) and sn.strip():
                requested = sn.strip()

        candidates: List[Tuple[str, str]] = []
        for tid in self._loaded_skill_ids:
            t = self._tools_by_id.get(tid)
            if t is None or not self._skill_has_events(t):
                continue
            dir_path = getattr(getattr(t, "info", None), "dir_path", None)
            if dir_path is None:
                continue
            candidates.append((tid, str(dir_path)))

        if not candidates:
            return None, (
                "register_skill_event was invoked but no event-bearing skill "
                "is loaded in this conversation. Load a skill that declares "
                "metadata.events first."
            )

        if requested:
            prefix = f"{self.profile}__"
            bare = requested[len(prefix):] if requested.startswith(prefix) else requested
            wanted = {requested, f"{self.profile}__{bare}", bare}
            for tid, src in candidates:
                if tid in wanted:
                    return {"_skill_id": tid, "_skill_source": src}, None
            return None, (
                f"register_skill_event named skill '{requested}', but it is "
                f"not currently loaded. Loaded event-bearing skills: "
                f"{', '.join(t for t, _ in candidates)}."
            )

        if len(candidates) == 1:
            tid, src = candidates[0]
            return {"_skill_id": tid, "_skill_source": src}, None

        return None, (
            "register_skill_event needs a skill_name because more than one "
            "event-bearing skill is loaded: "
            f"{', '.join(t for t, _ in candidates)}."
        )

    async def _run_builtin_tool_events(
        self,
        tool,
        *,
        query: str,
        arguments: Dict[str, Any],
        variables: Dict[str, str],
        llm_params: Dict[str, Any],
    ) -> AsyncGenerator[Tuple[str, Any], None]:
        """Drive ``tool.execute(...)`` and yield tagged events.

        Yields tuples ``(kind, payload)`` where ``kind`` is:
        - ``"status"``: a STATUS_UPDATE chunk for the caller to forward
        - ``"result"``: the final :class:`ToolResultEvent`
        - ``"error"``: the final :class:`ToolErrorEvent`
        """
        err: Optional[ToolErrorEvent] = None
        try:
            async for event in tool.execute(
                query=query,
                context_id=self.context_id,
                profile=self.profile,
                arguments=arguments,
                variables=variables,
                llm_params=llm_params,
            ):
                if isinstance(event, ToolThinkingEvent):
                    continue  # the dispatcher emits its own thinking artifact
                if isinstance(event, ToolStatusEvent):
                    yield (
                        "status",
                        {"type": ChatCompletionTypeEnum.STATUS_UPDATE, "data": event.raw},
                    )
                    continue
                if isinstance(event, ToolResultEvent):
                    yield ("result", event)
                    continue
                if isinstance(event, ToolErrorEvent):
                    err = event
                    break
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Tool '{tool.tool_id}' raised during execute()")
            err = ToolErrorEvent(message=str(e))
        if err is not None:
            yield ("error", err)

    # ── polymorphic dispatch ──────────────────────────────────────────

    async def _dispatch(
        self, tool, step: StepData,
    ) -> AsyncGenerator[ReasoningStreamResponseType, None]:
        """Run ``tool`` and translate :class:`ToolEvent`s into the yield protocol."""

        # Validate / load arguments (only for tools that support them)
        arguments: dict = {}
        if tool.tool_type in (ToolType.A2A, ToolType.MCP, ToolType.BUILTIN):
            client_arguments = self._load_arguments(tool.tool_id)
            if tool.arguments_schema:
                schema_props = set(tool.arguments_schema.get("properties", {}).keys())
                filtered = {k: v for k, v in client_arguments.items() if k in schema_props}
                try:
                    jsonschema.validate(instance=filtered, schema=tool.arguments_schema)
                    arguments = filtered
                except jsonschema.ValidationError as e:
                    error_field = ".".join(str(p) for p in e.absolute_path) if e.absolute_path else "root"
                    obs = (
                        f"Validation error for tool '{tool.tool_id}' arguments: "
                        f"field '{error_field}' - {e.message}. "
                        f"Please ask the user to provide valid arguments."
                    )
                    step.observation = [Part(root=TextPart(text=obs))]
                    step.observation_text = obs
                    self._record_step_with_observation(step, obs)
                    if self.context_id:
                        self._save_context(self.context_id)
                    if not self.reasoning:
                        yield {
                            "type": ChatCompletionTypeEnum.CLARIFY,
                            "data": obs,
                            "input_tokens": self._total_input_tokens,
                            "output_tokens": self._total_output_tokens,
                        }
                        return
                    async for r in self._loop(step):
                        yield r
                    return
            else:
                arguments = client_arguments

        # ``register_skill_event`` requires the dispatcher to pin a target
        # skill via ``_skill_id``/``_skill_source`` (the routed path through
        # ``_handle_skill_event_route`` injects them). When the LLM picks
        # ``register_skill_event`` directly off the action enum we have to
        # resolve those fields here, otherwise the tool's ``run()`` rejects
        # the call.
        if tool.tool_id == "register_skill_event":
            pinned, err_msg = self._pin_skill_for_register_event(
                step.action_input or "",
            )
            if err_msg is not None:
                step.observation = [Part(root=TextPart(text=err_msg))]
                step.observation_text = err_msg
                self._record_step_with_observation(step, err_msg)
                if self.context_id:
                    self._save_context(self.context_id)
                if not self.reasoning:
                    yield {
                        "type": ChatCompletionTypeEnum.CLARIFY,
                        "data": err_msg,
                        "input_tokens": self._total_input_tokens,
                        "output_tokens": self._total_output_tokens,
                    }
                    return
                async for r in self._loop(step):
                    yield r
                return
            arguments = {**arguments, **(pinned or {})}

        # Event-triggered runs must not register new watchers. ``system_file``
        # owns the ``register_file_watcher`` subtool now, so signal the run kind
        # via a dispatch argument; ``system_file.get_prepare_tools`` reads it and
        # hides that one subtool from the group's child LLM (file ops stay).
        if tool.tool_id == "system_file" and self._triggered_by_event:
            arguments = {**arguments, "_triggered_by_event": True}

        variables = self._load_variables(tool.tool_id) if tool.tool_type in (
            ToolType.A2A, ToolType.MCP, ToolType.BUILTIN,
        ) else {}
        llm_params = self._load_llm_params(tool.tool_id) if tool.tool_type in (
            ToolType.MCP, ToolType.BUILTIN,
        ) else {}

        # Refresh the tool's child LLM from current config BEFORE reading
        # Model_Label, so the Thinking Process shows the up-to-date model.
        if tool.tool_type in (ToolType.BUILTIN, ToolType.MCP):
            tool.refresh_llm(self.profile)

        # Yield a thinking artifact so the UI shows the tool invocation.
        # For skills, override the model-generated Action_Input with a concise
        # "Load skill <display name>" label so the Thinking Process clearly
        # reflects what is actually happening (the SKILL.md is being folded
        # into the system prompt, not passed any user-supplied input).
        if tool.tool_type is ToolType.SKILL:
            action_input = f"Load skill `{tool.name}`"
        else:
            action_input = step.action_input or step.thought or ""
        logger.debug(f"Action input '{tool.tool_id}': {action_input}")
        thinking_payload = {
            "Thought": step.thought,
            "Action": tool.tool_id,
            "Action_Input": action_input,
            "Model_Label": self._model_label_for(tool),
            "Reasoning_Model_Label": self.llm.model_label,
        }
        if tool.tool_type is not ToolType.INTRINSIC:
            yield {"type": ChatCompletionTypeEnum.THINKING_ARTIFACT, "data": thinking_payload}
            
        # Drive the tool's event stream via the shared helper.
        if tool.tool_type is not ToolType.SKILL and tool.tool_type is not ToolType.INTRINSIC:
            tool_action_input = f"{step.action_input} (How would you think about this scenario in order to execute it: {step.thought})"
        else:
            tool_action_input = action_input
        logger.debug(f"Tool action input '{tool.tool_id}': {tool_action_input}")

        result_event: Optional[ToolResultEvent] = None
        error_event: Optional[ToolErrorEvent] = None
        async for kind, ev_payload in self._run_builtin_tool_events(
            tool,
            query=tool_action_input,
            arguments=arguments,
            variables=variables,
            llm_params=llm_params,
        ):
            if kind == "status":
                yield ev_payload
            elif kind == "result":
                result_event = ev_payload
            elif kind == "error":
                error_event = ev_payload

        if error_event is not None:
            if error_event.auth_required:
                step.action = tool.tool_id
                self._record_step_with_observation(
                    step, f"Authentication required for tool '{tool.tool_id}'.",
                )
                self.current_step_count = 0
                if self.context_id:
                    self._save_context(self.context_id)
                yield {
                    "type": ChatCompletionTypeEnum.CLARIFY,
                    "data": error_event.message,
                    "input_tokens": self._total_input_tokens,
                    "output_tokens": self._total_output_tokens,
                }
                return
            obs = error_event.message
            step.observation = [Part(root=TextPart(text=obs))]
            step.observation_text = obs
            self._record_step_with_observation(step, obs)
            if self.context_id:
                self._save_context(self.context_id)
            if not self.reasoning:
                yield {
                    "type": ChatCompletionTypeEnum.CLARIFY,
                    "data": obs,
                    "input_tokens": self._total_input_tokens,
                    "output_tokens": self._total_output_tokens,
                }
                return
            async for r in self._loop(step):
                yield r
            return

        if result_event is None:
            yield {
                "type": ChatCompletionTypeEnum.CLARIFY,
                "data": step.thought or "I was unable to complete the reasoning process.",
                "input_tokens": self._total_input_tokens,
                "output_tokens": self._total_output_tokens,
            }
            return

        # Accumulate token usage
        if result_event.token_usage:
            self._total_input_tokens += result_event.token_usage.get("input_tokens", 0) or 0
            self._total_output_tokens += result_event.token_usage.get("output_tokens", 0) or 0

        behavior = result_event.behavior
        observation_text = result_event.observation_text
        observation_parts = result_event.observation_parts

        if behavior is ToolBehavior.TERMINATE:
            logger.debug(f"=== {tool.tool_id} (TERMINATE) ===")
            self._record_action_step(step, observation_text)
            # Final Answer ends the run — drop any skills folded into the
            # system prompt so the next run starts with a fresh instruction.
            self._loaded_skill_ids.clear()
            self._loaded_skill_sections.clear()
            if self.context_id:
                self._clear_context(self.context_id)
                # Reset the dynamic enum mirror so change_working_directory's
                # ``target`` falls back to the default values on the next turn.
                clear_context(self.context_id, LOADED_SKILLS_KEY)
                # Evict any stale per-call shell-dir override left by pre-fix
                # conversations so it can never shadow the unified working dir.
                clear_context(self.context_id, "current_shell_directory")
            done_chunk: Dict[str, Any] = {
                "type": ChatCompletionTypeEnum.DONE,
                "data": observation_text,
                "input_tokens": self._total_input_tokens,
                "output_tokens": self._total_output_tokens,
            }
            # Only attach input_section when the turn was multi-step — its
            # presence is the signal to callers that the trace is worth
            # summarizing.
            if self.current_step_count > 1:
                done_chunk["input_section"] = template_input.format(steps="\n".join(self.steps))
            yield done_chunk
            return

        if behavior is ToolBehavior.CLARIFY:
            logger.info(f"=== {tool.tool_id} (CLARIFY) ===")
            self._record_action_step(step, observation_text)
            self.current_step_count = 0
            # Casual Chat ends the run — drop any skills folded into the
            # system prompt so the next run starts with a fresh instruction.
            self._loaded_skill_ids.clear()
            self._loaded_skill_sections.clear()
            if self.context_id:
                self._save_context(self.context_id)
                clear_context(self.context_id, LOADED_SKILLS_KEY)
                # Evict any stale per-call shell-dir override left by pre-fix
                # conversations so it can never shadow the unified working dir.
                clear_context(self.context_id, "current_shell_directory")
            yield {
                "type": ChatCompletionTypeEnum.CLARIFY,
                "data": observation_text,
                "input_tokens": self._total_input_tokens,
                "output_tokens": self._total_output_tokens,
            }
            return

        if behavior is ToolBehavior.CONTINUE:
            logger.info(f"=== {tool.tool_id} (CONTINUE) ===")
            self._record_action_step(step, observation_text)
            yield {
                "type": ChatCompletionTypeEnum.CONTENT,
                "data": observation_text + "\n",
            }
            if self.context_id:
                self._save_context(self.context_id)
            if not self.reasoning:
                yield {
                    "type": ChatCompletionTypeEnum.DONE,
                    "data": "",
                    "input_tokens": self._total_input_tokens,
                    "output_tokens": self._total_output_tokens,
                }
                return
            async for r in self._loop(step):
                yield r
            return

        # OBSERVE (default for a2a/mcp/builtin/skill)
        logger.info(f"=== {tool.tool_id} (OBSERVE) ===")
        if tool.tool_type is ToolType.SKILL:
            # A SKILL action carries one of two intents:
            #   1. "load"  — run the skill now → fold SKILL.md into the prompt
            #   2. "event" — register a recurring trigger → call register_skill_event
            # Classify before deciding what to do with the SKILL.md content
            # the tool just produced.
            dir_path = tool.info.dir_path  # type: ignore[attr-defined]
            skill_source = str(dir_path)
            raw_user_input = (step.action_input or step.thought or "").strip()
            has_events = self._skill_has_events(tool)

            if has_events and raw_user_input:
                try:
                    routing = await classify_skill_request(
                        action_input=raw_user_input,
                        skill_id=tool.tool_id,
                        skill_name=tool.name,
                        source=skill_source,
                        llm=self.llm,
                        profile=self.profile,
                        skill_content=getattr(tool.info, "full_content", "") or "",  # type: ignore[attr-defined]
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"skill_classifier raised: {exc}. Defaulting to 'load'."
                    )
                    routing = {
                        "skill_id": tool.tool_id,
                        "skill_name": tool.name,
                        "source": skill_source,
                        "response_type": "load",
                        "instruction": (
                            f"You should only follow the instructions provided "
                            f"in the content loaded in {tool.tool_id} skill."
                        ),
                    }
            else:
                routing = {
                    "skill_id": tool.tool_id,
                    "skill_name": tool.name,
                    "source": skill_source,
                    "response_type": "load",
                    "instruction": (
                        f"You should only follow the instructions provided in "
                        f"the content loaded in {tool.tool_id} skill."
                    ),
                }

            if routing.get("response_type") == "event":
                async for r in self._handle_skill_event_route(
                    skill_tool=tool,
                    skill_step=step,
                    routing=routing,
                    user_input=raw_user_input,
                ):
                    yield r
                return

            # ── "load" path (today's behavior) ──────────────────────────
            # Skill content is treated as instruction: append SKILL.md to the
            # system prompt (rebuilt at the top of the next _loop iteration)
            # and remove the skill from the Tools block / Action enum so it
            # cannot be re-invoked and appended again.
            skill_text = getattr(tool.info, "full_content", "") or observation_text or ""  # type: ignore[attr-defined]
            tree_text = generate_dir_tree(dir_path)
            if tree_text:
                skill_text = f"Skill Directory Structure:\n```\n{tree_text}\n```\n\n{skill_text}"
            events_hint = self._render_events_hint(tool)
            if events_hint:
                skill_text = f"{skill_text}\n\n{events_hint}"
            self._loaded_skill_sections[tool.tool_id] = skill_text
            self._loaded_skill_ids.add(tool.tool_id)
            # Anchor the ONE conversation working directory to the loaded
            # skill's own dir, using the canonical override key that
            # system_file, exec_shell, and the "Current User Working Directory"
            # prompt line all read. Persist + broadcast so it survives a reopen
            # and refreshes any subscribed UI, exactly like the
            # change_working_directory tool. Subsequent relative paths
            # ("scripts/__main__.py", "references/devices.md") then resolve for
            # both tools without the LLM having to navigate.
            if self.context_id:
                set_in_memory_override(self.context_id, str(dir_path))
                # Mirror loaded skills into ContextStorage so per-request
                # callbacks (change_working_directory.prepare_tools) can
                # extend the ``target`` enum with the active skill IDs.
                set_context(
                    self.context_id,
                    LOADED_SKILLS_KEY,
                    sorted(self._loaded_skill_ids),
                )
                try:
                    from app.events.runner import get_conversation_storage
                    from app.utils.working_directory import persist_working_directory
                    await persist_working_directory(
                        self.context_id, str(dir_path), get_conversation_storage(),
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Failed to persist skill cwd anchor for %s", self.context_id,
                    )
                try:
                    from app.events import get_event_stream_bus
                    await get_event_stream_bus().publish(
                        self.context_id, "cwd", {"working_directory": str(dir_path)},
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Failed to publish skill cwd anchor for %s", self.context_id,
                    )
            # The full SKILL.md content is shown to the user in the frontend's
            # Thinking Process via RESULT_ARTIFACT, but the recorded step in
            # self.steps only carries a short pointer — the actual content
            # already lives in self.instruction, so the step history doesn't
            # need to duplicate it.
            skill_md_path = tool.info.dir_path / "SKILL.md"  # type: ignore[attr-defined]
            short_obs_text = f"[Skill loaded from {skill_md_path}]"
            short_obs_parts = [Part(root=TextPart(text=short_obs_text))]
            # Keep the recorded Action_Input in sync with the THINKING_ARTIFACT
            # ("Load skill `<name>`") rather than the raw model-generated input.
            step.action_input = action_input
            step.observation = short_obs_parts
            step.observation_text = short_obs_text
            yield {
                "type": ChatCompletionTypeEnum.RESULT_ARTIFACT,
                "data": {"Observation": observation_parts},
            }
            self._record_step_full(step, short_obs_text)
            if self.context_id:
                self._save_context(self.context_id)
            if not self.reasoning:
                async for r in self._finalize_without_reasoning(step, short_obs_text):
                    yield r
                return
            async for r in self._loop(step):
                yield r
            return
        step.observation = observation_parts
        step.observation_text = observation_text
        yield {
            "type": ChatCompletionTypeEnum.RESULT_ARTIFACT,
            "data": {"Observation": observation_parts},
        }
        self._record_step_full(step, observation_text)
        if self.context_id:
            self._save_context(self.context_id)
        if not self.reasoning:
            async for r in self._finalize_without_reasoning(step, observation_text):
                yield r
            return
        async for r in self._loop(step):
            yield r

    # ── skill→event routing ──────────────────────────────────────────

    async def _handle_skill_event_route(
        self,
        *,
        skill_tool,
        skill_step: StepData,
        routing: Dict[str, Any],
        user_input: str,
    ) -> AsyncGenerator[ReasoningStreamResponseType, None]:
        """Drive the "event" branch: record the skill step as routed-to-event,
        then auto-invoke ``register_skill_event`` with the skill pinned via
        injected arguments. Both invocations are recorded as steps in the trace.
        """
        skill_id = skill_tool.tool_id
        skill_source = routing.get("source") or str(skill_tool.info.dir_path)  # type: ignore[attr-defined]
        instruction = (routing.get("instruction") or "").strip()

        # 1. Record the skill step with a routing observation. The skill itself
        #    is NOT folded into the system prompt, so ``_loaded_skill_*`` stays
        #    empty and the skill remains in the Tools/Action enum.
        routing_obs = (
            f"[Routed to event subscription: skill={skill_id}, "
            f"response_type=event] {instruction}"
        ).strip()
        skill_step.action_input = user_input
        skill_step.observation = [Part(root=TextPart(text=routing_obs))]
        skill_step.observation_text = routing_obs
        yield {
            "type": ChatCompletionTypeEnum.RESULT_ARTIFACT,
            "data": {"Observation": skill_step.observation},
        }
        self._record_step_full(skill_step, routing_obs)
        if self.context_id:
            self._save_context(self.context_id)

        # 2. Look up register_skill_event. If unavailable, surface that as the
        #    second step's observation and continue the loop so the LLM can
        #    explain to the user.
        rse_tool = self._tools_by_id.get("register_skill_event")
        rse_step = StepData(
            thought=skill_step.thought_in_next or "Routing: event subscription",
            action="register_skill_event",
            action_input=user_input,
            thought_in_next=skill_step.thought_in_next,
        )

        if rse_tool is None:
            err_obs = (
                "register_skill_event tool is not available in this profile. "
                "Cannot register an event subscription."
            )
            rse_step.observation = [Part(root=TextPart(text=err_obs))]
            rse_step.observation_text = err_obs
            yield {
                "type": ChatCompletionTypeEnum.RESULT_ARTIFACT,
                "data": {"Observation": rse_step.observation},
            }
            self._record_step_full(rse_step, err_obs)
            if self.context_id:
                self._save_context(self.context_id)
            if not self.reasoning:
                async for r in self._finalize_without_reasoning(rse_step, err_obs):
                    yield r
                return
            async for r in self._loop(rse_step):
                yield r
            return

        # 3. Refresh child LLM and emit a thinking artifact for the auto call.
        if rse_tool.tool_type in (ToolType.BUILTIN, ToolType.MCP):
            rse_tool.refresh_llm(self.profile)
        yield {
            "type": ChatCompletionTypeEnum.THINKING_ARTIFACT,
            "data": {
                "Thought": rse_step.thought,
                "Action": rse_tool.tool_id,
                "Action_Input": user_input,
                "Model_Label": self._model_label_for(rse_tool),
                "Reasoning_Model_Label": self.llm.model_label,
            },
        }

        # 4. Drive register_skill_event with skill identity pinned via injected
        #    arguments. Existing _profile/_context_id injection happens inside
        #    the adapter; we add _skill_id and _skill_source so the tool knows
        #    which skill to subscribe and the prepare_tools callback can build
        #    the dynamic ``trigger`` enum from SKILL.md.
        rse_arguments: Dict[str, Any] = {
            "_skill_id": skill_id,
            "_skill_source": skill_source,
        }
        # Layer in any persisted client arguments for register_skill_event.
        try:
            rse_arguments.update(self._load_arguments(rse_tool.tool_id) or {})
        except Exception:  # noqa: BLE001
            pass
        rse_variables = self._load_variables(rse_tool.tool_id)
        rse_llm_params = self._load_llm_params(rse_tool.tool_id)

        result_event: Optional[ToolResultEvent] = None
        error_event: Optional[ToolErrorEvent] = None
        async for kind, ev_payload in self._run_builtin_tool_events(
            rse_tool,
            query=user_input,
            arguments=rse_arguments,
            variables=rse_variables,
            llm_params=rse_llm_params,
        ):
            if kind == "status":
                yield ev_payload
            elif kind == "result":
                result_event = ev_payload
            elif kind == "error":
                error_event = ev_payload

        # 5. Record the register_skill_event step. On error, surface it; do not
        #    fall back to the load path — that would silently invert the user's
        #    intent.
        if error_event is not None:
            obs_text = error_event.message or "register_skill_event failed."
            rse_step.observation = [Part(root=TextPart(text=obs_text))]
            rse_step.observation_text = obs_text
            yield {
                "type": ChatCompletionTypeEnum.RESULT_ARTIFACT,
                "data": {"Observation": rse_step.observation},
            }
            self._record_step_full(rse_step, obs_text)
        elif result_event is not None:
            if result_event.token_usage:
                self._total_input_tokens += result_event.token_usage.get("input_tokens", 0) or 0
                self._total_output_tokens += result_event.token_usage.get("output_tokens", 0) or 0
            obs_text = result_event.observation_text or ""
            obs_parts = result_event.observation_parts
            rse_step.observation = obs_parts
            rse_step.observation_text = obs_text
            yield {
                "type": ChatCompletionTypeEnum.RESULT_ARTIFACT,
                "data": {"Observation": obs_parts},
            }
            self._record_step_full(rse_step, obs_text)
        else:
            obs_text = "register_skill_event returned no result."
            rse_step.observation = [Part(root=TextPart(text=obs_text))]
            rse_step.observation_text = obs_text
            self._record_step_full(rse_step, obs_text)

        if self.context_id:
            self._save_context(self.context_id)

        if not self.reasoning:
            async for r in self._finalize_without_reasoning(
                rse_step, rse_step.observation_text or "",
            ):
                yield r
            return
        async for r in self._loop(rse_step):
            yield r

    # ── single-step finalization (reasoning disabled) ──────────────────

    async def _finalize_without_reasoning(
        self, step: StepData, observation_text: str,
    ) -> AsyncGenerator[ReasoningStreamResponseType, None]:
        """Send tool result back to LLM as a tool-call output and get a final text answer."""

        messages: List[ChatCompletionMessageParam] = [
            {"role": "system", "content": "Please respond concisely based on the tool call content without including any explanation."},
            {"role": "user", "content": step.input or (self.steps[0].replace("Input: ", "").strip() if self.steps else "")},
        ]

        # Assistant message with tool_calls (the reasoning step that picked the tool)
        call_id = "call_0"
        messages.append({
            "role": "assistant",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "tool_result",
                        "arguments": json.dumps({}),
                    },
                }
            ],
        })

        # Tool result message
        messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": observation_text or "No result",
        })

        logger.info(f"Messages for final LLM call: {messages}")

        # Second LLM call — no tools, just generate the final text answer
        content_parts: List[str] = []
        try:
            async for response in self.llm.chat_completion(
                messages=messages,
                tools=None,
                temperature=1,
            ):
                if response["type"] == ChatCompletionTypeEnum.CONTENT:
                    content = response.get("data")
                    if content:
                        content_parts.append(content)
                elif response["type"] == ChatCompletionTypeEnum.DONE:
                    self._total_input_tokens += response.get("input_tokens") or 0
                    self._total_output_tokens += response.get("output_tokens") or 0
                    break
        except AgentException as err:
            logger.error(f"Final LLM call failed in single-step mode: {err}")
            yield {
                "type": ChatCompletionTypeEnum.CLARIFY,
                "data": observation_text or "I encountered an error generating a response.",
                "input_tokens": self._total_input_tokens,
                "output_tokens": self._total_output_tokens,
            }
            return

        final_text = "".join(content_parts) if content_parts else observation_text or ""

        # Clear context (same as TERMINATE behavior)
        self._loaded_skill_ids.clear()
        self._loaded_skill_sections.clear()
        if self.context_id:
            self._clear_context(self.context_id)
            clear_context(self.context_id, LOADED_SKILLS_KEY)
            # Evict any stale per-call shell-dir override left by pre-fix
            # conversations so it can never shadow the unified working dir.
            clear_context(self.context_id, "current_shell_directory")

        yield {
            "type": ChatCompletionTypeEnum.DONE,
            "data": final_text,
            "input_tokens": self._total_input_tokens,
            "output_tokens": self._total_output_tokens,
        }

    # ── step bookkeeping ──────────────────────────────────────────────

    def _first_tool_id_of(self, tool_type: ToolType, name: Optional[str] = None) -> str:
        for t in self._tools:
            if t.tool_type is tool_type and (name is None or t.name == name):
                return t.tool_id
        return name or ""

    def _record_action_step(self, step: StepData, response_text: str) -> None:
        step_text = f"Action: {step.action}\nAction_Input: {response_text}\n"
        if step.thought_in_next:
            step_text += f"Thought_In_Next: {step.thought_in_next}\n"
        self.steps.append(step_text)
        self._trim_steps()

    def _record_step_with_observation(self, step: StepData, observation_text: str) -> None:
        step_text = (
            (f"Thought: {step.thought}\n" if step.thought else "")
            + (f"Action: {step.action}\n" if step.action else "")
            + (f"Action_Input: {step.action_input}\n" if step.action_input else "")
            + (
                f"Observation:\n{OBSERVATION_START_MARKER}\n{observation_text}\n{OBSERVATION_END_MARKER}\n"
                if observation_text else ""
            )
        )
        self.steps.append(step_text)
        self._trim_steps()

    def _record_step_full(self, step: StepData, observation_text: str) -> None:
        step_text = (
            (f"Thought: {step.thought}\n" if step.thought else "")
            + (f"Action: {step.action}\n" if step.action else "")
            + (f"Action_Input: {step.action_input}\n" if step.action_input else "")
            + (f"Thought_In_Next: {step.thought_in_next}\n" if step.thought_in_next else "")
            + (
                f"Observation:\n{OBSERVATION_START_MARKER}\n{observation_text}\n{OBSERVATION_END_MARKER}\n"
                if observation_text else ""
            )
        )
        self.steps.append(step_text)
        self._trim_steps()

    def _save_context(self, context_id: str) -> None:
        self.context_store.save_context(
            context_id=context_id, steps=self.steps, step_count=self.current_step_count,
        )

    def _clear_context(self, context_id: str) -> None:
        self.context_store.clear_context(context_id)

    def _trim_steps(self) -> None:
        while len(self.steps) > self.steps_length and len(self.steps) > 1:
            self.steps.pop(1)

    def _append_input_step(self, user_input: str) -> None:
        self.steps.append(f"Input: {user_input}\n")
        self._trim_steps()

    @classmethod
    def get_context(cls, context_id: str) -> Optional[Dict[str, Any]]:
        store = ReasoningContextStore()
        return store.get_context(context_id)

    @classmethod
    def clear_all_contexts(cls) -> None:
        """Clear all saved contexts (useful for cleanup)."""
        store = ReasoningContextStore()
        store.clear_all_contexts()
