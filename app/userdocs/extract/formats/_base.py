"""Plumbing every extractor shares: the per-file context, the text budget,
charset decoding, and the table grouping used by CSV, spreadsheets and
Markdown tables.

**Budgets are enforced where text is produced.** :meth:`Ctx.add` is the only
way a block enters the result, so ``max_text_mb`` cannot be exceeded by a
format that forgets to check it. When the budget runs out, ``add`` keeps what
fits, marks the result ``partial(too_large)`` and raises :class:`LimitReached`
to unwind the extractor; the dispatcher returns what was read.

**Table rows are grouped by content, not by count.** A block boundary that sits
every 40 rows moves when one row is inserted near the top, which changes every
later block and so every later chunk hash, and the index re-embeds the whole
table. :func:`group_rows` cuts after a row whose hash hits a modulus (with a
minimum and a maximum group size), so boundaries depend only on nearby rows and
line up again right after an edit. This is the same content-defined idea the
chunker uses, applied one level down.
"""

from __future__ import annotations

import codecs
import hashlib
import io
import math
import os
import re
import unicodedata
from datetime import date, datetime, time, timedelta
from typing import Any, BinaryIO, Iterable, Iterator

from app.userdocs.types import (
    ANCHOR_NONE,
    EXTRACT_METADATA_ONLY,
    EXTRACT_OK,
    EXTRACT_PARTIAL,
    Block,
    ExtractRequest,
    ExtractResult,
)

REASON_TOO_LARGE = "too_large"

DEFAULT_LIMITS: dict[str, int] = {
    "max_pages": 2000,
    "max_text_mb": 20,
    "max_rows_per_sheet": 5000,
    "max_archive_names": 500,
    "max_ocr_pages": 50,
    # Rendered scanned pages travel base64-encoded inside one JSON frame; this
    # caps that frame's OCR payload so a 50-page scan cannot need gigabytes.
    "max_ocr_mb": 64,
}


class LimitReached(Exception):
    """The text budget is spent. The result is already marked partial."""


class EncryptedDocument(Exception):
    """The file needs a password we do not have."""


class LegacyFormatError(Exception):
    """A legacy binary format this reader does not understand (Word 6/95, a
    damaged piece table, an unrecognised record stream)."""


