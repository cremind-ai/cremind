"""CLI: `cremind calendar google` reports who owns the Google link.

A 409 from ``connect`` is the normal answer once the gcalendar skill holds the
link, so the reason has to reach the user. The generic error path prints only the
error *code* (``_check_response`` keeps `error`, drops `message`), which would
read as an unexplained failure.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner


def _raise_api_error(monkeypatch: pytest.MonkeyPatch, status: int, payload: dict) -> None:
    """Make the client-layer google_connect fail like the server would."""
    from app.cli.client._base import APIError

    async def fake_connect(client):  # noqa: ANN001
        raise APIError(
            status=status,
            body=str(payload.get("error", "")),
            raw=json.dumps(payload).encode(),
        )

    # google_connect does a function-body import from this module, so patch here.
    import app.cli.client.calendar as calendar_client

    monkeypatch.setattr(calendar_client, "google_connect", fake_connect)


def test_connect_explains_that_the_skill_owns_the_link(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app

    message = (
        "Google Calendar is already linked through the gcalendar skill "
        "(linked@example.com), and that link drives this calendar."
    )
    _raise_api_error(monkeypatch, 409, {"error": "skill_managed", "message": message})

    result = CliRunner().invoke(app, ["--token", "t", "calendar", "google", "connect"])
    assert result.exit_code == 1
    assert "gcalendar skill" in result.output
    assert "linked@example.com" in result.output


def test_connect_still_explains_an_unavailable_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app

    _raise_api_error(
        monkeypatch, 409,
        {"error": "unavailable", "message": "this server's public URL could not be resolved"},
    )
    result = CliRunner().invoke(app, ["--token", "t", "calendar", "google", "connect"])
    assert result.exit_code == 1
    assert "public URL" in result.output


def test_connect_prints_the_authorize_url_when_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app

    async def fake_connect(client):  # noqa: ANN001
        return "https://accounts.google.com/o/oauth2/v2/auth?x=1"

    import app.cli.client.calendar as calendar_client

    monkeypatch.setattr(calendar_client, "google_connect", fake_connect)
    result = CliRunner().invoke(app, ["--token", "t", "calendar", "google", "connect"])
    assert result.exit_code == 0, result.output
    assert "accounts.google.com" in result.output


def test_disconnect_relays_the_still_connected_note(monkeypatch: pytest.MonkeyPatch) -> None:
    """Saying nothing here would imply Google was disconnected, which it wasn't."""
    from app.cli.main import app

    async def fake_disconnect(client):  # noqa: ANN001
        return {
            "ok": True, "google_connected": True, "google_source": "skill",
            "message": "The gcalendar skill's link still drives this calendar.",
        }

    import app.cli.client.calendar as calendar_client

    monkeypatch.setattr(calendar_client, "google_disconnect", fake_disconnect)
    result = CliRunner().invoke(app, ["--token", "t", "calendar", "google", "disconnect"])
    assert result.exit_code == 0, result.output
    assert "still drives this calendar" in result.output


def test_disconnect_is_quiet_when_it_really_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app

    async def fake_disconnect(client):  # noqa: ANN001
        return {"ok": True, "google_connected": False, "google_source": None}

    import app.cli.client.calendar as calendar_client

    monkeypatch.setattr(calendar_client, "google_disconnect", fake_disconnect)
    result = CliRunner().invoke(app, ["--token", "t", "calendar", "google", "disconnect"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == ""
