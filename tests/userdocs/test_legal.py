"""Legal overlay: article-bounded chunks, breadcrumbs, refs and metadata."""

from __future__ import annotations

import re

from app.userdocs.chunking import (
    chunk_blocks,
    detect_legal_meta,
    extract_refs,
    looks_legal,
    section_key_for,
)
from app.userdocs.chunking.chunker import MAX_TOKENS
from app.userdocs.chunking.legal import apply_legal_structure
from app.userdocs.types import ANCHOR_HARD, ANCHOR_NONE, ANCHOR_SOFT, Block
from tests.userdocs.legal_samples import (
    english_act_blocks,
    vietnamese_law_blocks,
    vietnamese_law_lines,
)

_ARTICLE_LINE = re.compile(r"^(?:Điều|ĐIỀU|Article|ARTICLE) (\d+)\.", re.MULTILINE)


# ── Detection ──────────────────────────────────────────────────────────────


def test_looks_legal_needs_five_article_headings() -> None:
    assert looks_legal(vietnamese_law_blocks())
    assert looks_legal(english_act_blocks())
    four = [Block(text=f"Điều {n}. Tiêu đề") for n in range(1, 5)]
    assert not looks_legal(four)
    assert looks_legal(four + [Block(text="ĐIỀU 5. VIẾT HOA")])


def test_cross_reference_at_line_start_is_not_an_article() -> None:
    # Wrapped prose that happens to start a line with "Điều N" + lower case.
    prose = [Block(text=f"Điều {n} của Luật này quy định về đất") for n in range(1, 9)]
    prose += [Block(text="Điều 3, Điều 4 và Điều 5 được sửa đổi")]
    assert not looks_legal(prose)


# ── Overlay ────────────────────────────────────────────────────────────────


def test_overlay_splits_structure_and_sets_anchors_and_locators() -> None:
    blocks = [
        Block(text="CHƯƠNG II\nQUY ĐỊNH CHUNG", locator={"line_start": 10}),
        Block(
            text="Điều 12. Nguyên tắc\n1. Khoản một dài\ntiếp tục khoản một\n"
                 "a) điểm a\nb) điểm b\n2. Khoản hai",
            locator={"line_start": 12, "line_end": 17, "page": 3},
        ),
    ]
    out = apply_legal_structure(blocks)
    texts = [b.text for b in out]
    assert texts == [
        "CHƯƠNG II\nQUY ĐỊNH CHUNG",
        "Điều 12. Nguyên tắc",
        "1. Khoản một dài\ntiếp tục khoản một",
        "a) điểm a",
        "b) điểm b",
        "2. Khoản hai",
    ]
    assert [b.anchor for b in out] == [
        ANCHOR_HARD, ANCHOR_HARD, ANCHOR_SOFT, ANCHOR_NONE, ANCHOR_NONE, ANCHOR_SOFT,
    ]
    assert out[0].role == "heading" and out[1].role == "heading"
    assert out[0].locator["heading"] == ["Chương II"]           # UPPER CASE canonicalised
    assert out[1].locator["heading"] == ["Chương II", "Điều 12"]
    assert out[1].locator["article"] == "12" and "clause" not in out[1].locator
    assert out[2].locator["heading"] == ["Chương II", "Điều 12", "Khoản 1"]
    assert (out[2].locator["clause"], out[2].locator["line_start"], out[2].locator["line_end"]) == ("1", 13, 14)
    assert out[3].locator["point"] == "a" and out[3].locator["clause"] == "1"
    assert out[3].locator["heading"] == ["Chương II", "Điều 12", "Khoản 1"]
    assert out[5].locator["clause"] == "2" and "point" not in out[5].locator
    assert all(b.locator["page"] == 3 for b in out[1:])
    # Input untouched.
    assert blocks[1].text.startswith("Điều 12.") and "article" not in blocks[1].locator


def test_overlay_roman_numerals_parts_sections_and_title_line() -> None:
    blocks = [
        Block(text="PHẦN THỨ NHẤT"),
        Block(text="Chương IV"),
        Block(text="NHỮNG QUY ĐỊNH CHUNG"),
        Block(text="Mục 2. GIAO ĐẤT"),
        Block(text="Tiểu mục 1"),
        Block(text="Điều 7a. Tiêu đề"),
        Block(text="MỤC LỤC"),  # table of contents, not "Mục L"
    ]
    out = apply_legal_structure(blocks)
    assert out[0].locator["heading"] == ["Phần thứ nhất"]
    assert out[1].locator["heading"] == ["Phần thứ nhất", "Chương IV"]
    # The all-caps line under a chapter heading is that chapter's title.
    assert out[2].role == "heading" and out[2].anchor == ANCHOR_NONE
    assert out[3].locator["heading"] == ["Phần thứ nhất", "Chương IV", "Mục 2"]
    assert out[4].locator["heading"] == ["Phần thứ nhất", "Chương IV", "Mục 2", "Tiểu mục 1"]
    assert out[5].locator["article"] == "7a"
    assert out[6].anchor == ANCHOR_NONE and out[6].role == "para"


