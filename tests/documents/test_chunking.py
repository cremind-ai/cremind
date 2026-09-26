"""chunk_blocks and the card builders: sizes, structure, locators, determinism."""

from __future__ import annotations

import random
import re

from app.documents import textnorm
from app.documents.chunking import (
    CHUNKER_VERSION,
    chunk_blocks,
    diff_chunks,
    make_caption_chunk,
    make_file_card,
    make_folder_card,
    make_ocr_chunks,
)
from app.documents.chunking.chunker import HEADING_MAX_TOKENS, MAX_TOKENS, MIN_TOKENS
from app.documents.types import (
    ANCHOR_HARD,
    ANCHOR_SOFT,
    CTYPE_BODY,
    CTYPE_CAPTION,
    CTYPE_FILE_CARD,
    CTYPE_FOLDER_CARD,
    CTYPE_OCR,
    Block,
    OldChunk,
)
from tests.documents.legal_samples import english_act_blocks, vietnamese_law_blocks

WORDS = (
    "người sử dụng đất quyền nghĩa vụ thu hồi bồi thường báo cáo doanh thu lợi nhuận "
    "the land law shall apply revenue profit quarter market robot tracking motion"
).split()


def _prose(rng: random.Random, n_words: int, punct: bool = True) -> str:
    out = []
    for i in range(n_words):
        w = rng.choice(WORDS)
        if punct and i % 11 == 10:
            w += "."
        out.append(w)
    return " ".join(out)


def _varied_blocks(seed: int = 1) -> list[Block]:
    """Markdown-ish document: headings of every level, short and huge
    paragraphs, a run-on sentence, CJK, a code block, a table."""
    rng = random.Random(seed)
    blocks: list[Block] = [Block(text="# Annual report", role="heading", level=1, anchor=ANCHOR_HARD)]
    line = 2
    for s in range(6):
        blocks.append(Block(text=f"## Section {s}", role="heading", level=2, anchor=ANCHOR_HARD))
        for p in range(rng.randint(3, 12)):
            if p == 4:
                blocks.append(Block(text=f"#### Detail {s}.{p}", role="heading", level=4,
                                    anchor=ANCHOR_SOFT))
            n = rng.choice([10, 25, 60, 120, 700]) if s != 3 else rng.choice([30, 80])
            text = _prose(rng, n)
            blocks.append(Block(text=text, locator={"line_start": line, "line_end": line}))
            line += 2
    blocks.append(Block(text=_prose(rng, 2500, punct=False)))                  # run-on
    blocks.append(Block(text="".join(rng.choice("土地法律权利义务报告收入") for _ in range(3000))))  # CJK
    blocks.append(Block(text="def f(x):\n    return x + 1\n" * 150, role="code"))
    blocks.append(Block(text="| a | b |\n|---|---|\n" + "| 1 | 2 |\n" * 300, role="table"))
    return blocks


def _check_sizes(chunks) -> None:
    for c in chunks:
        assert textnorm.estimate_tokens(c.text) <= MAX_TOKENS, (c.ordinal, len(c.text))
        assert textnorm.estimate_tokens(c.heading) + 1 <= HEADING_MAX_TOKENS
        # Embedded input incl. e5's "passage: " prefix fits MAX + 48 + 3.
        assert c.token_est + 3 <= MAX_TOKENS + HEADING_MAX_TOKENS + 3
        assert c.token_est == textnorm.estimate_tokens(c.embed_text)


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s)


# ── Basics ─────────────────────────────────────────────────────────────────


def test_empty_and_blank_input() -> None:
    assert chunk_blocks([]) == []
    assert chunk_blocks([Block(text=""), Block(text="  \n\t ")]) == []
    one = chunk_blocks([Block(text="  Hello world.  ")])
    # Trailing blanks go; leading indentation is kept (it is code's structure).
    assert len(one) == 1 and one[0].text == "  Hello world."
    assert one[0].text_hash == textnorm.text_hash("", "Hello world.")
    assert one[0].ordinal == 0 and one[0].ctype == CTYPE_BODY and one[0].occ == 0
    # 2: wrapped cross-references no longer start an article (legal overlay only).
    assert CHUNKER_VERSION == 2


def test_deterministic() -> None:
    for blocks in (_varied_blocks(1), vietnamese_law_blocks(), english_act_blocks()):
        a = chunk_blocks(blocks)
        b = chunk_blocks([Block.from_dict(x.to_dict()) for x in blocks])
        assert a == b
        assert [c.ordinal for c in a] == list(range(len(a)))


def test_no_chunk_exceeds_the_window() -> None:
    for seed in (1, 2, 3):
        _check_sizes(chunk_blocks(_varied_blocks(seed)))
    _check_sizes(chunk_blocks(vietnamese_law_blocks()))


