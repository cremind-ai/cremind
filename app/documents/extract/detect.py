"""Name a file's kind from its bytes, not its extension.

Users rename files, cloud exports come without extensions, and a ``.pdf`` saved
from a broken web page is sometimes HTML. The kind decides which extractor
runs, and the wrong extractor turns a readable file into ``corrupt``. So magic
bytes always decide. The extension is consulted only when the bytes cannot
settle the question: a ZIP whose directory is not in the head, an OLE file
whose stream names are not in the first directory sector, or a text file,
where the bytes say "text" but not which kind.

:func:`sniff` works on a head and tail alone. :func:`detect_file` reads a
file's head and tail, then looks inside ZIP and OLE containers, whose kind is
set by what they hold. It reads the file, so the caller must never hand it a
cloud placeholder: reading one would download it. :func:`detect_bytes` does the
same for a file already in memory (a Drive download has no path), so a ZIP
whose directory lies past the first 8 KiB is still read to the end.
"""

from __future__ import annotations

import codecs
import io
import os
import re
import struct
import zlib
from typing import BinaryIO, Callable

from app.documents.types import (
    KIND_ARCHIVE,
    KIND_AUDIO,
    KIND_BUNDLE,
    KIND_CODE,
    KIND_CSV,
    KIND_DATABASE,
    KIND_DOC,
    KIND_DOCX,
    KIND_EML,
    KIND_ENCRYPTED,
    KIND_EPUB,
    KIND_EXECUTABLE,
    KIND_FONT,
    KIND_HTML,
    KIND_IMAGE,
    KIND_JSON,
    KIND_MARKDOWN,
    KIND_MSG,
    KIND_ODP,
    KIND_ODS,
    KIND_ODT,
    KIND_OTHER,
    KIND_PDF,
    KIND_PPT,
    KIND_PPTX,
    KIND_RTF,
    KIND_TEXT,
    KIND_VIDEO,
    KIND_XLS,
    KIND_XLSX,
    KIND_XML,
)

__all__ = ["HEAD_BYTES", "TAIL_BYTES", "sniff", "detect_file", "detect_bytes", "is_executable_ext"]

HEAD_BYTES = 8192
TAIL_BYTES = 512

Kind = tuple[str, "str | None"]

# ── Extension tables (tie-breakers only) ──────────────────────────────────

_EXECUTABLE_EXTS = frozenset({
    ".exe", ".dll", ".so", ".dylib", ".msi", ".msp", ".msix", ".appx", ".app",
    ".dmg", ".pkg", ".mpkg", ".deb", ".rpm", ".appimage", ".snap", ".flatpak",
    ".bin", ".com", ".sys", ".drv", ".ocx", ".scr", ".cpl", ".efi", ".run",
    ".elf", ".out", ".o", ".a", ".lib", ".ko", ".pyd", ".node",
})

_BUNDLE_EXTS = frozenset({".app", ".bundle", ".framework"})

_MARKDOWN_EXTS = frozenset({".md", ".markdown", ".mdown", ".mkd", ".mkdn", ".mdx", ".rmd", ".qmd"})

_CODE_EXTS = frozenset({
    ".py", ".pyw", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts",
    ".cts", ".vue", ".svelte", ".astro", ".java", ".kt", ".kts", ".scala", ".sc",
    ".groovy", ".gradle", ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".cxx", ".c++",
    ".hpp", ".hh", ".hxx", ".ino", ".cs", ".fs", ".fsx", ".vb", ".swift", ".m",
    ".mm", ".rb", ".php", ".pl", ".pm", ".lua", ".r", ".jl", ".dart", ".ex",
    ".exs", ".erl", ".hrl", ".hs", ".elm", ".clj", ".cljs", ".cljc", ".ml",
    ".mli", ".nim", ".zig", ".sol", ".sql", ".sh", ".bash", ".zsh", ".fish",
    ".ksh", ".ps1", ".psm1", ".bat", ".cmd", ".asm", ".s", ".css", ".scss",
    ".sass", ".less", ".tf", ".hcl", ".proto", ".graphql", ".gql", ".cmake",
    ".pas", ".d", ".f90", ".f95", ".lisp", ".el", ".scm", ".rkt", ".tcl",
    ".coffee", ".vhd", ".vhdl", ".sv",
})