class Ctx:
    """One extraction: the request, its limits and the result being built."""

    def __init__(self, req: ExtractRequest, kind: str) -> None:
        self.req = req
        self.kind = kind
        self.limits: dict[str, Any] = dict(DEFAULT_LIMITS)
        for key, value in (req.limits or {}).items():
            if value is not None:
                self.limits[key] = value
        self.result = ExtractResult(status=EXTRACT_OK, kind=kind)
        # max_text_mb bounds both how many bytes a text file is read to and how
        # many characters of blocks a file may produce (a character is at
        # least one byte, so the two agree for text files).
        try:
            text_mb = float(self.limits["max_text_mb"])
        except (TypeError, ValueError):
            text_mb = DEFAULT_LIMITS["max_text_mb"]
        self.max_text_bytes = max(1, int(text_mb * 1024 * 1024))
        self._chars = 0

    # ── input ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self.req.name or os.path.basename(self.req.path or "")

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1].lower()

    def limit(self, key: str) -> int:
        try:
            return int(self.limits.get(key, DEFAULT_LIMITS.get(key, 0)))
        except (TypeError, ValueError):
            return int(DEFAULT_LIMITS.get(key, 0))

    def open(self) -> BinaryIO:
        """A fresh read-only binary stream over the input."""
        if self.req.data is not None:
            return io.BytesIO(self.req.data)
        if not self.req.path:
            raise FileNotFoundError("request has neither path nor data")
        return open(self.req.path, "rb")

    def source(self) -> str | BinaryIO:
        """A path when there is one (parsers stream from disk), else bytes."""
        if self.req.data is not None:
            return io.BytesIO(self.req.data)
        if not self.req.path:
            raise FileNotFoundError("request has neither path nor data")
        return self.req.path

    def read_bytes(self, max_bytes: int | None = None) -> tuple[bytes, bool]:
        """The input's bytes, at most ``max_bytes``; the flag says whether
        more was left unread."""
        if self.req.data is not None:
            data = self.req.data
            if max_bytes is not None and len(data) > max_bytes:
                return data[:max_bytes], True
            return data, False
        with self.open() as fh:
            if max_bytes is None:
                return fh.read(), False
            data = fh.read(max_bytes + 1)
        if len(data) > max_bytes:
            return data[:max_bytes], True
        return data, False

    def head(self, size: int = 8192) -> bytes:
        return self.read_bytes(size)[0]

    # ── output ─────────────────────────────────────────────────────────────

    def add(self, text: str, *, anchor: int = ANCHOR_NONE, level: int = 0,
            role: str = "para", locator: dict[str, Any] | None = None) -> None:
        """Append one block, charging it to the text budget."""
        text = tidy(text)
        if not text:
            return
        room = self.max_text_bytes - self._chars
        if len(text) > room:
            kept = text[:max(room, 0)]
            cut = kept.rfind("\n")
            if cut > len(kept) // 2:
                kept = kept[:cut]
            kept = kept.rstrip()
            if kept:
                self.result.blocks.append(
                    Block(text=kept, anchor=anchor, level=level, role=role, locator=dict(locator or {})))
            self._chars = self.max_text_bytes
            self.mark_partial()
            raise LimitReached()
        self.result.blocks.append(
            Block(text=text, anchor=anchor, level=level, role=role, locator=dict(locator or {})))
        self._chars += len(text)

    @property
    def budget_left(self) -> int:
        return self.max_text_bytes - self._chars

    def mark_partial(self, reason: str = REASON_TOO_LARGE) -> None:
        if self.result.status == EXTRACT_OK:
            self.result.status = EXTRACT_PARTIAL
            self.result.reason = reason

    def metadata_only(self, reason: str) -> None:
        self.result.status = EXTRACT_METADATA_ONLY
        self.result.reason = reason


# ── Text helpers ───────────────────────────────────────────────────────────

_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),  # before UTF-16 LE: it starts with the same FF FE
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def decode_text(data: bytes) -> tuple[str, str]:
    """Decode bytes of unknown charset: BOM, then strict UTF-8 (by far the
    common case, and fast), then charset-normalizer, then UTF-8 with
    replacement characters."""
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return data.decode(encoding, "replace"), encoding
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as exc:
        # A read cut at the byte budget can split the last character; that is
        # still UTF-8 and must not send the file to the charset guesser.
        if exc.reason == "unexpected end of data" and exc.start >= len(data) - 3:
            try:
                return data[:exc.start].decode("utf-8"), "utf-8"
            except UnicodeDecodeError:
                pass
    try:
        import charset_normalizer
        best = charset_normalizer.from_bytes(data).best()
    except ImportError:
        best = None
    if best is not None:
        return str(best), best.encoding
    return data.decode("utf-8", "replace"), "utf-8"


def read_text(ctx: Ctx) -> str:
    """The input decoded as text with ``\\n`` line ends, capped at the text
    budget (a larger file is read up to the cap and marked partial)."""
    data, truncated = ctx.read_bytes(ctx.max_text_bytes)
    if truncated:
        ctx.mark_partial()
    text, encoding = decode_text(data)
    ctx.result.doc_meta.setdefault("encoding", encoding)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def tidy(text: str) -> str:
    """Trailing whitespace off every line and blank lines off both ends.
    Leading indentation stays: it is meaning in code and Markdown."""
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    start, end = 0, len(lines)
    while start < end and not lines[start]:
        start += 1
    while end > start and not lines[end - 1]:
        end -= 1
    return "\n".join(lines[start:end])


