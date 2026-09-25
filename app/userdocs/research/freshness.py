"""Research reads the index, so it first brings the index up to date.

The pipelines read documents as the index holds them: the text extracted from
each file, passage by passage, never the file itself. That text is what the
file said when it was last indexed. With a native watcher the index trails
the folder by seconds; in polling mode (Docker Desktop, network drives, WSL's
Windows drives) it can trail by up to 15 minutes, and a folder created in the
meantime is not in the index at all. A verified answer built on yesterday's
draft is still wrong, so before a job reads anything:

1. **Discovery** (:func:`settle_discovery`, before the scope is resolved).
   Where only a scan finds new files, one is requested and waited for. With a
   native watcher, the changes it has seen but not handed over yet are waited
   for (seconds).
2. **Refresh** (:func:`refresh_files`, once the scope is known). Every
   in-scope local file is compared with the disk (size and modification time,
   ``stat`` only). A changed or vanished file is queued ahead of everything
   else, and one already queued is moved up. The job then waits while those
   files are being indexed, up to a limit, and checks them once more: a file
   saved again while it was being indexed goes round again.

A file that is still not indexed when the wait ends is never read from its
old text. It was queued, so when the caller resolves the scope again it shows
up in the coverage table as "not indexed yet", and the job asks the user
whether to go on without it (or to continue the job later). When sync is not
running at all (paused, on hold, suspended) nothing is waited for, and the
notes say why.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.userdocs.research.context import ResearchContext

POLL_S = 0.5
# A full scan in polling mode (it walks the whole folder).
SCAN_WAIT_S = 180.0
# Changes the native watcher is still settling (a file being written).
WATCH_SETTLE_S = 10.0
# Queued files being indexed: the overall cap, and how long the queue may go
# without finishing or working on any of them before the job stops waiting.
INDEX_WAIT_S = 600.0
STALL_S = 90.0
# The refresh checks the waited-for files this many times in all.
ROUNDS = 2
# Never wait into the time the job needs for reading.
RESERVE_S = 120.0

_BLOCKED = {
    "paused": "sync is paused",
    "hold": "sync is on hold",
    "suspended": "sync is suspended",
    "disabled": "sync is off",
    "awaiting_confirmation": "sync is waiting for a confirmation",
}


@dataclass
class Refreshed:
    # Files in scope were queued: the scope must be resolved again, so their
    # rows (new status, new text, or gone) are the ones read.
    changed: bool = False
    notes: list[str] = field(default_factory=list)


def _runtime(ctx: ResearchContext) -> Any:
    return getattr(ctx.engine, "runtime", None)


def _blocked(state: tuple[str, str | None]) -> str:
    what = _BLOCKED.get(state[0], "sync is not running")
    return f"{what} ({state[1]})" if state[1] else what


async def _wait(ctx: ResearchContext, done: Callable[[], bool], limit: float) -> bool:
    """Poll ``done`` (a blocking check, run off the loop) until it is true or
    ``limit`` passes. A cancelled or timed-out job stops here too."""
    end = time.monotonic() + max(0.0, min(limit, ctx.seconds_left - RESERVE_S))
    while True:
        if await ctx.io(done):
            return True
        if time.monotonic() >= end:
            return False
        await asyncio.sleep(POLL_S)


async def settle_discovery(ctx: ResearchContext) -> list[str]:
    """Make sure the index knows about every file in the folder, new ones
    included; returns notes for the dossier (empty when all went well)."""
    rt = _runtime(ctx)
    if rt is None:
        return []
    if await ctx.io(rt.finds_new_files_by_scan):
        blocker = await ctx.io(rt.scan_blocker)
        if blocker is not None:
            return [f"The folder was not checked for changes because {_blocked(blocker)}: files added or "
                    "edited since the last sync may be missing or outdated."]
        sid = ctx.step("Checking the folder for new and changed files")
        ticket = await ctx.io(rt.scan_ticket, "research")
        await _wait(ctx, lambda: rt.scan_done(ticket) or rt.scan_blocker() is not None, SCAN_WAIT_S)
        if await ctx.io(rt.scan_done, ticket):
            ctx.done_step(sid)
            return []
        blocker = await ctx.io(rt.scan_blocker)
        ctx.done_step(sid, ok=False, suffix=" — stopped" if blocker else " — did not finish in time")
        if blocker is not None:
            return [f"The folder check stopped because {_blocked(blocker)}: files added or edited since "
                    "the last sync may be missing or outdated."]
        return ["The folder check did not finish in time: files added in the last few minutes may be missing."]
    if not await ctx.io(rt.watch_settled) and not await _wait(ctx, rt.watch_settled, WATCH_SETTLE_S):
        return ["Some changes the folder watcher had just seen were still settling (a file being written?); "
                "they may not be included."]
    return []


def _files(n: int) -> tuple[str, str, str, str]:
    """``("1 file", "it", "was", "is")`` or ``("3 files", "they", "were", "are")``."""
    return (f"{n} file", "it", "was", "is") if n == 1 else (f"{n} files", "they", "were", "are")


async def _wait_indexed(ctx: ResearchContext, rt: Any, ids: list[int]) -> list[int]:
    """Wait while ``ids`` are being indexed; return the ones still queued
    when the wait ends (all done, over the limit, no progress for STALL_S,
    or sync stopped)."""
    db = ctx.engine.db
    total = len(ids)
    pending = list(ids)
    now = time.monotonic()
    end = now + max(0.0, min(INDEX_WAIT_S, ctx.seconds_left - RESERVE_S))
    last_move = now
    while True:
        rows = await ctx.io(db.files_by_ids, pending)
        left = [i for i in pending if (rows.get(i) or {}).get("status") == "dirty"]
        ctx.progress(total - len(left), total)
        if not left:
            return []
        now = time.monotonic()
        if len(left) < len(pending) or await ctx.io(rt.indexing_any, left):
            last_move = now
        pending = left
        if now >= end or now - last_move >= STALL_S or await ctx.io(rt.sync_blocker) is not None:
            return pending
        await asyncio.sleep(POLL_S)


async def refresh_files(ctx: ResearchContext, rows: list[dict[str, Any]]) -> Refreshed:
    """Re-index the files among ``rows`` (the scope) that changed since they
    were indexed, and wait for them. See the module docstring."""
    rt = _runtime(ctx)
    out = Refreshed()
    if rt is None or not rows:
        return out
    for _ in range(ROUNDS):
        changed, queued = await ctx.io(rt.queue_if_changed, rows)
        ids = list(dict.fromkeys(changed + queued))
        if not ids:
            break
        out.changed = True
        blocker = await ctx.io(rt.sync_blocker)
        if blocker is not None:
            n, it, was, is_ = _files(len(ids))
            out.notes.append(f"{n} in scope changed since {it} {was} last indexed, but {_blocked(blocker)}, "
                             f"so {it} {is_} listed as not indexed yet.")
            break
        sid = ctx.step(f"Re-indexing {_files(len(ids))[0]} changed since the last sync")
        left = await _wait_indexed(ctx, rt, ids)
        if left:
            ctx.done_step(sid, ok=False, suffix=f" — {len(left)} not finished")
            n, it, was, is_ = _files(len(left))
            out.notes.append(f"{n} in scope changed since {it} {was} last indexed and {was} not re-indexed in "
                             f"time, so {it} {is_} listed as not indexed yet.")
            break
        ctx.done_step(sid)
        # Once more over what was re-indexed: a file saved again while it was
        # being indexed was indexed at its older content.
        rows = list((await ctx.io(ctx.engine.db.files_by_ids, ids)).values())
    return out


__all__ = ["Refreshed", "refresh_files", "settle_discovery"]
