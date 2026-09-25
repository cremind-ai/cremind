"""`cremind userdocs caption|identity|allow-in` — option saves from the terminal.

The client functions are patched in ``app.cli.client.userdocs``, so nothing
reaches the network. What is pinned: each flag lands in the right options
group of the settings PUT (and only the flags given), list flags accept both
repeats and commas, and vision consent goes through the control action with
the exact "provider/model" the server showed — never through a settings PUT.
"""

from __future__ import annotations

import pytest

pytest.importorskip("typer")

from typer.testing import CliRunner  # noqa: E402

SETTINGS = {
    "local": {"options": {
        "caption": {"enabled": True, "daily_cap": None, "min_px": 256, "min_kb": 20},
        "identity": {"author_names": [], "emails": [], "camera_devices": []},
        "allow_in": {"web_cli": True, "channels": False, "rooms": False},
    }},
    "vision": {"ready": False, "provider": "openai", "model": "gpt-4o", "reason": "no_consent"},
}


@pytest.fixture
def calls(monkeypatch):
    import app.cli.client.userdocs as client

    seen: dict = {"put": [], "control": []}

    async def get_settings(c):
        return SETTINGS

    async def put_settings(c, body):
        seen["put"].append(body)
        return {"settings": SETTINGS}

    async def control(c, action, **params):
        seen["control"].append((action, params))
        return {"accepted": True, "snapshot": {"state": "idle"}}

    monkeypatch.setattr(client, "get_settings", get_settings)
    monkeypatch.setattr(client, "put_settings", put_settings)
    monkeypatch.setattr(client, "control", control)
    return seen


def _run(*args):
    from app.cli.main import app

    return CliRunner().invoke(app, ["--token", "t", *args])


def test_caption_limits_are_one_put_with_only_the_given_fields(calls):
    result = _run("userdocs", "caption", "--daily-cap", "200", "--min-px", "512")
    assert result.exit_code == 0, result.output
    assert calls["put"] == [{"kind": "local", "options": {"caption": {"daily_cap": 200, "min_px": 512}}}]
    assert calls["control"] == []


def test_consent_names_the_model_the_server_showed(calls):
    result = _run("userdocs", "caption", "--consent-vision")
    assert result.exit_code == 0, result.output
    assert calls["control"] == [("consent_vision", {"model": "openai/gpt-4o"})]
    assert calls["put"] == []  # consent never rides a settings save
    assert "openai/gpt-4o" in result.stderr


def test_consent_without_a_vision_model_fails(calls, monkeypatch):
    import app.cli.client.userdocs as client

    async def no_model(c):
        return {**SETTINGS, "vision": {"ready": False, "reason": "vision_model_unset"}}

    monkeypatch.setattr(client, "get_settings", no_model)
    result = _run("userdocs", "caption", "--consent-vision")
    assert result.exit_code == 1
    assert calls["control"] == []
    assert "LLM Providers" in result.stderr


def test_revoke_and_consent_are_exclusive(calls):
    result = _run("userdocs", "caption", "--consent-vision", "--revoke-consent")
    assert result.exit_code == 1
    assert calls["control"] == []


def test_identity_lists_take_repeats_and_commas(calls):
    result = _run("userdocs", "identity", "--name", "Ann Nguyen, Nguyễn Thị An", "--name", "A. N.",
                  "--camera", "iPhone 14")
    assert result.exit_code == 0, result.output
    assert calls["put"] == [{"kind": "local", "options": {"identity": {
        "author_names": ["Ann Nguyen", "Nguyễn Thị An", "A. N."],
        "camera_devices": ["iPhone 14"],
    }}}]


def test_identity_empty_value_clears_and_no_flags_only_prints(calls):
    assert _run("userdocs", "identity", "--email", "").exit_code == 0
    assert calls["put"] == [{"kind": "local", "options": {"identity": {"emails": []}}}]
    calls["put"].clear()
    result = _run("userdocs", "identity")
    assert result.exit_code == 0, result.output
    assert calls["put"] == []
    assert "camera_devices" in result.stdout


def test_allow_in_sends_only_the_switches_given(calls):
    result = _run("userdocs", "allow-in", "--channels", "--no-rooms")
    assert result.exit_code == 0, result.output
    assert calls["put"] == [{"kind": "local", "options": {"allow_in": {"channels": True, "rooms": False}}}]
