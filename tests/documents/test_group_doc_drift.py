"""Doc/code drift pin for `[cli]cremind group.md`.

CLAUDE.md mandates that a CLI command and its bundled doc move in lockstep.
`cremind group` is the most exposed case: it is a *nested* command tree
(`group members add`), so a flag added to a sub-sub-command is exactly the kind
of change a hand-written doc silently misses. This walks the whole tree, not
just the top level.

Two claims in the body are load-bearing rather than decorative — silence being a
real answer, and this page NOT being the one about platform group chats —
because without the first the feature reads as broken, and without the second a
reader configures the wrong feature entirely.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

pytest.importorskip("typer")

DOC = (
    Path(__file__).resolve().parents[2]
    / "app" / "documents" / "bundled" / "[cli]cremind group.md"
)

EXPECTED_COMMANDS = {
    "cremind group list",
    "cremind group create",
    "cremind group show",
    "cremind group set",
    "cremind group delete",
    "cremind group send",
    "cremind group history",
    "cremind group members add",
    "cremind group members remove",
}


def _doc_text() -> str:
    assert DOC.exists(), f"missing bundled doc: {DOC.name}"
    return DOC.read_text(encoding="utf-8")


def _frontmatter() -> str:
    lines = _doc_text().splitlines()
    assert lines[0] == "---", "frontmatter must open with --- on the first line"
    closing = next(i for i, line in enumerate(lines[1:], start=1) if line == "---")
    return "\n".join(lines[1:closing])


def test_frontmatter_is_well_formed():
    body = _frontmatter()
    assert body.startswith('description: "'), "description must be present and quoted"
    assert body.rstrip().endswith('"')


def test_the_description_stays_in_the_retrieval_sweet_spot():
    """~250-350 chars: shorter stops discriminating, longer dilutes the embedding
    (see app/documents/bundled/document.md)."""
    body = _frontmatter()
    value = body[body.index('"') + 1 : body.rindex('"')]
    assert 250 <= len(value) <= 350, f"description is {len(value)} characters"


def test_the_description_disambiguates_against_its_neighbours():
    value = _frontmatter().lower()
    assert "cremind conv" in value, "must say what it is not: `cremind conv`"
    assert "cremind channels" in value, "must say what it is not: `cremind channels`"


def test_every_subcommand_and_flag_is_documented():
    from app.cli.commands.groups import group_app

    text = _doc_text()
    documented: set[str] = set()
    _walk(group_app, "cremind group", text, documented)
    assert documented == EXPECTED_COMMANDS


def _walk(app, prefix: str, text: str, documented: set[str]) -> None:
    """Assert every command and long flag under `app` appears in the doc.

    Recurses into `registered_groups` so `group members add` is covered the same
    way `group list` is.
    """
    import typer

    for command in app.registered_commands:
        name = command.name or (command.callback.__name__ if command.callback else "")
        assert name, f"a subcommand of {prefix!r} has no resolvable name"
        full = f"{prefix} {name}"
        documented.add(full)
        assert full in text, f"subcommand {full!r} is undocumented"

        for param in inspect.signature(command.callback).parameters.values():
            default = param.default
            if not isinstance(default, typer.models.OptionInfo):
                continue
            for decl in default.param_decls or []:
                if decl.startswith("--"):
                    assert decl in text, f"flag {decl} of `{full}` is undocumented"

    for sub in app.registered_groups:
        sub_app = sub.typer_instance
        assert sub_app is not None, f"a sub-app of {prefix!r} has no typer instance"
        sub_name = sub.name or sub_app.info.name or ""
        assert sub_name, f"a sub-app of {prefix!r} has no name"
        _walk(sub_app, f"{prefix} {sub_name}", text, documented)


def test_the_doc_states_that_silence_is_an_answer():
    """Without this the first thing every user reports is "the other agents
    never replied" — which is the feature working."""
    text = _doc_text()
    assert "[silent]" in text
    assert "silent" in text.lower()


def test_the_doc_states_the_real_history_default():
    """`--after` is the one flag whose default a reader can get wrong in a way
    that silently loses posts: omitted asks for the NEWEST page, not the room's
    beginning, and only `--after -1` reads from the first post."""
    text = " ".join(_doc_text().lower().split())
    assert "the server returns the newest `--limit` posts" in text
    assert "--after -1" in text


def test_the_worked_example_numbers_the_timeline_from_zero():
    """`ordering` starts at 0, so a transcript whose first post is `#1` teaches
    the wrong cursor arithmetic to anyone deriving `--after` from it."""
    text = _doc_text()
    assert "[#0 14:20:03] Operator (user): what time is it now, Rex?" in text
    assert "[#1 14:20:07] Rex (agent): 14:20 here." in text


def test_the_doc_disambiguates_itself_from_channel_group_chats():
    """`cremind group` (Cremind's own multi-agent rooms) and `cremind channels
    groups` (one agent in a real platform group) are separate features with
    almost the same name. A reader who lands on the wrong one configures the
    wrong thing, so the disclaimer is pinned rather than left to prose drift."""
    text = _doc_text()
    assert "## Not the same as channel group chats" in text, (
        "the doc must carry a section telling readers this is not the "
        "platform-group feature"
    )
    # Reflowed prose puts line breaks anywhere, so match on collapsed whitespace.
    section = text.split("## Not the same as channel group chats", 1)[1]
    section = " ".join(section.split("\n## ", 1)[0].split())
    assert "cremind channels groups" in section, (
        "the section must name the command that DOES manage platform groups"
    )
