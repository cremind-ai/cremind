"""Doc/code drift pin for `[cli]cremind channels.md`.

CLAUDE.md mandates that a CLI command and its bundled doc move in lockstep.
`cremind channels` earned this pin when it grew a *nested* sub-app —
`channels groups`, one command per platform-group operation — because a flag
added to a sub-sub-command is exactly the kind of change a hand-written doc
silently misses. So this walks the whole tree, not just the top level.

The reciprocal "not the same as" notes are pinned too, in **both** docs:
`cremind channels groups` (one agent in a real platform group) and
`cremind group` (Cremind's own room of several agents) are separate features
with almost the same name, and a reader who lands on the wrong page configures
the wrong thing.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

pytest.importorskip("typer")

BUNDLED = Path(__file__).resolve().parents[2] / "app" / "documents" / "bundled"
DOC = BUNDLED / "[cli]cremind channels.md"
GROUP_DOC = BUNDLED / "[cli]cremind group.md"

EXPECTED_COMMANDS = {
    "cremind channels list",
    "cremind channels catalog",
    "cremind channels add",
    "cremind channels notify-filter",
    "cremind channels send",
    "cremind channels message",
    "cremind channels set-phone",
    "cremind channels set-confirm",
    "cremind channels edit",
    "cremind channels enable",
    "cremind channels disable",
    "cremind channels senders",
    "cremind channels approve",
    "cremind channels revoke",
    "cremind channels clear-history",
    "cremind channels forget",
    "cremind channels pair",
    "cremind channels repair",
    "cremind channels delete",
    "cremind channels groups list",
    "cremind channels groups approve",
    "cremind channels groups block",
    "cremind channels groups forget",
    "cremind channels groups members",
    "cremind channels groups policy",
    "cremind channels groups allow",
    "cremind channels groups deny",
    "cremind channels groups respond",
    "cremind channels groups refresh",
    "cremind channels groups available",
    "cremind channels groups add",
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


def test_the_description_is_substantial():
    """No 250-350 window here: unlike its neighbours this doc's description is a
    deliberately long catalogue of every operation, because `channels` is the
    one feature users look for by naming a platform rather than the command."""
    body = _frontmatter()
    value = body[body.index('"') + 1 : body.rindex('"')]
    assert len(value) > 200, f"description is only {len(value)} characters"


def test_the_description_covers_group_chats():
    value = _frontmatter().lower()
    assert "group chats" in value, "the description must mention group chats"
    assert "cremind group" in value, "must say what it is not: `cremind group`"


def test_every_subcommand_and_flag_is_documented():
    from app.cli.commands.channels import channels_app

    text = _doc_text()
    documented: set[str] = set()
    _walk(channels_app, "cremind channels", text, documented)
    assert documented == EXPECTED_COMMANDS


def _walk(app, prefix: str, text: str, documented: set[str]) -> None:
    """Assert every command and long flag under `app` appears in the doc.

    Recurses into `registered_groups` so `channels groups policy` is covered the
    same way `channels list` is.
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


def test_the_doc_explains_that_group_chats_are_opt_in_and_need_approval():
    """Both halves are the whole safety story: nothing is recorded while the
    channel flag is off, and nothing is read until a human approves the group.
    A doc that loses either reads like the agent joins groups by itself."""
    text = " ".join(_doc_text().lower().split())
    assert "group_chats_enabled" in text or "--group-chats" in text
    assert "pending" in text
    assert "cremind channels groups approve" in text


def test_the_doc_disambiguates_itself_from_cremind_group():
    """`cremind channels groups` and `cremind group` are separate features with
    almost the same name, so the disclaimer is pinned rather than left to prose
    drift."""
    text = _doc_text()
    assert "### Not the same as `cremind group`" in text, (
        "the doc must carry a section telling readers this is not the "
        "multi-profile-room feature"
    )
    section = text.split("### Not the same as `cremind group`", 1)[1]
    section = " ".join(section.split("\n## ", 1)[0].split())
    assert "cremind channels groups" in section
    assert "cremind group" in section


def test_the_group_doc_points_back_at_this_one():
    """The reciprocal note: somebody who reaches for `cremind group` wanting a
    Telegram group has to be told where the feature actually lives."""
    text = GROUP_DOC.read_text(encoding="utf-8")
    assert "## Not the same as channel group chats" in text
    section = text.split("## Not the same as channel group chats", 1)[1]
    section = " ".join(section.split("\n## ", 1)[0].split())
    assert "cremind channels groups" in section
