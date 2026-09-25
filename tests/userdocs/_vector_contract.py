"""The contract every vector-store adapter's userdocs primitives must meet.

Imported with ``*`` by ``test_vectors_qdrant.py`` and ``test_vectors_chroma.py``
(pytest collects test functions imported into a test module), each of which
provides the fixtures used here:

- ``adapter`` — the raw adapter (``QdrantClient`` / ``ChromaClient``) around a
  real, isolated backend;
- ``store``   — ``VectorStore(client=adapter)``, the wrapper the app uses;
- ``break_listing`` — makes listing collections fail like an unreachable
  store and returns a list that records any attempt to create a collection.
"""

from __future__ import annotations

import math

import pytest

from app.lib.exception import VectorStoreException
from app.userdocs.vectors import (
    CTYPE_CODES,
    KIND_CODES,
    PAYLOAD_INDEXES,
    SOURCE_CODES,
    VectorFilter,
    collection_name,
    make_payload,
)

DIM = 4
NAME = collection_name("11111111-2222-3333-4444-555555555555", "ep0001", "fake_model", DIM)


def _unit(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


# id → (vector, payload). Chosen so every filter field selects a distinct set.
POINTS = {
    1: (_unit([1, 0, 0, 0]), make_payload(source="local", ctype="body", file_id=10, folder_id=100, kind="pdf", day=19000)),
    2: (_unit([0.9, 0.1, 0, 0]), make_payload(source="local", ctype="file_card", file_id=10, folder_id=100, kind="pdf", day=19000)),
    3: (_unit([0, 1, 0, 0]), make_payload(source="drive", ctype="body", file_id=20, folder_id=200, kind="docx", day=19010)),
    4: (_unit([0, 0, 1, 0]), make_payload(source="local", ctype="caption", file_id=30, folder_id=100, kind="image")),
    5: (_unit([0.7, 0.7, 0, 0]), make_payload(source="local", ctype="folder_card", folder_id=100)),
}


def _fill(store):
    assert store.ensure_collection(NAME, DIM, payload_indexes=PAYLOAD_INDEXES) == "created"
    ids = sorted(POINTS)
    store.upsert_vectors(NAME, ids, [POINTS[i][0] for i in ids], [POINTS[i][1] for i in ids])


def _close(a, b, tol=1e-4):
    return len(a) == len(b) and all(abs(x - y) <= tol for x, y in zip(a, b))


def _hits(store, vector, filt=None, k=10):
    return {pid for pid, _ in store.query_vectors(NAME, vector, k, filt)}


# ── ensure_collection ───────────────────────────────────────────────────────


def test_ensure_creates_once_then_reports_present(store):
    assert NAME not in store.list_collections()
    assert store.ensure_collection(NAME, DIM, payload_indexes=PAYLOAD_INDEXES) == "created"
    assert NAME in store.list_collections()
    assert store.ensure_collection(NAME, DIM, payload_indexes=PAYLOAD_INDEXES) == "present"


def test_ensure_never_drops_existing_points(store):
    _fill(store)
    assert store.count(NAME) == len(POINTS)

    assert store.ensure_collection(NAME, DIM, payload_indexes=PAYLOAD_INDEXES) == "present"
    assert store.ensure_collection(NAME, DIM) == "present"

    assert store.count(NAME) == len(POINTS)
    assert _close(store.retrieve_vectors(NAME, [3])[3], POINTS[3][0])


def test_ensure_raises_instead_of_creating_when_listing_fails(store, break_listing):
    creates = break_listing()
    with pytest.raises(Exception):
        store.ensure_collection(NAME, DIM, payload_indexes=PAYLOAD_INDEXES)
    assert creates == []


# ── write / read primitives ─────────────────────────────────────────────────


def test_upsert_then_retrieve_returns_the_vectors_and_skips_unknown_ids(store):
    _fill(store)
    got = store.retrieve_vectors(NAME, [1, 3, 999])
    assert set(got) == {1, 3}
    assert _close(got[1], POINTS[1][0])
    assert _close(got[3], POINTS[3][0])
    assert store.retrieve_vectors(NAME, []) == {}


def test_upsert_replaces_an_existing_point(store):
    _fill(store)
    new_vec = _unit([0, 0, 0, 1])
    store.upsert_vectors(NAME, [1], [new_vec], [POINTS[1][1]])
    assert store.count(NAME) == len(POINTS)
    assert _close(store.retrieve_vectors(NAME, [1])[1], new_vec)


def test_upsert_rejects_mismatched_lengths(store):
    store.ensure_collection(NAME, DIM)
    with pytest.raises(ValueError):
        store.upsert_vectors(NAME, [1, 2], [POINTS[1][0]], [POINTS[1][1]])


def test_upsert_into_a_missing_collection_raises_and_creates_nothing(store):
    with pytest.raises(VectorStoreException):
        store.upsert_vectors(NAME, [1], [POINTS[1][0]], [POINTS[1][1]])
    assert NAME not in store.list_collections()


def test_delete_ids_removes_points_and_ignores_unknown_ids(store):
    _fill(store)
    store.delete_ids(NAME, [1, 999])
    store.delete_ids(NAME, [])
    assert store.count(NAME) == len(POINTS) - 1
    assert store.retrieve_vectors(NAME, [1]) == {}


def test_scroll_ids_pages_through_every_point(store):
    _fill(store)
    seen: list[int] = []
    offset = None
    for _ in range(20):  # bounded: 5 points at 2 per page
        ids, offset = store.scroll_ids(NAME, offset=offset, limit=2)
        assert len(ids) <= 2
        seen.extend(ids)
        if offset is None:
            break
    else:
        pytest.fail("scroll_ids never reported the end")
    assert sorted(seen) == sorted(POINTS)


def test_count_is_exact(store):
    store.ensure_collection(NAME, DIM)
    assert store.count(NAME) == 0
    _ids = [1, 2]
    store.upsert_vectors(NAME, _ids, [POINTS[i][0] for i in _ids], [POINTS[i][1] for i in _ids])
    assert store.count(NAME) == 2


# ── query_vectors ───────────────────────────────────────────────────────────


def test_query_returns_cosine_similarity_best_first(store):
    _fill(store)
    hits = store.query_vectors(NAME, _unit([1, 0, 0, 0]), 3)
    assert [pid for pid, _ in hits] == [1, 2, 5]
    assert abs(hits[0][1] - 1.0) < 1e-3
    assert abs(hits[1][1] - POINTS[2][0][0]) < 1e-3  # cos to e1 = first component
    scores = [s for _, s in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(isinstance(pid, int) for pid, _ in hits)


def test_query_k_limits_and_zero_k_returns_nothing(store):
    _fill(store)
    assert len(store.query_vectors(NAME, _unit([1, 1, 1, 1]), 2)) == 2
    assert store.query_vectors(NAME, _unit([1, 1, 1, 1]), 0) == []


@pytest.mark.parametrize(("filt", "expected"), [
    (VectorFilter(file_ids=[10]), {1, 2}),
    (VectorFilter(file_ids=[10, 20]), {1, 2, 3}),
    (VectorFilter(sources=[SOURCE_CODES["drive"]]), {3}),
    (VectorFilter(sources=[SOURCE_CODES["local"]]), {1, 2, 4, 5}),
    (VectorFilter(kinds=[KIND_CODES["image"]]), {4}),
    (VectorFilter(kinds=[KIND_CODES["pdf"], KIND_CODES["docx"]]), {1, 2, 3}),
    (VectorFilter(ctypes=[CTYPE_CODES["file_card"]]), {2}),
    (VectorFilter(ctypes=[CTYPE_CODES["folder_card"]]), {5}),
    # Inclusive at both ends; points without a day never match a range.
    (VectorFilter(day_range=(19000, 19000)), {1, 2}),
    (VectorFilter(day_range=(19001, 19010)), {3}),
    (VectorFilter(day_range=(0, 99999)), {1, 2, 3}),
    # Fields combine with AND.
    (VectorFilter(file_ids=[10], ctypes=[CTYPE_CODES["body"]]), {1}),
    (VectorFilter(sources=[SOURCE_CODES["local"]], day_range=(18000, 20000)), {1, 2}),
    (VectorFilter(), {1, 2, 3, 4, 5}),
], ids=lambda v: repr(v) if isinstance(v, set) else None)
def test_query_filters(store, filt, expected):
    _fill(store)
    assert _hits(store, _unit([1, 1, 1, 1]), filt) == expected


@pytest.mark.parametrize("filt", [
    VectorFilter(file_ids=[]),
    VectorFilter(sources=[]),
    VectorFilter(kinds=[]),
    VectorFilter(ctypes=[], file_ids=[10]),
])
def test_an_empty_filter_list_matches_nothing(store, filt):
    _fill(store)
    assert store.query_vectors(NAME, _unit([1, 1, 1, 1]), 10, filt) == []
