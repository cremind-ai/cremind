"""Generic "agent activity" feed for coding sub-agents (Claude Code, Codex, …).

A coding sub-agent (driven by a built-in tool such as ``claude_code``) runs as
a background task inside a Cremind conversation turn. While it works, its
reasoning/tool activity is streamed to the Web UI as a live, agent-agnostic
step feed rendered in the floating "Agent Activity" panel (a sibling of the
Plan-mode Todo panel). The steps are for the **user's eyes only** — they are
never fed back into Cremind's LLM context (Cremind sees only the tool's final
result string).

Design notes
------------
* **Direct bus publish, not the plan_state drain.** ``plan_state.push_emit`` is
  only drained between reasoning steps and only in plan / event-run mode, so
  activity emitted while the agent loop is blocked inside a long-poll tool call
  would arrive in bursts. This module publishes straight to the
  :class:`~app.events.stream_bus.ConversationStreamBus` from the sub-agent's own
  asyncio task (precedent: ``change_working_directory`` publishing ``cwd``), so
  steps reach the UI live even after the turn's ``end_run`` — the bus keeps the
  conversation→profile mapping past ``end_run``.
* **Full-snapshot semantics with a rolling step window.** Every publish carries
  the complete current state (idempotent, safe under SSE ring replay and client
  buffer truncation). The ``steps`` array is capped to the last
  :data:`_STEP_WINDOW` entries with a true ``total_steps`` count, bounding each
  event's size for long sessions.
* **Coalesced.** Intermediate mutations publish at most once per
  :data:`_COALESCE_SECONDS` (trailing edge). ``start`` and ``finish`` publish
  immediately so the panel appears/settles without delay.
* **Persistence.** The latest snapshot is stamped into the assistant message's
  ``message_metadata['agent_activity']`` by the stream runner at persist time so
  it survives page reload. When a task outlives the turn, :meth:`finish` patches
  the already-persisted message via ``update_message_metadata``.

The registry is keyed by ``conversation_id`` (one activity per conversation in
v1; a new :meth:`start` replaces a prior entry). Entries are evicted by a new
``start`` for the same conversation or an explicit :func:`clear`.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.utils.logger import logger

EVENT_TYPE = "agent_activity"

_COALESCE_SECONDS = 0.25
_STEP_WINDOW = 100
_TITLE_MAX = 140
_LABEL_MAX = 120
_DETAIL_MAX = 400

_TERMINAL_STATUSES = frozenset({"completed", "done", "failed", "error", "cancelled", "interrupted"})


def _truncate(text: Optional[str], limit: int) -> Optional[str]:
    if text is None:
        return None
    text = str(text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


@dataclass
class _Step:
    id: str
    ts: float
    kind: str
    label: str
    detail: Optional[str] = None
    status: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "kind": self.kind,
            "label": self.label,
            "detail": self.detail,
            "status": self.status,
        }


class AgentActivity:
    """Live activity handle for one coding sub-agent task.

    Obtained via :meth:`start`; the driving tool calls :meth:`add_step` /
    :meth:`resolve_step` as the sub-agent emits messages and :meth:`finish`
    when it terminates. All publishing is best-effort — a failure here must
    never break the sub-agent, so every publish is wrapped in try/except.
    """

    def __init__(
        self,
        *,
        conversation_id: str,
        profile: str,
        agent: str,
        task_id: str,
        title: str,
    ) -> None:
        self.conversation_id = conversation_id
        self.profile = profile
        self.agent = agent
        self.task_id = task_id
        self.title = _truncate(title, _TITLE_MAX) or ""
        self.status = "running"
        self.started_at = time.time()
        self.updated_at = self.started_at
        self._steps: List[_Step] = []
        self.total_steps = 0
        self.stats: Optional[Dict[str, Any]] = None
        self.usage: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None
        self._auto_seq = 0
        self._flush_task: Optional[asyncio.Task] = None
        self._dirty = False
        self.persist_message_id: Optional[str] = None
        self._patched = False

    # ── construction ────────────────────────────────────────────────────────
    @classmethod
    async def start(
        cls,
        *,
        context_id: str,
        profile: str,
        agent: str,
        task_id: str,
        title: str,
    ) -> "AgentActivity":
        """Create an activity, register it, and publish the first snapshot.

        ``context_id`` is the tool-injected reasoning-agent context id. For web
        conversations it equals the conversation_id; for channel/A2A ones it may
        differ, so we resolve it to the real conversation_id defensively.
        """
        conversation_id = await _resolve_conversation_id(profile, context_id)
        activity = cls(
            conversation_id=conversation_id,
            profile=profile,
            agent=agent,
            task_id=task_id,
            title=title,
        )
        _activities[conversation_id] = activity
        await activity._publish_now()
        return activity

    # ── mutation ──────────────────────────────────────────────────────────────
    async def add_step(
        self,
        *,
        kind: str,
        label: str,
        detail: Optional[str] = None,
        step_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> str:
        """Append a step (auto-id ``s{n}`` when ``step_id`` is None). Coalesced."""
        if step_id is None:
            self._auto_seq += 1
            step_id = f"s{self._auto_seq}"
        step = _Step(
            id=step_id,
            ts=time.time(),
            kind=kind,
            label=_truncate(label, _LABEL_MAX) or "",
            detail=_truncate(detail, _DETAIL_MAX),
            status=status,
        )
        self._steps.append(step)
        if len(self._steps) > _STEP_WINDOW:
            del self._steps[: len(self._steps) - _STEP_WINDOW]
        self.total_steps += 1
        self.updated_at = step.ts
        self._schedule_flush()
        return step_id

    async def resolve_step(
        self,
        step_id: str,
        *,
        status: str,
        detail_suffix: Optional[str] = None,
    ) -> None:
        """Mark a tool_use step ok/error (no-op if it scrolled out). Coalesced."""
        for step in reversed(self._steps):
            if step.id == step_id:
                step.status = status
                if detail_suffix:
                    combined = f"{step.detail}\n{detail_suffix}" if step.detail else detail_suffix
                    step.detail = _truncate(combined, _DETAIL_MAX)
                self.updated_at = time.time()
                self._schedule_flush()
                return

    async def update_usage(self, usage: Dict[str, Any]) -> None:
        """Replace the live context-usage payload. Coalesced like step mutations."""
        self.usage = usage
        self.updated_at = time.time()
        self._schedule_flush()

    async def finish(
        self,
        *,
        status: str,
        stats: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        """Terminal snapshot: publish immediately and patch the persisted message."""
        self.status = status
        self.stats = stats
        self.error = _truncate(error, _DETAIL_MAX)
        self.updated_at = time.time()
        self._cancel_flush()
        await self._publish_now()
        self._maybe_patch()

    # ── persistence hookup ───────────────────────────────────────────────────
    def set_persist_target(self, message_id: str) -> None:
        """Register the assistant message whose metadata holds the snapshot.

        Called by the stream runner once the turn's assistant message exists.
        If the task already finished (fast task within the turn), patch now;
        otherwise :meth:`finish` will patch when the task terminates. This
        two-sided check makes the ordering race-free.
        """
        self.persist_message_id = message_id
        self._maybe_patch()

    def _maybe_patch(self) -> None:
        if self._patched or not self.persist_message_id:
            return
        if self.status not in _TERMINAL_STATUSES:
            return
        self._patched = True
        try:
            asyncio.get_running_loop().create_task(self._patch_persisted())
        except RuntimeError:  # no running loop (shouldn't happen in practice)
            pass

    async def _patch_persisted(self) -> None:
        try:
            from app.events.runner import get_conversation_storage

            storage = get_conversation_storage()
            await storage.update_message_metadata(
                self.persist_message_id, {"agent_activity": self.snapshot()}
            )
        except Exception:  # noqa: BLE001 — persistence patch is best-effort
            logger.exception(
                "agent_activity: failed to patch persisted metadata for %s",
                self.conversation_id,
            )

    # ── snapshot + publish ────────────────────────────────────────────────────
    def snapshot(self) -> Dict[str, Any]:
        return {
            "agent": self.agent,
            "task_id": self.task_id,
            "status": self.status,
            "title": self.title,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "steps": [s.to_dict() for s in self._steps],
            "total_steps": self.total_steps,
            "stats": self.stats,
            "usage": self.usage,
            "error": self.error,
        }

    def _schedule_flush(self) -> None:
        self._dirty = True
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            self._flush_task = asyncio.get_running_loop().create_task(self._debounced_publish())
        except RuntimeError:
            self._flush_task = None

    def _cancel_flush(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
        self._flush_task = None

    async def _debounced_publish(self) -> None:
        try:
            await asyncio.sleep(_COALESCE_SECONDS)
        except asyncio.CancelledError:
            return
        if self._dirty:
            await self._publish_now()

    async def _publish_now(self) -> None:
        self._dirty = False
        try:
            from app.events.stream_bus import get_event_stream_bus

            await get_event_stream_bus().publish(
                self.conversation_id, EVENT_TYPE, self.snapshot()
            )
        except Exception:  # noqa: BLE001 — UI streaming is best-effort
            logger.exception(
                "agent_activity: failed to publish for %s", self.conversation_id
            )


# ── module-level registry ─────────────────────────────────────────────────────
_activities: Dict[str, AgentActivity] = {}


async def _resolve_conversation_id(profile: str, context_id: str) -> str:
    """Resolve a tool ``context_id`` to the bus's ``conversation_id`` key.

    Equal for web conversations (context_id back-filled to the conversation id);
    resolved via the conversation row for channel/A2A conversations. Falls back
    to ``context_id`` when resolution fails.
    """
    try:
        from app.events.runner import get_conversation_storage

        conv = await get_conversation_storage().get_conversation_by_context(
            profile, context_id
        )
        if conv and conv.get("id"):
            return conv["id"]
    except Exception:  # noqa: BLE001
        logger.debug(
            "agent_activity: conversation resolution failed for %s; using context_id",
            context_id,
        )
    return context_id


def get_snapshot(conversation_id: str) -> Optional[Dict[str, Any]]:
    activity = _activities.get(conversation_id)
    return activity.snapshot() if activity else None


def set_persist_target(conversation_id: str, message_id: str) -> None:
    activity = _activities.get(conversation_id)
    if activity is not None:
        activity.set_persist_target(message_id)


def clear(conversation_id: str) -> None:
    _activities.pop(conversation_id, None)
