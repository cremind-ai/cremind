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
    orig_upsert = listener._update_inventory
    orig_remove = listener._remove_from_inventory
    orig_names_upsert = listener._update_names_inventory
    orig_names_remove = listener._remove_from_names_inventory
    config.HA_ENTITY_FILTER = entity_filter or []
    listener._write_event = lambda events_dir, entity, et: (
        writes.append((entity["entity_id"], et)) or Path("x.md")
    )
    listener._save_state = lambda s: None
    # Keep these tests hermetic — don't touch the real devices.md / SKILL.md inventories.
    listener._update_inventory = lambda entity_id, new_state: None
    listener._remove_from_inventory = lambda entity_id: None
    listener._update_names_inventory = lambda entity_id, old_state, new_state: None
    listener._remove_from_names_inventory = lambda entity_id: None
    try:
        wrote = listener._handle_state_changed(data, state)
    finally:
        listener._write_event = orig_write
        listener._save_state = orig_save
        config.HA_ENTITY_FILTER = orig_filter
        listener._update_inventory = orig_upsert
        listener._remove_from_inventory = orig_remove
        listener._update_names_inventory = orig_names_upsert
        listener._remove_from_names_inventory = orig_names_remove
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


# --- devices.md inventory wiring ---

def test_inventory_upsert_on_change():
    """A fresh classified change updates the device's inventory line (before classify)."""
    state = listener._empty_state()
    calls = []
    orig_upsert = listener._update_inventory
    orig_write = listener._write_event
    orig_save = listener._save_state
    orig_names = listener._update_names_inventory
    listener._update_inventory = lambda entity_id, new_state: calls.append(entity_id)
    listener._write_event = lambda *a: Path("x.md")
    listener._save_state = lambda s: None
    listener._update_names_inventory = lambda entity_id, old_state, new_state: None
    try:
        listener._handle_state_changed(_evt(), state)
    finally:
        listener._update_inventory = orig_upsert
        listener._write_event = orig_write
        listener._save_state = orig_save
        listener._update_names_inventory = orig_names
    assert calls == ["light.kitchen"]


def test_inventory_remove_on_removed_entity():
    """A removed entity (new_state=None) is dropped from the inventory."""
    state = listener._empty_state()
    calls = []
    orig_remove = listener._remove_from_inventory
    orig_names_remove = listener._remove_from_names_inventory
    listener._remove_from_inventory = lambda entity_id: calls.append(entity_id)
    listener._remove_from_names_inventory = lambda entity_id: None
    try:
        listener._handle_state_changed(_evt(new_state_none=True), state)
    finally:
        listener._remove_from_inventory = orig_remove
        listener._remove_from_names_inventory = orig_names_remove
    assert calls == ["light.kitchen"]


# --- Device list (SKILL.md) name-index wiring ---

def test_names_inventory_rename_upserts():
    """A friendly-name change upserts the entity's line in the SKILL.md Device list."""
    calls = []
    orig = listener.device_names.upsert
    listener.device_names.upsert = lambda eid, name: calls.append((eid, name))
    try:
        old = {"state": "on", "attributes": {"friendly_name": "Old Name"}}
        new = {"state": "on", "attributes": {"friendly_name": "New Name"}}
        listener._update_names_inventory("light.kitchen", old, new)
    finally:
        listener.device_names.upsert = orig
    assert calls == [("light.kitchen", "New Name")]


def test_names_inventory_state_only_change_is_untouched():
    """A plain state change (same name) writes nothing to the Device list — the low-churn guarantee."""
    calls = []
    orig = listener.device_names.upsert
    listener.device_names.upsert = lambda eid, name: calls.append((eid, name))
    try:
        old = {"state": "off", "attributes": {"friendly_name": "Kitchen Light"}}
        new = {"state": "on", "attributes": {"friendly_name": "Kitchen Light"}}
        listener._update_names_inventory("light.kitchen", old, new)
    finally:
        listener.device_names.upsert = orig
    assert calls == [], "state-only change must not touch the Device list"


def test_names_inventory_new_entity_upserts():
    """A brand-new entity (no old_state) is added to the SKILL.md Device list."""
    calls = []
    orig = listener.device_names.upsert
    listener.device_names.upsert = lambda eid, name: calls.append((eid, name))
    try:
        new = {"state": "on", "attributes": {"friendly_name": "Kitchen Light"}}
        listener._update_names_inventory("light.kitchen", None, new)
    finally:
        listener.device_names.upsert = orig
    assert calls == [("light.kitchen", "Kitchen Light")]


def test_names_inventory_no_friendly_name_is_untouched():
    """With no friendly_name on either side both names fall back to entity_id => no rewrite."""
    calls = []
    orig = listener.device_names.upsert
    listener.device_names.upsert = lambda eid, name: calls.append((eid, name))
    try:
        old = {"state": "off", "attributes": {}}
        new = {"state": "on", "attributes": {}}
        listener._update_names_inventory("light.kitchen", old, new)
    finally:
        listener.device_names.upsert = orig
    assert calls == [], "no friendly_name on either side => name unchanged (entity_id)"


def test_names_inventory_remove_on_removed_entity():
    """A removed entity (new_state=None) is dropped from the SKILL.md Device list."""
    state = listener._empty_state()
    calls = []
    orig_names_remove = listener._remove_from_names_inventory
    orig_dev_remove = listener._remove_from_inventory
    listener._remove_from_names_inventory = lambda eid: calls.append(eid)
    listener._remove_from_inventory = lambda eid: None
    try:
        listener._handle_state_changed(_evt(new_state_none=True), state)
    finally:
        listener._remove_from_names_inventory = orig_names_remove
        listener._remove_from_inventory = orig_dev_remove
    assert calls == ["light.kitchen"]


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
    print("OK: entity filter, classification, dedup, devices/device_names wiring, sanitize.")
