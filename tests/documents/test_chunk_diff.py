"""The chunk diff, and the property it exists for: an edit re-embeds only
what it touched.

The property test is the contract behind "report.docx — 1 of 200 chunks
re-embedded": a synthetic ~200-chunk document takes 200 random
single-paragraph edits, and each is re-chunked and diffed against the
original. ≥95% of edits must add ≤3 chunks.
"""

from __future__ import annotations

import collections
import random

from app.documents.chunking import chunk_blocks, diff_chunks
from app.documents.types import ANCHOR_HARD, Block, Chunk, OldChunk

# ── diff_chunks unit behaviour ─────────────────────────────────────────────


def _chunk(ordinal: int, h: str, **locator) -> Chunk:
    return Chunk(ordinal=ordinal, ctype="body", heading="", text=h, text_hash=h, locator=locator)


def _old(pairs: list[tuple[int, str]]) -> list[OldChunk]:
    return [OldChunk(id=100 + i, text_hash=h, ordinal=o) for i, (o, h) in enumerate(pairs)]


def test_diff_keep_add_remove() -> None:
    old = _old([(0, "a"), (1, "b"), (2, "c")])
    new = [_chunk(0, "a"), _chunk(1, "x"), _chunk(2, "c", line_start=9)]
    d = diff_chunks(old, new)
    assert [(i, c.text_hash, c.ordinal) for i, c in d.keep] == [(100, "a", 0), (102, "c", 2)]
    assert [c.text_hash for c in d.add] == ["x"]
    assert d.remove == [101]


def test_diff_moved_chunk_is_kept_not_re_embedded() -> None:
    old = _old([(0, "a"), (1, "b"), (2, "c")])
    new = [_chunk(0, "c"), _chunk(1, "a"), _chunk(2, "b")]
    d = diff_chunks(old, new)
    assert d.add == [] and d.remove == []
    assert {(i, c.ordinal) for i, c in d.keep} == {(102, 0), (100, 1), (101, 2)}


def test_diff_duplicates_are_a_multiset_and_occ_is_renumbered() -> None:
    old = _old([(0, "d"), (1, "a"), (2, "d"), (3, "b"), (4, "d")])
    # One more copy of "d" inserted, "b" deleted.
    new = [_chunk(i, h) for i, h in enumerate(["d", "a", "d", "d", "d"])]
    for c in new:
        c.occ = 99  # garbage in: diff_chunks owns occ numbering
    d = diff_chunks(old, new)
    assert [c.occ for c in new] == [0, 0, 1, 2, 3]
    # The three old "d" rows are reused in ordinal order; the fourth copy is new.
    assert [(i, c.ordinal) for i, c in d.keep] == [(100, 0), (101, 1), (102, 2), (104, 3)]
    assert [(c.text_hash, c.ordinal) for c in d.add] == [("d", 4)]
    assert d.remove == [103]


def test_diff_removes_in_old_ordinal_order_and_handles_empty() -> None:
    old = _old([(5, "e"), (-1, "card"), (2, "c")])
    d = diff_chunks(old, [])
    assert d.remove == [101, 102, 100]
    d = diff_chunks([], [_chunk(0, "z")])
    assert [c.text_hash for c in d.add] == ["z"] and d.keep == [] and d.remove == []


# ── Synthetic documents ────────────────────────────────────────────────────

_VOCAB = (
    "nhà nước thống nhất quản lý đất đai theo quy hoạch và pháp luật người sử dụng đất có quyền "
    "nghĩa vụ các trường hợp thu hồi vì mục đích quốc phòng an ninh phát triển kinh tế xã hội "
    "lợi ích quốc gia công cộng bồi thường hỗ trợ tái định cư giá bảng cơ quan ủy ban nhân dân "
    "tỉnh huyện hộ gia đình cá nhân tổ chức doanh nghiệp vốn đầu tư nước ngoài báo cáo kết quả "
    "the land law shall apply to all users of land including households individuals business "
    "results revenue profit quarter growth market customers marketing strategy product services "
    "budget research artificial intelligence challenges opportunities robot tracking object motion"
).split()


def _sentence(rng: random.Random, n: int) -> str:
    words = [rng.choice(_VOCAB) for _ in range(n)]
    words[0] = words[0].capitalize()
    return " ".join(words) + rng.choice([".", ".", ".", "?", "!"])


def _paragraph(rng: random.Random) -> str:
    r = rng.random()
    if r < 0.40:
        n = rng.randint(6, 15)
    elif r < 0.85:
        n = rng.randint(15, 45)
    elif r < 0.99:
        n = rng.randint(45, 110)
    else:
        n = rng.randint(250, 350)  # over MAX: exercises the sentence split
    sentences = []
    left = n
    while left > 0:
        k = min(left, rng.randint(6, 25))
        sentences.append(_sentence(rng, k))
        left -= k
    if rng.random() < 0.3 and len(sentences) > 1:  # some multi-line paragraphs
        return "\n".join(" ".join(sentences[i:i + 2]) for i in range(0, len(sentences), 2))
    return " ".join(sentences)


def _document(seed: int, n: int = 1000) -> list[str]:
    rng = random.Random(seed)
    return [_paragraph(rng) for _ in range(n)]


def _text_file_blocks(paras: list[str]) -> list[Block]:
    """Blocks as a plain-text extractor would give them: paragraphs separated
    by one blank line, each with its 1-based line span."""
    blocks = []
    line = 1
    for p in paras:
        n = p.count("\n") + 1
        blocks.append(Block(text=p, locator={"line_start": line, "line_end": line + n - 1}))
        line += n + 1
    return blocks


