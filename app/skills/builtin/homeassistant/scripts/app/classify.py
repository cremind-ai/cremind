"""Classify a Home Assistant `state_changed` transition into a granular event type.

A single raw `state_changed` event carries (entity_id, old_state, new_state). We
map it to a specific, semantically meaningful event type based on the entity's
domain, its `device_class`, and the nature of the transition — so subscribers can
react to "a door opened" or "motion was detected" rather than a generic change.

`EVENT_TYPES` is the canonical, ordered list of every event type this skill can
emit. It drives the event drop-zone folders, the SKILL.md `events` metadata, and
this module's `classify()` return values (kept in lockstep).
"""
from __future__ import annotations

from typing import Optional

# States that mean "no real value" (HA uses these for offline/unknown entities).
_UNAVAILABLE = {"unavailable", "unknown", "none", ""}

# binary_sensor device_class groupings.
_BINARY_MOTION = {"motion", "moving"}
_BINARY_OCCUPANCY = {"occupancy", "presence"}
_BINARY_DOOR = {"door", "garage_door"}
_BINARY_WINDOW = {"window", "opening"}
_BINARY_MOISTURE = {"moisture"}
_BINARY_SMOKE = {"smoke", "gas", "carbon_monoxide"}

# sensor device_class -> event type.
_SENSOR_CLASS = {
    "temperature": "temperature_changed",
    "humidity": "humidity_changed",
    "power": "power_changed",
    "energy": "power_changed",
    "battery": "battery_level_changed",
}

# Canonical ordered list + human descriptions (used to build SKILL.md metadata).
DESCRIPTIONS: dict[str, str] = {
    "became_unavailable": "An entity went offline / its state became unavailable or unknown",
    "became_available": "An entity came back online from an unavailable/unknown state",
    "light_turned_on": "A light was turned on",
    "light_turned_off": "A light was turned off",
    "switch_turned_on": "A switch was turned on",
    "switch_turned_off": "A switch was turned off",
    "fan_turned_on": "A fan was turned on",
    "fan_turned_off": "A fan was turned off",
    "input_boolean_turned_on": "An input_boolean helper was turned on",
    "input_boolean_turned_off": "An input_boolean helper was turned off",
    "lock_locked": "A lock was locked",
    "lock_unlocked": "A lock was unlocked",
    "cover_opened": "A cover (garage door, blind, shade) finished opening",
    "cover_closed": "A cover finished closing",
    "cover_opening": "A cover started opening",
    "cover_closing": "A cover started closing",
    "motion_detected": "A motion sensor detected motion",
    "motion_cleared": "A motion sensor cleared (no more motion)",
    "occupancy_detected": "An occupancy/presence sensor detected presence",
    "occupancy_cleared": "An occupancy/presence sensor cleared",
    "door_opened": "A door (or garage door) binary sensor reports open",
    "door_closed": "A door (or garage door) binary sensor reports closed",
    "window_opened": "A window/opening binary sensor reports open",
    "window_closed": "A window/opening binary sensor reports closed",
    "moisture_detected": "A leak/moisture sensor detected water",
    "moisture_cleared": "A leak/moisture sensor cleared",
    "smoke_detected": "A smoke/gas/CO sensor triggered",
    "smoke_cleared": "A smoke/gas/CO sensor cleared",
    "binary_sensor_on": "A binary_sensor (no specific device_class) turned on",
    "binary_sensor_off": "A binary_sensor (no specific device_class) turned off",
    "person_arrived_home": "A person arrived home",
    "person_left_home": "A person left home",
    "person_location_changed": "A person moved to a different (non-home) zone",
    "device_arrived_home": "A device tracker arrived home",
    "device_left_home": "A device tracker left home",
    "climate_changed": "A climate/thermostat entity changed (mode or target)",
    "alarm_armed": "An alarm control panel was armed (home/away/night)",
    "alarm_disarmed": "An alarm control panel was disarmed",
    "alarm_triggered": "An alarm control panel was triggered",
    "media_started_playing": "A media player started playing",
    "media_paused": "A media player was paused",
    "media_stopped": "A media player stopped / went idle / turned off",
    "temperature_changed": "A temperature sensor value changed",
    "humidity_changed": "A humidity sensor value changed",
    "power_changed": "A power/energy sensor value changed",
    "battery_level_changed": "A battery sensor value changed",
    "sensor_value_changed": "A sensor (no specific device_class) value changed",
    "state_changed": "An entity changed state and matched no more specific event type",
}

