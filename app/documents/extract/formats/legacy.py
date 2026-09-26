"""Word 97-2003 ``.doc`` and PowerPoint 97-2003 ``.ppt``, read with olefile.

Vietnamese legal archives are full of ``.doc`` files, and the usual converters
either shell out to LibreOffice/antiword (not installed, and they write temp
files) or skip the format. Both formats are documented ([MS-DOC], [MS-PPT]),
and reading their text needs only the container parser olefile already
provides.

**.doc.** The ``WordDocument`` stream opens with the FIB. Its ``fcClx`` /
``lcbClx`` pair points into the table stream (``1Table`` or ``0Table``, per
``fWhichTblStm``) at the Clx, whose piece table maps character positions to
byte runs in ``WordDocument``: 8-bit cp1252 runs ("compressed") or UTF-16LE.
Character positions run main text, footnotes, headers, comments, endnotes,
text boxes; headers and comments are left out as repetitive or private.
Field codes (``HYPERLINK "..."``, between 0x13 and 0x14) are dropped and field
results kept. Word 6/95 files use a different FIB and are reported as
``legacy_format``.

**.ppt.** The ``PowerPoint Document`` stream is a tree of records. Placeholder
text (titles, bullets) sits in the SlideListWithText container, one run per
SlidePersistAtom, which gives slide order. Free text boxes live in each
Slide container, found through the persist directory that ``Current User``
points to. When that chain is broken, the slide-list text alone is used, and
failing that every Slide container's text in stream order, without slide
numbers.
"""

from __future__ import annotations

import re
import struct
from typing import Any, Iterator

from app.documents.types import ANCHOR_HARD, ANCHOR_SOFT

from ._base import Ctx, EncryptedDocument, LegacyFormatError, iso_datetime

# ── olefile helpers ────────────────────────────────────────────────────────


def _open_ole(ctx: Ctx) -> Any:
    import olefile

    return olefile.OleFileIO(ctx.source())


def _stream(ole: Any, name: str) -> bytes | None:
    if not ole.exists(name):
        return None
    with ole.openstream(name) as fh:
        return fh.read()


def _ole_meta(ctx: Ctx, ole: Any) -> None:
    """Title, author and dates from the OLE summary information streams."""
    try:
        meta = ole.get_metadata()
    except Exception:  # a damaged property set must not cost the text
        return
    codepage = getattr(meta, "codepage", None) or 1252
    encoding = "utf-8" if codepage == 65001 else f"cp{codepage}"

    def text(value: Any) -> str | None:
        if isinstance(value, bytes):
            try:
                value = value.decode(encoding, "replace")
            except LookupError:
                value = value.decode("cp1252", "replace")
        if isinstance(value, str):
            value = value.replace("\x00", "").strip()
            return value or None
        return None

    doc_meta = ctx.result.doc_meta
    for attr, key in (("title", "title"), ("author", "author"), ("last_saved_by", "last_modified_by"),
                      ("subject", "subject"), ("keywords", "keywords"),
                      ("creating_application", "app")):
        value = text(getattr(meta, attr, None))
        if value:
            doc_meta[key] = value
    for attr, key in (("create_time", "created"), ("last_saved_time", "modified")):
        value = getattr(meta, attr, None)
        if value is not None and getattr(value, "year", 0) > 1980:
            doc_meta[key] = iso_datetime(value)
    for attr, key in (("num_pages", "pages"), ("num_words", "words"), ("slides", "slides")):
        value = getattr(meta, attr, None)
        if isinstance(value, int) and value > 0:
            doc_meta[key] = value


# ── .doc ───────────────────────────────────────────────────────────────────

_FIB_IDENT = 0xA5EC
_NFIB_WORD97 = 0x00C0
_FC_CLX_INDEX = 33          # fcClx/lcbClx is pair 33 of FibRgFcLcb97
_F_ENCRYPTED = 0x0100
_F_WHICH_TABLE = 0x0200

_DOC_TRANSLATE = {
    0x07: "\t",   # table cell / row end
    0x0B: "\n",   # manual line break inside a paragraph
    0x1E: "-",    # non-breaking hyphen
    0x1F: None,   # optional hyphen
    0xA0: " ",
    0x01: None, 0x02: None, 0x03: None, 0x04: None, 0x05: None, 0x08: None,
}
_PARA_SPLIT_RE = re.compile(r"[\r\x0c]")


