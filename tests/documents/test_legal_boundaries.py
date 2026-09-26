"""Article boundaries: a wrapped cross-reference never starts an article.

The reported decree wraps "… quy định tại khoản 2" / "Điều 23 Nghị định
này." across two lines. The overlay read the second line as a new Điều 23
heading: the rest of Article 24 (clause 4 with it) was filed under Article
23, and the reference itself vanished (a heading line is not a reference).
The same happens in English with "… referred to in" / "Article 23(2) of this
Regulation." These tests pin the fix, and that genuine headings — standalone
labels, annotations such as "(repealed)", annexes that restart at Article 1 —
are still headings.
"""

from __future__ import annotations

from app.documents.chunking import chunk_blocks, extract_refs, looks_legal
from app.documents.chunking.legal import apply_legal_structure
from app.documents.types import Block
from tests.documents._foreign_worker_regs import DECREE_EN, DECREE_VI


def _blocks(lines: list[str]) -> list[Block]:
    return [Block(text=x, locator={"line_start": i + 1, "line_end": i + 1}) for i, x in enumerate(lines)]


def _by_article(lines: list[str]) -> dict[str, str]:
    """Article number -> the text of every chunk filed under it."""
    out: dict[str, str] = {}
    for c in chunk_blocks(_blocks(lines), legal=True):
        art = (c.section_key or "").split("/", 1)[0]
        if art.startswith("art:"):
            out[art[4:]] = out.get(art[4:], "") + "\n" + c.text
    return out


def _refs(lines: list[str], article: str) -> list[dict]:
    refs: list[dict] = []
    for c in chunk_blocks(_blocks(lines), legal=True):
        if (c.section_key or "").split("/", 1)[0] == f"art:{article}":
            refs += c.refs or []
    return refs


def test_the_plans_example_keeps_the_reference_in_article_24():
    lines = [
        "Article 22. Scope", "(1) This Regulation applies to permits.",
        "Article 23. Grounds", "(1) Loss.", "(2) A change of the holder's name.",
        "Article 24. Replacement procedure",
        "(3) The supporting document must satisfy",
        "Article 23(2) of this Regulation.",
        "(4) Supply the existing permit.",
        "Article 25. Fees", "(1) No fee is charged.",
        "Article 26. Entry into force", "(1) On signature.",
    ]
    arts = _by_article(lines)
    assert "Article 23(2) of this Regulation." in arts["24"] and "(4) Supply the existing permit." in arts["24"]
    assert "Supply the existing permit" not in arts["23"] and "of this Regulation" not in arts["23"]
    assert any(r["article"] == "23" and r["clause"] == "2" and r["doc"] == "self" for r in _refs(lines, "24"))


def test_english_decree_article_24_keeps_its_continuation_and_clause_4():
    arts = _by_article(DECREE_EN)
    assert "Article 23(2) of this Decree." in arts["24"]
    assert "(4) The existing work permit" in arts["24"]
    assert "existing work permit" not in arts["23"] and "Article 23(2) of this Decree" not in arts["23"]
    refs = _refs(DECREE_EN, "24")
    assert {(r["article"], r["clause"]) for r in refs} >= {("23", "2"), ("23", "1")}


def test_vietnamese_decree_article_24_keeps_its_continuation_and_clause_4():
    arts = _by_article(DECREE_VI)
    assert "Điều 23 Nghị định này." in arts["24"]
    assert "4. Giấy phép lao động còn thời hạn" in arts["24"]
    assert "Giấy phép lao động còn thời hạn, trừ" not in arts["23"]
    refs = _refs(DECREE_VI, "24")
    assert [(r["article"], r["clause"], r["doc"]) for r in refs] == [("23", "2", "self"), ("23", "1", "self")]
    # Its clauses are Article 24's: the chunk locator says so.
    blocks = apply_legal_structure(_blocks(DECREE_VI))
    clause4 = next(b for b in blocks if b.text.startswith("4. Giấy phép lao động còn thời hạn"))
    assert clause4.locator["article"] == "24" and clause4.locator["clause"] == "4"


def test_a_reference_at_the_start_of_a_chunk_is_still_a_reference():
    text = "Điều 23 Nghị định này.\n\n4. Giấy phép lao động còn thời hạn, trừ trường hợp bị mất."
    assert [(r["article"], r["doc"]) for r in extract_refs(text)] == [("23", "self")]
    assert [r["article"] for r in extract_refs("Article 23(2) of this Decree.\n(4) The permit.")] == ["23"]


def test_a_self_reference_wrapped_across_lines_is_still_this_document():
    """The reported decree prints "… khoản 1, 3, 5 và 6 Điều 18 Nghị định" /
    "này;": the reference is to the decree itself, and must be followed."""
    for text in ("giấy tờ quy định tại các khoản 1, 3, 5 và 6 Điều 18 Nghị định\nnày;\n\nc) Bản sao",
                 "giấy tờ quy định tại khoản 1 Điều 18 Nghị\nđịnh này;"):
        refs = extract_refs(text)
        assert [(r["article"], r["doc"]) for r in refs] == [("18", "self")], text
        assert "\n" not in refs[0]["raw"]
    # A bare keyword with no "này" after it still names another document.
    assert extract_refs("theo Điều 154 của Bộ luật\nLao động")[0]["doc"] == "Bộ luật"


def test_a_named_document_after_the_number_is_a_reference():
    lines = ["Điều 5. Hồ sơ", "1. Giấy tờ theo quy định tại", "Điều 12 Bộ luật Lao động.",
             "2. Ảnh màu.", "Điều 6. Thời hạn", "1. Ba ngày."]
    arts = _by_article(lines)
    assert "Điều 12 Bộ luật Lao động." in arts["5"] and "12" not in arts


def test_genuine_headings_are_kept():
    lines = [
        "Article 1. Scope", "(1) Text.",
        "Article 2 (repealed)",
        "Article 3", "Definitions", "(1) A term.",
        "ĐIỀU 4. Nghĩa vụ", "1. Nội dung.",
        "Article 5. Entry into force", "(1) On signature.",
        # An annex restarts the numbering: still articles.
        "ANNEX", "MODEL CONTRACT",
        "Article 1. Parties", "(1) The employer and the worker.",
        "Article 2. Term", "(1) One year.",
    ]
    blocks = apply_legal_structure(_blocks(lines))
    headings = [b.text for b in blocks if b.role == "heading" and b.text.startswith(("Article", "ĐIỀU"))]
    assert headings == ["Article 1. Scope", "Article 2 (repealed)", "Article 3", "ĐIỀU 4. Nghĩa vụ",
                        "Article 5. Entry into force", "Article 1. Parties", "Article 2. Term"]
    assert looks_legal(_blocks(lines))


def test_a_label_after_a_reference_lead_in_continues_it():
    lines = ["Điều 8. Hồ sơ", "1. Giấy tờ nêu tại khoản 3", "Điều 7", "2. Bản sao.",
             "Điều 9. Hiệu lực", "1. Từ ngày ký."]
    arts = _by_article(lines)
    assert "Điều 7" in arts["8"] and "2. Bản sao." in arts["8"] and "7" not in arts
