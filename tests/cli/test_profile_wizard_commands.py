"""`cremind profile wizard` end to end, with the server stubbed out.

The wizard is driven by an agent, one command per conversational turn, so its
*output* is as much a part of the feature as its behaviour: what the agent reads
after `start` decides whether it asks the user anything, and what it reads after
`finish` decides whether the user ever sees their configuration file.

Three things are therefore pinned here as hard as the state changes are.

**The turn break.** Every command that prints questions ends with a STOP line,
and `set` with no answers refuses instead of storing nothing. Without both, the
documented directive ("run state-changing commands when asked") leads the agent
straight to inventing an LLM provider and a key.

**The hand-off.** `finish` prints a login URL, a token, and a path the
`system_file` tool may read — plus instructions saying to use `read_file` on it
and *not* to write a Markdown link, which does not open. If that block goes, the
last mile of the feature silently stops working.

**Failing softly after the profile exists.** Once the POST succeeds the profile
and its token are real. Anything after that — the token file, the export, the
result record — reports its own failure rather than failing the command and
leaving the user believing nothing happened.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from app.cli import wizard_draft
from app.cli.client.config import ConfigExport
from app.cli.client.me import Me
from app.cli.client.setup import SetupResponse

_SNAPSHOT = {
    "profile": "javis",
    "token": "eyJ-new",
    "tokenExpiresAt": "2026-10-14T09:12:33Z",
    "loginUrl": "http://localhost:1515/#/login/javis",
    "tokenFile": "/home/li/.cremind/tokens/javis.token",
}


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def sysdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(tmp_path))
    monkeypatch.setenv("CREMIND_TOKEN", "admin-token")
    monkeypatch.setenv("CREMIND_SERVER", "http://localhost:1112")
    monkeypatch.delenv("CREMIND_PROFILE", raising=False)
    return tmp_path


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Stub every client function on its SOURCE module.

    The commands import them inside their bodies, so patching the command
    module's namespace would silently do nothing and the test would hit the
    network.
    """
    import app.cli.client._base as base
    import app.cli.client.channels as channels_client
    import app.cli.client.config as config_client
    import app.cli.client.llm as llm_client
    import app.cli.client.me as me_client
    import app.cli.client.setup as setup_client
    import app.cli.client.tools as tools_client
    import app.cli.session as session

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    state = {
        "profile": "admin",
        "setup_status": {"setup_complete": True, "profile_exists": False},
        "setup_response": {
            "success": True, "token": "eyJ-new", "expires_at": "2026-10-14T09:12:33Z",
            "profile": "javis", "warnings": [], "channels": [], "channel_errors": [],
        },
        "posted": [],
        "exports": [],
        "export_error": None,
        "tokens_written": [],
    }

    async def _get_me(_client):
        return Me.from_dict({"profile": state["profile"], "sub": state["profile"]})

    async def _get_setup_status(_client, _profile=None):
        return state["setup_status"]

    async def _complete_setup(_client, body):
        state["posted"].append(body)
        return SetupResponse.from_dict(state["setup_response"])

    async def _export_config(_client, fmt="md", *, agent_url=None, pending_https=False):
        state["exports"].append({"format": fmt, "agent_url": agent_url, "pending_https": pending_https})
        if state["export_error"] is not None:
            raise state["export_error"]
        content = (
            json.dumps(_SNAPSHOT).encode() if fmt == "json"
            else f"# Cremind Configuration ({fmt})".encode()
        )
        return ConfigExport(
            filename=f"cremind-javis-config.{fmt}", content=content,
            content_type="text/markdown", scope="profile",
        )

    async def _list_llm_providers(_client):
        return [{
            "name": "anthropic",
            "auth_methods": [{
                "id": "api_key", "kind": "api_key", "is_default": True,
                "fields": {"api_key": {"required": True}},
            }],
        }]

    async def _get_provider_models(_client, _provider, auth_method=None):
        return {"models": [{"id": "claude-sonnet-5"}]}

    async def _list_tools(_client, _type_filter=""):
        return [{"tool_id": "web_search", "required_fields": {}}]

    async def _get_channel_catalog(_client):
        return {"telegram": {"channel": {"modes": [
            {"id": "bot", "fields": {"bot_token": {"required": True}}},
            {"id": "userbot", "setup_kind": "qr", "fields": {}},
        ]}}}

    async def _get_config_schema(_client):
        return {"groups": {"memory": {"fields": {"enabled": {"type": "boolean"}}}}}

    def _write_token(profile, token):
        state["tokens_written"].append((profile, token))
        return tmp_path / "tokens" / f"{profile}.token"

    monkeypatch.setattr(base, "Client", lambda _cfg, **_kw: _FakeClient())
    monkeypatch.setattr(me_client, "get_me", _get_me)
    monkeypatch.setattr(setup_client, "get_setup_status", _get_setup_status)
    monkeypatch.setattr(setup_client, "complete_setup", _complete_setup)
    monkeypatch.setattr(config_client, "export_config", _export_config)
    monkeypatch.setattr(config_client, "get_config_schema", _get_config_schema)
    monkeypatch.setattr(llm_client, "list_llm_providers", _list_llm_providers)
    monkeypatch.setattr(llm_client, "get_provider_models", _get_provider_models)
    monkeypatch.setattr(tools_client, "list_tools", _list_tools)
    monkeypatch.setattr(channels_client, "get_channel_catalog", _get_channel_catalog)
    monkeypatch.setattr(session, "write_token", _write_token)
    return state


