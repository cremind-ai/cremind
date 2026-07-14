"""CLI: `cremind tools set-var` threads the `--force` (allow_unknown) opt-out.

``tools_set_var`` does a function-body import of ``set_tool_variables`` from
``app.cli.client.tools``, so we patch it there (no network).
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner


def _capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    async def fake_set(client, tool_id, variables, allow_unknown=False):
        captured["tool_id"] = tool_id
        captured["variables"] = variables
        captured["allow_unknown"] = allow_unknown

    import app.cli.client.tools as tools_client
    monkeypatch.setattr(tools_client, "set_tool_variables", fake_set)
    return captured


def test_set_var_default_no_force(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app
    captured = _capture(monkeypatch)
    result = CliRunner().invoke(
        app, ["--token", "t", "tools", "set-var", "claude_code", "CLAUDE_CODE_MODEL=opus"],
    )
    assert result.exit_code == 0, result.output
    assert captured["allow_unknown"] is False
    assert captured["variables"] == {"CLAUDE_CODE_MODEL": "opus"}


def test_set_var_force_threads_allow_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app
    captured = _capture(monkeypatch)
    result = CliRunner().invoke(
        app,
        ["--token", "t", "tools", "set-var", "claude_code",
         "CLAUDE_CODE_MODEL=my-custom", "--force"],
    )
    assert result.exit_code == 0, result.output
    assert captured["allow_unknown"] is True


def test_set_var_force_short_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app
    captured = _capture(monkeypatch)
    result = CliRunner().invoke(
        app,
        ["--token", "t", "tools", "set-var", "claude_code", "CLAUDE_CODE_MODEL=x", "-f"],
    )
    assert result.exit_code == 0, result.output
    assert captured["allow_unknown"] is True
