"""Reading a file in model-sized windows, every passage with its token.

Research reads files in full, but a model call takes at most a few thousand
tokens of document at a time. A window is a run of a file's chunks, cut at
section boundaries (a legal article, a top-level heading, a sheet, a slide)
where it can be, each chunk printed after its citation token:

    [doc:k7m2xq9a#1f3c9a0b] Điều 203, khoản 2
    <the chunk's text>

The model may cite only tokens printed in the window it was shown
(:attr:`Window.allowed`), so an answer cannot point at a passage the job did
not read. Document text is data: citation look-alikes inside it are
neutralised, and the window is wrapped as untrusted content before it goes
into a prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from app.tools.builtin.external_content import wrap_document_content
from app.documents import types as t
from app.documents.cite import escape_in_document_text, locator_label, make_token
from app.documents.research.context import estimate_tokens

WINDOW_TOKENS = 6000
_SKIP_CTYPES = frozenset({t.CTYPE_FILE_CARD, t.CTYPE_FOLDER_CARD})


@dataclass
class ChunkRef:
    """A chunk the model was shown, with what verification needs: the
    file it belongs to and its neighbours' text (a quote may straddle a
    chunk boundary)."""

    token: str
    chunk: dict[str, Any]
    file: dict[str, Any]
    prev_text: str = ""
    next_text: str = ""

    @property
    def label(self) -> str:
        return locator_label(self.chunk.get("locator") or {})

    @property
    def text(self) -> str:
        return self.chunk.get("text") or ""


@dataclass
class Window:
    file: dict[str, Any]
    chunks: list[dict[str, Any]]
    text: str                                   # the rendered window (tokens + texts)
    allowed: dict[str, ChunkRef] = field(default_factory=dict)
    tokens: int = 0                             # estimated size
    index: int = 0                              # 0-based position among the file's windows
    count: int = 1                              # how many windows the file has

    @property
    def label(self) -> str:
        """"HopDong.pdf, p. 1–4 (1/3)" — for progress lines."""
        first = locator_label(self.chunks[0].get("locator") or {}) if self.chunks else ""
        last = locator_label(self.chunks[-1].get("locator") or {}) if self.chunks else ""
        where = first if first == last or not last else f"{first} – {last}"
        part = f" ({self.index + 1}/{self.count})" if self.count > 1 else ""
        return f"{self.file.get('name') or self.file.get('rel_path')}{', ' + where if where else ''}{part}"

    def prompt_text(self) -> str:
        """The window as it goes into a prompt: delimited as untrusted data."""
        return wrap_document_content(self.text)


def chunk_token(file_row: dict[str, Any], chunk: dict[str, Any]) -> str:
    return make_token(file_row["cite_id"], chunk.get("text_hash") or "")


def chunk_tokens_est(chunk: dict[str, Any]) -> int:
    est = chunk.get("token_est")
    return int(est) if isinstance(est, int) and est > 0 else estimate_tokens(chunk.get("text") or "")


def render_chunk(file_row: dict[str, Any], chunk: dict[str, Any]) -> str:
    token = chunk_token(file_row, chunk)
    loc = chunk.get("locator") or {}
    label = locator_label(loc)
    heading = chunk.get("heading") or ""
    head = " ".join(x for x in (token, label, f"— {heading}" if heading and heading not in label else "") if x)
    return f"{head}\n{escape_in_document_text((chunk.get('text') or '').strip())}\n"


def _section_of(chunk: dict[str, Any]) -> str:
    """What a window prefers not to cut through: the top-level legal unit
    (``art:12``), else the first heading level, the sheet, the slide."""
    key = chunk.get("section_key") or ""
    if key:
        return key.split("/", 1)[0]
    loc = chunk.get("locator") or {}
    heading = loc.get("heading")
    if isinstance(heading, list) and heading:
        return "h:" + str(heading[0])
    if loc.get("sheet"):
        return "sheet:" + str(loc["sheet"])
    if loc.get("slide"):
        return "slide:" + str(loc["slide"])
    return ""


def body_chunks(chunks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The chunks that carry content (cards left out), in reading order."""
    return [c for c in chunks if c.get("ctype") not in _SKIP_CTYPES]


def build_windows(
    file_row: dict[str, Any], chunks: list[dict[str, Any]], *, max_tokens: int = WINDOW_TOKENS,
) -> list[Window]:
    """Cut ``chunks`` (one file's, in reading order; cards are dropped) into
    windows of at most ``max_tokens``, at section boundaries where possible.
    A section larger than a window is cut between chunks."""
    body = body_chunks(chunks)
    if not body:
        return []
    # Group consecutive chunks by section.
    sections: list[list[dict[str, Any]]] = []
    for c in body:
        if sections and _section_of(c) == _section_of(sections[-1][-1]):
            sections[-1].append(c)
        else:
            sections.append([c])
    groups: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    cur_tokens = 0
    for sec in sections:
        size = sum(chunk_tokens_est(c) for c in sec) + 12 * len(sec)
        if cur and cur_tokens + size > max_tokens:
            groups.append(cur)
            cur, cur_tokens = [], 0
        if size > max_tokens:
            for c in sec:
                cs = chunk_tokens_est(c) + 12
                if cur and cur_tokens + cs > max_tokens:
                    groups.append(cur)
                    cur, cur_tokens = [], 0
                cur.append(c)
                cur_tokens += cs
            continue
        cur += sec
        cur_tokens += size
    if cur:
        groups.append(cur)

    index = {int(c["id"]): i for i, c in enumerate(body)}
    windows: list[Window] = []
    for gi, group in enumerate(groups):
        allowed: dict[str, ChunkRef] = {}
        parts: list[str] = []
        for c in group:
            i = index[int(c["id"])]
            token = chunk_token(file_row, c)
            allowed[token] = ChunkRef(
                token=token, chunk=c, file=file_row,
                prev_text=(body[i - 1].get("text") or "") if i > 0 else "",
                next_text=(body[i + 1].get("text") or "") if i + 1 < len(body) else "",
            )
            parts.append(render_chunk(file_row, c))
        text = "\n".join(parts)
        windows.append(Window(file=file_row, chunks=group, text=text, allowed=allowed,
                              tokens=estimate_tokens(text), index=gi, count=len(groups)))
    return windows


def refs_for(file_row: dict[str, Any], chunks: list[dict[str, Any]]) -> dict[str, ChunkRef]:
    """Allowed-token map for an arbitrary list of one file's chunks (e.g. an
    article expanded in full), with neighbours taken from that list."""
    out: dict[str, ChunkRef] = {}
    for i, c in enumerate(chunks):
        token = chunk_token(file_row, c)
        out[token] = ChunkRef(
            token=token, chunk=c, file=file_row,
            prev_text=(chunks[i - 1].get("text") or "") if i > 0 else "",
            next_text=(chunks[i + 1].get("text") or "") if i + 1 < len(chunks) else "",
        )
    return out


__all__ = [
    "ChunkRef", "WINDOW_TOKENS", "Window", "body_chunks", "build_windows", "chunk_token", "chunk_tokens_est",
    "refs_for", "render_chunk",
]
