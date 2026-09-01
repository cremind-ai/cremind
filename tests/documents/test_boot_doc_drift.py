"""Doc/code drift pin for `[cli]cremind boot.md`.

CLAUDE.md mandates that a CLI command and its bundled doc move in lockstep;
nothing mechanically enforces it for `[cli]` docs, so this does it for
``cremind boot``. The ``description`` is the *only* text embedded into the
vector store, and the queries that should reach this doc are symptoms rather
than command names — someone whose server "isn't running after a reboot" has
no reason to guess that a command called ``boot`` is the answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("typer")

DOC = (
    Path(__file__).resolve().parents[2]
    / "app" / "documents" / "bundled" / "[cli]cremind boot.md"
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
    assert len(body) > 200


def test_the_description_carries_the_symptoms_users_actually_describe():
    description = _doc_text().split("---")[1].lower()
    for keyword in (
        "start automatically",
        "reboot",
        "logout",
        "restart",
        # The three OS mechanisms, so a user who knows what they want finds it.
        "systemd",
        "launchd",
        "scheduled task",
        # The after-setup HTTPS switch depends on this; that failure looks
        # like a TLS problem, not a supervision one.
        "after-setup",
        # `proc autostart` is a different feature with a confusingly close
        # name; the disambiguation has to be embedded, not just in the body.
        "autostart",
    ):
        assert keyword in description, f"description never mentions {keyword!r}"


def test_every_subcommand_and_flag_is_documented():
    import inspect

    import typer

    from app.cli.commands.boot import boot_app

    text = _doc_text()
    documented_names = set()

    for command in boot_app.registered_commands:
        name = command.name or (command.callback.__name__ if command.callback else "")
        assert name, "a boot subcommand has no resolvable name"
        documented_names.add(name)
        assert f"cremind boot {name}" in text, f"subcommand {name!r} is undocumented"

        for param in inspect.signature(command.callback).parameters.values():
            default = param.default
            if not isinstance(default, typer.models.OptionInfo):
                continue
            for decl in default.param_decls or []:
                # Typer declares a boolean toggle as one "--on/--off" string;
                # both halves are things a user types, so require both.
                for part in decl.split("/"):
                    if part.startswith("--"):
                        assert part in text, (
                            f"flag {part} of `boot {name}` is undocumented"
                        )

    assert documented_names == {"enable", "disable", "status"}


def test_the_doc_lists_every_platform_the_command_supports():
    """A user on a platform the doc omits has no way to check or undo by hand."""
    text = _doc_text()
    for marker in (
        "systemctl --user stop cremind",
        "launchctl bootout gui/",
        "Stop-ScheduledTask -TaskName 'Cremind Server'",
        "loginctl enable-linger",
        "cremind.service",
        "io.cremind.server.plist",
    ):
        assert marker in text, f"the manual path for {marker!r} is undocumented"


def test_the_doc_names_the_installer_opt_out():
    """The installers turn this on by default, so the way off must be findable."""
    text = _doc_text()
    assert "--no-boot-service" in text
    assert "-NoBootService" in text


def test_the_doc_explains_why_the_shim_is_the_launch_target():
    """The one mistake a user editing the unit by hand would make."""
    text = _doc_text()
    assert "bin/cremind" in text
    assert ".env" in text
