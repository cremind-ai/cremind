"""File kind from the name alone — for when the content must not be read.

Magic-byte detection (:mod:`app.userdocs.extract.detect`) is the truth and is
used for every file the engine extracts. This module exists for the cases where
opening the file is wrong: a cloud placeholder (reading it downloads it), a
secret-looking file (reading it is the thing we promised not to do), and the
pre-sync estimate (which must stay a cheap stat-only walk).
"""

from __future__ import annotations

import os

from app.userdocs import types as t
from app.userdocs.extract import detect as _detect

_IMAGE_EXTS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".avif", ".tif",
    ".tiff", ".bmp", ".jfif",
})
_AUDIO_EXTS = frozenset({".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".wma", ".amr"})
_VIDEO_EXTS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".m4v", ".3gp", ".flv"})
_ARCHIVE_EXTS = frozenset({
    ".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".iso", ".cab",
})
_FONT_EXTS = frozenset({".ttf", ".otf", ".woff", ".woff2"})
_DB_EXTS = frozenset({".db", ".sqlite", ".sqlite3", ".mdb", ".accdb"})


def guess_kind(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext in _detect._BUNDLE_EXTS:
        return t.KIND_BUNDLE
    if ext in _detect._EXECUTABLE_EXTS:
        return t.KIND_EXECUTABLE
    if ext in _IMAGE_EXTS:
        return t.KIND_IMAGE
    if ext in _AUDIO_EXTS:
        return t.KIND_AUDIO
    if ext in _VIDEO_EXTS:
        return t.KIND_VIDEO
    if ext in _ARCHIVE_EXTS:
        return t.KIND_ARCHIVE
    if ext in _FONT_EXTS:
        return t.KIND_FONT
    if ext in _DB_EXTS:
        return t.KIND_DATABASE
    if ext == ".pdf":
        return t.KIND_PDF
    if ext in _detect._ZIP_EXT_KINDS:
        return _detect._ZIP_EXT_KINDS[ext]
    if ext in _detect._OLE_EXT_KINDS:
        return _detect._OLE_EXT_KINDS[ext]
    if ext == ".rtf":
        return t.KIND_RTF
    if ext in _detect._MARKDOWN_EXTS:
        return t.KIND_MARKDOWN
    if ext in _detect._CODE_EXTS:
        return t.KIND_CODE
    if ext in _detect._CSV_EXTS:
        return t.KIND_CSV
    if ext in _detect._JSON_EXTS:
        return t.KIND_JSON
    if ext in _detect._XML_EXTS:
        return t.KIND_XML
    if ext in _detect._HTML_EXTS:
        return t.KIND_HTML
    if ext in _detect._EML_EXTS:
        return t.KIND_EML
    if ext in (".txt", ".text", ".log", ".ini", ".cfg", ".conf", ".toml", ".yaml", ".yml", ""):
        return t.KIND_TEXT
    return t.KIND_OTHER
