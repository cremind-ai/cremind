"""`cremind auth` — the authenticated path, `--local` recovery, and gating.

Three things carry the weight here:

* the JWT never reaches stdout unless explicitly asked for (agents run `cremind`
  through `exec_shell`, so stdout is conversation history);
* `--local` and `auth show` reach the command *without* the root callback
  resolving a token — that resolution hard-errors on a missing token file, which
  is precisely the situation the recovery path exists for;
* a 401 on the authenticated path points at `--local` rather than dead-ending.
"""

from __future__ import annotations

import json
import sys

import pytest
from typer.testing import CliRunner


def _invoke(runner: CliRunner, monkeypatch, args: list[str]):
    """Invoke the CLI, mirroring `args` into `sys.argv`.

    Mandatory for anything touching the gating: `_should_resolve_profile` reads
    `sys.argv` (Click doesn't expose the deep subcommand at root-callback
    stage), and `CliRunner` leaves it untouched.
    """
    from app.cli.main import app

    monkeypatch.setattr(sys, "argv", ["cremind", *args])
    return runner.invoke(app, args)


@pytest.fixture
def sysdir(tmp_path, monkeypatch):
    d = tmp_path / "sysdir"
    (d / "tokens").mkdir(parents=True)
    monkeypatch.setenv("CREMIND_SYSTEM_DIR", str(d))
    monkeypatch.setenv("WT_SESSION", "test-session-fixed")
    monkeypatch.delenv("CREMIND_TOKEN", raising=False)
    monkeypatch.delenv("CREMIND_PROFILE", raising=False)
    return d


@pytest.fixture
def no_picker(monkeypatch):
    """The TUI picker must never run in these tests."""
    import app.cli.tui.profile_picker as picker

    def _boom(*a, **k):
        raise AssertionError("the profile picker must not run here")

    monkeypatch.setattr(picker, "pick_profile", _boom)


def _write_token(sysdir, name: str, value: str | None = None) -> None:
    (sysdir / "tokens" / f"{name}.token").write_text(value or f"jwt-{name}", encoding="utf-8")


def _first_json(output: str) -> dict:
    """Parse the leading JSON object, ignoring the advisory that follows.

    `CliRunner` merges stderr into `output`; in a real terminal the advisory
    goes to stderr and stdout stays clean for piping.
    """
    parsed, _end = json.JSONDecoder().raw_decode(output[output.index("{"):])
    return parsed


def _patch_regenerate(monkeypatch, payload: dict) -> dict:
    """Stub the client wrapper. Commands do function-body imports, so the patch
    goes on the *client* module."""
    import app.cli.client.auth as auth_client

    captured: dict = {}

    async def fake(client, *, profile=None, expires_hours=None):
        captured["profile"] = profile
        captured["expires_hours"] = expires_hours
        return auth_client.RotatedToken.from_dict(payload)

    monkeypatch.setattr(auth_client, "regenerate_token", fake)
    return captured


_ROTATED = {
    "profile": "admin",
    "token": "JWT-BRAND-NEW",
    "expires_at": "2026-09-11T00:00:00+00:00",
    "serial": 3,
    "token_file": "/srv/.cremind/tokens/admin.token",
}


# ── regenerate: the authenticated path ─────────────────────────────────────


