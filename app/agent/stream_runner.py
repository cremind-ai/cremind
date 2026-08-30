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
import re
import uuid
from typing import Any, Dict, List, Optional

from a2a.types import DataPart, FilePart, Part

from app.agent.usage import reconcile
from app.constants import ChatCompletionTypeEnum
from app.events import task_result_inbox
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
        "system_file tools, understand an image's visual content with the "
        "image_understanding tool, or understand an audio clip's content with "
        "the audio_understanding tool. If the user asks to keep or save a file, use "
        "the system_file move_file tool to move it into their working directory "
        "(or copy_file to keep a copy).)"
    )
    note = "\n".join(lines)
    return f"{agent_query}\n\n{note}" if agent_query else note


def attachment_file_parts(
    attachments: Optional[List[Dict[str, Any]]],
) -> List[dict]:
    """File parts for composer-uploaded attachments on a persisted user message.

    They re-render as file chips on reload; the abs path lives under
    CREMIND_SYSTEM_DIR so the ``uri`` is servable via /api/files/open.
    """
    parts: List[dict] = []
    for a in (attachments or []):
        path = a.get("path")
        name = a.get("name") or os.path.basename(path or "")
        if not path:
            continue
        mime, _ = mimetypes.guess_type(name or path)
        parts.append({
            "kind": "file",
            "file": {
                "name": name,
                "mimeType": mime or "application/octet-stream",
                "uri": path,
            },
        })
    return parts


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


# The gdrive skill's structured error for a file it was never granted. It exits
# EXIT_NOT_GRANTED (3) and prints this JSON to stderr; see
# app/skills/builtin/gdrive/scripts/app/errors.py, which pins the marker, the
# ``file_id`` key and the exit code as a contract with this detector.
_DRIVE_NOT_GRANTED_MARKER = "drive_file_not_granted"
_DRIVE_FILE_ID_RE = re.compile(r'"file_id"\s*:\s*"([^"]*)"')


def _detect_drive_not_granted(serialized_parts: List[Dict[str, Any]]) -> Optional[str]:
    """An actionable error when a tool result carries gdrive's not-granted payload.

    Per-file Drive access means an automation can hit a file nobody granted, and
    the agent is told to notify-and-stop rather than open a consent URL no one is
    there to complete. That leaves the run *looking* successful, so the runner has
    to recognise the failure itself — an unattended run that quietly did nothing
    is the worst outcome available.

    The marker alone is not enough: an agent that merely reads gdrive's own source
    or docs would echo it. Requiring a non-zero exit code alongside keeps that from
    registering as a failure. Returns ``None`` when the observation is clean.

    The payload is ~1 KB, comfortably under the tool-result truncation limit, so
    the marker and file id survive intact.
    """
    for part in serialized_parts:
        if not isinstance(part, dict):
            continue
        data = part.get("data")
        if isinstance(data, dict):
            stderr = str(data.get("stderr") or "")
            if _DRIVE_NOT_GRANTED_MARKER in stderr and data.get("return_code") not in (0, None):
                match = _DRIVE_FILE_ID_RE.search(stderr)
                return _drive_not_granted_message(match.group(1) if match else "")
        text_body = part.get("text")
        if isinstance(text_body, str) and _DRIVE_NOT_GRANTED_MARKER in text_body:
            match = _DRIVE_FILE_ID_RE.search(text_body)
            return _drive_not_granted_message(match.group(1) if match else "")
    return None


def _drive_not_granted_message(file_id: str) -> str:
    target = f"Google Drive file '{file_id}'" if file_id else "a Google Drive file"
    flag = file_id or "<id>"
    return (
        f"This automation needed {target}, which has not been granted to Cremind "
        "(Cremind holds per-file Drive access). Grant it with "
        f"`cremind drive grant --file {flag}` or in Settings -> GSuite, then run "
        "the automation again."
    )


