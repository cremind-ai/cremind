"""Data contracts shared by the Documentation search pipeline stages.

discover → extract → chunk → diff → embed → write. Each stage lives in its own
package and talks to the next only through the plain dataclasses below, so a
stage can be tested (and replaced) without the others. Everything here must
stay JSON-serialisable: an :class:`ExtractResult` crosses a process boundary
(the extractor runs in a child process) and locators are stored as JSON.

Locator keys (all optional, only what the format knows):

- ``page`` / ``page_end``      1-based PDF page span
- ``line_start`` / ``line_end`` 1-based line span in a text-like file
- ``sheet`` + ``range``         spreadsheet sheet name and A1 range ("A2:F41")
- ``rows``                      [first, last] 1-based data rows (CSV)
- ``slide``                     1-based slide number
- ``heading``                   list of heading titles from the root down
- ``article`` / ``clause`` / ``point``  legal structure ("12", "2", "a")
- ``para``                      [first, last] paragraph numbers (DOCX)
- ``part``                      email part ("headers", "body", "attachment:<name>")
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# ── Block anchors: how strongly a block starts a new chunk ────────────────
ANCHOR_NONE = 0
# Page breaks, H4–H6, legal clauses, top-level code definitions: a cut is
# preferred here once the chunk is reasonably full.
ANCHOR_SOFT = 1
# H1–H3, legal articles, slides, sheets, email parts: a chunk never spans one.
ANCHOR_HARD = 2

# ── File kinds (what the magic bytes say, not what the extension claims) ──
KIND_TEXT = "text"
KIND_MARKDOWN = "markdown"
KIND_CODE = "code"
KIND_CSV = "csv"
KIND_JSON = "json"
KIND_XML = "xml"
KIND_HTML = "html"
KIND_PDF = "pdf"
KIND_DOCX = "docx"
KIND_XLSX = "xlsx"
KIND_PPTX = "pptx"
KIND_DOC = "doc"
KIND_XLS = "xls"
KIND_PPT = "ppt"
KIND_RTF = "rtf"
KIND_ODT = "odt"
KIND_ODS = "ods"
KIND_ODP = "odp"
KIND_EPUB = "epub"
KIND_EML = "eml"
KIND_MSG = "msg"
KIND_IMAGE = "image"
KIND_AUDIO = "audio"
KIND_VIDEO = "video"
KIND_ARCHIVE = "archive"
KIND_EXECUTABLE = "executable"
KIND_DATABASE = "database"
KIND_FONT = "font"
KIND_BUNDLE = "bundle"        # a macOS .app/.framework directory, indexed as one entry
KIND_ENCRYPTED = "encrypted"  # an encrypted OOXML container
KIND_OTHER = "other"          # binary we do not understand

# Content is never read for these; the index keeps name, path, size, dates.
METADATA_ONLY_KINDS = frozenset({
    KIND_AUDIO, KIND_VIDEO, KIND_ARCHIVE, KIND_EXECUTABLE, KIND_DATABASE,
    KIND_FONT, KIND_BUNDLE, KIND_ENCRYPTED, KIND_OTHER,
})

# Extraction outcome statuses.
EXTRACT_OK = "ok"
EXTRACT_PARTIAL = "partial"            # hit a size/page/row limit; the head is indexed
EXTRACT_METADATA_ONLY = "metadata_only"
EXTRACT_ERROR = "error"

# ── Chunk types ────────────────────────────────────────────────────────────
CTYPE_BODY = "body"
CTYPE_FILE_CARD = "file_card"
CTYPE_FOLDER_CARD = "folder_card"
CTYPE_CAPTION = "caption"
CTYPE_OCR = "ocr"


@dataclass
class Block:
    """One structural unit of extracted text, in reading order."""

    text: str
    anchor: int = ANCHOR_NONE
    # Heading depth 1–6 when ``role == "heading"``; 0 otherwise.
    level: int = 0
    # heading | para | table | code | list | quote | meta
    role: str = "para"
    locator: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Block":
        return cls(
            text=d.get("text") or "",
            anchor=int(d.get("anchor") or 0),
            level=int(d.get("level") or 0),
            role=d.get("role") or "para",
            locator=dict(d.get("locator") or {}),
        )


@dataclass
class ExtractRequest:
    """What the parent asks an extractor worker to do for one file."""

    name: str
    kind: str
    # Exactly one of ``path`` (a local file the worker opens read-only) or
    # ``data`` (bytes already in memory, e.g. a Google Drive export).
    path: str | None = None
    data: bytes | None = None
    # max_pages, max_text_mb, max_rows_per_sheet, max_archive_names
    limits: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractResult:
    """Everything an extractor learned about one file. JSON-serialisable
    (bytes are base64 inside ``ocr_pages``)."""

    status: str
    kind: str
    reason: str | None = None
    mime: str | None = None
    blocks: list[Block] = field(default_factory=list)
    # title, author, last_modified_by, subject, keywords, created, modified
    # (ISO 8601 strings), pages, sheets, slides, words, app, and
    # legal={number, issued, effective, consolidated, repeals} when detected.
    doc_meta: dict[str, Any] = field(default_factory=dict)
    # taken_at (ISO 8601, local camera time), make, model,
    # gps={lat, lon}, width, height, orientation, software
    exif: dict[str, Any] | None = None
    # width, height, format, is_camera_photo — set for images
    image: dict[str, Any] | None = None
    # Scanned PDF pages that need vision OCR: [{page, sha256, png_b64}]
    ocr_pages: list[dict[str, Any]] = field(default_factory=list)
    # Archive member names (never extracted), at most limits.max_archive_names
    listing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["blocks"] = [b.to_dict() for b in self.blocks]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExtractResult":
        return cls(
            status=d.get("status") or EXTRACT_ERROR,
            kind=d.get("kind") or KIND_OTHER,
            reason=d.get("reason"),
            mime=d.get("mime"),
            blocks=[Block.from_dict(b) for b in d.get("blocks") or []],
            doc_meta=dict(d.get("doc_meta") or {}),
            exif=d.get("exif"),
            image=d.get("image"),
            ocr_pages=list(d.get("ocr_pages") or []),
            listing=list(d.get("listing") or []),
        )


@dataclass
class Chunk:
    """One embeddable, citable unit of a file (or folder)."""

    ordinal: int
    ctype: str
    # Breadcrumb ("Luật Đất đai › Chương II › Điều 12"), capped in tokens.
    heading: str
    text: str
    # blake2b-128 hex of the normalized ``heading + "\\n" + text``. Identity
    # for the diff: equal hash ⇒ equal embedding input.
    text_hash: str
    # 0-based occurrence among chunks of this file sharing ``text_hash``.
    occ: int = 0
    section_key: str | None = None
    locator: dict[str, Any] = field(default_factory=dict)
    # Cross-references found in the text: [{raw, article, clause, doc}]
    refs: list[dict[str, Any]] = field(default_factory=list)
    token_est: int = 0
    # Diacritics-folded heading+text for keyword search; None when folding
    # changes nothing beyond case (saves storage for English text).
    folded: str | None = None

    @property
    def embed_text(self) -> str:
        return f"{self.heading}\n{self.text}" if self.heading else self.text


@dataclass
class OldChunk:
    """What the diff needs to know about a chunk already in the index."""

    id: int
    text_hash: str
    ordinal: int
    occ: int = 0
    locator: dict[str, Any] = field(default_factory=dict)
    section_key: str | None = None


@dataclass
class ChunkDiff:
    # (existing chunk id, its new Chunk) — same text, maybe a new position
    keep: list[tuple[int, Chunk]] = field(default_factory=list)
    # chunks whose text is new to this file: need a vector
    add: list[Chunk] = field(default_factory=list)
    # existing chunk ids whose text is gone
    remove: list[int] = field(default_factory=list)


@dataclass
class FsEntry:
    """One file or directory seen by a walk of a local source root."""

    rel_path: str           # POSIX separators, NFC, relative to the root
    abs_path: str
    is_dir: bool
    size: int = 0
    mtime_ns: int = 0
    ino: int = 0
    dev: int = 0
    birthtime: float | None = None
    # A cloud-storage placeholder (OneDrive Files-On-Demand, iCloud "optimize
    # storage", Dropbox online-only): reading it would download it.
    placeholder: bool = False
    # skip | metadata_only | index — from the IgnoreMatcher
    disposition: str = "index"


@dataclass
class ManifestRow:
    """A file as the index last saw it (for scan diffs and move matching)."""

    id: int
    path_hash: str
    rel_path: str
    size: int
    mtime_ns: int
    ino: int = 0
    dev: int = 0
    sha256: str | None = None
    status: str = "indexed"
