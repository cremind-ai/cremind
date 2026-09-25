"""DOCX, HTML, EPUB and MSG through markitdown, plus OOXML core metadata.

Each format calls its markitdown converter directly instead of going through
``MarkItDown().convert_stream``. The facade loads a Magika model (an ONNX
runtime with its own threads) on construction, sniffs every stream again, and
when the chosen converter fails it tries the rest, including the ZIP
converter, which would unpack a broken DOCX's members in memory. The kind is
already known here, so one converter runs and its failure is the answer.

The Markdown that comes back is parsed by :func:`.markdown.emit_markdown` in
``structure`` mode: its line numbers exist nowhere the user can open, so blocks
are located by heading path and paragraph number instead.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any

from ._base import Ctx, decode_text, iso_datetime
from .markdown import emit_markdown

# mammoth inlines pictures as data URIs; markitdown shortens them to
# "data:image/png;base64..." but the reference is still noise in a chunk.
_DATA_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(data:[^)]*\)")
_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([A-Za-z0-9_\-]+)""", re.IGNORECASE)

_CORE_FIELDS = {
    "title": "title",
    "creator": "author",
    "lastModifiedBy": "last_modified_by",
    "subject": "subject",
    "keywords": "keywords",
    "description": "description",
    "category": "category",
}
_CORE_DATES = {"created": "created", "modified": "modified"}
_APP_INTS = {"Pages": "pages", "Words": "words", "Slides": "slides"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _read_member(zf: zipfile.ZipFile, name: str, cap: int = 2 * 1024 * 1024) -> bytes | None:
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > cap:
        return None
    return zf.read(info)


def core_properties(ctx: Ctx) -> dict[str, Any]:
    """Title, author, dates and counts from an OOXML package's ``docProps``.
    Best effort: an unreadable part yields nothing, never an error."""
    try:
        from defusedxml import ElementTree as ET
    except ImportError:
        return {}
    try:
        with zipfile.ZipFile(ctx.source()) as zf:
            core = _read_member(zf, "docProps/core.xml")
            app = _read_member(zf, "docProps/app.xml")
    except (zipfile.BadZipFile, OSError, ValueError):
        return {}
    meta: dict[str, Any] = {}
    if core:
        try:
            for el in ET.fromstring(core):
                name, text = _local(el.tag), (el.text or "").strip()
                if not text:
                    continue
                if name in _CORE_FIELDS:
                    meta[_CORE_FIELDS[name]] = text
                elif name in _CORE_DATES:
                    meta[_CORE_DATES[name]] = iso_datetime(text)
        except Exception:  # defusedxml raises its own types for DTDs/entities
            pass
    if app:
        try:
            for el in ET.fromstring(app):
                name, text = _local(el.tag), (el.text or "").strip()
                if name in _APP_INTS and text.isdigit():
                    meta[_APP_INTS[name]] = int(text)
                elif name == "Application" and text:
                    meta["app"] = text
        except Exception:
            pass
    return meta


def _clean_markdown(markdown: str) -> str:
    markdown = _DATA_IMAGE_RE.sub(lambda m: f"[image: {m.group(1)}]" if m.group(1).strip() else "", markdown)
    return markdown.replace("\r\n", "\n").replace("\r", "\n")


def _stream_info(ext: str) -> Any:
    from markitdown import StreamInfo

    return StreamInfo(extension=ext)


def extract_docx(ctx: Ctx) -> None:
    from markitdown.converters import DocxConverter

    ctx.result.doc_meta.update(core_properties(ctx))
    data, _ = ctx.read_bytes()
    result = DocxConverter().convert(io.BytesIO(data), _stream_info(".docx"))
    emit_markdown(ctx, _clean_markdown(result.markdown), mode="structure")


def _decode_html(data: bytes) -> str:
    declared = _META_CHARSET_RE.search(data[:4096])
    if declared:
        try:
            return data.decode(declared.group(1).decode("ascii"), "replace")
        except LookupError:
            pass
    return decode_text(data)[0]


def extract_html(ctx: Ctx) -> None:
    from markitdown.converters import HtmlConverter

    data, truncated = ctx.read_bytes(ctx.max_text_bytes)
    if truncated:
        ctx.mark_partial()
    result = HtmlConverter().convert_string(_decode_html(data))
    if result.title and result.title.strip():
        ctx.result.doc_meta["title"] = result.title.strip()
    emit_markdown(ctx, _clean_markdown(result.markdown), mode="structure")


def extract_epub(ctx: Ctx) -> None:
    from markitdown.converters import EpubConverter

    data, _ = ctx.read_bytes()
    result = EpubConverter().convert(io.BytesIO(data), _stream_info(".epub"))
    if result.title and result.title.strip():
        ctx.result.doc_meta["title"] = result.title.strip()
    emit_markdown(ctx, _clean_markdown(result.markdown), mode="structure")


_MSG_HEADER_RE = re.compile(r"^\*\*(From|To|Subject):\*\*\s*(.*)$", re.MULTILINE)


def extract_msg(ctx: Ctx) -> None:
    from markitdown.converters import OutlookMsgConverter

    data, _ = ctx.read_bytes()
    result = OutlookMsgConverter().convert(io.BytesIO(data), _stream_info(".msg"))
    markdown = _clean_markdown(result.markdown)
    for field, value in _MSG_HEADER_RE.findall(markdown):
        if field == "From" and value.strip():
            ctx.result.doc_meta["author"] = value.strip()
        elif field == "Subject" and value.strip():
            ctx.result.doc_meta["title"] = value.strip()
    # markitdown's fixed "# Email Message" banner would otherwise become the
    # document's top heading and title.
    markdown = re.sub(r"\A\s*# Email Message\s*\n", "", markdown)
    emit_markdown(ctx, markdown, mode="structure")
