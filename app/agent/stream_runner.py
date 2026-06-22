"""Unified streaming runner for agent runs.

This module owns a single agent-to-bus pipeline used by *both* user-typed
messages (via ``POST /api/conversations/{id}/messages``) and skill-event
triggered runs (via :mod:`app.events.runner`). The previous codebase had two
near-identical implementations -- one in :class:`CremindAgentExecutor` and one
in :func:`app.events.runner.run_event` -- which diverged in subtle ways and
forced clients to choose between two streaming protocols (A2A SDK over a
client-owned HTTP request vs. SSE subscription with replay). Consolidating
here lets the SSE path serve both: a browser client POSTs a message, the run
executes in a background asyncio task, and any number of SSE subscribers
(across tabs, navigations, reconnects) receive the same chunks.

Responsibilities:

* Run :meth:`CremindAgent.run` for a conversation.
* Publish each chunk to the :class:`ConversationStreamBus` using the
  vocabulary already understood by the frontend (``text``, ``thinking``,
  ``result``, ``file``, ``terminal``, ``token_usage``, ``phase``,
  ``summary``, ``complete``, ``error``, ``user_message``).
* Persist the user message (when applicable) and the final assistant message
  to SQLite so a reload reproduces what was rendered.
* Register the asyncio task in a single registry keyed by ``run_id`` so a
  uniform cancel API can target it regardless of how the run was launched.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict, List, Optional

from a2a.types import DataPart, FilePart, Part

from app.agent import memory_runner
from app.agent.summary import summarize_reasoning
from app.config.user_config import resolve_memory_config
from app.constants import ChatCompletionTypeEnum
from app.events.notifications_buffer import get_event_notifications
from app.events.stream_bus import get_event_stream_bus
from app.lib.llm.exceptions import SetupRequiredError
from app.utils.logger import logger
from app.utils.message_tokens import resolve_message_tokens
from app.utils.task_context import current_task_id_var


# ── unified cancellation registry ───────────────────────────────────────────

# Maps run_id -> the asyncio.Task running the agent loop. Populated when a
# run starts; cleared when it ends. Both the A2A executor and the user-message
# POST handler register here so a single cancel endpoint targets either.
_running_runs: Dict[str, asyncio.Task] = {}


def _append_attachments_note(
    agent_query: str, attachments: Optional[List[Dict[str, Any]]],
) -> str:
    """Append a note listing uploaded-file absolute paths to the agent's input.

    Returns ``agent_query`` unchanged when there are no valid attachments.
    """
    if not attachments:
        return agent_query
    paths = [a.get("path") for a in attachments if isinstance(a, dict) and a.get("path")]
    if not paths:
        return agent_query
    lines = ["[Attached files — saved to a temporary folder; absolute paths:]"]
    lines += [f"- {p}" for p in paths]
    lines.append(
        "(Read or convert these with the system_file tools — or, to understand "
        "the visual content of an image, the image_understanding tool. If the "
        "user asks to keep or save a file, move it into their working directory.)"
    )
    note = "\n".join(lines)
    return f"{agent_query}\n\n{note}" if agent_query else note


def cancel_run(run_id: str) -> bool:
    """Cancel the running asyncio task for ``run_id``. Idempotent."""
    task = _running_runs.get(run_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True


def is_running(run_id: str) -> bool:
    task = _running_runs.get(run_id)
    return bool(task and not task.done())


# ── helpers (lifted from runner.py) ─────────────────────────────────────────


def _trim(text: str, n: int = 240) -> str:
    text = text.strip()
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


def _format_trigger_content(event_type: str, action: str, content: str) -> str:
    """Render a skill-event trigger as a markdown block for an agent bubble.

    Picks an outer fence wider than the longest backtick run inside ``content``
    so embedded code fences round-trip through markdown rendering.
    """
    longest_backtick_run = 0
    run = 0
    for ch in content:
        if ch == "`":
            run += 1
            if run > longest_backtick_run:
                longest_backtick_run = run
        else:
            run = 0
    fence = "`" * max(3, longest_backtick_run + 1)
    return (
        f"Trigger: {event_type}\n"
        f"Action: {action}\n"
        f"Content:\n"
        f"{fence}\n{content}\n{fence}"
    )


def _serialize_observation(observation_parts: List[Any]) -> List[Dict[str, Any]]:
    """Serialize Part objects to dicts for the frontend DataPart payload."""
    serialized: List[Dict[str, Any]] = []
    for obs_part in observation_parts:
        if hasattr(obs_part, "root") and hasattr(obs_part.root, "model_dump"):
            serialized.append(obs_part.root.model_dump(mode="json"))
        elif hasattr(obs_part, "model_dump"):
            serialized.append(obs_part.model_dump(mode="json"))
        elif isinstance(obs_part, dict):
            serialized.append(obs_part)
    return serialized


def _terminal_payloads(observation_parts: List[Any]) -> List[Dict[str, Any]]:
    """Extract one terminal payload per long-running subprocess in an observation."""
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for obs_part in observation_parts:
        root = getattr(obs_part, "root", obs_part)
        if not isinstance(root, DataPart):
            continue
        data = root.data or {}
        candidates: list[dict] = []
        if isinstance(data, dict):
            for value in data.values():
                if isinstance(value, dict):
                    candidates.append(value)
            candidates.append(data)
        for payload in candidates:
            pid = payload.get("process_id")
            if (
                payload.get("category") == "long_running"
                and isinstance(pid, str)
                and pid not in seen
            ):
                seen.add(pid)
                cmd = str(payload.get("command", "") or "")
                short = cmd if len(cmd) <= 36 else cmd[:36].rstrip() + " …"
                out.append({
                    "process_id": pid,
                    "command": cmd,
                    "command_short": short,
                    "working_directory": payload.get("working_directory", ""),
                    "pty": bool(payload.get("pty", False)),
                })
    return out


# ── unified runner ──────────────────────────────────────────────────────────


async def run_agent_to_bus(
    *,
    cremind_agent: Any,
    conversation_storage: Any,
    conversation_id: str,
    run_id: str,
    profile: str,
    query: str,
    history_messages: List[Any],
    reasoning: bool = True,
    user_parts: List[Any] | None = None,
    user_message_metadata: Dict[str, Any] | None = None,
    agent_message_metadata: Dict[str, Any] | None = None,
    attachments: List[Dict[str, Any]] | None = None,
    push_user_message: bool = True,
    publish_notification: bool = False,
    update_title_from_query: bool = True,
    trigger_event: Dict[str, Any] | None = None,
) -> None:
    """Run the reasoning agent for one conversation, publishing chunks to the bus.

    On entry, marks the run active on the bus so any SSE subscriber receives
    each chunk live. On exit (success, cancel, or failure), publishes a
    terminal ``complete`` (or ``error``) event, persists the assistant message
    to SQLite, and clears the bus's ring buffer so a fresh subscriber doesn't
    re-render persisted history.

    Idempotent re: bus state: both ``start_run`` and ``end_run`` are safe to
    call when no subscriber is connected.
    """
    bus = get_event_stream_bus()

    # Mark the run active on the bus before we publish anything. Late
    # subscribers (e.g. a tab opened after the user typed) get the replay.
    # ``profile`` is captured here so subsequent publishes can also fan to
    # the per-profile multiplexed stream.
    await bus.start_run(conversation_id, profile)

    # Tag spawned subprocesses with this run's id so cancellation can target
    # them precisely (mirrors the legacy executor's ContextVar usage).
    ctx_token = current_task_id_var.set(run_id)
    _running_runs[run_id] = asyncio.current_task()

    # Capture conversation context up front. We allow get_conversation to
    # fail (e.g. transient DB hiccup) without aborting the run -- the agent
    # loop only really needs ``context_id`` for tool storage scoping, and
    # falling back to ``conversation_id`` is harmless.
    conv: Optional[dict] = None
    try:
        conv = await conversation_storage.get_conversation(conversation_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            f"stream_runner: failed to load conversation {conversation_id}"
        )
    context_id = (conv or {}).get("context_id") or conversation_id
    title = (conv or {}).get("title") or "Untitled Chat"

    # Back-fill context_id on the conv row when it was created via the web
    # POST /api/conversations path (which leaves context_id=NULL). Without
    # this, tools like register_skill_event do get_conversation_by_context
    # and miss → they spawn a stray "Untitled Chat" sibling instead of
    # attaching to the active conversation.
    if conv and not conv.get("context_id"):
        try:
            await conversation_storage.update_conversation(
                conversation_id, context_id=context_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                f"stream_runner: failed to back-fill context_id for {conversation_id}"
            )

    # Hydrate the per-conversation working-directory override into the
    # in-memory ContextStorage so the reasoning agent's prompt-builder
    # picks up the same cwd the user last selected (in the file tree or
    # via change_working_directory) before the previous restart. A
    # persisted path that has since been deleted is cleared and the run
    # falls back to the user default.
    try:
        from app.utils.working_directory import hydrate_working_directory
        await hydrate_working_directory(context_id, conversation_storage)
    except Exception:  # noqa: BLE001
        logger.exception(
            f"stream_runner: failed to hydrate cwd for {conversation_id}"
        )

    # Skill-event runs have no client-side POST, so the frontend never sets
    # the 'streaming' tracker that opens the per-conversation SSE. Push a
    # started notification on the global notifications stream so the sidebar
    # can lazily open that SSE and light up the streaming-dot.
    if publish_notification:
        try:
            logger.debug(
                f"[debug:started] pushing started notification "
                f"profile={profile} conversation_id={conversation_id} title={title!r}"
            )
            entry = get_event_notifications().push(
                profile=profile,
                conversation_id=conversation_id,
                conversation_title=title,
                message_preview="",
                kind="started",
            )
            logger.debug(f"[debug:started] pushed entry id={entry.get('id')} kind={entry.get('kind')}")
        except Exception:  # noqa: BLE001
            logger.exception("stream_runner: failed to push started notification")

    final_text_parts: List[str] = []
    collected_thinking_steps: List[dict] = []
    collected_file_parts: List[Part] = []
    total_input_tokens = 0
    total_output_tokens = 0
    reasoning_input_section: str | None = None
    errored = False
    cancelled = False

    # Memory feature (independent background session). Read the per-profile
    # config once for this turn: used both to inject stored memory into the
    # prompt (consumption) and to decide whether to schedule a post-turn
    # extraction. Any failure simply disables memory for this turn.
    try:
        memory_cfg = resolve_memory_config(profile)
    except Exception:  # noqa: BLE001
        memory_cfg = None

    try:
        # 1. Persist the trigger / user message and announce it on the bus.
        #    For skill events we render the trigger as an *agent* bubble with
        #    a structured Trigger/Action/Content block so a reload can tell
        #    operator-typed turns apart from event-synthesised ones. The agent
        #    loop below still receives ``query`` as its user-side input — only
        #    the persistence + UI display change here.
        if trigger_event is not None:
            trigger_content = _format_trigger_content(
                event_type=str(trigger_event.get("event_type", "")),
                action=str(trigger_event.get("action", "")),
                content=str(trigger_event.get("content", "")),
            )
            trigger_msg_id: Optional[str] = None
            try:
                trigger_msg = await conversation_storage.add_message(
                    conversation_id=conversation_id,
                    role="agent",
                    content=trigger_content,
                    metadata=user_message_metadata,
                )
                trigger_msg_id = (
                    trigger_msg.get("id") if isinstance(trigger_msg, dict) else None
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"stream_runner: failed to persist trigger message for {conversation_id}"
                )

            await bus.publish(conversation_id, "event_trigger_message", {
                "id": trigger_msg_id,
                "content": trigger_content,
                "metadata": user_message_metadata or {},
            })
        elif push_user_message:
            user_msg_id: Optional[str] = None
            try:
                user_msg = await conversation_storage.add_message(
                    conversation_id=conversation_id,
                    role="user",
                    content=query,
                    parts=user_parts,
                    metadata=user_message_metadata,
                )
                user_msg_id = user_msg.get("id") if isinstance(user_msg, dict) else None
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"stream_runner: failed to persist user message for {conversation_id}"
                )

            await bus.publish(conversation_id, "user_message", {
                "id": user_msg_id,
                "content": query,
                "metadata": user_message_metadata or {},
            })

        # 2. Stream the agent loop, mirroring chunks to the bus and collecting
        #    enough state to persist the final assistant message verbatim.
        #    The stored user message keeps the raw `$VAR` / `@profile` tokens
        #    (so the UI can re-render them as editable chips); the agent only
        #    ever sees the rendered plain text.
        agent_query = await resolve_message_tokens(
            query, profile=profile, conversation_storage=conversation_storage,
        )

        # Append uploaded-attachment paths to what the agent sees (NOT to the
        # persisted/published user message — that stays exactly what the user
        # typed). The temp paths live inside the system_file tool's allowed
        # roots, so the agent can read / convert / move them by absolute path.
        agent_query = _append_attachments_note(agent_query, attachments)

        # Consumption: read current memory (short-term for this conversation +
        # long-term for this profile) and pass it to the agent as a system block.
        # Fully decoupled from extraction — the agent never triggers or waits on
        # the background memory session.
        memory_context = ""
        if memory_cfg is not None and memory_cfg.enabled:
            memory_context = await memory_runner.load_memory_context(
                conversation_id, profile,
            )

        try:
            async for chunk in cremind_agent.run(
                query=agent_query,
                task_history=history_messages,
                context_id=context_id,
                profile=profile,
                reasoning=reasoning,
                triggered_by_event=trigger_event is not None,
                memory_context=memory_context,
            ):
                ctype = chunk.get("type")

                if ctype == ChatCompletionTypeEnum.CONTENT:
                    data = chunk.get("data")
                    if data:
                        final_text_parts.append(data)
                        await bus.publish(conversation_id, "text", {"token": data})

                elif ctype == ChatCompletionTypeEnum.THINKING_ARTIFACT:
                    thinking_data = chunk.get("data", {}) or {}
                    await bus.publish(conversation_id, "thinking", thinking_data)
                    collected_thinking_steps.append({
                        "thought": thinking_data.get("Thought", ""),
                        "action": thinking_data.get("Action", ""),
                        "action_input": thinking_data.get("Action_Input", ""),
                        "model_label": thinking_data.get("Model_Label"),
                        "reasoning_model_label": thinking_data.get("Reasoning_Model_Label"),
                    })

                elif ctype == ChatCompletionTypeEnum.RESULT_ARTIFACT:
                    result_data = chunk.get("data", {}) or {}
                    observation_parts = result_data.get("Observation", []) or []
                    serialized_observation = _serialize_observation(observation_parts)

                    await bus.publish(conversation_id, "result", {
                        "Observation": serialized_observation,
                    })

                    for obs_part in observation_parts:
                        if hasattr(obs_part, "root") and isinstance(obs_part.root, FilePart):
                            collected_file_parts.append(obs_part)
                            file_payload = obs_part.root.model_dump(mode="json")
                            await bus.publish(conversation_id, "file", file_payload)

                    for terminal in _terminal_payloads(observation_parts):
                        await bus.publish(conversation_id, "terminal", terminal)

                    if collected_thinking_steps:
                        for step in reversed(collected_thinking_steps):
                            if "observation" not in step:
                                step["observation"] = serialized_observation
                                break

                elif ctype in (
                    ChatCompletionTypeEnum.DONE,
                    ChatCompletionTypeEnum.CLARIFY,
                ):
                    data = chunk.get("data")
                    if data:
                        final_text_parts.append(data)
                        await bus.publish(conversation_id, "text", {"token": data})
                    total_input_tokens = chunk.get("input_tokens") or total_input_tokens
                    total_output_tokens = chunk.get("output_tokens") or total_output_tokens
                    if (
                        ctype == ChatCompletionTypeEnum.DONE
                        and chunk.get("input_section")
                    ):
                        reasoning_input_section = chunk["input_section"]
        except asyncio.CancelledError:
            cancelled = True
            logger.info(f"stream_runner: run {run_id} cancelled")
            try:
                from app.tools.builtin.exec_shell import cancel_processes_by_task
                killed = await cancel_processes_by_task(run_id)
                if killed:
                    logger.info(
                        f"stream_runner: killed {killed} subprocess(es) for cancelled run {run_id}"
                    )
            except Exception:  # noqa: BLE001
                logger.exception("stream_runner: subprocess cancellation failed")
            try:
                await bus.publish(conversation_id, "error", {
                    "message": "Stopped by user.",
                    "cancelled": True,
                })
            except Exception:  # noqa: BLE001
                logger.exception("stream_runner: failed to publish cancel event")
        except SetupRequiredError as exc:
            # Missing piece of one-time setup (e.g. no model picked for a
            # model group, no API key entered). The user can fix it without
            # restarting the server, so emit an actionable payload — code +
            # settings link — and let the front-end / CLI surface it as a
            # warning rather than a generic "see server logs".
            errored = True
            logger.warning(
                f"stream_runner: setup required for {conversation_id}: "
                f"code={exc.code} message={exc!s}"
            )
            final_text_parts.append(str(exc))
            try:
                await bus.publish(conversation_id, "error", exc.to_event_payload())
            except Exception:  # noqa: BLE001
                logger.exception("stream_runner: failed to publish setup-required event")
        except Exception as exc:  # noqa: BLE001
            errored = True
            logger.exception(f"stream_runner: agent run failed for {conversation_id}")
            # Surface the actual exception text so the user has a concrete
            # message to act on; the full traceback is still in the server
            # log for deep debugging.
            err_message = str(exc).strip() or exc.__class__.__name__
            final_text_parts.append(err_message)
            try:
                await bus.publish(conversation_id, "error", {
                    "message": err_message,
                    "code": "agent_run_failed",
                })
            except Exception:  # noqa: BLE001
                logger.exception("stream_runner: failed to publish error event")

        # 3. Optional reasoning summary (skip on cancel/error to keep the UI
        #    state consistent with the partial response).
        summary_text: str | None = None
        if reasoning_input_section and not errored and not cancelled:
            try:
                await bus.publish(conversation_id, "phase", {"phase": "summarizing"})
                summary_text = await summarize_reasoning(
                    cremind_agent, reasoning_input_section, profile,
                )
                if summary_text:
                    await bus.publish(conversation_id, "summary", {"summary": summary_text})
            except Exception:  # noqa: BLE001
                logger.exception("stream_runner: failed to produce reasoning summary")
                summary_text = None

        # 4. Token usage (after summary so any summary tokens are included
        #    upstream; here we only report what the agent loop reported).
        if total_input_tokens or total_output_tokens:
            await bus.publish(conversation_id, "token_usage", {
                "token_usage": {
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                },
            })

        # 5. Persist the final assistant message so a reload reproduces the
        #    rendered state. Failures are logged but do not break the stream
        #    -- the user has already seen the response.
        final_text = (
            "".join(final_text_parts).strip()
            or ("(stopped)" if cancelled else "(no response)")
        )
        token_usage_data: dict | None = None
        if total_input_tokens or total_output_tokens:
            token_usage_data = {
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
            }
        persist_parts = collected_file_parts if collected_file_parts else None

        assistant_msg_id: Optional[str] = None
        try:
            assistant_msg = await conversation_storage.add_message(
                conversation_id=conversation_id,
                role="agent",
                content=final_text,
                parts=persist_parts,
                thinking_steps=collected_thinking_steps or None,
                token_usage=token_usage_data,
                summary=summary_text,
                metadata=agent_message_metadata,
            )
            assistant_msg_id = (
                assistant_msg.get("id") if isinstance(assistant_msg, dict) else None
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                f"stream_runner: failed to persist assistant message for {conversation_id}"
            )

        # 5b. Memory trigger: if enough NEW message-content tokens (reasoning
        #     excluded) have accrued since the last extraction, kick off a
        #     background "memory session". Non-blocking and single-flight — it
        #     never interleaves with the next turn. Skipped on cancel/error so
        #     partial turns don't pollute memory.
        if memory_cfg is not None and memory_cfg.enabled and not errored and not cancelled:
            try:
                pending = await memory_runner.pending_token_count(conversation_id)
                if pending >= memory_cfg.trigger_token_threshold:
                    memory_runner.schedule_extraction(conversation_id, profile)
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"stream_runner: memory trigger check failed for {conversation_id}"
                )

        # 6. Update the conversation row (title from first query, task_id).
        try:
            update_kwargs: Dict[str, Any] = {"task_id": run_id}
            if (
                update_title_from_query
                and (conv or {}).get("title") == "Untitled Chat"
                and query.strip()
            ):
                short = query.strip()[:40] + ("..." if len(query.strip()) > 40 else "")
                update_kwargs["title"] = short
                title = short  # for the optional notification below
            await conversation_storage.update_conversation(
                conversation_id, **update_kwargs,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                f"stream_runner: failed to update conversation row for {conversation_id}"
            )

        # Notify the conversations-list SSE subscribers — title may have just
        # changed, message count went up, and the row's updated_at advanced.
        try:
            from app.events.conversations_list_bus import publish_conversations_changed
            publish_conversations_changed(profile)
        except Exception:  # noqa: BLE001
            logger.debug("conversations-list publish failed", exc_info=True)

        # 7. Terminal event so subscribers can flip isStreaming=false.
        await bus.publish(conversation_id, "complete", {
            "assistant_id": assistant_msg_id,
            "errored": errored,
            "cancelled": cancelled,
        })

        # 8. Optional notification (only the skill-event path opts in today).
        if publish_notification:
            try:
                get_event_notifications().push(
                    profile=profile,
                    conversation_id=conversation_id,
                    conversation_title=title,
                    message_preview=_trim(final_text),
                    kind="error" if errored else "completed",
                    priority="high" if errored else "normal",
                )
            except Exception:  # noqa: BLE001
                logger.exception("stream_runner: failed to push notification")
    finally:
        _running_runs.pop(run_id, None)
        if ctx_token is not None:
            current_task_id_var.reset(ctx_token)
        await bus.end_run(conversation_id)


def make_run_id(conversation_id: str, kind: str = "msg") -> str:
    """Generate a unified run id. ``kind`` is purely informational (``msg`` or ``event``)."""
    return f"{kind}:{conversation_id}:{uuid.uuid4()}"
