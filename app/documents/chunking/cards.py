"""Card chunks: the embeddable description of a file, a folder, an image.

A file's *name and path* are deliberately kept out of its body chunks (see
:mod:`app.documents.chunking.chunker`), so that renaming or moving a 200-chunk
report re-embeds one thing — its file card — instead of all 200. The card is
also what "find the file I wrote about X" matches when the body is empty or
unreadable: metadata-only kinds (executables, archives, media) have nothing
but a card.

Cards are plain "Key: value" lines because that is what both the embedder and
FTS read well, and their text must be a pure function of the inputs: the card
is diffed by ``text_hash`` like any chunk, so a card that rendered a dict in
arbitrary order, or "now", would be re-embedded on every scan. For the same
reason dates render at day precision — a file saved five times today keeps
one card — and sizes render coarsely.

Captions (vision output rendered to text) and OCR text of scanned pages are
chunks of their own types so search can weigh or filter them apart from text
the user wrote.
"""

from __future__ import annotations

import re
from typing import Any

from app.documents import textnorm
from app.documents.chunking.chunker import MAX_TOKENS, _chunk
from app.documents.types import CTYPE_CAPTION, CTYPE_FILE_CARD, CTYPE_FOLDER_CARD, CTYPE_OCR, Block, Chunk

CARD_ORDINAL = -1
# How much of the content head / README a card quotes.
HEAD_TOKENS = 120
_MAX_LIST_ITEMS = 30
_WS_RE = re.compile(r"\s+")
_PARA_SPLIT_RE = re.compile(r"\n\s*\n")


def _one_line(text: str) -> str:
    return _WS_RE.sub(" ", textnorm.normalize_ws(text)).strip()


def _truncate_tokens(text: str, budget: int) -> str:
    """The longest word prefix of ``text`` (whitespace collapsed) within
    ``budget`` estimated tokens, with '…' when cut."""
    flat = _one_line(text)
    if textnorm.estimate_tokens(flat) <= budget:
        return flat
    words = flat.split(" ")
    lo, hi = 0, len(words)
    while lo < hi:  # largest word count that fits, leaving room for '…'
        mid = (lo + hi + 1) // 2
        if textnorm.estimate_tokens(" ".join(words[:mid])) + 1 <= budget:
            lo = mid
        else:
            hi = mid - 1
    return (" ".join(words[:lo]) + " …").strip()


def _day(iso: str | None) -> str | None:
    """'2026-09-25T14:03:11+07:00' → '2026-09-25'. Anything not ISO-shaped is
    kept verbatim (trimmed) rather than guessed at."""
    if not iso:
        return None
    s = str(iso).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    return s or None


