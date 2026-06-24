"""Token-usage + estimated-cost dashboard API.

Exposes ``GET /api/usage/summary`` — the aggregate behind the "Usage & Cost"
dashboard: grand totals, a daily time series, breakdowns by model / provider /
source (reasoning agent vs. each sub-agent/tool), top conversations, cache-hit
rate, and a ``has_unpriced`` flag for historical rows whose cost couldn't be
estimated.

Scope: results are limited to the caller's own profile. The ``admin`` profile
may pass ``?profile=<name>`` to inspect another profile, or omit it to span all
profiles. (The per-conversation breakdown lives on the conversations router at
``GET /api/conversations/{id}/usage``.)
"""

from __future__ import annotations

from datetime import datetime, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.api._auth import require_auth
from app.storage import get_usage_storage


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _float_param(request: Request, name: str):
    raw = request.query_params.get(name)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _int_param(request: Request, name: str, default: int = 0) -> int:
    raw = request.query_params.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bucket_to_iso(bucket: int) -> str:
    """Day-since-epoch bucket → ``YYYY-MM-DD`` for its (already local-adjusted) day."""
    return datetime.fromtimestamp(bucket * 86400, tz=timezone.utc).date().isoformat()


def get_usage_routes() -> list[Route]:

    async def handle_usage_summary(request: Request) -> JSONResponse:
        unauth = require_auth(request)
        if unauth is not None:
            return unauth

        caller = _profile_from_request(request)
        requested = request.query_params.get("profile")
        if caller == "admin":
            profile = requested or None  # admin: a named profile, or None = all
        else:
            profile = caller  # non-admin is pinned to their own profile

        start_ms = _float_param(request, "start")
        end_ms = _float_param(request, "end")
        tz_offset_min = _int_param(request, "tz_offset", 0)
        scope = {"profile": profile, "start_ms": start_ms, "end_ms": end_ms}

        usage = get_usage_storage()
        totals = await usage.totals(**scope)
        cache = await usage.cache_hit_rate(**scope)
        series = await usage.by_day(tz_offset_min=tz_offset_min, **scope)
        by_model = await usage.by_model(**scope)
        by_provider = await usage.by_provider(**scope)
        by_source = await usage.by_source(**scope)
        top_conversations = await usage.top_conversations(limit=10, **scope)
        has_unpriced = await usage.has_unpriced(**scope)

        for point in series:
            point["bucket"] = _bucket_to_iso(point["bucket"])

        return JSONResponse({
            "totals": totals,
            "cache_hit_rate": cache["cache_hit_rate"],
            "cache_read_usd": cache["cache_read_usd"],
            "cache_write_usd": cache["cache_write_usd"],
            "conversation_count": totals.get("conversation_count", 0),
            "request_count": totals.get("request_count", 0),
            "series": series,
            "by_model": by_model,
            "by_provider": by_provider,
            "by_source": by_source,
            "top_conversations": top_conversations,
            "has_unpriced": has_unpriced,
        })

    return [
        Route("/api/usage/summary", endpoint=handle_usage_summary, methods=["GET"]),
    ]
