"""HTTP / SSE / WebSocket client for the Cremind server.

Mirrors `cli/internal/client/` (Go). Imports of this package may not be free
of httpx/websockets cost; keep them inside command function bodies rather
than at module top level so `cremind --help` stays snappy.
"""

from app.cli.client._base import APIError, Client
from app.cli.client._sse import Event

__all__ = ["APIError", "Client", "Event"]
