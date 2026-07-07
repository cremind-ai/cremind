"""Unit tests for the Zalo Bot API adapter (app/channels/adapters/zalo.py).

Backs ``ZaloBotClient`` with an ``httpx.MockTransport`` so no network is
touched, and exercises the adapter's update parsing + outbound chunking with
lightweight fakes (no storage / no live agent).
"""

from __future__ import annotations

import asyncio
import json

import httpx

from app.channels.adapters.zalo import ZaloApiError, ZaloBotAdapter, ZaloBotClient


def _client_with(handler) -> ZaloBotClient:
    client = ZaloBotClient("12345:secret")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _adapter() -> ZaloBotAdapter:
    channel = {
        "id": "c1",
        "profile": "admin",
        "channel_type": "zalo",
        "mode": "bot",
        "config": {"bot_token": "12345:secret"},
    }
    return ZaloBotAdapter(channel, storage=object())


# ── ZaloBotClient ──────────────────────────────────────────────────────────


def test_send_message_hits_expected_url_and_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": "m1"}})

    async def run():
        client = _client_with(handler)
        try:
            await client.send_message("999", "hello")
        finally:
            await client.aclose()

    asyncio.run(run())
    assert seen["url"] == "https://bot-api.zaloplatforms.com/bot12345:secret/sendMessage"
    assert seen["body"] == {"chat_id": "999", "text": "hello"}


def test_error_response_raises_zalo_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error_code": 401, "description": "bad token"})

    async def run():
        client = _client_with(handler)
        try:
            await client.get_me()
        finally:
            await client.aclose()

    try:
        asyncio.run(run())
        assert False, "expected ZaloApiError"
    except ZaloApiError as exc:
        assert exc.error_code == 401
        assert not exc.is_polling_timeout


def test_408_is_polling_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error_code": 408, "description": "timeout"})

    async def run():
        client = _client_with(handler)
        try:
            await client.get_updates(timeout=1)
        finally:
            await client.aclose()

    try:
        asyncio.run(run())
        assert False, "expected ZaloApiError"
    except ZaloApiError as exc:
        assert exc.is_polling_timeout is True


# ── adapter update parsing ──────────────────────────────────────────────────


def test_handle_update_extracts_text_message():
    adapter = _adapter()
    seen: list[tuple] = []

    async def fake_inbound(sender_id, display_name, text):
        seen.append((sender_id, display_name, text))

    adapter._handle_inbound_safe = fake_inbound  # type: ignore[assignment]

    async def run():
        adapter._handle_update({
            "event_name": "message.text.received",
            "message": {
                "text": "hi there",
                "chat": {"id": "chat-7", "chat_type": "PRIVATE"},
                "from": {"id": "u1", "display_name": "Alice"},
            },
        })
        await asyncio.sleep(0.01)  # let the spawned task run

    asyncio.run(run())
    assert seen == [("chat-7", "Alice", "hi there")]


def test_handle_update_skips_non_text_events():
    adapter = _adapter()
    called = False

    async def fake_inbound(*a, **k):
        nonlocal called
        called = True

    adapter._handle_inbound_safe = fake_inbound  # type: ignore[assignment]

    async def run():
        adapter._handle_update({
            "event_name": "message.sticker.received",
            "message": {"chat": {"id": "c"}, "sticker": {"id": 1}},
        })
        adapter._handle_update({"event_name": "message.text.received", "message": {"chat": {"id": "c"}}})
        await asyncio.sleep(0.01)

    asyncio.run(run())
    assert called is False


def test_send_text_splits_to_zalo_limit():
    adapter = _adapter()
    sent: list[str] = []

    class FakeClient:
        async def send_message(self, chat_id, text):
            sent.append(text)

    adapter._api = FakeClient()  # type: ignore[assignment]

    async def run():
        await adapter._send_text("chat-1", "x" * 4500)

    asyncio.run(run())
    assert len(sent) >= 3
    assert all(len(chunk) <= 2000 for chunk in sent)
