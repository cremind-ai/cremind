"""User Document Search — query API (find / search / read) for the CLI and tests.

``POST /api/userdocs/query/{find|search|read}`` runs the same leaves the
agent calls (``user_documents__find_files`` / ``__search`` / ``__read``), with
the same arguments, and answers ``{text, …}``: ``text`` is what the agent would
read, the rest is the structured result (mode, items with their citation
tokens, paging). ``cremind userdocs search|find|read`` print ``text``.

The profile is always the caller's own (``request.user.username``); an id from
another profile's index is simply not found. By default the text is not cut
to the agent's tool-result budget (a person at a terminal wants all of it);
``"budgeted": true`` in the body renders it exactly as the agent sees it.

Nothing here registers citations: there is no conversation to register them
in. ``cremind userdocs cite`` resolves a token straight from the index.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.api._auth import require_auth

_LEAVES = {"find": "find_files", "search": "search", "read": "read"}
_STATUS = {"unavailable": 409, "invalid": 400, "not_found": 404}


def _tool_llm(profile: str) -> Any:
    """The tool group's child LLM (the ``low`` group) for thorough mode, as
    the agent's call would get it; None when the tool or a model is not set
    up — thorough mode then says so and runs a normal search."""
    try:
        from app.tools.registry import get_tool_registry

        group = get_tool_registry().get("user_documents")
        if group is None:
            return None
        group.refresh_llm(profile)
        return group.adapter._llm
    except Exception:  # noqa: BLE001
        return None


async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


def get_userdocs_query_routes() -> List[Route]:
    async def handle_query(request: Request) -> JSONResponse:
        denied = require_auth(request)
        if denied is not None:
            return denied
        leaf = _LEAVES.get(request.path_params.get("leaf") or "")
        if leaf is None:
            return JSONResponse({"error": "NotFound", "message": "Unknown query."}, status_code=404)
        body = await _json_body(request)
        profile = getattr(request.user, "username", "") or ""
        budgeted = bool(body.pop("budgeted", False))
        # Arguments only: nothing in the body can name a profile, an LLM or
        # the tool's private context — the leaves take exactly what the agent
        # may pass.
        args = {k: v for k, v in body.items() if not str(k).startswith("_")}

        from app.tools.builtin.user_documents import execute

        llm = await asyncio.to_thread(_tool_llm, profile) if leaf == "search" and args.get("thorough") else None
        result = await execute(leaf, profile, args, llm=llm, budgeted=budgeted)
        if result.error is not None:
            payload = dict(result.error)
            if result.text:
                payload["text"] = result.text
            return JSONResponse(payload, status_code=_STATUS.get(result.error_kind or "", 400))
        return JSONResponse({"text": result.text, **result.data})

    return [Route("/api/userdocs/query/{leaf}", handle_query, methods=["POST"])]