_CSV_EXTS = frozenset({".csv", ".tsv", ".tab"})
_JSON_EXTS = frozenset({".json", ".jsonl", ".ndjson", ".geojson", ".jsonc", ".json5", ".ipynb", ".har"})
_XML_EXTS = frozenset({
    ".xml", ".svg", ".xsd", ".xsl", ".xslt", ".rss", ".atom", ".kml", ".gpx",
    ".xaml", ".plist", ".resx", ".csproj", ".vcxproj", ".fsproj", ".props",
    ".targets", ".nuspec", ".wsdl", ".xlf", ".xliff", ".opml",
})
_HTML_EXTS = frozenset({".html", ".htm", ".xhtml", ".shtml"})
_EML_EXTS = frozenset({".eml", ".mht", ".mhtml"})

_CODE_MIME = {
    ".py": "text/x-python", ".pyw": "text/x-python", ".pyi": "text/x-python",
    ".js": "text/javascript", ".mjs": "text/javascript", ".cjs": "text/javascript",
    ".ts": "text/x-typescript", ".tsx": "text/x-typescript", ".java": "text/x-java",
    ".c": "text/x-c", ".h": "text/x-c", ".cpp": "text/x-c++", ".hpp": "text/x-c++",
    ".cs": "text/x-csharp", ".go": "text/x-go", ".rs": "text/x-rust",
    ".rb": "text/x-ruby", ".php": "text/x-php", ".sh": "text/x-shellscript",
    ".css": "text/css", ".sql": "application/sql",
}

_TEXT_MIME = {
    KIND_MARKDOWN: "text/markdown", KIND_CSV: "text/csv", KIND_JSON: "application/json",
    KIND_XML: "application/xml", KIND_HTML: "text/html", KIND_EML: "message/rfc822",
    KIND_TEXT: "text/plain", KIND_RTF: "application/rtf",
}

_KIND_MIME = {
    KIND_PDF: "application/pdf",
    KIND_DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    KIND_XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    KIND_PPTX: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    KIND_ODT: "application/vnd.oasis.opendocument.text",
    KIND_ODS: "application/vnd.oasis.opendocument.spreadsheet",
    KIND_ODP: "application/vnd.oasis.opendocument.presentation",
    KIND_EPUB: "application/epub+zip",
    KIND_DOC: "application/msword",
    KIND_XLS: "application/vnd.ms-excel",
    KIND_PPT: "application/vnd.ms-powerpoint",
    KIND_MSG: "application/vnd.ms-outlook",
    KIND_ENCRYPTED: "application/x-cfb",
}

# Where the extension alone names a container kind (used only when the bytes
# are a ZIP or OLE file whose contents we could not read).
_ZIP_EXT_KINDS = {
    ".docx": KIND_DOCX, ".docm": KIND_DOCX, ".dotx": KIND_DOCX, ".dotm": KIND_DOCX,
    ".xlsx": KIND_XLSX, ".xlsm": KIND_XLSX, ".xltx": KIND_XLSX, ".xltm": KIND_XLSX,
    ".pptx": KIND_PPTX, ".pptm": KIND_PPTX, ".ppsx": KIND_PPTX, ".potx": KIND_PPTX,
    ".odt": KIND_ODT, ".ott": KIND_ODT, ".ods": KIND_ODS, ".ots": KIND_ODS,
    ".odp": KIND_ODP, ".otp": KIND_ODP, ".epub": KIND_EPUB,
}
_OLE_EXT_KINDS = {
    ".doc": KIND_DOC, ".dot": KIND_DOC, ".xls": KIND_XLS, ".xlt": KIND_XLS,
    ".ppt": KIND_PPT, ".pps": KIND_PPT, ".pot": KIND_PPT, ".msg": KIND_MSG,
    # An OLE file named like an OOXML one is a password-protected OOXML file.
    ".docx": KIND_ENCRYPTED, ".xlsx": KIND_ENCRYPTED, ".pptx": KIND_ENCRYPTED,
}

# ── Container internals ────────────────────────────────────────────────────

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

# Root-storage CLSIDs of Windows Installer database, patch and transform files.
_MSI_CLSIDS = frozenset({
    "000C1084-0000-0000-C000-000000000046",
    "000C1086-0000-0000-C000-000000000046",
    "000C1082-0000-0000-C000-000000000046",
})

