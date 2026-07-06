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
import mimetypes
import os
import uuid
from typing import Any, Dict, List, Optional

from a2a.types import DataPart, FilePart, Part

from app.agent.usage import reconcile
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
        "(Pass these paths to the tools EXACTLY as written above — they are "
        "absolute paths; do not shorten them, strip the home/drive prefix, or "
        "convert them to relative paths. Read or convert a file with the "
        "system_file tools, or understand an image's visual content with the "
        "image_understanding tool. If the user asks to keep or save a file, use "
        "the system_file move_file tool to move it into their working directory "
        "(or copy_file to keep a copy).)"
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
    mode: str = "reasoning",
    plan_action: str | None = None,
    user_parts: List[Any] | None = None,
    user_message_metadata: Dict[str, Any] | None = None,
    agent_message_metadata: Dict[str, Any] | None = None,
    attachments: List[Dict[str, Any]] | None = None,
    push_user_message: bool = True,
    publish_notification: bool = False,
    update_title_from_query: bool = True,
    trigger_event: Dict[str, Any] | None = None,
    event_run_id: str | None = None,
    event_run: bool = False,
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

    # Plan mode: clear any stale registry entry for this run id (defensive; ids
    # are unique per run) so a fresh turn never sees a prior turn's parked state.
    from app.agent import plan_state
    plan_state.clear(run_id)

    # Event-run start hook: mark the tracking row running (this turn's stream
    # run_id is the cancel target) and clear any stale pending flag. Done once
    # per turn — the trigger turn and any user-reply turns all pass through here.
    if event_run_id:
        from app.events import run_state
        run_state.clear(run_id)
        try:
            from app.storage import get_event_run_storage
            await get_event_run_storage().update_status(
                event_run_id, status="running", run_id=run_id, clear_pending=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception(f"stream_runner: failed to mark event run running for {event_run_id}")

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

    # Plan mode: decide the phase for this turn from the request + the
    # conversation's persisted plan state (see _compute_plan_phase).
    plan_phase: str | None = None
    if mode == "plan":
        plan_phase = await _compute_plan_phase(
            conversation_storage, conversation_id, plan_action,
        )
    # Observability: make the received mode/phase visible for diagnosing
    # "plan mode didn't engage" (only log the non-default modes).
    if mode != "reasoning":
        logger.info(
            f"stream_runner: run {run_id} mode={mode} plan_phase={plan_phase}"
        )

    # Back-fill context_id on the conv row when it was created via the web
    # POST /api/conversations path (which leaves context_id=NULL). Without
    # this, flows like skill-event subscription do get_conversation_by_context
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
    #
    # Event runs are excluded: their conversations are hidden (never in the
    # sidebar), so a 'started' entry would wrongly lazy-track a hidden conv.
    # They get event-run-specific notifications at the terminal boundary below.
    if publish_notification and not event_run_id:
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
    # Long-running terminal artifacts, collected as flat DataPart dicts so a
    # reload can re-render the terminal chip (see mapBackendMessage on the UI
    # side, which keys off ``kind:"data"`` + ``data.process_id``). Deduped by
    # process_id across the turn's observations.
    collected_terminal_parts: List[dict] = []
    seen_terminal_pids: set[str] = set()
    total_input_tokens = 0
    total_cache_read_input_tokens = 0
    total_cache_creation_input_tokens = 0
    total_output_tokens = 0
    # Per-source attribution for the turn (one entry per LLM invocation),
    # carried on terminal chunks. Overwritten each terminal chunk so the final
    # one holds the complete list (mirrors how the token totals are captured).
    collected_usage_records: list[dict] = []
    # The turn's native reasoning trace (assistant tool_calls + tool results + final
    # answer), carried on the terminal DONE chunk. Persisted so later turns can replay
    # it into history. ``None`` for turns with no tool calls (those replay content-only).
    collected_llm_messages: list | None = None
    errored = False
    cancelled = False
    # The id of this turn's just-persisted user/trigger message, captured below.
    # Passed to compaction so the current turn (sent separately as the volatile
    # input) is excluded from the rebuilt history tail.
    current_turn_msg_id: Optional[str] = None

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

            current_turn_msg_id = trigger_msg_id
            await bus.publish(conversation_id, "event_trigger_message", {
                "id": trigger_msg_id,
                "content": trigger_content,
                "metadata": user_message_metadata or {},
            })
        elif push_user_message:
            # Persist composer-uploaded attachments as file parts on the user
            # message so they re-render as file chips on reload (their abs path
            # lives under CREMIND_SYSTEM_DIR, so the ``uri`` is servable via
            # /api/files/open). The caller's own ``user_parts`` win if supplied.
            attachment_parts: List[dict] = []
            for a in (attachments or []):
                path = a.get("path")
                name = a.get("name") or os.path.basename(path or "")
                if not path:
                    continue
                mime, _ = mimetypes.guess_type(name or path)
                attachment_parts.append({
                    "kind": "file",
                    "file": {
                        "name": name,
                        "mimeType": mime or "application/octet-stream",
                        "uri": path,
                    },
                })
            effective_user_parts = user_parts or (attachment_parts or None)

            user_msg_id: Optional[str] = None
            try:
                user_msg = await conversation_storage.add_message(
                    conversation_id=conversation_id,
                    role="user",
                    content=query,
                    parts=effective_user_parts,
                    metadata=user_message_metadata,
                )
                user_msg_id = user_msg.get("id") if isinstance(user_msg, dict) else None
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"stream_runner: failed to persist user message for {conversation_id}"
                )

            current_turn_msg_id = user_msg_id
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

        # Compaction (also memory generation): replace the raw history with
        # [running summary + verbatim tail], folding the oldest turns into the
        # summary when the tail is over threshold — and, when memory is enabled,
        # extracting long-term facts in the same fold pass. Synchronous (before the
        # prompt is assembled) so the threshold is never exceeded — notably on the
        # first turn after upgrade, when a long pre-existing conversation would
        # otherwise be sent whole. No-op / falls back to the raw history on error.
        from app.agent import compaction
        history_messages = await compaction.build_compacted_history(
            conversation_id=conversation_id,
            profile=profile,
            conversation_storage=conversation_storage,
            cremind_agent=cremind_agent,
            fallback_history=history_messages,
            exclude_message_id=current_turn_msg_id,
        )

        # Long-term memory is NOT injected into the prompt (that would bust the
        # cache every turn). The model retrieves it on demand via the
        # ``search_memory`` tool.
        try:
            async for chunk in cremind_agent.run(
                query=agent_query,
                task_history=history_messages,
                context_id=context_id,
                profile=profile,
                reasoning=reasoning,
                triggered_by_event=trigger_event is not None,
                event_run=event_run,
                mode=mode,
                plan_phase=plan_phase,
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
                        "step": thinking_data.get("Step"),
                        "call_id": thinking_data.get("Call_Id"),
                        "tool": thinking_data.get("Tool", ""),
                        "tool_input": thinking_data.get("Tool_Input", ""),
                        "model_label": thinking_data.get("Model_Label"),
                    })

                elif ctype == ChatCompletionTypeEnum.RESULT_ARTIFACT:
                    result_data = chunk.get("data", {}) or {}
                    call_id = result_data.get("Call_Id")
                    # ``Result`` is the new key; fall back to ``Observation``.
                    result_parts = (
                        result_data.get("Result") or result_data.get("Observation") or []
                    )
                    serialized_result = _serialize_observation(result_parts)

                    await bus.publish(conversation_id, "result", {
                        "step": result_data.get("Step"),
                        "call_id": call_id,
                        "Result": serialized_result,
                    })

                    for obs_part in result_parts:
                        if hasattr(obs_part, "root") and isinstance(obs_part.root, FilePart):
                            collected_file_parts.append(obs_part)
                            file_payload = obs_part.root.model_dump(mode="json")
                            await bus.publish(conversation_id, "file", file_payload)

                    for terminal in _terminal_payloads(result_parts):
                        await bus.publish(conversation_id, "terminal", terminal)
                        pid = terminal["process_id"]
                        if pid not in seen_terminal_pids:
                            seen_terminal_pids.add(pid)
                            collected_terminal_parts.append({
                                "kind": "data",
                                "data": {**terminal, "category": "long_running"},
                            })

                    # Attach the result to its originating step (match by call_id,
                    # so parallel tools in one step pair up correctly).
                    if call_id:
                        for step in collected_thinking_steps:
                            if step.get("call_id") == call_id and "result" not in step:
                                step["result"] = serialized_result
                                break
                    else:
                        for step in reversed(collected_thinking_steps):
                            if "result" not in step:
                                step["result"] = serialized_result
                                break

                elif ctype == ChatCompletionTypeEnum.PLAN_EVENT:
                    # Plan-mode UI signal. ``data`` is {"event": <name>,
                    # "data": {...}}; publish it verbatim as its own bus event so
                    # the UI can render the question form / plan approval / todos.
                    evt = chunk.get("data") or {}
                    evt_name = evt.get("event")
                    if evt_name:
                        await bus.publish(conversation_id, evt_name, evt.get("data") or {})

                elif ctype in (
                    ChatCompletionTypeEnum.DONE,
                    ChatCompletionTypeEnum.CLARIFY,
                ):
                    data = chunk.get("data")
                    if data:
                        final_text_parts.append(data)
                        await bus.publish(conversation_id, "text", {"token": data})
                    total_input_tokens = chunk.get("input_tokens") or total_input_tokens
                    total_cache_read_input_tokens = chunk.get("cache_read_input_tokens") or total_cache_read_input_tokens
                    total_cache_creation_input_tokens = chunk.get("cache_creation_input_tokens") or total_cache_creation_input_tokens
                    total_output_tokens = chunk.get("output_tokens") or total_output_tokens
                    if chunk.get("usage_records"):
                        collected_usage_records = chunk["usage_records"]
                    if chunk.get("llm_messages"):
                        collected_llm_messages = chunk["llm_messages"]
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

        # 4. Token usage — what the agent loop reported this turn.
        if total_input_tokens or total_output_tokens:
            await bus.publish(conversation_id, "token_usage", {
                "token_usage": {
                    "input_tokens": total_input_tokens,
                    "cache_read_input_tokens": total_cache_read_input_tokens,
                    "cache_creation_input_tokens": total_cache_creation_input_tokens,
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
                "cache_read_input_tokens": total_cache_read_input_tokens,
                "cache_creation_input_tokens": total_cache_creation_input_tokens,
                "output_tokens": total_output_tokens,
            }
            # The single largest prompt the model processed this turn (final reasoning
            # call) = the real context size, used by compaction to gauge the window.
            # The four totals above are summed across calls and over-count, so this is
            # stored separately.
            from app.agent.compaction import context_tokens_from_records
            ctx = context_tokens_from_records(collected_usage_records)
            if ctx is not None:
                token_usage_data["context_tokens"] = ctx
        persist_parts = (collected_file_parts or []) + collected_terminal_parts or None

        # Stamp the turn's reasoning provider/model onto the message metadata so
        # the aggregate ``token_usage`` blob is attributable even without the
        # per-source rows (and so any future backfill can recover it).
        reasoning_rec = next(
            (r for r in collected_usage_records if r.get("source_kind") == "reasoning"),
            None,
        )
        if reasoning_rec and (reasoning_rec.get("provider") or reasoning_rec.get("model")):
            agent_message_metadata = {
                **(agent_message_metadata or {}),
                "provider": reasoning_rec.get("provider"),
                "model": reasoning_rec.get("model"),
            }

        # Plan mode: stamp the turn's plan state onto the message metadata so a
        # reload / restart can restore the pending question form, the plan
        # awaiting approval, or the todo panel. The plan tools wrote this into the
        # per-run registry; read the final snapshot here (todos are overwritten on
        # each update_todos call, so this is the latest). Stamped even on cancel
        # (this block runs after CancelledError is caught), so an interrupted
        # execution keeps its todo progress for resume.
        if mode == "plan":
            plan_meta = _plan_metadata_for_persist(run_id, plan_phase, cancelled)
            if plan_meta:
                agent_message_metadata = {
                    **(agent_message_metadata or {}),
                    "plan_mode": plan_meta,
                }

        assistant_msg_id: Optional[str] = None
        try:
            assistant_msg = await conversation_storage.add_message(
                conversation_id=conversation_id,
                role="agent",
                content=final_text,
                parts=persist_parts,
                thinking_steps=collected_thinking_steps or None,
                llm_messages=collected_llm_messages,
                token_usage=token_usage_data,
                metadata=agent_message_metadata,
            )
            assistant_msg_id = (
                assistant_msg.get("id") if isinstance(assistant_msg, dict) else None
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                f"stream_runner: failed to persist assistant message for {conversation_id}"
            )

        # 5a. Persist the per-source usage breakdown (one row per LLM invocation:
        #     reasoning step vs. each tool/sub-agent), with frozen estimated cost.
        #     Keyed to the assistant turn just persisted. Best-effort — never
        #     break the stream over usage accounting.
        if collected_usage_records:
            try:
                if token_usage_data and not reconcile(collected_usage_records, token_usage_data):
                    logger.warning(
                        f"stream_runner: usage records don't reconcile with turn totals "
                        f"for {conversation_id} (records sum != aggregate)"
                    )
                from app.storage import get_usage_storage
                await get_usage_storage().add_usage_records(
                    conversation_id=conversation_id,
                    profile=profile,
                    records=collected_usage_records,
                    message_id=assistant_msg_id,
                    event_run_id=event_run_id,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"stream_runner: failed to persist usage records for {conversation_id}"
                )

        # Memory generation now happens inline at the compaction fold (above), so
        # there is no separate post-turn extraction trigger.

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

        # 6b. Post-turn compaction: auto-fold when enabled + over the auto band
        #     (emits "compaction_auto_folded"), else suggest the popup over the
        #     threshold ("compaction_suggested"). Runs inline on this conversation's
        #     worker, so it can't interleave with another turn. The deterministic
        #     floor in build_compacted_history guarantees safety regardless.
        if not errored and not cancelled:
            try:
                from app.agent import compaction
                evt = await compaction.after_turn_compaction(
                    cremind_agent,
                    conversation_id,
                    profile,
                    conversation_storage,
                    context_id=context_id,
                )
                if evt:
                    await bus.publish(conversation_id, evt["type"], evt["data"])
            except Exception:  # noqa: BLE001
                logger.debug("post-turn compaction check failed", exc_info=True)

        # 6c. Event-run status finalize. A clarifying question left the run
        #     'pending' (the agent called request_user_input and stopped);
        #     otherwise the turn completed / failed / was cancelled. Written
        #     before 'complete' so subscribers see a consistent status.
        run_row: dict | None = None
        if event_run_id:
            from app.events import run_state
            pending_q = run_state.get_pending(run_id)
            if cancelled:
                run_status = "cancelled"
            elif errored:
                run_status = "failed"
            elif pending_q:
                run_status = "pending"
            else:
                run_status = "completed"
            is_terminal = run_status != "pending"
            try:
                from app.storage import get_event_run_storage
                store = get_event_run_storage()
                run_row = await store.get(event_run_id)
                await store.update_status(
                    event_run_id,
                    status=run_status,
                    run_id=run_id,
                    pending_question=pending_q if run_status == "pending" else None,
                    error=(final_text if errored else None),
                    clear_pending=is_terminal,
                    increment_turn=True,
                    mark_finished=is_terminal,
                )
            except Exception:  # noqa: BLE001
                logger.exception(f"stream_runner: failed to finalize event run {event_run_id}")
            run_state.clear(run_id)
            try:
                from app.events.event_runs_admin_bus import publish_event_runs_changed
                publish_event_runs_changed(profile)
            except Exception:  # noqa: BLE001
                pass

        # 7. Terminal event so subscribers can flip isStreaming=false.
        await bus.publish(conversation_id, "complete", {
            "assistant_id": assistant_msg_id,
            "errored": errored,
            "cancelled": cancelled,
        })

        # 8. Optional notification. Event runs deep-link to the run detail
        #    (drawer) via event_run_id and use run-specific kinds; ordinary
        #    skill/schedule/file runs on chat conversations keep the plain kinds.
        if publish_notification:
            try:
                if event_run_id:
                    _push_event_run_notification(
                        profile=profile,
                        conversation_id=conversation_id,
                        title=title,
                        preview=_trim(final_text),
                        run_status=run_status,
                        event_run_id=event_run_id,
                        run_row=run_row,
                    )
                else:
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
        # Clear the plan-mode registry unconditionally (event runs clear
        # run_state above only for event runs; the plan registry must never leak
        # an entry per parked chat run).
        plan_state.clear(run_id)
        if ctx_token is not None:
            current_task_id_var.reset(ctx_token)
        await bus.end_run(conversation_id)


def make_run_id(conversation_id: str, kind: str = "msg") -> str:
    """Generate a unified run id. ``kind`` is purely informational (``msg`` or ``event``)."""
    return f"{kind}:{conversation_id}:{uuid.uuid4()}"


async def _compute_plan_phase(
    conversation_storage: Any, conversation_id: str, plan_action: str | None,
) -> str:
    """Determine the Plan-mode phase for this turn ("planning" | "execute").

    - An explicit ``accept`` decision → execute the approved plan.
    - The latest plan-bearing agent message tagged ``stage=="executing"`` with
      todos not all completed → execute (resume an in-progress / interrupted run).
    - Otherwise → planning. A fresh request, answered questions, a plan awaiting
      approval, or a resume-after-cancel all start in planning; the planning
      guidance itself routes an "implement the plan" message to execution (it has
      the update_todos tool available in the planning phase for exactly this).
    """
    if plan_action == "accept":
        return "execute"
    try:
        msgs = await conversation_storage.get_messages_after(
            conversation_id, -1, limit=50, newest_first=True,
        )
    except Exception:  # noqa: BLE001
        return "planning"
    for msg in reversed(msgs or []):
        if msg.get("role") != "agent":
            continue
        meta = msg.get("metadata")
        pm = meta.get("plan_mode") if isinstance(meta, dict) else None
        if not isinstance(pm, dict):
            continue
        if pm.get("stage") == "executing":
            todos = pm.get("todos") or []
            if not todos or not all(
                isinstance(t, dict) and t.get("status") == "completed" for t in todos
            ):
                return "execute"
        # The most recent plan-bearing agent message decides; stop scanning.
        return "planning"
    return "planning"


def _plan_metadata_for_persist(
    run_id: str, plan_phase: str | None, cancelled: bool,
) -> Optional[dict]:
    """Assemble the ``plan_mode`` metadata blob for the persisted assistant turn.

    Reads the per-run plan registry (:mod:`app.agent.plan_state`). On a cancelled
    turn we skip parked questions/plan (a half-run tool may have recorded them)
    but still persist the latest todo snapshot so an interrupted execution can
    resume from where it stopped.
    """
    from app.agent import plan_state
    questions = plan_state.get_questions(run_id)
    plan = plan_state.get_plan(run_id)
    todos = plan_state.get_todos(run_id)

    if not cancelled and questions:
        return {"stage": "awaiting_answers", "questions": questions.get("questions") or []}
    if not cancelled and plan:
        return {
            "stage": "awaiting_approval",
            "plan": {
                "path": plan.get("path"),
                "filename": plan.get("filename"),
                "title": plan.get("title"),
            },
        }
    if todos is not None:
        all_done = bool(todos) and all(
            isinstance(t, dict) and t.get("status") == "completed" for t in todos
        )
        stage = "completed" if (all_done and not cancelled) else "executing"
        return {"stage": stage, "todos": todos}
    return None


def _push_event_run_notification(
    *,
    profile: str,
    conversation_id: str,
    title: str,
    preview: str,
    run_status: str,
    event_run_id: str,
    run_row: dict | None,
) -> None:
    """Push a run-aware notification that deep-links to the Events run detail.

    ``pending`` is a high-priority prompt (the user must reply); ``failed`` is a
    normal-priority error; ``completed`` a normal completion. Cancelled runs are
    user-initiated, so they raise no notification.
    """
    if run_status == "cancelled":
        return
    kind_map = {
        "pending": "event_run_pending",
        "completed": "event_run_completed",
        "failed": "event_run_failed",
    }
    kind = kind_map.get(run_status)
    if kind is None:
        return
    extra = {
        "event_run_id": event_run_id,
        "source_kind": (run_row or {}).get("source_kind"),
        "subscription_id": (run_row or {}).get("subscription_id"),
    }
    if run_status == "pending":
        preview = (run_row or {}).get("pending_question") or preview
    get_event_notifications().push(
        profile=profile,
        conversation_id=conversation_id,
        conversation_title=title,
        message_preview=preview,
        kind=kind,
        priority="high" if run_status in ("pending", "failed") else "normal",
        extra=extra,
    )
