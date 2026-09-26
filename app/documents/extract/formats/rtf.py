"""RTF: a control-word stripper, one block per paragraph.

RTF is a token stream: ``{``/``}`` groups, ``\\word[N]`` control words,
``\\'hh`` bytes in the document's code page, ``\\uN`` Unicode escapes
followed by ``\\ucN`` fallback characters to skip, and plain text runs. The
reader keeps a per-group state (destination, fallback count, font code page),
drops destinations that hold no body text (font and colour tables, styles,
pictures, field instructions, headers, footers and footnotes, and every
``{\\*...}`` group it does not know), and reads the ``\\info`` group for
title, author and dates.

Bytes are decoded with the code page in force: the font's ``\\fcharset``
when it names one (Vietnamese RTF often sets 163, code page 1258), else the
document's ``\\ansicpg``. Consecutive ``\\'hh`` bytes are decoded together so
double-byte code pages (932, 936, 949, 950) come out whole.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ._base import Ctx, LegacyFormatError

_TOKEN_RE = re.compile(
    rb"\\([a-zA-Z]{1,32})(-?\d{1,10})? ?"   # control word with optional argument
    rb"|\\'([0-9a-fA-F]{2})"               # hex byte
    rb"|\\(.)"                             # control symbol
    rb"|([{}])"                            # group
    rb"|([^\\{}\r\n]+)"                    # text run
    rb"|[\r\n]+",                          # raw line ends are not text
    re.S,
)

_PARA = "\x1d"  # internal paragraph marker, never produced by decoding

_SKIP_DESTINATIONS = frozenset({
    "colortbl", "stylesheet", "listtable", "listoverridetable", "rsidtbl", "generator",
    "xmlnstbl", "mmathPr", "themedata", "colorschememapping", "datastore", "latentstyles",
    "pict", "shppict", "nonshppict", "object", "objdata", "objclass", "blipuid", "fldinst",
    "header", "headerl", "headerr", "headerf", "footer", "footerl", "footerr", "footerf",
    "footnote", "annotation", "atnid", "atnauthor", "bkmkstart", "bkmkend", "filetbl",
    "revtbl", "pgdsctbl", "userprops", "docvar", "template", "private", "protusertbl",
    "wgrffmtfilter", "fchars", "lchars", "pnseclvl", "falt", "panose", "fontemb",
    "fontfile", "passwordhash", "xe", "tc", "field_inst", "ftnsep", "ftnsepc", "ftncn",
    "aftnsep", "aftnsepc", "aftncn",
})

# \fcharset → Windows code page (2 is the Symbol font: no text worth decoding).
_CHARSET_CODEPAGE = {
    0: 1252, 77: 10000, 128: 932, 129: 949, 130: 1361, 134: 936, 136: 950,
    161: 1253, 162: 1254, 163: 1258, 177: 1255, 178: 1256, 186: 1257,
    204: 1251, 222: 874, 238: 1250, 254: 437,
}

_WORD_TEXT = {
    "par": _PARA, "sect": _PARA, "page": _PARA, "row": _PARA, "line": "\n",
    "tab": "\t", "cell": "\t", "emdash": "\u2014", "endash": "\u2013",
    "bullet": "\u2022", "lquote": "\u2018", "rquote": "\u2019",
    "ldblquote": "\u201c", "rdblquote": "\u201d", "emspace": " ", "enspace": " ",
    "qmspace": " ", "zwj": "\u200d", "zwnj": "\u200c", "zwbo": "", "zwnbo": "",
}
_SYMBOL_TEXT = {b"~": "\u00a0", b"-": "", b"_": "\u2011", b"{": "{", b"}": "}",
                b"\\": "\\", b"\n": _PARA, b"\r": _PARA, b"\t": "\t"}

_INFO_TEXT = {"title": "title", "author": "author", "operator": "last_modified_by",
              "subject": "subject", "keywords": "keywords"}
_INFO_TIME = {"creatim": "created", "revtim": "modified"}


def _codec(codepage: int | None) -> str:
    if codepage == 65001:
        return "utf-8"
    return f"cp{codepage or 1252}"


def rtf_to_text(data: bytes) -> tuple[list[str], dict[str, Any]]:
    """Paragraphs of body text and ``doc_meta`` from the ``\\info`` group."""
    state: dict[str, Any] = {"dest": "body", "uc": 1, "cp": None}
    stack: list[dict[str, Any]] = []
    body: list[str] = []
    meta_parts: dict[str, list[str]] = {}
    times: dict[str, dict[str, int]] = {}
    fonts: dict[int, int | None] = {}
    doc_cp = 1252
    font_being_defined: int | None = None
    pending = bytearray()
    skip = 0
    group_start = False

    def emit(text: str) -> None:
        dest = state["dest"]
        if dest == "body":
            body.append(text)
        elif dest.startswith("meta:"):
            meta_parts.setdefault(dest[5:], []).append(text)

    def flush() -> None:
        if pending:
            if state["dest"] == "body" or state["dest"].startswith("meta:"):
                codec = _codec(state["cp"] or doc_cp)
                try:
                    emit(bytes(pending).decode(codec, "replace"))
                except LookupError:
                    emit(bytes(pending).decode("cp1252", "replace"))
            pending.clear()

    pos, end = 0, len(data)
    while pos < end:
        m = _TOKEN_RE.match(data, pos)
        if m is None:
            pos += 1  # a lone backslash at the very end
            continue
        pos = m.end()
        word, arg, hexbyte, symbol, brace, run = m.groups()

        if hexbyte is not None:
            if skip:
                skip -= 1
            else:
                pending.append(int(hexbyte, 16))
            continue
        if run is not None:
            if skip:
                dropped = min(skip, len(run))
                skip -= dropped
                run = run[dropped:]
            pending.extend(run)
            continue

        flush()
        if brace is not None:
            skip = 0
            if brace == b"{":
                stack.append(dict(state))
                group_start = True
            else:
                if stack:
                    state = stack.pop()
                group_start = False
            continue
        if word is None and symbol is None:
            continue  # raw CR/LF between tokens

        skip = 0
        if symbol is not None:
            if symbol == b"*":
                if group_start:
                    state["dest"] = "skip"  # {\* ...}: an optional destination we don't read
                continue
            group_start = False
            if state["dest"] != "skip" and symbol in _SYMBOL_TEXT:
                emit(_SYMBOL_TEXT[symbol])
            continue

        name = word.decode("ascii")
        value = int(arg) if arg is not None else None
        starting = group_start
        group_start = False

        if name == "bin":
            pos += max(value or 0, 0)  # raw binary payload: never text
            continue
        if state["dest"] == "skip":
            continue
        if starting:
            if name == "fonttbl":
                state["dest"] = "fonttbl"
            elif name == "info":
                state["dest"] = "info"
            elif name in _SKIP_DESTINATIONS:
                state["dest"] = "skip"
                continue
            elif state["dest"] == "info" and name in _INFO_TEXT:
                state["dest"] = "meta:" + _INFO_TEXT[name]
                continue
            elif state["dest"] == "info" and name in _INFO_TIME:
                state["dest"] = "time:" + _INFO_TIME[name]
                times.setdefault(_INFO_TIME[name], {})
                continue

        dest = state["dest"]
        if name == "ansicpg" and value:
            doc_cp = value
        elif name in ("mac", "pc", "pca"):
            doc_cp = {"mac": 10000, "pc": 437, "pca": 850}[name]
        elif name == "f" and value is not None:
            if dest == "fonttbl":
                font_being_defined = value
            else:
                state["cp"] = fonts.get(value)
        elif name == "fcharset" and dest == "fonttbl" and font_being_defined is not None:
            fonts[font_being_defined] = _CHARSET_CODEPAGE.get(value or 0)
        elif name == "uc" and value is not None:
            state["uc"] = max(value, 0)
        elif name == "u" and value is not None:
            emit(chr(value + 65536 if value < 0 else value))
            skip = state["uc"]
        elif dest.startswith("time:") and name in ("yr", "mo", "dy", "hr", "min") and value is not None:
            times[dest[5:]][name] = value
        elif name in _WORD_TEXT and dest != "fonttbl":
            emit(_WORD_TEXT[name])
    flush()

    meta: dict[str, Any] = {}
    for key, parts in meta_parts.items():
        text = "".join(parts).strip()
        if text:
            meta[key] = text
    for key, parts_t in times.items():
        try:
            meta[key] = datetime(parts_t["yr"], parts_t.get("mo", 1), parts_t.get("dy", 1),
                                 parts_t.get("hr", 0), parts_t.get("min", 0)).isoformat()
        except (KeyError, ValueError):
            pass
    paragraphs = [p.strip() for p in "".join(body).split(_PARA)]
    return [p for p in paragraphs if p], meta


def extract_rtf(ctx: Ctx) -> None:
    data, _ = ctx.read_bytes()
    try:
        paragraphs, meta = rtf_to_text(data)
    except (ValueError, IndexError, RecursionError) as exc:
        raise LegacyFormatError(f"unreadable RTF: {exc}") from exc
    ctx.result.doc_meta.update(meta)
    for number, text in enumerate(paragraphs, start=1):
        ctx.add(text, locator={"para": [number, number]})
