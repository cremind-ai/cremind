"""Magic-byte detection: bytes decide, the extension only breaks ties."""

from __future__ import annotations

import io
import os
import struct
import sys
import tarfile
import zipfile

import pytest

from app.documents.extract.detect import detect_file, is_executable_ext, sniff
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

from .extract_fixtures import make_cfb, make_docx, make_epub, make_odf, make_pdf, make_word_streams, zip_bytes

_PE = (b"MZ" + b"\x90" * 58 + struct.pack("<I", 64) + b"PE\x00\x00" + struct.pack("<H", 0x8664) + b"\x00" * 64)


@pytest.mark.parametrize("head, ext, kind, mime", [
    (b"%PDF-1.7\n", "", KIND_PDF, "application/pdf"),
    (b"\x89PNG\r\n\x1a\n" + b"\x00" * 16, "", KIND_IMAGE, "image/png"),
    (b"\xff\xd8\xff\xe0" + b"\x00" * 16, ".png", KIND_IMAGE, "image/jpeg"),  # magic beats the extension
    (b"GIF89a" + b"\x00" * 10, "", KIND_IMAGE, "image/gif"),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "", KIND_IMAGE, "image/webp"),
    (b"II*\x00" + b"\x00" * 8, "", KIND_IMAGE, "image/tiff"),
    (b"BM" + b"\x00" * 12 + struct.pack("<I", 40) + b"\x00" * 8, "", KIND_IMAGE, "image/bmp"),
    (b"\x00\x00\x00\x1cftypheic\x00\x00\x00\x00mif1heicmiaf", "", KIND_IMAGE, "image/heic"),
    (b"\x00\x00\x00\x1cftypmif1\x00\x00\x00\x00mif1heic", "", KIND_IMAGE, "image/heic"),
    (b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1", "", KIND_IMAGE, "image/avif"),
    (b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2", "", KIND_VIDEO, "video/mp4"),
    (b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  ", "", KIND_VIDEO, "video/quicktime"),
    (b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00M4A mp42", "", KIND_AUDIO, "audio/mp4"),
    (b"ID3\x04\x00\x00" + b"\x00" * 10, "", KIND_AUDIO, "audio/mpeg"),
    (b"\xff\xfb\x90\x64" + b"\x00" * 10, "", KIND_AUDIO, "audio/mpeg"),
    (b"RIFF\x00\x00\x00\x00WAVEfmt ", "", KIND_AUDIO, "audio/wav"),
    (b"fLaC\x00\x00\x00\x22", "", KIND_AUDIO, "audio/flac"),
    (b"OggS\x00\x02" + b"\x00" * 20 + b"\x01vorbis", "", KIND_AUDIO, "audio/ogg"),
    (b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01webm", "", KIND_VIDEO, "video/webm"),
    (b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01matroska", "", KIND_VIDEO, "video/x-matroska"),
    (b"\x1f\x8b\x08\x00" + b"\x00" * 10, "", KIND_ARCHIVE, "application/gzip"),
    (b"7z\xbc\xaf\x27\x1c\x00\x04", "", KIND_ARCHIVE, "application/x-7z-compressed"),
    (b"Rar!\x1a\x07\x01\x00", "", KIND_ARCHIVE, "application/vnd.rar"),
    (b"\xfd7zXZ\x00\x00\x04", "", KIND_ARCHIVE, "application/x-xz"),
    (b"BZh91AY&SY" + b"\x00" * 6, "", KIND_ARCHIVE, "application/x-bzip2"),
    (b"\x28\xb5\x2f\xfd\x00\x00", "", KIND_ARCHIVE, "application/zstd"),
    (b"SQLite format 3\x00" + b"\x10\x00", "", KIND_DATABASE, "application/vnd.sqlite3"),
    (b"\x00\x01\x00\x00\x00\x0c\x00\x80\x00\x03\x00\x40", "", KIND_FONT, "font/ttf"),
    (b"OTTO\x00\x0c\x00\x80\x00\x03\x00\x40", "", KIND_FONT, "font/otf"),
    (b"wOF2\x00\x01\x00\x00" + b"\x00" * 8, "", KIND_FONT, "font/woff2"),
    (b"\x7fELF\x02\x01\x01" + b"\x00" * 9 + struct.pack("<HH", 2, 0x3E), "", KIND_EXECUTABLE,
     "application/x-executable"),
    (_PE, "", KIND_EXECUTABLE, "application/vnd.microsoft.portable-executable"),
    (b"\xcf\xfa\xed\xfe" + struct.pack("<I", 0x0100000C) + b"\x00" * 8, "", KIND_EXECUTABLE,
     "application/x-mach-binary"),
    (b"\xca\xfe\xba\xbe\x00\x00\x00\x02" + b"\x00" * 40, "", KIND_EXECUTABLE, "application/x-mach-binary"),
    (b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + b"\x00" * 40, ".class", KIND_OTHER, "application/java-vm"),
    (b"\xed\xab\xee\xdb\x03\x00" + b"\x00" * 10, "", KIND_EXECUTABLE, "application/x-rpm"),
    (b"!<arch>\ndebian-binary   ", ".deb", KIND_EXECUTABLE, "application/vnd.debian.binary-package"),
    (b"{\\rtf1\\ansi hello}", "", KIND_RTF, "application/rtf"),
])
def test_magic_table(head: bytes, ext: str, kind: str, mime: str) -> None:
    assert sniff(head, b"", ext) == (kind, mime)


def test_tar_needs_the_ustar_magic_at_257() -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("a.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    assert sniff(buf.getvalue()[:8192], b"", "") == (KIND_ARCHIVE, "application/x-tar")


def test_dmg_is_found_by_its_trailer() -> None:
    data = b"BZh91AY&SY" + os.urandom(3000) + b"koly" + b"\x00" * 508
    # The head alone looks like bzip2; the koly trailer wins.
    assert sniff(data[:8192], data[-512:], ".dmg") == (KIND_EXECUTABLE, "application/x-apple-diskimage")


@pytest.mark.parametrize("text", [
    b"The free software movement\n",   # "free" at offset 4: a QuickTime atom name
    b"true story about fonts\n",        # a TrueType signature
    b"OTTO is my cat\n",                # an OpenType signature
    b"MZ Corporation annual report\n",  # a DOS executable signature
    b"GGGG" * 300,                      # an MPEG-TS sync byte every 188 bytes
])
def test_text_that_starts_like_a_signature_stays_text(text: bytes) -> None:
    assert sniff(text, b"", ".txt")[0] == KIND_TEXT


def test_utf16_text_with_bom_is_text_not_audio() -> None:
    # FF FE is also a valid MPEG frame-sync pattern.
    data = "Xin chào".encode("utf-16")
    assert sniff(data, b"", ".txt") == (KIND_TEXT, "text/plain")


def test_late_pdf_header_needs_the_extension() -> None:
    head = b"garbage-prefix " + b"%PDF-1.4\n"
    assert sniff(head, b"", ".pdf")[0] == KIND_PDF
    assert sniff(head, b"", ".txt")[0] == KIND_TEXT


@pytest.mark.parametrize("ext, kind, mime", [
    (".md", KIND_MARKDOWN, "text/markdown"),
    (".py", KIND_CODE, "text/x-python"),
    (".rs", KIND_CODE, "text/x-rust"),
    (".csv", KIND_CSV, "text/csv"),
    (".tsv", KIND_CSV, "text/tab-separated-values"),
    (".json", KIND_JSON, "application/json"),
    (".xml", KIND_XML, "application/xml"),
    (".svg", KIND_XML, "image/svg+xml"),
    (".html", KIND_HTML, "text/html"),
    (".eml", KIND_EML, "message/rfc822"),
    (".txt", KIND_TEXT, "text/plain"),
    ("", KIND_TEXT, "text/plain"),
])
def test_text_subtype_by_extension(ext: str, kind: str, mime: str) -> None:
    assert sniff(b"plain words\n", b"", ext) == (kind, mime)


def test_text_subtype_by_content_without_extension() -> None:
    assert sniff(b'<?xml version="1.0"?><a/>', b"", "")[0] == KIND_XML
    assert sniff(b"<!DOCTYPE html><html></html>", b"", "")[0] == KIND_HTML
    assert sniff(b"#!/usr/bin/env python3\nprint(1)\n", b"", "") == (KIND_CODE, "text/x-python")
    assert sniff(b"From: a@b.c\nSubject: hi\n\nbody", b"", "")[0] == KIND_EML


def test_binary_without_magic_is_other_or_executable_by_extension() -> None:
    blob = bytes(range(256)) * 4
    assert sniff(blob, b"", ".dat") == (KIND_OTHER, None)
    assert sniff(blob, b"", ".bin") == (KIND_EXECUTABLE, None)


def test_legacy_single_byte_text_is_text() -> None:
    text = "Le café est très bon. Déjà vu, à la carte.\n".encode("cp1252") * 20
    assert sniff(text, b"", ".txt")[0] == KIND_TEXT


def test_is_executable_ext() -> None:
    for ext in (".exe", "dll", ".SO", ".dylib", ".msi", ".app", ".dmg", ".pkg", ".deb", ".rpm", ".AppImage", ".bin"):
        assert is_executable_ext(ext), ext
    for ext in (".pdf", ".txt", "", None):
        assert not is_executable_ext(ext)


# ── ZIP containers ─────────────────────────────────────────────────────────


def test_sniff_classifies_zip_from_the_first_members() -> None:
    docx = make_docx([(None, "x")])
    assert sniff(docx[:8192], b"", "")[0] == KIND_DOCX
    odt = make_odf("application/vnd.oasis.opendocument.text", "<office:text/>")
    assert sniff(odt[:8192], b"", "")[0] == KIND_ODT
    assert sniff(make_epub()[:8192], b"", "")[0] == KIND_EPUB


def test_sniff_zip_falls_back_to_extension_only_when_members_are_unseen() -> None:
    plain = zip_bytes({"a.txt": "hello"})
    assert sniff(plain, b"", ".docx")[0] == KIND_DOCX   # incomplete view: the name breaks the tie
    assert sniff(plain, b"", ".zip") == (KIND_ARCHIVE, "application/zip")


def _write(tmp_path, name: str, data: bytes) -> str:
    path = os.path.join(tmp_path, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def test_detect_file_reads_the_zip_directory(tmp_path) -> None:
    import openpyxl
    from pptx import Presentation

    wb = openpyxl.Workbook()
    xlsx = _write(tmp_path, "book.bin", b"")  # extension says nothing
    wb.save(xlsx)
    pptx = str(tmp_path / "deck")
    Presentation().save(pptx)
    ods = _write(tmp_path, "s", make_odf("application/vnd.oasis.opendocument.spreadsheet", "<office:spreadsheet/>"))
    odp = _write(tmp_path, "p", make_odf("application/vnd.oasis.opendocument.presentation", "<office:presentation/>"))
    assert detect_file(xlsx)[0] == KIND_XLSX
    assert detect_file(pptx)[0] == KIND_PPTX
    assert detect_file(ods)[0] == KIND_ODS
    assert detect_file(odp)[0] == KIND_ODP
    # A plain zip named .docx is still an archive once its directory is read.
    fake = _write(tmp_path, "fake.docx", zip_bytes({"notes.txt": "x"}))
    assert detect_file(fake) == (KIND_ARCHIVE, "application/zip")


def test_detect_file_jar_and_apk_are_archives(tmp_path) -> None:
    jar = _write(tmp_path, "a.jar", zip_bytes({"META-INF/MANIFEST.MF": "Manifest-Version: 1.0", "A.class": "x"}))
    apk = _write(tmp_path, "a.apk", zip_bytes({"AndroidManifest.xml": "x", "classes.dex": "x"}))
    assert detect_file(jar) == (KIND_ARCHIVE, "application/java-archive")
    assert detect_file(apk) == (KIND_ARCHIVE, "application/vnd.android.package-archive")


def test_docx_with_embedded_chart_is_still_docx(tmp_path) -> None:
    # The embedded workbook declares a spreadsheetml type; only the main part counts.
    ct = ('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="xlsx" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
          'wordprocessingml.document.main+xml"/></Types>')
    path = _write(tmp_path, "x", zip_bytes({"[Content_Types].xml": ct, "word/document.xml": "<x/>",
                                            "word/embeddings/chart.xlsx": "x"}))
    assert detect_file(path)[0] == KIND_DOCX


# ── OLE containers ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("streams, name, kind", [
    ({"Workbook": b"x"}, "a", KIND_XLS),
    ({"Book": b"x"}, "a", KIND_XLS),
    ({"PowerPoint Document": b"x", "Current User": b"x"}, "a", KIND_PPT),
    ({"__substg1.0_0037001F": b"x"}, "a", KIND_MSG),
    ({"EncryptedInfo": b"x", "EncryptedPackage": b"x"}, "a.docx", KIND_ENCRYPTED),
    ({"Contents": b"x"}, "a", KIND_OTHER),
])
def test_detect_file_classifies_ole_by_stream_names(tmp_path, streams, name, kind) -> None:
    path = _write(tmp_path, name, make_cfb(streams))
    assert detect_file(path)[0] == kind


def test_detect_file_doc_and_msi(tmp_path) -> None:
    doc = _write(tmp_path, "letter", make_cfb(make_word_streams([("hi\r", True)])))
    assert detect_file(doc) == (KIND_DOC, "application/msword")
    # {000C1084-0000-0000-C000-000000000046}, stored little-endian.
    msi_clsid = bytes.fromhex("84100C00" "0000" "0000" "C000000000000046")
    msi = _write(tmp_path, "setup", make_cfb({"x": b"1"}, clsid=msi_clsid))
    assert detect_file(msi) == (KIND_EXECUTABLE, "application/x-msi")


def test_sniff_ole_uses_the_first_directory_sector(tmp_path) -> None:
    data = make_cfb(make_word_streams([("hi\r", True)]))
    assert sniff(data[:8192], b"", "")[0] == KIND_DOC


# ── Files and directories ──────────────────────────────────────────────────


def test_detect_file_pdf_and_text(tmp_path) -> None:
    pdf = _write(tmp_path, "report", make_pdf([[(12, 72, 700, "hello")]]))
    notes = _write(tmp_path, "notes.md", "# Tiêu đề\n\nnội dung\n".encode())
    assert detect_file(pdf) == (KIND_PDF, "application/pdf")
    assert detect_file(notes) == (KIND_MARKDOWN, "text/markdown")


def test_detect_file_on_the_running_interpreter() -> None:
    kind, _mime = detect_file(sys.executable)
    assert kind == KIND_EXECUTABLE


def test_detect_file_bundle_directory(tmp_path) -> None:
    app = tmp_path / "Tool.app"
    app.mkdir()
    assert detect_file(str(app)) == (KIND_BUNDLE, None)
    plain = tmp_path / "folder"
    plain.mkdir()
    assert detect_file(str(plain)) == (KIND_OTHER, None)


def test_detect_file_missing_raises(tmp_path) -> None:
    with pytest.raises(OSError):
        detect_file(str(tmp_path / "missing.txt"))


def test_detect_file_does_not_modify_the_file(tmp_path) -> None:
    path = _write(tmp_path, "a.zip", zip_bytes({"a": "b"}))
    before = (os.path.getsize(path), os.stat(path).st_mtime_ns, open(path, "rb").read())
    detect_file(path)
    assert (os.path.getsize(path), os.stat(path).st_mtime_ns, open(path, "rb").read()) == before


def test_corrupt_zip_falls_back_to_the_head(tmp_path) -> None:
    data = make_docx([(None, "x")])
    path = _write(tmp_path, "cut.docx", data[: len(data) // 2])  # central directory lost
    assert detect_file(path)[0] == KIND_DOCX


def test_zip_member_bomb_is_not_read(tmp_path) -> None:
    # A [Content_Types].xml that inflates to 5 MB is skipped, not decompressed.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types>" + " " * (5 * 1024 * 1024) + "</Types>")
        zf.writestr("readme.txt", "x")
    path = _write(tmp_path, "b.zip", buf.getvalue())
    assert detect_file(path)[0] == KIND_OTHER  # an OPC package with no main part we read