def _human_size(size: int) -> str:
    """Coarse on purpose (one decimal below 10, whole units above): a small
    edit must not change the card, or it would be re-embedded every save."""
    n = float(max(0, int(size)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.1f} {unit}" if n < 10 else f"{n:.0f} {unit}"
        n /= 1024
    return f"{int(size)} B"  # pragma: no cover


def _fmt_value(v: Any) -> str:
    if isinstance(v, (list, tuple, set)):
        items = sorted(str(x) for x in v) if isinstance(v, set) else [str(x) for x in v]
        return ", ".join(items)
    if isinstance(v, dict):
        return ", ".join(f"{k}={v[k]}" for k in sorted(v, key=str))
    return str(v)


def _card(ctype: str, lines: list[str], ordinal: int = CARD_ORDINAL) -> Chunk:
    text = "\n".join(line for line in lines if line)
    if textnorm.estimate_tokens(text) > MAX_TOKENS:
        # Long metadata/dependency lists: stay inside the embedding window by
        # dropping trailing lines (the content/README head is last, so it
        # goes first), then words.
        text = _truncate_tokens_lines(text, MAX_TOKENS)
    return Chunk(
        ordinal=ordinal,
        ctype=ctype,
        heading="",
        text=text,
        text_hash=textnorm.text_hash("", text),
        occ=0,
        section_key=None,
        locator={},
        refs=[],
        token_est=textnorm.estimate_tokens(text),
        folded=textnorm.fold_if_needed(text),
    )


def make_file_card(
    *,
    name: str,
    rel_path: str,
    kind: str,
    size: int,
    mtime_iso: str,
    title: str | None = None,
    author: str | None = None,
    created_iso: str | None = None,
    taken_iso: str | None = None,
    camera: str | None = None,
    summary_text: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Chunk:
    """The ``file_card`` chunk (ordinal -1): name, path, type, size, dates,
    document/photo metadata, ``extra`` (sorted "Key: value" lines) and the
    head of the content (~120 tokens of ``summary_text``)."""
    lines = [
        f"File: {name}",
        f"Path: {rel_path}",
        f"Type: {kind}",
        f"Size: {_human_size(size)}",
        f"Modified: {_day(mtime_iso)}" if _day(mtime_iso) else "",
    ]
    if title and title.strip():
        lines.append(f"Title: {_one_line(title)}")
    if author and author.strip():
        lines.append(f"Author: {_one_line(author)}")
    if _day(created_iso):
        lines.append(f"Created: {_day(created_iso)}")
    if _day(taken_iso):
        lines.append(f"Taken: {_day(taken_iso)}")
    if camera and camera.strip():
        lines.append(f"Camera: {_one_line(camera)}")
    for key in sorted(extra or {}, key=str):
        val = (extra or {})[key]
        if val is None or val == "" or val == [] or val == {}:
            continue
        lines.append(f"{str(key).replace('_', ' ').capitalize()}: {_one_line(_fmt_value(val))}")
    if summary_text and summary_text.strip():
        lines.append(f"Content: {_truncate_tokens(summary_text, HEAD_TOKENS)}")
    return _card(CTYPE_FILE_CARD, lines)


def _looks_like_project(markers: list[str], languages: dict[str, int], top_files: list[str]) -> bool:
    # Marker files (README, pyproject, package.json, .git, …) or the implicit
    # rule from discovery: 3+ source files in one language, or a main.py.
    if markers:
        return True
    if any(int(n) >= 3 for n in languages.values()):
        return True
    return any(f.replace("\\", "/").rsplit("/", 1)[-1].lower() == "main.py" for f in top_files)


def make_folder_card(
    *,
    name: str,
    rel_path: str,
    file_count: int,
    languages: dict[str, int],
    markers: list[str],
    deps: list[str],
    readme_head: str | None,
    top_files: list[str],
    activity_min_iso: str | None,
    activity_max_iso: str | None,
    git_last_commit_iso: str | None = None,
) -> Chunk:
    """The ``folder_card`` chunk (ordinal -1). Says "Project folder" when
    markers or the language mix make it a project, so "the Python robot
    project" matches the folder itself, not just a file inside it."""
    project = _looks_like_project(markers, languages, top_files)
    lines = [
        f"{'Project folder' if project else 'Folder'}: {name}",
        f"Path: {rel_path}",
        f"Files: {int(file_count)}",
    ]
    if languages:
        langs = sorted(languages.items(), key=lambda kv: (-int(kv[1]), str(kv[0])))
        lines.append("Languages: " + ", ".join(f"{k} ({int(v)})" for k, v in langs[:_MAX_LIST_ITEMS]))
    if markers:
        lines.append("Project markers: " + ", ".join(sorted(set(markers))[:_MAX_LIST_ITEMS]))
    if deps:
        lines.append("Dependencies: " + ", ".join(sorted(set(deps))[:_MAX_LIST_ITEMS * 2]))
    if top_files:
        # Caller's order is meaningful (most relevant first); keep it.
        lines.append("Top files: " + ", ".join(top_files[:_MAX_LIST_ITEMS]))
    lo, hi = _day(activity_min_iso), _day(activity_max_iso)
    if lo and hi:
        lines.append(f"Activity: {lo} to {hi}" if lo != hi else f"Activity: {lo}")
    elif lo or hi:
        lines.append(f"Activity: {lo or hi}")
    if _day(git_last_commit_iso):
        lines.append(f"Last commit: {_day(git_last_commit_iso)}")
    if readme_head and readme_head.strip():
        lines.append(f"README: {_truncate_tokens(readme_head, HEAD_TOKENS)}")
    return _card(CTYPE_FOLDER_CARD, lines)


def _truncate_tokens_lines(text: str, budget: int) -> str:
    """Drop whole lines from the end, then cut the last line by words."""
    lines = text.split("\n")
    while len(lines) > 1 and textnorm.estimate_tokens("\n".join(lines)) > budget:
        lines.pop()
    joined = "\n".join(lines)
    if textnorm.estimate_tokens(joined) <= budget:
        return joined
    return _truncate_tokens(joined, budget)


def make_caption_chunk(caption_text: str, *, ordinal: int = 0) -> Chunk:
    """A ``caption`` chunk for an image's rendered vision caption, cut to fit
    the embedding window."""
    return _card(CTYPE_CAPTION, [textnorm.normalize_ws(caption_text or "")], ordinal=ordinal)


def make_ocr_chunks(page: int, text: str, start_ordinal: int) -> list[Chunk]:
    """``ocr`` chunks for one scanned page's vision-OCR text: paragraphs
    chunked like body text (never with the legal overlay — a page is too
    small a window to judge that), each located at ``page``."""
    paras = [p for p in _PARA_SPLIT_RE.split(text or "") if p.strip()]
    blocks = [Block(text=p, locator={"page": int(page)}) for p in paras]
    chunks = _chunk(blocks, False, CTYPE_OCR)
    for i, c in enumerate(chunks):
        c.ordinal = start_ordinal + i
    return chunks


__all__ = [
    "CARD_ORDINAL",
    "make_caption_chunk",
    "make_file_card",
    "make_folder_card",
    "make_ocr_chunks",
]
