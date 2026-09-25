"""Doc/code drift pin for `[cli]cremind userdocs.md` and
`[cli]cremind userdocs search.md`.

CLAUDE.md mandates that a CLI command and its bundled doc move in lockstep.
This walks the nested Typer groups (`userdocs`, `userdocs excludes`,
`userdocs admin`) so a new subcommand or flag cannot land undocumented. The
query subcommands (`search`, `find`, `read`, `cite`) live in their own doc —
one reference per question a user asks ("set it up" vs "search my files"),
each small enough to be delivered whole.

The ``description`` is the only text embedded into ``documentation_search``,
so it must carry what users actually ask ("search my files", "index my
documents") — and must say it is NOT Cremind's own documentation, or the two
search features get confused.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

pytest.importorskip("typer")

BUNDLED = Path(__file__).resolve().parents[2] / "app" / "documents" / "bundled"
DOC = BUNDLED / "[cli]cremind userdocs.md"
SEARCH_DOC = BUNDLED / "[cli]cremind userdocs search.md"

# Documented in SEARCH_DOC instead of DOC.
SEARCH_COMMANDS = frozenset({"search", "find", "read", "cite"})


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


@pytest.mark.parametrize("doc", [DOC, SEARCH_DOC], ids=lambda d: d.name)
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
    for keyword in ("own files", "index", "folder", "sync progress", "admin", "vector embedding"):
        assert keyword in description, f"description never mentions {keyword!r}"
    assert "not for cremind's own documentation" in description


def _in_search_doc(path: str) -> bool:
    return path.split(" ")[2] in SEARCH_COMMANDS and path.count(" ") == 2


def test_every_subcommand_and_flag_is_documented():
    import typer

    from app.cli.commands.userdocs import userdocs_app

    texts = {False: _doc_text(), True: _doc_text(SEARCH_DOC)}
    seen = []
    for path, callback in _walk(userdocs_app, "cremind userdocs"):
        seen.append(path)
        text = texts[_in_search_doc(path)]
        assert path in text, f"subcommand `{path}` is undocumented"
        for param in inspect.signature(callback).parameters.values():
            default = param.default
            if not isinstance(default, typer.models.OptionInfo):
                continue
            for decl in default.param_decls or []:
                for flag in decl.split("/"):
                    if flag.startswith("--"):
                        assert flag in text, f"flag {flag} of `{path}` is undocumented"
    assert "cremind userdocs status" in seen
    assert {f"cremind userdocs {c}" for c in SEARCH_COMMANDS} <= set(seen)


def test_the_search_doc_carries_what_users_ask():
    description = _description(SEARCH_DOC).lower()
    for keyword in ("own", "search", "find", "read", "cite", "project", "citation"):
        assert keyword in description, f"search doc description never mentions {keyword!r}"
    assert "not for cremind's own documentation" in description
    text = _doc_text(SEARCH_DOC)
    # The modes a result reports, and the citation token shape, are explained.
    for word in ("hybrid", "lexical_only", "catalog_only", "[ud:"):
        assert word in text


def test_the_doc_covers_the_confirmation_trap():
    """Scripts and the agent's shell cannot answer a prompt; they must know to
    pass --yes, and that exit code 2 means nothing was changed."""
    text = _doc_text()
    assert "--yes" in text
    assert "exit code 2" in text.lower() or "Exit code 2" in text


def test_the_doc_names_the_install_command_for_the_extras():
    assert "cremind features install userdocs" in _doc_text()
