"""The ``filters`` schema the agent is sent, and how null reads.

A provider that makes every schema field required (the Responses API's strict
mode, which the Codex backend applied to tools sent without ``strict``) left
the model nothing neutral to put in ``size_max`` or ``has_gps``, so it invented
``size_max: 0`` or ``has_gps: true`` and every PDF dropped out. Every filter,
and the object itself, therefore also takes ``null`` — read exactly like a
field left out — while any real value keeps restricting.
"""

from __future__ import annotations

import pytest

from app.documents.query import filters as F
from app.tools.builtin import documentation_search as ds

jsonschema = pytest.importorskip("jsonschema")

ALL_NULL = {name: None for name in ds.FILTERS_SCHEMA["properties"]}


def _validator():
    cls = jsonschema.validators.validator_for(ds.FILTERS_SCHEMA)
    cls.check_schema(ds.FILTERS_SCHEMA)
    return cls(ds.FILTERS_SCHEMA)


# ── the schema ─────────────────────────────────────────────────────────────


def test_the_object_and_every_field_take_null():
    assert ds.FILTERS_SCHEMA["type"] == ["object", "null"]
    for name, spec in ds.FILTERS_SCHEMA["properties"].items():
        assert "null" in spec["type"], name
        if "enum" in spec:
            assert None in spec["enum"], name
    assert "no constraint" in ds.FILTERS_SCHEMA["description"]


@pytest.mark.parametrize("value", [None, {}, ALL_NULL, {"types": ["pdf"], "size_max": None, "has_gps": None},
                                   {"source": None, "date_field": None, "image_origin": None}])
def test_neutral_values_are_valid(value):
    assert list(_validator().iter_errors(value)) == []


@pytest.mark.parametrize("value", [
    {"size_max": 0}, {"size_min": 1024, "size_max": 5_000_000}, {"has_gps": True}, {"has_gps": False},
    {"source": "drive"}, {"date_field": "taken", "date_from": "2026-01"}, {"types": ["pdf", "document"]},
])
def test_real_restrictions_stay_valid(value):
    assert list(_validator().iter_errors(value)) == []


@pytest.mark.parametrize("value", [
    {"source": "dropbox"}, {"image_origin": "drawing"}, {"types": ["spreadsheets"]}, {"size_max": "big"},
    {"folders": ["x"]}, "folder=x",
])
def test_bad_values_are_still_rejected(value):
    assert list(_validator().iter_errors(value))


def test_every_leaf_and_research_scope_share_the_schema():
    leaves = {t.name: t for t in ds.get_tools({})}
    assert leaves["find_files"].parameters["properties"]["filters"] is ds.FILTERS_SCHEMA
    assert leaves["search"].parameters["properties"]["filters"] is ds.FILTERS_SCHEMA
    research = leaves["research"].parameters["properties"]
    for key in ("scope", "reference_scope"):
        assert research[key]["type"] == ["object", "null"]
        assert research[key]["properties"] == ds.FILTERS_SCHEMA["properties"]


def test_the_schema_names_exactly_the_fields_the_parser_knows():
    """An all-null object parses (no unknown key), and one more key does not."""
    F.parse_filters(ALL_NULL)
    with pytest.raises(F.FilterError, match="unknown filter"):
        F.parse_filters({**ALL_NULL, "sizes": None})


# ── the parser ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", [None, {}, ALL_NULL])
def test_null_parses_to_no_constraint(raw):
    f = F.parse_filters(raw)
    assert f == F.Filters()
    assert not f.file_level and f.describe() == []


def test_values_beside_nulls_still_restrict():
    f = F.parse_filters({**ALL_NULL, "size_max": 0, "has_gps": False, "types": ["pdf"]})
    assert f.size_max == 0 and f.has_gps is False and f.types == ["pdf"]
    assert f.file_level
    assert F.parse_filters({"has_gps": True}).has_gps is True
    assert F.parse_filters({"size_min": 10, "size_max": 20}).size_min == 10


@pytest.mark.parametrize("raw", [{"has_gps": False}, {"has_gps": True}, {"size_max": 0}, {"size_min": 0}])
def test_a_falsy_value_is_still_a_file_level_filter(raw):
    """``has_gps: false`` used to read as "no filter" (a truthiness check where
    the size bounds use ``is not None``), so it never reached the scope."""
    assert F.parse_filters(raw).file_level


# ── the scope, over a real ``files`` table ─────────────────────────────────


class _Db:
    """The two calls ``resolve_scope`` makes, over SQLite, with ``exif``
    encoded the way the index stores it."""

    def __init__(self, rows):
        import sqlite3

        from app.documents.index.db import _json

        self._c = sqlite3.connect(":memory:")
        self._c.row_factory = sqlite3.Row
        self._c.execute("CREATE TABLE files (id INTEGER, rel_path TEXT, status TEXT, source TEXT, "
                        "size INTEGER, exif TEXT)")
        self._c.executemany("INSERT INTO files VALUES (?, ?, 'indexed', 'local', ?, ?)",
                            [(i, p, size, _json(exif) if exif else None) for i, p, size, exif in rows])

    def read_sql(self, sql, params):
        return [dict(r) for r in self._c.execute(sql, params)]

    def get_source_state(self, source):
        return None


_FILES = [
    (1, "OpenClaw/install-guide.pdf", 2_400_000, None),
    (2, "OpenClaw/release-notes.pdf", 380_000, None),
    (3, "Photos/beach.jpg", 3_100_000, {"make": "Canon", "gps": {"lat": 10.77, "lon": 106.7}}),
    (4, "Photos/desk.jpg", 2_000_000, {"make": "Canon"}),
    (5, "empty.txt", 0, None),
]


def _ids(raw):
    import datetime as _dt

    scope = F.resolve_scope(_Db(_FILES), F.parse_filters(raw), folders=[], tz=_dt.timezone.utc)
    return None if scope.file_ids is None else sorted(scope.file_ids)


@pytest.mark.parametrize("raw", [None, {}, ALL_NULL])
def test_null_filters_leave_every_file_in_scope(raw):
    assert _ids(raw) is None  # no id list: every visible file


@pytest.mark.parametrize("raw, expected", [
    ({**ALL_NULL, "size_max": 0}, [5]),
    ({"size_max": 0}, [5]),
    ({"size_min": 1_000_000}, [1, 3, 4]),
    ({"size_min": 100_000, "size_max": 2_500_000}, [1, 2, 4]),
    ({"has_gps": True}, [3]),
    ({"has_gps": False}, [1, 2, 4, 5]),
    ({**ALL_NULL, "has_gps": False, "size_min": 1}, [1, 2, 4]),
])
def test_real_values_restrict_the_scope(raw, expected):
    assert _ids(raw) == expected
