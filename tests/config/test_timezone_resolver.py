"""Unit tests for the configurable system-timezone resolver.

Pins the resolution chain in :func:`app.config.timezone.resolve_tzinfo`:

    profile's own setting > admin profile's setting > CREMIND_TIMEZONE env > OS

and the admin-inheritance rule (admin only fills in for profiles that have
*never* set their own). Also covers the ``system.timezone`` field's write-time
IANA validation.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

import app.config.timezone as tzmod
from app.config.config_schema import lookup
from app.config.timezone import resolve_tzinfo


def _key(tz) -> str | None:
    """The IANA key of a resolved zone (None for a bare OS-offset tzinfo)."""
    return getattr(tz, "key", None)


@pytest.fixture
def stored(monkeypatch):
    """Fake the per-profile user_config store. Populate ``rows[profile] = value``;
    a missing profile key means "never set" (get_dynamic returns None)."""
    rows: dict[str, str] = {}

    def fake_get_dynamic(table, key, default=None, profile=None):
        assert table == "user_config"
        assert key == "system.timezone"
        return rows.get(profile, default)

    monkeypatch.setattr(tzmod, "get_dynamic", fake_get_dynamic)
    # Clean env so cases that don't set it hit the OS fallback deterministically.
    monkeypatch.delenv("CREMIND_TIMEZONE", raising=False)
    return rows


def test_own_iana_wins(stored):
    stored["alice"] = "Asia/Tokyo"
    assert _key(resolve_tzinfo("alice")) == "Asia/Tokyo"


def test_never_set_inherits_admin(stored):
    stored["admin"] = "America/New_York"
    # bob has no row -> inherits admin
    assert _key(resolve_tzinfo("bob")) == "America/New_York"


def test_own_value_blocks_admin_override(stored):
    stored["admin"] = "America/New_York"
    stored["bob"] = "Europe/London"
    assert _key(resolve_tzinfo("bob")) == "Europe/London"


def test_own_auto_does_not_inherit_admin(stored, monkeypatch):
    # An explicit "auto" is still "the profile set something", so the admin
    # default must NOT apply — it falls to env/OS instead.
    stored["admin"] = "America/New_York"
    stored["bob"] = "auto"
    monkeypatch.setenv("CREMIND_TIMEZONE", "UTC")
    assert _key(resolve_tzinfo("bob")) == "UTC"


def test_env_var_used_when_nothing_set(stored, monkeypatch):
    monkeypatch.setenv("CREMIND_TIMEZONE", "Asia/Kolkata")
    assert _key(resolve_tzinfo("bob")) == "Asia/Kolkata"


def test_missing_admin_degrades_to_env(stored, monkeypatch):
    monkeypatch.setenv("CREMIND_TIMEZONE", "Europe/Paris")
    # No admin row, no bob row -> env
    assert _key(resolve_tzinfo("bob")) == "Europe/Paris"


def test_invalid_own_value_degrades_to_env(stored, monkeypatch):
    stored["bob"] = "Not/AZone"
    monkeypatch.setenv("CREMIND_TIMEZONE", "UTC")
    assert _key(resolve_tzinfo("bob")) == "UTC"


def test_admin_never_self_inherits(stored, monkeypatch):
    # admin with no row must not recurse; falls to env/OS.
    monkeypatch.setenv("CREMIND_TIMEZONE", "UTC")
    assert _key(resolve_tzinfo("admin")) == "UTC"


def test_admin_own_value(stored):
    stored["admin"] = "Asia/Ho_Chi_Minh"
    assert _key(resolve_tzinfo("admin")) == "Asia/Ho_Chi_Minh"


def test_no_profile_uses_env(stored, monkeypatch):
    monkeypatch.setenv("CREMIND_TIMEZONE", "UTC")
    assert _key(resolve_tzinfo(None)) == "UTC"


def test_os_fallback_never_raises(stored):
    # Nothing set, no env -> a concrete tzinfo (OS zone), no exception.
    tz = resolve_tzinfo("bob")
    assert tz is not None
    # A bare OS-offset zone has no IANA key but is a usable tzinfo.
    from datetime import datetime
    assert datetime(2026, 7, 21, 9, 0).replace(tzinfo=tz).utcoffset() is not None


# ── Field.validate (write-time IANA validation) ──────────────────────────────

def test_field_validate_accepts_iana_and_auto():
    _, _, field = lookup("system.timezone")
    field.validate("Asia/Tokyo")   # no raise
    field.validate("UTC")          # no raise
    field.validate("auto")         # sentinel, no raise


def test_field_validate_rejects_bad_zone():
    _, _, field = lookup("system.timezone")
    with pytest.raises(ValueError):
        field.validate("Mars/Phobos")


def test_field_coerce_keeps_string():
    _, _, field = lookup("system.timezone")
    assert field.coerce("Asia/Tokyo") == "Asia/Tokyo"
    assert field.coerce("auto") == "auto"