# The main part's content type decides an OOXML package's kind. Substring
# matching on every ContentType would be wrong: a .docx that embeds a chart
# declares a spreadsheetml type too.
_OOXML_MAIN_TYPES = (
    ("wordprocessingml.document.main", KIND_DOCX),
    ("wordprocessingml.template.main", KIND_DOCX),
    ("ms-word.document.macroenabled.main", KIND_DOCX),
    ("ms-word.template.macroenabledtemplate.main", KIND_DOCX),
    ("spreadsheetml.sheet.main", KIND_XLSX),
    ("spreadsheetml.template.main", KIND_XLSX),
    ("ms-excel.sheet.macroenabled.main", KIND_XLSX),
    ("ms-excel.template.macroenabled.main", KIND_XLSX),
    ("presentationml.presentation.main", KIND_PPTX),
    ("presentationml.slideshow.main", KIND_PPTX),
    ("presentationml.template.main", KIND_PPTX),
    ("ms-powerpoint.presentation.macroenabled.main", KIND_PPTX),
    ("ms-powerpoint.slideshow.macroenabled.main", KIND_PPTX),
    ("ms-powerpoint.template.macroenabled.main", KIND_PPTX),
)
_CONTENT_TYPE_RE = re.compile(rb'ContentType\s*=\s*"([^"]+)"', re.IGNORECASE)

_ODF_MIMES = {
    "application/vnd.oasis.opendocument.text": KIND_ODT,
    "application/vnd.oasis.opendocument.text-template": KIND_ODT,
    "application/vnd.oasis.opendocument.spreadsheet": KIND_ODS,
    "application/vnd.oasis.opendocument.spreadsheet-template": KIND_ODS,
    "application/vnd.oasis.opendocument.presentation": KIND_ODP,
    "application/vnd.oasis.opendocument.presentation-template": KIND_ODP,
    "application/epub+zip": KIND_EPUB,
}

# ISO base media (ftyp) brands.
_HEIF_BRANDS = {
    b"heic": "image/heic", b"heix": "image/heic", b"heim": "image/heic",
    b"heis": "image/heic", b"hevc": "image/heic-sequence", b"hevx": "image/heic-sequence",
    b"avif": "image/avif", b"avis": "image/avif",
    b"mif1": "image/heif", b"msf1": "image/heif-sequence",
}
_AUDIO_BRANDS = {b"M4A ", b"M4B ", b"M4P ", b"F4A ", b"F4B "}