def test_every_block_lands_in_exactly_one_chunk_in_order() -> None:
    rng = random.Random(11)
    blocks = [Block(text=_prose(rng, rng.randint(5, 150))) for _ in range(300)]
    chunks = chunk_blocks(blocks)
    joined = "\n\n".join(c.text for c in chunks)
    pos = 0
    for b in blocks:
        hits = [c.ordinal for c in chunks if b.text in c.text.split("\n\n")]
        assert len(hits) == 1
        idx = joined.find(b.text, pos)
        assert idx >= 0
        pos = idx + len(b.text)
    # Nothing added, nothing lost.
    assert joined == "\n\n".join(b.text for b in blocks)


def test_split_blocks_reconstruct_exactly() -> None:
    blocks = _varied_blocks(4)
    chunks = chunk_blocks(blocks)
    assert _squash("".join(c.text for c in chunks)) == _squash("".join(b.text for b in blocks))


def test_chunk_sizes_are_reasonable() -> None:
    rng = random.Random(8)
    blocks = [Block(text=_prose(rng, rng.randint(10, 60))) for _ in range(400)]
    chunks = chunk_blocks(blocks)
    sizes = [textnorm.estimate_tokens(c.text) for c in chunks]
    assert min(sizes[:-1]) >= MIN_TOKENS  # only the tail may be short
    assert 200 <= sum(sizes) / len(sizes) <= MAX_TOKENS


# ── Headings and breadcrumbs ───────────────────────────────────────────────


def test_breadcrumbs_and_hard_headings() -> None:
    chunks = chunk_blocks(_varied_blocks(5))
    first = chunks[0]
    # "# Annual report" rides along with "## Section 0" instead of standing alone.
    assert first.text.startswith("# Annual report\n\n## Section 0")
    assert first.heading == "Annual report › Section 0"
    for c in chunks:
        # H2 is HARD: no chunk holds two sections.
        assert len(re.findall(r"^## ", c.text, re.MULTILINE)) <= 1
        # A chunk never ends on a heading line.
        assert not c.text.rstrip().split("\n")[-1].startswith("#")
    sec3 = [c for c in chunks if c.heading.startswith("Annual report › Section 3")]
    assert sec3 and all(c.locator["heading"][:2] == ["Annual report", "Section 3"] for c in sec3)


def test_breadcrumb_is_capped_from_the_left() -> None:
    labels = [f"Level {i} heading with a fairly long descriptive title" for i in range(6)]
    blocks = [Block(text="#" * (i + 1) + " " + t, role="heading", level=i + 1) for i, t in enumerate(labels)]
    blocks.append(Block(text="Body text under the deepest heading."))
    [chunk] = chunk_blocks(blocks)
    assert chunk.heading.startswith("…")
    assert chunk.heading.endswith(labels[-1])
    assert textnorm.estimate_tokens(chunk.heading) + 1 <= HEADING_MAX_TOKENS
    assert chunk.locator["heading"] == labels  # the locator keeps the full path

    huge = "z" * 300
    [chunk] = chunk_blocks([Block(text=huge, role="heading", level=1), Block(text="body")])
    assert chunk.heading.startswith("…") and textnorm.estimate_tokens(chunk.heading) + 1 <= HEADING_MAX_TOKENS


def test_slides_never_share_a_chunk() -> None:
    blocks = []
    for s in range(1, 6):
        blocks.append(Block(text=f"Slide {s} title", role="heading", level=1, anchor=ANCHOR_HARD,
                            locator={"slide": s}))
        blocks.append(Block(text=f"Point about slide {s}.", locator={"slide": s}))
    chunks = chunk_blocks(blocks)
    assert [c.locator["slide"] for c in chunks] == [1, 2, 3, 4, 5]


# ── Locators ───────────────────────────────────────────────────────────────


def test_locator_merge() -> None:
    blocks = [
        Block(text="alpha " * 20, locator={"page": 3, "line_start": 1, "line_end": 2}),
        Block(text="beta " * 20, locator={"page": 4, "page_end": 5, "line_start": 3, "line_end": 9}),
    ]
    [c] = chunk_blocks(blocks)
    assert c.locator == {"page": 3, "page_end": 5, "line_start": 1, "line_end": 9}

    sheet = [
        Block(text="r1 " * 10, locator={"sheet": "Q1", "range": "A2:F41", "rows": [2, 41]}),
        Block(text="r2 " * 10, locator={"sheet": "Q1", "range": "A42:H81", "rows": [42, 81]}),
    ]
    [c] = chunk_blocks(sheet)
    assert c.locator == {"sheet": "Q1", "range": "A2:H81", "rows": [2, 81]}


