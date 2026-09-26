"""What a Documentation Search result actually put in front of the model.

A search or read result is sized to a token budget, so what it *could* show
and what it *did* show differ: context is dropped, snippets are shortened, a
long passage is cut, a neighbouring passage waits for the next page. Anything
that reasons about the answer's evidence — the agent's automatic source
review, the ``delivery`` object of the REST query API, the per-turn
diagnostics — needs the second, and it must come from the final rendered
text, never from the structured result the renderer started with (that one
lists passages the budget then left out).

:class:`DocumentEvidence` is that record. The renderer builds it from the
pieces it actually printed; the Documentation Search tool hands it, as a
typed object, through the built-in tool path to the agent (never as text the
model or a document could write). It names files and passages by their
citation tokens only — no names, paths or text — so it can travel outside
the untrusted-content block the text itself lives in.

A passage is ``complete`` when its whole text was printed in this result, and
partial otherwise (a search snippet that was shortened, a passage cut to fit).
"Delivered" is about passages: a file with one passage delivered was not read
in full, and a file whose passage was delivered may still add nothing to the
answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

EVIDENCE_VERSION = 1

OP_SEARCH = "search"
OP_READ = "read"

# Why a passage is in the result.
ROLE_MATCH = "match"        # a passage the search matched
ROLE_CONTEXT = "context"    # a neighbouring passage a search showed for context
ROLE_BODY = "body"          # a passage of a read

# Where the passage a read was centred on stands in this result.
FOCUS_COMPLETE = "complete"
FOCUS_PARTIAL = "partial"          # only part of it fit; the rest is on a later page
FOCUS_OMITTED = "omitted"          # this page shows other parts of the read
FOCUS_UNRESOLVED = "unresolved"    # the token names a passage the file no longer has


@dataclass(frozen=True)
class PassageDelivery:
    """One passage printed in a result."""

    token: str                 # "[doc:<file>#<chunk>]"
    fid: str                   # the file's (or folder's) cite id
    role: str                  # match | context | body
    complete: bool             # its whole text was printed here
    # Real text (a body, OCR or caption passage) — not a file or folder card.
    substantive: bool = True
    # Search only: the 1-based rank of the result it belongs to, and, for a
    # match, the retrieval confidence (high | medium | low). A heuristic about
    # how well the passage matched the query, not about whether it is right.
    order: int = 0
    confidence: str | None = None
    # Where it sits, by numbers only ("p. 49", "lines 40–58"): never the
    # document's own words, so it may be quoted outside the data block.
    position: str = ""


@dataclass(frozen=True)
class SourceDelivery:
    """One file a search result listed, in result order."""

    fid: str
    token: str                 # "[doc:<file>]"
    order: int                 # 1-based rank, counting earlier pages


@dataclass
class DocumentEvidence:
    """What one search or read result delivered."""

    op: str                                    # search | read
    passages: list[PassageDelivery] = field(default_factory=list)
    sources: list[SourceDelivery] = field(default_factory=list)
    # Tokens of the final text, counted like the budget it was fitted to.
    rendered_tokens: int = 0
    # Something the result could have shown was left out to fit the budget.
    truncated: bool = False
    # How to get what was left out, e.g. {"page": 2}; None when nothing waits.
    continuation: dict[str, Any] | None = None
    # read only
    fid: str | None = None
    focus: str | None = None                   # the passage the read was centred on
    focus_status: str | None = None            # complete | partial | omitted | unresolved
    stale: bool = False                        # the file changed since it was indexed
    metadata_only: bool = False                # no text indexed: only the file's details
    shape: str | None = None                   # whole | focus | part | envelope
    # A code (NotFound, SectionNotFound, …) when the leaf could not answer.
    error: str | None = None
    version: int = EVIDENCE_VERSION

    def complete_tokens(self) -> set[str]:
        return {p.token for p in self.passages if p.complete}

    def public(self) -> dict[str, Any]:
        """The ``delivery`` object of a REST query response: which passage
        tokens are visible and whether each is whole, the focus, truncation,
        continuation and the rendered size. Tokens only — no document text."""
        out: dict[str, Any] = {
            "v": self.version,
            "rendered_tokens": self.rendered_tokens,
            "truncated": self.truncated,
            "continuation": self.continuation,
            "passages": [
                {"token": p.token, "role": p.role, "complete": p.complete}
                for p in self.passages
            ],
        }
        if self.op == OP_READ:
            out["focus"] = (
                {"token": self.focus, "status": self.focus_status} if self.focus else None
            )
            out["stale"] = self.stale
            out["metadata_only"] = self.metadata_only
        return out


def error_evidence(op: str, code: str, *, fid: str | None = None, focus: str | None = None) -> DocumentEvidence:
    """The record of a leaf that answered with an error: nothing delivered."""
    return DocumentEvidence(op=op, error=code or "Error", fid=fid, focus=focus)


__all__ = [
    "DocumentEvidence",
    "EVIDENCE_VERSION",
    "FOCUS_COMPLETE",
    "FOCUS_OMITTED",
    "FOCUS_PARTIAL",
    "FOCUS_UNRESOLVED",
    "OP_READ",
    "OP_SEARCH",
    "PassageDelivery",
    "ROLE_BODY",
    "ROLE_CONTEXT",
    "ROLE_MATCH",
    "SourceDelivery",
    "error_evidence",
]
