# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "python-dotenv",
#   "requests",
#   "websocket-client>=1.6",
# ]
# ///
"""Unit tests: entity-filter matching, state_changed classification, dedup, sanitize.

`_handle_state_changed` is driven with `_write_event` / `_save_state` monkeypatched —
no network, no real files.

Run standalone:  python scripts/tests/test_listener.py
Or via pytest:   pytest scripts/tests/test_listener.py
"""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from app import config, listener  # noqa: E402


def _evt(entity_id="light.kitchen", new="on", old="off",
         last_updated="2026-06-14T10:00:00+00:00", new_state_none=False):
    new_state = None if new_state_none else {
        "state": new,
        "last_changed": last_updated,
        "last_updated": last_updated,
        "attributes": {"friendly_name": "Kitchen Light"},
    }
    return {"entity_id": entity_id, "old_state": {"state": old}, "new_state": new_state}


def _run_handle(data, state, *, entity_filter=None):
    writes = []
    orig_write = listener._write_event
    orig_save = listener._save_state
    orig_filter = config.HA_ENTITY_FILTER
    config.HA_ENTITY_FILTER = entity_filter or []
    listener._write_event = lambda events_dir, entity, et: (
        writes.append((entity["entity_id"], et)) or Path("x.md")
    )
    listener._save_state = lambda s: None
    try:
        wrote = listener._handle_state_changed(data, state)
    finally:
        listener._write_event = orig_write
        listener._save_state = orig_save
        config.HA_ENTITY_FILTER = orig_filter
    return wrote, writes


# --- entity filter ---

def test_entity_matches_empty_is_all():
    assert listener._entity_matches("light.kitchen", []) is True


def test_entity_matches_glob():
    assert listener._entity_matches("light.kitchen", ["light.*"]) is True
    assert listener._entity_matches("sensor.cpu", ["light.*"]) is False


def test_entity_matches_multiple_and_exact():
    assert listener._entity_matches("switch.fan", ["light.*", "switch.*"]) is True
    assert listener._entity_matches("light.kitchen", ["light.kitchen"]) is True


# --- classification / dedup ---

def test_emits_classified_event():
    state = listener._empty_state()
    wrote, writes = _run_handle(_evt(), state)
    assert wrote == "light_turned_on"
    assert writes == [("light.kitchen", "light_turned_on")]


def test_skips_filtered_entity():
    state = listener._empty_state()
    wrote, writes = _run_handle(_evt(entity_id="sensor.cpu"), state, entity_filter=["light.*"])
    assert wrote is None
    assert writes == []


def test_skips_removed_entity():
    state = listener._empty_state()
    wrote, _ = _run_handle(_evt(new_state_none=True), state)
    assert wrote is None


def test_dedup_same_last_updated():
    state = listener._empty_state()
    wrote1, _ = _run_handle(_evt(), state)
    wrote2, _ = _run_handle(_evt(), state)
    assert wrote1 == "light_turned_on"
    assert wrote2 is None, "same last_updated must dedup"


def test_emits_on_newer_timestamp():
    state = listener._empty_state()
    _run_handle(_evt(last_updated="2026-06-14T10:00:00+00:00"), state)
    wrote, _ = _run_handle(_evt(new="off", last_updated="2026-06-14T10:05:00+00:00"), state)
    assert wrote == "light_turned_off"


# --- filename sanitize ---

def test_sanitize_plain():
    assert listener._sanitize("Kitchen Light") == "Kitchen Light"


def test_sanitize_strips_separators():
    assert "/" not in listener._sanitize("a/b")
    assert "\\" not in listener._sanitize("a\\b")


def test_sanitize_empty_and_reserved():
    assert listener._sanitize("") == "entity"
    assert listener._sanitize("con").startswith("_")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK: entity filter, classification, dedup, sanitize.")
