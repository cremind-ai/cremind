"""Turning a model's claimed evidence into checked :class:`Evidence`.

A research model answers with ``{"token": "[ud:…#…]", "quote": "…"}`` pairs.
Each pair is accepted only if

1. the token names a passage the model was actually shown in *this* call
   (the allowed map of the window or provisions it was given) — tokens are
   normalised first, so ``[UD: K7M2XQ9A#1F3C…]`` still counts, but a token
   from elsewhere never does; and
2. the quote occurs in that passage (or straddles into a neighbour), as
   :func:`app.userdocs.quotes.verify_quote` judges it — exact, normalised
   typography, or a close fuzzy match with identical digits and no
   diacritics-only difference.

The accepted evidence carries the SOURCE's wording (``canonical``), never
the model's, so a paraphrase inside quotation marks cannot reach the user.
Everything else is dropped and counted as rejected; a finding left with no
evidence is dropped by the pipeline.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from app.userdocs.cite import normalize_token, parse_tokens
from app.userdocs.quotes import MISMATCH, verify_quote
from app.userdocs.research.types import Evidence
from app.userdocs.research.windows import ChunkRef

# A quote this short proves little; it must then be an exact substring.
MIN_QUOTE_CHARS = 12
MAX_QUOTE_CHARS = 1200

# The JSON-schema fragment pipelines put in their function definitions.
EVIDENCE_SCHEMA: dict[str, Any] = {
    "type": "array",
    "description": (
        "Where this comes from: the [ud:…] token printed before the passage, copied exactly, and "
        "a short verbatim quote from that passage (copy the words exactly; do not translate, "
        "fix or paraphrase)."
    ),
    "items": {
        "type": "object",
        "properties": {
            "token": {"type": "string"},
            "quote": {"type": "string"},
        },
        "required": ["token", "quote"],
        "additionalProperties": False,
    },
}


def canonical_token(raw: Any) -> str | None:
    """``raw`` in its canonical ``[ud:<fid>#<c8>]`` form, or None."""
    text = str(raw or "").strip()
    if not text:
        return None
    if not text.startswith("["):
        text = f"[{text}]"
    toks = parse_tokens(text)
    if not toks:
        return None
    return normalize_token(toks[0]["cite_id"], toks[0].get("c8"))


def check_one(token: Any, quote: Any, allowed: Mapping[str, ChunkRef]) -> Evidence | None:
    tok = canonical_token(token)
    if tok is None or tok not in allowed:
        return None
    ref = allowed[tok]
    q = str(quote or "").strip()[:MAX_QUOTE_CHARS]
    if not q:
        return None
    if len(q) < MIN_QUOTE_CHARS:
        if q not in ref.text:
            return None
        return Evidence(token=tok, quote=q, quote_status="exact", label=ref.label,
                        rel_path=ref.file.get("rel_path") or "")
    th = ref.chunk.get("text_hash")
    joined = " ".join(x for x in (ref.prev_text, ref.text, ref.next_text) if x)
    check = verify_quote(q, [(th, ref.text), (th, joined)])
    if check.status == MISMATCH or not check.canonical:
        # Includes a fuzzy near-miss that swapped, added or dropped a word
        # that matters ("do UBND giải quyết" for "do Tòa án giải quyết").
        return None
    return Evidence(token=tok, quote=check.canonical, quote_status=check.status, label=ref.label,
                    rel_path=ref.file.get("rel_path") or "")


def check_evidence(
    raw: Iterable[Any] | None, allowed: Mapping[str, ChunkRef], *, limit: int = 6,
) -> tuple[list[Evidence], int]:
    """Check each claimed ``{"token", "quote"}``; returns ``(accepted,
    rejected count)``. At most ``limit`` accepted per claim; duplicates (same
    token and quote) count once."""
    accepted: list[Evidence] = []
    rejected = 0
    seen: set[tuple[str, str]] = set()
    for item in raw or []:
        if not isinstance(item, dict):
            rejected += 1
            continue
        ev = check_one(item.get("token"), item.get("quote"), allowed)
        if ev is None:
            rejected += 1
            continue
        key = (ev.token, ev.quote)
        if key in seen:
            continue
        seen.add(key)
        if len(accepted) < limit:
            accepted.append(ev)
    return accepted, rejected


__all__ = ["EVIDENCE_SCHEMA", "MIN_QUOTE_CHARS", "canonical_token", "check_evidence", "check_one"]