def _event_run_final_status(
    *,
    cancelled: bool,
    errored: bool,
    pending_question: Optional[str],
    todos: List[Dict[str, Any]],
    drive_not_granted_error: Optional[str],
) -> tuple[str, Optional[str]]:
    """Decide an event run's terminal status. Returns ``(status, pending_question)``.

    Precedence: cancelled -> errored -> pending -> not-granted -> completed.
    Pending outranks the not-granted failure deliberately: a pending run keeps a
    live continuation channel, so the user can grant the file and reply to resume,
    whereas failing it would clear that pending state and strand the work.
    """
    if cancelled:
        return "cancelled", pending_question
    if errored:
        return "failed", pending_question
    if pending_question:
        return "pending", pending_question
    done = sum(1 for t in todos if t.get("status") == "completed")
    if todos and done < len(todos):
        return "pending", (
            f"Run ended with {done} of {len(todos)} tasks completed "
            "— reply here to continue the remaining work."
        )
    if drive_not_granted_error:
        return "failed", pending_question
    return "completed", pending_question


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


def _is_channel_group_conversation(conv: Optional[dict]) -> bool:
    """Whether this conversation is a platform group's (Telegram, Zalo, …).

    An ordinary ``kind="chat"`` row bound to an ordinary channel, so the context
    id is the only thing that distinguishes it. Import kept local: the channels
    package pulls in the adapter registry, and the stream runner is imported by
    the slim CLI paths that must not.
    """
    try:
        from app.channels.groups.origin import is_channel_group_context

        return is_channel_group_context((conv or {}).get("context_id"))
    except Exception:  # noqa: BLE001
        return False


