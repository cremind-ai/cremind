"""End-to-end wiring check: a schedule event's seeded ``next_fire_at`` reflects
the profile's configured timezone, not the process OS zone.

This is the core of the timezone bug fix — it exercises
``InternalCalendarProvider.create_event`` -> ``_seed_next_fire_at`` ->
``recurrence.to_epoch`` with a resolved zone.
"""

from __future__ import annotations

import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import app.calendar.provider as P


class _FakeStore:
    def __init__(self):
        self.inserted = None

    def insert(self, **kwargs):
        row = {"id": "row1", **kwargs}
        self.inserted = row
        return row


def _internal_provider():
    prov = P.InternalCalendarProvider()
    prov._store = _FakeStore()
    prov._manager = lambda: types.SimpleNamespace(arm=lambda row: None)
    return prov


def test_seed_next_fire_at_uses_configured_zone(monkeypatch):
    # Pin the resolver to New York regardless of the host OS zone.
    ny = ZoneInfo("America/New_York")
    monkeypatch.setattr(P, "resolve_tzinfo", lambda profile: ny)

    prov = _internal_provider()
    # A one-shot far in the future so it seeds (start >= now) deterministically.
    row = prov.create_event(
        profile="alice", conversation_id="c1", title="standup",
        schedule_kind="instant", dtstart="2027-03-10T09:00:00",
    )

    # 09:00 in New York (EDT, UTC-4 on 2027-03-10) is the absolute instant stored.
    expected = datetime(2027, 3, 10, 9, 0, 0, tzinfo=ny).timestamp()
    assert row["next_fire_at"] == expected
    assert row["status"] == "active"


def test_seed_next_fire_at_differs_by_zone(monkeypatch):
    prov_ny = _internal_provider()
    monkeypatch.setattr(P, "resolve_tzinfo", lambda profile: ZoneInfo("America/New_York"))
    ny = prov_ny.create_event(
        profile="alice", conversation_id="c1", title="x",
        schedule_kind="instant", dtstart="2027-03-10T09:00:00",
    )["next_fire_at"]

    prov_tk = _internal_provider()
    monkeypatch.setattr(P, "resolve_tzinfo", lambda profile: ZoneInfo("Asia/Tokyo"))
    tk = prov_tk.create_event(
        profile="bob", conversation_id="c1", title="x",
        schedule_kind="instant", dtstart="2027-03-10T09:00:00",
    )["next_fire_at"]

    # Same wall-clock 09:00, different zones -> different absolute instants.
    assert ny != tk
