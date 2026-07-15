"""Unit tests for the ``send_notification`` built-in tool's ``run()``.

The tool pushes a message to a profile's enabled notification-mode channels. It
resolves live adapters from the channel registry (patched here) and calls each
adapter's ``deliver_text``. Every failure path must surface as a structured
observation, never an exception.
"""

from __future__ import annotations

import asyncio

import app.channels.registry as reg
from app.tools.builtin.send_notification import SendNotificationTool


class _FakeAdapter:
    def __init__(self, channel_type, channel_id, *, recipients=0, raise_exc=None):
        self.channel_type = channel_type
        self.channel_id = channel_id
        self._recipients = recipients
        self._raise = raise_exc
        self.delivered: list[str] = []

    async def deliver_text(self, text):
        if self._raise is not None:
            raise self._raise
        self.delivered.append(text)
        return self._recipients


class _FakeRegistry:
    def __init__(self, adapters):
        self._adapters = list(adapters)

    def notification_adapters_for_profile(self, profile):
        return list(self._adapters)


def _run(arguments, *, registry=None, raise_runtime=False):
    def _get_registry(*_a, **_k):
        if raise_runtime:
            raise RuntimeError("not initialized")
        return registry

    reg_backup = None

    class _Patch:
        def __enter__(self):
            nonlocal reg_backup
            reg_backup = reg.get_channel_registry
            reg.get_channel_registry = _get_registry

        def __exit__(self, *exc):
            reg.get_channel_registry = reg_backup

    tool = SendNotificationTool()
    with _Patch():
        return asyncio.run(tool.run(arguments)).structured_content


def test_missing_message_is_structured_error():
    out = _run({"message": "  ", "_profile": "p"}, registry=_FakeRegistry([]))
    assert out["error"] == "InvalidArgument"


def test_missing_profile_is_structured_error():
    out = _run({"message": "hi"}, registry=_FakeRegistry([]))
    assert out["error"] == "NoProfile"


def test_registry_uninitialized_reports_channels_unavailable():
    out = _run({"message": "hi", "_profile": "p"}, raise_runtime=True)
    assert out["error"] == "ChannelsUnavailable"


def test_no_notification_channel_is_structured_error():
    out = _run({"message": "hi", "_profile": "p"}, registry=_FakeRegistry([]))
    assert out["error"] == "NoNotificationChannel"


def test_success_delivers_to_all_and_reports_counts():
    a = _FakeAdapter("telegram", "c1", recipients=2)
    b = _FakeAdapter("slack", "c2", recipients=1)
    out = _run({"message": "done", "_profile": "p"}, registry=_FakeRegistry([a, b]))
    assert out["delivered"] is True
    assert out["total_recipients"] == 3
    assert a.delivered == ["done"] and b.delivered == ["done"]
    statuses = {c["channel_type"]: c["status"] for c in out["channels"]}
    assert statuses == {"telegram": "sent", "slack": "sent"}


def test_channels_filter_targets_only_named_types():
    a = _FakeAdapter("telegram", "c1", recipients=1)
    b = _FakeAdapter("slack", "c2", recipients=1)
    out = _run(
        {"message": "hi", "_profile": "p", "channels": ["Telegram"]},
        registry=_FakeRegistry([a, b]),
    )
    assert [c["channel_type"] for c in out["channels"]] == ["telegram"]
    assert a.delivered == ["hi"] and b.delivered == []  # slack untouched


def test_unknown_channel_lists_available():
    a = _FakeAdapter("telegram", "c1", recipients=1)
    out = _run(
        {"message": "hi", "_profile": "p", "channels": ["discord"]},
        registry=_FakeRegistry([a]),
    )
    assert out["error"] == "UnknownChannel"
    assert out["available_channels"] == ["telegram"]
    assert a.delivered == []


def test_no_recipients_reports_actionable_message():
    a = _FakeAdapter("telegram", "c1", recipients=0)
    out = _run({"message": "hi", "_profile": "p"}, registry=_FakeRegistry([a]))
    assert out["delivered"] is False
    assert out["total_recipients"] == 0
    assert out["channels"][0]["status"] == "no_recipients"
    assert "message" in out  # actionable "ask the user to /start" hint


def test_transport_error_on_one_channel_does_not_abort_others():
    bad = _FakeAdapter("telegram", "c1", raise_exc=RuntimeError("boom"))
    good = _FakeAdapter("slack", "c2", recipients=1)
    out = _run({"message": "hi", "_profile": "p"}, registry=_FakeRegistry([bad, good]))
    assert out["delivered"] is True  # the good channel still delivered
    by_type = {c["channel_type"]: c for c in out["channels"]}
    assert by_type["telegram"]["status"] == "error"
    assert "boom" in by_type["telegram"]["error"]
    assert by_type["slack"]["status"] == "sent"
    # An error on one channel must NOT produce the "no recipients" hint.
    assert "message" not in out
