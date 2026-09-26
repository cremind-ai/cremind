"""What the agent is told when Documentation search cannot answer.

The message is relayed to the user, so the way out it names has to exist: the
admin's switch lives in Settings → My Documents → Administrator settings (it
moved there from Settings → Embedding) and in ``cremind docs admin set
--allow``. The gate is server-wide, so every profile — not just the admin —
gets the same pointer, and a profile that merely has it off is told about its
own switch instead.
"""

from __future__ import annotations

import typer.main

from app.documents import state as uds_state
from app.documents.query import open_engine

_GATED = {"allowed": False, "enabled": True, "state": "suspended", "reason": "admin_gate",
          "tool_mode": "hidden"}
_OFF = {"allowed": True, "enabled": False, "state": "disabled", "reason": None,
        "tool_mode": "hidden"}


def test_the_admin_gate_points_every_profile_at_the_real_switch(monkeypatch):
    monkeypatch.setattr(uds_state, "build_snapshot", lambda profile: dict(_GATED))
    for profile in ("admin", "javis"):
        access = open_engine(profile)
        assert access.engine is None
        assert access.code == "admin_gate", profile
        assert "Settings → My Documents → Administrator settings" in access.message
        assert "`cremind docs admin set --allow`" in access.message
        assert "Settings → Embedding" not in access.message


def test_a_profile_that_is_merely_off_is_told_about_its_own_switch(monkeypatch):
    monkeypatch.setattr(uds_state, "build_snapshot", lambda profile: dict(_OFF))
    access = open_engine("javis")
    assert access.code == "disabled"
    assert "cremind docs enable" in access.message
    assert "Administrator settings" not in access.message


def test_the_cli_flag_the_message_names_exists():
    from app.cli.main import app as cli

    root = typer.main.get_command(cli)
    set_cmd = root.commands["docs"].commands["admin"].commands["set"]
    flags = {opt for param in set_cmd.params for opt in param.opts}
    assert "--allow" in flags
