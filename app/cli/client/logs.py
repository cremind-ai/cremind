"""Server-log stream endpoint — `/api/server/logs/stream`.

Admin-only SSE tail of the backend's Loguru records (the feed behind the
Developer page). The command layer consumes it via `Client.stream(...)`; this
module just exposes the path, mirroring `processes_stream_path()`.
"""

from __future__ import annotations


def logs_stream_path() -> str:
    return "/api/server/logs/stream"