def test_english_clauses_and_points() -> None:
    blocks = [Block(text="Article 3. Scope\n(1) First\n(a) sub a\n(2) Second\n(b) sub b")]
    out = apply_legal_structure(blocks)
    assert [b.anchor for b in out] == [ANCHOR_HARD, ANCHOR_SOFT, ANCHOR_SOFT, ANCHOR_SOFT, ANCHOR_SOFT]
    assert out[1].locator["heading"] == ["Article 3", "(1)"]
    assert out[2].locator["clause"] == "1" and out[2].locator["point"] == "a"
    assert out[4].locator["clause"] == "2" and out[4].locator["point"] == "b"


# ── Chunking legal documents ───────────────────────────────────────────────


def _articles_in(text: str) -> list[str]:
    return _ARTICLE_LINE.findall(text)


def test_vietnamese_law_chunks_never_cross_an_article() -> None:
    chunks = chunk_blocks(vietnamese_law_blocks())
    seen: list[str] = []
    for c in chunks:
        arts = _articles_in(c.text)
        assert len(arts) <= 1, c.text
        if arts:
            assert c.section_key == f"art:{arts[0]}"
            assert c.locator["article"] == arts[0]
            seen.append(arts[0])
    assert seen == [str(n) for n in range(1, 13)]  # each article exactly once, in order


def test_vietnamese_law_breadcrumbs_and_short_article() -> None:
    chunks = chunk_blocks(vietnamese_law_blocks())
    by_key = {c.section_key: c for c in chunks if c.section_key}
    assert by_key["art:1"].heading == "LUẬT ĐẤT ĐAI › Chương I › Điều 1"
    assert by_key["art:7"].heading == "LUẬT ĐẤT ĐAI › Chương II › Điều 7"
    # The chapter heading and its title ride along into the first article.
    assert by_key["art:7"].text.startswith("CHƯƠNG II\n\nQUYỀN VÀ NGHĨA VỤ")
    assert by_key["art:7"].locator["heading"] == ["LUẬT ĐẤT ĐAI", "Chương II", "Điều 7"]
    # A two-line article is not merged into its neighbour.
    short = by_key["art:3"]
    assert short.text == "Điều 3. Nội dung quy định số 3\n\nLuật này áp dụng đối với cơ quan nhà nước."
    assert short.locator["line_start"] < short.locator["line_end"]
    # The preamble keeps the law's title as its breadcrumb.
    assert chunks[0].section_key is None and chunks[0].heading == "LUẬT ĐẤT ĐAI"


def test_long_article_splits_at_clauses() -> None:
    filler = ("Người sử dụng đất có trách nhiệm sử dụng đất đúng mục đích, đúng ranh giới thửa "
              "đất và tuân thủ các quy định về bảo vệ môi trường")
    lines = ["Chương I"]
    for n in range(1, 6):
        lines.append(f"Điều {n}. Tiêu đề {n}")
        # Article 2: five ~250-token clauses — no two fit one chunk.
        count, reps = (5, 6) if n == 2 else (1, 1)
        for k in range(1, count + 1):
            lines.append(f"{k}. " + " ".join([filler] * reps) + f", khoản {k}.")
    chunks = chunk_blocks([Block(text=t) for t in lines])
    art2 = [c for c in chunks if c.locator.get("article") == "2"]
    # The first piece holds the article's own heading, so it is keyed to the
    # article; the rest each lie within one clause.
    assert [c.section_key for c in art2] == ["art:2"] + [f"art:2/cl:{k}" for k in range(2, 6)]
    assert art2[0].text.startswith("Điều 2. Tiêu đề 2\n\n1. ")
    assert art2[0].heading == "Chương I › Điều 2" and "clause" not in art2[0].locator
    for k, c in enumerate(art2[1:], start=2):
        assert c.text.startswith(f"{k}. ")  # every cut falls on a clause boundary
        assert c.heading == f"Chương I › Điều 2 › Khoản {k}"
        assert c.locator["clause"] == str(k)
    for c in art2:
        assert c.token_est <= MAX_TOKENS + 48
    # Short articles stay one chunk each, keyed to the whole article.
    assert [c.section_key for c in chunks if c.locator.get("article") in {"1", "3", "4", "5"}] == [
        "art:1", "art:3", "art:4", "art:5",
    ]


def test_english_act_chunks() -> None:
    chunks = chunk_blocks(english_act_blocks())
    keys = [c.section_key for c in chunks if c.section_key]
    assert keys == [f"art:{n}" for n in range(1, 7)]
    by_key = {c.section_key: c for c in chunks if c.section_key}
    assert by_key["art:4"].heading == "THE LAND ACT 2020 › Chapter II › Article 4"
    for c in chunks:
        assert len(_articles_in(c.text)) <= 1


