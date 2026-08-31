"""Doc/code drift pin for `[cli]cremind tls.md`.

CLAUDE.md mandates that a CLI command and its bundled doc move in lockstep;
nothing mechanically enforces it for `[cli]` docs, so this does it for
``cremind tls``. The stakes here are unusual: the ``description`` is the *only*
text embedded into the vector store, and the queries that should reach this doc
are the browser's own error strings — a user pasting "ERR_CERT_AUTHORITY_INVALID"
or "your connection is not private" has to land on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("typer")

DOC = (
    Path(__file__).resolve().parents[2]
    / "app" / "documents" / "bundled" / "[cli]cremind tls.md"
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


def test_the_description_carries_the_errors_users_actually_paste():
    description = _doc_text().split("---")[1].lower()
    for keyword in (
        "err_cert_authority_invalid",
        "connection is not private",
        "certificate",
        "trust",
        "ca.pem",
        "cremind_ssl=auto",
        # Both modes generate a CA, so both must find this doc.
        "cremind_ssl=after-setup",
    ):
        assert keyword in description, f"description never mentions {keyword!r}"


def test_every_subcommand_and_flag_is_documented():
    import inspect

    import typer

    from app.cli.commands.tls import tls_app

    text = _doc_text()
    documented_names = set()

    for command in tls_app.registered_commands:
        name = command.name or (command.callback.__name__ if command.callback else "")
        assert name, "a tls subcommand has no resolvable name"
        documented_names.add(name)
        assert f"cremind tls {name}" in text, f"subcommand {name!r} is undocumented"

        for param in inspect.signature(command.callback).parameters.values():
            default = param.default
            if not isinstance(default, typer.models.OptionInfo):
                continue
            for decl in default.param_decls or []:
                if decl.startswith("--"):
                    assert decl in text, f"flag {decl} of `tls {name}` is undocumented"

    assert documented_names == {"export", "fingerprint", "trust"}


def test_the_doc_lists_every_platform_the_command_supports():
    """A user on a platform the doc omits has no manual fallback."""
    text = _doc_text()
    for marker in (
        "certutil -addstore -user Root",
        "security add-trusted-cert",
        "update-ca-certificates",
        "update-ca-trust",
    ):
        assert marker in text, f"the manual path for {marker!r} is undocumented"


def test_the_doc_explains_how_to_reach_a_remote_ca():
    """The default path is local; containers and clusters need the download."""
    text = _doc_text()
    assert "/ca.pem" in text
    assert "kubectl" in text and "docker" in text
    # PowerShell's redirection corrupts the PEM — the trap that wastes an hour.
    assert "Out-File -Encoding ascii" in text
