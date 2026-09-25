"""Fusing the ranked lists, and turning chunk hits into results.

The vector list and the keyword lists score on incomparable scales (a cosine
similarity, a bm25 value), so they are fused by rank alone with Reciprocal
Rank Fusion: a chunk scores ``Σ weight / (60 + rank)`` over the lists that
found it. That rewards agreement — a passage both lists rank highly beats one
that only a single list loves — without calibrating either score.

Weights, and why:

- vector 1.0 and lexical 1.0 — equal partners for ordinary prose;
- lexical 1.3 when the query holds identifiers or numbers ("45/2013/QH13",
  "2025", "invoice 1043"), which embeddings blur and keywords pin down;
- phrase 1.5 — an exact identifier or legal reference ("Điều 203") matching
  as a whole is the strongest evidence a query can give.

After fusion, identical text in several files (copies, attachments saved
twice) collapses to one hit that lists the other files in ``also_in``, and
hits are grouped the way the agent asked: by file (default), by folder (the
nearest project folder, else the parent — "the robot project", not its 40
source files), or not at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

RRF_K = 60
W_VECTOR = 1.0
W_LEXICAL = 1.0
W_LEXICAL_IDS = 1.3
W_PHRASE = 1.5

# A file's score: its best passage plus a little credit for a second one, so a
# file with two good passages edges out one with a single equally good one —
# but never a much better single passage.
SECOND_PASSAGE_CREDIT = 0.1
MAX_PASSAGES = 2

GROUP_BY = ("file", "folder", "chunk")


@dataclass
class RankedList:
    name: str               # vector | lexical | phrase
    weight: float
    ids: Sequence[int]      # chunk ids, best first


def rrf(lists: Sequence[RankedList], k: int = RRF_K) -> dict[int, tuple[float, dict[str, int]]]:
    """``{chunk id: (fused score, {list name: 1-based rank})}``. A chunk that
    appears twice in one list counts once, at its best rank."""
    out: dict[int, tuple[float, dict[str, int]]] = {}
    for lst in lists:
        seen: set[int] = set()
        for rank, cid in enumerate(lst.ids, start=1):
            if cid in seen:
                continue
            seen.add(cid)
            score, ranks = out.get(cid, (0.0, {}))
            ranks = dict(ranks)
            ranks[lst.name] = rank
            out[cid] = (score + lst.weight / (k + rank), ranks)
    return out


@dataclass
class Hit:
    """One chunk that matched, joined back to its file (or folder, for a
    folder card)."""

    chunk: dict[str, Any]
    file: dict[str, Any] | None
    folder: dict[str, Any] | None
    score: float
    ranks: dict[str, int] = field(default_factory=dict)
    also_in: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    # (column, epoch seconds) of the date that matched the filter (or the
    # date shown when there is none).
    date: tuple[str, float] | None = None
    # Neighbouring or same-section chunks shown for context.
    expansion: list[dict[str, Any]] = field(default_factory=list)

    @property
    def chunk_id(self) -> int:
        return int(self.chunk["id"])

    @property
    def confidence(self) -> str:
        """high: an exact phrase matched, or two lists both ranked it in
        their top ten; medium: one list ranked it top three; else low."""
        if "phrase" in self.ranks:
            return "high"
        good = [r for r in self.ranks.values() if r <= 10]
        if len(good) >= 2:
            return "high"
        if any(r <= 3 for r in self.ranks.values()):
            return "medium"
        return "low"


def dedupe_by_text(hits: list[Hit]) -> list[Hit]:
    """Keep the best hit per ``text_hash``; the files of the others go into
    its ``also_in`` (each file once, never the hit's own file). ``hits``
    must be best first."""
    kept: dict[str, Hit] = {}
    out: list[Hit] = []
    for h in hits:
        key = h.chunk.get("text_hash") or f"id:{h.chunk_id}"
        first = kept.get(key)
        if first is None:
            kept[key] = h
            out.append(h)
            continue
        if h.file is not None and (first.file is None or int(h.file["id"]) != int(first.file["id"])):
            if all(int(o["id"]) != int(h.file["id"]) for o in first.also_in):
                first.also_in.append(h.file)
    return out


@dataclass
class Group:
    """One result: a file, a folder, or (group_by=chunk) a single passage."""

    kind: str                               # file | folder
    key: tuple
    file: dict[str, Any] | None
    folder: dict[str, Any] | None
    score: float
    passages: list[Hit]
    # Best rank per list over ALL the group's hits, shown or not — the
    # evidence the group was found by.
    evidence: dict[str, int] = field(default_factory=dict)

    @property
    def best(self) -> Hit:
        return self.passages[0]


def group_hits(
    hits: list[Hit],
    by: str,
    *,
    folder_for_file: Callable[[dict[str, Any]], dict[str, Any] | None],
    max_passages: int = MAX_PASSAGES,
) -> list[Group]:
    """Group ``hits`` (best first) into results, best first.

    ``folder_for_file`` maps a file row to the folder that represents it when
    grouping by folder (nearest project folder, else its parent; None at the
    root). Folder-card hits always group under their own folder.
    """
    if by not in GROUP_BY:
        raise ValueError(f"group_by must be one of {', '.join(GROUP_BY)}")
    if by == "chunk":
        return [
            Group(kind="folder" if h.file is None else "file",
                  key=("c", h.chunk_id), file=h.file, folder=h.folder, score=h.score, passages=[h],
                  evidence=dict(h.ranks))
            for h in hits
        ]
    groups: dict[tuple, Group] = {}
    order: list[tuple] = []
    for h in hits:
        if h.file is None:
            folder = h.folder
            key = ("d", int(folder["id"])) if folder else ("c", h.chunk_id)
            kind, file_row = "folder", None
        elif by == "folder":
            folder = folder_for_file(h.file)
            key = ("d", int(folder["id"])) if folder else ("root", h.file.get("source"))
            kind, file_row = "folder", None
        else:
            folder = h.folder
            key = ("f", int(h.file["id"]))
            kind, file_row = "file", h.file
        g = groups.get(key)
        if g is None:
            groups[key] = Group(kind=kind, key=key, file=file_row, folder=folder, score=0.0, passages=[])
            order.append(key)
            g = groups[key]
        g.passages.append(h)
    out: list[Group] = []
    for key in order:
        g = groups[key]
        g.passages.sort(key=lambda x: -x.score)
        for h in g.passages:
            for name, rank in h.ranks.items():
                g.evidence[name] = min(rank, g.evidence.get(name, rank))
        second = g.passages[1].score if len(g.passages) > 1 else 0.0
        g.score = g.passages[0].score + SECOND_PASSAGE_CREDIT * second
        # A matching file card counts toward the score (name, path, dates and
        # content head all matched), but as a passage it only repeats the
        # body: show it when it is all the file has.
        body = [h for h in g.passages if h.chunk.get("ctype") != "file_card"]
        g.passages = (body or g.passages)[:max_passages]
        out.append(g)
    out.sort(key=lambda g: -g.score)
    return out


# Vector hits below this rank with no keyword support are the nearest
# neighbours of the query, not matches (vector search always returns k).
NOISE_VECTOR_RANK = 10


def drop_noise(groups: list[Group]) -> list[Group]:
    """Without calibrated per-model score floors, cut the tail that only the
    vector list produced — but only when keyword evidence exists elsewhere
    in the results, so a query that matches by meaning alone (another
    language, a paraphrase) keeps all of its results."""
    def keyword(g: Group) -> bool:
        return bool(set(g.evidence) - {"vector"})

    if not any(keyword(g) for g in groups):
        return groups
    return [g for g in groups if keyword(g) or g.evidence.get("vector", 10**9) <= NOISE_VECTOR_RANK]


def folder_resolver(folders: dict[int, dict[str, Any]]) -> Callable[[dict[str, Any]], dict[str, Any] | None]:
    """``folder_for_file`` over a ``{id: folder}`` map: walk up from the
    file's folder to the nearest project folder; without one, the file's own
    folder."""
    cache: dict[int, dict[str, Any] | None] = {}

    def nearest(file_row: dict[str, Any]) -> dict[str, Any] | None:
        start = file_row.get("folder_id")
        if start is None:
            return None
        start = int(start)
        if start in cache:
            return cache[start]
        node = folders.get(start)
        found = None
        hops = 0
        while node is not None and hops < 64:
            if node.get("is_project"):
                found = node
                break
            parent = node.get("parent_id")
            node = folders.get(int(parent)) if parent is not None else None
            hops += 1
        cache[start] = found or folders.get(start)
        return cache[start]

    return nearest


__all__ = [
    "GROUP_BY",
    "Group",
    "Hit",
    "MAX_PASSAGES",
    "RRF_K",
    "RankedList",
    "SECOND_PASSAGE_CREDIT",
    "W_LEXICAL",
    "W_LEXICAL_IDS",
    "W_PHRASE",
    "W_VECTOR",
    "dedupe_by_text",
    "drop_noise",
    "folder_resolver",
    "group_hits",
    "rrf",
]
