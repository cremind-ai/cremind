"""The shared filter schema: validation, dates in the profile's zone, loose
folder names, types, globs, and the soft identity boosts."""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

import pytest

from app.documents.query import filters as F

HCM = ZoneInfo("Asia/Ho_Chi_Minh")


# ── parsing ────────────────────────────────────────────────────────────────


def test_empty_filters():
    f = F.parse_filters(None)
    assert f.source == "all" and f.date_field == "any" and not f.file_level


@pytest.mark.parametrize("raw, fragment", [
    ({"types": ["spreadsheets"]}, "unknown type"),
    ({"source": "dropbox"}, "source must be"),
    ({"date_field": "birthday"}, "date_field must be"),
    ({"date_from": "yesterday"}, "not a date"),
    ({"date_from": "2026-02-30"}, "not a valid date"),
    ({"date_from": "2026-05-01", "date_to": "2026-04-01"}, "after date_to"),
    ({"size_min": "big"}, "size_min"),
    ({"image_origin": "drawing"}, "image_origin"),
    ({"file_ids": ["not an id!"]}, "file_ids"),
    ({"folders": ["x"]}, "unknown filter"),
    ("folder=x", "must be an object"),
])
def test_bad_values_name_the_field(raw, fragment):
    with pytest.raises(F.FilterError) as err:
        F.parse_filters(raw)
    assert fragment in str(err.value)


def test_values_are_normalised():
    f = F.parse_filters({
        "folder": "MKT-report", "types": ["PDF", "Word"], "extensions": ["*.DOCX", ".pdf", "xlsx"],
        "file_ids": ["[doc:k7m2xq9a#3f9c2e1b]", "k7m2xq9a", "AB2CDEFG"], "has_gps": True, "author": "me",
    })
    assert f.folders == ["MKT-report"]
    assert f.types == ["pdf", "word"]
    assert f.extensions == [".docx", ".pdf", ".xlsx"]
    assert f.file_ids == ["k7m2xq9a", "ab2cdefg"]
    assert f.file_level
    assert "author=me (soft)" in f.describe()


# ── dates ──────────────────────────────────────────────────────────────────


def test_a_day_window_is_the_whole_local_day():
    f = F.parse_filters({"date_from": "2026-09-23", "date_to": "2026-09-23"})
    w = F.make_window(f.date_from, f.date_to, HCM)
    assert w.label == "2026-09-23"
    assert w.start == _dt.datetime(2026, 9, 23, tzinfo=HCM).timestamp()
    assert w.end == _dt.datetime(2026, 9, 24, tzinfo=HCM).timestamp()
    late_evening = _dt.datetime(2026, 9, 23, 23, 30, tzinfo=HCM).timestamp()
    assert w.contains(late_evening)
    # 23:30 in Ho Chi Minh City is 16:30 UTC on the same day; 00:30 on the
    # 24th local is still the 23rd in UTC — and outside the local window.
    assert not w.contains(_dt.datetime(2026, 9, 24, 0, 30, tzinfo=HCM).timestamp())


def test_months_and_years_expand_to_their_days():
    f = F.parse_filters({"date_from": "2025", "date_to": "2025"})
    assert (f.date_from, f.date_to) == (_dt.date(2025, 1, 1), _dt.date(2025, 12, 31))
    f = F.parse_filters({"date_from": "2024-02", "date_to": "2024-02"})
    assert (f.date_from, f.date_to) == (_dt.date(2024, 2, 1), _dt.date(2024, 2, 29))
    f = F.parse_filters({"date_from": "2025-12", "date_to": "2025-12"})
    assert f.date_to == _dt.date(2025, 12, 31)


def test_widening_and_open_ends():
    w = F.make_window(_dt.date(2026, 9, 22), _dt.date(2026, 9, 24), HCM, widen_days=3)
    assert w.label == "2026-09-19..2026-09-27"
    open_end = F.make_window(_dt.date(2026, 1, 1), None, HCM, widen_days=14)
    assert open_end.label == "since 2025-12-18" and open_end.contains(4e9)
    assert F.make_window(None, None, HCM) is None


