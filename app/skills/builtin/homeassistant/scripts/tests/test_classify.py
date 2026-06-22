# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Unit tests: granular state_changed classification.

Run standalone:  python scripts/tests/test_classify.py
Or via pytest:   pytest scripts/tests/test_classify.py
"""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from app import classify  # noqa: E402


def c(entity_id, new, old="off", attrs=None):
    return classify.classify(entity_id, attrs or {}, old, new)


# --- availability (cross-domain, highest priority) ---

def test_became_unavailable():
    assert c("light.kitchen", "unavailable", old="on") == "became_unavailable"


def test_became_available():
    assert c("light.kitchen", "on", old="unavailable") == "became_available"


def test_unknown_to_unavailable_skipped():
    assert c("sensor.x", "unavailable", old="unknown") is None


def test_new_entity_with_no_value_skipped():
    # old is empty (brand-new entity) and new is unknown -> nothing meaningful
    assert c("sensor.x", "unknown", old="") is None


# --- on/off domains ---

def test_light_on_off():
    assert c("light.kitchen", "on") == "light_turned_on"
    assert c("light.kitchen", "off", old="on") == "light_turned_off"


def test_switch_and_fan_and_input_boolean():
    assert c("switch.fan", "on") == "switch_turned_on"
    assert c("fan.office", "off", old="on") == "fan_turned_off"
    assert c("input_boolean.guest", "on") == "input_boolean_turned_on"


# --- lock / cover ---

def test_lock():
    assert c("lock.front", "locked", old="unlocked") == "lock_locked"
    assert c("lock.front", "unlocked", old="locked") == "lock_unlocked"


def test_cover():
    assert c("cover.garage", "open", old="closed") == "cover_opened"
    assert c("cover.garage", "closing", old="open") == "cover_closing"


# --- binary_sensor by device_class ---

def test_binary_motion():
    assert c("binary_sensor.hall", "on", attrs={"device_class": "motion"}) == "motion_detected"
    assert c("binary_sensor.hall", "off", old="on", attrs={"device_class": "motion"}) == "motion_cleared"


def test_binary_door_window_leak_smoke():
    assert c("binary_sensor.front", "on", attrs={"device_class": "door"}) == "door_opened"
    assert c("binary_sensor.bed", "off", old="on", attrs={"device_class": "window"}) == "window_closed"
    assert c("binary_sensor.sink", "on", attrs={"device_class": "moisture"}) == "moisture_detected"
    assert c("binary_sensor.hall", "on", attrs={"device_class": "smoke"}) == "smoke_detected"


def test_binary_fallback():
    assert c("binary_sensor.x", "on", attrs={"device_class": "connectivity"}) == "binary_sensor_on"
    assert c("binary_sensor.x", "on") == "binary_sensor_on"  # no device_class


# --- presence ---

def test_person_and_device_tracker():
    assert c("person.alex", "home", old="not_home") == "person_arrived_home"
    assert c("person.alex", "not_home", old="home") == "person_left_home"
    assert c("person.alex", "Office", old="home") == "person_location_changed"
    assert c("device_tracker.phone", "home", old="not_home") == "device_arrived_home"
    assert c("device_tracker.phone", "not_home", old="home") == "device_left_home"


# --- alarm / media ---

def test_alarm():
    assert c("alarm_control_panel.home", "armed_away", old="disarmed") == "alarm_armed"
    assert c("alarm_control_panel.home", "disarmed", old="armed_away") == "alarm_disarmed"
    assert c("alarm_control_panel.home", "triggered", old="armed_away") == "alarm_triggered"


def test_media():
    assert c("media_player.tv", "playing", old="paused") == "media_started_playing"
    assert c("media_player.tv", "paused", old="playing") == "media_paused"
    assert c("media_player.tv", "idle", old="playing") == "media_stopped"


# --- sensors by device_class ---

def test_sensor_device_classes():
    assert c("sensor.out", "21.5", old="21.4", attrs={"device_class": "temperature"}) == "temperature_changed"
    assert c("sensor.hum", "55", old="54", attrs={"device_class": "humidity"}) == "humidity_changed"
    assert c("sensor.pwr", "120", old="118", attrs={"device_class": "power"}) == "power_changed"
    assert c("sensor.bat", "80", old="81", attrs={"device_class": "battery"}) == "battery_level_changed"
    assert c("sensor.misc", "x", old="y") == "sensor_value_changed"


# --- climate + generic fallback ---

def test_climate_and_fallback():
    assert c("climate.living", "heat", old="off") == "climate_changed"
    assert c("vacuum.rover", "cleaning", old="docked") == "state_changed"


# --- taxonomy integrity: every classify output is a declared EVENT_TYPE ---

def test_all_outputs_are_declared():
    samples = [
        ("light.k", "on"), ("light.k", "off"), ("switch.s", "on"), ("lock.l", "locked"),
        ("cover.c", "open"), ("binary_sensor.b", "on", {"device_class": "motion"}),
        ("person.p", "home"), ("alarm_control_panel.a", "triggered"),
        ("media_player.m", "playing"), ("sensor.s", "1", {"device_class": "temperature"}),
        ("climate.c", "heat"), ("vacuum.v", "cleaning"),
    ]
    declared = set(classify.EVENT_TYPES)
    for s in samples:
        eid, new = s[0], s[1]
        attrs = s[2] if len(s) > 2 else {}
        et = classify.classify(eid, attrs, "off", new)
        assert et in declared, f"{eid}->{new} produced undeclared {et!r}"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK: granular classification across domains + device classes.")
