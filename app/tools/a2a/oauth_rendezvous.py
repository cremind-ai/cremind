"""In-process rendezvous for the A2A OAuth callback.

Unlike the gmail/atlassian skills (which run as subprocesses and hand the
authorization code back via a file inbox under ``CREMIND_SYSTEM_DIR/oauth_inbox``),
A2A tool auth runs in the backend's own event loop. So when connecting to an
external A2A agent that requires OAuth, ``CremindA2AAuth.authenticate`` registers
an ``asyncio.Future`` keyed by the OAuth ``state`` here, then awaits it; the
backend route ``GET /api/oauth/a2a/callback`` (app/api/oauth_callback.py) resolves
it with the code. No auxiliary port and no file handoff are needed.
"""
from __future__ import annotations

import asyncio
from typing import Optional

# Pending consent flows keyed by OAuth ``state``. Single-process, in-memory.
_pending: dict[str, asyncio.Future] = {}


def register(state: str, future: asyncio.Future) -> None:
    """Record the Future the callback route should resolve for ``state``."""
    _pending[state] = future


def resolve(state: str, code: Optional[str]) -> bool:
    """Deliver ``code`` (or ``None`` on consent error) to the waiting Future.

    Returns ``True`` if a pending flow matched ``state`` and was resolved,
    ``False`` for an unknown/already-resolved state.
    """
    future = _pending.pop(state, None)
    if future is None or future.done():
        return False
    # The route and authenticate() share the backend loop, but resolve defensively
    # across threads so this is correct regardless of where the route runs.
    future.get_loop().call_soon_threadsafe(_set_result, future, code)
    return True


def cancel(state: str) -> None:
    """Drop a pending flow (timeout/cancel) so the registry can't leak entries."""
    _pending.pop(state, None)


def _set_result(future: asyncio.Future, code: Optional[str]) -> None:
    if not future.done():
        future.set_result(code)
