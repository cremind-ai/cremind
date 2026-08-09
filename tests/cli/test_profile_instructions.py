"""CLI: `cremind profile instructions get|set`.

Mirrors the persona sub-app: an inline positional OR stdin, with the same guard
against silently wiping the text when no content is supplied.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner


def _capture_set(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Stub the client-layer set_instructions and capture its (name, content)."""
    captured: dict[str, object] = {}

    async def fake_set_instructions(client, name, content):  # noqa: ANN001
        captured["name"] = name
        captured["content"] = content

    # instructions_set imports from app.cli.client.profiles inside the function
    # body, so the name resolves from that module at call time — patch it there.
    import app.cli.client.profiles as profiles_client

    monkeypatch.setattr(profiles_client, "set_instructions", fake_set_instructions)
    return captured


def test_instructions_set_accepts_inline_positional(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app

    captured = _capture_set(monkeypatch)
    text = "Register each new channel user in the 'Active-User' sheet."

    result = CliRunner().invoke(
        app,
        ["--token", "dummy", "profile", "instructions", "set", "admin", text],
    )

    assert result.exit_code == 0, result.output
    assert captured["name"] == "admin"
    assert captured["content"] == text


def test_instructions_set_falls_back_to_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app

    captured = _capture_set(monkeypatch)
    text = "Directive one.\nDirective two.\n"

    result = CliRunner().invoke(
        app,
        ["--token", "dummy", "profile", "instructions", "set", "admin"],
        input=text,
    )

    assert result.exit_code == 0, result.output
    assert captured["content"] == text


def test_instructions_set_empty_string_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    # An explicit "" is the documented way to clear standing instructions.
    from app.cli.main import app

    captured = _capture_set(monkeypatch)

    result = CliRunner().invoke(
        app,
        ["--token", "dummy", "profile", "instructions", "set", "admin", ""],
    )

    assert result.exit_code == 0, result.output
    assert captured["content"] == ""


def test_instructions_set_empty_stdin_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Empty pipe / `< /dev/null` / exec_shell's auto-EOF must not silently wipe.
    from app.cli.main import app

    captured = _capture_set(monkeypatch)

    result = CliRunner().invoke(
        app,
        ["--token", "dummy", "profile", "instructions", "set", "admin"],
        input="",
    )

    assert result.exit_code == 1, result.output
    assert "usage" in result.output.lower()
    assert captured == {}


def test_instructions_get_prints_content(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app

    async def fake_get_instructions(client, name):  # noqa: ANN001
        assert name == "admin"
        return "Always greet new users."

    import app.cli.client.profiles as profiles_client

    monkeypatch.setattr(profiles_client, "get_instructions", fake_get_instructions)

    result = CliRunner().invoke(
        app, ["--token", "dummy", "profile", "instructions", "get", "admin"],
    )

    assert result.exit_code == 0, result.output
    assert "Always greet new users." in result.output


def test_instructions_get_empty_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A profile that never set instructions prints nothing and exits 0."""
    from app.cli.main import app

    async def fake_get_instructions(client, name):  # noqa: ANN001
        return ""

    import app.cli.client.profiles as profiles_client

    monkeypatch.setattr(profiles_client, "get_instructions", fake_get_instructions)

    result = CliRunner().invoke(
        app, ["--token", "dummy", "profile", "instructions", "get", "admin"],
    )

    assert result.exit_code == 0, result.output
    assert result.output.strip() == ""
