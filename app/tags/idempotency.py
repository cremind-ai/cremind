"""Idempotency keys for the setup API (cremind-tag ``docs/setup-api.md``).

A mutation may carry ``Idempotency-Key: <key>`` (or ``"idempotency_key"`` in
its body). The first answer is stored under ``scope | route | key``; a retry
with the same key and the same body gets that answer back, a retry with a
different body 409 ``idempotency_key_reused``. Keys live 24 hours.

Only settled answers are stored: 2xx, and 4xx refusals that a retry would
repeat. A 5xx is never remembered, so a retry after a crash does the work.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Awaitable, Callable

from sqlalchemy import delete, insert, select
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.storage.models import TagIdempotencyModel

IDEMPOTENCY = TagIdempotencyModel.__table__
TTL_MS = 24 * 3600 * 1000.0
_MAX_KEY = 120


def request_key(request: Request, body: dict[str, Any] | None) -> str | None:
    raw = request.headers.get("idempotency-key") or (body or {}).get("idempotency_key")
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw or len(raw) > _MAX_KEY or any(ord(c) < 33 or ord(c) > 126 for c in raw):
        return None
    return raw


def _digest(body: dict[str, Any] | None) -> str:
    clean = {k: v for k, v in (body or {}).items() if k != "idempotency_key"}
    return hashlib.sha256(json.dumps(clean, sort_keys=True, separators=(",", ":"), default=str)
                          .encode("utf-8")).hexdigest()


async def run_once(scope: str, route: str, key: str | None, body: dict[str, Any] | None,
                   produce: Callable[[], Awaitable[JSONResponse]]) -> JSONResponse:
    """Answer from memory for a known key, else run ``produce`` and remember a
    settled answer."""
    if key is None:
        return await produce()
    from app.tags.storage import get_tag_storage

    store = get_tag_storage()
    full = f"{scope}|{route}|{key}"[:200]
    digest = _digest(body)
    now = time.time() * 1000
    async with store.engine.connect() as conn:
        row = (await conn.execute(select(IDEMPOTENCY).where(IDEMPOTENCY.c.key == full))).first()
    if row is not None and float(row.expires_at) > now:
        if row.request_sha256 != digest:
            return JSONResponse({"error": "idempotency_key_reused",
                                 "message": "This Idempotency-Key was used for a different request.",
                                 "detail": "This Idempotency-Key was used for a different request."},
                                status_code=409)
        return JSONResponse(row.response or {}, status_code=int(row.status_code))
    response = await produce()
    status = int(response.status_code)
    if status >= 500 or status in (401, 403, 429):
        return response
    try:
        payload = json.loads(bytes(response.body).decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return response
    try:
        async with store.engine.begin() as conn:
            await conn.execute(delete(IDEMPOTENCY).where(IDEMPOTENCY.c.key == full))
            await conn.execute(insert(IDEMPOTENCY), [{
                "key": full, "request_sha256": digest, "status_code": status, "response": payload,
                "created_at": now, "expires_at": now + TTL_MS,
            }])
    except IntegrityError:
        pass  # a concurrent twin stored it first; both answers came from the same work
    return response


async def prune(now: float | None = None) -> int:
    from app.tags.storage import get_tag_storage

    now = now or time.time() * 1000
    async with get_tag_storage().engine.begin() as conn:
        result = await conn.execute(delete(IDEMPOTENCY).where(IDEMPOTENCY.c.expires_at <= now))
    return int(result.rowcount or 0)


__all__ = ["prune", "request_key", "run_once"]
