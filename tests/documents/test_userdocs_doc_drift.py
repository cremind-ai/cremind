"""Doc/code drift pin for `[cli]cremind userdocs.md`.

CLAUDE.md mandates that a CLI command and its bundled doc move in lockstep.
This walks the nested Typer groups (`userdocs`, `userdocs excludes`,
`userdocs admin`) so a new subcommand or flag cannot land undocumented.

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

DOC = (
    Path(__file__).resolve().parents[2]
    / "app" / "documents" / "bundled" / "[cli]cremind userdocs.md"
)


def _doc_text() -> str:
    assert DOC.exists(), f"missing bundled doc: {DOC.name}"
    return DOC.read_text(encoding="utf-8")


def _description() -> str:
    return _doc_text().split("---")[1]


def _walk(app, prefix: str):
    """Yield (full command path, callback) for every command, depth-first."""
    for command in app.registered_commands:
        name = command.name or command.callback.__name__
        yield f"{prefix} {name}", command.callback
    for group in app.registered_groups:
        yield from _walk(group.typer_instance, f"{prefix} {group.name}")


def test_frontmatter_is_well_formed():
    lines = _doc_text().splitlines()
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


def test_every_subcommand_and_flag_is_documented():
    import typer

    from app.cli.commands.userdocs import userdocs_app

    text = _doc_text()
    seen = []
    for path, callback in _walk(userdocs_app, "cremind userdocs"):
        seen.append(path)
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


def test_the_doc_covers_the_confirmation_trap():
    """Scripts and the agent's shell cannot answer a prompt; they must know to
    pass --yes, and that exit code 2 means nothing was changed."""
    text = _doc_text()
    assert "--yes" in text
    assert "exit code 2" in text.lower() or "Exit code 2" in text


def test_the_doc_names_the_install_command_for_the_extras():
    assert "cremind features install userdocs" in _doc_text()
