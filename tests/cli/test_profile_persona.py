"""CLI: `cremind profile persona set` accepts an inline positional OR stdin.

Regression for the bug where an inline persona string was rejected as an
"unexpected extra argument" because `persona set` only read from stdin.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner


def _capture_set_persona(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Stub the client-layer set_persona and capture its (name, content)."""
    captured: dict[str, object] = {}

    async def fake_set_persona(client, name, content):  # noqa: ANN001
        captured["name"] = name
        captured["content"] = content

    # persona_set does `from app.cli.client.profiles import set_persona`
    # inside the function body, so the name is resolved from that module at
    # call time — patch it there, not on the command module.
    import app.cli.client.profiles as profiles_client

    monkeypatch.setattr(profiles_client, "set_persona", fake_set_persona)
    return captured


def test_persona_set_accepts_inline_positional(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app

    captured = _capture_set_persona(monkeypatch)
    persona = "You are **Cremind**, a concise assistant.\nAlways link file paths."

    result = CliRunner().invoke(
        app,
        ["--token", "dummy", "profile", "persona", "set", "admin", persona],
    )

    assert result.exit_code == 0, result.output
    assert captured["name"] == "admin"
    assert captured["content"] == persona


def test_persona_set_falls_back_to_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app

    captured = _capture_set_persona(monkeypatch)
    persona = "Persona from stdin.\nSecond line.\n"

    # CliRunner.invoke's `input=` makes stdin a non-tty StringIO, so the
    # isatty guard is not triggered and the read() path runs.
    result = CliRunner().invoke(
        app,
        ["--token", "dummy", "profile", "persona", "set", "admin"],
        input=persona,
    )

    assert result.exit_code == 0, result.output
    assert captured["name"] == "admin"
    assert captured["content"] == persona


def test_persona_set_empty_string_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    # `content is not None` (not truthiness) lets an explicit "" through, so
    # a caller can deliberately clear a persona with `persona set admin ""`.
    from app.cli.main import app

    captured = _capture_set_persona(monkeypatch)

    result = CliRunner().invoke(
        app,
        ["--token", "dummy", "profile", "persona", "set", "admin", ""],
    )

    assert result.exit_code == 0, result.output
    assert captured["content"] == ""


def test_persona_set_empty_stdin_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # No <content> arg + empty stdin (empty pipe / `< /dev/null` / exec_shell's
    # auto-EOF) must NOT silently store an empty persona — it errors instead and
    # makes no HTTP call.
    from app.cli.main import app

    captured = _capture_set_persona(monkeypatch)

    result = CliRunner().invoke(
        app,
        ["--token", "dummy", "profile", "persona", "set", "admin"],
        input="",
    )

    assert result.exit_code == 1, result.output
    assert "usage" in result.output.lower()
    assert captured == {}  # set_persona was never called


def test_persona_set_blank_stdin_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app

    captured = _capture_set_persona(monkeypatch)

    result = CliRunner().invoke(
        app,
        ["--token", "dummy", "profile", "persona", "set", "admin"],
        input="   \n\t  \n",
    )

    assert result.exit_code == 1, result.output
    assert captured == {}


def test_persona_set_text_in_name_slot_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # The reported bug: the persona text is passed as the single (name) arg with
    # no content. With empty stdin this now errors with a usage hint instead of
    # PUTting the whole blob as a profile name (which the server 403s).
    from app.cli.main import app

    captured = _capture_set_persona(monkeypatch)
    blob = "You are **Cremind**, the personal AI assistant at the heart of\nCremind."

    result = CliRunner().invoke(
        app,
        ["--token", "dummy", "profile", "persona", "set", blob],
        input="",
    )

    assert result.exit_code == 1, result.output
    assert captured == {}  # no bogus PUT with the blob as the profile name
