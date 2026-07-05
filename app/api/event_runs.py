"""REST API for event runs — the per-trigger execution history.

Each fired event trigger runs in its own hidden conversation, tracked by an
``event_runs`` row. These endpoints back the Events-page run-history child tables
and the run-detail drawer:

- ``GET /api/event-runs``         list runs (filter by source_kind / subscription / status)
- ``GET /api/event-runs/{id}``    one run's detail
- ``DELETE /api/event-runs/{id}`` delete a run (its conversation too; usage survives)
- ``POST /api/event-runs/{id}/cancel`` cancel a running run

Replying to a pending run, fetching its transcript, and its per-request usage
reuse the existing conversation endpoints (the run carries ``conversation_id``).
Per-run usage totals are a ``GROUP BY event_run_id`` rollup so they survive the
run's conversation being deleted/pruned.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.storage import get_event_run_storage, get_usage_storage
from app.utils.logger import logger


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _require_auth(request: Request) -> Optional[JSONResponse]:
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


def _run_json(run: Dict[str, Any], usage: Dict[str, Any] | None) -> Dict[str, Any]:
    """Serialize a run row + its usage rollup into the canonical RunJSON shape."""
    u = usage or {}
    return {
        "id": run["id"],
        "profile": run["profile"],
        "source_kind": run["source_kind"],
        "subscription_id": run["subscription_id"],
        "conversation_id": run.get("conversation_id"),
        "run_id": run.get("run_id"),
        "status": run["status"],
        "label": run.get("label") or "",
        "action": run.get("action") or "",
        "trigger_payload": run.get("trigger_payload"),
        "pending_question": run.get("pending_question"),
        "error": run.get("error"),
        "turn_count": run.get("turn_count", 0),
        "usage": {
            "input_tokens": u.get("input_tokens", 0),
            "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
            "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0),
            "output_tokens": u.get("output_tokens", 0),
            "total_tokens": u.get("total_tokens", 0),
            "total_usd": u.get("total_usd", 0.0),
            "request_count": u.get("request_count", 0),
        },
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "finished_at": run.get("finished_at"),
    }


async def _runs_with_usage(runs: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    """Attach the usage rollup to a list of run rows in one batched query."""
    if not runs:
        return []
    ids = [r["id"] for r in runs]
    try:
        rollup = await get_usage_storage().rollup_by_event_run(ids)
    except Exception:  # noqa: BLE001
        logger.exception("[event_runs] usage rollup failed")
        rollup = {}
    return [_run_json(r, rollup.get(r["id"])) for r in runs]


async def build_event_runs_admin_snapshot(profile: str) -> Dict[str, Any]:
    """Snapshot for the Events-page admin stream: recent runs + per-rule summaries."""
    store = get_event_run_storage()
    try:
        runs = await store.recent_for_profile(profile, limit=200)
        summaries = await store.subscription_summaries(profile)
    except Exception:  # noqa: BLE001
        logger.exception("[event_runs] admin snapshot failed")
        runs, summaries = [], {}
    return {
        "runs": await _runs_with_usage(runs),
        "summaries": summaries,
    }


def get_event_run_routes() -> list[Route]:

    async def handle_list(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        q = request.query_params
        try:
            limit = min(int(q.get("limit", 50)), 200)
            offset = max(int(q.get("offset", 0)), 0)
        except (TypeError, ValueError):
            limit, offset = 50, 0
        store = get_event_run_storage()
        runs, total = await store.list(
            profile=profile,
            source_kind=q.get("source_kind"),
            subscription_id=q.get("subscription_id"),
            status=q.get("status"),
            limit=limit,
            offset=offset,
        )
        return JSONResponse({
            "runs": await _runs_with_usage(runs),
            "total": total,
        })

    async def handle_get(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        run_id = request.path_params["run_id"]
        run = await get_event_run_storage().get(run_id)
        if run is None or run.get("profile") != profile:
            return JSONResponse({"error": "Run not found"}, status_code=404)
        rows = await _runs_with_usage([run])
        return JSONResponse({"run": rows[0]})

    async def handle_delete(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        run_id = request.path_params["run_id"]
        store = get_event_run_storage()
        run = await store.get(run_id)
        if run is None or run.get("profile") != profile:
            return JSONResponse({"error": "Run not found"}, status_code=404)

        # Cancel if still running, then tear down its conversation + row. Usage
        # rows survive (conversation_id SET-NULLs; event_run_id stays).
        from app.agent.stream_runner import cancel_run
        from app.events.run_dispatcher import _discard_conversation

        if run.get("status") == "running" and run.get("run_id"):
            try:
                cancel_run(run["run_id"])
            except Exception:  # noqa: BLE001
                logger.debug("[event_runs] cancel on delete failed", exc_info=True)
        await store.delete(run_id)
        await _discard_conversation(run.get("conversation_id"))

        try:
            from app.events.event_runs_admin_bus import publish_event_runs_changed
            publish_event_runs_changed(profile)
        except Exception:  # noqa: BLE001
            pass
        return JSONResponse({"ok": True})

    async def handle_cancel(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        run_id = request.path_params["run_id"]
        store = get_event_run_storage()
        run = await store.get(run_id)
        if run is None or run.get("profile") != profile:
            return JSONResponse({"error": "Run not found"}, status_code=404)
        cancelled = False
        if run.get("status") == "running" and run.get("run_id"):
            from app.agent.stream_runner import cancel_run
            cancelled = cancel_run(run["run_id"])
        return JSONResponse({"cancelled": bool(cancelled)})

    return [
        Route("/api/event-runs", handle_list, methods=["GET"]),
        Route("/api/event-runs/{run_id}", handle_get, methods=["GET"]),
        Route("/api/event-runs/{run_id}", handle_delete, methods=["DELETE"]),
        Route("/api/event-runs/{run_id}/cancel", handle_cancel, methods=["POST"]),
    ]
