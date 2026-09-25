"""Legal structure overlay: statutes, decrees and contracts cut at articles.

A legal answer has to cite a provision, and a provision is an article
("Điều 12", "Article 12") or a clause of one. Cutting such a document by
paragraph size alone would split one article across two chunks and glue two
small ones together, so a citation could never point at exactly one article.
The overlay rewrites the extractor's blocks before the chunker sees them:

- every structural line starts its own block (PDF and plain-text extraction
  routinely deliver "Điều 5. …\\n1. …\\n2. …" as one paragraph);
- Phần/Chương/Mục/Tiểu mục, Part/Chapter/Section and Điều/Article become
  HARD anchors, so no chunk crosses an article and small articles are never
  merged; clauses ("1." inside an article, "(1)"/"(a)" in English) become
  SOFT anchors, so a long article splits at a clause; points ("a)") stay
  NONE — they are too small to stand alone;
- each block's locator gets ``article``/``clause``/``point`` and a breadcrumb
  ``heading`` list (["LUẬT ĐẤT ĐAI", "Chương II", "Điều 12", "Khoản 2"]).

Recognition is line-based and conservative. A structural keyword only counts
at the start of a line and only when what follows the number looks like a
heading (end of line, "." ":" "–", or a capital letter): a wrapped line that
happens to begin "Điều 5 của Luật này …" is a cross-reference, not an article.
Keywords must be Title Case or UPPER CASE, never lower case, for the same
reason. Labels are canonicalised ("CHƯƠNG II" → "Chương II") so the
breadcrumb — and therefore every chunk hash under it — does not depend on
the document's typography.

Cross-references and document metadata (number, issue and effective dates,
consolidation, repeals) are regex-only here; research falls back to an LLM
for what these miss.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Iterable

from app.userdocs.types import ANCHOR_HARD, ANCHOR_NONE, ANCHOR_SOFT, Block

# Breadcrumb stack levels. Articles sit below every grouping level so a new
# Chương pops the current Điều, and clauses sit below articles.
LV_PART = 1
LV_CHAPTER = 2
LV_SECTION = 3
LV_SUBSECTION = 4
LV_ARTICLE = 5
LV_CLAUSE = 6

_ROMAN = r"[IVXLCDM]+"
# The number must end the word: "MỤC LỤC" (table of contents) is not "Mục L".
_END = r"(?!\w)"

# (compiled pattern, level, canonical keyword). Order matters: "Tiểu mục"
# before "Mục".
_HEADING_PATTERNS: list[tuple[re.Pattern[str], int, str]] = [
    (re.compile(rf"^(?:Phần|PHẦN)\s+((?:thứ|Thứ|THỨ)\s+\w+|{_ROMAN}|\d+){_END}(.*)$"),
     LV_PART, "Phần"),
    (re.compile(rf"^(?:Chương|CHƯƠNG)\s+({_ROMAN}|\d+){_END}(.*)$"), LV_CHAPTER, "Chương"),
    (re.compile(rf"^(?:Tiểu mục|Tiểu Mục|TIỂU MỤC)\s+(\d+|{_ROMAN}){_END}(.*)$"),
     LV_SUBSECTION, "Tiểu mục"),
    (re.compile(rf"^(?:Mục|MỤC)\s+(\d+|{_ROMAN}){_END}(.*)$"), LV_SECTION, "Mục"),
    (re.compile(rf"^(?:Điều|ĐIỀU)\s+(\d+[a-z]?){_END}(.*)$"), LV_ARTICLE, "Điều"),
    (re.compile(rf"^(?:Part|PART)\s+({_ROMAN}|\d+){_END}(.*)$"), LV_PART, "Part"),
    (re.compile(rf"^(?:Chapter|CHAPTER)\s+({_ROMAN}|\d+){_END}(.*)$"), LV_CHAPTER, "Chapter"),
    (re.compile(rf"^(?:Section|SECTION)\s+(\d+(?:\.\d+)*|{_ROMAN}){_END}(.*)$"),
     LV_SECTION, "Section"),
    (re.compile(rf"^(?:Article|ARTICLE)\s+(\d+[A-Za-z]?){_END}(.*)$"), LV_ARTICLE, "Article"),
]
_VI_KEYWORDS = frozenset({"Phần", "Chương", "Tiểu mục", "Mục", "Điều"})
_HEADING_INITIALS = frozenset("PCTMĐSA")

_VI_CLAUSE_RE = re.compile(r"^(\d{1,3})\.(?!\d)\s*(?=\S)")
_VI_POINT_RE = re.compile(r"^([a-zđ])\)\s")
_EN_CLAUSE_RE = re.compile(r"^\((\d{1,3}|[a-z])\)\s*")

# How many leading blocks count as "the head" of a document for title,
# number, issue-date and consolidation detection.
_HEAD_BLOCKS = 40
LEGAL_MIN_ARTICLES = 5


def _rest_is_heading(rest: str) -> bool:
    """Does the text after "Điều 12" look like a heading, not running prose?"""
    s = rest.lstrip()
    if not s:
        return True
    c = s[0]
    # "(" admits "Điều 5 (được bãi bỏ)"; a comma or a lower-case word
    # ("Điều 5, Điều 6 và …", "Điều 5 của Luật này") is running text.
    return c in ".:–—-()" or c.isupper()


def _match_heading(line: str) -> tuple[int, str, str] | None:
    """(level, canonical keyword, number) when ``line`` is a structural heading."""
    s = line.strip()
    # Every keyword starts with one of these capitals; most lines of a long
    # document are rejected here without running nine patterns.
    if not s or s[0] not in _HEADING_INITIALS:
        return None
    for pat, level, kw in _HEADING_PATTERNS:
        m = pat.match(s)
        if m and _rest_is_heading(m.group(2)):
            num = m.group(1)
            if num[:3].lower() == "thứ":
                num = num.lower()
            return level, kw, num
    return None


def _is_article_line(line: str) -> bool:
    h = _match_heading(line)
    return h is not None and h[0] == LV_ARTICLE


def looks_legal(blocks: Iterable[Block]) -> bool:
    """True when the document has at least 5 article headings."""
    count = 0
    for b in blocks:
        for line in (b.text or "").splitlines():
            if _is_article_line(line):
                count += 1
                if count >= LEGAL_MIN_ARTICLES:
                    return True
    return False


# ── Title detection ────────────────────────────────────────────────────────

_VI_TITLE_RE = re.compile(
    r"^(?:BỘ LUẬT|LUẬT|NGHỊ ĐỊNH|THÔNG TƯ LIÊN TỊCH|THÔNG TƯ|NGHỊ QUYẾT|PHÁP LỆNH|"
    r"QUYẾT ĐỊNH|HIẾN PHÁP|HỢP ĐỒNG)(?!\w)"
)
_EN_TITLE_CAPS_RE = re.compile(r"\b(?:ACT|LAW|CODE|DECREE|REGULATIONS?|CONSTITUTION|AGREEMENT|CONTRACT)\b")
_EN_TITLE_RE = re.compile(
    r"^(?:The\s+)?(?:[A-Z][\w'’-]*\s+){0,8}(?:Act|Law|Code|Decree|Agreement|Contract)"
    r"(?:\s+(?:of\s+)?\d{4})?$"
)
_TITLE_MAX_CHARS = 120


def _is_upper_line(s: str) -> bool:
    return any(c.isalpha() for c in s) and s == s.upper()


def _detect_title(blocks: list[Block]) -> str | None:
    """The document's own name ("LUẬT ĐẤT ĐAI", "THE LAND ACT 2020"), looked
    for above the first structural heading. The keyword may sit alone on its
    line with the name on the next ("LUẬT" / "ĐẤT ĐAI")."""
    lines: list[str] = []
    for b in blocks[:_HEAD_BLOCKS]:
        for line in (b.text or "").splitlines():
            s = line.strip()
            if not s:
                continue
            if _match_heading(s):
                break
            lines.append(s)
        else:
            continue
        break
    for i, s in enumerate(lines):
        if len(s) > _TITLE_MAX_CHARS or not _is_upper_line(s):
            if _EN_TITLE_RE.match(s) and len(s) <= _TITLE_MAX_CHARS:
                return s
            continue
        if _VI_TITLE_RE.match(s):
            if " " not in s and i + 1 < len(lines):
                nxt = lines[i + 1]
                if _is_upper_line(nxt) and len(s) + len(nxt) < _TITLE_MAX_CHARS:
                    return f"{s} {nxt}"
            return s
        if _EN_TITLE_CAPS_RE.search(s) and not s.startswith(("NO.", "NUMBER")):
            return s
    return None


# ── The overlay ────────────────────────────────────────────────────────────


class _State:
    """Where in the legal hierarchy the scan currently is."""

    def __init__(self, title: str | None) -> None:
        self.title = title
        self.stack: list[tuple[int, str]] = []
        self.article: str | None = None
        self.clause: str | None = None
        self.point: str | None = None
        self.vi = True

    def push(self, level: int, label: str) -> None:
        while self.stack and self.stack[-1][0] >= level:
            self.stack.pop()
        self.stack.append((level, label))

    def path(self) -> list[str]:
        head = [self.title] if self.title else []
        return head + [label for _, label in self.stack]


def _classify(line: str, st: _State) -> tuple[str, int, int] | None:
    """Update ``st`` for a structural line; return (kind, anchor, level).

    kind is "heading", "clause" or "point"; None for ordinary text.
    """
    h = _match_heading(line)
    if h:
        level, kw, num = h
        st.vi = kw in _VI_KEYWORDS
        st.push(level, f"{kw} {num}")
        if level == LV_ARTICLE:
            st.article = num
        else:
            st.article = None
        st.clause = st.point = None
        return "heading", ANCHOR_HARD, level
    if st.article is None:
        return None
    s = line.lstrip()
    if st.vi:
        m = _VI_CLAUSE_RE.match(s)
        if m:
            st.clause, st.point = m.group(1), None
            st.push(LV_CLAUSE, f"Khoản {st.clause}")
            return "clause", ANCHOR_SOFT, 0
        m = _VI_POINT_RE.match(s)
        if m:
            # Usually inside a clause, but some articles list points directly.
            st.point = m.group(1)
            return "point", ANCHOR_NONE, 0
        return None
    m = _EN_CLAUSE_RE.match(s)
    if m:
        tag = m.group(1)
        if tag.isdigit() or st.clause is None or not st.clause.isdigit():
            # "(1)", or a lettered item directly under the article: a clause.
            st.clause, st.point = tag, None
            st.push(LV_CLAUSE, f"({tag})")
        else:
            # "(a)" inside "(1)": a point of that clause.
            st.point = tag
        return "clause", ANCHOR_SOFT, 0
    return None


def _set_legal_locator(loc: dict[str, Any], st: _State) -> None:
    for key, val in (("article", st.article), ("clause", st.clause), ("point", st.point)):
        if val is None:
            loc.pop(key, None)
        else:
            loc[key] = val
    loc["heading"] = st.path()


def apply_legal_structure(blocks: list[Block]) -> list[Block]:
    """Return a new block list with legal structure made explicit (see the
    module docstring). The input blocks are not modified."""
    st = _State(_detect_title(blocks))
    out: list[Block] = []
    prev_section_heading = False
    for b in blocks:
        text = (b.text or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        # Segments: (first line index, classification or None). A new segment
        # starts at every structural line; the first one may be plain text.
        segs: list[tuple[int, tuple[str, int, int] | None, dict[str, Any]]] = []
        for i, line in enumerate(lines):
            cls = _classify(line, st)
            if cls is not None or i == 0:
                loc = dict(b.locator)
                _set_legal_locator(loc, st)
                segs.append((i, cls, loc))
        base_line = b.locator.get("line_start")
        for n, (start, cls, loc) in enumerate(segs):
            end = segs[n + 1][0] if n + 1 < len(segs) else len(lines)
            seg_text = "\n".join(lines[start:end])
            if isinstance(base_line, int) and len(segs) > 1:
                loc["line_start"] = base_line + start
                loc["line_end"] = base_line + end - 1
            if cls is None:
                # Plain text keeps what the extractor said about it — except a
                # short all-caps line right under a Chương/Mục heading, which is
                # that heading's title ("QUY ĐỊNH CHUNG") and must travel with
                # it rather than become a chunk of its own.
                role, level, anchor = b.role, b.level, b.anchor
                one = seg_text.strip()
                if (prev_section_heading and one and "\n" not in one
                        and len(one) <= 200 and _is_upper_line(one)):
                    role, level, anchor = "heading", st.stack[-1][0] if st.stack else 1, ANCHOR_NONE
                out.append(Block(text=seg_text, anchor=anchor, level=level, role=role, locator=loc))
                prev_section_heading = False
                continue
            kind, anchor, level = cls
            if kind == "heading":
                out.append(Block(text=seg_text, anchor=anchor, level=level, role="heading",
                                 locator=loc))
                prev_section_heading = level < LV_ARTICLE
            else:
                role = "para" if b.role == "heading" else b.role
                out.append(Block(text=seg_text, anchor=anchor, level=0, role=role, locator=loc))
                prev_section_heading = False
    return out


def section_key_for(locator: dict[str, Any]) -> str | None:
    """'art:12' or 'art:12/cl:2' — the provision a chunk lies wholly within."""
    art = locator.get("article")
    if not art:
        return None
    cl = locator.get("clause")
    return f"art:{art}/cl:{cl}" if cl else f"art:{art}"


# ── Cross-references ───────────────────────────────────────────────────────

_VI_DOC_KW = r"(?:Bộ luật|Luật|Nghị định|Thông tư|Nghị quyết|Pháp lệnh|Hiến pháp)"
# A cited document's name runs to the next punctuation, but never into the
# next reference ("Điều 3 của Luật này và Điều 5 Nghị định này" is two).
_VI_DOC_CH = r"(?:(?!\b(?:và|hoặc)\s+(?:Điều|điều|khoản|Khoản|điểm|Điểm)\b|\b(?:Điều|điều)\s+\d)[^,.;()\n])"
_VI_REF_RE = re.compile(
    r"(?:(?:điểm|Điểm)\s+(?P<point>[a-zđ])\s*,?\s+)?"
    r"(?:(?:khoản|Khoản)\s+(?P<clause>\d+)\s*,?\s+)?"
    r"(?:Điều|điều|ĐIỀU)\s+(?P<article>\d+[a-z]?)(?!\w)"
    rf"(?:\s+(?:của\s+)?(?P<doc>{_VI_DOC_KW}{_VI_DOC_CH}{{0,80}}))?"
)
_VI_SELF_RE = re.compile(rf"^{_VI_DOC_KW}\s+này(?!\w)")
# Words that end a law's name when the name runs on into the sentence
# ("Điều 12 của Luật Đất đai quy định …"). Deliberately short: "và", "theo",
# "với" occur inside real law names.
_VI_DOC_STOP_RE = re.compile(
    r"\s+(?:quy định|thì|được|là|để|khi|nếu|mà|đã|sẽ|phải|có hiệu lực|hướng dẫn)(?!\w)"
)
_EN_DOC_KW = r"(?:Law|Act|Code|Decree|Regulation|Agreement)"
_EN_REF_RE = re.compile(
    r"\b(?:Article|article|ARTICLE|Art\.)\s+(?P<article>\d+[A-Za-z]?)(?![\w])"
    r"(?:\((?P<clause>\d+)\))?(?:\((?P<point>[a-z])\))?"
    rf"(?:\s+of\s+(?:(?P<self>this\s+{_EN_DOC_KW})|(?:the\s+)?"
    rf"(?P<doc>[A-Z][\w\s,-]{{2,80}}?{_EN_DOC_KW}))(?!\w))?"
)
_EN_SECTION_REF_RE = re.compile(
    r"(?:\b(?:Section|section|SECTION)\s+|§\s*)(?P<section>\d+(?:\.\d+)*)(?![\w.]\d)"
    r"(?:\((?P<clause>\d+)\))?"
    rf"(?:\s+of\s+(?:(?P<self>this\s+{_EN_DOC_KW})|(?:the\s+)?"
    rf"(?P<doc>[A-Z][\w\s,-]{{2,80}}?{_EN_DOC_KW}))(?!\w))?"
)


def _at_heading_line(text: str, pos: int) -> bool:
    """Is the match at ``pos`` the article/section heading line itself?"""
    line_start = text.rfind("\n", 0, pos) + 1
    if text[line_start:pos].strip():
        return False
    line_end = text.find("\n", pos)
    line = text[line_start: line_end if line_end >= 0 else len(text)]
    return _match_heading(line) is not None


def _clean_vi_doc(doc: str | None) -> tuple[str | None, str]:
    """(doc value, the text of the name as cited) for a captured name."""
    if not doc:
        return None, ""
    m = _VI_SELF_RE.match(doc)
    if m:
        return "self", m.group(0)
    m = _VI_DOC_STOP_RE.search(doc)
    if m:
        doc = doc[: m.start()]
    doc = doc.strip()
    return (doc or None), doc


def extract_refs(text: str) -> list[dict[str, Any]]:
    """Cross-references in ``text``, in order of appearance, deduplicated.

    Each is ``{raw, article, clause, point, doc}``; ``doc`` is None (same
    document implied), ``"self"`` ("Luật này", "this Act") or the cited
    document's name. English "Section n(.m)" references carry ``section``
    instead of ``article``. A heading line ("Điều 12. …") is not a reference.
    """
    if not text:
        return []
    found: list[tuple[int, dict[str, Any]]] = []
    for m in _VI_REF_RE.finditer(text):
        if _at_heading_line(text, m.start()):
            continue
        doc, cited = _clean_vi_doc(m.group("doc"))
        raw = m.group(0)
        if m.group("doc"):
            # Trim ``raw`` to the document name as cleaned.
            raw = raw[: m.start("doc") - m.start()] + cited
        found.append((m.start(), {
            "raw": raw.strip(), "article": m.group("article"), "clause": m.group("clause"),
            "point": m.group("point"), "doc": doc,
        }))
    for m in _EN_REF_RE.finditer(text):
        if _at_heading_line(text, m.start()):
            continue
        doc = "self" if m.group("self") else (m.group("doc") or None)
        found.append((m.start(), {
            "raw": m.group(0).strip(), "article": m.group("article"), "clause": m.group("clause"),
            "point": m.group("point"), "doc": doc.strip() if doc and doc != "self" else doc,
        }))
    for m in _EN_SECTION_REF_RE.finditer(text):
        if _at_heading_line(text, m.start()):
            continue
        doc = "self" if m.group("self") else (m.group("doc") or None)
        found.append((m.start(), {
            "raw": m.group(0).strip(), "article": None, "clause": m.group("clause"),
            "point": None, "doc": doc.strip() if doc and doc != "self" else doc,
            "section": m.group("section"),
        }))
    found.sort(key=lambda t: t[0])
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for _, ref in found:
        key = (ref["article"], ref["clause"], ref["point"], ref["doc"], ref.get("section"))
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    return out


# ── Document metadata ──────────────────────────────────────────────────────

# "31/2024/QH15", "102/2024/NĐ-CP", "15/2023/TT-BTC", "05/2024/QĐ-TTg". The
# hyphenated issuer suffix allows lower case ("TTg" is the Prime Minister).
_DOC_NUM = r"\d{1,4}/\d{4}/[A-ZĐ]{1,6}(?:-[A-ZĐ][A-Za-zĐ]{0,7})*\d{0,2}"
_DOC_NUM_RE = re.compile(rf"(?<![\w/]){_DOC_NUM}(?![\w/])")
_NUMBER_NEAR_RE = re.compile(
    rf"(?:Số|số|SỐ|No\.|NO\.|Number|number)\s*[:.]?\s*({_DOC_NUM})(?![\w/])"
)
_VI_DATE = r"ngày\s+(\d{1,2})\s+tháng\s+(\d{1,2})\s+năm\s+(\d{4})"
_VI_DATE_SLASH = r"ngày\s+(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})"
_VI_PLACE_DATE_RE = re.compile(rf",\s*{_VI_DATE}", re.IGNORECASE)
_VI_DATE_RE = re.compile(_VI_DATE, re.IGNORECASE)
_VI_PASSED_RE = re.compile(rf"thông\s+qua\s+{_VI_DATE}", re.IGNORECASE)
_VI_EFFECTIVE_RE = re.compile(
    r"có\s+hiệu\s+lực(?:\s+thi\s+hành)?\s+(?:kể\s+)?từ\s+"
    rf"(?:{_VI_DATE}|{_VI_DATE_SLASH}|(?P<signing>ngày\s+(?:ký|ban\s+hành)))",
    re.IGNORECASE,
)
_VI_SELF_DOC_RE = re.compile(
    r"(?:Bộ luật|Luật|Nghị định|Thông tư|Nghị quyết|Quyết định|Pháp lệnh|Văn bản)\s+này",
    re.IGNORECASE,
)
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
_EN_DATE = (
    r"(?:(?P<mon1>[A-Z][a-z]+)\s+(?P<day1>\d{1,2}),?\s+(?P<year1>\d{4})"
    r"|(?P<day2>\d{1,2})\s+(?P<mon2>[A-Z][a-z]+),?\s+(?P<year2>\d{4}))"
)
_EN_EFFECTIVE_RE = re.compile(
    r"(?:shall\s+(?:take\s+effect|come\s+into\s+(?:force|effect)|enter\s+into\s+force)"
    r"|(?:takes|took)\s+effect|(?:comes|came)\s+into\s+force)"
    rf"\s+(?:on|from|as\s+of)\s+(?:the\s+)?{_EN_DATE}"
)
_EN_ISSUED_RE = re.compile(
    rf"(?:[Dd]ated|[Aa]dopted\s+on|[Ee]nacted\s+on|[Pp]assed\s+on|[Ss]igned\s+on|[Dd]one\s+at\s+[^,\n]+,\s+on)"
    rf"\s+(?:the\s+)?{_EN_DATE}"
)
_REPEAL_RE = re.compile(
    r"hết\s+hiệu\s+lực|bãi\s+bỏ|thay\s+thế|\brepeal(?:s|ed)?\b|\breplace[sd]?\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;!?])\s+|\n+")


def _iso(y: str, m: str, d: str) -> str | None:
    try:
        return _dt.date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return None


def _iso_vi(m: re.Match[str], offset: int = 1) -> str | None:
    d, mo, y = m.group(offset), m.group(offset + 1), m.group(offset + 2)
    if d is None:
        return None
    return _iso(y, mo, d)


def _iso_en(m: re.Match[str]) -> str | None:
    if m.group("mon1"):
        mon, day, year = m.group("mon1"), m.group("day1"), m.group("year1")
    else:
        mon, day, year = m.group("mon2"), m.group("day2"), m.group("year2")
    month = _MONTHS.get(mon.lower())
    return _iso(year, str(month), day) if month else None


def detect_legal_meta(blocks: list[Block]) -> dict[str, Any]:
    """Regex-level legal metadata for ``doc_meta.legal``.

    Keys, each present only when found: ``number`` ("31/2024/QH15", next to
    "Số"/"Luật số"/"No."), ``issued`` and ``effective`` (ISO dates),
    ``consolidated`` (True for a "VĂN BẢN HỢP NHẤT"), ``repeals`` (document
    numbers named in a "hết hiệu lực"/"bãi bỏ"/"thay thế"/"repeal" sentence).
    """
    texts = [b.text or "" for b in blocks]
    head = "\n".join(texts[:_HEAD_BLOCKS])
    full = "\n".join(texts)
    meta: dict[str, Any] = {}

    m = _NUMBER_NEAR_RE.search(head)
    if m:
        meta["number"] = m.group(1)

    # Issue date: the "Hà Nội, ngày …" line of a decree/circular header; a
    # law has none and states "… thông qua ngày …" at its end instead; else
    # the first date in the head that is not inside a "Căn cứ" (legal basis)
    # line, which cites *other* documents' dates.
    head_lines = [
        line for line in head.split("\n") if not line.strip().lower().startswith("căn cứ")
    ]
    issued = None
    for line in head_lines:
        m = _VI_PLACE_DATE_RE.search(line)
        if m and (issued := _iso_vi(m)):
            break
    if not issued:
        m = _VI_PASSED_RE.search(full)
        if m:
            issued = _iso_vi(m)
    if not issued:
        for line in head_lines:
            m = _VI_DATE_RE.search(line)
            if m and (issued := _iso_vi(m)):
                break
    if not issued:
        m = _EN_ISSUED_RE.search(head)
        if m:
            issued = _iso_en(m)
    if issued:
        meta["issued"] = issued

    # Effective date: prefer the sentence about this document itself ("Luật
    # này có hiệu lực …") over a transitional clause about one provision.
    effective = None
    fallback = None
    for m in _VI_EFFECTIVE_RE.finditer(full):
        if m.group("signing"):
            val = issued
        else:
            val = _iso_vi(m, 1) or _iso_vi(m, 4)
        if not val:
            continue
        sent_start = max(full.rfind(".", 0, m.start()), full.rfind("\n", 0, m.start())) + 1
        if _VI_SELF_DOC_RE.search(full[sent_start:m.start()]):
            effective = val
            break
        if fallback is None:
            fallback = val
    effective = effective or fallback
    if not effective:
        m = _EN_EFFECTIVE_RE.search(full)
        if m:
            effective = _iso_en(m)
    if effective:
        meta["effective"] = effective

    if re.search(r"văn bản hợp nhất|consolidated (?:text|version)", head, re.IGNORECASE):
        meta["consolidated"] = True

    repeals: list[str] = []
    own = meta.get("number")
    for sent in _SENTENCE_SPLIT_RE.split(full):
        if not _REPEAL_RE.search(sent):
            continue
        for num in _DOC_NUM_RE.findall(sent):
            if num != own and num not in repeals:
                repeals.append(num)
    if repeals:
        meta["repeals"] = repeals
    return meta


__all__ = [
    "apply_legal_structure",
    "detect_legal_meta",
    "extract_refs",
    "looks_legal",
    "section_key_for",
]
