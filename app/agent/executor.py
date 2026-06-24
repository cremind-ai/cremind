import asyncio
import uuid
from typing import Any, Dict, List, Tuple, cast

from a2a.server.agent_execution import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    AgentCard,
    DataPart,
    FilePart,
    TaskState,
    TextPart,
    Part,
    Message,
    Role,
    UnsupportedOperationError,
)

from a2a.utils import new_agent_text_message, new_task
from a2a.utils.errors import ServerError

from app.agent.agent import CremindAgent
from app.agent.summary import summarize_reasoning
from app.config.settings import BaseConfig
from app.constants import ChatCompletionTypeEnum
from app.lib.llm import LLMProvider
from app.storage.conversation_storage import ConversationStorage

from app.utils import logger, build_table_embeddings, find_similar_items
from app.utils.common import convert_db_messages_to_history, convert_task_history_to_messages
from app.utils.context_storage import get_context
from app.utils.task_context import current_task_id_var


class CremindAgentExecutor(AgentExecutor):
    """An AgentExecutor that runs the CremindAgent.

    This implements the A2A protocol surface used by external A2A clients
    (other agents calling us). The Cremind web frontend does NOT go through
    this path anymore -- it POSTs to ``/api/conversations/{id}/messages``
    and subscribes to ``/api/conversations/{id}/stream`` so streams survive
    navigation and fan out to multiple tabs. Both paths share the same
    cancellation registry: see ``cancel_by_task_id`` and the unified
    ``app.agent.stream_runner.cancel_run``.
    """

    def __init__(self, cremind_agent: CremindAgent, conversation_storage: ConversationStorage | None = None):
        logger.debug("Initializing CremindAgentExecutor...")
        self._running_tasks: dict[str, asyncio.Task] = {}
        self.cremind_agent: CremindAgent | None = cremind_agent
        self.conversation_storage = conversation_storage

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ):
        logger.debug("Starting execution")

        # Extract profile from JWT token (via call_context.state)
        profile = None
        call_context = context._call_context
        if call_context and hasattr(call_context, 'state') and call_context.state:
            profile = call_context.state.get("profile")

        if not profile:
            raise ServerError(error=UnsupportedOperationError(
                message="Profile is required. Please authenticate with a valid token."
            ))

        logger.debug(f"Profile: {profile}")
        logger.debug(context.context_id)
        logger.debug(context.task_id)

        context_message = context.message
        if context_message and hasattr(context_message, "parts"):
            logger.debug(context_message.parts)
        else:
            logger.debug("No message parts available on context.")

        query = context.get_user_input()
        logger.debug(query)

        task = context.current_task

        context_id = context.context_id

        if not task:
            if context_message is None:
                logger.error("Request context is missing the originating message.")
                raise ServerError(error=UnsupportedOperationError())

            task = new_task(context_message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue, task.id, task.context_id)

        if not self.cremind_agent:
            # Should not happen if initialized correctly
            logger.error("CremindAgent not initialized")
            raise ServerError(error=UnsupportedOperationError())

        artifact_id = str(uuid.uuid4())
        has_sent_first_chunk = False
        final_response_text_parts = []
        total_input_tokens = 0
        total_cache_read_input_tokens = 0
        total_cache_creation_input_tokens = 0
        total_output_tokens = 0
        collected_thinking_steps: list[dict] = []
        collected_file_parts: list[Part] = []
        reasoning_input_section: str | None = None

        # Initialize tiktoken encoder for token-by-token streaming. Lazy
        # import: tiktoken lives in the ``tokenization`` extras group and
        # may not be present on a thin-core install.
        from tiktoken import encoding_for_model
        encoder = encoding_for_model("gpt-4o")

        # Load history from DB (includes message IDs for the message_detail tool).
        # Fall back to task.history when no DB conversation exists yet.
        history_messages = []
        if self.conversation_storage and profile and context_id:
            try:
                conv = await self.conversation_storage.get_conversation_by_context(
                    profile=profile, context_id=context_id,
                )
                if conv:
                    db_msgs = await self.conversation_storage.get_messages(conv["id"])
                    if db_msgs:
                        history_messages = convert_db_messages_to_history(db_msgs, inject_ids=True)
            except Exception:
                logger.debug("Failed to load history from DB, falling back to task.history", exc_info=True)

        if not history_messages:
            history_messages = convert_task_history_to_messages(task.history or [])

        # Extract reasoning preference from request metadata (default: True)
        reasoning = context.metadata.get("reasoning", True)

        # Register this asyncio task so the cancel API can target it, and
        # publish the task id to the ContextVar so downstream code (notably
        # exec_shell's process registry) can tag spawned subprocesses for
        # later targeted termination.
        task_key = task.id
        self._running_tasks[task_key] = asyncio.current_task()
        ctx_token = current_task_id_var.set(task_key)

        try:
            async for chunk in self.cremind_agent.run(query, history_messages, context_id, profile=profile, reasoning=reasoning):
                # logger.debug(f"Received chunk from CremindAgent: {chunk}")
                if chunk["type"] == ChatCompletionTypeEnum.CONTENT:
                    content = chunk.get("data")
                    if content:
                        final_response_text_parts.append(content)

                        # Tokenize content and stream token by token
                        tokens = encoder.encode(content)
                        for token in tokens:
                            token_text = encoder.decode([token])
                            await updater.add_artifact(
                                [Part(root=TextPart(text=token_text))],
                                artifact_id,
                                name="Text Response",
                                append=has_sent_first_chunk,
                                last_chunk=False
                            )
                            has_sent_first_chunk = True
                            # Add delay to simulate token latency
                            await asyncio.sleep(0.001)  # 1ms delay per token
                elif chunk["type"] == ChatCompletionTypeEnum.THINKING_ARTIFACT:
                    # Emit thinking artifact as a separate DataPart
                    thinking_data = chunk.get("data", {})
                    await updater.add_artifact(
                        [Part(root=DataPart(data=thinking_data, kind="data", metadata=None))],
                        name="thinking",
                        append=False,
                        last_chunk=True
                    )
                    # Collect for persistence
                    collected_thinking_steps.append({
                        "thought": thinking_data.get("Thought", ""),
                        "action": thinking_data.get("Action", ""),
                        "action_input": thinking_data.get("Action_Input", ""),
                        "model_label": thinking_data.get("Model_Label"),
                        "reasoning_model_label": thinking_data.get("Reasoning_Model_Label"),
                    })
                elif chunk["type"] == ChatCompletionTypeEnum.RESULT_ARTIFACT:
                    # Observation is now list[Part] from the reasoning agent
                    result_data = chunk.get("data", {})
                    observation_parts = result_data.get("Observation", [])

                    # Serialize Part objects to dicts for the frontend DataPart
                    serialized_observation = []
                    for obs_part in observation_parts:
                        if hasattr(obs_part, "root") and hasattr(obs_part.root, "model_dump"):
                            serialized_observation.append(obs_part.root.model_dump(mode="json"))
                        elif hasattr(obs_part, "model_dump"):
                            serialized_observation.append(obs_part.model_dump(mode="json"))
                        elif isinstance(obs_part, dict):
                            serialized_observation.append(obs_part)

                    await updater.add_artifact(
                        [Part(root=DataPart(data={"Observation": serialized_observation}, kind="data", metadata=None))],
                        name="result",
                        append=False,
                        last_chunk=True
                    )

                    # Emit each FilePart as a separate "file" artifact for the frontend
                    for obs_part in observation_parts:
                        if hasattr(obs_part, "root") and isinstance(obs_part.root, FilePart):
                            collected_file_parts.append(obs_part)
                            await updater.add_artifact(
                                [obs_part],
                                name="file",
                                append=False,
                                last_chunk=True,
                            )

                    # Emit a "terminal" artifact for every long-running process
                    # spawned by exec_shell in this observation. The DataPart
                    # built by the builtin adapter carries {tool_name: structured_content};
                    # we scan each structured_content for category == "long_running".
                    emitted_terminal_pids: set[str] = set()
                    for obs_part in observation_parts:
                        root = getattr(obs_part, "root", obs_part)
                        if not isinstance(root, DataPart):
                            continue
                        data = root.data or {}
                        # Two shapes to support: the adapter wraps results under
                        # the tool name, but some code paths may pass the bare
                        # structured_content through.
                        candidates: list[dict] = []
                        for value in data.values() if isinstance(data, dict) else []:
                            if isinstance(value, dict):
                                candidates.append(value)
                        if isinstance(data, dict):
                            candidates.append(data)
                        for payload in candidates:
                            pid = payload.get("process_id")
                            if (
                                payload.get("category") == "long_running"
                                and isinstance(pid, str)
                                and pid not in emitted_terminal_pids
                            ):
                                emitted_terminal_pids.add(pid)
                                cmd = str(payload.get("command", "") or "")
                                short = cmd if len(cmd) <= 36 else cmd[:36].rstrip() + " …"
                                terminal_payload = {
                                    "process_id": pid,
                                    "command": cmd,
                                    "command_short": short,
                                    "working_directory": payload.get("working_directory", ""),
                                    "pty": bool(payload.get("pty", False)),
                                }
                                await updater.add_artifact(
                                    [Part(root=DataPart(data=terminal_payload, kind="data", metadata=None))],
                                    name="terminal",
                                    append=False,
                                    last_chunk=True,
                                )

                    # Attach observation parts to the latest thinking step for storage
                    if collected_thinking_steps:
                        for step in reversed(collected_thinking_steps):
                            if "observation" not in step:
                                step["observation"] = serialized_observation
                                break
                elif chunk["type"] in [ChatCompletionTypeEnum.DONE, ChatCompletionTypeEnum.CLARIFY]:
                    content = chunk.get("data")
                    if content:
                        final_response_text_parts.append(content)

                        # Tokenize content and stream token by token
                        tokens = encoder.encode(content)
                        for token in tokens:
                            token_text = encoder.decode([token])
                            await updater.add_artifact(
                                [Part(root=TextPart(text=token_text))],
                                artifact_id,
                                name="Text Response",
                                append=has_sent_first_chunk,
                                last_chunk=False
                            )
                            has_sent_first_chunk = True
                            # Add delay to simulate token latency
                            await asyncio.sleep(0.001)  # 1ms delay per token
                    # Capture token usage from the final chunk
                    total_input_tokens = chunk.get("input_tokens") or 0
                    total_cache_read_input_tokens = chunk.get("cache_read_input_tokens") or 0
                    total_cache_creation_input_tokens = chunk.get("cache_creation_input_tokens") or 0
                    total_output_tokens = chunk.get("output_tokens") or 0
                    # DONE carries input_section only when the turn was
                    # multi-step (i.e. worth summarizing); presence of this
                    # field is the signal to invoke the summarizer.
                    if chunk["type"] == ChatCompletionTypeEnum.DONE and chunk.get("input_section"):
                        reasoning_input_section = chunk["input_section"]
        except asyncio.CancelledError:
            logger.info(f"Task {task_key} cancelled by user")
            from app.tools.builtin.exec_shell import cancel_processes_by_task
            killed = await cancel_processes_by_task(task_key)
            if killed:
                logger.info(
                    f"Killed {killed} subprocess(es) for cancelled task {task_key}"
                )
            try:
                await updater.update_status(
                    TaskState.canceled,
                    message=new_agent_text_message("Stopped by user."),
                    final=True,
                )
            except Exception:
                logger.exception("Failed to send canceled status")
            current_task_id_var.reset(ctx_token)
            self._running_tasks.pop(task_key, None)
            return
        except ServerError:
            current_task_id_var.reset(ctx_token)
            self._running_tasks.pop(task_key, None)
            raise
        except Exception as e:
            current_task_id_var.reset(ctx_token)
            self._running_tasks.pop(task_key, None)
            logger.error(f"Error during agent execution: {e}")
            await updater.update_status(
                TaskState.failed,
                message=new_agent_text_message(str(e)),
            )
            raise ServerError(error=UnsupportedOperationError(message=str(e)))

        final_response_text = "".join(final_response_text_parts)
        # logger.debug(f"Final response text from CremindAgent: {final_response_text}")

        # Mark artifact as done
        await updater.add_artifact(
            [Part(root=TextPart(text=""))],
            artifact_id,
            name="Text Response",
            append=has_sent_first_chunk,
            last_chunk=True,
        )

        # Summarize the reasoning chain when the turn was multi-step.
        # Uses the low model group; failures must not break the user-visible answer.
        summary_text: str | None = None
        if reasoning_input_section:
            try:
                # Signal to the UI that the indicator should say "summarizing".
                await updater.add_artifact(
                    [Part(root=DataPart(
                        data={"phase": "summarizing"},
                        kind="data", metadata=None,
                    ))],
                    name="phase",
                    append=False,
                    last_chunk=True,
                )
                summary_text = await summarize_reasoning(
                    self.cremind_agent, reasoning_input_section, profile,
                )
                if summary_text:
                    await updater.add_artifact(
                        [Part(root=DataPart(
                            data={"summary": summary_text},
                            kind="data", metadata=None,
                        ))],
                        name="summary",
                        append=False,
                        last_chunk=True,
                    )
            except Exception:
                logger.exception("Failed to produce reasoning summary")
                summary_text = None

        # Emit token_usage artifact if we have any token data
        if total_input_tokens > 0 or total_output_tokens > 0:
            logger.debug(
                f"Total token usage - input: {total_input_tokens}, "
                f"cache_read: {total_cache_read_input_tokens}, "
                f"cache_creation: {total_cache_creation_input_tokens}, "
                f"output: {total_output_tokens}"
            )
            await updater.add_artifact(
                [Part(root=DataPart(data={
                    "token_usage": {
                        "input_tokens": total_input_tokens,
                        "cache_read_input_tokens": total_cache_read_input_tokens,
                        "cache_creation_input_tokens": total_cache_creation_input_tokens,
                        "output_tokens": total_output_tokens,
                    }
                }, kind="data", metadata=None))],
                name="token_usage"
            )

        final_parts: list[Part] = [Part(root=TextPart(text=final_response_text))]
        final_parts.extend(collected_file_parts)

        await updater.update_status(
            TaskState.working,
            Message(
                role=Role.agent,
                parts=final_parts,
                message_id=str(uuid.uuid4()),
                task_id=task.id,
                context_id=task.context_id,
            ),
        )

        # Persist completed messages to SQLite
        if self.conversation_storage and profile:
            try:
                conv = await self.conversation_storage.get_or_create_conversation(
                    profile=profile, context_id=context_id, task_id=task.id,
                )
                conversation_id = conv["id"]

                # Save user message
                user_parts = list(context_message.parts) if context_message and context_message.parts else None
                await self.conversation_storage.add_message(
                    conversation_id=conversation_id,
                    role="user",
                    content=query,
                    parts=user_parts,
                )

                # Save agent message
                token_usage_data = None
                if total_input_tokens > 0 or total_output_tokens > 0:
                    token_usage_data = {
                        "input_tokens": total_input_tokens,
                        "cache_read_input_tokens": total_cache_read_input_tokens,
                        "cache_creation_input_tokens": total_cache_creation_input_tokens,
                        "output_tokens": total_output_tokens,
                    }

                # Only persist non-text parts (FileParts); text is already in `content` column
                persist_parts = collected_file_parts if collected_file_parts else None

                await self.conversation_storage.add_message(
                    conversation_id=conversation_id,
                    role="agent",
                    content=final_response_text,
                    parts=persist_parts,
                    thinking_steps=collected_thinking_steps if collected_thinking_steps else None,
                    token_usage=token_usage_data,
                    summary=summary_text,
                )

                # Update conversation title from first user message if still default
                if conv.get("title") == "Untitled Chat" and query:
                    title = query.strip()[:40] + ("..." if len(query.strip()) > 40 else "")
                    await self.conversation_storage.update_conversation(
                        conversation_id, title=title, task_id=task.id,
                    )
                else:
                    await self.conversation_storage.update_conversation(
                        conversation_id, task_id=task.id,
                    )

                logger.debug(f"Persisted messages for conversation {conversation_id}")
            except Exception as e:
                logger.error(f"Failed to persist conversation messages: {e}")

        current_task_id_var.reset(ctx_token)
        self._running_tasks.pop(task_key, None)

    async def cancel(self, context: RequestContext, event_queue: EventQueue):
        """A2A-protocol cancel hook: cancel the running asyncio task for ``task_id``."""
        task_id = context.task_id
        if task_id and self.cancel_by_task_id(task_id):
            logger.info(f"A2A cancel: cancelled task {task_id}")
            return
        logger.info(f"A2A cancel: no active task for {task_id!r}")
        raise ServerError(error=UnsupportedOperationError())

    def cancel_by_task_id(self, task_id: str) -> bool:
        """Cancel the running asyncio task for ``task_id``.

        Returns True if a live task was found and cancellation was requested,
        False otherwise. Idempotent: safe to call when the task has already
        completed or never existed.
        """
        running = self._running_tasks.get(task_id)
        if running is None or running.done():
            return False
        running.cancel()
        return True
