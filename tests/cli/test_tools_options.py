"""CLI: `cremind tools options` — live option lists for dynamic variables.

``tools_options`` does a function-body import of ``get_tool_variable_options``
from ``app.cli.client.tools``, so we patch it there (the client call never hits
the network).
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner


def _patch_options(monkeypatch: pytest.MonkeyPatch, payload: dict) -> dict[str, object]:
    captured: dict[str, object] = {}

    async def fake_get(client, tool_id, refresh=False):
        captured["tool_id"] = tool_id
        captured["refresh"] = refresh
        return payload

    import app.cli.client.tools as tools_client
    monkeypatch.setattr(tools_client, "get_tool_variable_options", fake_get)
    return captured


_MODELS_PAYLOAD = {
    "tool_id": "claude_code",
    "variables": {
        "CLAUDE_CODE_MODEL": {
            "options": [
                {"id": "claude-sonnet-4-5", "label": "Sonnet 4.5"},
                {"id": "sonnet", "label": "sonnet (alias)"},
            ],
            "error": None,
            "source": "tool_variable_api_key",
        }
    },
}


def test_options_table_lists_models(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app
    _patch_options(monkeypatch, _MODELS_PAYLOAD)
    result = CliRunner().invoke(app, ["--token", "t", "tools", "options", "claude_code"])
    assert result.exit_code == 0, result.output
    assert "claude-sonnet-4-5" in result.output
    assert "CLAUDE_CODE_MODEL" in result.output


def test_options_json_returns_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app
    _patch_options(monkeypatch, _MODELS_PAYLOAD)
    result = CliRunner().invoke(
        app, ["--json", "--token", "t", "tools", "options", "claude_code"],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["variables"]["CLAUDE_CODE_MODEL"]["options"][0]["id"] == "claude-sonnet-4-5"


def test_options_refresh_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app
    captured = _patch_options(monkeypatch, _MODELS_PAYLOAD)
    result = CliRunner().invoke(
        app, ["--token", "t", "tools", "options", "claude_code", "--refresh"],
    )
    assert result.exit_code == 0, result.output
    assert captured["refresh"] is True


def test_options_error_surfaces_in_json(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app
    payload = {
        "tool_id": "claude_code",
        "variables": {
            "CLAUDE_CODE_MODEL": {
                "options": [], "error": "No Anthropic credential available.", "source": None,
            }
        },
    }
    _patch_options(monkeypatch, payload)
    result = CliRunner().invoke(
        app, ["--json", "--token", "t", "tools", "options", "claude_code"],
    )
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed["variables"]["CLAUDE_CODE_MODEL"]["error"] == "No Anthropic credential available."


def test_options_no_dynamic_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app
    _patch_options(monkeypatch, {"tool_id": "exec_shell", "variables": {}})
    result = CliRunner().invoke(app, ["--token", "t", "tools", "options", "exec_shell"])
    assert result.exit_code == 0, result.output
