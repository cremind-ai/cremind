"""Who the agent may message without asking — :mod:`app.channels.send_policy`.

Pure decision logic, so these tests are the cheapest place to pin the precedence
rules the whole feature rests on: a client's own override beats the profile
setting, and a recipient nobody has messaged before overrides both.
"""

from __future__ import annotations

import pytest

from app.channels import send_policy as sp


def _sender(mode=None, **kw):
    return {"sender_id": "s1", "display_name": "Lee", "send_confirmation": mode, **kw}


# ── override normalization ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("required", "required"),
        ("skip", "skip"),
        ("REQUIRED", "required"),   # tolerate case from a hand-written API call
        ("  skip  ", "skip"),
        (None, None),               # never set
        ("", None),                 # cleared
        ("default", None),          # the CLI word for "inherit"
        ("nonsense", None),
        (True, None),
    ],
)
def test_normalize_override(raw, expected):
    assert sp.normalize_override(raw) == expected


# ── precedence ─────────────────────────────────────────────────────────────


def test_profile_default_applies_when_no_override():
    assert sp.requires_confirmation(
        profile_default=True, sender=_sender(), cold=False,
    ) is True
    assert sp.requires_confirmation(
        profile_default=False, sender=_sender(), cold=False,
    ) is False


def test_client_skip_beats_a_profile_that_wants_confirmation():
    """The user's whole ask: exempt one client while the default stays on."""
    assert sp.requires_confirmation(
        profile_default=True, sender=_sender("skip"), cold=False,
    ) is False


def test_client_required_beats_a_profile_that_skips():
    """Symmetric: keep one client guarded even with the global switch off."""
    assert sp.requires_confirmation(
        profile_default=False, sender=_sender("required"), cold=False,
    ) is True


def test_unparseable_override_falls_back_to_the_profile():
    assert sp.requires_confirmation(
        profile_default=False, sender=_sender("garbage"), cold=False,
    ) is False


# ── the case that overrides everything ─────────────────────────────────────


def test_cold_recipient_always_confirms():
    """Messaging a stranger is the send you cannot take back."""
    for default in (True, False):
        assert sp.requires_confirmation(
            profile_default=default, sender=None, cold=True,
        ) is True


def test_cold_wins_even_over_an_explicit_skip():
    assert sp.requires_confirmation(
        profile_default=False, sender=_sender("skip"), cold=True,
    ) is True


def test_missing_sender_row_confirms():
    """No client record means nothing could have exempted them."""
    assert sp.requires_confirmation(
        profile_default=False, sender=None, cold=False,
    ) is True


# ── reasons shown in the preview ───────────────────────────────────────────


def test_describe_names_the_actual_reason():
    assert "never messaged" in sp.describe(None, cold=True)
    assert "never messaged" in sp.describe(_sender("skip"), cold=True)
    assert "always ask" in sp.describe(_sender("required"), cold=False)
    assert "profile" in sp.describe(_sender(), cold=False)


# ── the profile accessor ───────────────────────────────────────────────────


def test_profile_default_is_on_and_fails_safe(monkeypatch):
    import app.config.user_config as uc

    monkeypatch.setattr(uc, "get_user_config", lambda key, profile: False)
    assert sp.confirm_before_send_default("p1") is False

    # An unreadable setting must not become "send freely".
    def _boom(key, profile):
        raise RuntimeError("config storage down")

    monkeypatch.setattr(uc, "get_user_config", _boom)
    assert sp.confirm_before_send_default("p1") is True


def test_no_profile_confirms():
    assert sp.confirm_before_send_default("") is True
