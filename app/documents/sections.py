"""Markdown section structure for documentation bodies.

This module is deliberately **pure**: no file or network I/O, no imports from
``app.tools`` / ``app.documents.sync``, and no tokenizer of its own — the
caller injects one as a plain callable. That keeps it cheap to import and
trivial to test, and lets ``documentation_search`` decide what a "token" means.

The shape it produces:

- ``split_sections`` slices a body into a *head* (everything before the first
  ``##``/``###``) plus an ordered list of :class:`Section` records.
- Only levels 2 and 3 are section boundaries. A single ``#`` is reserved for
  the document title by the authoring contract (``bundled/document.md``), and
  ``####`` and deeper are ordinary content.
- Fenced code blocks are tracked so that shell comments (``# do the thing``)
  inside a ``bash`` fence are never mistaken for headings.

Each section carries two ends: ``own_end`` stops at the next heading of *any*
boundary level, while ``end`` runs to the next heading of the same level or
shallower — so an ``##`` section's ``end`` includes its ``###`` children.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

__all__ = [
    "Section",
    "SectionSizes",
    "BOUNDARY_LEVELS",
    "split_sections",
    "section_text",
    "section_own_text",
    "children",
    "size_sections",
    "render_toc",
    "normalize_heading",
    "find_section",
    "rank_sections_for_query",
    "open_fence_at_end",
    "query_words",
]

# Heading levels that open a section. Everything else is content.
BOUNDARY_LEVELS = (2, 3)

# A fence opener: up to 3 leading spaces, then 3+ backticks or 3+ tildes.
# Group 2 is the info string, which is only ever allowed on an opener.
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")

# ATX heading, outside fences. The trailing run of '#' is a closing sequence.
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")

# Word-ish token: letters/digits, with '-' and '.' allowed *between* them so
# that `gpt-5.1` survives but a trailing `.` or `,` is dropped.
_WORD_RE = re.compile(r"[^\W_]+(?:[-.][^\W_]+)*", re.UNICODE)

_LEADING_HASHES_RE = re.compile(r"^#+\s*")
_WHITESPACE_RE = re.compile(r"\s+")

# Words of at least this length match by prefix, so "channel" finds
# "channels" and "group" finds "groups".
_STEM_MIN = 4

_MAX_SUGGESTIONS = 8


@dataclass(frozen=True)
class Section:
    """One ``##`` or ``###`` section of a document body."""

    level: int
    """2 or 3."""

    title: str
    """Heading text verbatim — backticks kept, no leading/trailing ``#``."""

    start: int
    """Index of the heading line itself."""

    own_end: int
    """Exclusive end at the next heading of *any* boundary level."""

    end: int
    """Exclusive end at the next heading of level ``<= self.level``."""

    index: int
    """0-based position in document order."""


@dataclass(frozen=True)
class SectionSizes:
    """Token counts for a section: ``own`` excludes children, ``total`` includes them."""

    own: int
    total: int


# --------------------------------------------------------------------------
# splitting
# --------------------------------------------------------------------------


def _is_fence_close(line: str, char: str, length: int) -> bool:
    """Does ``line`` close a fence opened with ``length`` copies of ``char``?

    CommonMark's closing-fence rule: same character, a run at least as long as
    the opener's, up to 3 leading spaces, and nothing but whitespace after it.
    """
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3:
        return False
    run = 0
    for ch in stripped:
        if ch != char:
            break
        run += 1
    if run < length:
        return False
    return stripped[run:].strip() == ""


class _FenceTracker:
    """Line-by-line fenced-code state, shared by every caller that needs it."""

    def __init__(self) -> None:
        self._char: Optional[str] = None
        self._len = 0

    def feed(self, line: str) -> bool:
        """Consume ``line``; True when it is a fence delimiter or fenced content."""
        if self._char is not None:
            if _is_fence_close(line, self._char, self._len):
                self._char, self._len = None, 0
            return True
        opener = _FENCE_OPEN_RE.match(line)
        if opener:
            marker, info = opener.group(1), opener.group(2)
            # A backtick fence's info string may not itself contain a backtick.
            if not (marker[0] == "`" and "`" in info):
                self._char, self._len = marker[0], len(marker)
                return True
        return False

    @property
    def closing_marker(self) -> Optional[str]:
        """The line that would close the fence still open, or None."""
        return self._char * self._len if self._char else None


def _heading_positions(lines: list[str]) -> list[tuple[int, int, str]]:
    """Return ``(line index, level, title)`` for every heading outside fences.

    An *unclosed* fence swallows the rest of the document: no heading is
    reported after it. That is the same thing a Markdown renderer does, and it
    keeps a doc with a stray fence from splitting into nonsense sections.
    """
    found: list[tuple[int, int, str]] = []
    fences = _FenceTracker()
    for i, line in enumerate(lines):
        if fences.feed(line):
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            found.append((i, len(heading.group(1)), heading.group(2)))
    return found


