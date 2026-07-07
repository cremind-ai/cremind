"""Tests for the public Messenger webhook route (app/api/channels.py).

Covers GET verification (verify_token match / mismatch), POST signature
verification (X-Hub-Signature-256 HMAC), and event fan-out into the adapter's
``handle_webhook_message``. Uses a Starlette TestClient with fake storage +
registry so no DB or live adapter is needed.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from starlette.applications import Starlette
from starlette.testclient import TestClient

import app.api.channels as channels_mod
from app.api.channels import get_channel_routes

_CHANNEL_ID = "chan-1"
_VERIFY_TOKEN = "my-verify-token"
_APP_SECRET = "app-secret-xyz"


class _FakeStorage:
    async def get_channel(self, cid):
        if cid != _CHANNEL_ID:
            return None
        return {
            "id": _CHANNEL_ID,
            "channel_type": "messenger",
            "config": {
                "verify_token": _VERIFY_TOKEN,
                "app_secret": _APP_SECRET,
                "page_access_token": "PAGE_TOKEN",
            },
        }


class _FakeAdapter:
    def __init__(self):
        self.messages: list[tuple] = []

    async def handle_webhook_message(self, sender_id, text):
        self.messages.append((sender_id, text))


class _FakeRegistry:
    def __init__(self, adapter):
        self._adapter = adapter

    def get_adapter(self, cid):
        return self._adapter


def _client(monkeypatch, adapter=None):
    monkeypatch.setattr(
        channels_mod, "get_channel_registry", lambda *a, **k: _FakeRegistry(adapter),
    )
    app = Starlette(routes=get_channel_routes(_FakeStorage()))
    return TestClient(app)


def _sign(raw: bytes) -> str:
    return "sha256=" + hmac.new(_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()


# ── GET verification ────────────────────────────────────────────────────────


def test_get_verification_echoes_challenge_on_match(monkeypatch):
    client = _client(monkeypatch)
    resp = client.get(
        f"/api/channels/webhook/messenger/{_CHANNEL_ID}",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": _VERIFY_TOKEN,
            "hub.challenge": "CHALLENGE_123",
        },
    )
    assert resp.status_code == 200
    assert resp.text == "CHALLENGE_123"


def test_get_verification_rejects_bad_token(monkeypatch):
    client = _client(monkeypatch)
    resp = client.get(
        f"/api/channels/webhook/messenger/{_CHANNEL_ID}",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "WRONG",
            "hub.challenge": "CHALLENGE_123",
        },
    )
    assert resp.status_code == 403


def test_get_unknown_channel_404(monkeypatch):
    client = _client(monkeypatch)
    resp = client.get(
        "/api/channels/webhook/messenger/does-not-exist",
        params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "y"},
    )
    assert resp.status_code == 404


# ── POST delivery ────────────────────────────────────────────────────────────


def test_post_valid_signature_dispatches_message(monkeypatch):
    adapter = _FakeAdapter()
    client = _client(monkeypatch, adapter)
    payload = {
        "object": "page",
        "entry": [
            {"messaging": [{"sender": {"id": "PSID-1"}, "message": {"text": "hello bot"}}]},
        ],
    }
    raw = json.dumps(payload).encode()
    resp = client.post(
        f"/api/channels/webhook/messenger/{_CHANNEL_ID}",
        content=raw,
        headers={"X-Hub-Signature-256": _sign(raw), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert adapter.messages == [("PSID-1", "hello bot")]


def test_post_bad_signature_rejected(monkeypatch):
    adapter = _FakeAdapter()
    client = _client(monkeypatch, adapter)
    raw = json.dumps({"object": "page", "entry": []}).encode()
    resp = client.post(
        f"/api/channels/webhook/messenger/{_CHANNEL_ID}",
        content=raw,
        headers={"X-Hub-Signature-256": "sha256=deadbeef", "Content-Type": "application/json"},
    )
    assert resp.status_code == 403
    assert adapter.messages == []


def test_post_skips_echo_messages(monkeypatch):
    adapter = _FakeAdapter()
    client = _client(monkeypatch, adapter)
    payload = {
        "object": "page",
        "entry": [
            {"messaging": [{"sender": {"id": "PAGE"}, "message": {"text": "echo", "is_echo": True}}]},
        ],
    }
    raw = json.dumps(payload).encode()
    resp = client.post(
        f"/api/channels/webhook/messenger/{_CHANNEL_ID}",
        content=raw,
        headers={"X-Hub-Signature-256": _sign(raw), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert adapter.messages == []
