"""Programmatic fixtures for the extractor tests.

Every file the extraction tests read is built here from scratch, so no binary
fixture has to live in the repo: a minimal PDF writer (text at chosen sizes,
full-page images for "scanned" pages), hand-rolled DOCX/ODF/EPUB packages, a
Compound File Binary writer for OLE containers, and synthetic Word 97 and
PowerPoint 97 streams.
"""

from __future__ import annotations

import io
import struct
import zipfile

# ── PDF ────────────────────────────────────────────────────────────────────


def _pdf_escape(text: str) -> bytes:
    raw = text.encode("cp1252")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def make_pdf(pages: list[list[tuple[float, float, float, str]]], *, scanned: frozenset[int] = frozenset(),
             info: dict[str, str] | None = None) -> bytes:
    """A PDF whose page ``i`` shows ``(size, x, y, text)`` lines in Helvetica.
    Pages whose 1-based number is in ``scanned`` are one full-page image."""
    image = bytes((x * 37 + y * 11) % 256 for y in range(16) for x in range(16))
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # pages tree, filled below
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /XObject /Subtype /Image /Width 16 /Height 16 /ColorSpace /DeviceGray "
        b"/BitsPerComponent 8 /Length " + str(len(image)).encode() + b" >>\nstream\n" + image + b"\nendstream",
    ]
    kids = []
    for number, lines in enumerate(pages, start=1):
        if number in scanned:
            content = b"q 612 0 0 792 0 0 cm /Im1 Do Q\n"
        else:
            content = b"".join(
                b"BT /F1 %s Tf %s %s Td (" % (str(size).encode(), str(x).encode(), str(y).encode())
                + _pdf_escape(text) + b") Tj ET\n"
                for size, x, y, text in lines)
        objects.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream")
        content_id = len(objects)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> "
            b"/XObject << /Im1 4 0 R >> >> /Contents " + str(content_id).encode() + b" 0 R >>")
        kids.append(len(objects))
    objects[1] = (b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids)
                  + b"] /Count " + str(len(kids)).encode() + b" >>")
    info_id = None
    if info:
        objects.append(b"<< " + b" ".join(b"/" + k.encode() + b" (" + _pdf_escape(v) + b")"
                                           for k, v in info.items()) + b" >>")
        info_id = len(objects)
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    trailer = b"<< /Size %d /Root 1 0 R" % (len(objects) + 1)
    if info_id:
        trailer += b" /Info %d 0 R" % info_id
    out += b"trailer\n" + trailer + b" >>\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


def encrypt_pdf(data: bytes, user_password: str, owner_password: str = "owner") -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    writer.encrypt(user_password, owner_password, algorithm="RC4-128")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ── OOXML / ODF / EPUB packages ────────────────────────────────────────────

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_docx(paragraphs: list[tuple[str | None, str]], *, core: bool = True) -> bytes:
    """``paragraphs`` are ``(style or None, text)``; styles Heading1/Heading2."""
    def para(style: str | None, text: str) -> str:
        props = '<w:pPr><w:pStyle w:val="%s"/></w:pPr>' % style if style else ""
        return f'<w:p>{props}<w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'

    body = "".join(para(style, text) for style, text in paragraphs)
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
            'officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-'
            'officedocument.wordprocessingml.styles+xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-'
            'package.core-properties+xml"/>'
            '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-'
            'officedocument.extended-properties+xml"/>'
            '</Types>'),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="word/document.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/'
            'metadata/core-properties" Target="docProps/core.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/extended-properties" Target="docProps/app.xml"/>'
            '</Relationships>'),
        "word/_rels/document.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/styles" Target="styles.xml"/>'
            '</Relationships>'),
        "word/styles.xml": (
            f'<?xml version="1.0" encoding="UTF-8"?><w:styles xmlns:w="{_W}">'
            '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
            '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>'
            '</w:styles>'),
        "word/document.xml": (
            f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{_W}"><w:body>{body}</w:body></w:document>'),
    }
    if core:
        files["docProps/core.xml"] = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            '<dc:title>Quarterly Report</dc:title><dc:creator>Alice Nguyen</dc:creator>'
            '<cp:lastModifiedBy>Bob Tran</cp:lastModifiedBy><cp:keywords>sales, q3</cp:keywords>'
            '<dcterms:created xsi:type="dcterms:W3CDTF">2024-01-02T03:04:05Z</dcterms:created>'
            '<dcterms:modified xsi:type="dcterms:W3CDTF">2024-02-03T04:05:06Z</dcterms:modified>'
            '</cp:coreProperties>')
        files["docProps/app.xml"] = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
            '<Pages>3</Pages><Words>120</Words><Application>Microsoft Office Word</Application></Properties>')
    return zip_bytes(files)


