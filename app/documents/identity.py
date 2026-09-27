"""Which indexed document a person means by "Decree 165".

People name legal documents the short way — "Nghị định 165", "Decree No.
165", "ND 165" — while the document itself says "Số: 165/2024/NĐ-CP" and its
file may be called ``ND-165-2024-CP.pdf``. Matching those by filename words or
by search relevance failed both ways: a filename filter for "Nghị định 165"
matched nothing, a number search for "165" never equalled "165/2024/NĐ-CP",
and a topical relevance screen then threw the decree out. This module
resolves the identity *deterministically*, before any relevance judgement,
from what the index already holds: the legal metadata extraction found
(``doc_meta["legal"]``), the document's title, its header text and its file
name.

Rules (:func:`matches`):

- A complete identifier ("165/2024/NĐ-CP") matches only the same complete
  identifier (case, spacing and Đ/D aside).
- A short one needs an instrument cue — "Nghị định 165", "Decree 165", "NĐ
  165" — and matches the *number component*: 165 matches 165/2024/NĐ-CP,
  never 1650/… or 16/…. A bare "165" names nothing.
- Anything else the request supplies — a year, the kind of instrument, the
  issuer — must agree with the document when the document states it.
- A document's appendix ("Phụ lục Nghị định 165/2024/NĐ-CP",
  ``…_Phu-luc.pdf``) belongs to the same instrument: a related source, not a
  competing edition.
- One matching instrument resolves; several distinct ones (two years, two
  issuers) are ambiguous and the caller asks which.

Pure except :func:`resolve`, which reads the index.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from app.documents.textnorm import fold

# ── instrument kinds ───────────────────────────────────────────────────────

KIND_LAW = "law"
KIND_CODE = "code"
KIND_DECREE = "decree"
KIND_CIRCULAR = "circular"
KIND_JOINT_CIRCULAR = "joint_circular"
KIND_DECISION = "decision"
KIND_RESOLUTION = "resolution"
KIND_ORDINANCE = "ordinance"
KIND_DIRECTIVE = "directive"
KIND_CONSTITUTION = "constitution"
# A National Assembly number ("…/QH15") is a law or one of its resolutions.
KIND_ASSEMBLY = "assembly"

# Cue words, folded (no accents, lower case), longest first so "thong tu lien
# tich" wins over "thong tu" and "bo luat" over "luat".
_CUES: tuple[tuple[str, str], ...] = (
    ("thong tu lien tich", KIND_JOINT_CIRCULAR), ("joint circular", KIND_JOINT_CIRCULAR),
    ("bo luat", KIND_CODE), ("nghi dinh", KIND_DECREE), ("thong tu", KIND_CIRCULAR),
    ("quyet dinh", KIND_DECISION), ("nghi quyet", KIND_RESOLUTION), ("phap lenh", KIND_ORDINANCE),
    ("chi thi", KIND_DIRECTIVE), ("hien phap", KIND_CONSTITUTION), ("luat", KIND_LAW),
    ("decree", KIND_DECREE), ("circular", KIND_CIRCULAR), ("decision", KIND_DECISION),
    ("resolution", KIND_RESOLUTION), ("ordinance", KIND_ORDINANCE), ("directive", KIND_DIRECTIVE),
    ("constitution", KIND_CONSTITUTION), ("code", KIND_CODE), ("law", KIND_LAW), ("act", KIND_LAW),
    ("ttlt", KIND_JOINT_CIRCULAR), ("nd", KIND_DECREE), ("tt", KIND_CIRCULAR), ("qd", KIND_DECISION),
    ("nq", KIND_RESOLUTION), ("ct", KIND_DIRECTIVE),
)
# The abbreviation in an identifier's issuer part ("NĐ-CP", "TT-BTC").
_ISSUER_KIND = {
    "ND": KIND_DECREE, "TT": KIND_CIRCULAR, "TTLT": KIND_JOINT_CIRCULAR, "QD": KIND_DECISION,
    "NQ": KIND_RESOLUTION, "CT": KIND_DIRECTIVE, "PL": KIND_ORDINANCE,
}
_DISPLAY_ABBR = {"ND": "NĐ", "QD": "QĐ"}
_KIND_WORD_VI = {
    KIND_LAW: "Luật", KIND_CODE: "Bộ luật", KIND_DECREE: "Nghị định", KIND_CIRCULAR: "Thông tư",
    KIND_JOINT_CIRCULAR: "Thông tư liên tịch", KIND_DECISION: "Quyết định", KIND_RESOLUTION: "Nghị quyết",
    KIND_ORDINANCE: "Pháp lệnh", KIND_DIRECTIVE: "Chỉ thị", KIND_CONSTITUTION: "Hiến pháp",
}

_CUE_RE = "|".join(re.escape(c) for c, _k in _CUES)
# "165/2024/NĐ-CP", "31/2024/QH15", "05/2024/QĐ-TTg" — folded to upper case
# and Đ→D before matching (see :func:`norm_id`), so a typed "165/2024/nd-cp"
# counts too.
_COMPLETE_RE = re.compile(r"(?<![\w/])(\d{1,4})\s*/\s*((?:19|20)\d{2})\s*/\s*([A-Z]{1,8}(?:-[A-Z]{1,8}\d{0,2})*\d{0,2})"
                          r"(?![\w/])")
# A cue then a number: "nghi dinh 165", "decree no. 165 of 2024", "nd 165/2024".
_SHORT_RE = re.compile(
    rf"(?<![\w])({_CUE_RE})\s*(?:so|no\.?|number|#)?\s*(\d{{1,4}})(?![\w/])"
    r"(?:\s*(?:/|nam|of|,)?\s*((?:19|20)\d{2})(?!\d))?"
)
# A file name's form of an identifier: "ND-165-2024-CP", "TT_15_2023_BTC",
# "nd165-2024".
_FILENAME_RE = re.compile(
    r"(?<![a-z])(ttlt|nd|tt|qd|nq|ct)[-_ .]?(\d{1,4})[-_ .]((?:19|20)\d{2})(?:[-_ .]([a-z]{2,6}(?:-[a-z]{2,6})?))?(?![a-z0-9])"
)
_APPENDIX_RE = re.compile(r"(?<![a-z])(?:phu[\s_-]*luc|appendix|annex|annexes|appendices)(?![a-z])")
_LABELLED_RE = re.compile(r"(?:so|no\.?|number)\s*[:.]?\s*(\d{1,4}\s*/\s*(?:19|20)\d{2}\s*/\s*[a-z]{1,8}(?:-[a-z]{1,8}\d{0,2})*\d{0,2})")


def norm_id(value: str | None) -> str:
    """An identifier in its comparison spelling: no spaces, upper case,
    Đ→D ("165/2024/nđ-cp" → "165/2024/ND-CP")."""
    return re.sub(r"\s+", "", fold(str(value or ""))).upper()


@dataclass
class Parts:
    """An identifier split into its components (each None when unknown)."""

    num: int | None = None
    year: int | None = None
    issuer: str | None = None      # "ND-CP", "QH15", "TT-BTC" (comparison spelling)
    kind: str | None = None
    # The identifier as the document itself spells it ("165/2024/NĐ-CP").
    spelled: str | None = None

    @property
    def complete(self) -> bool:
        return self.num is not None and self.year is not None and bool(self.issuer)

    @property
    def id(self) -> str | None:
        """The complete identifier in comparison spelling, when complete."""
        return f"{self.num}/{self.year}/{self.issuer}" if self.complete else None

    def display(self) -> str | None:
        if not self.complete:
            return None
        if self.spelled:
            return self.spelled
        issuer = str(self.issuer)
        head, _, tail = issuer.partition("-")
        head = _DISPLAY_ABBR.get(head, head)
        return f"{self.num}/{self.year}/{head}{'-' + tail if tail else ''}"


def kind_of_issuer(issuer: str | None) -> str | None:
    if not issuer:
        return None
    head = issuer.split("-", 1)[0]
    if head in _ISSUER_KIND:
        return _ISSUER_KIND[head]
    if re.fullmatch(r"QH\d{0,2}", head):
        return KIND_ASSEMBLY
    if head.startswith("UBTVQH"):
        return KIND_ORDINANCE
    return None


_SPELLED_RE = re.compile(r"\d{1,4}\s*/\s*(?:19|20)\d{2}\s*/\s*[^\s,;()]+")


def parse_complete(text: str) -> Parts | None:
    """The first complete identifier in ``text``, with its own spelling kept
    when ``text`` spells it plainly."""
    m = _COMPLETE_RE.search(norm_id_spaced(text))
    if not m:
        return None
    issuer = m.group(3)
    parts = Parts(num=int(m.group(1)), year=int(m.group(2)), issuer=issuer, kind=kind_of_issuer(issuer))
    for s in _SPELLED_RE.findall(text or ""):
        cand = re.sub(r"\s+", "", s).rstrip(".:")
        if norm_id(cand) == parts.id:
            parts.spelled = cand
            break
    return parts


def norm_id_spaced(text: str) -> str:
    """``text`` folded and upper-cased, spacing kept (for regexes that need
    word boundaries)."""
    return fold(text or "").upper()


def kinds_agree(a: str | None, b: str | None) -> bool:
    if not a or not b or a == b:
        return True
    pair = {a, b}
    if KIND_ASSEMBLY in pair:
        return bool(pair & {KIND_LAW, KIND_CODE, KIND_RESOLUTION})
    return pair <= {KIND_LAW, KIND_CODE}


# ── what a request names ───────────────────────────────────────────────────


@dataclass
class DocRef:
    """A document a question or query names by its number."""

    raw: str
    parts: Parts = field(default_factory=Parts)

    @property
    def complete(self) -> bool:
        return self.parts.complete

    @property
    def label(self) -> str:
        return " ".join(self.raw.split())

    def to_dict(self) -> dict[str, Any]:
        return {"raw": self.raw, **asdict(self.parts)}


def parse_refs(text: str) -> list[DocRef]:
    """The documents ``text`` names by number: complete identifiers anywhere,
    and short numbers right after an instrument cue ("Nghị định 165",
    "Decree No. 165 of 2024", "ND 165"). A number with no cue ("165", "Article
    165") names nothing here. Each document once, in order."""
    out: list[DocRef] = []
    seen: set[tuple[Any, ...]] = set()
    src = text or ""
    folded = fold(src)
    for m in _COMPLETE_RE.finditer(folded.upper()):
        issuer = m.group(3)
        # The cue just before it, when there is one ("Nghị định số …").
        before = folded[max(0, m.start() - 30):m.start()]
        cue_kind = _cue_kind(before, tail=True)
        parts = Parts(num=int(m.group(1)), year=int(m.group(2)), issuer=issuer,
                      kind=kind_of_issuer(issuer) or cue_kind)
        key = ("c", parts.id)
        if key not in seen:
            seen.add(key)
            out.append(DocRef(raw=src[m.start():m.end()] if len(src) == len(folded) else m.group(0), parts=parts))
    covered = [(m.start(), m.end()) for m in _COMPLETE_RE.finditer(folded.upper())]
    for m in _SHORT_RE.finditer(folded):
        if any(a <= m.start(2) < b for a, b in covered):
            continue
        cue, num, year = m.group(1), m.group(2), m.group(3)
        kind = dict(_CUES)[cue]
        n = int(num)
        # "Luật 2024": a year after a cue is the year, not a number — unless
        # the text says "số"/"No." before it.
        explicit_no = bool(re.search(r"(?:so|no\.?|number|#)\s*$", folded[m.start(1):m.start(2)]))
        if year is None and 1900 <= n <= 2099 and len(num) == 4 and not explicit_no:
            continue
        parts = Parts(num=n, year=int(year) if year else None, kind=kind)
        key = ("s", parts.num, parts.year, parts.kind)
        if key in seen:
            continue
        seen.add(key)
        raw = src[m.start():m.end()] if len(src) == len(folded) else m.group(0)
        out.append(DocRef(raw=raw.strip(), parts=parts))
    return out


def _cue_kind(text: str, *, tail: bool = False) -> str | None:
    """The instrument kind a cue in ``text`` names (the last cue when
    ``tail``: the one right before a number)."""
    best: tuple[int, str] | None = None
    for cue, kind in _CUES:
        for m in re.finditer(rf"(?<![\w]){re.escape(cue)}(?![\w])", text):
            pos = m.start() if not tail else m.end()
            if best is None or (pos > best[0] if tail else pos < best[0]):
                best = (pos, kind)
    return best[1] if best else None


# ── what a document is ─────────────────────────────────────────────────────


@dataclass
class FileIdentity:
    """What identifies one indexed file as a legal document."""

    file_id: int
    fid: str
    rel_path: str
    name: str
    parts: Parts = field(default_factory=Parts)
    title: str | None = None
    appendix: bool = False
    # Where the number came from: legal_meta | title | header | filename.
    source: str | None = None

    def label(self) -> str:
        num = self.parts.display()
        if self.title and (not num or norm_id(num) in norm_id(self.title)):
            return " ".join(self.title.split())
        word = _KIND_WORD_VI.get(self.parts.kind or "", "")
        if num:
            return f"{'Phụ lục ' if self.appendix else ''}{word} {num}".strip()
        return self.name

    def to_dict(self) -> dict[str, Any]:
        return {"fid": self.fid, "file_id": self.file_id, "rel_path": self.rel_path, "label": self.label(),
                "number": self.parts.display(), "appendix": self.appendix, "source": self.source}


def file_identity(row: dict[str, Any], head_text: str | None = None) -> FileIdentity:
    """A file's identity from its row (legal metadata, title, name) and,
    when given, the text of its first passages."""
    meta = row.get("doc_meta") if isinstance(row.get("doc_meta"), dict) else {}
    legal = meta.get("legal") if isinstance(meta.get("legal"), dict) else {}
    title = str(meta.get("title") or "").strip() or None
    name = str(row.get("name") or str(row.get("rel_path") or "").rsplit("/", 1)[-1])
    ident = FileIdentity(file_id=int(row["id"]), fid=str(row.get("cite_id") or ""),
                         rel_path=str(row.get("rel_path") or ""), name=name, title=title)
    head = fold(head_text or "")[:3000]
    parts: Parts | None = None
    number = legal.get("number")
    if number:
        parts, ident.source = parse_complete(str(number)), "legal_meta"
    if parts is None and title:
        parts, ident.source = parse_complete(title), "title"
    if parts is None and head:
        m = _LABELLED_RE.search(head)
        if m:
            parts, ident.source = parse_complete(m.group(1)), "header"
            if parts is not None:
                # The header's own spelling ("NĐ-CP"), not the folded one.
                parts.spelled = next((re.sub(r"\s+", "", s).rstrip(".:")
                                      for s in _SPELLED_RE.findall(head_text or "")
                                      if norm_id(re.sub(r"\s+", "", s).rstrip(".:")) == parts.id), None)
    if parts is None:
        stem = fold(name.rsplit(".", 1)[0])
        fm = _FILENAME_RE.search(stem)
        if fm:
            abbr = fm.group(1).upper()
            suffix = (fm.group(4) or "").upper()
            issuer = f"{abbr}-{suffix}" if suffix else None
            parts = Parts(num=int(fm.group(2)), year=int(fm.group(3)), issuer=issuer,
                          kind=_ISSUER_KIND.get(abbr))
            ident.source = "filename"
    ident.parts = parts or Parts()
    if ident.parts.kind is None:
        ident.parts.kind = _cue_kind(fold(title or "")) or _cue_kind(head[:400])
    hay = " ".join(fold(x) for x in (title or "", name, head[:300]))
    ident.appendix = bool(_APPENDIX_RE.search(hay.replace("_", " ").replace("-", " ")))
    return ident


def matches(ref: DocRef, ident: FileIdentity) -> bool:
    """Whether ``ident`` is the document ``ref`` names (see the module
    docstring)."""
    rp, ip = ref.parts, ident.parts
    if rp.num is None or ip.num is None:
        return False
    if rp.complete:
        if ip.complete:
            return rp.id == ip.id
        # A file known only by number and year (its name) matches the parts
        # it has; its issuer is unknown.
        return rp.num == ip.num and rp.year == ip.year and kinds_agree(rp.kind, ip.kind)
    if rp.num != ip.num:
        return False
    if rp.year is not None and ip.year is not None and rp.year != ip.year:
        return False
    if rp.issuer and ip.issuer and rp.issuer != ip.issuer:
        return False
    return kinds_agree(rp.kind, ip.kind)


# ── resolving against the index ────────────────────────────────────────────

RESOLVED = "resolved"
AMBIGUOUS = "ambiguous"
NOT_FOUND = "not_found"


@dataclass
class Resolution:
    ref: DocRef
    status: str = NOT_FOUND
    # The instrument's full identity ("Nghị định 165/2024/NĐ-CP") when resolved.
    label: str | None = None
    number: str | None = None
    files: list[FileIdentity] = field(default_factory=list)
    # When ambiguous: one entry per distinct instrument.
    groups: list[dict[str, Any]] = field(default_factory=list)

    @property
    def main(self) -> list[FileIdentity]:
        return [f for f in self.files if not f.appendix] or list(self.files)

    def to_dict(self) -> dict[str, Any]:
        return {"ref": self.ref.to_dict(), "status": self.status, "label": self.label, "number": self.number,
                "files": [f.to_dict() for f in self.files], "groups": self.groups}


def _group_key(ident: FileIdentity) -> str:
    p = ident.parts
    return p.id or f"{p.kind or '?'}:{p.num}:{p.year or '?'}"


def group(ref: DocRef, idents: Iterable[FileIdentity]) -> Resolution:
    """The instruments among ``idents`` that ``ref`` names: resolved when
    they are one instrument (its appendices included), ambiguous when they
    are several."""
    hits = [i for i in idents if matches(ref, i)]
    res = Resolution(ref=ref)
    if not hits:
        return res
    groups: dict[str, list[FileIdentity]] = {}
    for i in hits:
        groups.setdefault(_group_key(i), []).append(i)
    known = [k for k in groups if "/" in k]
    loose = [k for k in groups if "/" not in k]
    if len(known) == 1:
        # Files known by number alone (no year or issuer to tell them
        # apart) go with the one complete instrument.
        for k in loose:
            groups[known[0]] += groups.pop(k)
    if len(groups) > 1:
        res.status = AMBIGUOUS
        res.groups = [{"number": members[0].parts.display() or key, "label": _pick_main(members).label(),
                       "files": [m.to_dict() for m in members]}
                      for key, members in sorted(groups.items())]
        res.files = [m for members in groups.values() for m in members]
        return res
    members = next(iter(groups.values()))
    members.sort(key=lambda m: (m.appendix, m.rel_path))
    main = _pick_main(members)
    res.status = RESOLVED
    res.files = members
    res.number = main.parts.display()
    res.label = main.label() if not main.appendix else (res.number or main.label())
    return res


def _pick_main(members: list[FileIdentity]) -> FileIdentity:
    return next((m for m in members if not m.appendix), members[0])


def _head_texts(db: Any, file_ids: list[int]) -> dict[int, str]:
    """The first few passages of each file (its header: "Số: …", the title
    lines) in one query."""
    out: dict[int, list[str]] = {}
    for start in range(0, len(file_ids), 400):
        batch = file_ids[start:start + 400]
        rows = db.read_sql(
            f"SELECT file_id, text FROM chunks WHERE file_id IN ({','.join('?' * len(batch))}) "
            "AND ctype NOT IN ('file_card', 'folder_card') AND ordinal BETWEEN 0 AND 2 ORDER BY file_id, ordinal",
            batch,
        )
        for r in rows:
            out.setdefault(int(r["file_id"]), []).append(str(r.get("text") or ""))
    return {k: "\n".join(v) for k, v in out.items()}


def candidates(db: Any, refs: list[DocRef], *, file_ids: Iterable[int] | None = None,
               hidden: Iterable[str] = ()) -> list[FileIdentity]:
    """Identities of the visible files that could be one of ``refs``: those
    whose name, metadata or header mentions a requested number. ``file_ids``
    limits the search to a scope."""
    nums = sorted({r.parts.num for r in refs if r.parts.num is not None})
    if not nums:
        return []
    hidden = set(hidden)
    like = " OR ".join(["name LIKE ? OR rel_path LIKE ? OR doc_meta LIKE ?"] * len(nums))
    params: list[Any] = []
    for n in nums:
        params += [f"%{n}%"] * 3
    where = f"status NOT IN ('missing', 'tombstone') AND ({like})"
    if file_ids is not None:
        ids = sorted({int(i) for i in file_ids})
        if not ids:
            return []
        rows: list[dict[str, Any]] = []
        for start in range(0, len(ids), 400):
            batch = ids[start:start + 400]
            rows += db.read_sql(f"SELECT * FROM files WHERE {where} AND id IN ({','.join('?' * len(batch))})",
                                [*params, *batch], table="files")
    else:
        rows = db.read_sql(f"SELECT * FROM files WHERE {where} LIMIT 2000", params, table="files")
    # Files whose header (not their name or metadata) carries the number.
    try:
        extra_ids = _header_hits(db, nums, file_ids)
    except Exception:  # noqa: BLE001 — the header search is a bonus
        extra_ids = []
    have = {int(r["id"]) for r in rows}
    if extra_ids:
        more = db.files_by_ids([i for i in extra_ids if i not in have])
        rows += [r for r in more.values() if r.get("status") not in ("missing", "tombstone")]
    rows = [r for r in rows if r.get("source") not in hidden]
    heads = _head_texts(db, [int(r["id"]) for r in rows])
    return [file_identity(r, heads.get(int(r["id"]))) for r in rows]


def _header_hits(db: Any, nums: list[int], file_ids: Iterable[int] | None) -> list[int]:
    """Files whose first passages mention one of ``nums`` next to a
    document-number label — found through the keyword index."""
    scope = None if file_ids is None else {int(i) for i in file_ids}
    out: list[int] = []
    for n in nums:
        try:
            rows = db.read_sql(
                "SELECT c.file_id AS file_id FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
                "WHERE chunks_fts MATCH ? AND c.ordinal BETWEEN 0 AND 2 AND c.file_id IS NOT NULL LIMIT 200",
                (f'"{n}"',),
            )
        except Exception:  # noqa: BLE001 — no FTS5 in this build: names and metadata only
            return out
        for r in rows:
            fid = int(r["file_id"])
            if (scope is None or fid in scope) and fid not in out:
                out.append(fid)
    return out


def resolve(db: Any, text_or_refs: str | list[DocRef], *, file_ids: Iterable[int] | None = None,
            hidden: Iterable[str] = ()) -> list[Resolution]:
    """Resolve every document ``text_or_refs`` names by number against the
    index (see the module docstring). ``file_ids`` keeps the resolution
    inside a scope."""
    refs = parse_refs(text_or_refs) if isinstance(text_or_refs, str) else list(text_or_refs)
    if not refs:
        return []
    idents = candidates(db, refs, file_ids=file_ids, hidden=hidden)
    return [group(r, idents) for r in refs]


__all__ = [
    "AMBIGUOUS", "DocRef", "FileIdentity", "NOT_FOUND", "Parts", "RESOLVED", "Resolution", "candidates",
    "file_identity", "group", "kinds_agree", "matches", "norm_id", "parse_complete", "parse_refs", "resolve",
]