def parse_piece_table(clx: bytes) -> list[tuple[int, int, int, bool]]:
    """``(cp_start, cp_end, byte_offset, compressed)`` for every piece in a
    Clx: any number of Prc entries (skipped), then one Pcdt holding the
    PlcPcd (n+1 character positions, then n 8-byte piece descriptors)."""
    pos = 0
    while pos < len(clx):
        clxt = clx[pos]
        if clxt == 0x01:
            if pos + 3 > len(clx):
                raise LegacyFormatError("truncated Prc")
            (cb,) = struct.unpack_from("<h", clx, pos + 1)
            if cb < 0:
                raise LegacyFormatError("negative Prc size")
            pos += 3 + cb
            continue
        if clxt != 0x02:
            raise LegacyFormatError(f"unexpected Clx entry type {clxt:#x}")
        if pos + 5 > len(clx):
            raise LegacyFormatError("truncated Pcdt")
        (lcb,) = struct.unpack_from("<I", clx, pos + 1)
        plc = clx[pos + 5:pos + 5 + lcb]
        if len(plc) != lcb or lcb < 16 or (lcb - 4) % 12:
            raise LegacyFormatError("malformed PlcPcd")
        n = (lcb - 4) // 12
        cps = struct.unpack_from(f"<{n + 1}I", plc, 0)
        pieces: list[tuple[int, int, int, bool]] = []
        for k in range(n):
            (fc,) = struct.unpack_from("<I", plc, 4 * (n + 1) + 8 * k + 2)
            compressed = bool(fc & 0x40000000)
            fc &= 0x3FFFFFFF
            if cps[k + 1] < cps[k]:
                raise LegacyFormatError("piece table not ascending")
            pieces.append((cps[k], cps[k + 1], fc // 2 if compressed else fc, compressed))
        return pieces
    raise LegacyFormatError("Clx has no piece table")


def piece_text(word: bytes, pieces: list[tuple[int, int, int, bool]], cp_start: int, cp_end: int) -> str:
    """The characters at positions ``[cp_start, cp_end)``."""
    parts: list[str] = []
    for p_start, p_end, offset, compressed in pieces:
        lo, hi = max(p_start, cp_start), min(p_end, cp_end)
        if lo >= hi:
            continue
        if compressed:
            parts.append(word[offset + lo - p_start:offset + hi - p_start].decode("cp1252", "replace"))
        else:
            raw = word[offset + 2 * (lo - p_start):offset + 2 * (hi - p_start)]
            parts.append(raw.decode("utf-16-le", "surrogatepass"))
    return "".join(parts)


def _strip_fields(text: str) -> str:
    """Drop field instructions (0x13..0x14) and keep field results (0x14..0x15),
    nested fields included."""
    if "\x13" not in text:
        return text
    out: list[str] = []
    stack: list[bool] = []  # True while inside a field's instruction part
    in_code = 0
    for ch in text:
        if ch == "\x13":
            stack.append(True)
            in_code += 1
        elif ch == "\x14":
            if stack and stack[-1]:
                stack[-1] = False
                in_code -= 1
        elif ch == "\x15":
            if stack and stack.pop():
                in_code -= 1
        elif not in_code:
            out.append(ch)
    return "".join(out)


def doc_sections(word: bytes, table0: bytes | None, table1: bytes | None) -> list[tuple[str, list[str]]]:
    """``[(section, paragraphs)]`` for a Word 97+ document's streams."""
    if len(word) < 34 or struct.unpack_from("<H", word, 0)[0] != _FIB_IDENT:
        raise LegacyFormatError("WordDocument does not start with a Word 97 FIB")
    nfib, = struct.unpack_from("<H", word, 2)
    flags, = struct.unpack_from("<H", word, 10)
    if flags & _F_ENCRYPTED:
        raise EncryptedDocument("Word document is password protected")
    if nfib < _NFIB_WORD97:
        raise LegacyFormatError(f"Word file version {nfib:#x} predates Word 97")
    csw, = struct.unpack_from("<H", word, 32)
    pos = 34 + 2 * csw
    cslw, = struct.unpack_from("<H", word, pos)
    rglw = pos + 2
    pos = rglw + 4 * cslw
    cb_fclcb, = struct.unpack_from("<H", word, pos)
    fclcb = pos + 2
    if cslw < 11 or cb_fclcb <= _FC_CLX_INDEX:
        raise LegacyFormatError("FIB too short")
    ccp_text, ccp_ftn, ccp_hdd = struct.unpack_from("<iii", word, rglw + 12)
    ccp_atn, ccp_edn, ccp_txbx = struct.unpack_from("<iii", word, rglw + 28)
    fc_clx, lcb_clx = struct.unpack_from("<II", word, fclcb + 8 * _FC_CLX_INDEX)
    table = table1 if flags & _F_WHICH_TABLE else table0
    if table is None:
        raise LegacyFormatError("table stream missing")
    clx = table[fc_clx:fc_clx + lcb_clx]
    if not lcb_clx or len(clx) != lcb_clx:
        raise LegacyFormatError("Clx outside the table stream")
    pieces = parse_piece_table(clx)

    sections: list[tuple[str, list[str]]] = []
    cp = 0
    for name, count, keep in (("main", ccp_text, True), ("footnotes", ccp_ftn, True),
                              ("headers", ccp_hdd, False), ("comments", ccp_atn, False),
                              ("endnotes", ccp_edn, True), ("text boxes", ccp_txbx, True)):
        count = max(count, 0)
        if keep and count:
            text = _strip_fields(piece_text(word, pieces, cp, cp + count)).translate(_DOC_TRANSLATE)
            paragraphs = [p.strip() for p in _PARA_SPLIT_RE.split(text)]
            sections.append((name, [p for p in paragraphs if p]))
        cp += count
    return sections


def extract_doc(ctx: Ctx) -> None:
    ole = _open_ole(ctx)
    try:
        _ole_meta(ctx, ole)
        word = _stream(ole, "WordDocument")
        if word is None:
            raise LegacyFormatError("no WordDocument stream")
        table0, table1 = _stream(ole, "0Table"), _stream(ole, "1Table")
    finally:
        ole.close()
    try:
        sections = doc_sections(word, table0, table1)
    except (struct.error, IndexError, ValueError) as exc:
        raise LegacyFormatError(f"unreadable piece table: {exc}") from exc
    para = 0
    for name, paragraphs in sections:
        if name != "main" and paragraphs:
            ctx.add(name.capitalize(), anchor=ANCHOR_SOFT, level=4, role="heading",
                    locator={"para": [para + 1, para + 1]})
        for text in paragraphs:
            para += 1
            ctx.add(text, locator={"para": [para, para]})


# ── .ppt ───────────────────────────────────────────────────────────────────

_RT_DOCUMENT = 0x03E8
_RT_SLIDE = 0x03EE
_RT_SLIDE_LIST = 0x0FF0
_RT_SLIDE_PERSIST = 0x03F3
_RT_TEXT_CHARS = 0x0FA0
_RT_TEXT_BYTES = 0x0FA8
_RT_USER_EDIT = 0x0FF5
_RT_PERSIST_DIR = 0x1772
_MAX_DEPTH = 24


def _records(data: bytes, start: int, end: int) -> Iterator[tuple[int, int, int, int, int]]:
    """``(type, instance, version, body_start, body_end)`` of the records
    laid end to end in ``data[start:end]``."""
    pos = start
    end = min(end, len(data))
    while pos + 8 <= end:
        ver_inst, rtype, length = struct.unpack_from("<HHI", data, pos)
        body = pos + 8
        stop = min(body + length, end)
        yield rtype, ver_inst >> 4, ver_inst & 0x0F, body, stop
        pos = stop


def _decode_atom(data: bytes, rtype: int, body: int, stop: int) -> str:
    raw = data[body:stop]
    text = raw.decode("utf-16-le", "surrogatepass") if rtype == _RT_TEXT_CHARS else raw.decode("latin-1")
    return text.replace("\r", "\n").replace("\x0b", "\n").strip()


def _texts_in(data: bytes, start: int, end: int, depth: int = 0) -> Iterator[str]:
    """Every text atom inside a container, depth first (drawing text boxes
    are nested several containers deep)."""
    for rtype, _inst, ver, body, stop in _records(data, start, end):
        if ver == 0x0F:
            if depth < _MAX_DEPTH:
                yield from _texts_in(data, body, stop, depth + 1)
        elif rtype in (_RT_TEXT_CHARS, _RT_TEXT_BYTES):
            text = _decode_atom(data, rtype, body, stop)
            if text:
                yield text


def _persist_directory(doc: bytes, current_user: bytes | None) -> tuple[dict[int, int], int | None]:
    """Persist id → stream offset (newest edit wins), and the document
    container's persist id, following the UserEditAtom chain."""
    offsets: dict[int, int] = {}
    doc_ref: int | None = None
    if not current_user or len(current_user) < 20:
        return offsets, None
    (edit,) = struct.unpack_from("<I", current_user, 16)
    seen: set[int] = set()
    while edit and edit not in seen and edit + 8 + 20 <= len(doc):
        seen.add(edit)
        _vi, rtype, _length = struct.unpack_from("<HHI", doc, edit)
        if rtype != _RT_USER_EDIT:
            break
        body = edit + 8
        last_edit, dir_off, doc_persist = struct.unpack_from("<III", doc, body + 8)
        if doc_ref is None:
            doc_ref = doc_persist
        if dir_off + 8 <= len(doc):
            _vi, dtype, dlen = struct.unpack_from("<HHI", doc, dir_off)
            if dtype == _RT_PERSIST_DIR:
                pos, stop = dir_off + 8, min(dir_off + 8 + dlen, len(doc))
                while pos + 4 <= stop:
                    (entry,) = struct.unpack_from("<I", doc, pos)
                    pos += 4
                    first_id, count = entry & 0xFFFFF, entry >> 20
                    for k in range(count):
                        if pos + 4 > stop:
                            break
                        offsets.setdefault(first_id + k, struct.unpack_from("<I", doc, pos)[0])
                        pos += 4
        edit = last_edit
    return offsets, doc_ref


def _slide_list(doc: bytes, start: int, end: int) -> list[tuple[int, list[str]]]:
    """``[(persist_ref, placeholder texts)]`` in slide order, from the
    SlideListWithText container (instance 0 = slides, not masters or notes)."""
    for rtype, inst, _ver, body, stop in _records(doc, start, end):
        if rtype == _RT_SLIDE_LIST and inst == 0:
            slides: list[tuple[int, list[str]]] = []
            for a_type, _i, _v, a_body, a_stop in _records(doc, body, stop):
                if a_type == _RT_SLIDE_PERSIST and a_stop - a_body >= 4:
                    slides.append((struct.unpack_from("<I", doc, a_body)[0], []))
                elif a_type in (_RT_TEXT_CHARS, _RT_TEXT_BYTES) and slides:
                    text = _decode_atom(doc, a_type, a_body, a_stop)
                    if text:
                        slides[-1][1].append(text)
            return slides
    return []


def ppt_slides(doc: bytes, current_user: bytes | None) -> list[tuple[int | None, list[str]]]:
    """``[(slide_number or None, texts)]``: numbered when slide order is known,
    otherwise one entry per Slide container in stream order."""
    try:
        persist, doc_ref = _persist_directory(doc, current_user)
    except struct.error:
        persist, doc_ref = {}, None

    container: tuple[int, int] | None = None
    if doc_ref is not None and doc_ref in persist:
        off = persist[doc_ref]
        if off + 8 <= len(doc):
            vi, rtype, length = struct.unpack_from("<HHI", doc, off)
            if rtype == _RT_DOCUMENT:
                container = (off + 8, off + 8 + length)
    if container is None:
        for rtype, _inst, _ver, body, stop in _records(doc, 0, len(doc)):
            if rtype == _RT_DOCUMENT:
                container = (body, stop)
                break

    slides = _slide_list(doc, *container) if container else []
    if slides:
        out: list[tuple[int | None, list[str]]] = []
        for number, (ref, texts) in enumerate(slides, start=1):
            texts = list(texts)
            off = persist.get(ref)
            if off is not None and off + 8 <= len(doc):
                vi, rtype, length = struct.unpack_from("<HHI", doc, off)
                if rtype == _RT_SLIDE:
                    for text in _texts_in(doc, off + 8, off + 8 + length):
                        if text not in texts:
                            texts.append(text)
            out.append((number, texts))
        return out
    # No slide list: every Slide container's text, in the order stored.
    fallback: list[tuple[int | None, list[str]]] = []
    for rtype, _inst, _ver, body, stop in _records(doc, 0, len(doc)):
        if rtype == _RT_SLIDE:
            texts = list(_texts_in(doc, body, stop))
            if texts:
                fallback.append((None, texts))
    if not fallback:
        raise LegacyFormatError("no slide text records found")
    return fallback


def extract_ppt(ctx: Ctx) -> None:
    ole = _open_ole(ctx)
    try:
        if ole.exists("EncryptedSummary"):
            raise EncryptedDocument("PowerPoint file is password protected")
        _ole_meta(ctx, ole)
        doc = _stream(ole, "PowerPoint Document")
        current_user = _stream(ole, "Current User")
    finally:
        ole.close()
    if doc is None:
        raise LegacyFormatError("no PowerPoint Document stream")
    try:
        slides = ppt_slides(doc, current_user)
    except (struct.error, IndexError, ValueError) as exc:
        raise LegacyFormatError(f"unreadable record stream: {exc}") from exc
    numbered = [n for n, _ in slides if n is not None]
    if numbered:
        ctx.result.doc_meta["slides"] = len(numbered)
    max_slides = ctx.limit("max_pages")
    for index, (number, texts) in enumerate(slides, start=1):
        if index > max_slides:
            ctx.mark_partial()
            break
        locator = {"slide": number} if number is not None else {}
        ctx.add("\n".join(texts), anchor=ANCHOR_HARD, role="para", locator=locator)
