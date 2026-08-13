"""Doc/code drift pin for `[cli]cremind google.md`.

CLAUDE.md mandates that a CLI command and its bundled doc move in lockstep;
nothing enforces it mechanically for `[cli]` docs, so this does — modelled on
:mod:`tests.documents.test_auth_doc_drift`.

The stakes here are higher than "the docs are stale". The ``description`` is the
*only* text embedded into the ``documentation_search`` vector store, so it is what
decides whether an agent asked "how do I disconnect my Google account?" ever finds
this command. And unlinking has two consequences an agent must not discover the
hard way — the shared grant across all five skills, and Drive's per-file grants
being destroyed for good — so both are pinned as load-bearing claims.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("typer")

DOC = (
    Path(__file__).resolve().parents[2]
    / "app" / "documents" / "bundled" / "[cli]cremind google.md"
)


def _doc_text() -> str:
    assert DOC.exists(), f"missing bundled doc: {DOC.name}"
    return DOC.read_text(encoding="utf-8")


def _description() -> str:
    return _doc_text().split("---")[1]


def test_frontmatter_is_well_formed():
    lines = _doc_text().splitlines()
    assert lines[0] == "---", "frontmatter must open with --- on the first line"
    closing = next(i for i, line in enumerate(lines[1:], start=1) if line == "---")
    body = "\n".join(lines[1:closing])
    assert body.startswith('description: "'), "description must be present and quoted"
    assert body.rstrip().endswith('"')
    assert len(body) > 200


def test_the_description_carries_the_queries_users_actually_type():
    description = _description().lower()
    for keyword in (
        "unlink",
        "disconnect",
        "revoke",
        "remove",
        "sign out",
        "log out",
        "switch google account",
        "wrong account",
    ):
        assert keyword in description, f"description never mentions {keyword!r}"


def test_the_description_names_every_google_skill():
    from app.google.registry import GOOGLE_SKILLS

    description = _description()
    for spec in GOOGLE_SKILLS:
        assert spec.dir_name in description, f"description never mentions {spec.dir_name}"


def test_every_subcommand_and_flag_is_documented():
    import inspect

    import typer

    from app.cli.commands.google import google_app

    text = _doc_text()
    documented_names = set()

    for command in google_app.registered_commands:
        name = command.name or (command.callback.__name__ if command.callback else "")
        assert name, "a google subcommand has no resolvable name"
        documented_names.add(name)
        assert f"cremind google {name}" in text, f"subcommand {name!r} is undocumented"

        for param in inspect.signature(command.callback).parameters.values():
            default = param.default
            if not isinstance(default, typer.models.OptionInfo):
                continue
            for decl in default.param_decls or []:
                if decl.startswith("--"):
                    assert decl in text, f"flag {decl} of `google {name}` is undocumented"

    assert documented_names == {"status", "unlink"}


def test_the_doc_states_the_shared_grant_consequence():
    """Without this an agent will promise a per-skill unlink Google cannot honour."""
    text = _doc_text().lower()
    assert "one app" in text
    assert "--force-revoke" in text


def test_the_doc_states_that_drive_grants_are_lost_for_good():
    text = _doc_text().lower()
    assert "permanently" in text or "for good" in text
    assert "pick the files again" in text


def test_the_doc_states_that_revoking_cannot_be_retried():
    """The failure mode most likely to produce a wrong agent suggestion."""
    text = _doc_text().lower()
    assert "one-way" in text
    assert "myaccount.google.com/connections" in text


def test_the_doc_covers_the_non_interactive_trap():
    text = _doc_text()
    assert "--yes" in text and "exec_shell" in text


def test_the_doc_documents_every_error_code_the_api_can_return():
    text = _doc_text()
    for code in ("unsupported_skill", "skill_not_installed", "wipe_failed"):
        assert code in text, f"error code {code!r} is undocumented"


def test_the_doc_distinguishes_itself_from_the_calendar_page_disconnect():
    """Two different Google credentials; confusing them is the likeliest mistake."""
    text = _doc_text()
    assert "cremind calendar google disconnect" in text


def test_the_doc_says_the_listener_needs_re_registering():
    text = _doc_text().lower()
    assert "autostart" in text
    assert "register" in text
