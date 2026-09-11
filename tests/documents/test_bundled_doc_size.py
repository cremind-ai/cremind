"""Bundled doc bodies stay within the delivery budget — or are known to be long.

The reasoning agent cuts every tool result to ``tool_result.max_tokens`` (4000
tokens by default). A `cremind` CLI reference also arrives with the execution
directive in front of it, so its body gets about 300 tokens less. A body over
its budget is still usable — documentation_search then delivers its head, a
table of contents and the matching sections, and the agent reads further
sections with ``read_documentation_section`` — but it costs the agent extra
round trips, so new docs should fit (see "Body conventions" in document.md).

Measured exactly as the tool measures it: tokens, with the real directive, not
a byte proxy (a byte threshold once let a CLI doc that is enveloped on every
search pass as "fitting").

The documents that were already long when section delivery shipped are listed
below. This is a ratchet: a new long document fails until it is split or
consciously added, and a listed document that has been trimmed under the budget
fails until it is removed from the list, so the list can only shrink.

Section structure (at least one ``##``, unique headings, no ``###`` too big to
deliver) is pinned for every doc in test_sections.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.documents.sections import open_fence_at_end

pytest.importorskip("tiktoken", reason="the delivery budget is measured in tiktoken tokens")

import app.tools.builtin.documentation_search as ds  # noqa: E402
from app.utils.common import count_content_tokens  # noqa: E402

BUNDLED = Path(__file__).resolve().parents[2] / "app" / "documents" / "bundled"

#: The default ``tool_result.max_tokens`` (app/config/settings.toml).
DEFAULT_CLAMP_TOKENS = 4000

#: Bodies over their budget when section delivery shipped. Shrink, never grow.
OVERSIZED_ALLOWLIST = frozenset({
    "[cli]cremind channels",
    "[cli]cremind tools",
    "[cli]cremind llm",
    "[cli]cremind profile",
    "[cli]cremind conv",
    "[cli]cremind group",
    "[cli]cremind config",
    "[cli]cremind file-watchers",
    "[tool]claude code",
    "[cli]cremind skill-events",
    "[cli]cremind server",
    "[cli]cremind tls",
    "[tool]codex",
    "[cli]cremind proc",
})


def _budget(stem: str) -> int:
    """The body budget documentation_search gives this doc at the default clamp."""
    reserved = (
        ds._CLI_EXECUTION_DIRECTIVE.format(fn="exec_shell") + "\n"
        if stem.startswith("[cli]") else ""
    )
    return DEFAULT_CLAMP_TOKENS - count_content_tokens(reserved) - ds._BUDGET_MARGIN_TOKENS

_FRONTMATTER = re.compile(r"^﻿?\s*---\r?\n.*?\r?\n---\r?\n?", re.DOTALL)


def _body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(text)
    assert match, f"{path.name}: no frontmatter"
    return text[match.end():]


DOCS = sorted(BUNDLED.glob("*.md"))


def _size(path: Path) -> int:
    return count_content_tokens(_body(path))


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_a_new_document_fits_the_delivery_budget(path: Path):
    if path.stem in OVERSIZED_ALLOWLIST:
        return
    size, budget = _size(path), _budget(path.stem)
    assert size <= budget, (
        f"{path.name} body is {size} tokens, over its {budget}-token delivery "
        "budget. Split it into smaller documents, or — if it genuinely belongs "
        "together — make every ##/### section self-contained and add it to "
        "OVERSIZED_ALLOWLIST."
    )


@pytest.mark.parametrize("stem", sorted(OVERSIZED_ALLOWLIST))
def test_the_allowlist_only_holds_documents_that_are_still_long(stem: str):
    path = BUNDLED / f"{stem}.md"
    assert path.exists(), f"{stem} no longer exists — remove it from OVERSIZED_ALLOWLIST"
    assert _size(path) > _budget(stem), (
        f"{stem} now fits the budget — remove it from OVERSIZED_ALLOWLIST"
    )


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_every_code_fence_is_closed(path: Path):
    """An unclosed fence swallows the rest of the document: every heading after
    it becomes code, so those sections vanish from the table of contents and
    cannot be read. Tracked exactly as the section splitter tracks fences."""
    assert open_fence_at_end(_body(path).splitlines()) is None, (
        f"{path.name} has an unclosed code fence"
    )
