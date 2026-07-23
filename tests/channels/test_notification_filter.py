"""Unit tests for the notification-mode filter (app/channels/notification_filter.py).

Covers the ``matches()`` truth table across every dimension, quiet-hours
(including a midnight-crossing window, injected via ``now`` for determinism),
and strict ``normalize_notification_filter`` validation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.channels.notification_filter as nf
from app.channels.notification_filter import (
    NotificationFilter,
    default_filter,
    format_notification,
    normalize_notification_filter,
)


def _entry(**kw):
    base = {
        "kind": "event_run_completed",
        "priority": "normal",
        "conversation_title": "Nightly backup",
        "message_preview": "Backed up 3 files",
        "created_at": 0,
    }
    base.update(kw)
    return base


def _filter(**filt) -> NotificationFilter:
    return NotificationFilter.parse({"notification_filter": filt})


# ── defaults ──────────────────────────────────────────────────────────────────

def test_default_forwards_event_run_but_drops_noise_and_otp():
    f = NotificationFilter.parse(None)
    assert f.matches(_entry(kind="event_run_completed")) is True
    assert f.matches(_entry(kind="started")) is False
    assert f.matches(_entry(kind="channel_otp", priority="high")) is False


def test_default_filter_dict_shape():
    d = default_filter()
    assert d["min_priority"] == "all"
    assert d["exclude_kinds"] == ["started", "channel_otp"]
    assert d["quiet_hours"]["enabled"] is False


# ── hard OTP guard ──────────────────────────────────────────────────────────────

def test_otp_always_dropped_even_when_allowlisted():
    f = _filter(kinds=["channel_otp"], exclude_kinds=[])
    assert f.matches(_entry(kind="channel_otp", priority="high")) is False


# ── kind allow / deny ────────────────────────────────────────────────────────────

def test_explicit_empty_exclude_forwards_started():
    f = _filter(exclude_kinds=[])
    assert f.matches(_entry(kind="started")) is True


def test_allowlist_overrides_exclude():
    f = _filter(kinds=["event_run_failed"], exclude_kinds=[])
    assert f.matches(_entry(kind="event_run_failed", priority="high")) is True
    assert f.matches(_entry(kind="event_run_completed")) is False


# ── priority ─────────────────────────────────────────────────────────────────────

def test_min_priority_high_drops_normal_keeps_high():
    f = _filter(min_priority="high")
    assert f.matches(_entry(kind="event_run_completed", priority="normal")) is False
    assert f.matches(_entry(kind="event_run_failed", priority="high")) is True


# ── source kinds ─────────────────────────────────────────────────────────────────

def test_source_kinds_allowlist():
    f = _filter(source_kinds=["schedule"])
    assert f.matches(_entry(source_kind="schedule")) is True
    assert f.matches(_entry(source_kind="file_watcher")) is False
    # An entry that carries no source_kind fails a set source filter.
    assert f.matches(_entry()) is False


# ── specific automation / conversation ───────────────────────────────────────────

def test_subscription_and_conversation_allowlists():
    f = _filter(subscription_ids=["sub-1"], conversation_ids=["conv-1"])
    assert f.matches(_entry(subscription_id="sub-1", conversation_id="conv-1")) is True
    assert f.matches(_entry(subscription_id="sub-2", conversation_id="conv-1")) is False
    assert f.matches(_entry(subscription_id="sub-1", conversation_id="conv-2")) is False


# ── keywords ─────────────────────────────────────────────────────────────────────

def test_keywords_any_case_insensitive_over_title_and_preview():
    f = _filter(keywords=["BACKUP"], keywords_mode="any")
    assert f.matches(_entry(conversation_title="Nightly backup")) is True
    f2 = _filter(keywords=["deploy"], keywords_mode="any")
    assert f2.matches(_entry(conversation_title="Nightly backup", message_preview="ok")) is False


def test_keywords_all_requires_every_word():
    f = _filter(keywords=["backup", "files"], keywords_mode="all")
    assert f.matches(_entry(message_preview="Backed up 3 files")) is True
    assert f.matches(_entry(message_preview="Backed up 3 things")) is False


# ── quiet hours ──────────────────────────────────────────────────────────────────

def test_quiet_hours_midnight_crossing():
    f = _filter(quiet_hours={"enabled": True, "start": "22:00", "end": "07:00", "allow_high": True})
    at_2330 = datetime(2026, 1, 1, 23, 30)
    at_noon = datetime(2026, 1, 1, 12, 0)
    # Inside the window: normal is muted, high still passes.
    assert f.matches(_entry(priority="normal"), now=at_2330) is False
    assert f.matches(_entry(priority="high", kind="event_run_failed"), now=at_2330) is True
    # Outside the window: delivered.
    assert f.matches(_entry(priority="normal"), now=at_noon) is True


def test_quiet_hours_no_allow_high_mutes_high_too():
    f = _filter(quiet_hours={"enabled": True, "start": "22:00", "end": "07:00", "allow_high": False})
    at_2330 = datetime(2026, 1, 1, 23, 30)
    assert f.matches(_entry(priority="high", kind="event_run_failed"), now=at_2330) is False


def test_quiet_hours_same_start_end_is_no_window():
    f = _filter(quiet_hours={"enabled": True, "start": "09:00", "end": "09:00"})
    assert f.matches(_entry(), now=datetime(2026, 1, 1, 9, 0)) is True


# ── normalization / validation ───────────────────────────────────────────────────

def test_normalize_defaults_and_exclude_default():
    d = normalize_notification_filter({})
    assert d["min_priority"] == "all"
    assert d["exclude_kinds"] == ["started", "channel_otp"]
    assert d["keywords_mode"] == "any"


def test_normalize_explicit_empty_exclude_is_kept():
    d = normalize_notification_filter({"exclude_kinds": []})
    assert d["exclude_kinds"] == []


def test_normalize_dedupes_and_caps_lists():
    d = normalize_notification_filter({"kinds": ["a", "a", "b"]})
    assert d["kinds"] == ["a", "b"]
    d2 = normalize_notification_filter({"subscription_ids": [str(i) for i in range(200)]})
    assert len(d2["subscription_ids"]) == 100


def test_normalize_strips_unknown_keys():
    d = normalize_notification_filter({"bogus": 1, "min_priority": "high"})
    assert "bogus" not in d
    assert d["min_priority"] == "high"


def test_normalize_accepts_comma_string_lists():
    d = normalize_notification_filter({"source_kinds": "schedule, file_watcher"})
    assert d["source_kinds"] == ["schedule", "file_watcher"]


@pytest.mark.parametrize("bad", [
    {"min_priority": "urgent"},
    {"keywords_mode": "both"},
    {"source_kinds": ["schedule", "bogus_source"]},
    {"quiet_hours": {"enabled": True, "start": "25:00", "end": "07:00"}},
    {"quiet_hours": {"enabled": True, "start": "22:00", "end": "07:00", "tz": "Mars/Phobos"}},
])
def test_normalize_rejects_invalid(bad):
    with pytest.raises(ValueError):
        normalize_notification_filter(bad)


# ── formatting ───────────────────────────────────────────────────────────────────

def test_format_notification_includes_title_and_preview():
    out = format_notification(_entry(kind="event_run_failed", priority="high"))
    assert "Nightly backup" in out
    assert "Backed up 3 files" in out
    assert "❌" in out  # failure emoji
    assert "🔴" in out  # high-priority marker


def test_format_notification_footer_uses_profile_timezone(monkeypatch):
    # created_at=0 is 1970-01-01 00:00 UTC; in a +7 zone that is 07:00. The
    # footer must render in the profile's resolved zone, not the OS process zone
    # (which is UTC on the Docker/VPS image — the reported bug rendered 00:00).
    monkeypatch.setattr(nf, "resolve_tzinfo", lambda profile: timezone(timedelta(hours=7)))
    out = format_notification(
        _entry(kind="event_run_completed", source_kind="schedule", created_at=0, profile="admin")
    )
    assert "_Schedule · 07:00_" in out


def test_format_notification_footer_falls_back_gracefully_without_profile(monkeypatch):
    # No profile on the entry → resolve_tzinfo(None) still yields a usable zone;
    # the footer must render a HH:MM string rather than crash or come out empty.
    monkeypatch.setattr(nf, "resolve_tzinfo", lambda profile: timezone.utc)
    out = format_notification(_entry(source_kind="schedule", created_at=0))
    assert "_Schedule · 00:00_" in out
