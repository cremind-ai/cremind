"""Doc/code drift pins for the four event-rule CLI docs.

CLAUDE.md mandates that a CLI command and its bundled doc move in lockstep, and
these four are the ones the "every rule reports back" change rewrote: every one
of them used to state, as fact, that a standing rule's runs never reach the
conversation that registered it. A doc that says that is worse than no doc —
the assistant retrieves it and repeats it to the user.

So each doc gets the same two guards: the command tree is walked (every
subcommand and long flag must appear), and the sentences the feature turned
false are pinned as ABSENT while the ones that replaced them are pinned as
present.

The description length is checked as "substantial" rather than against the
250-350 window ``document.md`` recommends: these four already ran 800-1200
characters before this change, and trimming them to the window is a separate
piece of work from keeping them true.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

pytest.importorskip("typer")

_BUNDLED = Path(__file__).resolve().parents[2] / "app" / "documents" / "bundled"


def _doc_text(name: str) -> str:
    doc = _BUNDLED / name
    assert doc.exists(), f"missing bundled doc: {name}"
    return doc.read_text(encoding="utf-8")


def _description(name: str) -> str:
    lines = _doc_text(name).splitlines()
    assert lines[0] == "---", "frontmatter must open with --- on the first line"
    closing = next(i for i, line in enumerate(lines[1:], start=1) if line == "---")
    body = "\n".join(lines[1:closing])
    assert body.startswith('description: "'), "description must be present and quoted"
    return body[body.index('"') + 1 : body.rindex('"')]


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
        assert sub_name, f"a sub-app of {prefix!r} has no name"
        _walk(sub_app, f"{prefix} {sub_name}", text, documented)


_DOCS = {
    "skill-events": "[cli]cremind skill-events.md",
    "file-watchers": "[cli]cremind file-watchers.md",
    "calendar": "[cli]cremind calendar.md",
    "event-runs": "[cli]cremind event-runs.md",
}


@pytest.mark.parametrize("doc", sorted(_DOCS.values()))
def test_the_description_is_substantial(doc):
    """It is the ONLY text embedded for retrieval, so it cannot be a stub."""
    assert len(_description(doc)) > 200


@pytest.mark.parametrize("doc", sorted(_DOCS.values()))
def test_no_doc_still_claims_a_rule_reports_nothing_back(doc):
    """The sentence the feature made false, in each of its old spellings."""
    text = " ".join(_doc_text(doc).lower().split())
    for stale in (
        "nothing is reported back",
        "never report back",
        "do not come back to this chat",
        "are never tasks",
    ):
        assert stale not in text, f"{doc} still claims: {stale!r}"


def test_every_skill_event_command_is_documented():
    from app.cli.commands.skill_events import skill_events_app

    documented: set[str] = set()
    _walk(skill_events_app, "cremind skill-events",
          _doc_text(_DOCS["skill-events"]), documented)
    # pause/resume were missing from the doc entirely until this change.
    assert {"cremind skill-events pause", "cremind skill-events resume"} <= documented


def test_every_file_watcher_command_is_documented():
    from app.cli.commands.file_watchers import file_watchers_app

    documented: set[str] = set()
    _walk(file_watchers_app, "cremind file-watchers",
          _doc_text(_DOCS["file-watchers"]), documented)
    assert {"cremind file-watchers pause", "cremind file-watchers resume"} <= documented


def test_every_calendar_command_is_documented():
    from app.cli.commands.calendar import calendar_app

    documented: set[str] = set()
    _walk(calendar_app, "cremind calendar", _doc_text(_DOCS["calendar"]), documented)
    assert "cremind calendar schedule list" in documented


def test_every_event_run_command_is_documented():
    from app.cli.commands.event_runs import event_runs_app

    documented: set[str] = set()
    _walk(event_runs_app, "cremind event-runs",
          _doc_text(_DOCS["event-runs"]), documented)
    assert "cremind event-runs list" in documented


def test_the_skill_events_doc_teaches_both_shapes():
    """A reader has to be able to tell "wait once" from "report forever"."""
    text = " ".join(_doc_text(_DOCS["skill-events"]).lower().split())
    assert "one-shot" in text
    assert "standing" in text
    assert "report" in text


def test_the_skill_events_doc_warns_that_simulate_sends_real_messages():
    """It fans out past the id in the URL, and every run now posts a result."""
    text = " ".join(_doc_text(_DOCS["skill-events"]).lower().split())
    assert "not a dry run" in text


def test_the_event_runs_doc_explains_when_delivered_is_blank():
    """It used to mean "an ordinary run"; now it means "nowhere to report"."""
    text = " ".join(_doc_text(_DOCS["event-runs"]).lower().split())
    assert "blank for an ordinary event run" not in text
    assert "delivered" in text
    assert "skipped" in text