_MACHO_MAGICS = (b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe")


def _norm_ext(ext: str | None) -> str:
    if not ext:
        return ""
    ext = ext.strip().lower()
    return ext if ext.startswith(".") else "." + ext


def is_executable_ext(ext: str | None) -> bool:
    """Is this an extension programs and installers use? Magic bytes win
    over this when a file has them; it only classifies opaque binaries."""
    return _norm_ext(ext) in _EXECUTABLE_EXTS


def _u16(b: bytes, off: int, big: bool = False) -> int:
    return struct.unpack_from(">H" if big else "<H", b, off)[0]


def _u32(b: bytes, off: int, big: bool = False) -> int:
    return struct.unpack_from(">I" if big else "<I", b, off)[0]


# ── Probes, most specific first ────────────────────────────────────────────


def _probe_pdf(head: bytes, tail: bytes, ext: str) -> Kind | None:
    # Acrobat accepts the header anywhere in the first 1024 bytes, but a text
    # file that merely mentions "%PDF-" must not become a PDF, so a late header
    # counts only when the name agrees.
    pos = head.find(b"%PDF-", 0, 1024)
    if pos == 0 or (pos > 0 and ext == ".pdf"):
        return KIND_PDF, "application/pdf"
    return None


def _zip_local_members(head: bytes) -> list[tuple[str, int, bytes]]:
    """(name, method, compressed bytes present in the head) for the local file
    headers that fit in ``head``. ODF and EPUB store their ``mimetype`` member
    first and uncompressed, so the head alone often settles those."""
    out: list[tuple[str, int, bytes]] = []
    pos = 0
    while pos + 30 <= len(head) and head[pos:pos + 4] == b"PK\x03\x04" and len(out) < 64:
        flags, method = struct.unpack_from("<HH", head, pos + 6)
        csize = _u32(head, pos + 18)
        nlen, xlen = struct.unpack_from("<HH", head, pos + 26)
        raw_name = head[pos + 30:pos + 30 + nlen]
        name = raw_name.decode("utf-8" if flags & 0x800 else "cp437", "replace")
        start = pos + 30 + nlen + xlen
        out.append((name, method, head[start:start + csize]))
        if flags & 0x08:
            break  # sizes live in a trailing descriptor; the next header is unfindable
        pos = start + csize
    return out


def _classify_zip(
    names: list[str], read: Callable[[str], bytes | None], ext: str, complete: bool,
) -> Kind:
    """Decide a ZIP container's kind from its member names. ``complete`` says
    whether ``names`` is the whole central directory or just what the head
    held; only an incomplete listing may fall back to the extension."""
    name_set = set(names)
    if "mimetype" in name_set:
        raw = read("mimetype") or b""
        mt = raw.strip().decode("ascii", "replace")
        if mt in _ODF_MIMES:
            return _ODF_MIMES[mt], mt
        if mt.startswith("application/vnd.oasis.opendocument."):
            return KIND_OTHER, mt  # drawings, formulas, charts: nothing to read
    if "[Content_Types].xml" in name_set:
        raw = read("[Content_Types].xml") or b""
        for ctype in _CONTENT_TYPE_RE.findall(raw):
            low = ctype.decode("ascii", "replace").lower()
            for marker, kind in _OOXML_MAIN_TYPES:
                if marker in low:
                    return kind, _KIND_MIME[kind]
    if "word/document.xml" in name_set:
        return KIND_DOCX, _KIND_MIME[KIND_DOCX]
    if "xl/workbook.xml" in name_set:
        return KIND_XLSX, _KIND_MIME[KIND_XLSX]
    if "ppt/presentation.xml" in name_set:
        return KIND_PPTX, _KIND_MIME[KIND_PPTX]
    if "AndroidManifest.xml" in name_set:
        return KIND_ARCHIVE, "application/vnd.android.package-archive"
    if "META-INF/MANIFEST.MF" in name_set or any(n.endswith(".class") for n in names[:200]):
        return KIND_ARCHIVE, "application/java-archive"
    if complete and "[Content_Types].xml" in name_set:
        # An OPC package we do not read (xlsb, vsdx, xps): not a user archive.
        return KIND_OTHER, "application/zip"
    if not complete and ext in _ZIP_EXT_KINDS:
        kind = _ZIP_EXT_KINDS[ext]
        return kind, _KIND_MIME.get(kind)
    return KIND_ARCHIVE, "application/zip"


def _probe_zip(head: bytes, tail: bytes, ext: str) -> Kind | None:
    if head[:4] not in _ZIP_MAGICS:
        return None
    members = _zip_local_members(head)
    stored = {name: (method, data) for name, method, data in members}

    def read(name: str) -> bytes | None:
        method, data = stored.get(name, (None, b""))
        if method == 0:
            return data
        if method == 8:
            try:
                # A partial raw-deflate stream still yields its leading bytes.
                return zlib.decompressobj(-15).decompress(data, 256 * 1024)
            except zlib.error:
                return None
        return None

    return _classify_zip([m[0] for m in members], read, ext, complete=False)


def _classify_ole(names: set[str], clsid: str, ext: str) -> Kind:
    low = {n.lower() for n in names}
    if "encryptedpackage" in low:
        return KIND_ENCRYPTED, _KIND_MIME[KIND_ENCRYPTED]
    if "worddocument" in low:
        return KIND_DOC, _KIND_MIME[KIND_DOC]
    if "workbook" in low or "book" in low:
        return KIND_XLS, _KIND_MIME[KIND_XLS]
    if "powerpoint document" in low:
        return KIND_PPT, _KIND_MIME[KIND_PPT]
    if any(n.startswith("__substg1.0_") for n in low):
        return KIND_MSG, _KIND_MIME[KIND_MSG]
    if clsid.upper() in _MSI_CLSIDS or ext in (".msi", ".msp"):
        return KIND_EXECUTABLE, "application/x-msi"
    if not names and ext in _OLE_EXT_KINDS:
        kind = _OLE_EXT_KINDS[ext]
        return kind, _KIND_MIME.get(kind)
    return KIND_OTHER, "application/x-cfb"


def _ole_names_from_head(head: bytes) -> tuple[set[str], str]:
    """Directory entry names in the OLE file's first directory sector, when
    that sector lies inside ``head``. Best effort: an empty set means "ask the
    extension" (:func:`detect_file` reads the whole directory instead)."""
    try:
        sector = 1 << _u16(head, 30)
        first_dir = _u32(head, 48)
        if sector not in (512, 4096) or first_dir >= 0xFFFFFFFA:
            return set(), ""
        off = (first_dir + 1) * sector
        if off + sector > len(head):
            return set(), ""
        names: set[str] = set()
        clsid = ""
        for i in range(sector // 128):
            entry = head[off + i * 128:off + (i + 1) * 128]
            nlen = _u16(entry, 64)
            if entry[66] == 0 or not 2 <= nlen <= 64:
                continue
            name = entry[:nlen - 2].decode("utf-16-le", "replace")
            if entry[66] == 5:  # root storage
                clsid = _format_clsid(entry[80:96])
            else:
                names.add(name)
        return names, clsid
    except (struct.error, IndexError):
        return set(), ""


def _format_clsid(raw: bytes) -> str:
    if len(raw) != 16 or not any(raw):
        return ""
    d1, d2, d3 = struct.unpack_from("<IHH", raw, 0)
    rest = raw[8:].hex().upper()
    return f"{d1:08X}-{d2:04X}-{d3:04X}-{rest[:4]}-{rest[4:]}"


def _probe_ole(head: bytes, tail: bytes, ext: str) -> Kind | None:
    if head[:8] != _OLE_MAGIC:
        return None
    names, clsid = _ole_names_from_head(head)
    return _classify_ole(names, clsid, ext)


def _probe_executable(head: bytes, tail: bytes, ext: str) -> Kind | None:
    if head[:4] == b"\x7fELF":
        shared = len(head) >= 18 and _u16(head, 16, big=head[5:6] == b"\x02") == 3
        return KIND_EXECUTABLE, "application/x-sharedlib" if shared else "application/x-executable"
    if head[:2] == b"MZ":
        pe = False
        if len(head) >= 0x40:
            lfanew = _u32(head, 0x3C)
            pe = lfanew + 4 <= len(head) and head[lfanew:lfanew + 4] == b"PE\x00\x00"
        # "MZ" alone is two letters a text file can start with.
        if pe or is_executable_ext(ext) or b"\x00" in head[:512]:
            return KIND_EXECUTABLE, "application/vnd.microsoft.portable-executable"
    if head[:4] in _MACHO_MAGICS:
        return KIND_EXECUTABLE, "application/x-mach-binary"
    if head[:4] == b"\xca\xfe\xba\xbe" and len(head) >= 8:
        # Shared by fat Mach-O and Java class files: a fat binary holds a
        # handful of architectures, while a class file has its major version
        # (45 and up) in the same four bytes.
        if 0 < _u32(head, 4, big=True) < 20:
            return KIND_EXECUTABLE, "application/x-mach-binary"
        return KIND_OTHER, "application/java-vm"
    if len(tail) >= 512 and tail[-512:-508] == b"koly":
        # UDIF disk images keep their trailer 512 bytes from the end; the head
        # can be anything, including bzip2 or zlib data, so this runs first.
        return KIND_EXECUTABLE, "application/x-apple-diskimage"
    if head[:4] == b"\xed\xab\xee\xdb":
        return KIND_EXECUTABLE, "application/x-rpm"
    if head[:8] == b"!<arch>\n":
        if b"debian-binary" in head[:128]:
            return KIND_EXECUTABLE, "application/vnd.debian.binary-package"
        if is_executable_ext(ext):
            return KIND_EXECUTABLE, "application/x-archive"
        return KIND_ARCHIVE, "application/x-archive"
    if head[:6] == b"xar!\x00\x1c":
        # macOS .pkg installers are xar archives.
        if ext in (".pkg", ".mpkg"):
            return KIND_EXECUTABLE, "application/x-xar"
        return KIND_ARCHIVE, "application/x-xar"
    return None


def _probe_ftyp(head: bytes) -> Kind | None:
    """ISO base media files: HEIC/AVIF photos, MP4/MOV video, M4A audio."""
    if len(head) < 12:
        return None
    box = head[4:8]
    if box in (b"moov", b"mdat", b"wide", b"pnot", b"free", b"skip") and head[0] == 0:
        # Classic QuickTime without ftyp. The leading NUL (an atom under
        # 16 MB) keeps "The free ..." in a text file from matching.
        return KIND_VIDEO, "video/quicktime"
    if box != b"ftyp":
        return None
    major = head[8:12]
    size = _u32(head, 0, big=True)
    compat = [head[i:i + 4] for i in range(16, min(size, len(head), 256) - 3, 4)]
    if major in _HEIF_BRANDS:
        if major in (b"mif1", b"msf1"):
            for brand in compat:
                if brand in _HEIF_BRANDS and brand not in (b"mif1", b"msf1"):
                    return KIND_IMAGE, _HEIF_BRANDS[brand]
        return KIND_IMAGE, _HEIF_BRANDS[major]
    if major in _AUDIO_BRANDS:
        return KIND_AUDIO, "audio/mp4"
    if major == b"crx ":
        return KIND_IMAGE, "image/x-canon-cr3"
    if major == b"qt  ":
        return KIND_VIDEO, "video/quicktime"
    if major.startswith(b"3g"):
        return KIND_VIDEO, "video/3gpp"
    known_video = major in (b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42",
                            b"avc1", b"dash", b"M4V ", b"f4v ", b"MSNV", b"NDAS")
    if not known_video:
        for brand in compat:
            if brand in _HEIF_BRANDS:
                return KIND_IMAGE, _HEIF_BRANDS[brand]
    return KIND_VIDEO, "video/mp4"


def _probe_image(head: bytes, tail: bytes, ext: str) -> Kind | None:
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return KIND_IMAGE, "image/png"
    if head[:3] == b"\xff\xd8\xff":
        return KIND_IMAGE, "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return KIND_IMAGE, "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return KIND_IMAGE, "image/webp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return KIND_IMAGE, "image/tiff"
    if head[:2] == b"BM" and len(head) >= 18 and _u32(head, 14) in (12, 40, 52, 56, 64, 108, 124):
        return KIND_IMAGE, "image/bmp"
    if head[:6] == b"8BPS\x00\x01":
        return KIND_IMAGE, "image/vnd.adobe.photoshop"
    if head[:4] in (b"\x00\x00\x01\x00", b"\x00\x00\x02\x00") and len(head) >= 22:
        count = _u16(head, 4)
        if 0 < count < 256 and head[9] == 0:
            return KIND_IMAGE, "image/x-icon"
    if head[:2] == b"\xff\x0a" or head[:12] == b"\x00\x00\x00\x0cJXL \r\n\x87\n":
        return KIND_IMAGE, "image/jxl"
    hit = _probe_ftyp(head)
    if hit and hit[0] == KIND_IMAGE:
        return hit
    return None


def _probe_media(head: bytes, tail: bytes, ext: str) -> Kind | None:
    hit = _probe_ftyp(head)
    if hit:
        return hit
    if head[:3] == b"ID3" and len(head) > 3 and head[3] in (2, 3, 4):
        return KIND_AUDIO, "audio/mpeg"
    if len(head) >= 3 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        # MPEG audio / ADTS frame sync. Reject the reserved version, layer,
        # bitrate and sample-rate codes so random 0xFF-led binaries are not
        # called audio.
        if (head[1] & 0xF6) == 0xF0:
            return KIND_AUDIO, "audio/aac"
        if ((head[1] >> 3) & 3) != 1 and (head[1] & 0x06) != 0 \
                and (head[2] >> 4) != 0xF and ((head[2] >> 2) & 3) != 3:
            return KIND_AUDIO, "audio/mpeg"
    if head[:4] == b"RIFF":
        if head[8:12] == b"WAVE":
            return KIND_AUDIO, "audio/wav"
        if head[8:12] == b"AVI ":
            return KIND_VIDEO, "video/x-msvideo"
    if head[:4] == b"fLaC" and len(head) > 4 and (head[4] & 0x7F) == 0:
        return KIND_AUDIO, "audio/flac"
    if head[:5] == b"OggS\x00":
        if b"\x80theora" in head[:512] or b"\x01video" in head[:512]:
            return KIND_VIDEO, "video/ogg"
        return KIND_AUDIO, "audio/ogg"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        if b"webm" in head[:64]:
            return KIND_VIDEO, "video/webm"
        if ext == ".mka":
            return KIND_AUDIO, "audio/x-matroska"
        return KIND_VIDEO, "video/x-matroska"
    if head[:8] == b"MThd\x00\x00\x00\x06":
        return KIND_AUDIO, "audio/midi"
    if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        return KIND_AUDIO, "audio/aiff"
    if head[:8] == b"\x30\x26\xb2\x75\x8e\x66\xcf\x11":
        if ext == ".wma":
            return KIND_AUDIO, "audio/x-ms-wma"
        return KIND_VIDEO, "video/x-ms-asf"
    if head[:4] == b"FLV\x01":
        return KIND_VIDEO, "video/x-flv"
    if head[:4] in (b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3"):
        return KIND_VIDEO, "video/mpeg"
    if len(head) > 752 and all(head[i] == 0x47 for i in range(0, 753, 188)) \
            and b"\x00" in head[:752]:
        # Five 188-byte packets in a row, and binary: a text file whose
        # every 188th character happens to be "G" does not qualify.
        return KIND_VIDEO, "video/mp2t"
    if head[:6] == b"#!AMR\n":
        return KIND_AUDIO, "audio/amr"
    return None


def _probe_archive(head: bytes, tail: bytes, ext: str) -> Kind | None:
    if head[:2] == b"\x1f\x8b":
        return KIND_ARCHIVE, "application/gzip"
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return KIND_ARCHIVE, "application/x-7z-compressed"
    if head[:6] == b"Rar!\x1a\x07":
        return KIND_ARCHIVE, "application/vnd.rar"
    if head[:6] == b"\xfd7zXZ\x00":
        return KIND_ARCHIVE, "application/x-xz"
    if head[:3] == b"BZh" and len(head) >= 10 and 0x31 <= head[3] <= 0x39 \
            and head[4:10] in (b"1AY&SY", b"\x17rE8P\x90"):
        return KIND_ARCHIVE, "application/x-bzip2"
    if head[:4] == b"\x28\xb5\x2f\xfd":
        return KIND_ARCHIVE, "application/zstd"
    if head[:4] == b"\x04\x22\x4d\x18":
        return KIND_ARCHIVE, "application/x-lz4"
    if head[:8] == b"MSCF\x00\x00\x00\x00":
        return KIND_ARCHIVE, "application/vnd.ms-cab-compressed"
    if head[:5] == b"LZIP\x01":
        return KIND_ARCHIVE, "application/x-lzip"
    if head[257:262] == b"ustar" and b"\x00" in head[:257]:
        return KIND_ARCHIVE, "application/x-tar"
    return None


def _probe_misc(head: bytes, tail: bytes, ext: str) -> Kind | None:
    if head[:16] == b"SQLite format 3\x00":
        return KIND_DATABASE, "application/vnd.sqlite3"
    if head[4:19] in (b"Standard Jet DB", b"Standard ACE DB"):
        return KIND_DATABASE, "application/x-msaccess"
    # Font signatures are short and two of them ("true", "OTTO") are words, so
    # each also needs a sane table count (big-endian, so text bytes make it
    # huge) or a known flavour.
    if head[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"true") and len(head) >= 12 \
            and 0 < _u16(head, 4, big=True) < 100:
        return KIND_FONT, "font/otf" if head[:4] == b"OTTO" else "font/ttf"
    if head[:4] == b"ttcf" and head[4:8] in (b"\x00\x01\x00\x00", b"\x00\x02\x00\x00"):
        return KIND_FONT, "font/collection"
    if head[:4] in (b"wOFF", b"wOF2") and head[4:8] in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
        return KIND_FONT, "font/woff" if head[:4] == b"wOFF" else "font/woff2"
    if head[:5] == b"{\\rtf":
        return KIND_RTF, "application/rtf"
    if head[:8] == b"bplist00":
        return KIND_OTHER, "application/x-bplist"
    return None


_PROBES = (_probe_pdf, _probe_zip, _probe_ole, _probe_executable, _probe_image,
           _probe_media, _probe_archive, _probe_misc)

_UTF16_BOMS = (codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)


def _looks_text(head: bytes) -> bool:
    """True when ``head`` reads as text: no NUL bytes (except in UTF-16/32
    with a BOM), and it decodes as UTF-8 or as a charset the detector names
    with few control characters."""
    if not head:
        return True
    if head.startswith(_UTF16_BOMS):
        return True
    if b"\x00" in head:
        return False
    try:
        # final=False: a multi-byte character cut by the 8 KiB boundary is fine.
        codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
        return True
    except UnicodeDecodeError:
        pass
    controls = sum(1 for b in head if b < 9 or (13 < b < 32 and b != 27))
    if controls > len(head) * 0.02:
        return False
    try:
        import charset_normalizer
    except ImportError:
        return True
    return charset_normalizer.from_bytes(head).best() is not None


_EMAIL_START_RE = re.compile(
    rb"^(?:From |Return-Path:|Received:|Delivered-To:|MIME-Version:|Message-ID:|From:)", re.IGNORECASE)


def _text_kind(head: bytes, ext: str) -> Kind:
    if ext in _MARKDOWN_EXTS:
        return KIND_MARKDOWN, _TEXT_MIME[KIND_MARKDOWN]
    if ext in _CODE_EXTS:
        return KIND_CODE, _CODE_MIME.get(ext, "text/plain")
    if ext in _CSV_EXTS:
        return KIND_CSV, "text/tab-separated-values" if ext in (".tsv", ".tab") else "text/csv"
    if ext in _JSON_EXTS:
        return KIND_JSON, _TEXT_MIME[KIND_JSON]
    if ext in _XML_EXTS:
        return KIND_XML, "image/svg+xml" if ext == ".svg" else _TEXT_MIME[KIND_XML]
    if ext in _HTML_EXTS:
        return KIND_HTML, _TEXT_MIME[KIND_HTML]
    if ext in _EML_EXTS:
        return KIND_EML, _TEXT_MIME[KIND_EML]
    # No telling extension: a few unambiguous openings.
    start = head.lstrip(b"\xef\xbb\xbf \t\r\n")[:256].lower()
    if start.startswith(b"<?xml"):
        return KIND_XML, "image/svg+xml" if b"<svg" in head[:2048].lower() else _TEXT_MIME[KIND_XML]
    if start.startswith((b"<!doctype html", b"<html")):
        return KIND_HTML, _TEXT_MIME[KIND_HTML]
    if start.startswith(b"#!") and b"python" in start.split(b"\n", 1)[0]:
        return KIND_CODE, "text/x-python"
    if not ext and _EMAIL_START_RE.match(head):
        return KIND_EML, _TEXT_MIME[KIND_EML]
    return KIND_TEXT, _TEXT_MIME[KIND_TEXT]


def sniff(head: bytes, tail: bytes, ext: str | None) -> Kind:
    """``(kind, mime)`` from a file's first bytes (up to 8 KiB), its last 512
    bytes and its extension. Magic bytes win; the extension only breaks ties.
    ZIP and OLE files are classified from what the head shows of their
    contents; :func:`detect_file` looks further."""
    head = head or b""
    tail = tail or b""
    ext = _norm_ext(ext)
    if not head.startswith(_UTF16_BOMS):
        # A UTF-16 LE BOM (FF FE) is also a valid MPEG frame-sync pattern.
        for probe in _PROBES:
            hit = probe(head, tail, ext)
            if hit:
                return hit
    if _looks_text(head):
        return _text_kind(head, ext)
    if is_executable_ext(ext):
        return KIND_EXECUTABLE, None
    return KIND_OTHER, None


def _refine_zip(source: str | BinaryIO, ext: str, fallback: Kind) -> Kind:
    import zipfile

    try:
        with zipfile.ZipFile(source) as zf:
            names = zf.namelist()

            def read(name: str) -> bytes | None:
                try:
                    info = zf.getinfo(name)
                    if info.file_size > 1024 * 1024:
                        return None  # a real [Content_Types].xml is a few KB
                    return zf.read(name)
                except (KeyError, OSError, RuntimeError, zipfile.BadZipFile, NotImplementedError, zlib.error):
                    return None

            return _classify_zip(names, read, ext, complete=True)
    except (zipfile.BadZipFile, OSError, ValueError, EOFError):
        return fallback


def _refine_ole(source: str | BinaryIO, ext: str, fallback: Kind) -> Kind:
    """``source`` is a path or a file object, never raw bytes: olefile reads
    bytes shorter than a minimal OLE file as a *file name*."""
    try:
        import olefile
    except ImportError:
        return fallback
    try:
        ole = olefile.OleFileIO(source)
    except Exception:  # olefile raises plain OSError/ValueError subclasses on junk
        return fallback
    try:
        names = {entry[0] for entry in ole.listdir(streams=True, storages=True) if entry}
        clsid = getattr(ole.root, "clsid", "") or ""
        return _classify_ole(names, clsid, ext)
    except Exception:
        return fallback
    finally:
        ole.close()


def detect_file(path: str) -> Kind:
    """``(kind, mime)`` for a local file: :func:`sniff` on its head and tail,
    then a look inside ZIP and OLE containers. Raises :class:`OSError` when the
    file cannot be opened; the caller decides what a locked file means."""
    ext = os.path.splitext(path)[1].lower()
    if os.path.isdir(path):
        return (KIND_BUNDLE, None) if ext in _BUNDLE_EXTS else (KIND_OTHER, None)
    with open(path, "rb") as fh:
        head = fh.read(HEAD_BYTES)
        size = os.fstat(fh.fileno()).st_size
        if size <= len(head):
            tail = head[-TAIL_BYTES:]
        else:
            fh.seek(size - TAIL_BYTES)
            tail = fh.read(TAIL_BYTES)
    found = sniff(head, tail, ext)
    if head[:4] in _ZIP_MAGICS:
        return _refine_zip(path, ext, found)
    if head[:8] == _OLE_MAGIC:
        return _refine_ole(path, ext, found)
    return found


def detect_bytes(data: bytes, name: str) -> Kind:
    """``(kind, mime)`` for a file held in memory, named ``name`` (only its
    extension is used, as a tie-breaker). The same decision as
    :func:`detect_file`: :func:`sniff`, then the whole ZIP directory or OLE
    directory, read from ``data`` through a stream."""
    data = data or b""
    ext = os.path.splitext(name or "")[1].lower()
    head = data[:HEAD_BYTES]
    tail = data[-TAIL_BYTES:] if data else b""
    found = sniff(head, tail, ext)
    if head[:4] in _ZIP_MAGICS:
        return _refine_zip(io.BytesIO(data), ext, found)
    if head[:8] == _OLE_MAGIC:
        return _refine_ole(io.BytesIO(data), ext, found)
    return found