def test_regenerate_renders_profile_serial_and_expiry(sysdir, monkeypatch):
    _patch_regenerate(monkeypatch, _ROTATED)
    result = CliRunner().invoke(
        __import__("app.cli.main", fromlist=["app"]).app,
        ["--token", "t", "auth", "regenerate", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert "admin" in result.output
    assert "3" in result.output


def test_regenerate_hides_the_jwt_by_default(sysdir, monkeypatch):
    from app.cli.main import app

    _patch_regenerate(monkeypatch, _ROTATED)
    result = CliRunner().invoke(app, ["--token", "t", "auth", "regenerate", "--yes"])
    assert result.exit_code == 0, result.output
    assert "JWT-BRAND-NEW" not in result.output


def test_show_token_prints_the_jwt_and_the_export_hint(sysdir, monkeypatch):
    from app.cli.main import app

    _patch_regenerate(monkeypatch, _ROTATED)
    result = CliRunner().invoke(
        app, ["--token", "t", "auth", "regenerate", "--yes", "--show-token"]
    )
    assert result.exit_code == 0, result.output
    assert "JWT-BRAND-NEW" in result.output
    assert "export CREMIND_TOKEN=" in result.output


def test_json_omits_the_token_unless_asked(sysdir, monkeypatch):
    from app.cli.main import app

    _patch_regenerate(monkeypatch, _ROTATED)
    result = CliRunner().invoke(app, ["--json", "--token", "t", "auth", "regenerate", "--yes"])
    assert result.exit_code == 0, result.output
    parsed = _first_json(result.output)
    assert "token" not in parsed
    assert parsed["serial"] == 3

    result = CliRunner().invoke(
        app, ["--json", "--token", "t", "auth", "regenerate", "--yes", "--show-token"]
    )
    assert _first_json(result.output)["token"] == "JWT-BRAND-NEW"


def test_regenerate_forwards_profile_and_expiry(sysdir, monkeypatch):
    from app.cli.main import app

    captured = _patch_regenerate(monkeypatch, {**_ROTATED, "profile": "bob"})
    result = CliRunner().invoke(
        app,
        ["--token", "t", "auth", "regenerate", "--yes", "--profile", "bob", "--expires-hours", "5"],
    )
    assert result.exit_code == 0, result.output
    assert captured == {"profile": "bob", "expires_hours": 5}


def test_declining_the_confirmation_never_calls_the_server(sysdir, monkeypatch):
    """Rotation must not happen unattended — CliRunner supplies empty stdin, so
    typer.confirm aborts."""
    from app.cli.main import app

    captured = _patch_regenerate(monkeypatch, _ROTATED)
    result = CliRunner().invoke(app, ["--token", "t", "auth", "regenerate"])
    assert result.exit_code == 1
    assert captured == {}


def test_regenerate_updates_the_local_token_file(sysdir, monkeypatch):
    """A rotation must not lock the CLI that performed it out of the server."""
    from app.cli.main import app

    _write_token(sysdir, "admin", "jwt-OLD")
    _patch_regenerate(monkeypatch, _ROTATED)
    result = CliRunner().invoke(app, ["--token", "t", "auth", "regenerate", "--yes"])
    assert result.exit_code == 0, result.output
    assert (sysdir / "tokens" / "admin.token").read_text(encoding="utf-8") == "JWT-BRAND-NEW"


def test_no_write_file_leaves_the_token_file_alone(sysdir, monkeypatch):
    from app.cli.main import app

    _write_token(sysdir, "admin", "jwt-OLD")
    _patch_regenerate(monkeypatch, _ROTATED)
    result = CliRunner().invoke(
        app, ["--token", "t", "auth", "regenerate", "--yes", "--no-write-file"]
    )
    assert result.exit_code == 0, result.output
    assert (sysdir / "tokens" / "admin.token").read_text(encoding="utf-8") == "jwt-OLD"


def _raise_api_error(monkeypatch, status: int, payload: dict):
    import app.cli.client.auth as auth_client
    from app.cli.client._base import APIError

    async def fake(client, *, profile=None, expires_hours=None):
        raise APIError(
            status=status,
            body=str(payload.get("error", "")),
            raw=json.dumps(payload).encode(),
        )

    monkeypatch.setattr(auth_client, "regenerate_token", fake)


def test_403_surfaces_the_servers_message(sysdir, monkeypatch):
    from app.cli.main import app

    _raise_api_error(
        monkeypatch, 403,
        {"error": "admin_required", "message": "Only the admin profile can rotate another"},
    )
    result = CliRunner().invoke(
        app, ["--token", "t", "auth", "regenerate", "--yes", "--profile", "bob"]
    )
    assert result.exit_code == 1
    assert "Only the admin profile" in result.output


def test_401_points_at_the_local_recovery_path(sysdir, monkeypatch):
    """The dead-end error becomes a signpost — this is why --local exists."""
    from app.cli.main import app

    _raise_api_error(monkeypatch, 401, {"error": "Unauthenticated"})
    result = CliRunner().invoke(app, ["--token", "t", "auth", "regenerate", "--yes"])
    assert result.exit_code == 1
    assert "--local" in result.output


# ── --local recovery ───────────────────────────────────────────────────────


def _stub_local(monkeypatch, *, rotate=None, serial=0, names=("admin",)):
    import app.cli.commands.auth as auth_cmd

    def _rotate(profile, *, hours=None):
        if rotate is not None:
            return rotate(profile, hours)
        return {
            "profile": profile,
            "token": f"JWT-LOCAL-{profile}",
            "expires_at": "2026-09-11T00:00:00+00:00",
            "serial": 9,
            "token_file": f"/srv/.cremind/tokens/{profile}.token",
        }

    monkeypatch.setattr(
        auth_cmd, "_load_local_backend",
        lambda: (_rotate, lambda p: serial, lambda: list(names)),
    )


def test_local_recovers_a_profile_whose_token_file_is_gone(sysdir, monkeypatch, no_picker):
    """The headline recovery case."""
    from app.cli.main import app

    _write_token(sysdir, "admin")  # a different profile has a token; `ghost` doesn't
    _stub_local(monkeypatch, names=("admin", "ghost"))

    result = _invoke(
        CliRunner(), monkeypatch,
        ["auth", "regenerate", "--local", "--profile", "ghost", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert (sysdir / "tokens" / "ghost.token").read_text(encoding="utf-8") == "JWT-LOCAL-ghost"
    # The root callback's hard error must never have been reached.
    assert "has no token file" not in result.output


def test_local_never_touches_the_http_client(sysdir, monkeypatch, no_picker):
    from app.cli.main import app
    import app.cli.client.auth as auth_client

    async def _boom(*a, **k):
        raise AssertionError("--local must not call the server")

    monkeypatch.setattr(auth_client, "regenerate_token", _boom)
    _stub_local(monkeypatch)

    result = _invoke(CliRunner(), monkeypatch, ["auth", "regenerate", "--local", "--yes"])
    assert result.exit_code == 0, result.output


def test_local_sole_db_profile_is_auto_selected(sysdir, monkeypatch, no_picker):
    """Resolution comes from the DB, not from which token files happen to exist
    — with no token files at all, `solo` must still be found."""
    _stub_local(monkeypatch, names=("solo",))

    result = _invoke(CliRunner(), monkeypatch, ["auth", "regenerate", "--local", "--yes"])
    assert result.exit_code == 0, result.output
    assert (sysdir / "tokens" / "solo.token").exists()


def test_local_multi_profile_non_interactive_lists_the_names(sysdir, monkeypatch, no_picker):
    _stub_local(monkeypatch, names=("admin", "bob"))

    result = _invoke(CliRunner(), monkeypatch, ["auth", "regenerate", "--local", "--yes"])
    assert result.exit_code == 1
    assert "--profile" in result.output
    assert "admin" in result.output and "bob" in result.output


@pytest.mark.parametrize(
    "message,expected",
    [
        ("no database is configured here. Run `cremind setup` first.", "cremind setup"),
        ("`--local` needs the Cremind server package", "server package"),
    ],
)
def test_local_backend_failures_are_phrased_not_tracebacks(
    sysdir, monkeypatch, no_picker, message, expected
):
    import app.cli.commands.auth as auth_cmd

    def _boom():
        raise auth_cmd._LocalError(message)

    monkeypatch.setattr(auth_cmd, "_load_local_backend", _boom)
    result = _invoke(CliRunner(), monkeypatch, ["auth", "regenerate", "--local", "--yes"])
    assert result.exit_code == 1
    assert expected in result.output
    assert "Traceback" not in result.output


def test_local_missing_column_points_at_db_upgrade(sysdir, monkeypatch, no_picker):
    from sqlalchemy.exc import OperationalError

    def _rotate(profile, hours):
        raise OperationalError("UPDATE profiles", {}, Exception("no such column: token_serial"))

    _stub_local(monkeypatch, rotate=_rotate)
    result = _invoke(CliRunner(), monkeypatch, ["auth", "regenerate", "--local", "--yes"])
    assert result.exit_code == 1
    assert "cremind db upgrade" in result.output


def test_local_database_locked_is_explained(sysdir, monkeypatch, no_picker):
    from sqlalchemy.exc import OperationalError

    def _rotate(profile, hours):
        raise OperationalError("UPDATE profiles", {}, Exception("database is locked"))

    _stub_local(monkeypatch, rotate=_rotate)
    result = _invoke(CliRunner(), monkeypatch, ["auth", "regenerate", "--local", "--yes"])
    assert result.exit_code == 1
    assert "locked" in result.output


def test_local_unknown_profile_points_at_profile_list(sysdir, monkeypatch, no_picker):
    def _rotate(profile, hours):
        raise LookupError("ghost")

    _stub_local(monkeypatch, rotate=_rotate)
    result = _invoke(
        CliRunner(), monkeypatch,
        ["auth", "regenerate", "--local", "--profile", "ghost", "--yes"],
    )
    assert result.exit_code == 1
    assert "cremind profile list" in result.output


# ── status & show ──────────────────────────────────────────────────────────


def _patch_status(monkeypatch, payload: dict):
    import app.cli.client.auth as auth_client

    async def fake(client, *, profile=None):
        return auth_client.AuthStatus.from_dict(payload)

    monkeypatch.setattr(auth_client, "get_auth_status", fake)


def test_status_reports_a_valid_token(sysdir, monkeypatch):
    from app.cli.main import app

    _patch_status(monkeypatch, {
        "profile": "admin", "sub": "admin", "iat": 0, "exp": 0,
        "token_serial": 2, "current_serial": 2, "valid": True, "token_file": "/x/admin.token",
    })
    result = CliRunner().invoke(app, ["--token", "t", "auth", "status"])
    assert result.exit_code == 0, result.output
    assert "admin" in result.output


def test_status_exits_1_and_names_both_serials_when_revoked(sysdir, monkeypatch):
    from app.cli.main import app

    _patch_status(monkeypatch, {
        "profile": "admin", "sub": "admin", "iat": 0, "exp": 0,
        "token_serial": 4, "current_serial": 7, "valid": False, "token_file": "",
    })
    result = CliRunner().invoke(app, ["--token", "t", "auth", "status"])
    assert result.exit_code == 1
    assert "4" in result.output and "7" in result.output
    assert "regenerate" in result.output


def test_show_prints_the_token_verbatim(sysdir, monkeypatch, no_picker):
    _write_token(sysdir, "solo", "JWT-ON-DISK")
    result = _invoke(CliRunner(), monkeypatch, ["auth", "show"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "JWT-ON-DISK"


def test_show_path_prints_the_path(sysdir, monkeypatch, no_picker):
    _write_token(sysdir, "solo")
    result = _invoke(CliRunner(), monkeypatch, ["auth", "show", "--path"])
    assert result.exit_code == 0, result.output
    assert result.output.strip().endswith("solo.token")


def test_show_missing_file_hints_at_local_recovery(sysdir, monkeypatch, no_picker):
    result = _invoke(CliRunner(), monkeypatch, ["auth", "show", "--profile", "ghost"])
    assert result.exit_code == 1
    assert "--local" in result.output


# ── root-callback gating ───────────────────────────────────────────────────


def test_local_skips_profile_resolution_entirely(sysdir, monkeypatch, no_picker):
    """Two token files on disk would normally be ambiguous and pop the picker."""
    _write_token(sysdir, "admin")
    _write_token(sysdir, "bob")
    _stub_local(monkeypatch, names=("admin", "bob"))

    result = _invoke(
        CliRunner(), monkeypatch,
        ["auth", "regenerate", "--local", "--profile", "admin", "--yes"],
    )
    assert result.exit_code == 0, result.output


def test_auth_show_skips_profile_resolution(sysdir, monkeypatch, no_picker):
    _write_token(sysdir, "admin")
    _write_token(sysdir, "bob")
    result = _invoke(CliRunner(), monkeypatch, ["auth", "show", "--profile", "admin"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "jwt-admin"


def test_the_authenticated_path_still_resolves_a_token(sysdir, monkeypatch, no_picker):
    """The gating must not have broken the normal path: with no --local and no
    --token, the sole on-disk profile's token still reaches the request."""
    import app.cli.client._base as base
    import app.cli.client.auth as auth_client

    _write_token(sysdir, "solo", "JWTVAL-solo")
    captured: dict = {}

    async def fake_get_json(self, path):
        captured["auth"] = self._http.headers.get("Authorization")
        return {
            "profile": "solo", "sub": "solo", "iat": 0, "exp": 0,
            "token_serial": 0, "current_serial": 0, "valid": True, "token_file": "",
        }

    monkeypatch.setattr(base.Client, "get_json", fake_get_json)
    result = _invoke(CliRunner(), monkeypatch, ["auth", "status"])
    assert result.exit_code == 0, result.output
    assert captured["auth"] == "Bearer JWTVAL-solo"
    assert auth_client.get_auth_status is not None  # the real wrapper was used


def test_explicit_profile_without_a_token_file_names_the_recovery_command(
    sysdir, monkeypatch, no_picker
):
    _write_token(sysdir, "admin")
    result = _invoke(CliRunner(), monkeypatch, ["-p", "ghost", "auth", "status"])
    assert result.exit_code == 1
    assert "auth regenerate --local" in result.output


def test_help_never_resolves_a_profile(sysdir, monkeypatch, no_picker):
    _write_token(sysdir, "admin")
    _write_token(sysdir, "bob")
    result = _invoke(CliRunner(), monkeypatch, ["auth", "regenerate", "--help"])
    assert result.exit_code == 0, result.output


def test_group_subcommand_parser():
    from app.cli.main import _group_subcommand

    assert _group_subcommand(["-p", "x", "auth", "regenerate", "--yes"], "auth") == "regenerate"
    assert _group_subcommand(["auth", "--local", "regenerate"], "auth") == "regenerate"
    assert _group_subcommand(["auth", "show"], "profile") is None
    assert _group_subcommand(["auth"], "auth") is None
