"""The live "Research activity" panel of a conversation.

A research job runs for minutes, in its own task, long after the tool call
that started it may have returned to the agent with a PRELIMINARY status. The
user watches it here: the question, the phase ("Reading files"), a progress
bar, the steps as the pipeline takes them ("Reading ABC/HopDong.pdf (3/6)"),
and the tokens spent against the job's budget — with a Cancel button.

Modelled on :mod:`app.agent.agent_activity` (the Claude Code / Codex feed),
for the same reasons:

* **Published from the job's own task**, straight to the conversation's
  stream bus, so progress reaches the UI live whether or not a turn is
  running (the bus keeps the conversation→profile mapping past a turn).
* **Full snapshots with a rolling step window**: every publish carries the
  whole state (idempotent under SSE replay), with only the last
  :data:`_STEP_WINDOW` steps and the true ``total_steps``.
* **Coalesced**: step and progress changes publish at most once per
  :data:`_COALESCE_SECONDS`; ``start`` and ``finish`` publish at once.
* **Persisted**: the stream runner stamps the latest snapshot into the turn's
  assistant message (``message_metadata['research_activity']``) and names
  that message as the persist target; when the job settles after the turn,
  :meth:`ResearchActivity.finish` patches the saved message, so a reload
  shows how the job ended.

The pipelines never see this module: they report through
:class:`ActivityProgressSink`, a :class:`~.context.ProgressSink` that also
keeps the in-memory record the job's status view (``JobView.progress``)
reads. One activity per conversation — the latest job; a job resumed with
``continue_job`` picks its own activity back up, so the panel keeps its
earlier steps.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.documents.research.context import ProgressSink
from app.utils.logger import logger

EVENT_TYPE = "research_activity"

_COALESCE_SECONDS = 0.25
_STEP_WINDOW = 80
_TITLE_MAX = 200
_LABEL_MAX = 160
_DETAIL_MAX = 400
_SUMMARY_MAX = 600

# Statuses in which the job is still working; any other status is settled
# (final, or waiting for the user) and is what gets patched into the saved
# message. Mirrors research.types.ACTIVE without importing the job layer.
_WORKING = frozenset({"queued", "planning", "running"})


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
        return {"id": self.id, "ts": self.ts, "kind": self.kind, "label": self.label,
                "detail": self.detail, "status": self.status}


class ResearchActivity:
    """Live activity handle for one conversation's research job.

    Obtained with :meth:`start`. Mutations are synchronous (the pipelines
    report from plain function calls) and schedule a coalesced publish on the
    loop the activity was started on — also when called from a worker
    thread. Everything is best-effort: a failed publish never fails the job.
    """

    def __init__(self, *, conversation_id: str, profile: str, job_id: str, title: str, mode: str) -> None:
        self.conversation_id = conversation_id
        self.profile = profile
        self.job_id = job_id
        self.title = _truncate(title, _TITLE_MAX) or ""
        self.mode = mode
        self.status = "running"
        self.phase: Optional[str] = None
        self.started_at = time.time()
        self.updated_at = self.started_at
        self.done = 0
        self.total = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.budget = 0
        self.summary: Optional[str] = None
        self.error: Optional[str] = None
        # Why a settled job ended as it did (the dossier's outcome: reason,
        # detail, counts), so the panel can tell "stopped early" from
        # "insufficient evidence".
        self.outcome: Optional[Dict[str, Any]] = None
        self._steps: List[_Step] = []
        self.total_steps = 0
        self._seq = 0
        self._dirty = False
        self._flush_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[int] = None
        self.persist_message_id: Optional[str] = None
        self._patched = False

    # ── construction ──────────────────────────────────────────────────────

    @classmethod
    async def start(cls, *, conversation_id: str, profile: str, job_id: str, title: str,
                    mode: str) -> "ResearchActivity":
        """Register the activity for ``conversation_id`` and publish it.

        The same job starting again (``continue_job`` after a question, a
        resume after a restart) revives its existing activity: its steps stay,
        and it waits for the new turn's message before patching anything, so
        the earlier turn keeps the snapshot of how it paused."""
        existing = _activities.get(conversation_id)
        if existing is not None and existing.job_id == job_id and existing.profile == profile:
            activity = existing
            activity.status = "running"
            activity.summary = None
            activity.error = None
            activity.outcome = None
            activity.persist_message_id = None
            activity._patched = False
            activity.updated_at = time.time()
        else:
            if existing is not None:
                existing._cancel_flush()
            activity = cls(conversation_id=conversation_id, profile=profile, job_id=job_id, title=title,
                           mode=mode)
            _activities[conversation_id] = activity
        activity._bind_loop()
        await activity._publish_now()
        return activity

    def _bind_loop(self) -> None:
        try:
            self._loop = asyncio.get_running_loop()
            self._loop_thread = threading.get_ident()
        except RuntimeError:
            self._loop = None
            self._loop_thread = None

    # ── mutation ──────────────────────────────────────────────────────────

    def add_step(self, *, kind: str, label: str, detail: str | None = None, status: str = "running") -> str:
        """Append a step; returns its id (``s<n>``, unique for this activity
        even across resumes)."""
        self._seq += 1
        step = _Step(id=f"s{self._seq}", ts=time.time(), kind=kind or "step",
                     label=_truncate(label, _LABEL_MAX) or "", detail=_truncate(detail, _DETAIL_MAX),
                     status=status)
        self._steps.append(step)
        if len(self._steps) > _STEP_WINDOW:
            del self._steps[: len(self._steps) - _STEP_WINDOW]
        self.total_steps += 1
        self.updated_at = step.ts
        self._schedule_flush()
        return step.id

    def resolve_step(self, step_id: str, *, status: str, detail_suffix: str | None = None) -> None:
        """Settle a step (a no-op once it has scrolled out of the window)."""
        for step in reversed(self._steps):
            if step.id == step_id:
                step.status = status
                if detail_suffix:
                    step.detail = _truncate(f"{step.detail or ''}{detail_suffix}", _DETAIL_MAX)
                self.updated_at = time.time()
                self._schedule_flush()
                return

    def set_phase(self, phase: str) -> None:
        self.phase = _truncate(phase, _LABEL_MAX)
        self.updated_at = time.time()
        self._schedule_flush()

    def set_progress(self, done: int, total: int) -> None:
        self.done, self.total = max(0, int(done or 0)), max(0, int(total or 0))
        self.updated_at = time.time()
        self._schedule_flush()

    def update_usage(self, tokens_in: int, tokens_out: int, budget: int) -> None:
        self.tokens_in, self.tokens_out, self.budget = int(tokens_in or 0), int(tokens_out or 0), int(budget or 0)
        self.updated_at = time.time()
        self._schedule_flush()

    async def finish(self, *, status: str, summary: str | None = None, error: str | None = None,
                     outcome: Optional[Dict[str, Any]] = None) -> None:
        """The run ended (final, or stopped to ask): publish now and patch
        the saved message when one is known. ``outcome`` is the dossier's
        (counts and the reason; its diagnostic trace is left out)."""
        self.status = status
        self.summary = _truncate(summary, _SUMMARY_MAX)
        self.error = _truncate(error, _DETAIL_MAX)
        self.outcome = ({k: v for k, v in outcome.items() if k != "trace"} if isinstance(outcome, dict)
                        else None)
        self.updated_at = time.time()
        # Steps left "running" belong to a run that is over.
        if status not in _WORKING:
            for step in self._steps:
                if step.status == "running":
                    step.status = "failed" if status in ("failed",) else "stopped"
        self._cancel_flush()
        await self._publish_now()
        self._maybe_patch()

    # ── persistence ───────────────────────────────────────────────────────

    def set_persist_target(self, message_id: str) -> None:
        """The assistant message whose metadata carries the snapshot (set by
        the stream runner once the turn's message exists). Two-sided with
        :meth:`finish`, so either order ends with the message patched."""
        self.persist_message_id = message_id
        self._maybe_patch()

    def _maybe_patch(self) -> None:
        if self._patched or not self.persist_message_id or self.status in _WORKING:
            return
        self._patched = True
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            running.create_task(self._patch_persisted())
        elif loop is not None and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._patch_persisted(), loop)

    async def _patch_persisted(self) -> None:
        try:
            from app.events.runner import get_conversation_storage

            await get_conversation_storage().update_message_metadata(
                self.persist_message_id, {"research_activity": self.snapshot()})
        except Exception:  # noqa: BLE001 — the patch is best-effort
            logger.exception(f"[documents] research activity: could not patch the saved message of "
                             f"{self.conversation_id}")

    # ── snapshot and publishing ───────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "conversation_id": self.conversation_id,
            "status": self.status,
            "title": self.title,
            "mode": self.mode,
            "phase": self.phase,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "progress": {"done": self.done, "total": self.total},
            "steps": [s.to_dict() for s in self._steps],
            "total_steps": self.total_steps,
            "usage": {"tokens_in": self.tokens_in, "tokens_out": self.tokens_out, "budget": self.budget},
            "summary": self.summary,
            "error": self.error,
            "outcome": self.outcome,
        }

    def _schedule_flush(self) -> None:
        self._dirty = True
        loop = self._loop
        if loop is not None and self._loop_thread is not None and threading.get_ident() != self._loop_thread:
            # Reported from a worker thread (a pipeline step inside ctx.io):
            # hop onto the job's loop to schedule the publish there.
            if not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(self._schedule_flush_on_loop)
                except RuntimeError:
                    pass
            return
        self._schedule_flush_on_loop()

    def _schedule_flush_on_loop(self) -> None:
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
        if self._dirty and _activities.get(self.conversation_id) is self:
            await self._publish_now()

    async def _publish_now(self) -> None:
        self._dirty = False
        try:
            from app.events.stream_bus import get_event_stream_bus

            await get_event_stream_bus().publish(self.conversation_id, EVENT_TYPE, self.snapshot())
        except Exception:  # noqa: BLE001 — UI streaming is best-effort
            logger.exception(f"[documents] research activity: could not publish for {self.conversation_id}")


class ActivityProgressSink(ProgressSink):
    """A :class:`ProgressSink` that also drives a :class:`ResearchActivity`.

    Every call keeps :class:`ProgressSink`'s own record (it feeds
    ``JobView.progress`` for the tool, REST and CLI) and is forwarded to the
    activity. Step ids are the sink's own; the activity's ids are mapped
    behind them, because a revived activity keeps numbering where it was
    while a new run's sink starts again at ``s1``."""

    def __init__(self, activity: ResearchActivity | None) -> None:
        super().__init__()
        self.activity = activity
        self._ids: dict[str, str] = {}

    def set_phase(self, name: str) -> None:
        super().set_phase(name)
        if self.activity is not None:
            self.activity.set_phase(name)

    def add_step(self, *, kind: str, label: str, detail: str | None = None, status: str = "running") -> str:
        sid = super().add_step(kind=kind, label=label, detail=detail, status=status)
        if self.activity is not None:
            self._ids[sid] = self.activity.add_step(kind=kind, label=label, detail=detail, status=status)
        return sid

    def resolve_step(self, step_id: str, *, status: str, detail_suffix: str | None = None) -> None:
        super().resolve_step(step_id, status=status, detail_suffix=detail_suffix)
        if self.activity is not None and step_id in self._ids:
            self.activity.resolve_step(self._ids[step_id], status=status, detail_suffix=detail_suffix)

    def set_progress(self, done: int, total: int) -> None:
        super().set_progress(done, total)
        if self.activity is not None:
            self.activity.set_progress(done, total)

    def update_usage(self, tokens_in: int, tokens_out: int, budget: int) -> None:
        """Not part of :class:`ProgressSink`: the runner reports spend here
        from the budgeted LLM's usage callback."""
        if self.activity is not None:
            self.activity.update_usage(tokens_in, tokens_out, budget)


# ── the registry ───────────────────────────────────────────────────────────

_activities: Dict[str, ResearchActivity] = {}


def get_snapshot(conversation_id: str) -> Optional[Dict[str, Any]]:
    activity = _activities.get(conversation_id)
    return activity.snapshot() if activity is not None else None


def get_activity(conversation_id: str) -> Optional[ResearchActivity]:
    return _activities.get(conversation_id)


def set_persist_target(conversation_id: str, message_id: str) -> None:
    activity = _activities.get(conversation_id)
    if activity is not None:
        activity.set_persist_target(message_id)


def clear(conversation_id: str) -> None:
    """Forget the conversation's activity (the conversation was deleted)."""
    activity = _activities.pop(conversation_id, None)
    if activity is not None:
        activity._cancel_flush()


__all__ = ["ActivityProgressSink", "EVENT_TYPE", "ResearchActivity", "clear", "get_activity", "get_snapshot",
           "set_persist_target"]
