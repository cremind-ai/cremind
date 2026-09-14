"""Doc/code drift pin for `[cli]cremind profile wizard.md`.

Three things about this document are load-bearing rather than decorative.

First, retrieval. Only the frontmatter ``description`` is embedded, and the
judge that picks a document sees nothing but its name and that description. The
query this whole feature exists for — "create a profile called javis" — has to
land here and not on ``[cli]cremind profile``, whose ``create`` makes an empty
shell. So the wizard's description must lead with the create intent and the
profile document's must not: two documents opening with the same recall sentence
pull their embeddings together and neither wins reliably.

Second, the hand-off. Everything the agent must do *after* ``finish`` is
mechanism a model cannot guess: a Markdown link to a file path will not open
(file routes take the token in a header), only a tool result carrying the file
renders a download, and a slow ``finish`` comes back as a running process whose
output has to be collected separately. If those sentences go, the feature's last
mile silently stops working.

Third, the turn break. The shared CLI directive tells the agent to run
state-changing commands as soon as they are asked for. For this family that is
exactly wrong — every step needs a value only the user has — so the document has
to say so explicitly, and the commands print a STOP line saying it too.
"""

from __future__ import annotations

import inspect
from pathlib import Path

BUNDLED = Path(__file__).resolve().parents[2] / "app" / "documents" / "bundled"
DOC = BUNDLED / "[cli]cremind profile wizard.md"
PROFILE_DOC = BUNDLED / "[cli]cremind profile.md"
SETUP_DOC = BUNDLED / "[cli]cremind setup.md"

EXPECTED_COMMANDS = {
    "cremind profile wizard start",
    "cremind profile wizard status",
    "cremind profile wizard set",
    "cremind profile wizard skip",
    "cremind profile wizard finish",
    "cremind profile wizard cancel",
}


def _text(path: Path) -> str:
    assert path.exists(), f"missing bundled doc: {path.name}"
    return path.read_text(encoding="utf-8")


def _description(path: Path) -> str:
    text = _text(path).lstrip()
    end = text.find("---", 3)
    assert end != -1, f"{path.name}: frontmatter is not closed"
    return text[3:end]


def _walk(app, prefix: str, text: str, documented: set[str]) -> None:
    """Assert every command and long flag under ``app`` appears in the doc."""
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
        _walk(sub_app, f"{prefix} {sub_name}", text, documented)


def test_every_subcommand_and_flag_is_documented():
    from app.cli.commands.profile_wizard import wizard_app

    documented: set[str] = set()
    _walk(wizard_app, "cremind profile wizard", _text(DOC), documented)
    assert documented == EXPECTED_COMMANDS


def test_the_description_wins_the_create_a_profile_query():
    description = _description(DOC).lower()
    for keyword in ("create", "make, add, register", "profile", "wizard", "token"):
        assert keyword in description, f"description never mentions {keyword!r}"
    # Says what it is NOT, so the judge can tell the three apart.
    assert "cremind setup" in description, "must disambiguate from first-run setup"
    assert "--adopt" in description, "the bare-profile rescue path is the other half"


def test_the_profile_doc_cedes_the_create_intent():
    """Both documents leading with "Create … profiles" is the failure mode.

    ``cremind profile create`` registers a name and nothing else. If its document
    keeps the recall sentence for "create a profile", the agent finds it first
    and does exactly what it did before this feature existed.
    """
    description = _description(PROFILE_DOC)
    value = description.split('"', 1)[1] if '"' in description else description
    assert not value.lstrip().lower().startswith("create"), (
        "[cli]cremind profile's description must not open with 'Create' — that "
        "is the wizard's query now"
    )
    assert "cremind profile wizard" in value, (
        "it must still point at the wizard, or the shell-creating command looks "
        "like the whole answer"
    )
    body = _text(PROFILE_DOC)
    assert "cremind profile wizard start" in body, (
        "the `create` section must name the command that finishes the job"
    )


def test_the_doc_carries_the_agent_hand_off():
    text = _text(DOC)
    for needle in (
        "read_file",          # the only affordance that renders a download
        "config_file",        # what to call it on
        "login_url",          # what the user needs to sign in
        "token_file",         # where the server keeps a copy
        "exec_shell_output",  # a slow finish comes back as a running process
        "cremind channels pair",
    ):
        assert needle in text, f"the hand-off section never mentions {needle!r}"
    assert "will not open" in text, (
        "the doc must say a Markdown link to the file path does not work — "
        "otherwise that is exactly what the agent will send"
    )


def test_the_doc_overrides_the_run_it_now_directive():
    text = _text(DOC)
    assert "exception" in text.lower(), (
        "the shared CLI directive says to run state-changing commands without "
        "asking; this family must state that it is the exception"
    )
    assert "end your turn" in text, "the turn break IS the question — say so"


def test_the_setup_doc_documents_the_payload_keys_the_wizard_sends():
    text = _text(SETUP_DOC)
    for key in ("user_config", "channel_configs", "adopt_existing"):
        assert key in text, (
            f"`{key}` is accepted by POST /api/config/setup but undocumented"
        )