def _as_old(chunks: list[Chunk]) -> list[OldChunk]:
    return [OldChunk(id=1000 + i, text_hash=c.text_hash, ordinal=c.ordinal, occ=c.occ,
                     locator=dict(c.locator), section_key=c.section_key)
            for i, c in enumerate(chunks)]


def _edit(rng: random.Random, paras: list[str]) -> tuple[str, list[str]]:
    """One random single-paragraph edit."""
    p = list(paras)
    i = rng.randrange(len(p))
    kind = rng.choice(["words", "add_line", "del_line", "insert"])
    if kind == "words":
        words = p[i].split(" ")
        for _ in range(rng.randint(1, 3)):
            words[rng.randrange(len(words))] = rng.choice(_VOCAB)
        p[i] = " ".join(words)
    elif kind == "add_line":
        p[i] = p[i] + "\n" + _sentence(rng, rng.randint(5, 15))
    elif kind == "del_line":
        lines = p[i].split("\n")
        if len(lines) > 1:
            del lines[rng.randrange(len(lines))]
            p[i] = "\n".join(lines)
        else:
            del p[i]  # a one-line paragraph: delete the paragraph
    else:
        p.insert(i, _paragraph(rng))
    return kind, p


# ── The property ───────────────────────────────────────────────────────────


def test_single_paragraph_edits_re_embed_at_most_three_chunks() -> None:
    paras = _document(1234)
    base = chunk_blocks(_text_file_blocks(paras))
    assert 180 <= len(base) <= 260, len(base)
    old = _as_old(base)

    rng = random.Random(99)
    dist: collections.Counter[int] = collections.Counter()
    for _ in range(200):
        _, edited = _edit(rng, paras)
        new = chunk_blocks(_text_file_blocks(edited))
        d = diff_chunks(old, new)
        dist[len(d.add)] += 1
        # The diff accounts for every chunk on both sides.
        assert len(d.keep) + len(d.add) == len(new)
        assert len(d.keep) + len(d.remove) == len(old)
    within = sum(n for adds, n in dist.items() if adds <= 3)
    # Observed when written (seed 1234 / 99, 222 chunks): 0 adds ×1, 1 ×143,
    # 2 ×48, 3 ×8 → 100% at ≤3; seeds 42/7/2026/5 gave 99.5-100%. The greedy
    # "flush when hash % k == 0, or before MAX" rule the design started from
    # scored 96.5% here and 90-92% on denser documents, with tails of 6-9
    # re-embedded chunks — right on the threshold, hence the anchor rule.
    assert within >= 190, sorted(dist.items())


def test_inserting_a_paragraph_at_the_top_changes_at_most_three_chunks() -> None:
    paras = _document(42)
    old = _as_old(chunk_blocks(_text_file_blocks(paras)))
    for seed in range(5):
        edited = [_paragraph(random.Random(seed))] + paras
        d = diff_chunks(old, chunk_blocks(_text_file_blocks(edited)))
        assert len(d.add) <= 3, (seed, len(d.add))


def test_line_shift_keeps_chunks_and_moves_their_locators() -> None:
    paras = _document(7, n=300)
    base = chunk_blocks(_text_file_blocks(paras))
    old = _as_old(base)
    by_id = {o.id: o for o in old}
    # One new line at the very top of the file, glued to the first paragraph.
    edited = ["A brand new first line of the file.\n" + paras[0]] + paras[1:]
    new = chunk_blocks(_text_file_blocks(edited))
    d = diff_chunks(old, new)
    assert len(d.add) <= 3
    assert len(d.keep) >= len(new) - 3
    for old_id, chunk in d.keep:
        prev = by_id[old_id]
        assert chunk.locator["line_start"] == prev.locator["line_start"] + 1
        assert chunk.locator["line_end"] == prev.locator["line_end"] + 1


def test_duplicate_paragraph_insert_does_not_cascade() -> None:
    rng = random.Random(5)
    boiler = "Confidential — do not distribute. " * 14
    blocks: list[Block] = []
    for s in range(12):
        blocks.append(Block(text=f"## Part {s}", role="heading", level=2, anchor=ANCHOR_HARD))
        blocks.extend(Block(text=_paragraph(rng)) for _ in range(4))
        blocks.append(Block(text="## Notice", role="heading", level=2, anchor=ANCHOR_HARD))
        blocks.append(Block(text=boiler))
    base = chunk_blocks(blocks)
    notices = [c for c in base if c.heading == "Notice"]
    assert len(notices) == 12 and [c.occ for c in notices] == list(range(12))
    old = _as_old(base)

    # Insert another copy of the notice section in the middle.
    extra = [Block(text="## Notice", role="heading", level=2, anchor=ANCHOR_HARD), Block(text=boiler)]
    edited = blocks[:28] + extra + blocks[28:]  # between sections 3 and 4
    d = diff_chunks(old, chunk_blocks(edited))
    assert len(d.add) == 1 and d.add[0].heading == "Notice" and d.add[0].occ == 12
    assert d.remove == []
    # Deleting a copy costs no embedding at all.
    d = diff_chunks(old, chunk_blocks(blocks[:-2]))
    assert d.add == [] and len(d.remove) == 1


def test_rechunking_an_unchanged_file_is_all_keep() -> None:
    blocks = _text_file_blocks(_document(3, n=200))
    old = _as_old(chunk_blocks(blocks))
    d = diff_chunks(old, chunk_blocks(blocks))
    assert d.add == [] and d.remove == [] and len(d.keep) == len(old)
    assert [i for i, _ in d.keep] == [o.id for o in old]