def test_legal_detection_can_be_forced_off() -> None:
    chunks = chunk_blocks(vietnamese_law_blocks(), legal=False)
    assert all(c.section_key is None for c in chunks)


# ── Cross-references ───────────────────────────────────────────────────────


def test_extract_refs_vietnamese() -> None:
    text = (
        "Theo khoản 2 Điều 5 và điểm a khoản 1 Điều 4, trừ trường hợp quy định tại "
        "Điều 12 của Luật Đất đai quy định chi tiết; xem Điều 3 của Luật này và Điều 5 Nghị định này."
    )
    refs = extract_refs(text)
    simple = [(r["raw"], r["article"], r["clause"], r["point"], r["doc"]) for r in refs]
    assert simple == [
        ("khoản 2 Điều 5", "5", "2", None, None),
        ("điểm a khoản 1 Điều 4", "4", "1", "a", None),
        ("Điều 12 của Luật Đất đai", "12", None, None, "Luật Đất đai"),
        ("Điều 3 của Luật này", "3", None, None, "self"),
        ("Điều 5 Nghị định này", "5", None, None, "self"),
    ]


def test_extract_refs_english_and_dedup() -> None:
    text = (
        "Subject to Article 3(2) of the Land Act and Article 7 of this Law, see Section 4.2. "
        "Again Article 3(2) of the Land Act."
    )
    refs = extract_refs(text)
    assert [(r["article"], r["clause"], r["doc"], r.get("section")) for r in refs] == [
        ("3", "2", "Land Act", None),
        ("7", None, "self", None),
        (None, None, None, "4.2"),
    ]
    assert refs[0]["raw"] == "Article 3(2) of the Land Act"


def test_heading_line_is_not_a_reference() -> None:
    assert extract_refs("Điều 12. Nguyên tắc sử dụng đất") == []
    assert extract_refs("Article 4. Scope") == []
    refs = extract_refs("Điều 12. Sửa đổi Điều 5 của Luật này")
    assert [(r["article"], r["doc"]) for r in refs] == [("5", "self")]


def test_chunk_refs_are_populated() -> None:
    chunk = next(c for c in chunk_blocks(vietnamese_law_blocks()) if c.section_key == "art:2")
    raws = [r["raw"] for r in chunk.refs]
    assert "khoản 2 Điều 5" in raws and "Điều 12 của Luật Đất đai" in raws
    assert any(r["doc"] == "self" for r in chunk.refs)


def test_section_key_for() -> None:
    assert section_key_for({"article": "12"}) == "art:12"
    assert section_key_for({"article": "12", "clause": "2"}) == "art:12/cl:2"
    assert section_key_for({"page": 3}) is None


# ── Metadata ───────────────────────────────────────────────────────────────


def test_detect_legal_meta_vietnamese_law() -> None:
    meta = detect_legal_meta(vietnamese_law_blocks())
    assert meta == {
        "number": "31/2024/QH15",
        "issued": "2024-01-18",       # "… thông qua ngày 18 tháng 01 năm 2024"
        "effective": "2024-08-01",
        "repeals": ["45/2013/QH13"],
    }


def test_detect_legal_meta_decree_header_consolidated_and_signing_date() -> None:
    blocks = [Block(text=t) for t in [
        "VĂN BẢN HỢP NHẤT",
        "CHÍNH PHỦ",
        "Số: 102/2024/NĐ-CP",
        "Hà Nội, ngày 30 tháng 7 năm 2024",
        "Căn cứ Luật Tổ chức Chính phủ ngày 19 tháng 6 năm 2015;",
        "Điều 1. Phạm vi",
        "Khoản 2 Điều 5 có hiệu lực từ ngày 01 tháng 01 năm 2026.",
        "Nghị định này có hiệu lực thi hành kể từ ngày ký.",
        "Nghị định số 43/2014/NĐ-CP và Quyết định số 05/2020/QĐ-TTg hết hiệu lực.",
    ]]
    meta = detect_legal_meta(blocks)
    assert meta == {
        "number": "102/2024/NĐ-CP",
        "issued": "2024-07-30",
        "effective": "2024-07-30",    # "kể từ ngày ký" = the signing date
        "consolidated": True,
        "repeals": ["43/2014/NĐ-CP", "05/2020/QĐ-TTg"],
    }


def test_detect_legal_meta_english() -> None:
    meta = detect_legal_meta(english_act_blocks())
    assert meta == {"number": "12/2020/LA", "issued": "2020-03-03", "effective": "2021-01-01"}


def test_detect_legal_meta_returns_only_found_keys() -> None:
    assert detect_legal_meta([Block(text="Just a memo about lunch.")]) == {}


def test_sample_law_is_what_the_tests_assume() -> None:
    lines = vietnamese_law_lines()
    assert sum(1 for line in lines if line.startswith("Điều ")) == 12
