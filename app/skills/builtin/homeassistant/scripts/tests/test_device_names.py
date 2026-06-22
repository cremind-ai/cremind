# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "python-dotenv",
# ]
# ///
"""Unit tests: the SKILL.md "## Device list" section — rendering, full_sync, single-line
upsert/remove, and (critically) byte-for-byte preservation of everything above the marker.

`config.SKILL_FILE` is redirected to a temp SKILL.md so no real skill file is touched. The
writer no longer creates the file from scratch (SKILL.md is a shipped file), so each test seeds
a realistic prefix (docs + a `## Device list` heading + the marker) before exercising the writer.

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

# The real shipped SKILL.md (before we redirect config.SKILL_FILE at the line below).
_REAL_SKILL_MD = _SCRIPTS.parent / "SKILL.md"

# Redirect the writer at a throwaway temp SKILL.md for the whole module.
_TMP = Path(tempfile.mkdtemp(prefix="ha_device_list_test_"))
config.SKILL_FILE = _TMP / "SKILL.md"

# A realistic SKILL.md prefix: body docs the writer must preserve, the `## Device list`
# heading + intro, then the marker as the final prefix line. Device lines (if any) follow it.
_PREFIX = (
    "# fixture skill\n"
    "\n"
    "Some documentation above the device list that must survive every write.\n"
    "\n"
    "## Device list\n"
    "\n"
    "Intro sentence about the device list.\n"
    "\n"
    f"{device_names.MARKER}\n"
)


def _seed(prefix: str = _PREFIX):
    """Write a SKILL.md fixture (defaults to a marker-terminated prefix, no device lines)."""
    config.SKILL_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.SKILL_FILE.write_text(prefix, encoding="utf-8")


def _unseed():
    try:
        config.SKILL_FILE.unlink()
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

def test_full_sync_sorted_with_marker_preserves_prefix():
    _seed()
    device_names.full_sync([
        _row("light.c", "C"),
        _row("light.a", "A"),
        _row("light.b", "B"),
    ])
    text = config.SKILL_FILE.read_text(encoding="utf-8")
    assert text.startswith(_PREFIX)  # everything above the marker is byte-identical
    keys = [ln.split(" | ", 1)[0] for ln in _lines()]
    assert keys == ["light.a", "light.b", "light.c"]


def test_full_sync_renders_only_id_and_name():
    _seed()
    device_names.full_sync([_row("light.a", "Kitchen Light")])
    assert _lines() == ["light.a | Kitchen Light"]  # no type/state columns


def test_full_sync_empty_keeps_prefix_no_lines():
    _seed()
    device_names.full_sync([])
    text = config.SKILL_FILE.read_text(encoding="utf-8")
    assert text == _PREFIX  # marker present, zero device lines, prefix intact
    assert _lines() == []


def test_full_sync_skips_write_when_unchanged():
    """full_sync runs on every (re)connect; identical rows must not rewrite SKILL.md (else the
    skills watcher would re-scan on every reconnect for nothing)."""
    _seed()
    device_names.full_sync([_row("light.a", "A"), _row("light.b", "B")])
    writes = []
    orig = device_names._atomic_write
    device_names._atomic_write = lambda text: writes.append(text)
    try:
        device_names.full_sync([_row("light.b", "B"), _row("light.a", "A")])  # same set
        assert writes == [], "unchanged full_sync must not rewrite SKILL.md"
        device_names.full_sync([_row("light.a", "A renamed"), _row("light.b", "B")])
        assert len(writes) == 1, "a changed full_sync must rewrite exactly once"
    finally:
        device_names._atomic_write = orig


def test_full_sync_missing_file_is_noop():
    _unseed()
    device_names.full_sync([_row("light.a", "A")])
    assert not config.SKILL_FILE.exists()  # never conjures SKILL.md


# --- upsert ---

def test_upsert_update_in_place_keeps_others_byte_identical():
    _seed()
    device_names.full_sync([_row("light.a", "A"), _row("light.b", "B"), _row("light.c", "C")])
    prefix_before, before = device_names._split_existing()
    device_names.upsert("light.b", "B renamed")
    prefix_after, after = device_names._split_existing()
    assert prefix_after == prefix_before          # prefix (whole SKILL.md head) untouched
    assert after[0] == before[0]                  # light.a byte-identical
    assert after[2] == before[2]                  # light.c byte-identical
    assert after[1] == "light.b | B renamed"      # only light.b changed
    assert before[1] != after[1]


def test_upsert_insert_keeps_sorted_order():
    _seed()
    device_names.full_sync([_row("light.a", "A"), _row("light.c", "C")])
    device_names.upsert("light.b", "B")
    keys = [ln.split(" | ", 1)[0] for ln in _lines()]
    assert keys == ["light.a", "light.b", "light.c"]


def test_upsert_append_largest_key():
    _seed()
    device_names.full_sync([_row("light.a", "A")])
    device_names.upsert("switch.z", "Z")
    keys = [ln.split(" | ", 1)[0] for ln in _lines()]
    assert keys == ["light.a", "switch.z"]


def test_upsert_missing_file_is_noop():
    _unseed()
    device_names.upsert("light.a", "A")
    assert not config.SKILL_FILE.exists()  # never conjures SKILL.md


def test_upsert_missing_marker_self_heals_without_clobbering():
    """A SKILL.md with no marker gets a `## Device list` section appended — the existing doc
    body is preserved, never overwritten."""
    body = "# fixture skill\n\nImportant docs with no device-list section yet.\n"
    _seed(body)
    device_names.upsert("light.a", "A")
    text = config.SKILL_FILE.read_text(encoding="utf-8")
    assert text.startswith(body)               # original body preserved
    assert device_names.MARKER in text         # section appended
    assert "## Device list" in text
    assert _lines() == ["light.a | A"]         # device line lives below the marker


def test_upsert_sanitizes_name():
    _seed()
    device_names.upsert("sensor.note", "My | Note\nsensor")
    assert _lines()[0] == "sensor.note | My / Note sensor"


# --- remove ---

def test_remove_drops_line():
    _seed()
    device_names.full_sync([_row("light.a", "A"), _row("light.b", "B")])
    device_names.remove("light.a")
    keys = [ln.split(" | ", 1)[0] for ln in _lines()]
    assert keys == ["light.b"]


def test_remove_absent_is_noop():
    _seed()
    device_names.full_sync([_row("light.a", "A")])
    before = config.SKILL_FILE.read_bytes()
    device_names.remove("light.nonexistent")
    assert config.SKILL_FILE.read_bytes() == before  # file untouched, byte-for-byte


def test_remove_missing_file_noop():
    _unseed()
    device_names.remove("light.x")
    assert not config.SKILL_FILE.exists()  # never creates the file


# --- prefix-preservation across the full lifecycle ---

def test_prefix_preserved_across_full_sync_upsert_remove():
    _seed()
    device_names.full_sync([_row("light.a", "A"), _row("light.b", "B")])
    assert config.SKILL_FILE.read_text(encoding="utf-8").startswith(_PREFIX)
    device_names.upsert("light.c", "C")
    assert config.SKILL_FILE.read_text(encoding="utf-8").startswith(_PREFIX)
    device_names.remove("light.a")
    assert config.SKILL_FILE.read_text(encoding="utf-8").startswith(_PREFIX)


# --- marker integrity of the real shipped SKILL.md ---

def test_shipped_skill_md_has_marker_as_last_line():
    """The shipped SKILL.md must carry the exact MARKER bytes as its last meaningful line, with
    no device lines below it (the listener populates those at runtime)."""
    text = _REAL_SKILL_MD.read_text(encoding="utf-8")
    assert device_names.MARKER in text, "shipped SKILL.md is missing the Device list marker"
    after = text.split(device_names.MARKER, 1)[1]
    assert after.strip() == "", "nothing may follow the marker in the shipped SKILL.md"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK: sanitize, render, full_sync (id|name only, idempotent), upsert (in-place/insert/"
          "append/self-heal), remove, prefix preservation, shipped-marker integrity.")
