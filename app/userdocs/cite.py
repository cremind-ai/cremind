"""Citation tokens: the one format every part of the answer path agrees on.

The agent cites evidence from the user's documents by copying tokens that the
User Documents tools print next to every result::

    [ud:k7m2xq9a]            a file or folder (its 8-character cite id)
    [ud:k7m2xq9a#3f9c2e1b]   one chunk of it (first 8 hex of the chunk hash)

Compact (about a dozen LLM tokens), stable while *other* parts of the file are
edited (the chunk part is content-addressed), and meaningless outside the
profile that issued it. Tools register every token they emit
(:mod:`app.userdocs.citations`); when the answer is saved, each token in it is
checked against that registry, so an invented or tampered token is flagged
instead of rendered as a trustworthy source.

This module is dependency-free on purpose: the web UI, the channel renderer
and the CLI all parse the same grammar, and the CLI must not import the server.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

# Crockford base32, lowercase: 0-9 a-z without i, l, o, u.
CITE_ALPHABET_RE = "[0-9a-hjkmnp-tv-z]"

TOKEN_RE = re.compile(rf"\[ud:({CITE_ALPHABET_RE}{{8}})(?:#([0-9a-f]{{8}}))?\]")

# What models actually write when they copy a token imperfectly: full-width
# brackets, a space after the colon, upper case, or several tokens in one
# bracket ("[ud:a#b; ud:c#d]"). Parsed leniently, *verified* strictly.
# Case-insensitivity comes from spelling both cases out, NOT from IGNORECASE:
# with IGNORECASE, Python's [A-Za-z] also matches "ſ" (long s), "K" (Kelvin
# sign), "İ" and "ı" — the TypeScript twin in ui/src/utils/citations.ts does
# not, and the two must number citations identically.
_TOLERANT_RE = re.compile(
    r"[\[【]\s*((?:[uU][dD]:\s*[0-9A-Za-z]{8}(?:#[0-9A-Fa-f]{8})?\s*[;,]?\s*)+)[\]】]"
)
_ONE_RE = re.compile(r"[uU][dD]:\s*([0-9A-Za-z]{8})(?:#([0-9A-Fa-f]{8}))?")


def make_token(cite_id: str, text_hash: str | None = None) -> str:
    """``[ud:<cite_id>]`` or ``[ud:<cite_id>#<first 8 of text_hash>]``."""
    if text_hash:
        return f"[ud:{cite_id}#{text_hash[:8]}]"
    return f"[ud:{cite_id}]"


def normalize_token(cite_id: str, c8: str | None) -> str:
    return make_token(cite_id.lower(), c8.lower() if c8 else None)


def parse_tokens(text: str) -> list[dict[str, Any]]:
    """Every citation in ``text``, in order of appearance, tolerant of the
    usual copying mistakes. Returns ``[{token, cite_id, c8, start, end}]``
    where ``token`` is the canonical form (what the registry stores)."""
    out: list[dict[str, Any]] = []
    if not text or "ud:" not in text.lower():
        return out
    for m in _TOLERANT_RE.finditer(text):
        for one in _ONE_RE.finditer(m.group(1)):
            cite_id, c8 = one.group(1).lower(), (one.group(2) or "").lower() or None
            out.append({
                "token": normalize_token(cite_id, c8),
                "cite_id": cite_id,
                "c8": c8,
                "start": m.start(),
                "end": m.end(),
            })
    return out


def number_tokens(text: str) -> dict[str, int]:
    """Distinct canonical tokens numbered 1, 2, 3… by first appearance — the
    numbering every renderer (web, channels, CLI) must share."""
    numbers: dict[str, int] = {}
    for item in parse_tokens(text):
        numbers.setdefault(item["token"], len(numbers) + 1)
    return numbers


def escape_in_document_text(text: str) -> str:
    """Neutralise anything that looks like a citation inside document text,
    so a document cannot plant a token in the agent's answer. The full-width
    colon keeps it readable to a person and invisible to the parser."""
    return re.sub(r"\[(\s*)ud:", "[\\1ud：", text, flags=re.IGNORECASE) if text else text


def locator_label(locator: dict[str, Any] | None) -> str:
    """A short human label for where a chunk sits: "p. 12–13", "lines 40–58",
    "sheet 'Q3' A2:F41", "slide 5", "Điều 12, khoản 2"."""
    loc = locator or {}
    parts: list[str] = []
    if loc.get("article"):
        art = f"Article {loc['article']}" if not _looks_vietnamese(loc) else f"Điều {loc['article']}"
        if loc.get("clause"):
            art += f", {'clause' if not _looks_vietnamese(loc) else 'khoản'} {loc['clause']}"
        parts.append(art)
    if loc.get("page"):
        end = loc.get("page_end")
        parts.append(f"p. {loc['page']}" + (f"–{end}" if end and end != loc["page"] else ""))
    if loc.get("sheet"):
        parts.append(f"sheet '{loc['sheet']}'" + (f" {loc['range']}" if loc.get("range") else ""))
    if loc.get("slide"):
        parts.append(f"slide {loc['slide']}")
    if loc.get("rows") and isinstance(loc["rows"], (list, tuple)) and len(loc["rows"]) == 2:
        parts.append(f"rows {loc['rows'][0]}–{loc['rows'][1]}")
    if not loc.get("page") and loc.get("line_start"):
        end = loc.get("line_end")
        parts.append(f"lines {loc['line_start']}" + (f"–{end}" if end and end != loc["line_start"] else ""))
    if not parts and loc.get("heading"):
        heading = loc["heading"]
        if isinstance(heading, list) and heading:
            parts.append(" › ".join(str(h) for h in heading[-2:]))
    return ", ".join(parts)


def _looks_vietnamese(loc: dict[str, Any]) -> bool:
    heading = loc.get("heading") or []
    joined = " ".join(str(h) for h in heading) if isinstance(heading, list) else str(heading)
    return any(w in joined for w in ("Điều", "Chương", "Khoản", "Mục"))


@dataclass
class IssuedCitation:
    """One token a tool printed, as the citation registry records it."""

    token: str
    cite_id: str
    # "file" | "folder"
    target: str = "file"
    # The index-DB row id of the file or folder (not exposed to clients).
    ref_id: int | None = None
    text_hash: str | None = None
    # "local" | "drive"
    source_kind: str = "local"
    locator: dict[str, Any] = field(default_factory=dict)
    label: str = ""
    rel_path: str = ""
    snippet: str = ""
    # The tool leaf that printed it (find_files | search | read | research).
    leaf: str = ""
    web_link: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