def test_the_matched_date_is_reported_and_first_seen_needs_the_cutoff():
    w = F.make_window(_dt.date(2026, 9, 22), _dt.date(2026, 9, 24), HCM)
    inside = _dt.datetime(2026, 9, 23, 10, tzinfo=HCM).timestamp()
    outside = _dt.datetime(2026, 8, 1, tzinfo=HCM).timestamp()
    row = {"mtime": outside, "birthtime": inside, "first_seen_at": inside}
    assert F.matched_date(row, "any", w, None) == ("birthtime", inside)
    assert F.matched_date(row, "modified", w, None) is None
    only_seen = {"mtime": outside, "first_seen_at": inside}
    # Seen during the first sync (cutoff after it): not evidence of anything.
    assert F.matched_date(only_seen, "any", w, first_seen_cutoff=inside + 1) is None
    assert F.matched_date(only_seen, "any", w, first_seen_cutoff=inside - 1) == ("first_seen_at", inside)
    assert F.matched_date({"mtime": outside}, "any", None, None) == ("mtime", outside)


def test_date_sql_covers_every_field_of_any():
    f = F.parse_filters({"date_from": "2026-09-22", "date_to": "2026-09-24"})
    scope = F.Scope(window=F.make_window(f.date_from, f.date_to, HCM), first_seen_cutoff=123.0)
    where, params = F.file_conditions(f, scope)
    sql = " ".join(where)
    for col in ("taken_at", "doc_created_at", "birthtime", "mtime", "drive_modified_by_me_at", "first_seen_at"):
        assert col in sql
    assert 123.0 in params
    assert "status NOT IN ('missing', 'tombstone')" in sql


# ── types, globs ───────────────────────────────────────────────────────────


def test_types_map_to_kinds():
    assert "docx" in F.TYPE_KINDS["word"] and "doc" in F.TYPE_KINDS["word"]
    assert "csv" in F.TYPE_KINDS["spreadsheet"]
    assert {"pdf", "markdown", "pptx"} <= F.TYPE_KINDS["document"]
    assert "xlsx" not in F.TYPE_KINDS["document"]
    assert F.type_of_kind("markdown") == "text" and F.type_of_kind("xls") == "spreadsheet"
    assert F.type_of_kind("database") == "other" and F.type_of_kind("epub") == "document"


def test_types_and_extensions_widen_each_other():
    f = F.parse_filters({"types": ["pdf"], "extensions": ["pages"]})
    where, params = F.file_conditions(f, F.Scope())
    assert any(" OR " in w and "kind IN" in w and "ext IN" in w for w in where)
    assert "pdf" in params and ".pages" in params


@pytest.mark.parametrize("path, globs, expected", [
    ("Clients/ABC/2025/contract.pdf", ["Clients/*/2025/**"], True),
    ("Clients/ABC/2024/contract.pdf", ["Clients/*/2025/**"], False),
    ("Reports/Q3.PDF", ["*.pdf"], True),
    ("Tài liệu/Hợp đồng.docx", ["tai lieu/*"], True),
    ("a/b/c.txt", ["a/**"], True),
])
def test_glob_match(path, globs, expected):
    assert F.glob_match(path, globs) is expected


# ── folders ────────────────────────────────────────────────────────────────


def _folders(*paths):
    out = []
    for i, p in enumerate(paths, start=1):
        out.append({"id": i, "source": "local", "rel_path": p, "name": p.rsplit("/", 1)[-1], "status": "live"})
    return out


FOLDERS = _folders("MKT-report", "MKT-report/2025", "Clients", "Clients/ABC", "Archive/Clients/ABC",
                   "Projects/robot-tracker", "Tài liệu")


