"""Doc drift pins for the search-tool rename.

Two tools swapped names: ``documentation_search`` was Cremind's own manual and
is now the user's own documents (it was ``user_documents``); the manual is
``cremind_documentation_search``. Every bundled doc is embedded for
``cremind_documentation_search`` and read by the agent, so a doc that still
used an id in its old meaning would teach the agent to configure the wrong
tool — silently, since both ids exist. These tests pin the docs to the new
meaning and pin the explanations a user (or the agent) needs after upgrading:
what each id means now, what it meant before, the retired REST/CLI names,
and the per-conversation Search tools selector.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BUNDLED = Path(__file__).resolve().parents[2] / "app" / "cremind_documents" / "bundled"

DOCUMENTATION_SEARCH_DOC = "[tool]documentation search"
CREMIND_DOCS_DOC = "[tool]cremind documentation search"
PRIORITY = "Documentation search → Cremind documentation search → Memory search → Web search"


def _text(stem: str) -> str:
    path = BUNDLED / f"{stem}.md"
    assert path.exists(), f"missing bundled doc: {path.name}"
    return path.read_text(encoding="utf-8")


def _description(stem: str) -> str:
    return _text(stem).split("---")[1]


def _flat(text: str) -> str:
    """Line-wrapped prose as one line, so a phrase split by a wrap still matches."""
    return re.sub(r"\s+", " ", text)


ALL_DOCS = sorted(p.stem for p in BUNDLED.glob("*.md"))


def test_the_personal_document_tool_doc_explains_itself_and_the_rename():
    text = _flat(_text(DOCUMENTATION_SEARCH_DOC))
    for needle in (
        "`tool_id` `documentation_search`",
        "documentation_search__find_files", "documentation_search__search",
        "documentation_search__read", "documentation_search__research",
        "`user_documents`", "`user_documents__search` is now `documentation_search__search`",
        "`[ud:…]`", "[doc:",
        "never touches Cremind's own manual — that is `cremind_documentation_search`",
    ):
        assert needle in text, f"{DOCUMENTATION_SEARCH_DOC}.md never says {needle!r}"
    description = _description(DOCUMENTATION_SEARCH_DOC)
    assert "user's OWN files" in description
    assert "Formerly named user_documents" in description
    assert "Not Cremind's own documentation (that is cremind_documentation_search)" in description


def test_the_personal_document_tool_doc_describes_the_search_tools_selector():
    """Per-conversation selection: where it is, what it does and does not do,
    the priority order, the cache warning and the CLI."""
    text = _flat(_text(DOCUMENTATION_SEARCH_DOC))
    section = text[text.index("## Search tools per conversation"):]
    section = section[:section.index("## ", 3)]
    for needle in (
        "**Search tools**", PRIORITY, "**next response**", "prompt-cache",
        "never enables a tool turned off in Settings", "starts indexing", "`allow_in`",
        "`cremind conv search-tools`", "`cremind group search-tools`",
    ):
        assert needle in section, f"the Search tools section never says {needle!r}"
    assert "Search tools selector" in _description(DOCUMENTATION_SEARCH_DOC)


def test_the_manual_tool_doc_explains_the_rename_and_the_selector():
    text = _flat(_text(CREMIND_DOCS_DOC))
    for needle in (
        "`tool_id` `cremind_documentation_search`",
        "Before the rename this tool's id was `documentation_search`",
        "`documentation_search__search_documentation`",
        "`documentation_search__read_documentation_section`",
        "`cremind_documentation_search__search_documentation`",
        "`cremind_documentation_search__read_documentation_section`",
        "It is not **Documentation search** (`documentation_search`)",
        "cremind tools set-var cremind_documentation_search DEFAULT_TOP_K=20",
        "cremind tools set-leaf cremind_documentation_search read_documentation_section=false",
        "**Search tools**", PRIORITY,
    ):
        assert needle in text, f"{CREMIND_DOCS_DOC}.md never says {needle!r}"
    description = _description(CREMIND_DOCS_DOC)
    assert "Cremind's own manuals" in description
    assert "Named documentation_search before the rename" in description


def test_the_function_names_in_the_docs_match_the_alias_table():
    """The docs name each old function and its replacement; the dispatcher's
    alias table is what makes that true for replayed history."""
    from app.agent.search_tools import HISTORICAL_FUNCTION_ALIASES

    manual = _flat(_text(CREMIND_DOCS_DOC))
    personal = _flat(_text(DOCUMENTATION_SEARCH_DOC))
    for old, (tool_id, function) in HISTORICAL_FUNCTION_ALIASES.items():
        new = f"{tool_id}__{function}"
        doc = manual if tool_id == "cremind_documentation_search" else personal
        assert new in doc, f"the docs never name {new}"
        if old.startswith("documentation_search__"):
            assert old in manual, f"{CREMIND_DOCS_DOC}.md never names the old {old}"


def test_the_tools_cli_doc_lists_both_search_tools_and_the_swap():
    text = _flat(_text("[cli]cremind tools"))
    assert "| Cremind Documentation Search | `cremind_documentation_search` |" in text
    assert "| Documentation Search | `documentation_search` |" in text
    swap = text[text.index("### Two search tools swapped ids"):]
    assert "| `documentation_search` | `cremind_documentation_search` | Cremind's own manuals |" in swap
    assert "| `user_documents` | `documentation_search` | the user's own indexed files |" in swap
    assert "`426 ClientUpgradeRequired`" in text and "pip install -U cremind" in text
    assert "`cremind conv search-tools`" in text


def test_the_clean_doc_names_both_components_and_matches_the_cli():
    from app.cli.commands.clean import _COMPONENTS

    text = _text("[cli]cremind clean")
    for _component, flag in _COMPONENTS:
        assert f"`{flag}`" in text, f"clean doc never documents {flag}"
    assert "`cremind_documents`" in text and "`documentation_search`" in text
    assert "426 ClientUpgradeRequired" in text
    assert "the document index" not in text


def test_embedding_names_every_feature_it_backs():
    text = _flat(_text("[cli]cremind embedding"))
    for needle in ("`cremind_documentation_search`", "`documentation_search`", "lexical_only",
                   "suspended(embedding_off)", "memory"):
        assert needle in text, f"embedding doc never says {needle!r}"


def test_the_authoring_guide_separates_the_manual_from_the_users_files():
    text = _flat(_text("document"))
    assert "retrieval in `cremind_documentation_search`" in _description("document")
    assert "not about indexing the user's own files" in _description("document").lower()
    assert "**Documentation search** (the `documentation_search` tool, `cremind docs`)" in text
    assert "an Cremind" not in text


@pytest.mark.parametrize("stem", ALL_DOCS)
def test_no_doc_sends_anyone_to_a_retired_name_without_saying_so(stem):
    """Old names may appear only where a doc explains the rename (within a
    few lines: a wrapped sentence, or a before/after table's header)."""
    lines = _text(stem).splitlines()
    for number, line in enumerate(lines):
        for retired in ("/api/userdocs", "cremind userdocs", "--json documents", "cremind documents",
                        "user_documents__", "`user_documents`", "[cli]cremind documents"):
            if retired not in line:
                continue
            context = " ".join(lines[max(0, number - 3):number + 2]).lower()
            assert re.search(r"renam|formerly|before the|was `|now `|pre-release|no such command", context), (
                f"{stem}.md:{number + 1} uses the retired {retired!r} without explaining the rename"
            )


@pytest.mark.parametrize("stem", ALL_DOCS)
def test_no_doc_configures_the_manual_under_its_old_id(stem):
    """``read_documentation_section`` / ``search_documentation`` are the
    manual's functions; pairing them with the bare ``documentation_search`` id
    in a command is the old meaning."""
    text = _text(stem)
    bad = re.findall(r"cremind tools (?:set-leaf|leaves) documentation_search [^\n`]*_documentation", text)
    assert not bad, f"{stem}.md configures the manual under the old id: {bad}"
