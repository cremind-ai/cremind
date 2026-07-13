"""Event-runs endpoints — `/api/event-runs*`.

Thin async wrappers over the Event Runs API: the per-trigger execution history
each fired event rule (skill event / file watcher / schedule) produces. Every
fired trigger runs in its own hidden conversation tracked by an ``event_runs``
row with a status and per-run token usage.

Replying to a run that is pending your input reuses the existing conversation
``/messages`` endpoint (the run carries a ``conversation_id``); the transcript
and per-request usage likewise come from the conversation endpoints.
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote


async def list_runs(
    client,
    *,
    kind: Optional[str] = None,
    subscription: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List runs (newest first). ``kind`` is the canonical ``source_kind``
    (``skill_event`` / ``file_watcher`` / ``schedule``). Returns
    ``{"runs": [...], "total": N}``."""
    params: dict[str, Any] = {}
    if kind:
        params["source_kind"] = kind
    if subscription:
        params["subscription_id"] = subscription
    if status:
        params["status"] = status
    if limit:
        params["limit"] = limit
    resp = await client.get_json("/api/event-runs", params=params or None)
    return resp if isinstance(resp, dict) else {}


async def get_run(client, run_id: str) -> dict[str, Any]:
    """Fetch one run by its event-run id; returns the RunJSON object."""
    resp = await client.get_json(f"/api/event-runs/{quote(run_id, safe='')}")
    if isinstance(resp, dict) and isinstance(resp.get("run"), dict):
        return resp["run"]
    return {}


async def delete_run(client, run_id: str) -> None:
    """Delete a run and its hidden conversation (usage rollup survives)."""
    await client.delete(f"/api/event-runs/{quote(run_id, safe='')}")


async def cancel_run(client, run_id: str) -> bool:
    """Cancel a running event run. Returns False when it wasn't running."""
    resp = await client.post_json(f"/api/event-runs/{quote(run_id, safe='')}/cancel")
    if isinstance(resp, dict):
        return bool(resp.get("cancelled") or False)
    return False


async def reply(
    client,
    conversation_id: str,
    message: str,
    *,
    reasoning: bool = True,
) -> dict[str, Any]:
    """Send a message into a run's conversation (resumes a pending run)."""
    resp = await client.post_json(
        f"/api/conversations/{quote(conversation_id, safe='')}/messages",
        {"text": message, "reasoning": reasoning},
    )
    return resp if isinstance(resp, dict) else {}