def test_split_block_narrows_line_locator() -> None:
    rng = random.Random(2)
    lines = [_prose(rng, 40) for _ in range(40)]
    block = Block(text="\n".join(lines), locator={"line_start": 100, "line_end": 139})
    chunks = chunk_blocks([block])
    assert len(chunks) > 1
    spans = [(c.locator["line_start"], c.locator["line_end"]) for c in chunks]
    assert spans[0][0] == 100 and spans[-1][1] == 139
    for (a1, b1), (a2, _) in zip(spans, spans[1:]):
        assert a1 <= b1 <= a2  # a sentence cut can share a line
    for c, (a, b) in zip(chunks, spans):
        assert c.text.count("\n") == b - a


# ── Oversized text: sentence and Gear splits ───────────────────────────────


def test_run_on_text_splits_on_word_boundaries_and_is_stable() -> None:
    rng = random.Random(6)
    words = [rng.choice(WORDS) for _ in range(4000)]
    text = " ".join(words)
    chunks = chunk_blocks([Block(text=text)])
    assert len(chunks) > 5
    _check_sizes(chunks)
    vocab = set(WORDS)
    for c in chunks:
        assert set(c.text.split()) <= vocab  # no word cut in half
    # Change one word in the middle: the Gear cuts resynchronise.
    words2 = list(words)
    words2[2000] = "thay_đổi"
    new = chunk_blocks([Block(text=" ".join(words2))])
    old = [OldChunk(id=i, text_hash=c.text_hash, ordinal=c.ordinal) for i, c in enumerate(chunks)]
    assert len(diff_chunks(old, new).add) <= 3


def test_cjk_without_punctuation_is_split_and_stable() -> None:
    rng = random.Random(7)
    chars = [rng.choice("土地法律权利义务报告收入市场机器人") for _ in range(4000)]
    chunks = chunk_blocks([Block(text="".join(chars))])
    assert len(chunks) >= 10
    _check_sizes(chunks)
    assert "".join(c.text for c in chunks) == "".join(chars)
    chars2 = list(chars)
    chars2[2000:2000] = list("新")
    new = chunk_blocks([Block(text="".join(chars2))])
    old = [OldChunk(id=i, text_hash=c.text_hash, ordinal=c.ordinal) for i, c in enumerate(chunks)]
    assert len(diff_chunks(old, new).add) <= 3


def test_repeated_identical_lines_split_evenly() -> None:
    # No line can out-rank an identical one, so there are no anchors; the
    # fallback must still bound every chunk and stay fast on a big log.
    chunks = chunk_blocks([Block(text="same log line here\n" * 20000)])
    _check_sizes(chunks)
    sizes = [textnorm.estimate_tokens(c.text) for c in chunks]
    assert min(sizes) >= MIN_TOKENS
    assert _squash("".join(c.text for c in chunks)) == _squash("same log line here" * 20000)


# ── Chunk fields ───────────────────────────────────────────────────────────


def test_folded_only_when_it_adds_something() -> None:
    [vi] = chunk_blocks([Block(text="Người sử dụng đất có quyền")])
    assert vi.folded == "nguoi su dung dat co quyen"
    [en] = chunk_blocks([Block(text="The land law applies")])
    assert en.folded is None


def test_duplicate_chunks_get_occurrence_numbers() -> None:
    body = "Boilerplate disclaimer paragraph that repeats. " * 12
    blocks = []
    for i in range(4):
        blocks.append(Block(text="## Notes", role="heading", level=2, anchor=ANCHOR_HARD))
        blocks.append(Block(text=body))
        blocks.append(Block(text=f"## Unique {i}", role="heading", level=2, anchor=ANCHOR_HARD))
        blocks.append(Block(text=f"Unique content number {i}. " * 10))
    chunks = chunk_blocks(blocks)
    notes = [c for c in chunks if c.heading == "Notes"]
    assert len(notes) == 4 and len({c.text_hash for c in notes}) == 1
    assert [c.occ for c in notes] == [0, 1, 2, 3]


# ── Cards ──────────────────────────────────────────────────────────────────


def test_file_card_lines_and_determinism() -> None:
    kw = dict(name="report.docx", rel_path="MKT-report/2026/report.docx", kind="docx", size=48_213,
              mtime_iso="2026-09-23T10:15:00+07:00", title="Q3 results", author="Lan Nguyễn",
              created_iso="2026-09-01T08:00:00", summary_text="Doanh thu quý 3 tăng " * 200,
              extra={"pages": 12, "keywords": ["AI", "sales"]})
    card = make_file_card(**kw)
    assert card.ctype == CTYPE_FILE_CARD and card.ordinal == -1 and card.heading == ""
    lines = card.text.split("\n")
    assert lines[:5] == [
        "File: report.docx", "Path: MKT-report/2026/report.docx", "Type: docx",
        "Size: 47 KB", "Modified: 2026-09-23",
    ]
    assert "Title: Q3 results" in lines and "Author: Lan Nguyễn" in lines
    assert "Created: 2026-09-01" in lines
    assert lines.index("Keywords: AI, sales") < lines.index("Pages: 12")  # extra sorted by key
    content = [line for line in lines if line.startswith("Content: ")][0]
    assert content.endswith("…") and textnorm.estimate_tokens(content) <= 125
    assert card.folded is not None and "nguyen" in card.folded
    # Same inputs (extra in another order) → same card; same day → same hash.
    kw2 = dict(kw, extra={"keywords": ["AI", "sales"], "pages": 12}, mtime_iso="2026-09-23T23:59:59")
    assert make_file_card(**kw2) == card
    assert make_file_card(**dict(kw, mtime_iso="2026-09-24T00:00:00")).text_hash != card.text_hash