def zip_bytes(files: dict[str, str | bytes], *, stored_first: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if stored_first:
            zf.writestr(zipfile.ZipInfo(stored_first), files[stored_first], compress_type=zipfile.ZIP_STORED)
        for name, content in files.items():
            if name != stored_first:
                zf.writestr(name, content)
    return buf.getvalue()


_ODF_NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" '
    'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/"'
)

ODF_META = (
    f'<?xml version="1.0" encoding="UTF-8"?><office:document-meta {_ODF_NS}><office:meta>'
    '<dc:title>Biên bản họp</dc:title><meta:initial-creator>Lan</meta:initial-creator>'
    '<dc:creator>Minh</dc:creator><meta:creation-date>2023-05-01T10:00:00</meta:creation-date>'
    '<dc:date>2023-06-01T11:30:00</dc:date><meta:keyword>họp</meta:keyword>'
    '<meta:document-statistic meta:page-count="2" meta:word-count="42"/>'
    '</office:meta></office:document-meta>')


def make_odf(mimetype: str, body: str) -> bytes:
    content = (f'<?xml version="1.0" encoding="UTF-8"?><office:document-content {_ODF_NS}>'
               f'<office:body>{body}</office:body></office:document-content>')
    return zip_bytes({"mimetype": mimetype, "content.xml": content, "meta.xml": ODF_META},
                     stored_first="mimetype")


def make_epub() -> bytes:
    container = ('<?xml version="1.0"?><container version="1.0" '
                 'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                 '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                 '</rootfiles></container>')
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>The Robot Book</dc:title>'
           '<dc:creator>Ada</dc:creator></metadata>'
           '<manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>'
           '<spine><itemref idref="c1"/></spine></package>')
    chapter = ('<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>c</title></head>'
               '<body><h1>Motion tracking</h1><p>The robot follows a red ball.</p></body></html>')
    return zip_bytes({"mimetype": "application/epub+zip", "META-INF/container.xml": container,
                      "OEBPS/content.opf": opf, "OEBPS/ch1.xhtml": chapter}, stored_first="mimetype")


# ── OLE compound files ─────────────────────────────────────────────────────

_ENDOFCHAIN = 0xFFFFFFFE
_FREESECT = 0xFFFFFFFF
_FATSECT = 0xFFFFFFFD
_NOSTREAM = 0xFFFFFFFF


def make_cfb(streams: dict[str, bytes], *, clsid: bytes = b"\x00" * 16) -> bytes:
    """A version-3 compound file holding ``streams`` at the root. Each stream
    is padded to 4096 bytes so it lives in regular sectors (no mini stream)."""
    sector = 512
    names = list(streams)
    datas = [data + b"\x00" * max(0, 4096 - len(data)) for data in streams.values()]
    dir_sectors = ((1 + len(names)) * 128 + sector - 1) // sector
    fat = [_FATSECT]
    for i in range(dir_sectors):
        fat.append(2 + i if i < dir_sectors - 1 else _ENDOFCHAIN)
    starts = []
    next_sector = 1 + dir_sectors
    for data in datas:
        count = (len(data) + sector - 1) // sector
        starts.append(next_sector)
        for k in range(count):
            fat.append(next_sector + k + 1 if k < count - 1 else _ENDOFCHAIN)
        next_sector += count
    assert len(fat) <= 128, "fixture too large for one FAT sector"
    fat += [_FREESECT] * (128 - len(fat))

    header = bytearray(512)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<HHHHH", header, 24, 0x003E, 0x0003, 0xFFFE, 9, 6)
    struct.pack_into("<IIIIIIIII", header, 40, 0, 1, 1, 0, 4096, _ENDOFCHAIN, 0, _ENDOFCHAIN, 0)
    struct.pack_into("<109I", header, 76, 0, *([_FREESECT] * 108))

    def entry(name: str, kind: int, child: int, right: int, start: int, size: int, cls: bytes) -> bytes:
        e = bytearray(128)
        raw = name.encode("utf-16-le") + b"\x00\x00"
        e[:len(raw)] = raw
        struct.pack_into("<HBB", e, 64, len(raw), kind, 1)
        struct.pack_into("<III", e, 68, _NOSTREAM, right, child)
        e[80:96] = cls
        struct.pack_into("<IQ", e, 116, start, size)
        return bytes(e)

    entries = [entry("Root Entry", 5, 1 if names else _NOSTREAM, _NOSTREAM, _ENDOFCHAIN, 0, clsid)]
    for i, name in enumerate(names):
        right = i + 2 if i + 1 < len(names) else _NOSTREAM
        entries.append(entry(name, 2, _NOSTREAM, right, starts[i], len(datas[i]), b"\x00" * 16))
    directory = b"".join(entries)
    empty = bytearray(128)
    struct.pack_into("<III", empty, 68, _NOSTREAM, _NOSTREAM, _NOSTREAM)
    while len(directory) < dir_sectors * sector:
        directory += bytes(empty)
    body = b"".join(d + b"\x00" * (-len(d) % sector) for d in datas)
    return bytes(header) + struct.pack("<128I", *fat) + directory + body


