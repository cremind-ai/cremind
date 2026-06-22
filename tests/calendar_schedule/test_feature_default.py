"""The Calendar & Schedule feature flag defaults to ON (only explicit 'false' opts out)."""

from __future__ import annotations

import app.calendar.feature as feature


class _FakeManager:
    def __init__(self, meta):
        self._meta = meta

    def get_meta(self, tool_id, profile):
        return dict(self._meta)


def _wire(monkeypatch, meta):
    monkeypatch.setattr(feature, "_manager", lambda: _FakeManager(meta))


def test_default_on_when_unset(monkeypatch):
    _wire(monkeypatch, {})  # no row at all
    assert feature.is_enabled("alice") is True


def test_explicit_true(monkeypatch):
    _wire(monkeypatch, {"calendar_schedule_enabled": "true"})
    assert feature.is_enabled("alice") is True


def test_explicit_false_opts_out(monkeypatch):
    _wire(monkeypatch, {"calendar_schedule_enabled": "false"})
    assert feature.is_enabled("alice") is False


def test_empty_profile_is_off(monkeypatch):
    _wire(monkeypatch, {})
    assert feature.is_enabled("") is False