def open_fence_at_end(lines: list[str]) -> Optional[str]:
    """The marker that closes a code fence left open after ``lines``, or None.

    For callers that cut a document short: appending text after an unclosed
    fence would make it read as part of the code block.
    """
    fences = _FenceTracker()
    for line in lines:
        fences.feed(line)
    return fences.closing_marker


def split_sections(body: str) -> tuple[list[str], list[str], list[Section]]:
    """Split ``body`` into ``(lines, head, sections)``.

    - ``lines`` is the whole body split on newlines; every ``Section`` indexes
      into it, so callers must keep it around for :func:`section_text`.
    - ``head`` is the lines before the first ``##``/``###`` — the ``#`` title
      and any intro prose. With no boundary heading at all it is the whole body
      and ``sections`` is empty.
    """
    lines = body.splitlines()
    boundaries = [h for h in _heading_positions(lines) if h[1] in BOUNDARY_LEVELS]
    if not boundaries:
        return lines, list(lines), []

    head = lines[: boundaries[0][0]]

    sections: list[Section] = []
    for i, (start, level, title) in enumerate(boundaries):
        own_end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(lines)
        end = len(lines)
        for next_start, next_level, _ in boundaries[i + 1 :]:
            if next_level <= level:
                end = next_start
                break
        sections.append(
            Section(
                level=level,
                title=title,
                start=start,
                own_end=own_end,
                end=end,
                index=i,
            )
        )

    return lines, head, sections


def section_text(lines: list[str], section: Section) -> str:
    """The section including its children — what a section read returns."""
    return "\n".join(lines[section.start : section.end])


def section_own_text(lines: list[str], section: Section) -> str:
    """The section's own prose, stopping at its first child heading."""
    return "\n".join(lines[section.start : section.own_end])


def children(sections: list[Section], section: Section) -> list[Section]:
    """The deeper sections nested inside ``section``, in document order."""
    return [
        s
        for s in sections
        if s.level > section.level and section.start < s.start < section.end
    ]


def size_sections(
    lines: list[str],
    sections: list[Section],
    tokenizer: Callable[[str], int],
) -> dict[int, SectionSizes]:
    """Measure every section with ``tokenizer``, keyed by ``Section.index``."""
    return {
        s.index: SectionSizes(
            own=tokenizer(section_own_text(lines, s)),
            total=tokenizer(section_text(lines, s)),
        )
        for s in sections
    }


def render_toc(sections: list[Section], sizes: dict[int, SectionSizes]) -> str:
    """A one-line-per-section outline with inclusive token counts.

    The count is the *inclusive* total because that is what reading the section
    would actually cost the caller. ``sizes`` must cover every section passed
    in (build it with :func:`size_sections`); a gap raises ``KeyError`` rather
    than printing a made-up count.
    """
    return "\n".join(
        f"{'  ' * max(0, s.level - 2)}- {s.title} ({sizes[s.index].total})"
        for s in sections
    )


# --------------------------------------------------------------------------
# lookup
# --------------------------------------------------------------------------


def normalize_heading(text: str) -> str:
    """Fold a heading (or a user's guess at one) to a comparable key.

    Hyphens become spaces, so ``set args`` finds `` `cremind tools set-args` ``
    and ``coding agents`` finds ``coding-agents``: people (and models) type
    subcommand names both ways.
    """
    s = _LEADING_HASHES_RE.sub("", text.strip())
    s = s.replace("`", "").replace("-", " ")
    s = _WHITESPACE_RE.sub(" ", s).strip().lower()
    return s.rstrip(":. ").strip()


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text)


def query_words(text: str) -> list[str]:
    """The words of ``text`` exactly as the matchers here see them."""
    return _words(normalize_heading(text))


def _word_matches(a: str, b: str) -> bool:
    """Equal, or one is a prefix of the other once it is long enough to stem."""
    if a == b:
        return True
    if len(a) >= _STEM_MIN and b.startswith(a):
        return True
    if len(b) >= _STEM_MIN and a.startswith(b):
        return True
    return False


def _contains_words(haystack: list[str], needle: list[str]) -> bool:
    """Is ``needle`` a contiguous run of whole words inside ``haystack``?"""
    if not needle or len(needle) > len(haystack):
        return False
    for i in range(len(haystack) - len(needle) + 1):
        if haystack[i : i + len(needle)] == needle:
            return True
    return False


def _strip_cremind(text: str) -> str:
    return text[len("cremind ") :] if text.startswith("cremind ") else text


