"""`cremind setup complete` surfaces the server's non-fatal warnings.

``setup complete`` is the headless flow — the one the backend deliberately
does NOT reject over a missing main model, because bootstrapping now and
choosing the model later is legitimate. That makes it the flow most in need of
the warning: without it, a scripted install mints a token for a profile that
will ignore every message it ever receives, and says nothing.

The client dataclass drops any field it doesn't declare, so both halves are
pinned here: the parse, and what the command prints.
"""

from __future__ import annotations

import json
import sys

import pytest
from typer.testing import CliRunner

from app.cli.client.setup import SetupResponse

_WARNING = {
    "code": "no_main_model",
    "message": "Profile 'bobo' has no main model, so it cannot answer anything yet.",
}
_RESPONSE = {
    "success": True,
    "token": "eyJhbGciOi",
    "expires_at": "2026-06-01T14:00:00Z",
    "profile": "bobo",
    "warnings": [_WARNING],
}


# ── the client parse ──────────────────────────────────────────────────────


def test_warnings_survive_the_dataclass() -> None:
    resp = SetupResponse.from_dict(_RESPONSE)
    assert resp.warnings == (_WARNING,)
    assert resp.token == "eyJhbGciOi"


def test_a_response_with_no_warnings_parses_to_empty() -> None:
    """Older servers don't send the field at all."""
    assert SetupResponse.from_dict({"success": True}).warnings == ()


@pytest.mark.parametrize("raw", ["not-a-list", None, ["a string, not an entry"]])
def test_a_malformed_warnings_field_never_breaks_the_parse(raw) -> None:
    """The token is the point of this call; a junk warnings field must not
    cost the caller their credentials."""
    resp = SetupResponse.from_dict({"success": True, "token": "t", "warnings": raw})
    assert resp.token == "t"
    assert all(isinstance(w, dict) for w in resp.warnings)


# ── the command output ────────────────────────────────────────────────────


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _invoke(runner: CliRunner, monkeypatch, args: list[str], response: dict):
    """Run `cremind setup complete` against a stubbed server response."""
    import app.cli.commands.setup as setup_cmd
    from app.cli.main import app

    async def _fake_complete_setup(_client, _body):
        return SetupResponse.from_dict(response)

    # The command imports the client function inside its body, so patch it at
    # the source module rather than on the command's namespace.
    import app.cli.client.setup as setup_client
    monkeypatch.setattr(setup_client, "complete_setup", _fake_complete_setup)

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    import app.cli.client._base as base
    monkeypatch.setattr(base, "Client", lambda _cfg: _FakeClient())
    assert setup_cmd is not None  # imported for the side effect of registration

    monkeypatch.setattr(sys, "argv", ["cremind", *args])
    return runner.invoke(app, args, input=json.dumps({"profile": "bobo"}))


def test_a_warning_is_printed_next_to_the_token(runner, monkeypatch) -> None:
    result = _invoke(runner, monkeypatch, ["setup", "complete"], _RESPONSE)

    assert result.exit_code == 0, result.output
    assert "no main model" in result.output
    # The token hint still follows it — the warning informs, it doesn't replace.
    assert "CREMIND_TOKEN=eyJhbGciOi" in result.output


def test_json_mode_carries_the_warnings_array(runner, monkeypatch) -> None:
    result = _invoke(runner, monkeypatch, ["--json", "setup", "complete"], _RESPONSE)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["warnings"] == [_WARNING]


def test_a_clean_setup_prints_no_warning_line(runner, monkeypatch) -> None:
    clean = {**_RESPONSE, "warnings": []}
    result = _invoke(runner, monkeypatch, ["setup", "complete"], clean)

    assert result.exit_code == 0, result.output
    assert "Warning:" not in result.output
