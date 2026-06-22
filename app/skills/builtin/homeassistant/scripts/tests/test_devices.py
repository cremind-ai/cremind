# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "python-dotenv",
# ]
# ///
"""Unit tests: references/devices.md rendering, full_sync, single-line upsert/remove.

`config.DEVICES_FILE` / `config.REFERENCES_DIR` are redirected to a temp dir so no real
inventory file is touched.

Run standalone:  python scripts/tests/test_devices.py
Or via pytest:   pytest scripts/tests/test_devices.py
"""
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from app import config, devices  # noqa: E402

# Redirect the inventory file into a throwaway temp dir for the whole module.
_TMP = Path(tempfile.mkdtemp(prefix="ha_devices_test_"))
config.REFERENCES_DIR = _TMP
config.DEVICES_FILE = _TMP / "devices.md"


def _clean():
    config.REFERENCES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        config.DEVICES_FILE.unlink()
    except FileNotFoundError:
        pass


def _row(entity_id, name, dtype, state):
    return {"entity_id": entity_id, "name": name, "dtype": dtype, "state": state}


def _device_lines():
    _, lines = devices._split_existing()
    return lines


# --- device_type ---

def test_device_type_domain_only():
    assert devices.device_type("light.kitchen", {}) == "light"
    assert devices.device_type("light.kitchen", None) == "light"


def test_device_type_with_device_class():
    assert devices.device_type("binary_sensor.front_door", {"device_class": "door"}) == "binary_sensor/door"
    assert devices.device_type("sensor.t", {"device_class": "temperature"}) == "sensor/temperature"


# --- sanitize / render ---

def test_sanitize_cell_strips_pipe_and_newlines():
    assert devices._sanitize_cell("a|b") == "a/b"
    assert devices._sanitize_cell("line1\nline2\tx   y") == "line1 line2 x y"
    assert devices._sanitize_cell("  trim me  ") == "trim me"
    assert devices._sanitize_cell(None) == ""


def test_render_line_format():
    assert devices._render_line("light.a", "Kitchen", "light", "on") == "light.a | Kitchen | light | on"


def test_row_from_state():
    row = devices.row_from_state({
        "entity_id": "binary_sensor.door",
        "state": "off",
        "attributes": {"friendly_name": "Front Door", "device_class": "door"},
    })
    assert row == {"entity_id": "binary_sensor.door", "name": "Front Door", "dtype": "binary_sensor/door", "state": "off"}


# --- full_sync ---

def test_full_sync_sorted_with_header_marker():
    _clean()
    devices.full_sync([
        _row("light.c", "C", "light", "on"),
        _row("light.a", "A", "light", "off"),
        _row("light.b", "B", "light", "on"),
    ])
    text = config.DEVICES_FILE.read_text(encoding="utf-8")
    assert devices.MARKER in text
    keys = [ln.split(" | ", 1)[0] for ln in _device_lines()]
    assert keys == ["light.a", "light.b", "light.c"]


def test_full_sync_empty_writes_header_only():
    _clean()
    devices.full_sync([])
    text = config.DEVICES_FILE.read_text(encoding="utf-8")
    assert devices.MARKER in text
    assert _device_lines() == []


# --- upsert ---

def test_upsert_update_in_place_keeps_others_byte_identical():
    _clean()
    devices.full_sync([
        _row("light.a", "A", "light", "on"),
        _row("light.b", "B", "light", "off"),
        _row("light.c", "C", "light", "on"),
    ])
    header_before, before = devices._split_existing()
    devices.upsert("light.b", "B", "light", "on")
    header_after, after = devices._split_existing()
    assert header_after == header_before          # header untouched
    assert after[0] == before[0]                  # light.a byte-identical
    assert after[2] == before[2]                  # light.c byte-identical
    assert after[1] == "light.b | B | light | on"  # only light.b changed
    assert before[1] != after[1]


def test_upsert_insert_keeps_sorted_order():
    _clean()
    devices.full_sync([
        _row("light.a", "A", "light", "on"),
        _row("light.c", "C", "light", "on"),
    ])
    devices.upsert("light.b", "B", "light", "off")
    keys = [ln.split(" | ", 1)[0] for ln in _device_lines()]
    assert keys == ["light.a", "light.b", "light.c"]


def test_upsert_append_largest_key():
    _clean()
    devices.full_sync([_row("light.a", "A", "light", "on")])
    devices.upsert("switch.z", "Z", "switch", "off")
    keys = [ln.split(" | ", 1)[0] for ln in _device_lines()]
    assert keys == ["light.a", "switch.z"]


def test_upsert_missing_file_creates_with_header():
    _clean()
    assert not config.DEVICES_FILE.exists()
    devices.upsert("light.a", "A", "light", "on")
    text = config.DEVICES_FILE.read_text(encoding="utf-8")
    assert devices.MARKER in text
    assert _device_lines() == ["light.a | A | light | on"]


def test_upsert_missing_marker_rebuilds():
    _clean()
    config.DEVICES_FILE.write_text("garbage with no marker\n", encoding="utf-8")
    devices.upsert("light.a", "A", "light", "on")
    text = config.DEVICES_FILE.read_text(encoding="utf-8")
    assert devices.MARKER in text
    assert "light.a | A | light | on" in text


def test_upsert_sanitizes_name_and_state():
    _clean()
    devices.upsert("sensor.note", "My | Note\nsensor", "sensor", "a\nb")
    line = _device_lines()[0]
    assert line == "sensor.note | My / Note sensor | sensor | a b"


# --- remove ---

def test_remove_drops_line():
    _clean()
    devices.full_sync([
        _row("light.a", "A", "light", "on"),
        _row("light.b", "B", "light", "off"),
    ])
    devices.remove("light.a")
    keys = [ln.split(" | ", 1)[0] for ln in _device_lines()]
    assert keys == ["light.b"]


def test_remove_absent_is_noop():
    _clean()
    devices.full_sync([_row("light.a", "A", "light", "on")])
    before = config.DEVICES_FILE.read_bytes()
    devices.remove("light.nonexistent")
    assert config.DEVICES_FILE.read_bytes() == before  # file untouched, byte-for-byte


def test_remove_missing_file_noop():
    _clean()
    devices.remove("light.x")
    assert not config.DEVICES_FILE.exists()  # never creates the file


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK: device_type, sanitize, full_sync, upsert (in-place/insert/append), remove.")
