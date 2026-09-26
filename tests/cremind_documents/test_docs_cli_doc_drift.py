"""Doc/code drift pin for `[cli]cremind docs.md`,
`[cli]cremind docs search.md`, `[cli]cremind docs research.md` and
`[cli]cremind docs drive.md`.

CLAUDE.md mandates that a CLI command and its bundled doc move in lockstep.
This walks the nested Typer groups (`docs`, `docs excludes`,
`docs admin`, `docs research`, `docs drive`, `docs drive
folders`) so a new subcommand or flag cannot land undocumented. The query
subcommands (`search`, `find`, `read`, `cite`), the research jobs
(`research …`) and Google Drive indexing (`drive …`) live in their own docs —
one reference per question a user asks ("set it up" vs "search my files" vs
"research this folder" vs "index my Drive"), each small enough to be
delivered whole.

The ``description`` is the only text embedded into ``cremind_documentation_search``,
so it must carry what users actually ask ("search my files", "index my
documents") — and must say it is NOT Cremind's own documentation, or the two
search features get confused.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

pytest.importorskip("typer")

BUNDLED = Path(__file__).resolve().parents[2] / "app" / "cremind_documents" / "bundled"
DOC = BUNDLED / "[cli]cremind docs.md"
SEARCH_DOC = BUNDLED / "[cli]cremind docs search.md"
RESEARCH_DOC = BUNDLED / "[cli]cremind docs research.md"
DRIVE_DOC = BUNDLED / "[cli]cremind docs drive.md"

# Documented in SEARCH_DOC instead of DOC.
SEARCH_COMMANDS = frozenset({"search", "find", "read", "cite"})
# Every command of this group is documented in RESEARCH_DOC.
RESEARCH_GROUP = "research"
RESEARCH_COMMANDS = frozenset({"run", "status", "continue", "cancel", "list"})
# Every command of this group (and its `folders` subgroup) is in DRIVE_DOC.
DRIVE_GROUP = "drive"
DRIVE_COMMANDS = frozenset({"status", "enable", "disable", "sync", "folders list", "folders set"})
ALL_DOCS = [DOC, SEARCH_DOC, RESEARCH_DOC, DRIVE_DOC]


def _doc_text(doc: Path = DOC) -> str:
    assert doc.exists(), f"missing bundled doc: {doc.name}"
    return doc.read_text(encoding="utf-8")


def _description(doc: Path = DOC) -> str:
    return _doc_text(doc).split("---")[1]


def _walk(app, prefix: str):
    """Yield (full command path, callback) for every command, depth-first."""
    for command in app.registered_commands:
        name = command.name or command.callback.__name__
        yield f"{prefix} {name}", command.callback
    for group in app.registered_groups:
        yield from _walk(group.typer_instance, f"{prefix} {group.name}")


@pytest.mark.parametrize("doc", ALL_DOCS, ids=lambda d: d.name)
def test_frontmatter_is_well_formed(doc):
    lines = _doc_text(doc).splitlines()
    assert lines[0] == "---"
    closing = next(i for i, line in enumerate(lines[1:], start=1) if line == "---")
    body = "\n".join(lines[1:closing])
    assert body.startswith('description: "') and body.rstrip().endswith('"')
    # The embedder and the judge both cap the description at 1200 characters.
    assert 200 < len(body) <= 1200


def test_the_description_carries_what_users_ask():
    description = _description().lower()
    for keyword in ("own files", "index", "folder", "sync progress", "admin", "vector embedding",
                    "google drive"):
        assert keyword in description, f"description never mentions {keyword!r}"
    assert "not for cremind's own documentation" in description


def _doc_for(path: str) -> Path:
    parts = path.split(" ")
    if parts[2] == RESEARCH_GROUP:
        return RESEARCH_DOC
    if parts[2] == DRIVE_GROUP:
        return DRIVE_DOC
    if parts[2] in SEARCH_COMMANDS and len(parts) == 3:
        return SEARCH_DOC
    return DOC


def test_every_subcommand_and_flag_is_documented():
    import typer

    from app.cli.commands.docs import docs_app

    texts = {doc: _doc_text(doc) for doc in ALL_DOCS}
    seen = []
    for path, callback in _walk(docs_app, "cremind docs"):
        seen.append(path)
        text = texts[_doc_for(path)]
        assert path in text, f"subcommand `{path}` is undocumented"
        for param in inspect.signature(callback).parameters.values():
            default = param.default
            if not isinstance(default, typer.models.OptionInfo):
                continue
            for decl in default.param_decls or []:
                for flag in decl.split("/"):
                    if flag.startswith("--"):
                        assert flag in text, f"flag {flag} of `{path}` is undocumented"
    assert "cremind docs status" in seen
    assert {f"cremind docs {c}" for c in SEARCH_COMMANDS} <= set(seen)
    assert {f"cremind docs research {c}" for c in RESEARCH_COMMANDS} <= set(seen)
    assert {f"cremind docs drive {c}" for c in DRIVE_COMMANDS} <= set(seen)


def test_the_drive_doc_carries_what_users_ask():
    """Retrieval must find it for "index/search my Google Drive", and the body
    must teach what a script cannot guess: disabling deletes, whole-Drive
    accounts need folders, and what each hold means for the index."""
    description = _description(DRIVE_DOC).lower()
    for keyword in ("own", "google drive", "index", "search", "folder", "whole-drive", "per-file",
                    "disable", "deletes", "gdrive skill", "not for cremind's own documentation"):
        assert keyword in description, f"drive doc description never mentions {keyword!r}"
    text = _doc_text(DRIVE_DOC)
    for word in ("DriveFoldersRequired", "DriveNotLinked", "auth_revoked", "drive_unlinked",
                 "drive_unreachable", "drive_misconfigured", "7 days", "cremind google unlink gdrive",
                 "--source drive", "exits 2"):
        assert word in text, f"drive doc never explains {word!r}"
    # The main doc and the search doc point to it / to the source filter.
    assert "cremind docs drive" in _doc_text()
    assert "--source" in _doc_text(SEARCH_DOC)


def test_the_research_doc_carries_what_users_ask():
    """Retrieval must tell research apart from a quick search ("compile the
    MKT-report folder", "analyze ABC's dispute under the land law"), and the
    body must teach the parts a script cannot guess."""
    description = _description(RESEARCH_DOC).lower()
    for keyword in ("own", "research", "compile", "analy", "legal", "edition", "coverage",
                    "citation", "--follow", "not for cremind's own documentation"):
        assert keyword in description, f"research doc description never mentions {keyword!r}"
    text = _doc_text(RESEARCH_DOC)
    # Exit codes, answering a clarification, and the chat/tool boundary.
    assert "| 2 |" in text and "needs_clarification" in text and "interrupted" in text
    assert "--answer edition=" in text and "confirm=true" in text
    assert "documentation_search__research" in text
    # The main doc points to it.
    assert "cremind docs research" in _doc_text()


def test_the_search_doc_carries_what_users_ask():
    description = _description(SEARCH_DOC).lower()
    for keyword in ("own", "search", "find", "read", "cite", "project", "citation"):
        assert keyword in description, f"search doc description never mentions {keyword!r}"
    assert "not for cremind's own documentation" in description
    text = _doc_text(SEARCH_DOC)
    # The modes a result reports, and the citation token shape, are explained.
    for word in ("hybrid", "lexical_only", "catalog_only", "[doc:"):
        assert word in text


def test_the_doc_covers_the_confirmation_trap():
    """Scripts and the agent's shell cannot answer a prompt; they must know to
    pass --yes, and that exit code 2 means nothing was changed."""
    text = _doc_text()
    assert "--yes" in text
    assert "exit code 2" in text.lower() or "Exit code 2" in text


def test_the_doc_names_the_install_command_for_the_extras():
    assert "cremind features install documentation_search" in _doc_text()


@pytest.mark.parametrize("doc", ALL_DOCS, ids=lambda d: d.name)
def test_every_json_example_runs_the_docs_group(doc):
    """The group was `userdocs`, and for a while the examples said
    `cremind --json documents …` — a group that never existed. Every `--json`
    example must name the real one."""
    import re

    groups = re.findall(r"cremind --json ([a-z][a-z-]*)", _doc_text(doc))
    assert groups, f"{doc.name} shows no --json example"
    assert set(groups) == {"docs"}, f"{doc.name}: --json examples run {sorted(set(groups))}"


def test_the_doc_says_what_the_group_was_called_before_the_rename():
    text = _doc_text()
    assert "cremind userdocs" in text and "/api/userdocs" in text
    assert "410 EndpointRenamed" in text
    assert "formerly `cremind userdocs`" in _description().lower()
