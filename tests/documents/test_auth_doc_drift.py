"""Doc/code drift pin for `[cli]cremind auth.md`.

CLAUDE.md mandates that a CLI command and its bundled doc move in lockstep, but
nothing mechanically enforces it for `[cli]` docs. This does, for the one doc
most likely to drift: every subcommand and every long-form flag on
``cremind auth`` has to appear in the doc, and the frontmatter has to stay
well-formed (the ``description`` is the *only* text embedded into the vector
store, so a broken one silently makes the command unfindable).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("typer")

DOC = (
    Path(__file__).resolve().parents[2]
    / "app" / "documents" / "bundled" / "[cli]cremind auth.md"
)


def _doc_text() -> str:
    assert DOC.exists(), f"missing bundled doc: {DOC.name}"
    return DOC.read_text(encoding="utf-8")


def test_frontmatter_is_well_formed():
    lines = _doc_text().splitlines()
    assert lines[0] == "---", "frontmatter must open with --- on the first line"
    closing = next(i for i, line in enumerate(lines[1:], start=1) if line == "---")
    body = "\n".join(lines[1:closing])
    assert body.startswith('description: "'), "description must be present and quoted"
    assert body.rstrip().endswith('"')
    # Long enough to discriminate against [cli]cremind profile.md, which also
    # talks about token files and CREMIND_TOKEN.
    assert len(body) > 200


def test_the_description_carries_the_queries_users_actually_type():
    description = _doc_text().split("---")[1]
    for keyword in ("revoke", "rotate", "regenerate", "leaked", "expired", "locked out"):
        assert keyword in description.lower(), f"description never mentions {keyword!r}"


def test_every_subcommand_and_flag_is_documented():
    from app.cli.commands.auth import auth_app

    text = _doc_text()
    documented_names = set()

    for command in auth_app.registered_commands:
        name = command.name or (command.callback.__name__ if command.callback else "")
        assert name, "an auth subcommand has no resolvable name"
        documented_names.add(name)
        assert f"cremind auth {name}" in text, f"subcommand {name!r} is undocumented"

        import inspect

        import typer

        for param in inspect.signature(command.callback).parameters.values():
            default = param.default
            if not isinstance(default, typer.models.OptionInfo):
                continue
            for decl in default.param_decls or []:
                if decl.startswith("--"):
                    assert decl in text, f"flag {decl} of `auth {name}` is undocumented"

    assert documented_names == {"status", "regenerate", "show"}


def test_the_doc_states_that_rotation_revokes():
    """The single most important claim in the doc — if this ever drops out,
    users will assume regenerate is a harmless re-mint."""
    text = _doc_text().lower()
    assert "rotation is revocation" in text
    assert "--yes" in text and "exec_shell" in text  # the non-interactive trap
