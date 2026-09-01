"""``POST /api/channels/{id}/notify`` — the operator's ad-hoc push, now with files.

The endpoint accepts two body forms: JSON (``message`` + optional
``attachments`` of ABSOLUTE server paths, validated against the profile's own
roots) and multipart (a ``payload`` JSON field plus file parts, spooled and
cleaned up — the remote-CLI form). Same conventions as
``test_channels_direct_message``: routes resolved from the route table, fake
request objects, the registry patched on ``app.api.channels``'s own name.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from typing import Callable

import app.api.channels as api_channels
from app.api.channels import get_channel_routes
from app.config.settings import BaseConfig

_NOTIFY = "/api/channels/{channel_id}/notify"
_PP = {"channel_id": "ch1"}


def _handler(store, path: str, method: str) -> Callable:
    for route in get_channel_routes(store):
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} not registered")


class _FakeUpload:
    def __init__(self, filename: str, data: bytes):
        self.filename = filename
        self._data = data

    async def read(self, n: int) -> bytes:
        chunk, self._data = self._data, b""
        return chunk


class _FakeForm:
    def __init__(self, fields: dict):
        self._fields = fields

    def get(self, key):
        return self._fields.get(key)

    def values(self):
        return list(self._fields.values())


def _req(body=None, *, username="p1", form=None):
    async def _json():
        if body is None:
            raise ValueError("no body")
        return body

    async def _form():
        return _FakeForm(form or {})

    content_type = (
        "multipart/form-data; boundary=x" if form is not None
        else "application/json"
    )
    return SimpleNamespace(
        method="POST",
        user=SimpleNamespace(is_authenticated=True, username=username),
        path_params=dict(_PP),
        json=_json,
        form=_form,
        headers={"content-type": content_type},
    )


class _Adapter:
    def __init__(self, *, recipients=2):
        self._recipients = recipients
        self.texts: list[str] = []
        self.files: list[str] = []

    async def deliver_text(self, text):
        self.texts.append(text)
        return self._recipients

    async def deliver_file(self, path, *, name=None, caption=None):
        # Read the bytes NOW: the spool is removed right after delivery, and a
        # transport that lazily opened the path later would find it gone.
        with open(path, "rb") as fh:
            fh.read()
        self.files.append(path)
        return self._recipients


class _Store:
    def __init__(self, channel):
        self._channel = channel

    async def get_channel(self, cid):
        return dict(self._channel) if cid == self._channel["id"] else None


def _channel(mode="notification"):
    return {"id": "ch1", "profile": "p1", "channel_type": "telegram",
            "mode": mode, "config": {}}


def _call(store, body=None, *, adapter=None, form=None):
    backup = api_channels.get_channel_registry
    api_channels.get_channel_registry = lambda *a, **k: SimpleNamespace(
        get_adapter=lambda cid: adapter,
    )
    try:
        handler = _handler(store, _NOTIFY, "POST")
        return asyncio.run(handler(_req(body, form=form)))
    finally:
        api_channels.get_channel_registry = backup


def _body(resp) -> dict:
    return json.loads(resp.body)


def _profile_file(monkeypatch, tmp_path, profile="p1", name="report.pdf") -> str:
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))
    target = tmp_path / profile
    target.mkdir(parents=True, exist_ok=True)
    path = target / name
    path.write_bytes(b"pdf")
    return str(path)


def test_plain_json_message_delivers():
    adapter = _Adapter()
    resp = _call(_Store(_channel()), {"message": "hi"}, adapter=adapter)
    assert resp.status_code == 200
    assert _body(resp) == {"delivered": True, "recipients": 2}
    assert adapter.texts == ["hi"]


def test_json_attachment_in_profile_roots_is_delivered(monkeypatch, tmp_path):
    path = _profile_file(monkeypatch, tmp_path)
    adapter = _Adapter()
    resp = _call(
        _Store(_channel()), {"message": "report", "attachments": [path]},
        adapter=adapter,
    )
    body = _body(resp)
    assert resp.status_code == 200
    assert body["files_delivered"] == 1
    assert adapter.files == [os.path.realpath(path)]


def test_json_attachment_outside_profile_roots_is_a_400(monkeypatch, tmp_path):
    other = _profile_file(monkeypatch, tmp_path, profile="p2", name="secret.txt")
    adapter = _Adapter()
    resp = _call(
        _Store(_channel()), {"message": "hi", "attachments": [other]},
        adapter=adapter,
    )
    assert resp.status_code == 400
    assert _body(resp)["rejected"]
    assert adapter.texts == [] and adapter.files == []


def test_multipart_uploads_deliver_and_the_spool_is_removed(monkeypatch, tmp_path):
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))
    adapter = _Adapter()
    form = {
        "payload": json.dumps({"message": "report attached"}),
        "file": _FakeUpload("report.pdf", b"pdf-bytes"),
    }
    resp = _call(_Store(_channel()), adapter=adapter, form=form)
    body = _body(resp)
    assert resp.status_code == 200
    assert body["files_delivered"] == 1
    assert adapter.texts == ["report attached"]
    (delivered,) = adapter.files
    assert os.path.basename(delivered) == "report.pdf"
    # The throwaway spool is gone once delivery finished.
    assert not os.path.exists(os.path.dirname(delivered))


def test_multipart_with_files_needs_no_message(monkeypatch, tmp_path):
    monkeypatch.setattr(BaseConfig, "CREMIND_SYSTEM_DIR", str(tmp_path))
    adapter = _Adapter()
    form = {"file": _FakeUpload("report.pdf", b"pdf-bytes")}
    resp = _call(_Store(_channel()), adapter=adapter, form=form)
    assert resp.status_code == 200
    assert adapter.texts == []            # no empty text pushed
    assert len(adapter.files) == 1


def test_a_non_notification_channel_is_still_a_400():
    resp = _call(_Store(_channel(mode="bot")), {"message": "hi"}, adapter=_Adapter())
    assert resp.status_code == 400