def find_section(
    sections: list[Section], query: str
) -> tuple[list[Section], list[str]]:
    """Resolve ``query`` to sections, or offer suggestions.

    Tiers are tried in order and the first that yields anything wins; its
    matches come back with empty suggestions. Callers read the result as:
    one match is the happy path, several sharing one normalized title are
    duplicate headings, and several distinct titles are genuinely ambiguous.
    """
    if not sections:
        return [], []

    nq = normalize_heading(query)
    if not nq:
        return [], []

    normalized = [normalize_heading(s.title) for s in sections]
    title_words = [_words(n) for n in normalized]
    qw = _words(nq)
    nq_bare = _strip_cremind(nq)

    tiers: list[Callable[[int], bool]] = [
        # 1. straight equality
        lambda i: normalized[i] == nq,
        # 2. equal once a leading "cremind " is dropped from both sides
        lambda i: _strip_cremind(normalized[i]) == nq_bare,
        # 3. the query is a whole-word run inside the title (compound headings)
        lambda i: _contains_words(title_words[i], qw),
        # 4. the title is a whole-word run inside the query (a pasted heading)
        lambda i: _contains_words(qw, title_words[i]),
        # 5. every query word hits a title word, stemming words >= 4 chars
        lambda i: bool(qw)
        and all(any(_word_matches(w, tw) for tw in title_words[i]) for w in qw),
    ]

    for tier_no, tier in enumerate(tiers, start=1):
        hits = [i for i in range(len(sections)) if tier(i)]
        if not hits:
            continue
        if tier_no == 4:
            hits = _drop_subsumed(hits, title_words)
        return [sections[i] for i in hits], []

    return [], _suggest(sections, normalized, nq, qw)


def _drop_subsumed(hits: list[int], title_words: list[list[str]]) -> list[int]:
    """Drop a hit whose title is a whole-word run inside a longer hit's title.

    Pasting "### `cremind clean components` — Flags" also contains a sibling
    heading that is just "Components"; the pasted heading is the one meant, so
    the shorter title it swallows must not turn the lookup ambiguous.
    Identical titles never subsume each other, so duplicate headings survive.
    """
    return [
        i
        for i in hits
        if not any(
            len(title_words[j]) > len(title_words[i])
            and _contains_words(title_words[j], title_words[i])
            for j in hits
        )
    ]


# A heading this similar to the query is suggested even when it shares no word.
_CLOSE_RATIO = 0.5


def _suggest(
    sections: list[Section],
    normalized: list[str],
    nq: str,
    qw: list[str],
) -> list[str]:
    """Best candidates first: headings sharing query words, and close spellings.

    Ranked by how many query words a heading answers to, then by overall
    spelling similarity, then document order. Ranking matters because in a
    CLI reference nearly every heading repeats the document's own name: a typo
    like "channels paring" shares "channels" with dozens of subcommands, and a
    plain document-order list would fill up with the first few of them and
    never reach "`cremind channels pair`".
    """
    scored: list[tuple[int, float, int, str]] = []
    for i, section in enumerate(sections):
        title_words = _words(normalized[i])
        shared = sum(
            1 for w in qw if any(_word_matches(w, tw) for tw in title_words)
        )
        ratio = difflib.SequenceMatcher(None, nq, normalized[i]).ratio()
        if shared == 0 and ratio < _CLOSE_RATIO:
            continue
        scored.append((shared, ratio, i, section.title))

    scored.sort(key=lambda row: (-row[0], -row[1], row[2]))

    out: list[str] = []
    seen: set[str] = set()
    for _, _, _, title in scored:
        if title in seen:
            continue
        seen.add(title)
        out.append(title)
        if len(out) >= _MAX_SUGGESTIONS:
            break
    return out


def rank_sections_for_query(
    sections: list[Section],
    query: str,
    stop_words: Iterable[str],
) -> list[Section]:
    """Order sections by how many query words their heading answers to.

    ``stop_words`` exists because the document's own name is usually repeated
    in the query ("cremind channels doc: how do I add one") and would otherwise
    match every heading in the file. When a parent and one of its children both
    score, only the child is kept: the parent's text is inclusive and would
    drag the whole file along with it.
    """
    stops = {w.lower() for w in stop_words}
    qw = [w for w in _words(normalize_heading(query)) if w not in stops]
    if not qw:
        return []

    scored: dict[int, int] = {}
    for s in sections:
        title_words = _words(normalize_heading(s.title))
        score = sum(
            1 for w in qw if any(_word_matches(w, tw) for tw in title_words)
        )
        if score:
            scored[s.index] = score

    by_index = {s.index: s for s in sections}
    keep = [
        by_index[i]
        for i in scored
        if not any(c.index in scored for c in children(sections, by_index[i]))
    ]
    keep.sort(key=lambda s: (-scored[s.index], s.index))
    return keep