async def _resolve_message_origin(
    conversation_storage: Any,
    conv: Optional[dict],
    conversation_id: str,
    *,
    event_run: bool,
) -> Optional[dict]:
    """Describe where this conversation's user messages come from.

    Returns ``{"source": "web_ui"}``, a ``{"source": "channel", ...}`` dict
    carrying the channel and sender identity, or ``None`` when there is nothing
    useful to say. The agent renders it as a system-prompt section so a persona
    or standing instruction can key off WHO is talking.

    Derived from the CONVERSATION row rather than the turn's metadata on
    purpose. Every conversation has a channel (web chats bind to the profile's
    hidden ``main`` channel), so the answer is constant for the whole
    conversation — whereas per-turn metadata would flip to "web" the moment an
    operator typed into a channel sender's conversation from the web composer,
    fragmenting the cached system prefix and losing the sender's identity.

    Event runs get ``None``: their trigger is already described by
    ``EVENT_RUN_GUIDANCE`` plus the trigger message, and they form a disjoint
    prompt-cache population.
    """
    if event_run or not conv:
        return None
    try:
        # A seat in a group chat answers "who is talking?" differently: not one
        # sender on one channel, but a room with a roster, where every message
        # already says who wrote it. Resolved before the channel lookup because a
        # seat lives on the profile's ``main`` channel and would otherwise be
        # described as the Web UI.
        if conv.get("kind") == "group_chat":
            from app.groups.origin import resolve_group_origin

            return await resolve_group_origin(conversation_storage, conv)

        # A platform group's conversation IS an ordinary channel conversation —
        # same kind, same channel — so it has to be recognised before the
        # sender lookup below, which would find no sender and describe a room
        # full of people as a channel nobody is on the other end of.
        from app.channels.groups.origin import (
            is_channel_group_context,
            resolve_channel_group_origin,
        )

        if is_channel_group_context(conv.get("context_id")):
            return await resolve_channel_group_origin(conversation_storage, conv)

        channel_id = conv.get("channel_id")
        channel = (
            await conversation_storage.get_channel(channel_id) if channel_id else None
        )
        # No channel row, or the profile's hidden catch-all → the Web UI/CLI.
        if not channel or channel.get("channel_type") == "main":
            return {"source": "web_ui"}

        channel_type = channel.get("channel_type")
        channel_name = channel_type
        try:
            from app.config import load_channel_catalog
            catalog = load_channel_catalog(channel_type) or {}
            channel_name = (
                (catalog.get("channel") or {}).get("display_name") or channel_type
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                f"stream_runner: no catalog display name for {channel_type}",
                exc_info=True,
            )

        sender = await conversation_storage.get_sender_by_conversation(conversation_id)
        return {
            "source": "channel",
            "channel_id": channel_id,
            "channel_type": channel_type,
            "channel_name": channel_name,
            "sender_id": (sender or {}).get("sender_id"),
            "sender_display_name": (sender or {}).get("display_name"),
        }
    except Exception:  # noqa: BLE001
        # Never fail a run over prompt garnish — omit the section instead.
        logger.exception(
            f"stream_runner: failed to resolve message origin for {conversation_id}"
        )
        return None


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
    existing_user_message_id: str | None = None,
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
        # Carry an unfinished todo list from the previous turn into this turn's
        # registry so a "reply to continue" turn that never calls update_todos
        # still finalizes as 'pending' (not falsely 'completed'). A continuation
        # that finishes the work overwrites this via update_todos. No-op on the
        # first (trigger) turn — there is no prior plan-bearing message.
        await _seed_event_run_todos(conversation_storage, conversation_id, run_id)

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

    # This turn runs in a member's seat in a group chat, which changes what
    # happens at both ends of it: the room is told the agent started thinking,
    # and whatever it says at the end is posted there rather than just stored.
    is_group_chat = (conv or {}).get("kind") == "group_chat"
    # A platform group (Telegram/Zalo/…): an ordinary conversation row, so only
    # the context id says so. Shares the seat's compaction and outcome-stamping
    # rules, and nothing else — the two features are independent.
    is_channel_group = _is_channel_group_conversation(conv)

    message_origin = await _resolve_message_origin(
        conversation_storage, conv, conversation_id, event_run=event_run,
    )

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
        await hydrate_working_directory(
            conversation_id, conversation_storage, context_key=context_id,
        )
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
    # Where the visible flow was cut by a mid-turn message, as offsets into this
    # turn's text and thinking steps. Collected as the breaks happen rather than
    # on the terminal chunk: a cancelled turn never sends one, and its partial
    # answer is persisted too — the segments have to survive that.
    collected_mid_turn_breaks: list[dict] = []
    errored = False
    cancelled = False
    # Set when an unattended run touches a Drive file nobody granted. Kept apart
    # from ``errored``: the turn itself did not blow up, so chat-side behaviour is
    # unchanged — only the event-run row and its notification reflect the failure.
    drive_not_granted_error: str | None = None
    # The id of this turn's just-persisted user/trigger message, captured below.
    # Passed to compaction so the current turn (sent separately as the volatile
    # input) is excluded from the rebuilt history tail.
    current_turn_msg_id: Optional[str] = None
    # Handle for the group-seat frame mirror, bound below and released in the
    # finally. Initialised here so the teardown never reads it unbound when the
    # turn dies between the two.
    seat_mirror = None

    try:
        # 0. Claim this conversation as mid-turn for event-task delivery. A task
        #    result that lands from here until the ``finally`` parks in the
        #    conversation's inbox (a notice on the agent's next tool result)
        #    instead of queueing a turn behind this one, where it would stay
        #    invisible until this turn ended.
        #
        #    FIRST statement inside the try — and unbound FIRST in the finally —
        #    so a binding exists only across a stretch that is guaranteed to be
        #    torn down. (``bus.is_active`` cannot serve this: start_run sits
        #    outside the try, so a stale True is reachable, and under an
        #    is_active fork a stale flag would park every later result for this
        #    conversation with no turn-end flush to rescue it.)
        task_result_inbox.bind_run(run_id, conversation_id)

        # 0b. In a group, everyone can see who is composing. Published here so
        #     the indicator appears the moment the turn is claimed rather than
        #     when the first token arrives — a member that spends thirty seconds
        #     on tool calls before speaking would otherwise look asleep.
        if is_group_chat:
            from app.groups.hooks import bind_seat_mirror, publish_agent_status

            await publish_agent_status(conv=conv, profile=profile, state="thinking")
            # 0c. And mirror this seat's frames onto the room's stream, so the
            #     others watch the work rather than a spinner. Bound here, with
            #     the indicator, because everything published from now on is
            #     part of the turn the indicator is announcing.
            seat_mirror = await bind_seat_mirror(conv, profile)

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
            # message. The caller's own ``user_parts`` win if supplied.
            effective_user_parts = user_parts or (
                attachment_file_parts(attachments) or None
            )

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
        elif existing_user_message_id:
            # A message that was parked for a mid-turn injection and ended up
            # running as its own turn: the row (and its bus frame) already exist,
            # so nothing is persisted here — but it IS this turn's user message,
            # and compaction must leave it out of the history tail rather than
            # feed it alongside ``query``.
            current_turn_msg_id = existing_user_message_id
            # Clients mark the start of a turn by the ``user_message`` frame, and
            # this path publishes none — so a viewer joining mid-turn would
            # rebuild it starting from the PREVIOUS turn's marker. Say plainly
            # that a run is starting instead; there is no message to carry.
            await bus.publish(conversation_id, "run_started", {
                "message_id": existing_user_message_id,
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
                message_origin=message_origin,
                task_chain_depth=int((trigger_event or {}).get("task_chain_depth") or 0),
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
                        "token_usage": thinking_data.get("Token_Usage"),
                    })

                elif ctype == ChatCompletionTypeEnum.RESULT_ARTIFACT:
                    result_data = chunk.get("data", {}) or {}
                    call_id = result_data.get("Call_Id")
                    # ``Result`` is the new key; fall back to ``Observation``.
                    result_parts = (
                        result_data.get("Result") or result_data.get("Observation") or []
                    )
                    serialized_result = _serialize_observation(result_parts)

                    # Only unattended runs need this: an interactive turn can just
                    # show the agent's own explanation and let the user grant the
                    # file there and then. Keep the first hit — the earliest
                    # ungranted file is the one that derailed the run.
                    if event_run_id and drive_not_granted_error is None:
                        drive_not_granted_error = _detect_drive_not_granted(serialized_result)

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

                elif ctype == ChatCompletionTypeEnum.FLOW_BREAK:
                    # Something was folded into the running turn — a message the
                    # user sent, or an awaited event task's result. Live clients
                    # end the current assistant bubble here and open a fresh one;
                    # the offsets record the same cut against this turn's text
                    # and thinking steps so a reload rebuilds the identical
                    # layout from the single persisted row.
                    #
                    # Two arrive per interruption the agent replies to: one
                    # before the reply streams, one after, so the reply stands
                    # as its own message between the work either side of it.
                    # ``message_ids`` is empty on the second (and on a task
                    # result, which has no row of its own to interleave).
                    brk = dict(chunk.get("data") or {})
                    brk["content_offset"] = len("".join(final_text_parts))
                    brk["thinking_offset"] = len(collected_thinking_steps)
                    collected_mid_turn_breaks.append(brk)
                    await bus.publish(conversation_id, "flow_break", {
                        "message_ids": brk.get("message_ids") or [],
                        "step": brk.get("step"),
                    })
                    # In a seat, a break is where a reply to the interruption
                    # ends — so this is where the room hears it. Holding it to
                    # the end of the turn (step 6e) would delay it by exactly
                    # the work the sender was interrupting. Awaited inline, like
                    # 6e: the post has to reach the room in timeline order, and
                    # the turn is already paused around the reply anyway.
                    if is_group_chat:
                        try:
                            from app.groups.hooks import on_shadow_turn_segment

                            await on_shadow_turn_segment(
                                conversation_id=conversation_id,
                                profile=profile,
                                run_id=run_id,
                                raw_text="".join(final_text_parts),
                                mid_turn_breaks=collected_mid_turn_breaks,
                                context_id=(conv or {}).get("context_id"),
                            )
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                f"stream_runner: mid-turn group post failed for "
                                f"{conversation_id}"
                            )

                elif ctype == ChatCompletionTypeEnum.PLAN_EVENT:
                    # Plan-mode UI signal. ``data`` is {"event": <name>,
                    # "data": {...}}; publish it verbatim as its own bus event so
                    # the UI can render the question form / plan approval / todos.
                    evt = chunk.get("data") or {}
                    evt_name = evt.get("event")
                    if evt_name:
                        evt_data = dict(evt.get("data") or {})
                        # Event runs execute in a hidden per-firing conversation
                        # that the UI never opens directly, so their ``todos``
                        # frames would otherwise carry no way to tell an
                        # event-run panel apart from a normal chat one. Stamp the
                        # frame with the run id + a human title so the client can
                        # open an independent, self-describing floating panel per
                        # firing (rides the replay ring too, so late-connecting
                        # clients still identify an in-flight run). Chat plan-mode
                        # frames are untouched — the branch is gated on
                        # ``event_run_id``.
                        if event_run_id and evt_name == "todos":
                            evt_data["event_run_id"] = event_run_id
                            evt_data["run_title"] = title
                            evt_data["run_source"] = (
                                (user_message_metadata or {}).get("source")
                            )
                        await bus.publish(conversation_id, evt_name, evt_data)

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
        # Event runs also expose ``update_todos`` (a multi-step action can drive a
        # live todo panel), so persist their todo snapshot too. ``plan_phase`` is
        # None there, which yields only the ``{stage, todos}`` branch — exactly the
        # todo panel, no question/plan form (those tools aren't exposed on runs).
        if mode == "plan" or event_run:
            plan_meta = _plan_metadata_for_persist(run_id, plan_phase, cancelled)
            if plan_meta:
                agent_message_metadata = {
                    **(agent_message_metadata or {}),
                    "plan_mode": plan_meta,
                }

        # Agent activity (Claude Code and future coding sub-agents): stamp the
        # latest activity snapshot so a reload restores the floating panel.
        # Unconditional (not plan/event-gated) — the claude_code tool runs in
        # normal reasoning turns. When the sub-agent outlives the turn, its
        # AgentActivity.finish() later patches this message via
        # update_message_metadata (see set_persist_target below).
        try:
            from app.agent import agent_activity
            activity_snapshot = agent_activity.get_snapshot(conversation_id)
        except Exception:  # noqa: BLE001
            activity_snapshot = None
        if activity_snapshot:
            agent_message_metadata = {
                **(agent_message_metadata or {}),
                "agent_activity": activity_snapshot,
            }

        # Where mid-turn messages cut the visible flow. One persisted turn, but
        # the UI renders it as the same sequence of bubbles the user watched
        # arrive — without this a reload collapses the whole turn back into one.
        # The run id rides along because a seat that spoke mid-turn posted under
        # it, and the boot sweep needs it to recognise those posts as this
        # turn's rather than saying them all over again.
        if collected_mid_turn_breaks:
            agent_message_metadata = {
                **(agent_message_metadata or {}),
                "mid_turn_breaks": collected_mid_turn_breaks,
                "run_id": run_id,
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

        # 5. Commit the user messages this turn absorbed mid-flight. Gated on the
        #    trace: ``collected_llm_messages`` is only set by the agent's terminal
        #    DONE chunk, and it contains every drained message (they are appended
        #    to _turn_messages before the LLM call that carries them). So a trace
        #    on disk means the injection is durable and the row must not also
        #    replay as a plain user message — while a cancelled or errored turn
        #    persists no trace, leaves these uncommitted, and the turn-end flush
        #    below re-delivers them as a follow-up turn.
        if assistant_msg_id and collected_llm_messages:
            for parked in task_result_inbox.commit_user_messages(conversation_id):
                message_id = parked.get("message_id")
                if not message_id:
                    continue
                try:
                    await conversation_storage.update_message_metadata(
                        message_id,
                        {"mid_turn": {"state": "consumed", "run_id": run_id}},
                    )
                except Exception:  # noqa: BLE001
                    # The trace carries the content either way; the row is left
                    # 'pending' (invisible to history) until the boot sweep
                    # releases it. Loud, because it is a real inconsistency.
                    logger.exception(
                        f"stream_runner: failed to mark mid-turn message {message_id} "
                        f"consumed in {conversation_id}"
                    )

        # Register the persisted message as the activity's patch target. If the
        # sub-agent already finished (fast task within the turn) this is a no-op;
        # otherwise finish() patches it when the background task completes.
        if activity_snapshot and assistant_msg_id:
            try:
                from app.agent import agent_activity
                agent_activity.set_persist_target(conversation_id, assistant_msg_id)
            except Exception:  # noqa: BLE001
                pass

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
                    # Both kinds of room fold without asking. A Cremind seat is
                    # hidden from the sidebar; a platform group's conversation is
                    # visible but nobody is watching it — the people talking are
                    # in Zalo, not in this web view, so a "compact?" popup waits
                    # for a click that never comes while the group runs on the
                    # deterministic floor, which DROPS old turns instead of
                    # summarising them.
                    force_auto=is_group_chat or is_channel_group,
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
        task_delivery: str | None = None
        if event_run_id:
            from app.events import run_state
            # A run only completes when its todo list (if it drove one) is fully
            # done. An incomplete list leaves the run 'pending' so it stays
            # non-terminal (survives restarts, never auto-pruned) and can be
            # resumed by replying in the run's chat — the same continuation path
            # as a request_user_input pause.
            run_status, pending_q = _event_run_final_status(
                cancelled=cancelled,
                errored=errored,
                pending_question=run_state.get_pending(run_id),
                todos=plan_state.get_todos(run_id) or [],
                drive_not_granted_error=drive_not_granted_error,
            )
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
                    error=(final_text if errored else drive_not_granted_error),
                    clear_pending=is_terminal,
                    increment_turn=True,
                    mark_finished=is_terminal,
                )
                # ``run_row`` was read before the write above, so its
                # pending_question is stale; sync it so the pending notification
                # below shows this turn's actual prompt (the incomplete-todos
                # message or the request_user_input question).
                if run_row is not None:
                    run_row["pending_question"] = (
                        pending_q if run_status == "pending" else None
                    )
            except Exception:  # noqa: BLE001
                logger.exception(f"stream_runner: failed to finalize event run {event_run_id}")
            run_state.clear(run_id)
            try:
                from app.events.event_runs_admin_bus import publish_event_runs_changed
                publish_event_runs_changed(profile)
            except Exception:  # noqa: BLE001
                pass

            # 6d. Event TASK delivery. When this run belongs to a one-shot task,
            #     its result goes back to the conversation that registered it so
            #     the agent there continues the flow. Hooked here rather than in
            #     the dispatcher because a task run that parked as 'pending' and
            #     was answered later terminates from a different call stack —
            #     this is the one place every terminal path passes through.
            #     A no-op (one cheap read) for ordinary event runs.
            if is_terminal:
                try:
                    from app.events.event_task_delivery import on_run_terminal
                    task_delivery = await on_run_terminal(
                        event_run_id=event_run_id,
                        profile=profile,
                        status=run_status,
                        final_text=final_text,
                        error=(final_text if errored else drive_not_granted_error),
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        f"stream_runner: event task delivery failed for {event_run_id}"
                    )

        # 6e. Group chat: in a member's seat the final answer IS that agent's
        #     post, so this is where it reaches the room. After the message is
        #     persisted (the post is derived from it, and its metadata records
        #     the outcome so a crash cannot double-post) and before the terminal
        #     frame, so a client that reacts to 'complete' already sees the post.
        #     An answer of exactly "[silent]" posts nothing: in a room where
        #     everyone is asked, most members have nothing to add.
        if is_group_chat:
            try:
                from app.groups.hooks import on_shadow_turn_complete

                await on_shadow_turn_complete(
                    conversation_storage=conversation_storage,
                    conversation_id=conversation_id,
                    profile=profile,
                    run_id=run_id,
                    assistant_msg_id=assistant_msg_id,
                    raw_text="".join(final_text_parts),
                    final_text=final_text,
                    mid_turn_breaks=collected_mid_turn_breaks,
                    cancelled=cancelled,
                    errored=errored,
                    context_id=(conv or {}).get("context_id"),
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"stream_runner: group chat post failed for {conversation_id}"
                )

        # 6f. Platform group: record what this turn did with its answer, on the
        #     agent's own row. The stamp is what keeps a silent turn out of the
        #     model's replayed history and out of the relevance judge's
        #     transcript — so it has to be written HERE, before 'complete',
        #     rather than by the channel forwarder that posts the answer. The
        #     forwarder runs concurrently with whatever the group says next: a
        #     stamp landing late means turn N+1 replays the "[silent]" row and
        #     turn N+2 does not, which is a deletion in the middle of the
        #     history and costs the prompt cache every time it happens.
        if is_channel_group and assistant_msg_id:
            try:
                from app.channels.groups.origin import group_id_from_context
                from app.groups.render import strip_silent_lines

                # The same function the forwarder posts by, so "what was said"
                # and "what the history says was said" cannot disagree.
                spoken = strip_silent_lines("".join(final_text_parts))
                await conversation_storage.update_message_metadata(
                    assistant_msg_id,
                    {"channel_group": {
                        "group_id": group_id_from_context(
                            (conv or {}).get("context_id"),
                        ),
                        "kind": "sent" if spoken else "silent",
                    }},
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"stream_runner: could not stamp the group outcome for "
                    f"{conversation_id}"
                )

        # 7. Terminal event so subscribers can flip isStreaming=false — unless a
        #    mid-turn message is about to run as a follow-up turn (the flush in
        #    the finally below), in which case clients keep their streaming state
        #    up rather than flickering idle for the length of one enqueue.
        await bus.publish(conversation_id, "complete", {
            "assistant_id": assistant_msg_id,
            "errored": errored,
            "cancelled": cancelled,
            "followup_queued": task_result_inbox.has_unconsumed_user_messages(
                conversation_id
            ),
        })

        # 8. Optional notification. Event runs deep-link to the run detail
        #    (drawer) via event_run_id and use run-specific kinds; ordinary
        #    skill/schedule/file runs on chat conversations keep the plain kinds.
        #    An event TASK whose result was just delivered raises none: the
        #    origin conversation is about to run its own turn and notify from
        #    there, and two notifications for one outcome read as a duplicate.
        if publish_notification:
            try:
                from app.events.event_task_delivery import SUPPRESSES_RUN_NOTIFICATION
                if task_delivery in SUPPRESSES_RUN_NOTIFICATION:
                    pass
                elif event_run_id:
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
        # Release the event-task binding FIRST: from here on this conversation
        # is idle, so a task result landing mid-teardown delivers itself rather
        # than parking with nobody left to read it.
        task_result_inbox.unbind_run(run_id)
        _running_runs.pop(run_id, None)
        # Drop the room's "thinking" indicator for this member, whatever ended
        # the turn — a crashed turn that left one lit would read as an agent
        # stuck composing forever.
        if is_group_chat:
            try:
                from app.groups.hooks import publish_agent_status, unbind_seat_mirror

                # Detach BEFORE announcing idle, and in that order: the tap
                # would otherwise still be live while ``publish_agent_status``
                # waits on its room lookup, and anything published in that gap
                # (a cwd write from the files API, a racing publisher) arrives
                # after "idle" and re-creates the live card the status just
                # cleared. A tap left attached for longer than that would also
                # mirror the NEXT turn's steps, under a run the room already
                # watched finish.
                await unbind_seat_mirror(conversation_id, seat_mirror)
                await publish_agent_status(
                    conv=conv, profile=profile, state="idle",
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "stream_runner: group idle status failed", exc_info=True,
                )
        # Clear the plan-mode registry unconditionally (event runs clear
        # run_state above only for event runs; the plan registry must never leak
        # an entry per parked chat run).
        plan_state.clear(run_id)
        if ctx_token is not None:
            current_task_id_var.reset(ctx_token)
        await bus.end_run(conversation_id)

        # Turn-end reconciliation, part 1: user messages. Anything the user sent
        # mid-turn that this turn did not absorb — it arrived during the final
        # step, or the turn was cancelled or errored before its trace persisted —
        # runs NOW as one coalesced follow-up turn. The injection is the
        # optimisation; this is the guarantee that a sent message is always
        # answered. Runs for event-run conversations too (a reply to a running
        # event run is exactly this case), and BEFORE the task-result flush so
        # the person waiting on an answer is served first.
        if task_result_inbox.has_unconsumed_user_messages(conversation_id):
            try:
                from app.events.user_message_delivery import flush_user_inbox
                await flush_user_inbox(
                    conversation_id=conversation_id, profile=profile,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "stream_runner: turn-end user-message flush failed for "
                    f"{conversation_id}"
                )

        # Turn-end reconciliation, part 2: any task result that landed while this
        # turn was running and the agent chose not to read is injected NOW, as one
        # coalesced continuation turn — so a waiting flow can never stall on the
        # agent ignoring a notice, and the notice stays a safe optimisation.
        #
        # In the ``finally`` (not after step 8) so an errored or cancelled turn
        # still reconciles; a flush interrupted here simply leaves the marker
        # set and the next turn on this conversation retries. The in-memory
        # ``has_pending`` gate keeps a DB query off every ordinary turn end.
        if not event_run and task_result_inbox.has_pending(conversation_id):
            try:
                from app.events.event_task_delivery import flush_origin_inbox
                await flush_origin_inbox(
                    conversation_id=conversation_id, profile=profile,
                    reason="turn_end",
                )
                task_result_inbox.reset(conversation_id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "stream_runner: turn-end task-result flush failed for "
                    f"{conversation_id}"
                )


