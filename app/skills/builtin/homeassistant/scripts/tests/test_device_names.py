# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "python-dotenv",
# ]
# ///
"""Unit tests: references/device_names.md rendering, full_sync, single-line upsert/remove.

`config.DEVICE_NAMES_FILE` / `config.REFERENCES_DIR` are redirected to a temp dir so no real
index file is touched.

Run standalone:  python scripts/tests/test_device_names.py
Or via pytest:   pytest scripts/tests/test_device_names.py
"""
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from app import config, device_names  # noqa: E402

# Redirect the index file into a throwaway temp dir for the whole module.
_TMP = Path(tempfile.mkdtemp(prefix="ha_device_names_test_"))
config.REFERENCES_DIR = _TMP
config.DEVICE_NAMES_FILE = _TMP / "device_names.md"


def _clean():
    config.REFERENCES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        config.DEVICE_NAMES_FILE.unlink()
    except FileNotFoundError:
        pass


def _row(entity_id, name):
    # Extra keys (dtype/state) are ignored — mirrors devices.row_from_state output.
    return {"entity_id": entity_id, "name": name, "dtype": "light", "state": "on"}


def _lines():
    _, lines = device_names._split_existing()
    return lines


# --- sanitize / render ---

def test_sanitize_cell_strips_pipe_and_newlines():
    assert device_names._sanitize_cell("a|b") == "a/b"
    assert device_names._sanitize_cell("line1\nline2\tx   y") == "line1 line2 x y"
    assert device_names._sanitize_cell("  trim me  ") == "trim me"
    assert device_names._sanitize_cell(None) == ""


def test_render_line_format():
    assert device_names._render_line("light.a", "Kitchen") == "light.a | Kitchen"


# --- full_sync ---

def test_full_sync_sorted_with_header_marker():
    _clean()
    device_names.full_sync([
        _row("light.c", "C"),
        _row("light.a", "A"),
        _row("light.b", "B"),
    ])
    text = config.DEVICE_NAMES_FILE.read_text(encoding="utf-8")
    assert device_names.MARKER in text
    keys = [ln.split(" | ", 1)[0] for ln in _lines()]
    assert keys == ["light.a", "light.b", "light.c"]


def test_full_sync_renders_only_id_and_name():
    _clean()
    device_names.full_sync([_row("light.a", "Kitchen Light")])
    assert _lines() == ["light.a | Kitchen Light"]  # no type/state columns


def test_full_sync_empty_writes_header_only():
    _clean()
    device_names.full_sync([])
    text = config.DEVICE_NAMES_FILE.read_text(encoding="utf-8")
    assert device_names.MARKER in text
    assert _lines() == []


# --- upsert ---

def test_upsert_update_in_place_keeps_others_byte_identical():
    _clean()
    device_names.full_sync([_row("light.a", "A"), _row("light.b", "B"), _row("light.c", "C")])
    header_before, before = device_names._split_existing()
    device_names.upsert("light.b", "B renamed")
    header_after, after = device_names._split_existing()
    assert header_after == header_before          # header untouched
    assert after[0] == before[0]                  # light.a byte-identical
    assert after[2] == before[2]                  # light.c byte-identical
    assert after[1] == "light.b | B renamed"      # only light.b changed
    assert before[1] != after[1]


def test_upsert_insert_keeps_sorted_order():
    _clean()
    device_names.full_sync([_row("light.a", "A"), _row("light.c", "C")])
    device_names.upsert("light.b", "B")
    keys = [ln.split(" | ", 1)[0] for ln in _lines()]
    assert keys == ["light.a", "light.b", "light.c"]


def test_upsert_append_largest_key():
    _clean()
    device_names.full_sync([_row("light.a", "A")])
    device_names.upsert("switch.z", "Z")
    keys = [ln.split(" | ", 1)[0] for ln in _lines()]
    assert keys == ["light.a", "switch.z"]


def test_upsert_missing_file_creates_with_header():
    _clean()
    assert not config.DEVICE_NAMES_FILE.exists()
    device_names.upsert("light.a", "A")
    text = config.DEVICE_NAMES_FILE.read_text(encoding="utf-8")
    assert device_names.MARKER in text
    assert _lines() == ["light.a | A"]


def test_upsert_missing_marker_rebuilds():
    _clean()
    config.DEVICE_NAMES_FILE.write_text("garbage with no marker\n", encoding="utf-8")
    device_names.upsert("light.a", "A")
    text = config.DEVICE_NAMES_FILE.read_text(encoding="utf-8")
    assert device_names.MARKER in text
    assert "light.a | A" in text


def test_upsert_sanitizes_name():
    _clean()
    device_names.upsert("sensor.note", "My | Note\nsensor")
    assert _lines()[0] == "sensor.note | My / Note sensor"


# --- remove ---

def test_remove_drops_line():
    _clean()
    device_names.full_sync([_row("light.a", "A"), _row("light.b", "B")])
    device_names.remove("light.a")
    keys = [ln.split(" | ", 1)[0] for ln in _lines()]
    assert keys == ["light.b"]


def test_remove_absent_is_noop():
    _clean()
    device_names.full_sync([_row("light.a", "A")])
    before = config.DEVICE_NAMES_FILE.read_bytes()
    device_names.remove("light.nonexistent")
    assert config.DEVICE_NAMES_FILE.read_bytes() == before  # file untouched, byte-for-byte


def test_remove_missing_file_noop():
    _clean()
    device_names.remove("light.x")
    assert not config.DEVICE_NAMES_FILE.exists()  # never creates the file


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK: sanitize, render, full_sync (id|name only), upsert (in-place/insert/append), remove.")
