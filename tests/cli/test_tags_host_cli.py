"""`cremind tags host …` — this computer as a gateway computer of a Cremind elsewhere.

The enrollment itself is faked here (its flow runs for real in
tests/tags/test_tags_remote_host_e2e.py); what is pinned is the command's
contract with the Cremind app and with people: the link is read from stdin or
a file (never the command line), ``--events`` speaks JSON lines — the link
first, then ``approve``/``decline`` after the ``bound`` event — a decline or
a failure ends with a ``failed`` event and exit 1, and ``run`` on a computer
that was never set up exits 4 with what to do.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("typer")

from typer.testing import CliRunner  # noqa: E402

LINK = ("cremind://tags/setup?v=1&server=https%3A%2F%2Fcremind.example.org&session=6fb5bffe-fdb4-4192-860d-"
        "341115d9d060&token=" + "A" * 43)


@pytest.fixture
def fake(monkeypatch, tmp_path):
    import app.cli.commands.tags  # noqa: F401 - the parent group first: it registers its sub-apps
    import app.cli.commands.tags_host as cmd
    import app.tags.runtime.host.enroll as enroll_mod
    from app.tags.hosting.paths import RuntimePaths

    paths = RuntimePaths(tmp_path / ".tag-runtime")
    monkeypatch.setattr(cmd, "_paths", lambda: paths)
    state: dict = {"links": []}

    def enroll(link, paths_, *, approve, on_event, version, **_kw):
        state["links"].append(link)
        bound = SimpleNamespace(server_origin="https://cremind.example.org", profile_name="anna",
                                verification_phrase="amber orbit lantern tidal")
        on_event("bound", {"server": bound.server_origin, "profile": "anna", "computer": "LAPTOP-9",
                           "words": bound.verification_phrase, "state": "waiting_for_approval"})
        if not approve(bound):
            raise enroll_mod.EnrollError("declined", "Setting up this computer was declined.")
        on_event("approved", {})
        on_event("confirmed", {})
        enrollment = enroll_mod.Enrollment(
            server="https://cremind.example.org", host_id="h-1", credential_id="tagh_x", secret="s" * 43,
            profile="anna", profile_id="pid", host_name="LAPTOP-9", server_installation_id="i",
            enrolled_at="2026-09-28T10:00:00Z")
        on_event("enrolled", {"server": enrollment.server, "profile": "anna", "host_id": "h-1",
                              "computer": "LAPTOP-9"})
        return enrollment

    monkeypatch.setattr(enroll_mod, "enroll", enroll)
    state["paths"] = paths
    return state


def _run(*args, input: str | None = None):
    from app.cli.main import app

    return CliRunner().invoke(app, ["--token", "t", *args], input=input)


def _events(output: str) -> list[dict]:
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


def test_events_mode_reads_the_link_then_the_decision_from_stdin(fake):
    result = _run("tags", "host", "enroll", "--events", input=LINK + "\napprove\n")
    assert result.exit_code == 0, result.output
    assert fake["links"] == [LINK]
    events = _events(result.output)
    assert [e["event"] for e in events] == ["bound", "approved", "confirmed", "enrolled"]
    assert events[0]["words"] == "amber orbit lantern tidal" and events[0]["profile"] == "anna"
    assert "A" * 43 not in result.output and "s" * 43 not in result.output, "no secret is ever printed"


def test_a_decline_ends_with_a_failed_event(fake):
    result = _run("tags", "host", "enroll", "--events", input=LINK + "\ndecline\n")
    assert result.exit_code == 1
    events = _events(result.output)
    assert events[-1] == {"event": "failed", "code": "declined", "message": "Setting up this computer was declined."}


def test_people_compare_the_words_before_approving(fake, tmp_path):
    link_file = tmp_path / "link.txt"
    link_file.write_text(LINK + "\n", encoding="utf-8")
    result = _run("tags", "host", "enroll", "--link-file", str(link_file), input="n\n")
    assert result.exit_code == 1 and "amber orbit lantern tidal" in result.output
    result = _run("tags", "host", "enroll", "--link-file", str(link_file), "--yes")
    assert result.exit_code == 0, result.output
    assert "This computer is now a gateway computer of https://cremind.example.org for profile anna" in \
        " ".join(result.output.split())


def test_run_and_status_on_a_computer_that_was_never_set_up(fake):
    result = _run("tags", "host", "run")
    assert result.exit_code == 4 and "cremind tags host enroll" in result.output
    result = _run("--json", "tags", "host", "status")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["enrolled"] is False and doc["running"] is False and "components" in doc["readiness"]
    assert doc["migration"] == {"moved": 0, "failed": 0, "rolled_back": 0}
    assert "Cremind Connect" not in _run("tags", "host", "status").output
    result = _run("tags", "host", "forget", "--yes")
    assert result.exit_code == 0 and "not set up as a gateway computer" in result.output


def test_status_tells_what_moved_in_from_cremind_connect(fake):
    journals = fake["paths"].migration_dir
    journals.mkdir(parents=True)
    for worker_id, state in (("w-1", "complete"), ("w-2", "complete"), ("w-3", "in_progress")):
        (journals / f"{worker_id}.json").write_text(json.dumps({"state": state}), encoding="utf-8")
    result = _run("tags", "host", "status")
    assert result.exit_code == 0, result.output
    assert "Moved in: 2 gateway(s) from Cremind Connect, 1 to retry at the next start" in \
        " ".join(result.output.split())
