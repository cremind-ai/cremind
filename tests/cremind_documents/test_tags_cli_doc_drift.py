"""Doc/code drift pin for `[cli]cremind tags.md` and `[cli]cremind tags hardware.md`.

CLAUDE.md mandates that a CLI command and its bundled doc move in lockstep.
This walks the nested Typer groups (`tags`, `tags deliveries`, `tags
credentials`, `tags hardware`) so a new subcommand or flag cannot land
undocumented. The admin's hardware commands live in their own doc: a profile
asking "pin a note on my desk tag" and an admin asking "give this tag to bob"
are different questions, and each doc stays small enough to be delivered whole.

The ``description`` is the only text embedded into ``cremind_documentation_search``,
so it must carry what users ask ("e-paper", "pin a note", "claim a tag") and
say which of the two docs is the other one. The lists the docs spell out —
card kinds, hardware command kinds — are pinned to the server's own.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

pytest.importorskip("typer")

BUNDLED = Path(__file__).resolve().parents[2] / "app" / "cremind_documents" / "bundled"
DOC = BUNDLED / "[cli]cremind tags.md"
HARDWARE_DOC = BUNDLED / "[cli]cremind tags hardware.md"
ALL_DOCS = [DOC, HARDWARE_DOC]
HARDWARE_GROUP = "hardware"


def _doc_text(doc: Path = DOC) -> str:
    assert doc.exists(), f"missing bundled doc: {doc.name}"
    return doc.read_text(encoding="utf-8")


def _description(doc: Path = DOC) -> str:
    return _doc_text(doc).split("---")[1]


def _walk(app, prefix: str):
    """Yield (full command path, callback) for every command, depth-first."""
    for command in app.registered_commands:
        name = command.name or command.callback.__name__
        yield f"{prefix} {name}", command.callback
    for group in app.registered_groups:
        yield from _walk(group.typer_instance, f"{prefix} {group.name}")


def _doc_for(path: str) -> Path:
    return HARDWARE_DOC if path.split(" ")[2] == HARDWARE_GROUP else DOC


@pytest.mark.parametrize("doc", ALL_DOCS, ids=lambda d: d.name)
def test_frontmatter_is_well_formed(doc):
    lines = _doc_text(doc).splitlines()
    assert lines[0] == "---"
    closing = next(i for i, line in enumerate(lines[1:], start=1) if line == "---")
    body = "\n".join(lines[1:closing])
    assert body.startswith('description: "') and body.rstrip().endswith('"')
    # The embedder and the judge both cap the description at 1200 characters;
    # document.md asks for about two sentences.
    assert 200 < len(body) <= 1200
    assert len(body) <= 450, "keep the description to about two sentences (document.md)"


def test_every_subcommand_and_flag_is_documented_in_the_right_doc():
    import typer

    from app.cli.commands.tags import tags_app

    texts = {doc: _doc_text(doc) for doc in ALL_DOCS}
    seen = []
    for path, callback in _walk(tags_app, "cremind tags"):
        seen.append(path)
        text = texts[_doc_for(path)]
        assert path in text, f"subcommand `{path}` is undocumented in {_doc_for(path).name}"
        for param in inspect.signature(callback).parameters.values():
            default = param.default
            if not isinstance(default, typer.models.OptionInfo):
                continue
            for decl in default.param_decls or []:
                for flag in decl.split("/"):
                    if flag.startswith("--"):
                        assert flag in text, f"flag {flag} of `{path}` is undocumented"
    for expected in ("cremind tags list", "cremind tags display", "cremind tags deliveries list",
                     "cremind tags credentials create", "cremind tags hardware claim",
                     "cremind tags hardware set-defaults"):
        assert expected in seen


def test_the_descriptions_carry_what_users_ask_and_point_at_each_other():
    description = _description().lower()
    for keyword in ("cremind tag", "e-paper", "pin a note", "clear", "preview", "battery",
                    "delivery history", "content credential", "`cremind tags hardware`"):
        assert keyword in description, f"tags doc description never mentions {keyword!r}"
    hardware = _description(HARDWARE_DOC).lower()
    for keyword in ("cremind tag hardware", "admin", "companion", "credential", "bridges", "claim",
                    "release", "forget", "defaults", "`cremind tags`"):
        assert keyword in hardware, f"hardware doc description never mentions {keyword!r}"


def test_the_docs_name_their_web_ui_places():
    assert "Sidebar → Tags" in _doc_text() and "Settings → Tags" in _doc_text()
    assert "Settings → Tags" in _doc_text(HARDWARE_DOC)


@pytest.mark.parametrize("doc", ALL_DOCS, ids=lambda d: d.name)
def test_every_json_example_runs_the_tags_group_with_the_root_flag(doc):
    groups = re.findall(r"cremind --json ([a-z][a-z-]*)", _doc_text(doc))
    assert groups, f"{doc.name} shows no --json example"
    assert set(groups) == {"tags"}, f"{doc.name}: --json examples run {sorted(set(groups))}"


def test_the_docs_explain_what_a_script_cannot_guess():
    text = _doc_text()
    for word in ("otp_refused", "clear_pending", "device_not_found", "no_preview", "already_terminal",
                 "invalid_settings", "--body-file", "stdin", "cremind-tag connect", "**once**",
                 "milliseconds", "`own`", "KIND=inherit"):
        assert word in text, f"tags doc never explains {word!r}"
    hardware = _doc_text(HARDWARE_DOC)
    for word in ("403", "cremind -p admin", "bridge_required", "bridge_not_found", "unknown_profile",
                 "use_tag_endpoint", "--yes", "exits 2", "cremind-tag connect", "**once**",
                 "cremind tags credentials create", "milliseconds"):
        assert word in hardware, f"hardware doc never explains {word!r}"


def test_every_hint_the_cli_prints_is_a_code_the_docs_explain():
    """The CLI prints a hint under these error codes; the docs' troubleshooting
    must name the same codes, so neither side drifts alone."""
    from app.cli.commands import tags as cmd

    documented = _doc_text() + _doc_text(HARDWARE_DOC)
    for code in ("otp_refused", "clear_pending", "device_not_found", "no_preview", "already_terminal",
                 "bridge_required", "bridge_not_found", "unknown_profile", "use_tag_endpoint",
                 "unknown_command"):
        assert code in cmd._HINTS or code in cmd._ADMIN_HINTS, code
        assert code in documented, code


# ── lists the docs and the CLI spell out, pinned to the server ──────────────


def test_the_hardware_command_kinds_match_the_server():
    from app.cli.commands import tags as cmd
    from app.tags.service import ADMIN_COMMAND_KINDS

    assert set(cmd._COMMAND_KINDS) == set(ADMIN_COMMAND_KINDS)
    text = _doc_text(HARDWARE_DOC)
    for kind in ADMIN_COMMAND_KINDS:
        assert f"`{kind}`" in text, f"hardware doc never lists the {kind} command"
        assert kind in cmd._ADMIN_HINTS["unknown_command"], kind


def test_the_card_kinds_listed_match_the_server():
    from app.tags.routing import ROUTABLE_KINDS

    text = _doc_text()
    section = text[text.index("Card kinds:"):]
    section = section[: section.index("\n\n")]
    listed = set(re.findall(r"`([a-z_]+)`", section))
    assert listed == set(ROUTABLE_KINDS)


def test_the_documented_limits_match_the_server():
    from app.tags import service

    text = _doc_text()
    assert "at most 120 characters" in text and "at most 400 characters" in text
    assert service.PINNED_TTL_MIN_S == 60 and service.PINNED_TTL_MAX_S == 7 * 24 * 3600
    assert "1 minute to 7 days" in text
    assert service.PINNED_TTL_DEFAULT_S == 24 * 3600 and "| `--ttl` | 1 day |" in text