def make_word_streams(pieces: list[tuple[str, bool]], *, footnote_chars: int = 0, which_table: int = 1,
                      encrypted: bool = False, nfib: int = 0x00C1) -> dict[str, bytes]:
    """``WordDocument`` plus table stream for a Word 97 file whose text is
    ``pieces`` (``(text, compressed)``), the last ``footnote_chars`` of it
    counted as footnotes."""
    word = bytearray(8192)
    struct.pack_into("<HH", word, 0, 0xA5EC, nfib)
    struct.pack_into("<H", word, 10, (0x0200 if which_table else 0) | (0x0100 if encrypted else 0))
    struct.pack_into("<H", word, 32, 14)          # csw
    struct.pack_into("<H", word, 62, 22)          # cslw
    rglw = 64
    total = sum(len(text) for text, _ in pieces)
    struct.pack_into("<ii", word, rglw + 12, total - footnote_chars, footnote_chars)
    struct.pack_into("<H", word, rglw + 88, 93)   # cbRgFcLcb
    fclcb = rglw + 90
    pos = 0x800
    cps, fcs = [0], []
    for text, compressed in pieces:
        if compressed:
            raw, fc = text.encode("cp1252"), (pos * 2) | 0x40000000
        else:
            raw, fc = text.encode("utf-16-le"), pos
        word[pos:pos + len(raw)] = raw
        fcs.append(fc)
        pos += len(raw) + (len(raw) % 2)
        cps.append(cps[-1] + len(text))
    plc = struct.pack(f"<{len(cps)}I", *cps) + b"".join(struct.pack("<HIH", 0, fc, 0) for fc in fcs)
    clx = b"\x01" + struct.pack("<h", 2) + b"\x00\x00" + b"\x02" + struct.pack("<I", len(plc)) + plc
    table = bytearray(4096)
    table[0x100:0x100 + len(clx)] = clx
    struct.pack_into("<II", word, fclcb + 33 * 8, 0x100, len(clx))
    return {"WordDocument": bytes(word), ("1Table" if which_table else "0Table"): bytes(table)}


def _rec(rtype: int, payload: bytes = b"", *, ver: int = 0, inst: int = 0) -> bytes:
    return struct.pack("<HHI", (inst << 4) | ver, rtype, len(payload)) + payload


def _container(rtype: int, children: list[bytes], *, inst: int = 0) -> bytes:
    return _rec(rtype, b"".join(children), ver=0x0F, inst=inst)


def make_ppt_streams(slides: list[tuple[list[str], list[str]]], *, with_current_user: bool = True) -> dict[str, bytes]:
    """``PowerPoint Document`` (+ ``Current User``) for slides given as
    ``(placeholder texts, text box texts)``."""
    slwt: list[bytes] = []
    for i, (placeholders, _boxes) in enumerate(slides):
        slwt.append(_rec(0x03F3, struct.pack("<IIiII", i + 1, 0, len(placeholders), 256 + i, 0)))
        for text in placeholders:
            slwt.append(_rec(0x0F9F, struct.pack("<I", 0)))
            slwt.append(_rec(0x0FA0, text.encode("utf-16-le")))
    stream = bytearray(_container(0x03E8, [_container(0x0FF0, slwt, inst=0)]))
    slide_offsets = []
    for _placeholders, boxes in slides:
        slide_offsets.append(len(stream))
        drawing = _container(0x040C, [_container(0xF00D, [_rec(0x0FA8, b.encode("latin-1")) for b in boxes])])
        stream += _container(0x03EE, [drawing])
    persist_dir = len(stream)
    entries = (struct.pack("<I", (len(slides) << 20) | 1)
               + b"".join(struct.pack("<I", off) for off in slide_offsets)
               + struct.pack("<II", (1 << 20) | 100, 0))
    stream += _rec(0x1772, entries)
    edit = len(stream)
    stream += _rec(0x0FF5, struct.pack("<IHBBIIII", 0, 0, 0, 3, 0, persist_dir, 100, 0) + b"\x00" * 4)
    out = {"PowerPoint Document": bytes(stream)}
    if with_current_user:
        out["Current User"] = _rec(0x0FF6, struct.pack("<III", 20, 0xE391C05F, edit) + b"\x00" * 8)
    return out
