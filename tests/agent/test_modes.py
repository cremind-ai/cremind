"""``normalize_mode`` — the plan/reasoning/instant selector with back-compat.

An explicit valid mode is honored; anything else (None, unknown string) is
derived from the legacy ``reasoning`` boolean rather than raising.
"""

from __future__ import annotations

from app.agent.modes import normalize_mode


def test_explicit_modes_are_honored():
    assert normalize_mode("plan") == "plan"
    assert normalize_mode("reasoning") == "reasoning"
    assert normalize_mode("instant") == "instant"


def test_absent_mode_derives_from_reasoning_flag():
    assert normalize_mode(None, reasoning=True) == "reasoning"
    assert normalize_mode(None, reasoning=False) == "instant"


def test_unknown_mode_degrades_not_raises():
    assert normalize_mode("bogus", reasoning=True) == "reasoning"
    assert normalize_mode("bogus", reasoning=False) == "instant"
    assert normalize_mode("", reasoning=True) == "reasoning"


def test_plan_is_never_derived_from_boolean():
    # The boolean can only produce reasoning/instant; plan must be explicit.
    assert normalize_mode(None, reasoning=True) != "plan"
    assert normalize_mode(None, reasoning=False) != "plan"
