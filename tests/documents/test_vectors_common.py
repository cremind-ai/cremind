"""Backend-neutral pieces of the documents vector layer.

Collection names, payload codes and filters are persisted or compared across
restarts, so their exact shapes are pinned here. The ``VectorStore`` wrapper
must also keep constructing over an adapter that knows nothing of the new
primitives: a TypeError there would take the embedding subsystem — and every
chat — down.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from app.documents import types as t
from app.documents import vectors as v
from app.documents.embed_prompts import PROMPT_SCHEME
from app.vectorstores.base import VectorStore, VectorStoreBase

UID = "3f2b8c1e-0d4a-4a57-9f7e-2c1d5b6a7e80"


# ── collection names ────────────────────────────────────────────────────────


def test_collection_name_shape():
    name = v.collection_name(UID, "a1b2c3", "me5_multilingual-e5-base", 768)
    assert name == f"doc_{v.profile_tag(UID)}_a1b2c3_me5_multilingual-e5-base_768_p{PROMPT_SCHEME}"
    assert len(v.profile_tag(UID)) == 12
    assert len(name) <= v.MAX_COLLECTION_NAME


def test_every_identity_part_changes_the_name():
    base = v.collection_name(UID, "a1b2c3", "me5_multilingual-e5-base", 768)
    assert v.collection_name("another-uid", "a1b2c3", "me5_multilingual-e5-base", 768) != base
    assert v.collection_name(UID, "zzzzzz", "me5_multilingual-e5-base", 768) != base
    assert v.collection_name(UID, "a1b2c3", "gemma_embeddinggemma-300m", 768) != base
    assert v.collection_name(UID, "a1b2c3", "me5_multilingual-e5-base", 384) != base


def test_long_model_keys_are_cut_to_63_chars_without_merging():
    a = v.collection_name(UID, "a1b2c3", "prov_" + "x" * 80 + "-one", 1024)
    b = v.collection_name(UID, "a1b2c3", "prov_" + "x" * 80 + "-two", 1024)
    assert len(a) <= 63 and len(b) <= 63
    assert a != b
    assert a.endswith(f"_1024_p{PROMPT_SCHEME}")


def test_names_are_safe_for_chroma():
    name = v.collection_name(UID, "EP-01", "Weird Provider/Model:Name!", 768)
    assert name[0].isalnum() and name[-1].isalnum()
    assert all(c.isalnum() or c in "_-" for c in name)
    assert 3 <= len(name) <= 63
    assert v.parse_collection_name(name) == (v.profile_tag(UID), "ep01")


def test_an_unusable_epoch_is_rejected():
    with pytest.raises(ValueError):
        v.collection_name(UID, "---", "me5_x", 768)
    with pytest.raises(ValueError):
        v.collection_name(UID, "a" * 40, "me5_x", 768)


def test_parse_collection_name_round_trips_and_ignores_others():
    name = v.collection_name(UID, "a1b2c3", "me5_multilingual-e5-base", 768)
    assert v.parse_collection_name(name) == (v.profile_tag(UID), "a1b2c3")
    for other in ("cremind_docs", "memory_admin", "ud_nothex_ab_x_1_p1", "", "doc_"):
        assert v.parse_collection_name(other) is None


# ── payload codes ───────────────────────────────────────────────────────────


def test_codes_are_pinned():
    # Persisted in every collection: renumbering silently corrupts filters.
    assert v.SOURCE_CODES == {"local": 0, "drive": 1}
    assert v.CTYPE_CODES == {"body": 1, "file_card": 2, "folder_card": 3, "caption": 4, "ocr": 5}
    assert v.KIND_CODES["text"] == 1
    assert v.KIND_CODES["pdf"] == 8
    assert v.KIND_CODES["image"] == 22
    assert v.KIND_CODES["other"] == 31
    assert v.KIND_NONE == 0


def test_every_kind_and_ctype_has_a_unique_code():
    kinds = {val for name, val in vars(t).items() if name.startswith("KIND_") and isinstance(val, str)}
    ctypes = {val for name, val in vars(t).items() if name.startswith("CTYPE_") and isinstance(val, str)}
    assert kinds == set(v.KIND_CODES)
    assert ctypes == set(v.CTYPE_CODES)
    assert len(set(v.KIND_CODES.values())) == len(v.KIND_CODES)
    assert v.KIND_NONE not in v.KIND_CODES.values()
    assert len(set(v.CTYPE_CODES.values())) == len(v.CTYPE_CODES)


def test_source_codes_cover_the_settings_source_kinds():
    from app.documents.settings import SOURCE_KINDS

    assert set(v.SOURCE_CODES) == set(SOURCE_KINDS)


def test_make_payload_leaves_unknowns_out():
    p = v.make_payload(source="drive", ctype="body", file_id=7, folder_id=3, kind="docx", day=19500)
    assert p == {"s": 1, "t": 1, "k": 9, "f": 7, "g": 3, "d": 19500}
    card = v.make_payload(source="local", ctype="folder_card", folder_id=3)
    assert card == {"s": 0, "t": 3, "k": v.KIND_NONE, "g": 3}
    assert all(isinstance(x, int) for x in p.values())


def test_make_payload_rejects_unknown_ctype_and_source_but_files_unknown_kinds_as_other():
    with pytest.raises(ValueError):
        v.make_payload(source="local", ctype="nonsense")
    with pytest.raises(ValueError):
        v.make_payload(source="dropbox", ctype="body")
    assert v.make_payload(source="local", ctype="body", kind="brand-new")["k"] == v.KIND_CODES["other"]


def test_payload_indexes_cover_every_key_as_integer():
    assert v.PAYLOAD_INDEXES == {k: "integer" for k in ("f", "g", "s", "k", "t", "d")}


def test_epoch_day():
    assert v.epoch_day(0) == 0
    assert v.epoch_day(86399.9) == 0
    assert v.epoch_day(86400) == 1
    assert v.epoch_day(-1) == -1


# ── VectorFilter ────────────────────────────────────────────────────────────


def test_filter_conditions():
    f = v.VectorFilter(file_ids=[1, 2], sources=[0], kinds=[8], ctypes=[1, 2], day_range=(10, 20))
    assert f.conditions() == [
        ("f", "in", [1, 2]),
        ("s", "in", [0]),
        ("k", "in", [8]),
        ("t", "in", [1, 2]),
        ("d", "range", (10, 20)),
    ]
    assert not f.matches_nothing
    assert v.VectorFilter().conditions() == []


def test_an_empty_list_matches_nothing_but_none_matches_everything():
    assert v.VectorFilter(file_ids=[]).matches_nothing
    assert v.VectorFilter(ctypes=[]).matches_nothing
    assert not v.VectorFilter(file_ids=None).matches_nothing


# ── the VectorStore wrapper ─────────────────────────────────────────────────


class _OldAdapter(VectorStoreBase):
    """An adapter written before the documents primitives existed."""

    def create_collection(self, **kwargs: Any) -> str:
        return "c"

    def list_collections(self) -> List[str]:
        return []

    def delete_collection(self, collection_name, force=False, **kwargs):
        return None

    def add_texts(self, collection_name, embedding_function, texts, ids, metadatas=None, **kwargs):
        return ids

    def get_texts(self, collection_name, ids, filter=None, **kwargs):
        return []

    def update_texts(self, collection_name, embedding_function, texts, ids, metadatas=None, **kwargs):
        return ids

    def delete_texts(self, collection_name, ids=None, filter=None, **kwargs):
        return None

    def query(self, query_text, collection_name, embedding_function, limit=3, filter=None, **kwargs):
        return []

    def collection_exists(self, collection_name):
        return False

    def create_named_collection(self, collection_name, size):
        return collection_name

    def list_all_points(self, collection_name, with_vectors=False, filter=None):
        return []

    def query_by_vector(self, collection_name, vector, limit=10, filter=None, **kwargs):
        return []

    def add_points(self, collection_name, points):
        return None


def test_the_wrapper_still_constructs_over_an_old_adapter():
    adapter = _OldAdapter()  # would raise TypeError if the new methods were abstract
    store = VectorStore(client=adapter)
    assert store.list_collections() == []

    calls = [
        lambda: store.ensure_collection("n", 4),
        lambda: store.upsert_vectors("n", [1], [[0.0] * 4], [{}]),
        lambda: store.retrieve_vectors("n", [1]),
        lambda: store.delete_ids("n", [1]),
        lambda: store.scroll_ids("n"),
        lambda: store.count("n"),
        lambda: store.query_vectors("n", [0.0] * 4, 3),
    ]
    for call in calls:
        with pytest.raises(NotImplementedError):
            call()


def test_the_wrapper_forwards_every_new_primitive():
    seen: list = []

    class _Recording(_OldAdapter):
        def ensure_collection(self, name, dim, *, payload_indexes=None):
            seen.append(("ensure", name, dim, payload_indexes))
            return "created"

        def upsert_vectors(self, name, ids, vectors, payloads):
            seen.append(("upsert", name, ids, vectors, payloads))

        def retrieve_vectors(self, name, ids):
            seen.append(("retrieve", name, ids))
            return {1: [1.0]}

        def delete_ids(self, name, ids):
            seen.append(("delete", name, ids))

        def scroll_ids(self, name, offset=None, limit=1000):
            seen.append(("scroll", name, offset, limit))
            return [1], None

        def count(self, name):
            seen.append(("count", name))
            return 5

        def query_vectors(self, name, vector, k, filt=None):
            seen.append(("query", name, vector, k, filt))
            return [(1, 0.5)]

    store = VectorStore(client=_Recording())
    filt = v.VectorFilter(file_ids=[1])
    assert store.ensure_collection("n", 4, payload_indexes={"f": "integer"}) == "created"
    store.upsert_vectors("n", [1], [[1.0]], [{"f": 1}])
    assert store.retrieve_vectors("n", [1]) == {1: [1.0]}
    store.delete_ids("n", [1])
    assert store.scroll_ids("n", offset=3, limit=10) == ([1], None)
    assert store.count("n") == 5
    assert store.query_vectors("n", [1.0], 2, filt) == [(1, 0.5)]
    assert seen == [
        ("ensure", "n", 4, {"f": "integer"}),
        ("upsert", "n", [1], [[1.0]], [{"f": 1}]),
        ("retrieve", "n", [1]),
        ("delete", "n", [1]),
        ("scroll", "n", 3, 10),
        ("count", "n"),
        ("query", "n", [1.0], 2, filt),
    ]


def test_pre_rename_collection_names_still_parse():
    # A ud_* collection an older version created is still recognised (kept
    # while its index claims it, collected once orphaned) until it is migrated.
    name = v.collection_name(UID, "a1b2c3", "me5_multilingual-e5-base", 768)
    legacy = "ud_" + name[len(v.COLLECTION_PREFIX):]
    assert v.parse_collection_name(legacy) == v.parse_collection_name(name) == (v.profile_tag(UID), "a1b2c3")
    assert v.parse_collection_name("documentation_search") is None
    assert v.parse_collection_name("cremind_documentation_search") is None
