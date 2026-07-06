"""CLI: `cremind conv send --mode` threading + `--no-reasoning` deprecation.

``conv_send`` does a function-body import of ``run_stream`` from
``app.cli.streaming``, so we patch it there (mirrors test_profile_persona's note).
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner


def _capture_run_stream(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    async def fake_run_stream(client, conversation_id, *, send_text="", mode=None, renderer, **kw):
        captured["conversation_id"] = conversation_id
        captured["send_text"] = send_text
        captured["mode"] = mode

    import app.cli.streaming as streaming
    monkeypatch.setattr(streaming, "run_stream", fake_run_stream)
    return captured


def test_mode_plan_threads_through(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app
    captured = _capture_run_stream(monkeypatch)
    result = CliRunner().invoke(
        app, ["--token", "t", "conv", "send", "c1", "hi", "--mode", "plan"],
    )
    assert result.exit_code == 0, result.output
    assert captured["mode"] == "plan"
    assert captured["send_text"] == "hi"


def test_default_sends_no_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app
    captured = _capture_run_stream(monkeypatch)
    result = CliRunner().invoke(app, ["--token", "t", "conv", "send", "c1", "hi"])
    assert result.exit_code == 0, result.output
    assert captured["mode"] is None


def test_no_reasoning_maps_to_instant_with_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app
    captured = _capture_run_stream(monkeypatch)
    result = CliRunner().invoke(
        app, ["--token", "t", "conv", "send", "c1", "hi", "--no-reasoning"],
    )
    assert result.exit_code == 0, result.output
    assert captured["mode"] == "instant"
    assert "deprecated" in result.output.lower()


def test_no_reasoning_conflicts_with_other_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app
    _capture_run_stream(monkeypatch)
    result = CliRunner().invoke(
        app,
        ["--token", "t", "conv", "send", "c1", "hi", "--mode", "plan", "--no-reasoning"],
    )
    assert result.exit_code != 0


def test_invalid_mode_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app
    _capture_run_stream(monkeypatch)
    result = CliRunner().invoke(
        app, ["--token", "t", "conv", "send", "c1", "hi", "--mode", "bogus"],
    )
    assert result.exit_code != 0