def make_run_id(conversation_id: str, kind: str = "msg") -> str:
    """Generate a unified run id. ``kind`` is purely informational (``msg`` or ``event``)."""
    return f"{kind}:{conversation_id}:{uuid.uuid4()}"


async def _seed_event_run_todos(
    conversation_storage: Any, conversation_id: str, run_id: str,
) -> None:
    """Seed the plan registry from the last persisted *incomplete* todo snapshot.

    An event run can span multiple turns (left 'pending' with unfinished todos,
    then resumed by a user reply). The per-run registry is cleared each turn, so
    without this a continuation turn that doesn't call ``update_todos`` would look
    like an empty list and finalize as 'completed'. Mirrors ``_compute_plan_phase``'s
    newest-first scan; no-op on the first (trigger) turn and when the last snapshot
    is already fully completed.
    """
    from app.agent import plan_state
    try:
        msgs = await conversation_storage.get_messages_after(
            conversation_id, -1, limit=50, newest_first=True,
        )
    except Exception:  # noqa: BLE001
        return
    for msg in reversed(msgs or []):
        if msg.get("role") != "agent":
            continue
        meta = msg.get("metadata")
        pm = meta.get("plan_mode") if isinstance(meta, dict) else None
        if not isinstance(pm, dict):
            continue
        todos = pm.get("todos") or []
        if todos and not all(
            isinstance(t, dict) and t.get("status") == "completed" for t in todos
        ):
            plan_state.set_todos(run_id, todos)
        # The most recent plan-bearing agent message decides; stop scanning.
        return


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
