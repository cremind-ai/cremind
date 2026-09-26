"""Doc/code drift pin for `[cli]cremind backup.md`.

CLAUDE.md mandates that a CLI command and its bundled doc move in lockstep.
This pins every flag of every ``cremind backup`` subcommand into the body, and
the profiles' working directories — archived by default since each profile got
its own — into the ``description``, the only text that is embedded: someone
asking "does a backup include my files?" must land here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

typer = pytest.importorskip("typer")

DOC = (
    Path(__file__).resolve().parents[2]
    / "app" / "cremind_documents" / "bundled" / "[cli]cremind backup.md"
)


def _parts() -> tuple[str, str]:
    text = DOC.read_text(encoding="utf-8")
    _, frontmatter, body = text.split("---", 2)
    return frontmatter, body


def test_every_flag_of_every_subcommand_is_documented() -> None:
    from app.cli.commands.backup import backup_app

    group = typer.main.get_command(backup_app)
    _, body = _parts()
    for name, command in group.commands.items():
        assert f"cremind backup {name}" in body, name
        for param in command.params:
            for opt in getattr(param, "opts", ()):
                if opt.startswith("--") and opt != "--help":
                    assert opt in body, f"{name}: {opt} is not documented"


def test_the_description_says_the_working_directories_are_included() -> None:
    frontmatter, _ = _parts()
    description = frontmatter.lower()
    assert "working directory" in description
    assert "--no-workspaces" in description


def test_the_body_says_what_is_not_archived_and_how_restore_treats_them() -> None:
    _, body = _parts()
    assert "outside* the\n  workspaces folder" in body or "outside the workspaces folder" in body
    assert "nothing else is deleted" in body
    assert "target's* workspaces folder" in body
