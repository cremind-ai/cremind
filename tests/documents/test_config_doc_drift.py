"""Doc/code drift pin for `[cli]cremind config.md`.

Two things this doc got wrong and must not lose again.

First, the Developer page's configuration-file re-download shipped with no
documentation at all. The whole point of that card is recovering a file the
Setup Wizard hands over exactly once, so a user who lost it asks
``documentation_search`` for it — and only the frontmatter ``description`` is
embedded, so the retrieval intent has to live there, not just in the body.

Second, ``--json`` is a *root* flag on the `cremind` app, not an option on any
`cremind config` subcommand: ``cremind config schema --json`` exits with
``No such option: --json``. The doc's examples are copy-pasted by users and by
the agent, so a wrong form there is a broken command.
"""

from __future__ import annotations

import re
from pathlib import Path

DOC = (
    Path(__file__).resolve().parents[2]
    / "app" / "documents" / "bundled" / "[cli]cremind config.md"
)


def _doc_text() -> str:
    assert DOC.exists(), f"missing bundled doc: {DOC.name}"
    return DOC.read_text(encoding="utf-8")


def _description() -> str:
    text = _doc_text().lstrip()
    end = text.find("---", 3)
    assert end != -1, "frontmatter is not closed"
    return text[3:end]


def _code_blocks() -> list[str]:
    return re.findall(r"```(?:bash|text)?\n(.*?)```", _doc_text(), re.S)


def test_the_description_carries_the_config_file_recovery_intent():
    description = _description().lower()
    for keyword in (
        "configuration file",
        "setup wizard",
        "re-download",
        "lost my setup file",
        "recover my token",
        "developer",
    ):
        assert keyword in description, f"description never mentions {keyword!r}"


def test_the_body_documents_what_the_developer_card_actually_ships():
    text = _doc_text()
    assert "Sidebar → Developer → Configuration File" in text, (
        "the doc must name the exact path to the card"
    )
    assert "cremind-<profile>-config." in text, "the filename shape is missing"
    for label in ("Markdown (.md)", "JSON (.json)", "Env file (.env)"):
        assert label in text, f"format option {label!r} is not documented"
    assert "admin-only" in text, "the admin-only gate is not documented"
    assert "plain text" in text, "the JWT/passwords warning is missing"
    # No CLI command produces the file, so the doc has to point at the pieces.
    for command in ("cremind auth show", "cremind server environment"):
        assert command in text, f"CLI equivalent {command!r} is not mentioned"


def test_no_example_puts_json_after_a_config_subcommand():
    """``--json`` is declared on the root callback, never on a subcommand."""
    bad = re.compile(r"cremind\s+config\s+\w+[^\n|]*--json")
    for block in _code_blocks():
        for line in block.splitlines():
            assert not bad.search(line), (
                "`--json` is a ROOT flag — write `cremind --json config <sub>`; "
                f"this example fails at parse time: {line.strip()!r}"
            )
    assert "cremind --json config" in _doc_text(), (
        "the doc must show the working root-flag form at least once"
    )
