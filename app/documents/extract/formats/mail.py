"""EML (and MHT): headers, the body text, and attachment names.

Parsed with the stdlib ``email`` package under ``policy.default``, which
decodes RFC 2047 headers and transfer encodings. Each part (headers, body,
attachment list) starts with a hard anchor, so a chunk never mixes the
envelope with the message. Attachments are listed by name only; their
content is never decoded here.

The module is ``mail``, not ``email``, so it can never shadow the stdlib.
"""

from __future__ import annotations

import email
from email import policy
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from app.documents.types import ANCHOR_HARD, ANCHOR_NONE

from ._base import Ctx, decode_text

_HEADERS = ("From", "To", "Cc", "Date", "Subject")


class _HtmlText(HTMLParser):
    """HTML to plain text with paragraph breaks at block elements."""

    _BLOCK = {"p", "div", "br", "li", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6",
              "blockquote", "pre", "hr", "ul", "ol", "section", "article"}
    _SKIP = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BLOCK:
            self.parts.append("\n\n" if tag != "br" else "\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skipping = max(0, self._skipping - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _HtmlText()
    parser.feed(html)
    parser.close()
    lines = [" ".join(line.split()) for line in "".join(parser.parts).split("\n")]
    return "\n".join(lines)


def _part_text(part: Any) -> str:
    try:
        content = part.get_content()
    except (LookupError, KeyError, UnicodeError, ValueError, AssertionError):
        content = None
    if isinstance(content, bytes):
        content = decode_text(content)[0]
    if content is None:
        payload = part.get_payload(decode=True) or b""
        content = decode_text(payload)[0] if isinstance(payload, bytes) else str(payload)
    return content.replace("\r\n", "\n").replace("\r", "\n")


def extract_eml(ctx: Ctx) -> None:
    with ctx.open() as fh:
        msg = email.message_from_binary_file(fh, policy=policy.default)

    header_lines = []
    for name in _HEADERS:
        value = msg.get(name)
        if value is not None and str(value).strip():
            header_lines.append(f"{name}: {' '.join(str(value).split())}")
    doc_meta = ctx.result.doc_meta
    if msg.get("Subject"):
        doc_meta["title"] = " ".join(str(msg["Subject"]).split())
    if msg.get("From"):
        doc_meta["author"] = " ".join(str(msg["From"]).split())
    if msg.get("Date"):
        try:
            doc_meta["created"] = parsedate_to_datetime(str(msg["Date"])).isoformat()
        except (TypeError, ValueError, IndexError):
            pass
    if header_lines:
        ctx.add("\n".join(header_lines), anchor=ANCHOR_HARD, role="meta", locator={"part": "headers"})

    body = msg.get_body(preferencelist=("plain", "html"))
    if body is not None:
        text = _part_text(body)
        if body.get_content_subtype() == "html":
            text = html_to_text(text)
        anchor = ANCHOR_HARD
        for para in _paragraphs(text):
            ctx.add(para, anchor=anchor, locator={"part": "body"})
            anchor = ANCHOR_NONE

    names = []
    for part in msg.iter_attachments():
        name = part.get_filename()
        if not name and part.get_content_type() == "message/rfc822":
            name = "(attached message)"
        if name:
            names.append(" ".join(str(name).split()))
    if names:
        doc_meta["attachments"] = names
        ctx.add("Attachments: " + ", ".join(names), anchor=ANCHOR_HARD, role="meta",
                locator={"part": "attachments"})


def _paragraphs(text: str) -> list[str]:
    out: list[str] = []
    current: list[str] = []
    for line in text.split("\n"):
        if line.strip():
            current.append(line)
        elif current:
            out.append("\n".join(current))
            current = []
    if current:
        out.append("\n".join(current))
    return out