EVENT_TYPES: list[str] = list(DESCRIPTIONS.keys())


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


def classify(
    entity_id: str,
    attributes: Optional[dict],
    old_state: Optional[str],
    new_state: Optional[str],
) -> Optional[str]:
    """Return the event type for a transition, or None to skip it.

    `old_state` / `new_state` are the raw HA state strings (e.g. "on", "home",
    "playing"); None is treated as no value.
    """
    domain = _domain(entity_id)
    new = (new_state or "").strip().lower()
    old = (old_state or "").strip().lower()
    attrs = attributes or {}

    # 1) Availability transitions (cross-domain, highest priority).
    new_unavail = new in _UNAVAILABLE
    old_unavail = old in _UNAVAILABLE
    if new_unavail and not old_unavail:
        return "became_unavailable"
    if old_unavail and not new_unavail and old != "":
        return "became_available"
    if new_unavail:
        return None  # unavailable<->unknown churn, or a brand-new entity with no value

    device_class = str(attrs.get("device_class") or "").lower()

    # 2) Domain-specific semantics.
    if domain == "light":
        return "light_turned_on" if new == "on" else "light_turned_off" if new == "off" else "state_changed"
    if domain == "switch":
        return "switch_turned_on" if new == "on" else "switch_turned_off" if new == "off" else "state_changed"
    if domain == "fan":
        return "fan_turned_on" if new == "on" else "fan_turned_off" if new == "off" else "state_changed"
    if domain == "input_boolean":
        return "input_boolean_turned_on" if new == "on" else "input_boolean_turned_off" if new == "off" else "state_changed"
    if domain == "lock":
        if new == "locked":
            return "lock_locked"
        if new == "unlocked":
            return "lock_unlocked"
        return "state_changed"
    if domain == "cover":
        return {
            "open": "cover_opened",
            "closed": "cover_closed",
            "opening": "cover_opening",
            "closing": "cover_closing",
        }.get(new, "state_changed")
    if domain == "binary_sensor":
        on = new == "on"
        if device_class in _BINARY_MOTION:
            return "motion_detected" if on else "motion_cleared"
        if device_class in _BINARY_OCCUPANCY:
            return "occupancy_detected" if on else "occupancy_cleared"
        if device_class in _BINARY_DOOR:
            return "door_opened" if on else "door_closed"
        if device_class in _BINARY_WINDOW:
            return "window_opened" if on else "window_closed"
        if device_class in _BINARY_MOISTURE:
            return "moisture_detected" if on else "moisture_cleared"
        if device_class in _BINARY_SMOKE:
            return "smoke_detected" if on else "smoke_cleared"
        return "binary_sensor_on" if on else "binary_sensor_off"
    if domain == "person":
        if new == "home":
            return "person_arrived_home"
        if new == "not_home":
            return "person_left_home"
        return "person_location_changed"
    if domain == "device_tracker":
        if new == "home":
            return "device_arrived_home"
        if new == "not_home":
            return "device_left_home"
        return "state_changed"
    if domain == "climate":
        return "climate_changed"
    if domain == "alarm_control_panel":
        if new == "triggered":
            return "alarm_triggered"
        if new == "disarmed":
            return "alarm_disarmed"
        if new.startswith("armed"):
            return "alarm_armed"
        return "state_changed"  # arming / pending
    if domain == "media_player":
        if new == "playing":
            return "media_started_playing"
        if new == "paused":
            return "media_paused"
        if new in ("idle", "off", "standby"):
            return "media_stopped"
        return "state_changed"
    if domain == "sensor":
        return _SENSOR_CLASS.get(device_class, "sensor_value_changed")

    # 3) Fallback for any other domain.
    return "state_changed"
