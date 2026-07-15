"""Optional-feature endpoints — `/api/features` and `/api/features/install`.

`GET /api/features` returns a **map** keyed by feature id
(`{installed, requires_restart_after_install, extras}`). `POST
/api/features/install` streams pip output as *named* SSE frames
(`event: log|done|error`) — consumed via `Client.stream_post`.
"""

from __future__ import annotations

from typing import Any

from app.cli.client._base import Client


async def get_features(client: Client) -> dict[str, Any]:
    resp = await client.get_json("/api/features")
    return resp if isinstance(resp, dict) else {}


def features_install_path() -> str:
    return "/api/features/install"
