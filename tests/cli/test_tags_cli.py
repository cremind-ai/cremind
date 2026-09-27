"""`cremind tags …` — the Cremind Tag CLI (profile commands and the admin `hardware` sub-app).

The commands import their client functions inside the body, so every wrapper
in ``app.cli.client.tags`` is patched with a recording fake and nothing reaches
the network. What is pinned: each command's happy path sends exactly the
request the REST API takes (tag names resolved to ids first), `cremind --json`
prints the server's object verbatim, a secret is printed once and only by the
command that creates it, the 409/422 bodies are explained with the server's
message plus a hint (the OTP refusal above all), a non-admin gets a plain
"needs the admin profile" instead of a bare 403, destructive hardware verbs
change nothing without --yes (exit 2), and the CLI stays free of server
modules.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys

import pytest

pytest.importorskip("typer")

from typer.testing import CliRunner  # noqa: E402

DESK = "3f2a9c1e-5b7d-4e2f-8a6c-000000000001"
KITCHEN = "3f2a9c1e-9999-4e2f-8a6c-000000000002"  # shares only the 8-char prefix with DESK
COMP = "c0c0c0c0-1111-4222-8333-000000000001"
COMP2 = "c0c0c0c0-1111-4222-8333-000000000002"
BRIDGE = "b1b1b1b1-1111-4222-8333-000000000001"
CMD = "5d0c9a7e-0000-4000-8000-000000000001"
SECRET = "S3cr3t-value-that-must-appear-once-AAAAAAAAAA"
AUTH = f"CremindTag tagc_abcdefghijklmnopqrstuvwxyz.{SECRET}"


def _device(device_id: str, name: str, hw_id: str, **kw) -> dict:
    return {
        "id": device_id, "companion_id": COMP, "kind": "tag", "hw_id": hw_id, "name": name,
        "owner_profile": "alice", "bridge_device_id": BRIDGE, "epoch": 3, "rotation": 0,
        "board": 16, "panel": 1, "width": 400, "height": 300, "planes": 1, "fw": "0.1.0", "info": {},
        "status": "ok", "battery_mv": 2900, "rssi": -61, "last_contact_at": 1790000000000.0,
        "desired_revision": 18, "displayed_revision": 17, "displayed_digest": "abcd1234",
        "clear_required": False, "claimed_at": 1789000000000.0, "created_at": 1789000000000.0,
        "updated_at": 1790000000000.0, "previews": {"desired": 18, "displayed": 17},
        "companion_name": "Desk PC", "companion_online": True, **kw,
    }


def _delivery(delivery_id: int = 501, **kw) -> dict:
    return {
        "id": delivery_id, "seq": 1249, "profile": "alice", "device_id": DESK, "companion_id": COMP,
        "epoch": 3, "event_id": None, "kind": "pinned_note", "priority": 55, "replace_key": f"pinned:{DESK}",
        "resolves": None, "card": {"v": 1, "kind": "pinned_note", "title": "Back at 3", "body": "Ring"},
        "stage": "queued", "terminal": False, "outcome": None, "status_code": None, "revision": None,
        "digest": None, "detail": None, "timing": None, "stage_times": {"queued": 1790000000000.0},
        "created_at": 1790000000000.0, "updated_at": 1790000000000.0, "expires_at": 1790086400000.0,
        "finished_at": None, **kw,
    }


def _command(kind: str = "refresh_tag", **kw) -> dict:
    return {"id": CMD, "companion_id": COMP, "kind": kind, "args": {"tag_id": "1A2B3C4D"},
            "requested_by": "alice", "status": "queued", "result": None, "error": None,
            "created_at": 1790000000000.0, "claimed_at": None, "completed_at": None,
            "expires_at": 1790003600000.0, **kw}


def _credential(kind: str = "content", **kw) -> dict:
    return {"id": "tagc_abcdefghijklmnopqrstuvwxyz", "companion_id": COMP, "kind": kind,
            "profile": "alice" if kind == "content" else None, "label": "Desk", "created_by": "alice",
            "created_at": 1790000000000.0, "last_used_at": None, "revoked_at": None, "revoked": False, **kw}


SETTINGS = {
    "profile": "alice", "enabled": True,
    "options": {"language": "vi", "routes": {"usage": "all", "notification": "none"}},
    "defaults": {"qr_links": True},
    "effective": {"layout": "status", "show_excerpts": False, "qr_links": True, "progress_cadence_s": 300,
                  "language": "vi", "timezone": "",
                  "routes": {"notification": "none", "needs_input": "all", "usage": "all"}},
    "timezone": "Asia/Ho_Chi_Minh", "updated_at": 1790000000000.0,
    "routable_kinds": ["notification", "task_outcome", "needs_input", "usage"],
    "layouts": ["status"], "icons": ["info", "push_pin"], "is_admin": False,
}

COMPANION = {"id": COMP, "name": "Desk PC", "online": True, "last_seen_at": 1790000000000.0, "version": "0.1.0"}

INVENTORY = {
    "companions": [{**COMPANION, "created_by": "admin", "created_at": 1789000000000.0, "host": "desk-pc",
                    "credentials": [_credential("hardware")]}],
    "devices": [
        _device(DESK, "Desk", "1A2B3C4D"),
        _device(KITCHEN, "", "5E6F7A8B", owner_profile=None, status="unclaimed"),
        {**_device(BRIDGE, "Hall bridge", "br-9f"), "kind": "bridge", "owner_profile": None},
    ],
    "commands": [_command()],
}


def _responses() -> dict:
    return {
        "overview": {"profile": "alice", "enabled": True,
                     "devices": [_device(DESK, "Desk", "1A2B3C4D"), _device(KITCHEN, "Kitchen", "5E6F7A8B")],
                     "counts": {"devices": 2, "active_deliveries": 1, "needs_input": 1, "failed_24h": 0}},
        "get_settings": SETTINGS,
        "put_settings": SETTINGS,
        "get_device": {"device": _device(DESK, "Desk", "1A2B3C4D"), "deliveries": [_delivery()]},
        "rename_device": {"device": _device(DESK, "Office", "1A2B3C4D")},
        "display": {"delivery": _delivery()},
        "clear": {"delivery": _delivery(502, kind="clear")},
        "refresh": {"command": _command("refresh_tag")},
        "identify": {"command": _command("identify")},
        "get_preview": (b"\x89PNG\r\n\x1a\nfake", 17),
        "list_deliveries": {"deliveries": [_delivery(900), _delivery(880)], "next_before": 880},
        "get_delivery": {"delivery": _delivery(stage="displayed", terminal=True, outcome="displayed",
                                                stage_times={"queued": 1790000000000.0,
                                                             "displayed": 1790000060000.0})},
        "cancel_delivery": {"delivery": _delivery(stage="cancelled", terminal=True)},
        "list_companions": {"companions": [COMPANION]},
        "list_credentials": {"credentials": [_credential(), _credential(id="tagc_old", revoked=True,
                                                                         revoked_at=1790000000000.0)]},
        "create_credential": {"credential": _credential(), "secret": SECRET, "authorization": AUTH},
        "revoke_credential": {"credential": _credential(revoked=True, revoked_at=1790000000000.0)},
        "hardware_inventory": INVENTORY,
        "register_companion": {"companion": INVENTORY["companions"][0], "credential": _credential("hardware"),
                               "secret": SECRET, "authorization": AUTH},
        "rotate_companion": {"credential": _credential("hardware"), "secret": SECRET, "authorization": AUTH,
                             "revoked": ["tagc_old"]},
        "delete_companion": {"deleted": True},
        "create_command": {"command": _command("scan_unprovisioned", args={"duration_s": 120})},
        "get_command": {"command": _command(status="succeeded", result={"found": [{"uuid": "8f3e"}]})},
        "claim_tag": {"device": _device(KITCHEN, "Desk", "5E6F7A8B", owner_profile="bob", epoch=4),
                      "commands": [_command("assign_tag"), _command("clear_tag")]},
        "assign_tag": {"device": _device(DESK, "Desk", "1A2B3C4D", epoch=4), "command": _command("assign_tag")},
        "release_tag": {"device": _device(DESK, "Desk", "1A2B3C4D", owner_profile=None, epoch=5),
                        "commands": [_command("clear_tag")]},
        "rename_hardware_device": {"device": {**_device(BRIDGE, "Hall", "br-9f"), "kind": "bridge"}},
        "delete_hardware_device": {"deleted": True, "device": _device(KITCHEN, "", "5E6F7A8B")},
        "get_defaults": {"defaults": {"language": "vi", "routes": {"usage": "all"}},
                         "builtin": {"layout": "status", "language": "en",
                                     "routes": {"notification": "all", "usage": "none"}}},
        "put_defaults": {"defaults": {"language": "en"}, "builtin": {"layout": "status", "routes": {}}},
    }


@pytest.fixture
def api(monkeypatch):
    """Every client wrapper replaced by a recording fake. ``state["raise"]``
    maps a wrapper name to the exception it raises instead."""
    import app.cli.client.tags as c

    state: dict = {"calls": [], "raise": {}, "responses": _responses()}

    def make(name: str):
        async def fake(client, *args, **kwargs):
            state["calls"].append((name, args, kwargs))
            if name in state["raise"]:
                raise state["raise"][name]
            return copy.deepcopy(state["responses"][name])
        return fake

    for name in state["responses"]:
        assert hasattr(c, name), name
        monkeypatch.setattr(c, name, make(name))
    return state


def _run(*args, input: str | None = None):
    from app.cli.main import app

    return CliRunner().invoke(app, ["--token", "t", *args], input=input)


def _calls(state, name):
    return [(a, k) for n, a, k in state["calls"] if n == name]


def _api_error(status: int, payload: dict):
    from app.cli.client._base import APIError

    return APIError(status=status, body=str(payload.get("error", "")), raw=json.dumps(payload).encode())


def _tag_error(status: int, code: str, message: str, **extra):
    return _api_error(status, {"error": code, "message": message, "detail": message, **extra})


# ── profile: tags ──────────────────────────────────────────────────────────


def test_list_prints_counts_and_the_tags(api):
    result = _run("tags", "list")
    assert result.exit_code == 0, result.output
    for text in ("enabled", "yes", "needs input", DESK, "Desk", "Kitchen", "2.90 V", "rev 17 (18 pending)"):
        assert text in result.output, text


def test_list_says_how_to_turn_tags_on(api):
    api["responses"]["overview"]["enabled"] = False
    api["responses"]["overview"]["devices"] = []
    result = _run("tags", "list")
    assert result.exit_code == 0, result.output
    assert "cremind tags set --enable" in result.output
    assert "cremind tags hardware claim" in result.output


def test_json_mode_is_the_root_flag_and_prints_the_server_object(api):
    result = _run("--json", "tags", "list")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == _responses()["overview"]


def test_show_resolves_a_name_to_the_id(api):
    result = _run("tags", "show", "desk")
    assert result.exit_code == 0, result.output
    assert _calls(api, "get_device") == [((DESK,), {})]
    assert "1A2B3C4D" in result.output and "recent deliveries" in result.output


def test_a_full_id_is_sent_without_a_lookup(api):
    result = _run("tags", "show", KITCHEN)
    assert result.exit_code == 0, result.output
    assert _calls(api, "overview") == []
    assert _calls(api, "get_device") == [((KITCHEN,), {})]


@pytest.mark.parametrize("ref, resolved", [("5e6f7a8b", KITCHEN), ("KITCHEN", KITCHEN), (DESK[:12], DESK)])
def test_a_tag_is_named_by_hardware_id_name_or_prefix(api, ref, resolved):
    result = _run("tags", "refresh", ref)
    assert result.exit_code == 0, result.output
    assert _calls(api, "refresh") == [((resolved,), {})]


def test_an_ambiguous_prefix_or_an_unknown_name_fails_before_any_write(api):
    ambiguous = _run("tags", "clear", "3f2a9c1e")
    assert ambiguous.exit_code == 1
    assert "matches several tags" in ambiguous.output
    unknown = _run("tags", "clear", "garage")
    assert unknown.exit_code == 1
    assert "no tag matches 'garage'" in unknown.output and "cremind tags list" in unknown.output
    assert _calls(api, "clear") == []


def test_another_profiles_tag_is_a_plain_404_with_a_hint(api):
    api["raise"]["get_device"] = _tag_error(404, "device_not_found", "No tag with that id.")
    result = _run("tags", "show", KITCHEN)
    assert result.exit_code == 1
    assert "404: device_not_found: No tag with that id." in result.output
    assert "cremind tags list" in result.output


def test_settings_shows_each_value_and_where_it_comes_from(api):
    result = _run("tags", "settings")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Asia/Ho_Chi_Minh" in out and "(the profile's own)" in out
    assert "profile" in out and "admin default" in out and "built-in" in out
    assert "push_pin" in out


def test_set_merges_into_the_profiles_own_overrides(api):
    result = _run(
        "tags", "set", "--layout", "status", "--no-excerpts", "--timezone", "own",
        "--route", "needs_input=Kitchen,Desk", "--route", "usage=inherit", "--inherit", "language",
    )
    assert result.exit_code == 0, result.output
    [(args, _)] = _calls(api, "put_settings")
    assert args == ({"options": {
        "layout": "status", "show_excerpts": False, "timezone": "",
        "routes": {"notification": "none", "needs_input": [KITCHEN, DESK]},
    }},)


def test_set_enable_alone_leaves_the_options_untouched(api):
    result = _run("tags", "set", "--enable")
    assert result.exit_code == 0, result.output
    assert _calls(api, "put_settings") == [(({"enabled": True},), {})]
    assert _calls(api, "get_settings") == []


def test_set_with_nothing_to_change_is_refused(api):
    result = _run("tags", "set")
    assert result.exit_code == 1
    assert "nothing to change" in result.output
    assert api["calls"] == []


def test_set_rejects_an_unknown_card_kind_before_writing(api):
    result = _run("tags", "set", "--route", "weather=all")
    assert result.exit_code == 1
    assert "unknown card kind weather" in result.output
    assert _calls(api, "put_settings") == []


def test_set_prints_each_rejected_field(api):
    api["raise"]["put_settings"] = _tag_error(
        422, "invalid_settings", "progress_cadence_s: must be a number of seconds between 60 and 3600",
        details={"progress_cadence_s": "must be a number of seconds between 60 and 3600"},
    )
    result = _run("tags", "set", "--progress-cadence", "5")
    assert result.exit_code == 1
    assert "invalid_settings" in result.output
    assert "  progress_cadence_s: must be a number of seconds" in result.output


def test_rename(api):
    result = _run("tags", "rename", "desk", "Office")
    assert result.exit_code == 0, result.output
    assert _calls(api, "rename_device") == [((DESK, "Office"), {})]
    assert "Office" in result.output


def test_display_sends_the_note_with_a_body_file(api, tmp_path):
    note = tmp_path / "note.txt"
    note.write_text("Ring the bell\n", encoding="utf-8")
    result = _run("tags", "display", "desk", "Back at 3", "-f", str(note), "--icon", "info", "--ttl", "2h")
    assert result.exit_code == 0, result.output
    assert _calls(api, "display") == [((DESK, {"title": "Back at 3", "body": "Ring the bell",
                                               "icon": "info", "ttl_s": 7200}), {})]
    assert "delivery 501" in result.output and "cremind tags deliveries show 501" in result.output


def test_display_reads_the_body_from_stdin(api):
    result = _run("tags", "display", DESK, "Standup", "--body-file", "-", input="line one\nline two\n")
    assert result.exit_code == 0, result.output
    [(args, _)] = _calls(api, "display")
    assert args[1] == {"title": "Standup", "body": "line one\nline two"}


@pytest.mark.parametrize("argv, message", [
    (["--body", "x", "--body-file", "-"], "not both"),
    (["--ttl", "soon"], "--ttl takes seconds"),
])
def test_display_refuses_bad_input_locally(api, argv, message):
    result = _run("tags", "display", DESK, "Title", *argv)
    assert result.exit_code == 1
    assert message in result.output
    assert _calls(api, "display") == []


def test_display_explains_an_otp_refusal(api):
    api["raise"]["display"] = _tag_error(
        422, "otp_refused", "The text looks like it contains a one-time code; codes are never shown on a tag.",
    )
    result = _run("tags", "display", DESK, "Your code is 482913")
    assert result.exit_code == 1
    assert "otp_refused" in result.output and "one-time code" in result.output
    assert "Nothing was sent" in result.output


def test_display_explains_a_pending_clear(api):
    api["raise"]["display"] = _tag_error(
        409, "clear_pending", "The tag's screen is still being cleared after a change of owner; try again shortly.",
    )
    result = _run("tags", "display", DESK, "Hello")
    assert result.exit_code == 1
    assert "409: clear_pending" in result.output and "Try again in a minute" in result.output


def test_clear_refresh_identify(api):
    cleared = _run("tags", "clear", "desk")
    assert cleared.exit_code == 0, cleared.output
    assert "delivery 502 queued" in cleared.output
    refreshed = _run("tags", "refresh", "desk")
    identified = _run("tags", "identify", "desk")
    assert refreshed.exit_code == identified.exit_code == 0
    assert _calls(api, "clear") == [((DESK,), {})]
    assert _calls(api, "refresh") == [((DESK,), {})]
    assert _calls(api, "identify") == [((DESK,), {})]
    assert f"command {CMD}" in identified.output


def test_preview_writes_the_png(api, tmp_path):
    target = tmp_path / "desk.png"
    result = _run("tags", "preview", "desk", "--kind", "desired", "--out", str(target))
    assert result.exit_code == 0, result.output
    assert target.read_bytes() == b"\x89PNG\r\n\x1a\nfake"
    assert _calls(api, "get_preview") == [((DESK,), {"kind": "desired"})]
    assert "revision 17" in result.output


def test_preview_json_reports_the_file(api, tmp_path):
    target = tmp_path / "p.png"
    result = _run("--json", "tags", "preview", DESK, "-o", str(target))
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"path": str(target), "kind": "displayed", "revision": 17, "bytes": 12}


def test_a_missing_preview_leaves_no_file(api, tmp_path):
    api["raise"]["get_preview"] = _tag_error(404, "no_preview", "The companion has not sent a displayed preview yet.")
    target = tmp_path / "none.png"
    result = _run("tags", "preview", DESK, "--out", str(target))
    assert result.exit_code == 1
    assert not target.exists()
    assert "no_preview" in result.output


def test_preview_rejects_an_unknown_kind(api):
    result = _run("tags", "preview", DESK, "--kind", "old")
    assert result.exit_code == 1
    assert _calls(api, "get_preview") == []


def test_companions(api):
    result = _run("tags", "companions")
    assert result.exit_code == 0, result.output
    assert COMP in result.output and "Desk PC" in result.output


# ── profile: deliveries ────────────────────────────────────────────────────


def test_deliveries_list_passes_the_filters_and_prints_the_next_page(api):
    result = _run("tags", "deliveries", "list", "--device", "desk", "--state", "active",
                  "--limit", "2", "--before", "950")
    assert result.exit_code == 0, result.output
    assert _calls(api, "list_deliveries") == [
        ((), {"device": DESK, "state": "active", "limit": 2, "before": 950}),
    ]
    assert "older: cremind tags deliveries list --before 880 --device desk --state active" in result.output
    assert "Back at 3" in result.output and "Desk" in result.output


def test_deliveries_list_json_skips_the_name_lookup(api):
    result = _run("--json", "tags", "deliveries", "list")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["next_before"] == 880
    assert _calls(api, "overview") == []


def test_deliveries_show(api):
    result = _run("tags", "deliveries", "show", "501")
    assert result.exit_code == 0, result.output
    assert _calls(api, "get_delivery") == [((501,), {})]
    assert "displayed" in result.output and "Back at 3" in result.output


def test_deliveries_cancel_and_the_already_finished_case(api):
    ok = _run("tags", "deliveries", "cancel", "501")
    assert ok.exit_code == 0, ok.output
    assert "cancelled delivery 501" in ok.output
    api["raise"]["cancel_delivery"] = _tag_error(409, "already_terminal", "The delivery already finished (displayed).",
                                                 delivery=_delivery(stage="displayed"))
    done = _run("tags", "deliveries", "cancel", "501")
    assert done.exit_code == 1
    assert "already_terminal" in done.output and "Nothing to cancel." in done.output


# ── profile: credentials ───────────────────────────────────────────────────


def test_credentials_list_never_shows_a_secret(api):
    result = _run("tags", "credentials", "list")
    assert result.exit_code == 0, result.output
    assert "tagc_abcdefghijklmnopqrstuvwxyz" in result.output and "revoked" in result.output
    assert SECRET not in result.output


def test_credentials_create_prints_the_secret_once_with_a_warning(api):
    result = _run("tags", "credentials", "create", "--label", "Desk")
    assert result.exit_code == 0, result.output
    assert _calls(api, "create_credential") == [((COMP, "Desk"), {})]
    assert result.stdout.count(SECRET) == 1
    assert AUTH in result.stdout
    assert "shown ONCE" in result.stderr and "cremind-tag connect" in result.stderr


def test_credentials_create_json_carries_the_secret_once(api):
    result = _run("--json", "tags", "credentials", "create", "--companion", "desk pc")
    assert result.exit_code == 0, result.output
    assert result.output.count(SECRET) == 2  # `secret` and inside `authorization`, one object
    assert json.loads(result.stdout)["secret"] == SECRET


def test_credentials_create_asks_which_companion_when_there_are_several(api):
    api["responses"]["list_companions"]["companions"].append({**COMPANION, "id": COMP2, "name": "Lab PC"})
    result = _run("tags", "credentials", "create")
    assert result.exit_code == 1
    assert "--companion" in result.output and "Lab PC" in result.output
    assert _calls(api, "create_credential") == []


def test_credentials_revoke(api):
    result = _run("tags", "credentials", "revoke", "tagc_abcdefghijklmnopqrstuvwxyz")
    assert result.exit_code == 0, result.output
    assert _calls(api, "revoke_credential") == [(("tagc_abcdefghijklmnopqrstuvwxyz",), {})]


# ── hardware (admin) ────────────────────────────────────────────────────────


@pytest.mark.parametrize("argv", [
    ["list"], ["claim", "1A2B3C4D", "--owner", "bob"], ["run", "collect_diagnostics"],
])
def test_a_non_admin_is_told_to_run_hardware_as_admin(api, argv):
    api["raise"]["hardware_inventory"] = _api_error(403, {"error": "Admin profile required"})
    api["raise"]["create_command"] = _api_error(403, {"error": "Admin profile required"})
    result = _run("tags", "hardware", *argv)
    assert result.exit_code == 1
    assert "needs the admin profile" in result.output
    assert "cremind -p admin tags hardware list" in result.output


def test_hardware_list(api):
    result = _run("tags", "hardware", "list")
    assert result.exit_code == 0, result.output
    for text in ("companions", "devices", "commands", "desk-pc", "1 active", "br-9f", "unclaimed", CMD):
        assert text in result.output, text


def test_hardware_status(api):
    result = _run("tags", "hardware", "status", CMD)
    assert result.exit_code == 0, result.output
    assert _calls(api, "get_command") == [((CMD,), {})]
    assert "succeeded" in result.output and '"uuid": "8f3e"' in result.output


def test_hardware_register_prints_the_secret_once(api):
    result = _run("tags", "hardware", "register", "Desk PC")
    assert result.exit_code == 0, result.output
    assert _calls(api, "register_companion") == [(("Desk PC",), {})]
    assert result.stdout.count(SECRET) == 1
    assert "shown ONCE" in result.stderr


def test_hardware_rotate(api):
    result = _run("tags", "hardware", "rotate", "desk pc")
    assert result.exit_code == 0, result.output
    assert _calls(api, "rotate_companion") == [((COMP,), {})]
    assert "revoked: tagc_old" in result.output and result.stdout.count(SECRET) == 1


def test_hardware_remove_changes_nothing_without_yes(api):
    dry = _run("tags", "hardware", "remove", "Desk PC")
    assert dry.exit_code == 2
    assert "3 device(s)" in dry.output and "Re-run with --yes" in dry.output
    assert _calls(api, "delete_companion") == []
    done = _run("tags", "hardware", "remove", "Desk PC", "--yes")
    assert done.exit_code == 0, done.output
    assert _calls(api, "delete_companion") == [((COMP,), {})]


def test_hardware_run_builds_the_command_arguments(api):
    result = _run("tags", "hardware", "run", "scan-unprovisioned", "--duration", "120")
    assert result.exit_code == 0, result.output
    assert _calls(api, "create_command") == [((COMP, "scan_unprovisioned", {"duration_s": 120}), {})]
    assert f"cremind tags hardware status {CMD}" in result.output


def test_hardware_run_provision_bridge(api):
    result = _run("tags", "hardware", "run", "provision_bridge", "--companion", COMP,
                  "--uuid", "8f3e1c0a", "--name", "Hall")
    assert result.exit_code == 0, result.output
    assert _calls(api, "create_command") == [((COMP, "provision_bridge", {"uuid": "8f3e1c0a", "name": "Hall"}), {})]
    assert _calls(api, "hardware_inventory") == []


def test_hardware_run_points_ownership_kinds_to_claim(api):
    api["raise"]["create_command"] = _tag_error(
        422, "use_tag_endpoint", "'assign_tag' is queued by the claim / assign / release endpoints.",
    )
    result = _run("tags", "hardware", "run", "assign_tag")
    assert result.exit_code == 1
    assert "use_tag_endpoint" in result.output and "cremind tags hardware claim" in result.output


def test_hardware_claim_resolves_the_tag_and_bridge_by_hardware_id(api):
    result = _run("tags", "hardware", "claim", "5E6F7A8B", "--owner", "bob", "--bridge", "br-9f", "--name", "Desk")
    assert result.exit_code == 0, result.output
    assert _calls(api, "claim_tag") == [((KITCHEN, "bob"), {"bridge_id": BRIDGE, "name": "Desk"})]
    assert len(_calls(api, "hardware_inventory")) == 1
    assert "now belongs to bob" in result.output and "assign_tag" in result.output


def test_hardware_claim_explains_a_missing_bridge_and_an_unknown_profile(api):
    api["raise"]["claim_tag"] = _tag_error(409, "bridge_required", "Name the bridge to assign the tag to.")
    result = _run("tags", "hardware", "claim", KITCHEN, "--owner", "bob")
    assert result.exit_code == 1
    assert "bridge_required" in result.output and "--bridge" in result.output
    api["raise"]["claim_tag"] = _tag_error(422, "unknown_profile", "No profile named 'bobo'.")
    result = _run("tags", "hardware", "claim", KITCHEN, "--owner", "bobo")
    assert result.exit_code == 1
    assert "cremind profile list" in result.output


def test_hardware_assign_release_rename(api):
    assert _run("tags", "hardware", "assign", "1A2B3C4D", "--bridge", "Hall bridge").exit_code == 0
    assert _calls(api, "assign_tag") == [((DESK, BRIDGE), {})]
    assert _run("tags", "hardware", "release", "Desk").exit_code == 0
    assert _calls(api, "release_tag") == [((DESK,), {})]
    renamed = _run("tags", "hardware", "rename", "br-9f", "Hall")
    assert renamed.exit_code == 0, renamed.output
    assert _calls(api, "rename_hardware_device") == [((BRIDGE, "Hall"), {})]


def test_hardware_forget_changes_nothing_without_yes(api):
    dry = _run("tags", "hardware", "forget", "Desk")
    assert dry.exit_code == 2
    assert "It still belongs to alice" in dry.output and "Re-run with --yes" in dry.output
    assert _calls(api, "delete_hardware_device") == []
    done = _run("tags", "hardware", "forget", "Desk", "--yes")
    assert done.exit_code == 0, done.output
    assert _calls(api, "delete_hardware_device") == [((DESK,), {})]


def test_hardware_defaults_and_set_defaults(api):
    shown = _run("tags", "hardware", "defaults")
    assert shown.exit_code == 0, shown.output
    assert "ADMIN DEFAULT" in shown.output or "admin default" in shown.output.lower()
    result = _run("tags", "hardware", "set-defaults", "--qr-links", "--route", "usage=inherit",
                  "--route", "notification=none", "--inherit", "language")
    assert result.exit_code == 0, result.output
    assert _calls(api, "put_defaults") == [(({"qr_links": True, "routes": {"notification": "none"}},), {})]


def test_set_defaults_with_nothing_to_change_is_refused(api):
    result = _run("tags", "hardware", "set-defaults")
    assert result.exit_code == 1
    assert api["calls"] == []


# ── the client wrappers ────────────────────────────────────────────────────


def test_the_client_builds_the_documented_requests(tmp_path):
    """The wrappers themselves: method, path (ids escaped), query and body."""
    import asyncio

    import app.cli.client.tags as c

    seen = []

    class _Client:
        async def get_json(self, path, *, params=None):
            seen.append(("GET", path, params))
            return {}

        async def get_bytes(self, path, *, params=None):
            seen.append(("GET", path, params))
            return b"png", {"x-tag-revision": "9"}

        async def post_json(self, path, body=None, *, params=None):
            seen.append(("POST", path, body))
            return {}

        async def put_json(self, path, body=None, *, params=None):
            seen.append(("PUT", path, body))
            return {}

        async def patch_json(self, path, body=None, *, params=None):
            seen.append(("PATCH", path, body))
            return {}

        async def delete(self, path, body=None, *, params=None):
            seen.append(("DELETE", path, body))
            return {}

    async def go():
        cl = _Client()
        await c.overview(cl)
        await c.get_settings(cl)
        await c.put_settings(cl, {"enabled": True})
        await c.get_device(cl, "a/b")
        await c.rename_device(cl, "d1", "Desk")
        await c.display(cl, "d1", {"title": "t"})
        await c.clear(cl, "d1")
        await c.refresh(cl, "d1")
        await c.identify(cl, "d1")
        saved = await c.download_preview(cl, "d1", tmp_path / "p.png", kind="desired")
        await c.list_deliveries(cl, device="d1", state="active", limit=5, before=900)
        await c.list_deliveries(cl)
        await c.get_delivery(cl, 501)
        await c.cancel_delivery(cl, 501)
        await c.list_companions(cl)
        await c.list_credentials(cl)
        await c.create_credential(cl, "c1", "Desk")
        await c.create_credential(cl, "c1")
        await c.revoke_credential(cl, "tagc_x")
        await c.hardware_inventory(cl)
        await c.register_companion(cl, "Desk PC")
        await c.rotate_companion(cl, "c1")
        await c.delete_companion(cl, "c1")
        await c.create_command(cl, "c1", "scan_unprovisioned", {"duration_s": 60})
        await c.create_command(cl, "c1", "collect_diagnostics")
        await c.get_command(cl, "k1")
        await c.claim_tag(cl, "t1", "bob", bridge_id="b1", name="Desk")
        await c.claim_tag(cl, "t1", "bob")
        await c.assign_tag(cl, "t1", "b1")
        await c.release_tag(cl, "t1")
        await c.rename_hardware_device(cl, "t1", "Desk")
        await c.delete_hardware_device(cl, "t1")
        await c.get_defaults(cl)
        await c.put_defaults(cl, {"language": "vi"})
        return saved

    saved = asyncio.run(go())
    assert saved == {"path": str(tmp_path / "p.png"), "kind": "desired", "revision": 9, "bytes": 3}
    assert (tmp_path / "p.png").read_bytes() == b"png"
    assert seen == [
        ("GET", "/api/tags", None),
        ("GET", "/api/tags/settings", None),
        ("PUT", "/api/tags/settings", {"enabled": True}),
        ("GET", "/api/tags/devices/a%2Fb", None),
        ("PATCH", "/api/tags/devices/d1", {"name": "Desk"}),
        ("POST", "/api/tags/devices/d1/display", {"title": "t"}),
        ("POST", "/api/tags/devices/d1/clear", {}),
        ("POST", "/api/tags/devices/d1/refresh", {}),
        ("POST", "/api/tags/devices/d1/identify", {}),
        ("GET", "/api/tags/devices/d1/preview", {"kind": "desired"}),
        ("GET", "/api/tags/deliveries", {"device": "d1", "state": "active", "limit": 5, "before": 900}),
        ("GET", "/api/tags/deliveries", None),
        ("GET", "/api/tags/deliveries/501", None),
        ("POST", "/api/tags/deliveries/501/cancel", {}),
        ("GET", "/api/tags/companions", None),
        ("GET", "/api/tags/credentials", None),
        ("POST", "/api/tags/credentials", {"companion_id": "c1", "label": "Desk"}),
        ("POST", "/api/tags/credentials", {"companion_id": "c1"}),
        ("DELETE", "/api/tags/credentials/tagc_x", None),
        ("GET", "/api/tags/hardware", None),
        ("POST", "/api/tags/hardware/companions", {"name": "Desk PC"}),
        ("POST", "/api/tags/hardware/companions/c1/rotate", {}),
        ("DELETE", "/api/tags/hardware/companions/c1", None),
        ("POST", "/api/tags/hardware/commands",
         {"companion_id": "c1", "kind": "scan_unprovisioned", "args": {"duration_s": 60}}),
        ("POST", "/api/tags/hardware/commands", {"companion_id": "c1", "kind": "collect_diagnostics"}),
        ("GET", "/api/tags/hardware/commands/k1", None),
        ("POST", "/api/tags/hardware/tags/t1/claim", {"owner": "bob", "bridge_id": "b1", "name": "Desk"}),
        ("POST", "/api/tags/hardware/tags/t1/claim", {"owner": "bob"}),
        ("POST", "/api/tags/hardware/tags/t1/assign", {"bridge_id": "b1"}),
        ("POST", "/api/tags/hardware/tags/t1/release", {}),
        ("PATCH", "/api/tags/hardware/devices/t1", {"name": "Desk"}),
        ("DELETE", "/api/tags/hardware/devices/t1", None),
        ("GET", "/api/tags/hardware/defaults", None),
        ("PUT", "/api/tags/hardware/defaults", {"defaults": {"language": "vi"}}),
    ]


def test_timestamps_are_read_as_milliseconds():
    from datetime import datetime

    from app.cli.commands.tags import _fmt_ts

    ms = 1790000000000.0
    assert _fmt_ts(ms) == datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    assert _fmt_ts(None) == ""


def test_the_cli_imports_no_server_module():
    """CLAUDE.md's import discipline: the slim `pip install cremind` has no
    server dependencies, so no CLI module may import one at top level."""
    code = (
        "import sys, app.cli.main, app.cli.commands.tags, app.cli.client.tags\n"
        "bad = ('app.server', 'app.api', 'app.tools', 'app.agent', 'app.skills', 'app.events', "
        "'app.cremind_documents', 'app.channels', 'app.databases', 'app.storage', 'app.documents', "
        "'app.tags')\n"
        "print(sorted(m for m in sys.modules if m.startswith(bad)))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "[]"
