"""File Watcher API.

REST + SSE endpoints for the conversation-scoped filesystem watch
subscription system:

- list / delete / register subscriptions
- stream live admin-page snapshots over SSE
- stream live notifications (re-uses the shared notifications bus)

Mirrors :mod:`app.api.events`. The simulate endpoint is intentionally
omitted — the proper way to test a file watcher is to actually create or
modify a file at the watched path.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app.config.settings import get_user_working_directory
from app.events import get_file_watcher_manager
from app.events.file_watcher_admin_bus import get_file_watcher_admin_stream_bus
from app.storage import get_conversation_storage, get_file_watcher_storage
from app.utils.logger import logger


_VALID_EVENT_TYPES = {"created", "modified", "deleted", "moved"}
_VALID_TARGET_KINDS = {"file", "folder", "any"}


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _require_auth(request: Request) -> Optional[JSONResponse]:
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


def _enrich_subscription(sub: Dict[str, Any]) -> Dict[str, Any]:
    manager = get_file_watcher_manager()
    return {**sub, "armed": manager.is_armed(sub)}


async def _list_subscriptions_for_profile(profile: str) -> List[Dict[str, Any]]:
    store = get_file_watcher_storage()
    rows = store.list_by_profile(profile)
    if not rows:
        return []
    conv_storage = get_conversation_storage()
    titles: Dict[str, str] = {}
    for row in rows:
        cid = row["conversation_id"]
        if cid in titles:
            continue
        try:
            conv = await conv_storage.get_conversation(cid)
        except Exception:  # noqa: BLE001
            conv = None
        titles[cid] = (conv or {}).get("title") or "Untitled Chat"
    return [
        {
            **_enrich_subscription(row),
            "conversation_title": titles.get(row["conversation_id"], ""),
        }
        for row in rows
    ]


async def _build_admin_snapshot(profile: str) -> Dict[str, Any]:
    """Bundle subscriptions for the file-watcher events page."""
    subscriptions = await _list_subscriptions_for_profile(profile)
    return {"subscriptions": subscriptions}


def publish_file_watchers_admin_changed(profile: Optional[str]) -> None:
    """Tell the file-watcher admin SSE endpoint to push a fresh snapshot."""
    if not profile:
        return
    try:
        get_file_watcher_admin_stream_bus().publish(profile, {})
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"file-watchers admin bus publish failed: {exc}")


def _resolve_path(raw_path: Optional[str]) -> tuple[str, bool]:
    """Resolve relative→USER_WORKING_DIR, absolute→as-is. Returns (abs_path, was_relative)."""
    base = get_user_working_directory()
    if not raw_path or not raw_path.strip():
        return os.path.abspath(os.path.normpath(base)), True
    candidate = raw_path.strip()
    if candidate.startswith("~"):
        candidate = os.path.expanduser(candidate)
    if os.path.isabs(candidate):
        return os.path.abspath(os.path.normpath(candidate)), False
    return os.path.abspath(os.path.normpath(os.path.join(base, candidate))), True


def _is_under(path: str, parent: str) -> bool:
    try:
        normalized_path = os.path.normcase(os.path.abspath(path))
        normalized_parent = os.path.normcase(os.path.abspath(parent))
        common = os.path.commonpath([normalized_path, normalized_parent])
        return common == normalized_parent
    except ValueError:
        return False


def _auto_name(root_path: str, extensions: List[str], target_kind: str) -> str:
    base = os.path.basename(root_path.rstrip(os.sep)) or root_path
    if extensions:
        ext_part = ",".join(extensions)
    elif target_kind != "any":
        ext_part = target_kind
    else:
        ext_part = "all"
    return f"{base}-{ext_part}"


def _normalize_extensions(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, list):
        items = raw
    else:
        return []
    out: List[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        s = item.strip().lower()
        if not s:
            continue
        if not s.startswith("."):
            s = f".{s}"
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def get_file_watcher_routes() -> list[Route]:

    async def handle_list(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        subs = await _list_subscriptions_for_profile(profile)
        return JSONResponse({"subscriptions": subs})

    async def handle_delete(request: Request) -> JSONResponse:
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        sub_id = request.path_params["id"]
        store = get_file_watcher_storage()
        existing = store.get(sub_id)
        if existing is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        if profile and existing["profile"] != profile:
            return JSONResponse({"error": "Forbidden"}, status_code=403)
        store.delete(sub_id)
        try:
            get_file_watcher_manager().disarm(existing)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to disarm watcher after delete")
        publish_file_watchers_admin_changed(profile)
        return JSONResponse({"ok": True})

    async def handle_create(request: Request) -> JSONResponse:
        """Register a file watcher from the API (CLI / admin tooling).

        The agent path uses the ``register_file_watcher`` builtin tool;
        this endpoint is for CLI parity. Identical validation rules apply.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        action = (body.get("action") or "").strip()
        if not action:
            return JSONResponse(
                {"error": "Missing parameter", "message": "action is required"},
                status_code=400,
            )

        target_kind = (body.get("target_kind") or "any").strip().lower()
        if target_kind not in _VALID_TARGET_KINDS:
            return JSONResponse(
                {
                    "error": "invalid_target_kind",
                    "message": f"target_kind must be one of {sorted(_VALID_TARGET_KINDS)}",
                },
                status_code=400,
            )

        triggers_raw = body.get("triggers") or list(_VALID_EVENT_TYPES)
        if not isinstance(triggers_raw, list):
            return JSONResponse(
                {"error": "invalid_triggers", "message": "triggers must be an array"},
                status_code=400,
            )
        triggers: List[str] = []
        seen: set[str] = set()
        for t in triggers_raw:
            if not isinstance(t, str):
                continue
            tt = t.strip().lower()
            if not tt or tt in seen:
                continue
            if tt not in _VALID_EVENT_TYPES:
                return JSONResponse(
                    {
                        "error": "invalid_trigger",
                        "message": f"unknown trigger {tt!r}; valid: "
                                   f"{sorted(_VALID_EVENT_TYPES)}",
                    },
                    status_code=400,
                )
            seen.add(tt)
            triggers.append(tt)
        if not triggers:
            triggers = list(_VALID_EVENT_TYPES)

        recursive = body.get("recursive")
        recursive_flag = True if recursive is None else bool(recursive)

        extensions = _normalize_extensions(body.get("extensions"))

        resolved_path, was_relative = _resolve_path(body.get("path"))
        if was_relative:
            base = get_user_working_directory()
            if not _is_under(resolved_path, base):
                return JSONResponse(
                    {
                        "error": "path_escape",
                        "message": (
                            f"Relative path resolves outside the user working "
                            f"directory ({base}); refusing to watch."
                        ),
                    },
                    status_code=400,
                )
        if not os.path.exists(resolved_path) or not os.path.isdir(resolved_path):
            return JSONResponse(
                {
                    "error": "invalid_path",
                    "message": f"{resolved_path} is not an existing directory",
                },
                status_code=400,
            )

        name = (body.get("name") or "").strip()
        if not name:
            name = _auto_name(resolved_path, extensions, target_kind)

        # Resolve (or create) the conversation row.
        conversation_id = (body.get("conversation_id") or "").strip()
        conv_storage = get_conversation_storage()
        if conversation_id:
            existing_conv = await conv_storage.get_conversation(conversation_id)
            if existing_conv is None:
                return JSONResponse(
                    {"error": "Not found", "message": "conversation not found"},
                    status_code=404,
                )
            if existing_conv.get("profile") != profile:
                return JSONResponse({"error": "Forbidden"}, status_code=403)
        else:
            # No conversation_id provided — open a fresh conversation labelled
            # by the watch name so the user can see it in the sidebar.
            conv = await conv_storage.create_conversation(
                profile=profile,
                title=f"File Watcher: {name}",
            )
            conversation_id = conv["id"]

        store = get_file_watcher_storage()
        row = store.insert(
            conversation_id=conversation_id,
            profile=profile,
            name=name,
            root_path=resolved_path,
            recursive=recursive_flag,
            target_kind=target_kind,
            event_types=",".join(triggers),
            extensions=",".join(extensions),
            action=action,
        )

        armed = False
        try:
            armed = get_file_watcher_manager().arm(row)
        except Exception:  # noqa: BLE001
            logger.exception("file_watchers.handle_create: arm failed")

        publish_file_watchers_admin_changed(profile)
        return JSONResponse({**row, "armed": armed}, status_code=201)

    async def handle_admin_stream(request: Request) -> Any:
        """SSE endpoint pushing the live file-watcher admin snapshot."""
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)

        bus = get_file_watcher_admin_stream_bus()
        queue = bus.subscribe(profile)

        async def generator():
            def _frame(payload: Dict[str, Any]) -> bytes:
                return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

            try:
                snapshot = await _build_admin_snapshot(profile)
                yield _frame({"type": "snapshot", "data": snapshot})
                yield _frame({"type": "ready", "data": {}})

                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    snapshot = await _build_admin_snapshot(profile)
                    yield _frame({"type": "snapshot", "data": snapshot})
            finally:
                bus.unsubscribe(profile, queue)

        headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
        return StreamingResponse(
            generator(), media_type="text/event-stream", headers=headers,
        )

    return [
        Route("/api/file-watchers", handle_list, methods=["GET"]),
        Route("/api/file-watchers", handle_create, methods=["POST"]),
        Route(
            "/api/file-watchers/admin/stream",
            handle_admin_stream, methods=["GET"],
        ),
        Route("/api/file-watchers/{id}", handle_delete, methods=["DELETE"]),
    ]