# C0 controls other than tab/newline: NUL breaks Postgres text columns and
# JSON consumers, and the rest is layout noise from binary formats.
_CONTROL_TABLE = {c: None for c in range(32) if c not in (9, 10, 13)}
_CONTROL_TABLE[0x0B] = "\n"
_CONTROL_TABLE[0x0C] = "\n"
_CONTROL_TABLE[0x7F] = None


def clean_str(value: str) -> str:
    """Drop control characters, repair lone UTF-16 surrogates (RTF ``\\u``
    escapes and broken PDF text produce them, and either would make the result
    impossible to encode as UTF-8 or store), and compose to NFC.

    NFC matters for Vietnamese: code page 1258 (and some PDFs and copy-pasted
    text) spell a toned vowel as a base letter plus a combining mark, while
    people type the precomposed letter, so undecomposed text would not match
    their searches."""
    value = value.translate(_CONTROL_TABLE)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        value = value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    if not unicodedata.is_normalized("NFC", value):
        value = unicodedata.normalize("NFC", value)
    return value


def json_safe(value: Any) -> Any:
    """``value`` reduced to JSON types with clean strings (for doc_meta/exif)."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return clean_str(value).strip()
    if isinstance(value, bytes):
        return clean_str(value.decode("utf-8", "replace")).strip()
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(v) for v in value]
    return clean_str(str(value)).strip()


def iso_datetime(value: Any) -> str | None:
    """An ISO 8601 string for a datetime, or for a string that parses as one."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return text


# ── Tables ─────────────────────────────────────────────────────────────────

_ROW_MIN = 8          # never cut a group before this many rows...
_ROW_MAX = 40         # ...and always cut at this many
_ROW_CHARS = 1200     # or once the group's text would overflow a chunk
_ROW_MODULUS = 16     # expected extra rows past the minimum before a hash cut

_WS_RE = re.compile(r"\s+")


def cell_text(value: Any) -> str:
    """One spreadsheet cell as display text on a single line."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return f"{value:.10g}"
    if isinstance(value, datetime):
        if value.time() == time(0) and value.tzinfo is None:
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    return _WS_RE.sub(" ", str(value)).strip()


def trim_row(cells: list[str]) -> list[str]:
    end = len(cells)
    while end and not cells[end - 1]:
        end -= 1
    return cells[:end]


def _row_hash(cells: list[str]) -> int:
    raw = "\x1f".join(cells).encode("utf-8", "surrogatepass")
    return int.from_bytes(hashlib.blake2b(raw, digest_size=4).digest(), "big")


def group_rows(rows: Iterable[tuple[int, list[str]]]) -> Iterator[list[tuple[int, list[str]]]]:
    """Split ``(row_number, cells)`` into groups of at most 40 rows whose
    boundaries depend on row content (see the module docstring)."""
    group: list[tuple[int, list[str]]] = []
    chars = 0
    for row in rows:
        group.append(row)
        chars += sum(len(c) for c in row[1]) + 3 * len(row[1]) + 2
        if (len(group) >= _ROW_MAX or chars >= _ROW_CHARS
                or (len(group) >= _ROW_MIN and _row_hash(row[1]) % _ROW_MODULUS == 0)):
            yield group
            group, chars = [], 0
    if group:
        yield group


def render_table(header: list[str] | None, rows: list[list[str]]) -> str:
    """Rows as a Markdown table, headed by ``header`` so every group reads on
    its own: a chunk holding rows 200-240 still says what each column is."""
    def esc(cell: str) -> str:
        return cell.replace("|", "\\|")

    width = max([len(header or [])] + [len(r) for r in rows] + [1])
    lines: list[str] = []
    if header:
        padded = list(header) + [""] * (width - len(header))
        lines.append("| " + " | ".join(esc(c) for c in padded) + " |")
        lines.append("|" + "---|" * width)
    for row in rows:
        lines.append("| " + " | ".join(esc(c) for c in row) + " |")
    return "\n".join(lines)
