"""Process endpoints — `/api/processes*` and `/api/autostart-processes*`.

Mirrors `cli/internal/client/processes.go`. The PTY WebSocket attach itself
is handled in Phase 4 (`cremind proc attach`); this module exposes the URL helper
so that command can build the ws:// URL.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.cli.client._base import Client


async def list_processes(client: Client) -> list[dict[str, Any]]:
    resp = await client.get_json("/api/processes")
    if isinstance(resp, dict) and isinstance(resp.get("processes"), list):
        return [p for p in resp["processes"] if isinstance(p, dict)]
    return []


async def get_process(client: Client, pid: str) -> dict[str, Any]:
    resp = await client.get_json(f"/api/processes/{quote(pid, safe='')}")
    return resp if isinstance(resp, dict) else {}


async def stop_process(client: Client, pid: str) -> None:
    await client.post_json(f"/api/processes/{quote(pid, safe='')}/stop")


async def send_process_stdin(
    client: Client,
    pid: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    resp = await client.post_json(
        f"/api/processes/{quote(pid, safe='')}/stdin",
        body,
    )
    return resp if isinstance(resp, dict) else {}


async def resize_process_pty(
    client: Client,
    pid: str,
    cols: int,
    rows: int,
) -> None:
    await client.post_json(
        f"/api/processes/{quote(pid, safe='')}/resize",
        {"cols": cols, "rows": rows},
    )


def processes_stream_path() -> str:
    return "/api/processes/stream"


def process_websocket_path(pid: str) -> str:
    """Path used by `connect_ws` to build the full ws:// URL for a PTY attach."""
    return f"/api/processes/{quote(pid, safe='')}/ws"


# ── autostart ─────────────────────────────────────────────────────────────

async def list_autostart_processes(client: Client) -> list[dict[str, Any]]:
    resp = await client.get_json("/api/autostart-processes")
    if isinstance(resp, dict) and isinstance(resp.get("autostart"), list):
        return [a for a in resp["autostart"] if isinstance(a, dict)]
    return []


async def create_autostart_from_process(
    client: Client,
    pid: str,
    force: bool = False,
) -> dict[str, Any]:
    body: dict[str, Any] = {"process_id": pid}
    if force:
        body["force"] = True
    resp = await client.post_json("/api/autostart-processes", body)
    return resp if isinstance(resp, dict) else {}


async def delete_autostart_process(client: Client, autostart_id: str) -> None:
    await client.delete(f"/api/autostart-processes/{quote(autostart_id, safe='')}")


async def run_autostart_process(
    client: Client,
    autostart_id: str,
) -> dict[str, Any]:
    resp = await client.post_json(
        f"/api/autostart-processes/{quote(autostart_id, safe='')}/run"
    )
    return resp if isinstance(resp, dict) else {}
