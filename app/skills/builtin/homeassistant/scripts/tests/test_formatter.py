# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Unit tests: event markdown formatting + attribute truncation.

Run standalone:  python scripts/tests/test_formatter.py
Or via pytest:   pytest scripts/tests/test_formatter.py
"""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from app import formatter  # noqa: E402


def _entity(**over):
    base = {
        "entity_id": "light.kitchen",
        "friendly_name": "Kitchen Light",
        "domain": "light",
        "state": "on",
        "previous_state": "off",
        "last_changed": "2026-06-14T10:00:00+00:00",
        "last_updated": "2026-06-14T10:00:00+00:00",
        "attributes": {"brightness": 255},
    }
    base.update(over)
    return base


def test_frontmatter_keys_present():
    md = formatter.format_event_markdown(_entity(), event_type="light_turned_on")
    assert md.startswith("---\n")
    assert "entity_id: light.kitchen" in md
    assert "domain: light" in md
    assert "event_type: light_turned_on" in md


def test_transition_sentence():
    md = formatter.format_event_markdown(_entity(), event_type="light_turned_on")
    assert "Kitchen Light changed from off to on." in md


def test_attributes_are_json_string():
    md = formatter.format_event_markdown(_entity(), event_type="light_turned_on")
    # JSON object embedded as a quoted YAML scalar.
    assert "attributes:" in md
    assert "brightness" in md


def test_attributes_truncated():
    md = formatter.format_event_markdown(
        _entity(attributes={"blob": "x" * 5000}), event_type="light_turned_on"
    )
    assert "(truncated)" in md


def test_missing_previous_state_says_unknown():
    md = formatter.format_event_markdown(
        _entity(previous_state=""), event_type="light_turned_on"
    )
    assert "changed from unknown to on." in md


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK: event markdown + attribute truncation.")