def test_file_card_photo_and_size_bound() -> None:
    card = make_file_card(name="IMG_0001.HEIC", rel_path="Photos/IMG_0001.HEIC", kind="image",
                          size=2_400_000, mtime_iso="2025-06-01T09:00:00",
                          taken_iso="2025-05-31T17:42:10", camera="Apple iPhone 15")
    assert "Taken: 2025-05-31" in card.text and "Camera: Apple iPhone 15" in card.text
    assert "Size: 2.3 MB" in card.text
    big = make_file_card(name="a.zip", rel_path="a.zip", kind="archive", size=1, mtime_iso="",
                         extra={"listing": [f"folder/file_{i}.txt" for i in range(2000)]})
    assert textnorm.estimate_tokens(big.text) <= MAX_TOKENS
    assert big.text.startswith("File: a.zip") and "Modified" not in big.text


def test_folder_cards() -> None:
    proj = make_folder_card(
        name="robot-tracker", rel_path="Code/robot-tracker", file_count=42,
        languages={"Python": 12, "C++": 3, "Markdown": 2}, markers=["pyproject.toml", ".git"],
        deps=["opencv-python", "numpy"], readme_head="# Robot tracker\nTracks object motion with OpenCV.",
        top_files=["main.py", "tracker.py"], activity_min_iso="2025-01-03T10:00:00",
        activity_max_iso="2025-11-20T18:00:00", git_last_commit_iso="2025-11-20T17:59:00",
    )
    assert proj.ctype == CTYPE_FOLDER_CARD and proj.ordinal == -1
    lines = proj.text.split("\n")
    assert lines[0] == "Project folder: robot-tracker"
    assert "Languages: Python (12), C++ (3), Markdown (2)" in lines
    assert "Project markers: .git, pyproject.toml" in lines
    assert "Dependencies: numpy, opencv-python" in lines
    assert "Activity: 2025-01-03 to 2025-11-20" in lines
    assert "Last commit: 2025-11-20" in lines
    assert lines[-1] == "README: # Robot tracker Tracks object motion with OpenCV."

    plain = make_folder_card(name="Invoices", rel_path="Invoices", file_count=5, languages={},
                             markers=[], deps=[], readme_head=None, top_files=["a.pdf"],
                             activity_min_iso="2026-01-01", activity_max_iso="2026-01-01")
    assert plain.text.split("\n")[0] == "Folder: Invoices" and "Activity: 2026-01-01" in plain.text
    implicit = make_folder_card(name="scripts", rel_path="scripts", file_count=4,
                                languages={"Python": 4}, markers=[], deps=[], readme_head=None,
                                top_files=[], activity_min_iso=None, activity_max_iso=None)
    assert implicit.text.startswith("Project folder: scripts")
    main_py = make_folder_card(name="x", rel_path="x", file_count=1, languages={"Python": 1},
                               markers=[], deps=[], readme_head=None, top_files=["src/main.py"],
                               activity_min_iso=None, activity_max_iso=None)
    assert main_py.text.startswith("Project folder: x")


def test_caption_and_ocr_chunks() -> None:
    cap = make_caption_chunk("  Two puppies playing   in a park.\r\n\r\n\r\nTags: dog, park  ", ordinal=3)
    assert cap.ctype == CTYPE_CAPTION and cap.ordinal == 3 and cap.heading == ""
    assert cap.text == "Two puppies playing in a park.\n\nTags: dog, park"
    assert cap.text_hash == textnorm.text_hash("", cap.text)

    rng = random.Random(9)
    page_text = "\n\n".join(_prose(rng, 60) for _ in range(12))
    ocr = make_ocr_chunks(7, page_text, start_ordinal=40)
    assert len(ocr) >= 2
    assert all(c.ctype == CTYPE_OCR and c.locator.get("page") == 7 for c in ocr)
    assert [c.ordinal for c in ocr] == list(range(40, 40 + len(ocr)))
    assert _squash("".join(c.text for c in ocr)) == _squash(page_text)
    assert make_ocr_chunks(1, "  \n\n ", 0) == []