@pytest.mark.parametrize("name, expected", [
    ("MKT-report", ["MKT-report"]),
    ("mkt report", ["MKT-report"]),           # separators and case
    ("mkt_report", ["MKT-report"]),
    ("MKT", ["MKT-report"]),                  # name prefix
    ("robot-traker", ["Projects/robot-tracker"]),  # a typo: fuzzy
    ("tai lieu", ["Tài liệu"]),               # accents
    ("Projects/robot-tracker", ["Projects/robot-tracker"]),
])
def test_folder_names_resolve_loosely(name, expected):
    rows, notes, unresolved = F.resolve_folder_names(FOLDERS, [name])
    assert [r["rel_path"] for r in rows] == expected
    assert not unresolved


def test_an_ambiguous_folder_name_keeps_all_and_says_so():
    rows, notes, _ = F.resolve_folder_names(FOLDERS, ["ABC"])
    assert [r["rel_path"] for r in rows] == ["Archive/Clients/ABC", "Clients/ABC"]
    assert any("matched 2 folders" in n for n in notes)


def test_nested_matches_collapse_to_the_outer_folder():
    rows, _, _ = F.resolve_folder_names(FOLDERS, ["MKT-report", "2025"])
    assert [r["rel_path"] for r in rows] == ["MKT-report"]


def test_an_unknown_folder_is_reported_with_suggestions():
    rows, notes, unresolved = F.resolve_folder_names(FOLDERS, ["Clints-archive-zzz"])
    assert not rows and unresolved == ["Clints-archive-zzz"]
    assert notes and "no folder matches" in notes[0]


def test_scope_accepts_folders_and_files():
    scope = F.Scope(folder_prefixes=[("local", "Projects")], cards_allowed=True)
    assert scope.accepts_folder({"source": "local", "rel_path": "Projects/robot"})
    assert not scope.accepts_folder({"source": "local", "rel_path": "Projects-old"})
    scope.set_file_ids([1, 2])
    assert scope.accepts_file({"id": 1, "status": "indexed", "source": "local"})
    assert not scope.accepts_file({"id": 1, "status": "missing", "source": "local"})
    assert not scope.accepts_file({"id": 3, "status": "indexed", "source": "local"})
    held = F.Scope(hidden_sources=frozenset({"drive"}))
    assert not held.accepts_file({"id": 1, "status": "indexed", "source": "drive"})


# ── soft filters ───────────────────────────────────────────────────────────


def test_author_me_boosts_and_missing_metadata_is_neutral():
    f = F.parse_filters({"author": "me"})
    ident = {"author_names": ["Nguyễn Văn A"], "emails": ["a@example.com"]}
    mine = {"doc_meta": {"author": "Nguyen Van A"}}
    theirs = {"doc_meta": {"author": "Someone Else"}}
    unknown = {"doc_meta": {}}
    assert F.soft_boost(mine, f, ident)[0] > 1.0
    assert F.soft_boost(theirs, f, ident)[0] == 1.0
    assert F.soft_boost(unknown, f, ident)[0] == 1.0


def test_taken_by_me_uses_the_configured_cameras():
    f = F.parse_filters({"taken_by": "me"})
    photo = {"kind": "image", "exif": {"make": "Canon", "model": "EOS R6"}, "is_camera_photo": 1}
    assert F.soft_boost(photo, f, {"camera_devices": ["canon eos"]})[0] > 1.0
    assert F.soft_boost(photo, f, {"camera_devices": ["iPhone"]})[0] == 1.0
    # No cameras configured: any camera photo gets a small lift.
    assert F.soft_boost(photo, f, {})[0] > 1.0


def test_image_origin_is_soft():
    shot = {"kind": "image", "name": "Screenshot 2026-09-20.png", "exif": {}, "is_camera_photo": 0}
    photo = {"kind": "image", "name": "IMG_1.jpg", "exif": {}, "is_camera_photo": 1}
    cam = F.parse_filters({"image_origin": "camera"})
    scr = F.parse_filters({"image_origin": "screenshot"})
    assert F.soft_boost(photo, cam, {})[0] > 1.0 > F.soft_boost(shot, cam, {})[0]
    assert F.soft_boost(shot, scr, {})[0] > 1.0 > F.soft_boost(photo, scr, {})[0]
