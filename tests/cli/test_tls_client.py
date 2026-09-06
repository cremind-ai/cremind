"""Remote TLS client waits for the server's tab-quiesce commit barrier."""
from __future__ import annotations

import asyncio

from app.cli.client import tls


class _Client:
    def __init__(self) -> None:
        self.calls = 0

    async def post_json(self, path: str, body: dict) -> dict:
        assert path == "/api/tls/activate"
        assert body["transition_id"] == "transition-1"
        self.calls += 1
        if self.calls == 1:
            return {
                "transition": {"phase": "quiescing"},
                "quiesce_pending": 2,
            }
        return {"transition": {"phase": "activating"}}


def test_activate_waits_for_registered_tabs(monkeypatch):
    client = _Client()
    monkeypatch.setattr(tls.asyncio, "sleep", lambda _seconds: _done())

    result = asyncio.run(tls.activate(client, "transition-1", "AA:BB"))

    assert result["transition"]["phase"] == "activating"
    assert client.calls == 2


async def _done() -> None:
    return None
