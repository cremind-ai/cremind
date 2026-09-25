"""Reciprocal-rank fusion, dedupe and grouping — the arithmetic of ranking."""

from __future__ import annotations

import pytest

from app.documents.query.fusion import (
    NOISE_VECTOR_RANK,
    RRF_K,
    W_LEXICAL_IDS,
    W_PHRASE,
    Group,
    Hit,
    RankedList,
    dedupe_by_text,
    drop_noise,
    folder_resolver,
    group_hits,
    rrf,
)


def test_rrf_sums_weight_over_rank_plus_k():
    fused = rrf([
        RankedList("vector", 1.0, [10, 20, 30]),
        RankedList("lexical", W_LEXICAL_IDS, [20, 40]),
        RankedList("phrase", W_PHRASE, [40]),
    ])
    assert fused[10][0] == pytest.approx(1.0 / (RRF_K + 1))
    assert fused[20][0] == pytest.approx(1.0 / (RRF_K + 2) + W_LEXICAL_IDS / (RRF_K + 1))
    assert fused[40][0] == pytest.approx(W_LEXICAL_IDS / (RRF_K + 2) + W_PHRASE / (RRF_K + 1))
    assert fused[20][1] == {"vector": 2, "lexical": 1}


def test_agreement_beats_a_single_list():
    fused = rrf([RankedList("vector", 1.0, [1, 2]), RankedList("lexical", 1.0, [2, 1])])
    assert fused[1][0] == pytest.approx(fused[2][0])  # symmetric
    fused = rrf([RankedList("vector", 1.0, [1, 2]), RankedList("lexical", 1.0, [2])])
    assert fused[2][0] > fused[1][0]


def test_a_duplicate_in_one_list_counts_once_at_its_best_rank():
    fused = rrf([RankedList("lexical", 1.0, [5, 5, 6])])
    assert fused[5][0] == pytest.approx(1.0 / (RRF_K + 1))
    assert fused[6][1] == {"lexical": 3}


def _hit(cid, file_id, score, *, text_hash=None, ctype="body", ranks=None, folder=None):
    f = {"id": file_id, "cite_id": f"f{file_id:07d}", "folder_id": folder, "rel_path": f"f{file_id}.txt"}
    return Hit(chunk={"id": cid, "file_id": file_id, "text_hash": text_hash or f"h{cid}", "ctype": ctype},
               file=f, folder=None, score=score, ranks=ranks or {"lexical": 1})


def test_identical_text_in_several_files_collapses_to_also_in():
    hits = [_hit(1, 1, 0.9, text_hash="same"), _hit(2, 2, 0.8, text_hash="same"),
            _hit(3, 3, 0.7, text_hash="same"), _hit(4, 2, 0.6, text_hash="other")]
    out = dedupe_by_text(hits)
    assert [h.chunk_id for h in out] == [1, 4]
    assert [f["id"] for f in out[0].also_in] == [2, 3]


def test_file_score_is_best_plus_a_tenth_of_the_second():
    hits = [_hit(1, 1, 0.5), _hit(2, 1, 0.3), _hit(3, 2, 0.55)]
    groups = group_hits(hits, "file", folder_for_file=lambda r: None)
    by_file = {g.file["id"]: g for g in groups}
    assert by_file[1].score == pytest.approx(0.5 + 0.1 * 0.3)
    assert by_file[2].score == pytest.approx(0.55)
    assert [g.file["id"] for g in groups] == [2, 1]


def test_at_most_two_passages_and_the_card_only_when_alone():
    hits = [_hit(1, 1, 0.9, ctype="file_card"), _hit(2, 1, 0.8), _hit(3, 1, 0.7), _hit(4, 1, 0.6)]
    g = group_hits(hits, "file", folder_for_file=lambda r: None)[0]
    assert [h.chunk_id for h in g.passages] == [2, 3]
    assert g.evidence == {"lexical": 1}
    only_card = group_hits([_hit(9, 5, 0.4, ctype="file_card")], "file", folder_for_file=lambda r: None)[0]
    assert [h.chunk_id for h in only_card.passages] == [9]


def test_group_by_folder_uses_the_nearest_project():
    folders = {
        1: {"id": 1, "parent_id": None, "is_project": 0, "rel_path": "Projects"},
        2: {"id": 2, "parent_id": 1, "is_project": 1, "rel_path": "Projects/robot"},
        3: {"id": 3, "parent_id": 2, "is_project": 0, "rel_path": "Projects/robot/src"},
        4: {"id": 4, "parent_id": None, "is_project": 0, "rel_path": "Notes"},
    }
    nearest = folder_resolver(folders)
    hits = [_hit(1, 1, 0.9, folder=3), _hit(2, 2, 0.8, folder=2), _hit(3, 3, 0.7, folder=4),
            _hit(4, 4, 0.1, folder=None)]
    groups = group_hits(hits, "folder", folder_for_file=nearest)
    assert [g.key for g in groups] == [("d", 2), ("d", 4), ("root", None)]
    assert groups[0].folder["rel_path"] == "Projects/robot"
    assert len(groups[0].passages) == 2


def test_a_folder_card_groups_under_its_own_folder():
    card = Hit(chunk={"id": 7, "file_id": None, "folder_id": 2, "text_hash": "c", "ctype": "folder_card"},
               file=None, folder={"id": 2, "rel_path": "Projects/robot"}, score=0.4, ranks={"vector": 1})
    groups = group_hits([card], "file", folder_for_file=lambda r: None)
    assert groups[0].kind == "folder" and groups[0].key == ("d", 2)


def test_chunk_grouping_keeps_every_passage():
    groups = group_hits([_hit(1, 1, 0.5), _hit(2, 1, 0.4)], "chunk", folder_for_file=lambda r: None)
    assert len(groups) == 2


def test_the_vector_only_tail_is_cut_only_when_keywords_matched_something():
    def group(cid, ranks):
        return Group(kind="file", key=("f", cid), file={"id": cid}, folder=None, score=1.0,
                     passages=[_hit(cid, cid, 1.0, ranks=ranks)], evidence=ranks)

    keyword = group(1, {"lexical": 1})
    near = group(2, {"vector": NOISE_VECTOR_RANK})
    far = group(3, {"vector": NOISE_VECTOR_RANK + 5})
    assert drop_noise([keyword, near, far]) == [keyword, near]
    # Nothing matched by keyword (another language, a paraphrase): keep all.
    assert drop_noise([near, far]) == [near, far]


def test_confidence():
    assert _hit(1, 1, 1, ranks={"phrase": 9}).confidence == "high"
    assert _hit(1, 1, 1, ranks={"vector": 4, "lexical": 8}).confidence == "high"
    assert _hit(1, 1, 1, ranks={"vector": 2}).confidence == "medium"
    assert _hit(1, 1, 1, ranks={"vector": 30}).confidence == "low"


def test_unknown_group_by_is_rejected():
    with pytest.raises(ValueError):
        group_hits([], "page", folder_for_file=lambda r: None)