def _run(runner: CliRunner, monkeypatch: pytest.MonkeyPatch, args: list[str]):
    """Invoke the real CLI. ``sys.argv`` is mirrored because the root callback
    reads it to decide whether to resolve a profile."""
    from app.cli.main import app

    monkeypatch.setattr(sys, "argv", ["cremind", *args])
    return runner.invoke(app, args)


def _start(runner, monkeypatch, *extra: str):
    return _run(runner, monkeypatch, ["profile", "wizard", "start", "javis", *extra])


# ── start ────────────────────────────────────────────────────────────────


def test_start_writes_a_draft_and_asks_the_first_step(runner, monkeypatch, sysdir, server) -> None:
    result = _start(runner, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "next_step" in result.stdout and "llm" in result.stdout
    assert "cremind llm providers list" in result.stdout, "it must name where to get real values"
    assert wizard_draft.load("admin", "javis") is not None


def test_every_question_block_ends_by_telling_the_agent_to_stop(runner, monkeypatch, sysdir, server) -> None:
    """The shared CLI directive says to run state-changing commands as soon as
    they are asked for. Every step here needs a value only the user has."""
    result = _start(runner, monkeypatch)
    assert "STOP:" in result.stdout
    assert "do not choose values for them" in result.stdout


def test_an_existing_profile_is_refused_with_the_flag_that_fixes_it(runner, monkeypatch, sysdir, server) -> None:
    server["setup_status"] = {"setup_complete": True, "profile_exists": True}
    result = _start(runner, monkeypatch)
    assert result.exit_code == 1
    assert "--adopt" in result.stderr
    assert wizard_draft.load("admin", "javis") is None


def test_adopt_on_a_profile_that_is_not_there_is_refused(runner, monkeypatch, sysdir, server) -> None:
    result = _start(runner, monkeypatch, "--adopt")
    assert result.exit_code == 1
    assert "drop --adopt" in result.stderr


def test_a_server_that_has_never_been_set_up_is_sent_to_the_right_command(runner, monkeypatch, sysdir, server) -> None:
    server["setup_status"] = {"setup_complete": False}
    result = _start(runner, monkeypatch)
    assert result.exit_code == 1
    assert "cremind setup complete" in result.stderr


def test_a_non_admin_token_is_refused_before_any_questions(runner, monkeypatch, sysdir, server) -> None:
    """Setup refuses a non-admin for every profile after the first, so asking
    four steps' worth of questions first would waste the user's time."""
    server["profile"] = "bob"
    result = _start(runner, monkeypatch)
    assert result.exit_code == 1
    assert "runs as admin" in result.stderr
    assert "cremind auth show --profile admin" in result.stderr


def test_the_admin_profile_is_never_a_wizard_target(runner, monkeypatch, sysdir, server) -> None:
    result = _run(runner, monkeypatch, ["profile", "wizard", "start", "admin"])
    assert result.exit_code == 1
    assert "cremind setup reconfigure" in result.stderr


def test_a_second_start_does_not_silently_discard_the_first(runner, monkeypatch, sysdir, server) -> None:
    _start(runner, monkeypatch)
    result = _start(runner, monkeypatch)
    assert result.exit_code == 1
    assert "cremind profile wizard cancel javis" in result.stderr


# ── set ──────────────────────────────────────────────────────────────────


def test_set_records_the_answers_and_asks_the_next_step(runner, monkeypatch, sysdir, server) -> None:
    _start(runner, monkeypatch)
    result = _run(runner, monkeypatch, [
        "profile", "wizard", "set", "javis", "llm",
        "provider=anthropic", "model=claude-sonnet-5", "api_key=sk-ant",
    ])
    assert result.exit_code == 0, result.output
    draft = wizard_draft.load("admin", "javis")
    assert draft.payload("llm")["model_group.high"] == "anthropic/claude-sonnet-5"
    assert "step tools" in result.stdout


def test_set_with_no_answers_refuses_instead_of_doing_nothing(runner, monkeypatch, sysdir, server) -> None:
    """This is the command an agent reaches for when it has not actually put
    the questions to anyone. A no-op would look like progress."""
    _start(runner, monkeypatch)
    result = _run(runner, monkeypatch, ["profile", "wizard", "set", "javis", "llm"])
    assert result.exit_code == 1
    assert "STOP:" in result.stdout
    assert "nothing to set" in result.stderr


def test_answers_merge_across_two_turns(runner, monkeypatch, sysdir, server) -> None:
    _start(runner, monkeypatch)
    _run(runner, monkeypatch, [
        "profile", "wizard", "set", "javis", "llm", "provider=anthropic", "model=claude-sonnet-5",
    ])
    _run(runner, monkeypatch, ["profile", "wizard", "set", "javis", "llm", "api_key=sk-ant"])
    payload = wizard_draft.load("admin", "javis").payload("llm")
    assert payload["model_group.high"] == "anthropic/claude-sonnet-5"
    assert payload["anthropic.api_key"] == "sk-ant"


def test_a_rejected_answer_leaves_the_draft_untouched(runner, monkeypatch, sysdir, server) -> None:
    _start(runner, monkeypatch)
    result = _run(runner, monkeypatch, [
        "profile", "wizard", "set", "javis", "llm", "provider=nope", "model=x",
    ])
    assert result.exit_code == 1
    assert "cremind llm providers list" in result.stderr
    assert wizard_draft.load("admin", "javis").status("llm") == wizard_draft.STATUS_PENDING


def test_no_validate_stores_an_answer_the_catalog_does_not_know(runner, monkeypatch, sysdir, server) -> None:
    """A model published after this server's catalogue was built is a real
    case; refusing it outright would be worse than a warning."""
    _start(runner, monkeypatch)
    result = _run(runner, monkeypatch, [
        "profile", "wizard", "set", "javis", "llm",
        "provider=anthropic", "model=claude-tomorrow", "--no-validate",
    ])
    assert result.exit_code == 0, result.output
    assert wizard_draft.load("admin", "javis").payload("llm")["model_group.high"] == (
        "anthropic/claude-tomorrow"
    )


def test_a_channel_is_recorded_with_its_required_field(runner, monkeypatch, sysdir, server) -> None:
    _start(runner, monkeypatch)
    result = _run(runner, monkeypatch, [
        "profile", "wizard", "set", "javis", "channels",
        "channel_type=telegram", "mode=bot", "bot_token=123:abc",
    ])
    assert result.exit_code == 0, result.output
    entries = wizard_draft.load("admin", "javis").payload("channels")
    assert entries[0]["config"]["bot_token"] == "123:abc"


def test_a_json_payload_file_replaces_the_step(runner, monkeypatch, sysdir, server, tmp_path) -> None:
    _start(runner, monkeypatch)
    path = tmp_path / "channels.json"
    path.write_text(json.dumps([
        {"channel_type": "telegram", "mode": "bot", "config": {"bot_token": "x"}},
    ]), encoding="utf-8")
    result = _run(runner, monkeypatch, [
        "profile", "wizard", "set", "javis", "channels", "--json-file", str(path),
    ])
    assert result.exit_code == 0, result.output
    assert wizard_draft.load("admin", "javis").payload("channels")[0]["mode"] == "bot"


def test_an_unknown_step_lists_the_real_ones(runner, monkeypatch, sysdir, server) -> None:
    _start(runner, monkeypatch)
    result = _run(runner, monkeypatch, ["profile", "wizard", "set", "javis", "nope", "a=b"])
    assert result.exit_code == 1
    assert "llm, tools, memory, channels" in result.stderr


# ── skip ─────────────────────────────────────────────────────────────────


def test_skipping_the_llm_step_says_what_it_costs(runner, monkeypatch, sysdir, server) -> None:
    """A profile with no model is agent-dead and the failure is invisible: a
    group chat swallows the error and simply never replies."""
    _start(runner, monkeypatch)
    result = _run(runner, monkeypatch, ["profile", "wizard", "skip", "javis", "llm"])
    assert result.exit_code == 0, result.output
    assert "cannot answer anything" in result.stderr
    assert wizard_draft.load("admin", "javis").status("llm") == wizard_draft.STATUS_SKIPPED


# ── status ───────────────────────────────────────────────────────────────


def test_status_masks_the_credential_it_is_holding(runner, monkeypatch, sysdir, server) -> None:
    _start(runner, monkeypatch)
    _run(runner, monkeypatch, [
        "profile", "wizard", "set", "javis", "llm", "provider=anthropic", "api_key=sk-ant-secret",
    ])
    result = _run(runner, monkeypatch, ["--json", "profile", "wizard", "status", "javis"])
    assert result.exit_code == 0, result.output
    assert "sk-ant-secret" not in result.stdout
    payload = json.loads(result.stdout)
    assert payload["steps"]["llm"]["payload"]["anthropic.api_key"] == wizard_draft.MASK


def test_status_can_be_asked_about_one_step(runner, monkeypatch, sysdir, server) -> None:
    _start(runner, monkeypatch)
    result = _run(runner, monkeypatch, [
        "profile", "wizard", "status", "javis", "--step", "channels",
    ])
    assert result.exit_code == 0, result.output
    assert "step channels" in result.stdout


def test_status_without_a_draft_says_how_to_begin(runner, monkeypatch, sysdir, server) -> None:
    result = _run(runner, monkeypatch, ["profile", "wizard", "status", "javis"])
    assert result.exit_code == 1
    assert "cremind profile wizard start javis" in result.stderr


# ── finish ───────────────────────────────────────────────────────────────


def _ready(runner, monkeypatch) -> None:
    _start(runner, monkeypatch)
    _run(runner, monkeypatch, [
        "profile", "wizard", "set", "javis", "llm",
        "provider=anthropic", "model=claude-sonnet-5", "api_key=sk-ant",
    ])
    for step in ("tools", "memory", "channels"):
        _run(runner, monkeypatch, ["profile", "wizard", "skip", "javis", step])


def test_finish_posts_exactly_what_was_answered(runner, monkeypatch, sysdir, server) -> None:
    _ready(runner, monkeypatch)
    result = _run(runner, monkeypatch, ["profile", "wizard", "finish", "javis"])
    assert result.exit_code == 0, result.output
    assert server["posted"] == [{
        "profile": "javis",
        "llm_config": {
            "default_provider": "anthropic",
            "model_group.high": "anthropic/claude-sonnet-5",
            "anthropic.api_key": "sk-ant",
        },
    }]


def test_finish_hands_over_the_token_the_login_url_and_the_file(runner, monkeypatch, sysdir, server) -> None:
    _ready(runner, monkeypatch)
    result = _run(runner, monkeypatch, ["profile", "wizard", "finish", "javis"])
    assert result.exit_code == 0, result.output
    assert "eyJ-new" in result.stdout
    assert "http://localhost:1515/#/login/javis" in result.stdout
    assert "config_file" in result.stdout
    assert server["tokens_written"] == [("javis", "eyJ-new")]

    written = sysdir / "admin" / "exports" / "cremind-javis-config.md"
    assert written.exists(), "the file must land where system_file can read it"
    assert wizard_draft.load("admin", "javis") is None, "the draft is spent"


def test_finish_tells_the_agent_how_to_make_the_file_downloadable(runner, monkeypatch, sysdir, server) -> None:
    """Only a tool result carrying the file renders a download; a Markdown link
    to the path 401s, because file routes take the token in a header."""
    _ready(runner, monkeypatch)
    result = _run(runner, monkeypatch, ["profile", "wizard", "finish", "javis"])
    assert "Next steps for an agent:" in result.stderr
    assert "read_file" in result.stderr
    assert "will not open" in result.stderr
    assert "paste the" in result.stderr


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_the_written_configuration_file_is_private(runner, monkeypatch, sysdir, server) -> None:
    _ready(runner, monkeypatch)
    _run(runner, monkeypatch, ["profile", "wizard", "finish", "javis"])
    written = sysdir / "admin" / "exports" / "cremind-javis-config.md"
    assert oct(written.stat().st_mode & 0o777) == "0o600"


def test_finish_refuses_while_a_step_is_unanswered(runner, monkeypatch, sysdir, server) -> None:
    _start(runner, monkeypatch)
    result = _run(runner, monkeypatch, ["profile", "wizard", "finish", "javis"])
    assert result.exit_code == 1
    assert "still unanswered" in result.stderr
    assert server["posted"] == []


def test_finish_refuses_an_llm_step_that_has_no_main_model(runner, monkeypatch, sysdir, server) -> None:
    """Skipping is a decision; answering halfway is an accident, and the result
    is a profile that cannot answer anything."""
    _start(runner, monkeypatch)
    _run(runner, monkeypatch, ["profile", "wizard", "set", "javis", "llm", "provider=anthropic"])
    for step in ("tools", "memory", "channels"):
        _run(runner, monkeypatch, ["profile", "wizard", "skip", "javis", step])
    result = _run(runner, monkeypatch, ["profile", "wizard", "finish", "javis"])
    assert result.exit_code == 1
    assert "incomplete" in result.stderr
    assert server["posted"] == []


def test_finish_carries_the_agent_url_override_into_the_export(runner, monkeypatch, sysdir, server) -> None:
    """On the documented development setup APP_URL names the internal bind, so
    the login URL derived from it opens nothing."""
    _ready(runner, monkeypatch)
    result = _run(runner, monkeypatch, [
        "profile", "wizard", "finish", "javis", "--agent-url", "http://localhost:1515",
    ])
    assert result.exit_code == 0, result.output
    assert all(e["agent_url"] == "http://localhost:1515" for e in server["exports"])


def test_no_file_skips_the_export_but_still_reports_the_login(runner, monkeypatch, sysdir, server) -> None:
    _ready(runner, monkeypatch)
    result = _run(runner, monkeypatch, ["profile", "wizard", "finish", "javis", "--no-file"])
    assert result.exit_code == 0, result.output
    assert [e["format"] for e in server["exports"]] == ["json"], "still asked for the login URL"
    assert not (sysdir / "admin" / "exports").exists()


def test_a_failed_export_warns_but_does_not_fail_the_command(runner, monkeypatch, sysdir, server) -> None:
    """The profile and its token already exist by then. Exiting non-zero would
    tell the user nothing happened, and they would try again into a 409."""
    from app.cli.client._base import APIError

    _ready(runner, monkeypatch)
    server["export_error"] = APIError(status=500, body="boom")
    result = _run(runner, monkeypatch, ["profile", "wizard", "finish", "javis"])
    assert result.exit_code == 0, result.output
    assert "eyJ-new" in result.stdout, "the token is still handed over"
    assert "Warning:" in result.stderr


def test_a_channel_needing_pairing_prints_the_command_to_run(runner, monkeypatch, sysdir, server) -> None:
    _ready(runner, monkeypatch)
    server["setup_response"] = {
        **server["setup_response"],
        "channels": [{"id": "ch_1", "channel_type": "telegram", "mode": "userbot"}],
    }
    result = _run(runner, monkeypatch, ["profile", "wizard", "finish", "javis"])
    assert result.exit_code == 0, result.output
    assert "needs pairing" in result.stdout
    assert "channels pair ch_1" in result.stderr


def test_setup_warnings_reach_the_operator(runner, monkeypatch, sysdir, server) -> None:
    _ready(runner, monkeypatch)
    server["setup_response"] = {
        **server["setup_response"],
        "warnings": [{"code": "adopted_existing", "message": "already existed"}],
        "channel_errors": [{"channel_type": "slack", "error": "bad token"}],
    }
    result = _run(runner, monkeypatch, ["profile", "wizard", "finish", "javis"])
    assert "Warning: already existed" in result.stderr
    assert "channel slack: bad token" in result.stderr


def test_finish_json_carries_everything_the_table_does(runner, monkeypatch, sysdir, server) -> None:
    _ready(runner, monkeypatch)
    result = _run(runner, monkeypatch, ["--json", "profile", "wizard", "finish", "javis"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["token"] == "eyJ-new"
    assert payload["login_url"] == "http://localhost:1515/#/login/javis"
    assert payload["config_file"].endswith("cremind-javis-config.md")
    assert payload["skipped"] == ["tools", "memory", "channels"]
    assert "read_file" in payload["agent_next_steps"]


def test_status_after_finish_reprints_the_result(runner, monkeypatch, sysdir, server) -> None:
    """``finish`` can run for minutes, which is long enough for an agent's
    shell capture to come back as a running process instead of output. The
    token and the file path must be recoverable afterwards."""
    _ready(runner, monkeypatch)
    _run(runner, monkeypatch, ["profile", "wizard", "finish", "javis"])
    result = _run(runner, monkeypatch, ["--json", "profile", "wizard", "status", "javis"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["token"] == "eyJ-new"
    assert payload["login_url"] == "http://localhost:1515/#/login/javis"


def test_adopting_sends_the_flag(runner, monkeypatch, sysdir, server) -> None:
    server["setup_status"] = {"setup_complete": True, "profile_exists": True}
    _start(runner, monkeypatch, "--adopt")
    _run(runner, monkeypatch, [
        "profile", "wizard", "set", "javis", "llm",
        "provider=anthropic", "model=claude-sonnet-5", "api_key=sk-ant",
    ])
    for step in ("tools", "memory", "channels"):
        _run(runner, monkeypatch, ["profile", "wizard", "skip", "javis", step])
    result = _run(runner, monkeypatch, ["profile", "wizard", "finish", "javis"])
    assert result.exit_code == 0, result.output
    assert server["posted"][0]["adopt_existing"] is True


def test_a_draft_that_disagrees_with_the_server_is_refused(runner, monkeypatch, sysdir, server) -> None:
    """Someone created the profile while the wizard was mid-flight; posting now
    would 409 after the questions were already asked."""
    _ready(runner, monkeypatch)
    server["setup_status"] = {"setup_complete": True, "profile_exists": True}
    result = _run(runner, monkeypatch, ["profile", "wizard", "finish", "javis"])
    assert result.exit_code == 1
    assert "--adopt" in result.stderr
    assert server["posted"] == []


# ── cancel ───────────────────────────────────────────────────────────────


def test_cancel_removes_the_draft_and_creates_nothing(runner, monkeypatch, sysdir, server) -> None:
    _start(runner, monkeypatch)
    result = _run(runner, monkeypatch, ["profile", "wizard", "cancel", "javis"])
    assert result.exit_code == 0, result.output
    assert wizard_draft.load("admin", "javis") is None
    assert server["posted"] == []


def test_cancelling_nothing_says_so(runner, monkeypatch, sysdir, server) -> None:
    result = _run(runner, monkeypatch, ["profile", "wizard", "cancel", "javis"])
    assert result.exit_code == 1
    assert "no wizard draft" in result.stderr


# ── the bare create command ──────────────────────────────────────────────


def test_profile_create_stays_pipe_clean_and_points_at_the_wizard(
    runner, monkeypatch, sysdir,
) -> None:
    """What it makes is a shell — no model, no token. stdout stays just the
    name so `$(cremind profile create x)` keeps working."""
    import app.cli.client._base as base
    import app.cli.client.profiles as profiles_client

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    async def _create(_client, _name):
        return None

    monkeypatch.setattr(base, "Client", lambda _cfg, **_kw: _FakeClient())
    monkeypatch.setattr(profiles_client, "create_profile", _create)

    result = _run(runner, monkeypatch, ["profile", "create", "javis"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "javis\n"
    assert "cremind profile wizard start javis --adopt" in result.stderr
